"""
Book Lab server
- POST /ai        : proxies to Anthropic (the API key lives here, never in the browser)
- GET  /books     : list Henry's books
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
import os, json, sqlite3, base64, asyncio, datetime
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


def db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE IF NOT EXISTS books (
        id INTEGER PRIMARY KEY,
        title TEXT NOT NULL,
        author TEXT,
        date TEXT NOT NULL,
        cover TEXT,
        data TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""")
    return con


def check(code: str | None):
    if not FAMILY_CODE or code != FAMILY_CODE:
        raise HTTPException(401, "wrong family code")


async def backup_to_github():
    if not (GH_TOKEN and GH_REPO):
        return {"skipped": "no GITHUB_TOKEN / GITHUB_REPO"}
    con = db()
    rows = con.execute("SELECT * FROM books ORDER BY id").fetchall()
    payload = []
    for r in rows:
        d = json.loads(r["data"])
        d.pop("cover", None)  # keep the backup small; covers stay in the DB
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
    task = asyncio.create_task(daily_backup_loop())
    yield
    task.cancel()


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
        raise HTTPException(502, f"anthropic error: {r.text[:300]}")
    data = r.json()
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    return {"text": text}


@app.get("/books")
def list_books(x_family_code: str | None = Header(default=None)):
    check(x_family_code)
    con = db()
    rows = con.execute("SELECT data FROM books ORDER BY id DESC").fetchall()
    return [json.loads(r["data"]) for r in rows]


class Book(BaseModel):
    id: int
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
    henryWins: int = 0
    aiWins: int = 0


@app.post("/books")
def save_book(b: Book, x_family_code: str | None = Header(default=None)):
    check(x_family_code)
    con = db()
    con.execute(
        "INSERT OR REPLACE INTO books (id, title, author, date, cover, data, created_at) VALUES (?,?,?,?,?,?,?)",
        (b.id, b.title, b.author or "", b.date, b.cover, json.dumps(b.model_dump(), ensure_ascii=False), datetime.datetime.utcnow().isoformat()),
    )
    con.commit()
    return {"ok": True}


@app.delete("/books/{book_id}")
def delete_book(book_id: int, x_family_code: str | None = Header(default=None)):
    check(x_family_code)
    con = db()
    con.execute("DELETE FROM books WHERE id=?", (book_id,))
    con.commit()
    return {"ok": True}


@app.post("/backup")
async def backup(x_family_code: str | None = Header(default=None)):
    check(x_family_code)
    return await backup_to_github()


@app.get("/")
def health():
    con = db()
    n = con.execute("SELECT COUNT(*) FROM books").fetchone()[0]
    return {"ok": True, "books": n, "backup": bool(GH_TOKEN and GH_REPO)}
