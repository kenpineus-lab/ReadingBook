"""
Book Lab server

One database, many households. A household is the tenant: it owns its readers,
their books, their settings and its own join code. Nothing is global except the
Anthropic key and the operator's admin code.

- POST /ai              : proxies to Anthropic (the API key lives here, never in the browser)
- GET  /me              : who this code belongs to - household name and its readers
- GET/POST /books, DELETE /books/{id}, GET /books/{id}/cover
- POST /members, DELETE /members/{id}
- POST /backup          : JSON snapshot to a private GitHub repo (also runs daily)
- POST/GET /households  : operator only (X-Admin-Code), for handing out codes

Env vars (set in Railway):
  ANTHROPIC_API_KEY   required
  FAMILY_CODE         the first household's code; used once, to migrate the original family
  ADMIN_CODE          required to create households; without it no new household can exist
  ALLOWED_ORIGIN      e.g. https://kenpineus-lab.github.io   (comma-separate for several)
  GITHUB_TOKEN        optional - fine-grained token with contents:write on the backup repo
  GITHUB_REPO         optional - e.g. kenpineus-lab/ReadingBook-backup
  DB_PATH             optional - default /data/booklab.db (mount a Railway volume at /data)
  AI_DAILY_CAP        optional - default calls/day per household (default 300)
"""
import os, io, re, json, sqlite3, base64, hashlib, secrets, asyncio, datetime
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx

API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
FAMILY_CODE = os.environ.get("FAMILY_CODE", "")
ADMIN_CODE = os.environ.get("ADMIN_CODE", "")
ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGIN", "*").split(",")]
GH_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GH_REPO = os.environ.get("GITHUB_REPO", "")
DB_PATH = os.environ.get("DB_PATH", "/data/booklab.db")
AI_DAILY_CAP = int(os.environ.get("AI_DAILY_CAP", "300"))

CODE_MIN = 6
ICON_MAX = 12          # a couple of emoji, including any variation selectors
NAME_MAX = 24
COVER_MAX = 1024       # longest edge kept for the full cover
THUMB_MAX = 160        # what a library row actually draws (38x52 css px, retina)
MEMBER_MAX = 12        # readers per household

# The family this was built for. Used once, to carry the original data across.
SEED_MEMBERS = [
    ("henry",   "Henry",   "\U0001f43b", "a 3rd grader", 0),
    ("jeremy",  "Jeremy",  "\U0001f697", "an 8th grader", 0),
    ("rebecca", "Rebecca", "\U0001f338", "an adult reader", 1),
    ("han",     "Han",     "☕",     "an adult reader", 1),
]

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS households (
        hh INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        code_hash TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        ai_cap INTEGER NOT NULL DEFAULT 300,
        backup INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS members (
        hh INTEGER NOT NULL,
        id TEXT NOT NULL,
        name TEXT NOT NULL,
        icon TEXT NOT NULL DEFAULT '',
        level TEXT NOT NULL DEFAULT 'a young reader',
        level_en TEXT,
        is_adult INTEGER NOT NULL DEFAULT 0,
        pos INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (hh, id)
    )""",
    """CREATE TABLE IF NOT EXISTS books (
        hh INTEGER NOT NULL,
        profile TEXT NOT NULL,
        id INTEGER NOT NULL,
        title TEXT NOT NULL,
        author TEXT,
        date TEXT NOT NULL,
        cover TEXT,
        data TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (hh, profile, id)
    )""",
    """CREATE TABLE IF NOT EXISTS settings (
        hh INTEGER NOT NULL,
        k TEXT NOT NULL,
        v TEXT NOT NULL,
        PRIMARY KEY (hh, k)
    )""",
    """CREATE TABLE IF NOT EXISTS ai_usage (
        hh INTEGER NOT NULL,
        day TEXT NOT NULL,
        n INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (hh, day)
    )""",
]


# ----------------------------------------------------------------- images

def _resize(data_url: str, longest: int, quality: int) -> str | None:
    """Re-encode a data: URL down to `longest` px. None if it isn't a usable image."""
    try:
        from PIL import Image
    except Exception:
        return None
    if not data_url or "," not in data_url:
        return None
    try:
        raw = base64.b64decode(data_url.split(",", 1)[1])
        im = Image.open(io.BytesIO(raw))
        im.load()
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        w, h = im.size
        k = min(1.0, longest / float(max(w, h)))
        if k < 1.0:
            im = im.resize((max(1, int(w * k)), max(1, int(h * k))), Image.LANCZOS)
        out = io.BytesIO()
        im.save(out, "JPEG", quality=quality, optimize=True)
        return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode("ascii")
    except Exception:
        return None


def _normalise_images(d: dict) -> dict:
    """A saved book keeps a reasonable cover and a tiny thumbnail. The list
    endpoint sends only the thumbnail - the full cover is never in a list."""
    cov = d.get("cover")
    if not cov:
        return d
    smaller = _resize(cov, COVER_MAX, 82)
    if smaller and len(smaller) < len(cov):
        d["cover"] = smaller
    d["thumb"] = _resize(d["cover"], THUMB_MAX, 72) or d.get("thumb")
    return d


# ----------------------------------------------------------------- schema

def _global(con, k, default=None):
    r = con.execute("SELECT v FROM settings WHERE hh=0 AND k=?", (k,)).fetchone()
    return r["v"] if r else default


def _set_global(con, k, v):
    con.execute("INSERT OR REPLACE INTO settings (hh, k, v) VALUES (0,?,?)", (k, v))


def _salt(con) -> str:
    s = _global(con, "code_salt")
    if not s:
        s = secrets.token_hex(16)
        _set_global(con, "code_salt", s)
        con.commit()
    return s


def code_hash(con, code: str) -> str:
    return hashlib.sha256((_salt(con) + ":" + code).encode("utf-8")).hexdigest()


def _migrate_v3_to_v4(con):
    """The single-family database becomes household 1. Books, icons and the
    original FAMILY_CODE all move across; nothing is thrown away."""
    old_books = con.execute("SELECT * FROM books_v3").fetchall()
    old_settings = {r["k"]: r["v"] for r in con.execute("SELECT k, v FROM settings_v3")}

    code = FAMILY_CODE or secrets.token_hex(4)
    con.execute("INSERT INTO households (name, code_hash, created_at, ai_cap, backup) VALUES (?,?,?,?,1)",
                ("Han's family", code_hash(con, code), datetime.datetime.utcnow().isoformat(), AI_DAILY_CAP))
    hh = con.execute("SELECT hh FROM households ORDER BY hh DESC LIMIT 1").fetchone()["hh"]

    for pos, (mid, name, icon, level, adult) in enumerate(SEED_MEMBERS):
        con.execute("INSERT OR REPLACE INTO members (hh, id, name, icon, level, level_en, is_adult, pos) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (hh, mid, name, old_settings.get("icon:" + mid, icon), level,
                     ("a 3rd grader who reads English about two grades ahead, so pitch the questions "
                      "and the writing at a 5th-grade level") if mid == "henry" else None,
                     adult, pos))

    known = {m[0] for m in SEED_MEMBERS}
    for r in old_books:
        p = r["profile"] if r["profile"] in known else "henry"
        con.execute("INSERT OR REPLACE INTO books (hh, profile, id, title, author, date, cover, data, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (hh, p, r["id"], r["title"], r["author"], r["date"], r["cover"], r["data"], r["created_at"]))
    for k, v in old_settings.items():
        if k.startswith("icon:"):
            continue                      # icons now live on the member row
        if k in ("covers_compacted", "code_salt"):
            _set_global(con, k, v)        # server-wide, not this household's
            continue
        con.execute("INSERT OR REPLACE INTO settings (hh, k, v) VALUES (?,?,?)", (hh, k, v))

    con.execute("DROP TABLE books_v3")
    con.execute("DROP TABLE settings_v3")
    _set_global(con, "migrated_v4", datetime.datetime.utcnow().isoformat())
    con.commit()


def _ensure_schema(con):
    books_cols = [r["name"] for r in con.execute("PRAGMA table_info(books)")]
    needs_move = bool(books_cols) and "hh" not in books_cols
    if needs_move:
        # keep the old tables aside, build the new shape, then carry the data over
        con.execute("ALTER TABLE books RENAME TO books_v3")
        if [r["name"] for r in con.execute("PRAGMA table_info(settings)")]:
            con.execute("ALTER TABLE settings RENAME TO settings_v3")
        else:
            con.execute("CREATE TABLE settings_v3 (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
    for stmt in SCHEMA:
        con.execute(stmt)
    con.commit()
    if needs_move:
        _migrate_v3_to_v4(con)


def db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    _ensure_schema(con)
    return con


# ----------------------------------------------------------------- auth

def house(code: str | None):
    """Resolve the join code to a household, or refuse. Every request does this;
    nothing in this server is reachable without belonging to somebody."""
    con = db()
    if not code or len(code.strip()) < CODE_MIN:
        raise HTTPException(401, "wrong family code")
    r = con.execute("SELECT * FROM households WHERE code_hash=?", (code_hash(con, code.strip()),)).fetchone()
    if not r:
        raise HTTPException(401, "wrong family code")
    return con, r


def admin(code: str | None):
    if not ADMIN_CODE or not code or not secrets.compare_digest(code, ADMIN_CODE):
        raise HTTPException(401, "not the operator")


def members_of(con, hh) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT id, name, icon, level, level_en, is_adult FROM members WHERE hh=? ORDER BY pos, id", (hh,))]


def prof(con, hh, p: str | None) -> str:
    ids = [m["id"] for m in members_of(con, hh)]
    p = (p or (ids[0] if ids else "")).strip().lower()
    if p not in ids:
        raise HTTPException(400, "unknown reader for this household")
    return p


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return s[:24] or "reader"


# ----------------------------------------------------------------- backup

async def _backup_household(con, hh_row) -> dict:
    hh = hh_row["hh"]
    rows = con.execute("SELECT profile, data FROM books WHERE hh=? ORDER BY profile, id", (hh,)).fetchall()
    payload = []
    for r in rows:
        d = json.loads(r["data"])
        d.pop("cover", None)      # keep the backup small; covers stay in the DB
        d.pop("thumb", None)
        d["profile"] = r["profile"]
        payload.append(d)
    body = json.dumps({"household": hh_row["name"],
                       "exported": datetime.datetime.utcnow().isoformat() + "Z",
                       "count": len(payload), "books": payload}, ensure_ascii=False, indent=2)
    path = "books-%d-%s.json" % (hh, slug(hh_row["name"]))
    url = f"https://api.github.com/repos/{GH_REPO}/contents/{path}"
    headers = {"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"}
    async with httpx.AsyncClient(timeout=30) as c:
        sha = None
        r = await c.get(url, headers=headers)
        if r.status_code == 200:
            sha = r.json().get("sha")
        put = {"message": f"backup {datetime.date.today().isoformat()} - {hh_row['name']} ({len(payload)} books)",
               "content": base64.b64encode(body.encode("utf-8")).decode("ascii")}
        if sha:
            put["sha"] = sha
        r = await c.put(url, headers=headers, json=put)
        if r.status_code not in (200, 201):
            raise HTTPException(502, f"github backup failed: {r.text[:200]}")
    return {"ok": True, "count": len(payload), "file": path}


async def daily_backup_loop():
    while True:
        await asyncio.sleep(24 * 3600)
        if not (GH_TOKEN and GH_REPO):
            continue
        try:
            con = db()
            # only households that asked for it - another family's writing does
            # not belong in the operator's repo by default
            for r in con.execute("SELECT * FROM households WHERE backup=1").fetchall():
                await _backup_household(con, r)
        except Exception:
            pass


def _compact_covers_sync():
    """The covers saved before the phone-side shrink are 1-9MB each, which made
    switching profiles a 50MB download. Rewrite them once, in the background."""
    con = db()
    if _global(con, "covers_compacted"):
        return "already done"
    rows = con.execute("SELECT hh, profile, id, data FROM books").fetchall()
    done = saved = 0
    for r in rows:
        try:
            d = json.loads(r["data"])
        except Exception:
            continue
        before = len(r["data"])
        if not d.get("cover"):
            continue
        d = _normalise_images(d)
        blob = json.dumps(d, ensure_ascii=False)
        if len(blob) < before:
            con.execute("UPDATE books SET cover=?, data=? WHERE hh=? AND profile=? AND id=?",
                        (d.get("cover"), blob, r["hh"], r["profile"], r["id"]))
            done += 1
            saved += before - len(blob)
    _set_global(con, "covers_compacted", datetime.datetime.utcnow().isoformat())
    con.commit()
    return "compacted %d covers, %.1f MB saved" % (done, saved / 1048576.0)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db()
    compact = asyncio.create_task(asyncio.to_thread(_compact_covers_sync))
    task = asyncio.create_task(daily_backup_loop())
    yield
    task.cancel()
    compact.cancel()


app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=ORIGINS, allow_methods=["*"], allow_headers=["*"])


# ----------------------------------------------------------------- AI

class AIReq(BaseModel):
    system: str
    messages: list
    max_tokens: int = 1600


def _spend_ai(con, hh_row):
    """One shared code per household means one household can only ever cost the
    operator so much in a day. Without this, handing a code to a friend hands
    them the credit card."""
    hh, day = hh_row["hh"], datetime.date.today().isoformat()
    con.execute("INSERT OR IGNORE INTO ai_usage (hh, day, n) VALUES (?,?,0)", (hh, day))
    n = con.execute("SELECT n FROM ai_usage WHERE hh=? AND day=?", (hh, day)).fetchone()["n"]
    cap = hh_row["ai_cap"] or AI_DAILY_CAP
    if n >= cap:
        raise HTTPException(429, {"code": "daily_cap", "status": 429,
                                  "detail": "this household has used its %d AI calls for today" % cap})
    con.execute("UPDATE ai_usage SET n=n+1 WHERE hh=? AND day=?", (hh, day))
    con.commit()


@app.post("/ai")
async def ai(req: AIReq, x_family_code: str | None = Header(default=None)):
    con, hh_row = house(x_family_code)
    if not API_KEY:
        raise HTTPException(500, "server has no ANTHROPIC_API_KEY")
    _spend_ai(con, hh_row)
    async with httpx.AsyncClient(timeout=90) as c:
        r = await c.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-sonnet-4-6", "max_tokens": min(req.max_tokens, 3000),
                  "system": req.system, "messages": req.messages},
        )
    if r.status_code != 200:
        # The page can't act on a raw upstream blob, and a child certainly can't.
        body = r.text[:400]
        low = body.lower()
        if "credit balance is too low" in low:
            code = "no_credit"
        elif r.status_code == 429 or "rate_limit" in low:
            code = "rate_limited"
        elif r.status_code == 529 or "overloaded" in low:
            code = "overloaded"
        elif r.status_code in (401, 403) or "authentication" in low or "invalid x-api-key" in low:
            code = "bad_key"
        elif "image" in low and "too large" in low:
            code = "image_too_big"
        else:
            code = "upstream"
        raise HTTPException(502, {"code": code, "status": r.status_code, "detail": body})
    data = r.json()
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    return {"text": text}


# ----------------------------------------------------------------- who am I

@app.get("/me")
def me(x_family_code: str | None = Header(default=None)):
    """Everything the page needs to draw itself for this household."""
    con, hh = house(x_family_code)
    day = datetime.date.today().isoformat()
    used = con.execute("SELECT n FROM ai_usage WHERE hh=? AND day=?", (hh["hh"], day)).fetchone()
    return {
        "household": hh["name"],
        "members": members_of(con, hh["hh"]),
        "ai": {"used_today": used["n"] if used else 0, "cap": hh["ai_cap"] or AI_DAILY_CAP},
        "backup": bool(hh["backup"] and GH_TOKEN and GH_REPO),
    }


class MemberReq(BaseModel):
    id: str | None = None
    name: str | None = None
    icon: str | None = None
    level: str | None = None
    level_en: str | None = None
    is_adult: bool | None = None


@app.post("/members")
def upsert_member(req: MemberReq, x_family_code: str | None = Header(default=None)):
    con, hh = house(x_family_code)
    existing = {m["id"]: m for m in members_of(con, hh["hh"])}

    mid = (req.id or slug(req.name or "")).strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,23}", mid or ""):
        raise HTTPException(400, "a reader needs a name")
    if mid not in existing and len(existing) >= MEMBER_MAX:
        raise HTTPException(400, "that's as many readers as one household can hold")

    old = existing.get(mid)
    name = (req.name or (old or {}).get("name") or mid).strip()[:NAME_MAX]
    if not name:
        raise HTTPException(400, "a reader needs a name")
    icon = (req.icon if req.icon is not None else (old or {}).get("icon") or "").strip()
    if icon and (len(icon) > ICON_MAX or any(c.isspace() or ord(c) < 32 for c in icon)):
        raise HTTPException(400, "icon must be 1-%d characters, no whitespace" % ICON_MAX)
    level = (req.level or (old or {}).get("level") or "a young reader").strip()[:120]
    level_en = req.level_en if req.level_en is not None else (old or {}).get("level_en")
    is_adult = int(req.is_adult) if req.is_adult is not None else int((old or {}).get("is_adult") or 0)
    pos = old["pos"] if old and "pos" in old.keys() else len(existing)

    con.execute("INSERT OR REPLACE INTO members (hh, id, name, icon, level, level_en, is_adult, pos) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (hh["hh"], mid, name, icon, level, (level_en or None)[:200] if level_en else None, is_adult, pos))
    con.commit()
    return {"ok": True, "members": members_of(con, hh["hh"])}


@app.delete("/members/{member_id}")
def remove_member(member_id: str, x_family_code: str | None = Header(default=None)):
    con, hh = house(x_family_code)
    n = con.execute("SELECT COUNT(*) c FROM books WHERE hh=? AND profile=?",
                    (hh["hh"], member_id)).fetchone()["c"]
    if n:
        # removing a reader would orphan their writing; say so instead
        raise HTTPException(409, "that reader has %d books saved - remove the books first" % n)
    con.execute("DELETE FROM members WHERE hh=? AND id=?", (hh["hh"], member_id))
    con.commit()
    return {"ok": True, "members": members_of(con, hh["hh"])}


# ----------------------------------------------------------------- books

@app.get("/books")
def list_books(profile: str | None = None, x_family_code: str | None = Header(default=None)):
    con, hh = house(x_family_code)
    if (profile or "").strip().lower() == "all":
        rows = con.execute("SELECT profile, data FROM books WHERE hh=? ORDER BY id DESC", (hh["hh"],)).fetchall()
    else:
        rows = con.execute("SELECT profile, data FROM books WHERE hh=? AND profile=? ORDER BY id DESC",
                           (hh["hh"], prof(con, hh["hh"], profile))).fetchall()
    out = []
    for r in rows:
        d = json.loads(r["data"])
        d["profile"] = r["profile"]
        # A library row draws a 38x52 thumbnail. Sending the full photo for that
        # turned a profile switch into a 50MB download.
        d["has_cover"] = bool(d.get("cover"))
        d.pop("cover", None)
        out.append(d)
    return out


@app.get("/books/{book_id}/cover")
def book_cover(book_id: int, profile: str | None = None, x_family_code: str | None = Header(default=None)):
    """The full photo, only when something actually wants to show it."""
    con, hh = house(x_family_code)
    r = con.execute("SELECT data FROM books WHERE hh=? AND profile=? AND id=?",
                    (hh["hh"], prof(con, hh["hh"], profile), book_id)).fetchone()
    if not r:
        raise HTTPException(404, "no such book")
    return {"cover": json.loads(r["data"]).get("cover")}


class Book(BaseModel):
    id: int
    profile: str | None = None
    title: str
    author: str | None = ""
    date: str
    cover: str | None = None
    lang: str | None = "en"
    genre: str | None = "other"
    kind: str | None = "fiction"
    audience: str | None = "me"
    order: list = []
    answers: list = []
    follow: list = []
    report: dict | None = None
    thumb: str | None = None      # small copy for lists; derived on save, never uploaded
    edits: list = []              # every AI rewrite the reader asked for, with before/after
    report0: dict | None = None   # the AI's untouched first draft; never overwritten once set
    updatedAt: str | None = None  # set when a saved report is revised later
    henryWins: int = 0            # legacy names: "different from the AI" and "same as the AI"
    aiWins: int = 0


@app.post("/books")
def save_book(b: Book, x_family_code: str | None = Header(default=None)):
    con, hh = house(x_family_code)
    p = prof(con, hh["hh"], b.profile)
    d = b.model_dump()
    d["profile"] = p
    d = _normalise_images(d)
    con.execute(
        "INSERT OR REPLACE INTO books (hh, profile, id, title, author, date, cover, data, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (hh["hh"], p, b.id, b.title, b.author or "", b.date, d.get("cover"),
         json.dumps(d, ensure_ascii=False), datetime.datetime.utcnow().isoformat()),
    )
    con.commit()
    return {"ok": True, "profile": p}


@app.delete("/books/{book_id}")
def delete_book(book_id: int, profile: str | None = None, x_family_code: str | None = Header(default=None)):
    con, hh = house(x_family_code)
    cur = con.execute("DELETE FROM books WHERE hh=? AND profile=? AND id=?",
                      (hh["hh"], prof(con, hh["hh"], profile), book_id))
    con.commit()
    return {"ok": True, "deleted": cur.rowcount}


@app.post("/backup")
async def backup(x_family_code: str | None = Header(default=None)):
    con, hh = house(x_family_code)
    if not (GH_TOKEN and GH_REPO):
        return {"skipped": "no GITHUB_TOKEN / GITHUB_REPO"}
    if not hh["backup"]:
        return {"skipped": "backup is off for this household"}
    return await _backup_household(con, hh)


# ----------------------------------------------------------------- operator

class HouseReq(BaseModel):
    name: str
    code: str
    members: list = []          # [{name, icon, level, level_en, is_adult}]
    ai_cap: int | None = None
    backup: bool = False


@app.post("/households")
def create_household(req: HouseReq, x_admin_code: str | None = Header(default=None)):
    """Only the operator makes households. Self-serve signup is a different
    product with a different set of problems; this is enough to run a pilot."""
    admin(x_admin_code)
    code = (req.code or "").strip()
    if len(code) < CODE_MIN:
        raise HTTPException(400, "a join code needs at least %d characters" % CODE_MIN)
    con = db()
    if con.execute("SELECT 1 FROM households WHERE code_hash=?", (code_hash(con, code),)).fetchone():
        raise HTTPException(409, "that code is already in use")
    con.execute("INSERT INTO households (name, code_hash, created_at, ai_cap, backup) VALUES (?,?,?,?,?)",
                ((req.name or "A household").strip()[:60], code_hash(con, code),
                 datetime.datetime.utcnow().isoformat(), req.ai_cap or AI_DAILY_CAP, int(bool(req.backup))))
    hh = con.execute("SELECT hh FROM households ORDER BY hh DESC LIMIT 1").fetchone()["hh"]
    seen = set()
    for pos, m in enumerate((req.members or [])[:MEMBER_MAX]):
        mid = slug(m.get("name", ""))
        while mid in seen:
            mid += "2"
        seen.add(mid)
        con.execute("INSERT INTO members (hh, id, name, icon, level, level_en, is_adult, pos) VALUES (?,?,?,?,?,?,?,?)",
                    (hh, mid, (m.get("name") or mid)[:NAME_MAX], (m.get("icon") or "")[:ICON_MAX],
                     (m.get("level") or "a young reader")[:120], (m.get("level_en") or None),
                     int(bool(m.get("is_adult"))), pos))
    con.commit()
    return {"ok": True, "hh": hh, "members": members_of(con, hh)}


@app.get("/households")
def list_households(x_admin_code: str | None = Header(default=None)):
    admin(x_admin_code)
    con = db()
    out = []
    for r in con.execute("SELECT * FROM households ORDER BY hh").fetchall():
        books = con.execute("SELECT COUNT(*) c FROM books WHERE hh=?", (r["hh"],)).fetchone()["c"]
        used = con.execute("SELECT n FROM ai_usage WHERE hh=? AND day=?",
                           (r["hh"], datetime.date.today().isoformat())).fetchone()
        out.append({"hh": r["hh"], "name": r["name"], "created": r["created_at"][:10],
                    "readers": len(members_of(con, r["hh"])), "books": books,
                    "ai_today": used["n"] if used else 0, "ai_cap": r["ai_cap"], "backup": bool(r["backup"])})
    return out


class HousePatch(BaseModel):
    name: str | None = None
    code: str | None = None          # replacing it locks the old one out immediately
    ai_cap: int | None = None
    backup: bool | None = None


@app.patch("/households/{hh}")
def update_household(hh: int, req: HousePatch, x_admin_code: str | None = Header(default=None)):
    admin(x_admin_code)
    con = db()
    row = con.execute("SELECT * FROM households WHERE hh=?", (hh,)).fetchone()
    if not row:
        raise HTTPException(404, "no such household")
    name = (req.name or row["name"]).strip()[:60] or row["name"]
    cap = req.ai_cap if req.ai_cap is not None else row["ai_cap"]
    backup = int(req.backup) if req.backup is not None else row["backup"]
    ch = row["code_hash"]
    if req.code:
        code = req.code.strip()
        if len(code) < CODE_MIN:
            raise HTTPException(400, "a join code needs at least %d characters" % CODE_MIN)
        ch = code_hash(con, code)
        clash = con.execute("SELECT hh FROM households WHERE code_hash=? AND hh<>?", (ch, hh)).fetchone()
        if clash:
            raise HTTPException(409, "that code is already in use")
    con.execute("UPDATE households SET name=?, code_hash=?, ai_cap=?, backup=? WHERE hh=?",
                (name, ch, max(1, int(cap)), backup, hh))
    con.commit()
    return {"ok": True}


@app.delete("/households/{hh}")
def delete_household(hh: int, purge: int = 0, x_admin_code: str | None = Header(default=None)):
    """Everything that household ever wrote goes with it, so say so out loud."""
    admin(x_admin_code)
    con = db()
    row = con.execute("SELECT * FROM households WHERE hh=?", (hh,)).fetchone()
    if not row:
        raise HTTPException(404, "no such household")
    books = con.execute("SELECT COUNT(*) c FROM books WHERE hh=?", (hh,)).fetchone()["c"]
    if books and not purge:
        raise HTTPException(409, {"code": "has_books", "books": books,
                                  "detail": "%s has %d books saved" % (row["name"], books)})
    for t in ("books", "members", "settings", "ai_usage"):
        con.execute("DELETE FROM %s WHERE hh=?" % t, (hh,))
    con.execute("DELETE FROM households WHERE hh=?", (hh,))
    con.commit()
    return {"ok": True, "deleted_books": books}


@app.get("/")
def health():
    con = db()
    return {"ok": True,
            "households": con.execute("SELECT COUNT(*) c FROM households").fetchone()["c"],
            "books": con.execute("SELECT COUNT(*) c FROM books").fetchone()["c"],
            "backup": bool(GH_TOKEN and GH_REPO),
            "migrated_v4": _global(con, "migrated_v4", False),
            "covers_compacted": _global(con, "covers_compacted", False)}
