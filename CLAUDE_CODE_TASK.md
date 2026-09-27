# Claude Code에 붙여넣을 작업 지시

아래를 그대로 Claude Code에 붙여넣으세요. 이 폴더(`book-lab/`)를 레포 루트로 쓰는 전제입니다.

---

이 폴더는 Book Lab 프로젝트다. `CLAUDE.md`를 먼저 읽고 시작해라.

## 해야 할 일

1. **레포에 올리기**
   - 원격: https://github.com/kenpineus-lab/ReadingBook
   - 이 폴더의 내용(`index.html`, `CLAUDE.md`, `README.md`, `.gitignore`, `server/`)을 레포 루트에 그대로 올려라. 레포가 비어 있지 않으면 기존 파일과 충돌하는지 먼저 보여주고 물어봐라.
   - 커밋 메시지: `Book Lab: frontend, server, docs`
   - push 전에 `git status`와 `git diff --stat`을 보여주고 확인을 받아라.

2. **올리기 전 검증**
   - `index.html`의 `<script>` 본문을 추출해 `npx esbuild --target=es2017`로 문법 검사
   - `python -c "import ast; ast.parse(open('server/main.py').read())"`
   - 둘 다 통과해야 push한다. 실패하면 고치지 말고 뭐가 틀렸는지 보여줘라.

3. **GitHub Pages 켜기**
   - `gh` CLI가 있으면: `gh api -X POST repos/kenpineus-lab/ReadingBook/pages -f source[branch]=main -f source[path]=/`
   - 없으면 나한테 Settings → Pages 에서 켜라고 알려줘라.
   - 배포 후 https://kenpineus-lab.github.io/ReadingBook/ 이 200을 돌려주는지 `curl -I`로 확인.

4. **백업용 비공개 레포 만들기** (gh가 있을 때만)
   - `gh repo create kenpineus-lab/ReadingBook-backup --private --description "Henry reading log backup"`
   - 토큰 발급은 내가 직접 한다. 어디서 만드는지만 안내해라 (fine-grained, contents read/write, 이 레포만).

5. **Railway 배포**
   - 직접 하지 마라. README.md 1번 섹션을 요약해서 내가 따라 할 체크리스트로 만들어줘라.
   - 특히 Root Directory = `server`, 볼륨 `/data` 마운트, 환경변수 6개를 빠짐없이 적어라.

## 하지 말 것

- API 키, 토큰, 가족 코드를 어떤 파일에도 쓰지 마라. 전부 Railway 환경변수다.
- `index.html`에 빌드 도구나 프레임워크를 넣지 마라.
- 내 확인 없이 push하거나 외부 서비스를 만들지 마라.

끝나면 다음 세 줄만 보고해라: Pages 주소 / 백업 레포 주소 / Railway에서 내가 넣어야 할 값 목록.
