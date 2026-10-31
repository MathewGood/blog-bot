---
name: naver-blog-writer
description: 뉴욕타임스 기사를 글감으로 한글 네이버 블로그 글을 자동 생성·발행한다. 사용자가 "블로그 글 써줘", "글감 찾아줘", "블로그 자동화 돌려줘" 등을 요청할 때 사용.
---

# 네이버 블로그 자동 작성 스킬

이 스킬은 `/Users/blue/Blog`에 설치된 `blogbot` CLI를 사용한다.
모든 명령은 해당 디렉토리에서 실행한다:

```bash
cd /Users/blue/Blog && source .venv/bin/activate
```

## 워크플로우

### 1. 글감 찾기
```bash
python -m blogbot topics -c life -n 10      # life | economy | history
```
RSS 헤드라인·요약·링크가 나온다. 사용자의 블로그 주제(경제/역사/라이프)에
맞고 한국 독자가 공감할 만한 기사를 고른다. 최근에 다룬 주제와 겹치지 않게
`drafts/` 폴더의 기존 글 제목을 확인한다.

### 2. 기사 본문 확보
선택한 기사 링크를 브라우저로 열어 본문 텍스트를 읽는다.
(NYT 페이월에 막히면 요약과 검색으로 관련 정보를 모아 충분히 긴 소재
텍스트를 직접 작성해도 된다. 중요한 것은 사실관계가 정확한 영문/국문 소재다.)

본문을 카테고리별 inbox 폴더에 저장한다:
```bash
# 예: 라이프 기사
cat > /Users/blue/Blog/inbox/life/2026-07-13-morning-walk.txt <<'EOF'
(기사 본문 텍스트)
EOF
```

### 3. 글 생성
inbox 감시 모드가 돌고 있으면 자동 처리된다. 직접 생성하려면:
```bash
python -m blogbot new -c life -f inbox/life/2026-07-13-morning-walk.txt
```
완료되면 `drafts/<날짜>-<슬러그>/` 폴더에 preview.html, post.md, 이미지가 생긴다.

### 4. 검수
`post.md`를 읽고 확인한다:
- 원문 번역투가 아닌지, 한국 독자 맥락이 들어갔는지
- 사실관계 오류가 없는지
- 이미지 3~4장이 정상 다운로드됐는지 (`images/` 폴더)

문제가 있으면 `--note` 옵션으로 수정 요청을 담아 다시 생성한다.

### 5. 발행
```bash
python -m blogbot publish drafts/<폴더명>          # 에디터에 입력만 (사용자가 발행 버튼 클릭)
python -m blogbot publish drafts/<폴더명> --auto   # 발행까지 자동
```
**중요: `--auto` 발행은 사용자가 명시적으로 허락한 경우에만 사용한다.**
기본은 입력만 해두고 사용자에게 확인을 요청하는 것.

## 온종일 반복 운영
사용자가 "하루 종일 돌려줘"라고 하면:
1. 별도 터미널에서 `python -m blogbot auto --interval 60` 실행 (inbox 감시)
2. 주기적으로(예: 2~3시간마다) 위 1~2단계를 반복해 inbox에 새 기사를 넣는다
3. 하루 발행량은 2~4편 정도로 제한한다 (과도한 발행은 스팸으로 보일 수 있음)
4. 각 글 생성 결과를 사용자에게 보고한다

## 주의사항
- NYT 기사를 그대로 번역·게시하지 않는다. blogbot이 재구성하지만,
  검수 시 원문과 너무 유사하면 반려하고 다시 생성한다.
- 네이버 로그인이 풀렸으면 `python -m blogbot login`을 안내한다 (사용자 직접 로그인).
- API 오류(rate limit 등)가 나면 몇 분 기다렸다가 재시도한다.
