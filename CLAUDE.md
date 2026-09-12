# Book Lab — CLAUDE.md

A reading-log app for Henry (3rd grade, bilingual EN/KO, uses a tablet). He photographs a book cover, answers a tap-only quiz, tells the AI how he felt, plans and edits his own book report, and keeps stats. The AI is deliberately shown to be *fallible* — it guesses answers and Henry, who actually read the book, catches its mistakes.

Owner: HAN (kenpineus-lab). Repo: https://github.com/kenpineus-lab/ReadingBook

## What this is teaching (keep this in mind when changing anything)

1. **The AI can be confidently wrong.** Every quiz question shows what the AI guessed and how sure it was. Henry checks the book. Never make the AI look infallible.
2. **Writing in the AI era = directing + judging, not typing.** Henry decides who the report is for and what order the parts go in *before* the AI writes. Then he edits with intent buttons (shorter / funnier / say why / use the name / don't spoil). Never auto-save a report he hasn't confirmed.
3. **Tap, don't type.** He types slowly. Every step must work with buttons only. A text field is a fallback, never the main path.
4. **Pride through numbers.** Stats are big, simple, and his: books read, this month, weeks in a row, AI beaten, genres. Don't bury them.

## Layout

```
index.html            static frontend → GitHub Pages (no secrets, no build step)
server/main.py        FastAPI → Railway (holds the API key, stores data, backs up to GitHub)
server/requirements.txt, Procfile, railway.json
README.md             deploy guide (Korean)
```

## Frontend (`index.html`)

Plain ES2017 JS in one file. No framework, no bundler. Everything lives in the `(function(){ ... })()` IIFE.

- **Config:** `bl:srv` (server URL) and `bl:code` (family code) in `localStorage`. Set once per device via the ONE-TIME SETUP panel. Nothing else is stored on the device — all book data comes from the server.
- **State:** the `S` object. `S.lang` is `"en"` or `"ko"` and drives every prompt and label via `T(en, ko)`.
- **Screens** (divs toggled by `show(id)`): `library` → `quiz` → `follow` → `plan` → `report`.
- **AI calls:** `ai(system, messages, maxTokens)` → `POST {server}/ai`. The server proxies to Anthropic. Responses are parsed with `tag()` / `kv()` / `blocks()` — a tag-and-key:value format chosen because JSON from the model breaks on quotes and newlines. Keep that format; don't switch to JSON.
- **Flow:**
  1. Photo → `onPhoto` → vision call returns `<book>title/author/language</book>` → `startQuiz()` immediately.
  2. `startQuiz` asks for `<meta>genre/kind</meta>` + five `<q>` blocks each with `mypick` (what the AI thinks) and `sure`.
  3. `makeFollowups` → three `<fq>` feelings questions. `reroll()` regenerates choices.
  4. `renderPlan` → Henry picks audience (teacher/friend/me) and taps sections in order. "Just write it" skips.
  5. `makeReport` → AI writes only the chosen sections in the chosen order for that audience → `renderConfirm`.
  6. `renderConfirm` → tap a section to reveal `FIXES` buttons → `fixSection(k, fix)` rewrites that section. Stars adjustable. `saveReport` POSTs to `/books` only after "Yes, that's my report!".
- **Stats:** computed client-side in `renderLibrary` from `/books`. Genre labels/icons in `GN`.

## Server (`server/main.py`)

- `POST /ai` — proxy. Model is `claude-sonnet-4-6`. Never expose the key.
- `GET/POST /books`, `DELETE /books/{id}` — SQLite at `DB_PATH` (default `/data/booklab.db`; Railway volume must be mounted at `/data`).
- `POST /backup` + a daily loop → commits `henry-books.json` (covers stripped) to `GITHUB_REPO` using `GITHUB_TOKEN`.
- Every endpoint except `/` requires header `x-family-code` == `FAMILY_CODE`.
- CORS from `ALLOWED_ORIGIN` (comma-separated).

Env: `ANTHROPIC_API_KEY`, `FAMILY_CODE`, `ALLOWED_ORIGIN`, `DB_PATH`, `GITHUB_TOKEN`, `GITHUB_REPO`.

## Working on this

- Edit `index.html` directly. Validate with: extract the `<script>` body and run `npx esbuild --target=es2017` on it. That catches syntax errors; there is no test suite.
- Validate the server with `python -c "import ast; ast.parse(open('server/main.py').read())"` and, for behavior, the `fastapi.testclient` snippet in the README history: set `DB_PATH=/tmp/t.db FAMILY_CODE=1234`, then POST/GET/DELETE `/books` with the header.
- Both languages: any new user-facing string goes through `T(en, ko)`. Any new AI prompt must branch on `L()` so Korean books get Korean questions (해요체).
- Do not add a build step, a framework, or `localStorage` for book data. The whole point is that the page is a dumb client and the server + GitHub backup are the source of truth.
- Don't gate features behind book counts. HAN explained everything to Henry up front; all tools are available from the first book.

## Deploy (short version — full steps in README.md)

1. Railway: deploy repo, **Root Directory = `server`**, add env vars, **mount volume at `/data`**, generate domain.
2. GitHub: create private `kenpineus-lab/ReadingBook-backup`, fine-grained token (contents: read/write), set `GITHUB_TOKEN` + `GITHUB_REPO`.
3. Pages: this repo → Settings → Pages → `main` / root. URL: https://kenpineus-lab.github.io/ReadingBook/
4. Tablet: open the URL, enter server URL + family code once, add to home screen.

## Backlog (not started)

- Profiles per child (Henry + older brother) with grade level → question difficulty.
- "Currently reading" state with chapter-by-chapter check-ins.
- Open Library title search as a fallback when cover recognition fails.
- Parent dashboard (read-only view of all reports and stats).
- If this ever goes beyond the family: COPPA applies to under-13 data in the US. Needs parental-consent flow and legal review before onboarding anyone else's kids.
