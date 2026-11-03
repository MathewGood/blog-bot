# blogbot — NYT 기사 → 한글 네이버 블로그 자동 작성

영문 기사(주로 뉴욕타임스)를 입력하면, AI 티가 나지 않는 자연스러운 한글
블로그 글로 **재구성**하고, 무료 스톡 이미지 3~4장을 곁들여 네이버 블로그에
올릴 수 있는 초안을 만들어 주는 도구입니다.

- 카테고리: 경제 / 역사 / 라이프
- 글 생성: Claude API (구조화 출력 + 프롬프트 캐싱)
- 이미지: Pexels / Unsplash 무료 스톡 자동 검색 (저작권 안전, 출처 표기)
- 발행: Playwright로 네이버 스마트에디터에 자동 입력
- OpenClaw 연동: 글감 수집→생성→발행을 하루 종일 반복

## 설치

```bash
cd /Users/blue/Blog
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# 네이버 발행 기능을 쓸 경우:
playwright install chromium

cp .env.example .env
# .env를 열어 API 키를 채운다:
#   ANTHROPIC_API_KEY  (필수)
#   PEXELS_API_KEY 또는 UNSPLASH_ACCESS_KEY  (이미지용, 무료 발급)
#   NAVER_BLOG_ID      (발행용)
```

## 사용법

### 0. 웹 GUI (권장)

**가장 쉬운 방법**: Finder에서 `blogbot.command` 더블클릭.
터미널이 열리면서 대시보드(http://127.0.0.1:8765)가 브라우저에 자동으로 뜹니다.

터미널로 실행하려면 (반드시 가상환경을 먼저 활성화):
```bash
cd /Users/blue/Blog
source .venv/bin/activate
python -m blogbot web
```
> ⚠ `source .venv/bin/activate` 없이 실행하면 `command not found: python` 또는
> `ModuleNotFoundError`가 납니다. 아래 모든 CLI 명령도 마찬가지입니다.
① 글감 찾기(NYT RSS) → ② 기사 붙여넣고 글 생성 → ③ 초안 미리보기·네이버 발행을
모두 버튼으로 제어할 수 있습니다. 아래 CLI 명령들은 같은 기능의 터미널 버전입니다.

### 1. 글감 찾기 (선택)
```bash
python -m blogbot topics -c life
```
카테고리: `life` | `economy` | `history`
NYT RSS에서 최신 기사 제목·요약·링크를 보여줍니다.

### 2. 글 생성
```bash
# 파일로 입력
python -m blogbot new -c life -f article.txt --open
# 붙여넣기로 입력 (Ctrl-D로 종료)
python -m blogbot new -c economy
```
결과는 `drafts/<날짜>-<슬러그>/`에 저장됩니다:
- `preview.html` — 브라우저 미리보기 (이미지 포함)
- `post.md` — 마크다운 원고
- `post.json` — 메타데이터
- `images/` — 다운로드된 이미지

`--note "제목에 커피를 넣어줘"` 처럼 추가 요청도 가능합니다.

### 3. 네이버 발행
```bash
# 최초 1회, 직접 로그인 (세션 저장)
python -m blogbot login
# 에디터에 입력 → 발행 버튼은 직접 클릭
python -m blogbot publish drafts/2026-07-13-xxx
# 발행까지 자동
python -m blogbot publish drafts/2026-07-13-xxx --auto
```
> 네이버 UI 변경 시 `blogbot/naver.py`의 셀렉터 수정이 필요할 수 있습니다.
> 자동화가 실패하면 `preview.html`을 브라우저로 열어 전체 선택(⌘A)→복사→
> 스마트에디터에 붙여넣기 해도 이미지까지 잘 들어갑니다.

### 4. 온종일 자동 모드
```bash
python -m blogbot auto --interval 60
```
`inbox/life/`, `inbox/economy/`, `inbox/history/`에 기사 텍스트 파일(.txt/.md)을
넣으면 자동으로 글을 생성합니다. 처리된 파일은 `inbox/processed/`로 이동합니다.
`--auto-publish`를 붙이면 네이버 발행까지 자동으로 합니다.

## OpenClaw 연동

`openclaw-skill/naver-blog-writer/`를 OpenClaw 스킬 폴더로 복사하세요:

```bash
mkdir -p ~/.openclaw/skills
cp -r openclaw-skill/naver-blog-writer ~/.openclaw/skills/
```

이후 OpenClaw에게 이렇게 시킬 수 있습니다:
- "블로그 글감 찾아서 라이프 글 하나 써줘"
- "오늘 하루 2~3시간마다 글감 찾아서 블로그 글 만들어줘"

OpenClaw이 글감 검색 → 기사 확보 → `inbox/`에 저장 → 생성 → 검수 → 발행
안내까지 수행합니다. 자동 발행(`--auto`)은 사용자가 명시적으로 허락할 때만
사용하도록 스킬에 명시되어 있습니다.

## 검색 노출·체류시간 구성

해설형 글(사연 각색·판 인기글 해설 제외)은 본문이 끝난 뒤 `blogbot/seo.py`가 다음을 붙입니다.

- **제목**: `검색 앵커 명사구 : 후킹 문구` (50자 이내). 본문에 없는 숫자·수량 표현은 제목에 쓰지 못합니다.
- **비교표·체크리스트**(본문 중반), **마무리 질문**, 그 뒤 맨 마지막에 **30초 요약 카드**. 숫자는 본문에 있는 것만 허용합니다.
- **모바일 문단**: 한 문단 110자(약 3~4줄) 이내로 문장 경계에서 나눕니다.

## 문체 조정

글 스타일이 마음에 안 들면 `prompts/style_guide.md`를 수정하세요.
이 파일이 곧 프롬프트입니다. (페르소나, 문체, 금지 패턴, 분량 등)

## 저작권 주의

이 도구는 기사를 번역·복제하지 않고 **소재로 삼아 재구성**하도록
설계되어 있습니다. 그래도 발행 전에 결과물이 원문과 지나치게 유사하지
않은지 확인하는 것을 권장합니다. 이미지는 Pexels/Unsplash 무료 라이선스
사진만 사용하며 출처를 캡션에 표기합니다.

## 비용 참고

기본 모델은 `claude-opus-4-8`($5/$25 per 1M tokens)입니다. 글 1편에
대략 수천~1만 토큰 수준이며, 시스템 프롬프트는 캐싱되어 반복 실행 시
비용이 절감됩니다. 더 저렴하게 쓰려면 `.env`에서
`BLOG_MODEL=claude-sonnet-5`로 바꾸세요.

## 판 인기글 — 발췌 + 사회적 관점 해설
판 인기글은 각색하지 않고, 원문에서 짧은 발췌문 3~4개(닉네임·지역·연락처 등은 ○○로 가림)를 인용 블록으로 넣은 뒤
그 사연을 사회적 관점에서 어떻게 볼지 쓰는 해설 칼럼으로 씁니다. 발췌문은 원문과 대조해 확인된 것만 쓰고(2개 미만이면 제외),
교정 단계가 인용문을 고쳐 쓰면 원본으로 되돌리며 지어낸 인용은 지웁니다. 글 끝에 발췌·출처 안내와 원문 주소가 붙습니다.
직접 붙여넣은 사연은 기존의 각색 이야기 방식입니다. 문체 규칙은 `prompts/pann_commentary.md`.
