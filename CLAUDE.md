# Book Lab — CLAUDE.md

A reading-log app for the family — Henry (3rd grade), Jeremy, Rebecca, and Han (Dad). Bilingual EN/KO, used on a tablet. He photographs a book cover, answers a tap-only quiz, tells the AI how he felt, plans and edits his own book report, and keeps stats. The AI is deliberately shown to be *fallible* — it guesses answers and Henry, who actually read the book, catches its mistakes.

Owner: HAN (kenpineus-lab). Repo: https://github.com/kenpineus-lab/ReadingBook

## What this is teaching (keep this in mind when changing anything)

1. **The AI can be confidently wrong.** Every quiz question shows what the AI guessed and how sure it was. Henry checks the book. Never make the AI look infallible.
2. **Writing in the AI era = directing + judging, not typing.** Henry decides who the report is for and what order the parts go in *before* the AI writes. Then he edits with intent buttons (shorter / funnier / say why / use the name / don't spoil). Never auto-save a report he hasn't confirmed.
3. **Tap, don't type.** He types slowly. Every step must work with buttons only. A text field is a fallback, never the main path.
4. **Pride through numbers.** Stats are big, simple, and theirs: books read, this month, weeks in a row, times they read it their own way, genres. Don't bury them.
5. **A different answer is not a wrong answer.** The quiz is not scored and nobody wins it. Where the reader and the AI diverge, say "different" and colour it with the ribbon violet — never red, never a trophy, never win/lose. Red is reserved for actual errors. The point stands on its own: only one of the two actually read the book. `henryWins`/`aiWins` are legacy field names that now mean *different* and *same*; don't re-add scoring language on top of them.

## Layout

```
index.html            static frontend → GitHub Pages (no secrets, no build step)
assets/               paper.svg, shelf.svg, mark.svg, icon-*.png — hand-made, no CDN
manifest.webmanifest  home-screen identity (standalone, icons, theme colour)
server/main.py        FastAPI → Railway (holds the API key, stores data, backs up to GitHub)
server/requirements.txt, Procfile, railway.json
README.md             deploy guide (Korean)
```

## Profiles

Four shelves: `henry`, `jeremy`, `rebecca`, `han`, plus `all` — the Family shelf,
which is a **view, not a person**: `GET /books?profile=all` merges every shelf, and
writing or deleting with `profile=all` is a 400. The capture button is hidden there,
because a book always belongs to whoever read it. `isFamily()` guards the UI; deletes
from that view target the owner's shelf, not `all`. `bl:profile` in `localStorage` picks
one per device; every `/books` call is scoped by it, so books and stats never mix.
`PROFILES` in `index.html` is the single source of truth — id, display name, icon, and
`level` (the reading level the AI prompts are written for: Henry 3rd grade, Jeremy 8th
grade, Rebecca and Han adults). `level` is not decoration — it sets how hard the quiz
questions come back and how the report is written, so keep it accurate as the kids grow.
An optional `levelEn` overrides it for English books only: Henry reads Korean at grade
level but English two grades ahead, so `RL()` picks by `L()`, not by profile alone. Any
phrase put in `level`/`levelEn` has to survive being dropped mid-sentence in five prompts —
keep it a noun phrase. `adult: true` is a separate axis: `level` sets how *hard*, `voice()`
sets how it *sounds*. Rebecca and Han get adult prose (평서체 in Korean); the kids get a
child's register. Every prompt that used to hard-code "like a kid" now calls `voice()` —
if you add a prompt, call it there too or an adult will get a third-grader's sentences.

**Icons** are per person and live on the server (`GET/POST /profiles`, a `settings` table
keyed `icon:<profile>`), so a choice made on the tablet shows up on the laptop. The picker
is a fixed grid of 40 (`ICONS`) — faces first, then things — tapping only, because opening the
emoji keyboard would put a text field on the one screen designed to need none. `who(id)` layers the chosen icon over
the default, so everything that renders a profile picks it up for free. The server's `PROFILES`
tuple must list the same ids. Han's shelf is Dad's, for testing.

Language follows the **book**, not the profile: the cover-photo call returns
`language: en|ko` and that drives every prompt via `L()`. Don't tie it to the profile.

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
  6. `renderConfirm` → tap a section to reveal `FIXES` chips → `fixSection(k, fix, custom)` rewrites
     that section — the section labels exist **only here**, as scaffolding for choosing what
     to fix. Every part carries a visible `✎ fix this` pill and the first part opens with its
     chips already showing: an affordance nobody can see is an affordance nobody uses, and a
     demonstration beats an instruction. `openCustom(k)` is the typed fallback — the reader dictates the change in
     their own words. Stars adjustable. `saveReport` POSTs to `/books` only after
     "Yes, that's my report!".
- **Revision:** `editReport(b)` reopens a saved book — `✎` on any library row. It restores the
  run's state (draft, order, audience, answers, language) and sets `S.editingId` / `S.editBase`,
  so `saveReport` writes back to the **same id** and keeps the original `date`, `profile` and quiz
  counts; `updatedAt` records the revision. `S.asProfile` points `RP()` at the book's owner, so
  revising from the Family view still uses that person's level and voice. Three rules hold this
  together and none of them is optional: **`report0` (the AI's untouched first draft) is frozen
  and never rewritten** — `renderReport` shows it under "What the AI wrote first", which is the
  app's whole claim made visible; **`edits` append, never reset**, so a rewrite a week later sits
  in the same history; and the confirm screen says plainly that this is a revision. Legacy books
  saved before this have no `report0` and we do **not** invent one from their current text.
  There is deliberately no "write it again from scratch" button — that is gambling, not revising.
- **Edit log:** every accepted rewrite is pushed to `S.edits` with `{section, fix, custom, before,
  after, lang, at}` and saved on the book as `edits`. This is the evidence that a person
  directed the machine — it is the point of the app, not telemetry. Keep it, and keep
  `before`/`after` whole. `Book.edits` on the server must exist or Pydantic silently drops it.
- **Stats:** computed client-side in `renderLibrary` from `/books` (already profile-scoped). Genre labels/icons in `GN`.
  Each tile is tappable: `S.stat` holds which one is open and `statDetail(kind)` renders the books
  or questions behind that number, below the tiles. A number the reader can't open is a claim they
  have to take on faith — don't add one.
- **Theme:** daylight is the default; `bl:theme = "night"` opts out. The toggle is in the header.
- **Finished report is one piece of writing, not a form.** `renderReport`, `printReport` and
  `reportText` drop the section labels entirely and set the parts as consecutive paragraphs
  (`.essay`, drop cap, indented runs). The report prompt is told it will be printed with no
  headings, so each part must read as the next paragraph. Don't put the labels back.
- **Report actions:** the coloured button is "Do another book", because that is what happens
  next nine times out of ten. Print and Copy sit below it, plain. The copied text is the report
  and nothing else — no quiz tally — because it gets pasted where the writing is wanted.
- **Finished report** offers Print and Copy only. There is no mail sending: the server has no
  mail provider, and a button that says "Send" must actually send.

## Server (`server/main.py`)

- `POST /ai` — proxy. Model is `claude-sonnet-4-6`. Never expose the key.
- `GET/POST /profiles` — per-person display settings (currently just the icon) in a `settings`
  key/value table. The icon is validated as a short label: 1-12 chars, no whitespace, no control
  characters. It is never interpolated anywhere as HTML without `esc()`.
- `GET/POST /books`, `DELETE /books/{id}` — SQLite at `DB_PATH` (default `/data/booklab.db`; Railway volume must be mounted at `/data`). Rows are keyed on `(profile, id)`; reads and deletes take `?profile=`, writes take it in the body. An unknown profile is a 400.
- `_ensure_schema` migrates in place on every connect: v1 (no profile column) → all rows become `henry`; v2 → the old `test` profile becomes `han`. Both are idempotent.
- `POST /backup` + a daily loop → commits `henry-books.json` (covers stripped) to `GITHUB_REPO` using `GITHUB_TOKEN`.
- Every endpoint except `/` requires header `x-family-code` == `FAMILY_CODE`.
- CORS from `ALLOWED_ORIGIN` (comma-separated).

Env: `ANTHROPIC_API_KEY`, `FAMILY_CODE`, `ALLOWED_ORIGIN`, `DB_PATH`, `GITHUB_TOKEN`, `GITHUB_REPO`.

## Working on this

- Edit `index.html` directly. Validate with: extract the `<script>` body and run `npx esbuild --target=es2017` on it. That catches syntax errors; there is no test suite.
- Validate the server with `python -c "import ast; ast.parse(open('server/main.py').read())"` and, for behavior, the `fastapi.testclient` snippet in the README history: set `DB_PATH=/tmp/t.db FAMILY_CODE=1234`, then POST/GET/DELETE `/books` with the header.
- **Never hardcode a person.** No prompt and no UI string may contain "Henry"/"헨리" or a
  gendered pronoun — there are four readers and one of them is an adult woman. Prompts say
  `RN()` / `RL()` / `voice()` and use *they*; follow-up questions are written in the second
  person ("you") and the report in the first ("I"). Grep for `he|his|him|헨리` before
  committing a prompt change.
- **Never show a status code to a child.** The server classifies every upstream failure
  (`no_credit`, `rate_limited`, `overloaded`, `bad_key`, `image_too_big`, `upstream`) and the
  page maps each to a sentence naming what a person has to go and do, with the raw text one
  tap away behind "show details". Catch blocks call `fail(e, fallback)`, never `err(e.message)`
  — a swallowed reason once cost an evening working out that the API account was simply empty.
- **Photos are shrunk on the device** (`shrink()`, 1024px long edge, JPEG 0.82) before they
  ever reach the network. The API downsizes big images anyway, so this is about the upload
  completing on a tablet, not about tokens.
- **One tap, one AI call.** `busy()` raises `#veil`, a full-screen pointer trap, and sets
  `S.working`; every async entry point returns early while it is set. Without this a second
  tap started a second call whose answer replaced the first — the question on screen changed
  by itself a moment after it appeared. Any new AI-calling handler needs the same guard.
- Both languages: any new user-facing string goes through `T(en, ko)`. Any new AI prompt must branch on `L()` so Korean books get Korean questions (해요체).
- **Three sizes, one column.** Phone is the default; `@media (min-width:680px)` widens the
  measure, grows the type and turns the stat tiles into one four-across band via
  `.stats .row{display:contents}`; `min-width:1080px` widens a little further and lets the
  artwork carry the rest. `.essay` is capped at 62ch — a desktop window is not a reason to
  set a 9-year-old's writing across 140 characters. Also `@media (hover:none)` for thumb-sized
  targets and `max-height:520px` for a phone held sideways.
- Two themes, one palette contract: `:root` is night, `html[data-theme="day"]` overrides the same tokens, and `bl:theme` remembers the choice per device. Any new surface colour must be a token (`--well`, `--chip`, `--chip2`, `--dim`, `--ph`, `--onAccent`) — a raw hex in CSS or in a JS inline style will survive the theme swap and look broken in daylight.
- Artwork lives in `assets/` as real files, not data URIs — they are same-origin on Pages and stay editable. The palette is warm library (leather, paper cream, gilt); no flat yellow.
- `.hide` must stay `display:none!important` — `.cover{display:flex}` is defined after it and used to win.
- Do not add a build step, a framework, or `localStorage` for book data. The whole point is that the page is a dumb client and the server + GitHub backup are the source of truth.
- Don't gate features behind book counts. HAN explained everything to Henry up front; all tools are available from the first book.

## Deploy (short version — full steps in README.md)

1. Railway: deploy repo, **Root Directory = `server`**, add env vars, **mount volume at `/data`**, generate domain.
2. GitHub: create private `kenpineus-lab/ReadingBook-backup`, fine-grained token (contents: read/write), set `GITHUB_TOKEN` + `GITHUB_REPO`.
3. Pages: this repo → Settings → Pages → `main` / root. URL: https://kenpineus-lab.github.io/ReadingBook/
4. Tablet: open the URL, enter server URL + family code once, add to home screen.

## If this ever becomes a real service

Not now — the family build deliberately stops short of all of this. Recorded so the
questions are on the table the day launching is actually considered.

- **Visibility per book.** `private` / `family` / `crew` on each saved book, defaulting to
  `family`. Today every book in the house is visible to everyone with the family code; that
  is fine for one household and wrong for anyone else. A 4th state — "counts only", where
  outsiders see *that* someone read 12 books but not which, or what they wrote — is better
  modelled as a separate profile-level setting than as a 4th visibility level.
- **Identity is the real blocker, not the UI.** One shared `FAMILY_CODE` is the whole security
  model. Crews need per-person accounts, per-person tokens, and a way to find and add someone
  else's profile — that is real auth, session management, and an abuse surface (who can request
  whom, how you block, what a child can accept without a parent). The visibility flags are an
  afternoon; this is the project.
- **Same book, different reports.** The strongest idea in the pile: two people who read the same
  book comparing what they each wrote. Works inside one family today with no new infrastructure
  (match on normalised title in the `all` view) and is worth building there first — it proves the
  idea before any of the sharing machinery exists.
- **COPPA, and it is not optional.** Sharing a child's writing beyond their own family means
  collecting and disclosing under-13 personal information: verifiable parental consent, a
  retention and deletion policy, and legal review before a single outside account is created.
  Kids' book reports are exactly the category regulators care about. Decide this before writing
  the auth, not after.

## Backlog (not started)

- Let each person pick their own profile icon (currently hardcoded in `PROFILES`).

- "Currently reading" state with chapter-by-chapter check-ins.
- Open Library title search as a fallback when cover recognition fails.
- Parent dashboard (read-only view of all reports and stats).
- If this ever goes beyond the family: COPPA applies to under-13 data in the US. Needs parental-consent flow and legal review before onboarding anyone else's kids.
