"""
Book Lab server
- POST /ai        : proxies to Anthropic (the API key lives here, never in the browser)
- GET  /books     : list one profile's books  (?profile=henry|test)
- POST /books     : save a finished book
- DELETE /books/{id}
- POST /backup    : push a JSON snapshot to a private GitHub repo (also runs daily)

Env vars (set in Railway):
  ANTHROPIC_API_KEY   required
  FAMILY_CODE         required — the page sends this in a header; keeps strangers out
  ALLOWED_ORIGIN      e.g. https://kenpineus-lab.github.io   (comma-separate for several)
  GITHUB_TOKEN        optional — fine-grained token with contents:write on the backup repo
  GITHUB_REPO         optional — e.g. kenpineus-lab/henry-reading-backup
  DB_PATH             optional — default /data/booklab.db (mount a Railway volume at /data)
"""
import os, io, json, sqlite3, base64, asyncio, datetime
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx

API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
FAMILY_CODE = os.environ.get("FAMILY_CODE", "")
ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGIN", "*").split(",")]
GH_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GH_REPO = os.environ.get("GITHUB_REPO", "")
DB_PATH = os.environ.get("DB_PATH", "/data/booklab.db")


PROFILES = ("henry", "jeremy", "rebecca", "han")   # one shelf per person; stats never mix
DEFAULT_PROFILE = "henry"

SCHEMA = """CREATE TABLE IF NOT EXISTS books (
    profile TEXT NOT NULL DEFAULT 'henry',
    id INTEGER NOT NULL,
    title TEXT NOT NULL,
    author TEXT,
    date TEXT NOT NULL,
    cover TEXT,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (profile, id)
)"""


ICON_MAX = 12          # a couple of emoji, including any variation selectors
COVER_MAX = 1024       # longest edge kept for the full cover
THUMB_MAX = 160        # what a library row actually draws (38x52 css px, retina)


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


def _ensure_settings(con):
    con.execute("CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT NOT NULL)")


def _ensure_schema(con):
    cols = [r["name"] for r in con.execute("PRAGMA table_info(books)")]
    _ensure_settings(con)
    if not cols:                       # fresh database
        con.execute(SCHEMA)
        con.commit()
        return
    if "profile" in cols:
        # v2 -> v3: the sandbox profile "test" became Han's own shelf
        if con.execute("SELECT COUNT(*) FROM books WHERE profile='test'").fetchone()[0]:
            con.execute("UPDATE books SET profile='han' WHERE profile='test'")
            con.commit()
        return
    # v1 -> v2: books predate profiles, so they are all Henry's. Re-key on (profile, id).
    con.executescript(
        "ALTER TABLE books RENAME TO books_v1; "
        + SCHEMA + "; "
        + "INSERT INTO books (profile, id, title, author, date, cover, data, created_at) "
          "SELECT 'henry', id, title, author, date, cover, data, created_at FROM books_v1; "
          "DROP TABLE books_v1;"
    )
    con.commit()


def db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    _ensure_schema(con)
    return con


def prof(p: str | None) -> str:
    p = (p or DEFAULT_PROFILE).strip().lower()
    if p not in PROFILES:
        raise HTTPException(400, f"unknown profile (use one of: {', '.join(PROFILES)})")
    return p


def check(code: str | None):
    if not FAMILY_CODE or code != FAMILY_CODE:
        raise HTTPException(401, "wrong family code")


async def backup_to_github():
    if not (GH_TOKEN and GH_REPO):
        return {"skipped": "no GITHUB_TOKEN / GITHUB_REPO"}
    con = db()
    rows = con.execute("SELECT * FROM books ORDER BY profile, id").fetchall()
    payload = []
    for r in rows:
        d = json.loads(r["data"])
        d.pop("cover", None)  # keep the backup small; covers stay in the DB
        d.pop("thumb", None)
        d["profile"] = r["profile"]
        payload.append(d)
    body = json.dumps({"exported": datetime.datetime.utcnow().isoformat() + "Z", "count": len(payload), "books": payload}, ensure_ascii=False, indent=2)

    path = "henry-books.json"
    url = f"https://api.github.com/repos/{GH_REPO}/contents/{path}"
    headers = {"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"}
    async with httpx.AsyncClient(timeout=30) as c:
        sha = None
        r = await c.get(url, headers=headers)
        if r.status_code == 200:
            sha = r.json().get("sha")
        put = {
            "message": f"backup {datetime.date.today().isoformat()} ({len(payload)} books)",
            "content": base64.b64encode(body.encode("utf-8")).decode("ascii"),
        }
        if sha:
            put["sha"] = sha
        r = await c.put(url, headers=headers, json=put)
        if r.status_code not in (200, 201):
            raise HTTPException(502, f"github backup failed: {r.text[:200]}")
    return {"ok": True, "count": len(payload)}


def _compact_covers_sync():
    """The covers saved before the phone-side shrink are 1-9MB each, which made
    switching profiles a 50MB download. Rewrite them once, in the background."""
    con = db()
    if con.execute("SELECT v FROM settings WHERE k='covers_compacted'").fetchone():
        return "already done"
    rows = con.execute("SELECT profile, id, data FROM books").fetchall()
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
            con.execute("UPDATE books SET cover=?, data=? WHERE profile=? AND id=?",
                        (d.get("cover"), blob, r["profile"], r["id"]))
            done += 1
            saved += before - len(blob)
    con.execute("INSERT OR REPLACE INTO settings (k, v) VALUES ('covers_compacted', ?)",
                (datetime.datetime.utcnow().isoformat(),))
    con.commit()
    return "compacted %d covers, %.1f MB saved" % (done, saved / 1048576.0)


async def daily_backup_loop():
    while True:
        await asyncio.sleep(24 * 3600)
        try:
            await backup_to_github()
        except Exception:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    db()
    # off the event loop: decoding a dozen multi-megabyte JPEGs takes a moment
    compact = asyncio.create_task(asyncio.to_thread(_compact_covers_sync))
    task = asyncio.create_task(daily_backup_loop())
    yield
    task.cancel()
    compact.cancel()


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


class AIReq(BaseModel):
    system: str
    messages: list
    max_tokens: int = 1600


@app.post("/ai")
async def ai(req: AIReq, x_family_code: str | None = Header(default=None)):
    check(x_family_code)
    if not API_KEY:
        raise HTTPException(500, "server has no ANTHROPIC_API_KEY")
    async with httpx.AsyncClient(timeout=90) as c:
        r = await c.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-sonnet-4-6", "max_tokens": min(req.max_tokens, 3000), "system": req.system, "messages": req.messages},
        )
    if r.status_code != 200:
        # The page can't act on a raw upstream blob, and a child certainly can't.
        # Classify it so the screen can say what actually needs doing.
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


@app.get("/books")
def list_books(profile: str | None = None, x_family_code: str | None = Header(default=None)):
    check(x_family_code)
    con = db()
    # "all" is the shared family view - readable, never writable
    if (profile or "").strip().lower() == "all":
        rows = con.execute("SELECT profile, data FROM books ORDER BY id DESC").fetchall()
    else:
        rows = con.execute("SELECT profile, data FROM books WHERE profile=? ORDER BY id DESC", (prof(profile),)).fetchall()
    out = []
    for r in rows:
        d = json.loads(r["data"])
        d["profile"] = r["profile"]    # authoritative, even for rows saved before profiles existed
        # A library row draws a 38x52 thumbnail. Sending the full photo for that
        # turned a profile switch into a 50MB download.
        d["has_cover"] = bool(d.get("cover"))
        d.pop("cover", None)
        out.append(d)
    return out


@app.get("/books/{book_id}/cover")
def book_cover(book_id: int, profile: str | None = None, x_family_code: str | None = Header(default=None)):
    """The full photo, only when something actually wants to show it."""
    check(x_family_code)
    con = db()
    r = con.execute("SELECT data FROM books WHERE profile=? AND id=?", (prof(profile), book_id)).fetchone()
    if not r:
        raise HTTPException(404, "no such book")
    return {"cover": json.loads(r["data"]).get("cover")}


class Book(BaseModel):
    id: int
    profile: str | None = DEFAULT_PROFILE
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
    thumb: str | None = None  # small copy for lists; derived on save, never uploaded
    edits: list = []          # every AI rewrite the reader asked for, with before/after
    report0: dict | None = None   # the AI's untouched first draft; never overwritten once set
    updatedAt: str | None = None  # set when a saved report is revised later
    henryWins: int = 0
    aiWins: int = 0


@app.post("/books")
def save_book(b: Book, x_family_code: str | None = Header(default=None)):
    check(x_family_code)
    p = prof(b.profile)
    d = b.model_dump()
    d["profile"] = p
    d = _normalise_images(d)
    con = db()
    con.execute(
        "INSERT OR REPLACE INTO books (profile, id, title, author, date, cover, data, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (p, b.id, b.title, b.author or "", b.date, d.get("cover"), json.dumps(d, ensure_ascii=False), datetime.datetime.utcnow().isoformat()),
    )
    con.commit()
    return {"ok": True, "profile": p}


@app.delete("/books/{book_id}")
def delete_book(book_id: int, profile: str | None = None, x_family_code: str | None = Header(default=None)):
    check(x_family_code)
    p = prof(profile)
    con = db()
    cur = con.execute("DELETE FROM books WHERE profile=? AND id=?", (p, book_id))
    con.commit()
    return {"ok": True, "deleted": cur.rowcount}


@app.post("/backup")
async def backup(x_family_code: str | None = Header(default=None)):
    check(x_family_code)
    return await backup_to_github()


class IconReq(BaseModel):
    profile: str
    icon: str


@app.get("/profiles")
def get_profiles(x_family_code: str | None = Header(default=None)):
    """Per-person display settings. Lives on the server so a chosen icon follows
    the person to every device, not just the one they picked it on."""
    check(x_family_code)
    con = db()
    out = {}
    for r in con.execute("SELECT k, v FROM settings WHERE k LIKE 'icon:%'"):
        out[r["k"][5:]] = r["v"]
    return out


@app.post("/profiles")
def set_profile(req: IconReq, x_family_code: str | None = Header(default=None)):
    check(x_family_code)
    p = prof(req.profile)
    icon = (req.icon or "").strip()
    # a label, not a payload: no whitespace, no control characters, and short
    if not icon or len(icon) > ICON_MAX or any(c.isspace() or ord(c) < 32 for c in icon):
        raise HTTPException(400, "icon must be 1-%d characters, no whitespace" % ICON_MAX)
    con = db()
    con.execute("INSERT OR REPLACE INTO settings (k, v) VALUES (?,?)", ("icon:" + p, icon))
    con.commit()
    return {"ok": True, "profile": p, "icon": icon}


@app.get("/")
def health():
    con = db()
    n = con.execute("SELECT COUNT(*) FROM books").fetchone()[0]
    counts = {p: 0 for p in PROFILES}
    for r in con.execute("SELECT profile, COUNT(*) AS c FROM books GROUP BY profile"):
        counts[r["profile"]] = r["c"]
    compacted = con.execute("SELECT v FROM settings WHERE k='covers_compacted'").fetchone()
    return {"ok": True, "books": n, "profiles": counts, "backup": bool(GH_TOKEN and GH_REPO),
            "covers_compacted": compacted["v"] if compacted else False}
