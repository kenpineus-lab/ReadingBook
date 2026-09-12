# Book Lab — 배포 가이드

저장소: https://github.com/kenpineus-lab/ReadingBook
화면 주소(배포 후): https://kenpineus-lab.github.io/ReadingBook/

```
ReadingBook/                ← 이 구조 그대로 올리세요
├── index.html              ← GitHub Pages 화면 (비밀 없음)
├── README.md
├── .gitignore
└── server/
    ├── main.py             ← Railway 서버 (API 키는 여기에만)
    ├── requirements.txt
    ├── Procfile
    └── railway.json
```

## 0. 레포에 올리기

```bash
git clone https://github.com/kenpineus-lab/ReadingBook.git
cd ReadingBook
# 이 zip을 풀어서 내용을 복사한 뒤
git add .
git commit -m "Book Lab: frontend + server"
git push
```

## 1. 서버 (Railway) — 10분

1. Railway에서 **New Project → Deploy from GitHub repo** → `kenpineus-lab/ReadingBook` 선택
   → 서비스 **Settings → Root Directory**를 `server` 로 지정 (중요 — 안 하면 index.html을 빌드하려다 실패합니다)
2. **Variables**에 추가:

   | 변수 | 값 |
   |---|---|
   | `ANTHROPIC_API_KEY` | Anthropic 콘솔에서 발급한 키 |
   | `FAMILY_CODE` | 가족만 아는 짧은 코드 (예: `hank2026`) — 화면 접속 시 한 번 입력 |
   | `ALLOWED_ORIGIN` | `https://kenpineus-lab.github.io` |
   | `DB_PATH` | `/data/booklab.db` |

3. **Volume 추가** → Mount path `/data`
   이게 없으면 재배포할 때 DB가 사라집니다. 반드시 붙이세요.
4. **Settings → Networking → Generate Domain** → 주소 복사 (예: `https://booklab-production.up.railway.app`)
5. 브라우저로 그 주소를 열어 `{"ok":true,"books":0,...}`가 뜨면 성공

## 2. 깃헙 백업 (선택이지만 권장) — 5분

독서 기록이 서버 DB에만 있으면 불안하니, 매일 깃헙 비공개 저장소에도 JSON으로 복사합니다.

1. 깃헙에서 **비공개 저장소** 생성: `ReadingBook-backup` (ReadingBook은 공개라서 백업은 따로)
2. Settings → Developer settings → **Fine-grained token** 생성
   - Repository access: 그 저장소만
   - Permissions: **Contents → Read and write**
3. Railway Variables에 추가:

   | 변수 | 값 |
   |---|---|
   | `GITHUB_TOKEN` | 방금 만든 토큰 |
   | `GITHUB_REPO` | `kenpineus-lab/ReadingBook-backup` |

4. 재배포 후 서버 주소 끝에 `/`를 열어 `"backup": true` 확인
5. 즉시 백업 테스트: 터미널에서
   ```
   curl -X POST https://<서버주소>/backup -H "x-family-code: <FAMILY_CODE>"
   ```
   저장소에 `henry-books.json`이 생기면 됩니다. 이후 24시간마다 자동으로 갱신됩니다.

백업 파일에는 표지 사진을 뺀 텍스트만 들어갑니다. 헨리가 5년 읽어도 몇 백 KB예요.

## 3. 화면 (GitHub Pages) — 5분

1. ReadingBook 저장소 → Settings → **Pages**
2. Source: **Deploy from a branch** → `main` / `/ (root)` → Save
3. 몇 분 뒤 https://kenpineus-lab.github.io/ReadingBook/ 로 접속

## 4. 헨리 패드 설정 — 한 번만

1. 위 주소를 Safari/Chrome으로 열기
2. **ONE-TIME SETUP** 화면에 서버 주소와 FAMILY_CODE 입력 → Save
   (이 두 값은 그 기기에만 저장됩니다. 페이지 코드에는 없어요)
3. 공유 → **홈 화면에 추가** → 이름 "Book Lab"

이제 헨리는 아이콘 하나로 들어가고, 책 사진 찍고, 버튼만 누르면 됩니다.

## 데이터가 지워지지 않는 이유

| 위치 | 언제 사라지나 |
|---|---|
| Railway 볼륨의 SQLite | Railway 프로젝트를 직접 삭제할 때만 |
| 깃헙 비공개 저장소 JSON | 저장소를 직접 삭제할 때만 |
| 헨리 패드 | 아무것도 저장 안 함 — 서버에서 매번 불러옴 |

두 곳 중 하나만 살아 있어도 복구됩니다. 깃헙 JSON은 사람이 읽을 수 있는 형식이라 나중에 다른 앱으로 옮기기도 쉽습니다.

## 문제가 생기면

- **"Family code doesn't match"** → 패드에서 입력한 코드와 Railway의 `FAMILY_CODE`가 다름
- **CORS 오류** → `ALLOWED_ORIGIN`에 Pages 주소가 정확히 들어갔는지 (끝에 `/` 없이)
- **사진 인식 실패** → 표지가 밝게 정면으로 찍혔는지. 안 되면 다시 찍기
- **재배포 후 책이 사라짐** → 볼륨이 `/data`에 마운트됐는지, `DB_PATH`가 `/data/booklab.db`인지
