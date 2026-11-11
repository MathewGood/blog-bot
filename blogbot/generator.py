"""Claude API로 원문 기사를 한글 블로그 글로 재구성한다.

kimi-* / moonshot-* 모델을 선택하면 Anthropic 대신 Moonshot AI(OpenAI 호환)로 라우팅된다.
"""

from __future__ import annotations

import json
import time
import re
import threading

import anthropic
import requests

from . import config

POST_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "블로그 글 제목 (한글). '검색 앵커 명사구 : 후킹 문구' 형식(예: '증여세 면제 한도 : 부모님 비상금 이체 전에 알아둘 3가지'). 앵커(독자가 검색창에 칠 핵심 검색어)를 맨 앞에 두고 전체 38자 안팎(최대 50자). 첫 섹션 첫 문단 첫 문장에 이 앵커를 그대로 넣는다"},
        "slug": {
            "type": "string",
            "description": "영문 소문자 kebab-case 슬러그 (예: why-koreans-love-coffee)",
        },
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "네이버 블로그 태그 6~10개 (한글, # 없이, 공백 없이)",
        },
        "sections": {
            "type": "array",
            "description": "글 본문 8~10개 섹션(각 600~900자). 첫 섹션은 도입부라 heading을 빈 문자열로 둔다.",
            "items": {
                "type": "object",
                "properties": {
                    "heading": {
                        "type": "string",
                        "description": "소제목. 도입부/마무리는 빈 문자열",
                    },
                    "body_md": {
                        "type": "string",
                        "description": "마크다운 본문 (여러 문단 가능)",
                    },
                    "highlight": {
                        "type": "string",
                        "description": (
                            "이 섹션의 핵심을 담은 강조 문구 한 문장(공백 포함 12~30자, ~다 체, 숫자 없이, 따옴표 없이). "
                            "통계 요약이 아니라 기억에 남는 교훈 한 줄. 도입부·마무리 섹션은 빈 문자열"
                        ),
                    },
                },
                "required": ["heading", "body_md", "highlight"],
                "additionalProperties": False,
            },
        },
        "images": {
            "type": "array",
            "description": "삽입할 이미지 4개(최대 4개). 섹션 사이에 고르게 배치한다.",
            "items": {
                "type": "object",
                "properties": {
                    "after_section": {
                        "type": "integer",
                        "description": "이 인덱스의 섹션 뒤에 삽입 (0부터)",
                    },
                    "query_en": {
                        "type": "string",
                        "description": "스톡 사진 검색용 영어 키워드 2~4단어. 해당 섹션 내용과 직결되는 구체적인 사물/장면",
                    },
                    "alt_query_en": {
                        "type": "string",
                        "description": "첫 검색이 부적합할 때 쓸 대안 영어 검색어 (다른 각도의 장면/사물)",
                    },
                    "caption": {
                        "type": "string",
                        "description": "사진이 무엇인지 설명하는 간략한 한글 캡션 한 문장(25자 안팎, ~다 체). 출처·촬영자·AI 여부는 쓰지 않는다",
                    },
                },
                "required": ["after_section", "query_en", "alt_query_en", "caption"],
                "additionalProperties": False,
            },
        },
        "source_line": {
            "type": "string",
            "description": (
                "글 맨 마지막에 붙일 출처 표기. 형식: "
                "'출처: [원문 제목] by [저자명] ([URL])'. "
                "URL은 대화 맥락(원문 참고 프롬프트)에 명시되어 있으면 그대로 사용하고, "
                "없으면 URL 부분을 빈 괄호 대신 아예 생략해 '출처: [제목] by [저자명]' 형태로만 쓴다. "
                "저자를 특정할 수 없으면 'by [저자명]'을 생략한다. "
                "출처가 없으면 빈 문자열."
            ),
        },
    },
    "required": ["title", "slug", "tags", "sections", "images", "source_line"],
    "additionalProperties": False,
}


_AUD = threading.local()   # generate_post()가 이 스레드의 독자 설정을 담는다


def _audience() -> str:
    return getattr(_AUD, "v", "women")


_STORY_AUD = {
    "women": ("가족·관계·살림·건강 같은 생활 이야기에 공감하는 40~65세 여성",
              "시어머니·남편·자녀·친구 사이의 엇갈림처럼 집과 가족 안의 장면이 잘 통한다."),
    "worker": ("직장생활을 오래 한 40~65세 직장인",
               "상사·동료·후배, 회식, 출퇴근, 월급과 가계 같은 직장·생활 장면이 잘 통한다."),
}


def _story_mode() -> bool:
    return getattr(_AUD, "mode", "feature") == "story"


def _pann_mode() -> bool:
    """판 인기글 — 사연을 일부 발췌해 사회적 관점으로 해설하는 글(각색 이야기가 아님)."""
    return getattr(_AUD, "mode", "feature") == "pann"


def _style_text() -> str:
    """문체 가이드. 사연 각색 모드면 각색용 가이드로 통째로 바꾸고,
    직장인 독자면 페르소나·독자 원칙 부분만 직장인용으로 바꾼다."""
    if _story_mode() or _pann_mode():
        who, hint = _STORY_AUD.get(_audience(), _STORY_AUD["women"])
        fname = "pann_commentary.md" if _pann_mode() else "story_adapt.md"
        text = (config.ROOT / "prompts" / fname).read_text(encoding="utf-8")
        return text.replace("{독자결}", hint).replace("{독자}", who)
    style = config.STYLE_GUIDE.read_text(encoding="utf-8")
    if _audience() == "worker":
        a, b = style.index("## 페르소나"), style.index("## 잡지 피처의 문법")
        worker = (config.ROOT / "prompts" / "audience_worker.md").read_text(encoding="utf-8")
        style = style[:a] + worker + "\n" + style[b:]
    # 트렌드·뉴스 해설은 돈과 자본의 흐름으로 원인·전망을 분석한다
    money = (config.ROOT / "prompts" / "money_lens.md").read_text(encoding="utf-8")
    who = _STORY_AUD.get(_audience(), _STORY_AUD["women"])[0]
    return style + "\n" + money.replace("{독자}", who)


def _system_prompt() -> str:
    style = _style_text()
    intro = ("당신은 한국의 개인 블로거를 대신해 글을 쓰는 대필 작가입니다.\n"
             "온라인 커뮤니티 사연의 일부를 인용하고, 그 사연을 사회적 관점에서 해설하는 칼럼을 씁니다.\n"
             if _pann_mode() else
             "당신은 한국의 개인 블로거를 대신해 글을 쓰는 대필 작가입니다.\n"
             "사연 요약을 뼈대로 받아, 인물과 장면을 새로 지은 재미있는 각색 이야기로 씁니다.\n"
             if _story_mode() else
             "당신은 한국의 개인 블로거를 대신해 글을 쓰는 대필 작가입니다.\n"
             "영문 기사(주로 뉴욕타임스)를 소재로 받아, 그것을 완전히 소화한 뒤\n"
             "한국 독자를 위한 독자적인 블로그 글로 재구성합니다.\n")
    return f"""{intro}
가장 중요한 목표: **AI가 쓴 티가 전혀 나지 않는, 진짜 사람이 쓴 것 같은 글.**
아래 문체 가이드를 철저히 따르세요.

{style}

이미지 계획도 함께 세웁니다. 이미지는 무료 스톡 사진 사이트(Pexels)에서
검색하므로, query_en은 스톡 사진에 실제로 존재할 법한 보편적인 장면·사물로
정하세요 (예: "morning coffee window" O, "korean economy graph 2026" X).
사람이 나오는 장면이라면 한국인(동북아시아인)이 나오는 장면으로 계획하세요.
""" + config.time_note()


def _research_block(research_brief: str) -> str:
    """리서치 브리프를 프롬프트에 삽입할 블록으로 만든다."""
    if not research_brief:
        return ""
    brief = research_brief[:8000]  # 5,000자 내외 작성을 위해 자료를 넉넉히 전달
    return f"""

<research>
{brief}
</research>

지침: 위 <research>는 공신력 있는 출처에서 조사한 보강 자료다.
- 이 자료의 사실·수치를 본문에 자연스럽게 녹여 내용을 더 충실하게 만들어라.
- 항목 대부분을 실제로 활용해 섹션을 채워라. 분량(5,000자 내외)은 이 자료로 채우는 것이다.
- 출처 기관명은 "질병관리청에 따르면"처럼 문장 안에서 자연스럽게 언급해도 좋다.
- 원문 기사와 <research>에 없는 수치·사실은 절대 지어내지 마라."""


MIN_BODY_CHARS = 4500   # 이보다 짧으면 확장 패스를 돌린다 (최대 2회)
MAX_EXPAND_PASSES = 2
TARGET_BODY_CHARS = "4,500~5,500자(5,000자 내외)"


def body_chars(post: dict) -> int:
    return sum(len(s.get("body_md") or "") for s in post.get("sections", []))


def _expand_prompt(post: dict, research_brief: str | None) -> str:
    if _pann_mode():
        return f"""아래는 방금 쓴 사연 해설 칼럼의 JSON이다. 분량이 목표({TARGET_BODY_CHARS})에 미치지 못한다. 현재 본문 {body_chars(post)}자.

같은 글감·같은 문체(~다 체, 의견은 "~라고 본다")를 유지하면서 {TARGET_BODY_CHARS}로 늘려라.
- 인용 블록(`> `로 시작하는 문단)은 한 글자도 바꾸거나 새로 만들지 않는다. 늘리는 것은 **필자의 해설 문단**이다.
- 늘리는 재료는 **양쪽 입장의 사정, 구조·관행·세대·인식의 차이 읽기, 누구나 겪는 일반적 사례(통계 없이)와 비유, 필자의 생각, 작은 변화 제안**이다.
- 문단은 새 문단을 추가해 채우고, 문단 하나는 2문장·70~110자로 짧게 끊는다.
- 같은 내용을 되풀이하거나 요약으로 늘리지 않는다. 통계·연구·기관·실존 인물·브랜드·사연에 없는 사실은 넣지 않는다.
- 제목·슬러그·이미지 계획·source_line(빈 문자열)은 바꾸지 않는다. 첫·마지막 섹션 heading은 빈 문자열, 중간 섹션의 highlight는 12~30자 ~다 체 숫자 없이.

같은 JSON 스키마로 전체 글을 다시 출력하라.

<current_post>
{json.dumps(post, ensure_ascii=False)}
</current_post>"""
    if _story_mode():
        return f"""아래는 방금 쓴 각색 이야기의 JSON이다. 분량이 목표({TARGET_BODY_CHARS})에 미치지 못한다. 현재 본문 {body_chars(post)}자.

같은 이야기·같은 인물·같은 문체(3인칭 ~다 체, 대사는 큰따옴표 구어체)를 유지하면서 {TARGET_BODY_CHARS}로 늘려라.
- 섹션이 7~8개가 되게 장면을 나누거나 추가하고, 각 섹션을 550~750자(짧은 문단 5~6개)로 채운다.
- 늘리는 재료는 **장면 묘사, 대사 주고받기, 인물의 행동·표정, 사소한 소품, 서술자의 능청스러운 한마디, 웃음 포인트**다.
- 같은 내용을 되풀이하거나 요약으로 늘리지 않는다. 통계·연구·기관·실존 인물·브랜드는 넣지 않는다.
- 제목·슬러그·이미지 계획·source_line(빈 문자열)은 바꾸지 않는다. 첫·마지막 섹션 heading은 빈 문자열, 중간 섹션의 highlight는 12~30자 ~다 체 숫자 없이.

같은 JSON 스키마로 전체 글을 다시 출력하라.

<current_post>
{json.dumps(post, ensure_ascii=False)}
</current_post>"""
    return f"""아래는 방금 작성한 블로그 글의 JSON이다. 분량이 목표({TARGET_BODY_CHARS})에
미치지 못한다. 현재 본문 {body_chars(post)}자.

이 글을 **같은 주제·같은 문체·같은 구성**으로 유지하면서 {TARGET_BODY_CHARS}로 늘려라.

늘리는 방법(반드시 이 방식만 사용):
- 문단은 길게 늘이지 말고 **새 문단을 추가해** 채운다. 문단 하나는 2문장·70~110자(모바일 3~4줄)로 짧게 끊는다. 사실 → 맥락 → 그래서 무엇이 달라지는가 순으로 전개한다.
- <research> 자료에서 아직 쓰지 않은 통계·연구·사례·인물을 더 가져와 새 문단을 만든다.
- 구체적인 장면·숫자·비교를 추가한다 (잡지 피처처럼 읽히게).
- 필요하면 섹션을 1~2개 더 만든다. 한국 관련 섹션은 2개 이상 유지한다.
- 늘리는 방향은 **돈의 흐름 분석**이다: 누가 비용을 내고 누가 얻는지, 규모·비중의 비교, 앞으로의 시나리오(조건부), 가계에 닿는 의미. 전망은 단정하지 않는다.

금지:
- 같은 내용을 다른 말로 반복하거나, 앞 내용을 요약하는 문단 추가
- **자료에 없는 수치를 만들어 내는 것.** 숫자는 <research>나 원문에 있는 것만 쓰고,
  쓸 때는 출처 기관·연도를 함께 밝힌다. 근거가 없으면 숫자 없이 서술로 채운다.
  ("서울 직장인은 평균 4,000보" 처럼 출처 없는 추정치를 쓰면 실패다.)
- 문체 변경(~다 체 유지), 제목·슬러그·이미지 계획·출처 라인 변경

같은 JSON 스키마로 전체 글을 다시 출력하라.

<current_post>
{json.dumps(post, ensure_ascii=False)}
</current_post>{_research_block(research_brief)}"""


def _split_long_paragraphs(post: dict, target: int = 190):
    """각색 이야기: 한 덩어리로 길게 나온 문단을 문장 경계에서 나눠 읽기 편하게 한다(모델이 문단을 안 나눌 때의 안전장치)."""
    for sec in post.get("sections", []):
        paras = [p for p in (sec.get("body_md") or "").split("\n\n") if p.strip()]
        out = []
        for p in paras:
            if len(p) <= 320:
                out.append(p.strip())
                continue
            sents = re.split(r"(?<=[.!?…”\"])\s+", p.strip())
            cur = ""
            for s in sents:
                if cur and len(cur) >= target:
                    out.append(cur.strip())
                    cur = ""
                cur += (" " if cur else "") + s
            if cur.strip():
                out.append(cur.strip())
        sec["body_md"] = "\n\n".join(out)


def generate_post(*args, audience: str = "women", mode: str = "feature", **kwargs) -> dict:
    """글 생성 + 강조 문구 보정(본 생성이 기준을 어긴 섹션만 짧게 다시 쓴다).

    audience: 'women'(40~65세 여성, 기본) | 'worker'(40~65세 직장인).
    mode: 'feature'(기사·자료 기반 해설) | 'story'(사연을 각색한 읽을거리 — 자료 없이 이야기를 새로 짓는다)
    | 'pann'(판 인기글 — 사연 일부를 발췌하고 사회적 관점으로 해설)."""
    audience = audience if audience in config.AUDIENCES else "women"
    _AUD.v = audience
    _AUD.mode = mode if mode in ("feature", "story", "pann") else "feature"
    try:
        post = _generate_post(*args, **kwargs)
        post["audience"] = audience
        if _pann_mode():
            post["source_line"] = config.PANN_NOTICE
        elif _story_mode():
            post["source_line"] = config.STORY_NOTICE
            _split_long_paragraphs(post)
        try:
            return ensure_highlights(post)
        except Exception as e:  # 강조 문구는 부가 요소 — 실패해도 글 생성은 성공 처리
            print(f"  ⚠ 강조 문구 보정 건너뜀: {str(e)[:80]}")
            return post
    finally:
        _AUD.v = "women"
        _AUD.mode = "feature"


_HL_PROMPT = """한국 블로그(독자: {audience}) 글의 섹션별 '강조 문구'를 쓴다.
강조 문구는 화면에서 큰 글씨 인용구(“ ”)로 보이는, 독자가 기억할 한 문장이다.
규칙: 공백 포함 12~30자 · ~다 체 · 숫자·고유명사·따옴표 금지 · 통계 요약이 아니라 교훈이나 통찰 ·
본문 문장 그대로 베끼기 금지 · 불안을 부추기지 않는 차분한 어조.
좋은 예: "돌봄은 혼자 짊어질수록 오래가지 못한다", "노후 준비는 금액보다 순서가 먼저다"

섹션들:
{sections}

JSON만 출력: {{"highlights":[{{"i":섹션번호,"text":"문구"}}]}}"""


def ensure_highlights(post: dict) -> dict:
    """기준(숫자 없음·8~45자)에 맞지 않거나 빠진 강조 문구를 Haiku(없으면 Kimi)로 채운다."""
    from . import builder, llm_guard

    cleaned = builder.clean_highlights(post)
    need = [i for i, s in enumerate(cleaned["sections"])
            if s.get("heading", "").strip() and not s.get("highlight")]
    if not need:
        return cleaned
    block = "\n".join(
        f"[{i}] 소제목: {cleaned['sections'][i]['heading']}\n{cleaned['sections'][i]['body_md'][:700]}"
        for i in need)
    prompt = _HL_PROMPT.format(sections=block, audience=config.AUDIENCES[_audience()]["label"])
    text = ""
    if llm_guard.anthropic_available():
        try:
            client = anthropic.Anthropic(timeout=60.0, max_retries=1)
            resp = client.messages.create(model=config.CLAUDE_MODEL, max_tokens=1200,
                                          messages=[{"role": "user", "content": prompt}])
            text = "".join(b.text for b in resp.content if b.type == "text")
        except Exception as e:
            if llm_guard.is_credit_error(e):
                llm_guard.mark_credit_out("Anthropic 크레딧 부족")
    if not text and config.KIMI_API_KEY:
        from . import research

        text = research._kimi_chat([{"role": "user", "content": prompt}], config.FALLBACK_MODEL,
                                   max_tokens=1200, json_mode=True, timeout=90)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return cleaned
    for h in json.loads(m.group(0)).get("highlights", []):
        i = h.get("i")
        if isinstance(i, int) and i in need:
            cleaned["sections"][i]["highlight"] = str(h.get("text") or "")
    return builder.clean_highlights(cleaned)


def _generate_post(
    article_text: str,
    category: str,
    extra_note: str = "",
    model: str | None = None,
    research_brief: str | None = None,
) -> dict:
    """영문 기사 텍스트 → 블로그 글 dict (POST_SCHEMA 형태).

    model을 지정하지 않으면 config.MODEL(.env의 BLOG_MODEL 또는 기본값)을 사용한다.
    kimi-* / moonshot-* 모델은 Moonshot AI(OpenAI 호환 API)로 분기한다.
    research_brief가 있으면 공신력 자료로 본문을 보강한다.
    """
    from . import llm_guard

    category_ko = config.CATEGORIES.get(category, category)
    selected_model = config.enforce_model(model or config.MODEL)

    # EXAONE 분기 (LG AI연구원, OpenAI 호환) — Anthropic 클라이언트 초기화조차 하지 않음
    if config.is_exaone_model(selected_model):
        try:
            return _generate_with_exaone(
                article_text, category_ko, extra_note, selected_model, research_brief
            )
        except ProviderBusy as e:
            # 재시도(최대 약 6분)로도 EXAONE이 풀리지 않으면 글 한 편이 통째로 실패하지 않게 Kimi로 대신 쓴다
            if not config.KIMI_API_KEY:
                raise
            post = _generate_with_kimi(
                article_text, category_ko, extra_note, config.FALLBACK_MODEL, research_brief
            )
            post["fallback_from"] = selected_model
            post["fallback_reason"] = f"EXAONE 서비스 과부하: {str(e)[:80]}"
            return post

    # Kimi/Moonshot 분기 — Anthropic 클라이언트 초기화조차 하지 않음
    if config.is_kimi_model(selected_model):
        return _generate_with_kimi(
            article_text, category_ko, extra_note, selected_model, research_brief
        )

    def _fallback(reason: str) -> dict:
        """Claude를 못 쓸 때 Kimi로 전환 (키가 없으면 원래 오류를 그대로 알린다)."""
        if not config.KIMI_API_KEY:
            raise llm_guard.CreditExhausted(
                f"{reason} — 전환할 Kimi 키도 없어 생성할 수 없습니다. "
                "platform.claude.com → Billing에서 충전하세요."
            )
        post = _generate_with_kimi(
            article_text, category_ko, extra_note, config.FALLBACK_MODEL, research_brief
        )
        post["fallback_from"] = selected_model
        post["fallback_reason"] = reason
        return post

    # 차단기가 걸려 있으면(최근 크레딧 부족 감지) 호출 없이 바로 Kimi로
    if not llm_guard.anthropic_available():
        return _fallback("Anthropic 크레딧 부족(차단 중)")

    try:
        post = _generate_with_claude(
            article_text, category_ko, extra_note, selected_model, research_brief
        )
        llm_guard.mark_ok()
        return _expand_if_short(post, selected_model, research_brief,
                                material_chars=len(article_text or "") + len(research_brief or ""))
    except Exception as e:
        # 타입이 아니라 '메시지'로 판단한다. 스트리밍 중 오류는 BadRequestError가 아니라
        # 본문 dict만 담은 다른 예외로 올라와 예전에는 폴백이 걸리지 않았다(2026-09-21 실측).
        if llm_guard.is_credit_error(e):
            llm_guard.mark_credit_out("Anthropic 크레딧 부족")
            return _fallback("Anthropic 크레딧 부족")
        transient = isinstance(e, (anthropic.APITimeoutError, anthropic.APIConnectionError,
                                   anthropic.RateLimitError, anthropic.InternalServerError))
        if not transient:
            # overloaded(529)·502 등도 문자열로 판별 (스트리밍 오류 이벤트 대비)
            msg = str(e).lower()
            transient = any(k in msg for k in ("overloaded", "rate limit", "timeout",
                                               "internal server error", "503", "529"))
        if transient and config.KIMI_API_KEY:
            return _fallback(f"Claude 일시 장애({type(e).__name__})")
        raise


# 원문+조사 자료가 이보다 적으면 분량 확장을 하지 않는다. 자료가 빈약한데 늘리라고 하면
# 모델이 기억에 의존해 사실을 지어낸다 (2026-10-04 실측: 금나라 인구 600만, 요나라 400년 등).
MIN_MATERIAL_FOR_EXPAND = 5000


def _expand_if_short(post: dict, selected_model: str, research_brief: str | None,
                      log=None, material_chars: int = None) -> dict:
    """목표 분량에 못 미치면 같은 모델로 한 번 더 늘린다. 실패하면 원본을 그대로 쓴다."""
    started_at = body_chars(post)
    # 각색 글은 자료가 짧은 게 정상이고 지어낼 사실도 없으므로 자료 길이와 무관하게 확장한다
    if (not (_story_mode() or _pann_mode()) and material_chars is not None and material_chars < MIN_MATERIAL_FOR_EXPAND
            and started_at < MIN_BODY_CHARS):
        post["expand_skipped"] = (f"자료 {material_chars:,}자로 부족 — 사실 오류를 막기 위해 "
                                  f"분량 확장 생략 (본문 {started_at:,}자)")
        return post
    client = anthropic.Anthropic(timeout=300.0, max_retries=1)
    for _ in range(MAX_EXPAND_PASSES):
        n = body_chars(post)
        if n >= MIN_BODY_CHARS:
            break
        if log:
            log(f"  · 본문 {n}자 — 목표({TARGET_BODY_CHARS})에 미달해 확장 중...")
        try:
            resp = client.messages.create(
                model=selected_model,
                max_tokens=32000,
                system=[{"type": "text", "text": _system_prompt(),
                         "cache_control": {"type": "ephemeral"}}],
                output_config={"format": {"type": "json_schema", "schema": POST_SCHEMA}},
                messages=[{"role": "user", "content": _expand_prompt(post, research_brief)}],
            )
            text = next(b.text for b in resp.content if b.type == "text")
            bigger = json.loads(text)
        except Exception:
            break
        if body_chars(bigger) <= n:  # 더 길어지지 않으면 그만
            break
        # 확장 결과는 스키마 필드만 담고 있으므로, 생성 단계에서 붙인 부가 필드
        # (category·model·fallback 정보 등)를 그대로 이어 붙인다 (예전엔 여기서 유실됐음)
        for k, v in post.items():
            if k not in POST_SCHEMA["properties"]:
                bigger.setdefault(k, v)
        post = bigger
    if body_chars(post) != started_at:
        post["expanded_from"] = started_at
    return post


def _generate_with_claude(
    article_text: str,
    category_ko: str,
    extra_note: str,
    selected_model: str,
    research_brief: str | None,
) -> dict:
    client = anthropic.Anthropic(timeout=300.0, max_retries=1)
    # adaptive thinking은 Fable 5 / Opus 4.6+ / Sonnet 4.6+ 계열에서만 지원.
    # Haiku 4.5 같은 모델은 이 파라미터를 붙이면 400을 반환한다.
    supports_adaptive_thinking = selected_model in {
        "claude-fable-5",
        "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6",
        "claude-sonnet-5", "claude-sonnet-4-6",
    }

    user_msg = f"""카테고리: {category_ko}

아래는 참고할 원문 기사입니다. 이 기사를 소재로 문체 가이드에 따라
블로그 글을 작성해 주세요. 번역이 아니라 재구성입니다.
{f"추가 요청사항: {extra_note}" if extra_note else ""}

<article>
{article_text}
</article>{_research_block(research_brief)}"""

    stream_kwargs = dict(
        model=selected_model,
        max_tokens=32000,
        system=[
            {
                "type": "text",
                "text": _system_prompt(),
                # 하루 종일 반복 호출하므로 시스템 프롬프트를 캐싱한다
                "cache_control": {"type": "ephemeral"},
            }
        ],
        output_config={
            "format": {"type": "json_schema", "schema": POST_SCHEMA},
        },
        messages=[{"role": "user", "content": user_msg}],
    )
    if supports_adaptive_thinking:
        stream_kwargs["thinking"] = {"type": "adaptive"}

    with client.messages.stream(**stream_kwargs) as stream:
        response = stream.get_final_message()

    if response.stop_reason == "refusal":
        raise RuntimeError("모델이 이 요청을 거부했습니다. 기사 내용을 확인해 주세요.")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("출력이 잘렸습니다. 기사를 나눠서 다시 시도해 주세요.")

    text = next(b.text for b in response.content if b.type == "text")
    post = json.loads(text)
    post["category"] = category_ko
    post["model"] = selected_model
    return post


# ─────────────────────────────────────────────────────────────
# Kimi (Moonshot AI) 호출 — OpenAI 호환 chat/completions
# ─────────────────────────────────────────────────────────────

def _generate_with_kimi(
    article_text: str,
    category_ko: str,
    extra_note: str,
    model: str,
    research_brief: str | None = None,
) -> dict:
    if not config.KIMI_API_KEY:
        raise RuntimeError(
            "KIMI_API_KEY가 설정돼 있지 않습니다. .env 파일에 다음 줄을 추가하세요:\n"
            "  KIMI_API_KEY=sk-...\n"
            "(Moonshot Platform에서 발급: https://platform.moonshot.ai/console/api-keys)"
        )
    return _generate_openai_compat(
        article_text, category_ko, extra_note, model, research_brief,
        label="Kimi", base_url=config.KIMI_BASE_URL, api_key=config.KIMI_API_KEY,
        key_name="KIMI_API_KEY",
        extra_payload={"response_format": {"type": "json_object"},
                       # kimi-k2.6/k2.7 계열은 temperature=1만 허용. 예전 moonshot-v1 계열은 0~1 자유.
                       "temperature": 1 if model.startswith("kimi") else 0.6})


class ProviderBusy(RuntimeError):
    """외부 모델 서비스가 과부하(429·5xx)로 재시도 끝에도 응답하지 못함."""


# 서비스 과부하 재시도: 429(전체 동시 요청 초과 포함)·502/503/504는 잠시 뒤 다시 시도한다.
_BUSY_STATUS = (429, 500, 502, 503, 504)
_BUSY_WAITS = (15, 30, 45, 60, 90, 120)   # 초 — 최대 약 6분. retry-after 헤더가 있으면 그 값을 우선


def _busy_wait(r, attempt: int) -> float:
    try:
        ms = r.headers.get("retry-after-ms")
        if ms:
            return min(max(float(ms) / 1000, 1.0), 300.0)
        ra = r.headers.get("retry-after")
        if ra:
            return min(max(float(ra), 1.0), 300.0)
    except ValueError:
        pass
    return float(_BUSY_WAITS[min(attempt, len(_BUSY_WAITS) - 1)])


def _generate_with_exaone(
    article_text: str,
    category_ko: str,
    extra_note: str,
    model: str,
    research_brief: str | None = None,
) -> dict:
    if not config.EXAONE_API_KEY:
        raise RuntimeError(
            "EXAONE_API_KEY가 설정돼 있지 않습니다. .env 파일에 다음 줄을 추가하세요:\n"
            "  EXAONE_API_KEY=...\n(EXAONE API Platform에서 발급: https://docs.exaone.ai)"
        )
    # EXAONE 문서에는 response_format(json 모드)이 없다 — 스키마는 시스템 프롬프트로만 지시한다.
    # 항상 추론을 거치므로 reasoning_effort로 속도를 조절한다(글쓰기는 low 기본).
    return _generate_openai_compat(
        article_text, category_ko, extra_note, model, research_brief,
        label="EXAONE", base_url=config.EXAONE_BASE_URL, api_key=config.EXAONE_API_KEY,
        key_name="EXAONE_API_KEY",
        extra_payload={"temperature": 1.0, "top_p": 0.95,
                       "reasoning_effort": config.EXAONE_REASONING_EFFORT})


def _generate_openai_compat(
    article_text: str,
    category_ko: str,
    extra_note: str,
    model: str,
    research_brief: str | None,
    *,
    label: str,
    base_url: str,
    api_key: str,
    key_name: str,
    extra_payload: dict,
) -> dict:
    """OpenAI 호환 chat/completions(스트리밍)로 글을 생성한다 — Kimi·EXAONE 공용."""
    # json_object 모드(또는 프롬프트)만으로 JSON을 받는다 — 스키마는 시스템 프롬프트로 지시
    style = _style_text()
    schema_text = json.dumps(POST_SCHEMA, ensure_ascii=False, indent=2)
    intro = ("당신은 한국의 개인 블로거를 대신해 글을 쓰는 대필 작가입니다.\n"
             "온라인 커뮤니티 사연의 일부를 인용하고, 그 사연을 사회적 관점에서 해설하는 칼럼을 씁니다. "
             "AI가 쓴 티가 전혀 나지 않아야 합니다.\n"
             if _pann_mode() else
             "당신은 한국의 개인 블로거를 대신해 글을 쓰는 대필 작가입니다.\n"
             "사연 요약을 뼈대로 받아, 인물과 장면을 새로 지은 재미있는 각색 이야기로 씁니다. "
             "AI가 쓴 티가 전혀 나지 않아야 합니다.\n"
             if _story_mode() else
             "당신은 한국의 개인 블로거를 대신해 글을 쓰는 대필 작가입니다.\n"
             "영문 기사를 소재로 받아, 완전히 소화한 뒤 한국 독자를 위한 자연스러운\n"
             "블로그 글로 재구성합니다. AI가 쓴 티가 전혀 나지 않아야 합니다.\n")
    system_prompt = f"""{intro}
{style}

**출력 형식(엄격)**: 다음 JSON Schema를 정확히 지키는 단일 JSON 객체로만 답하세요.
설명이나 코드펜스 없이 순수 JSON만 반환하세요.

{schema_text}

이미지 검색어(query_en)는 스톡 사진에 실제로 존재할 법한 보편적인 장면·사물로
정하세요 (예: "morning coffee window" O, "korean economy graph 2026" X).""" + config.time_note()

    user_msg = f"""카테고리: {category_ko}

아래 원문 기사를 소재로 블로그 글을 재구성해 주세요. 번역이 아닙니다.
{f"추가 요청사항: {extra_note}" if extra_note else ""}

<article>
{article_text}
</article>{_research_block(research_brief)}"""

    url = f"{base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ],
        # 조사 브리프가 붙으면 출력이 길어져 8192로는 JSON이 중간에 잘린다.
        "max_tokens": 32000,
        # 긴 출력은 비스트리밍 시 읽기 타임아웃이 난다 — 스트리밍으로 받는다.
        "stream": True,
        **extra_payload,
    }
    r = None
    for attempt in range(len(_BUSY_WAITS) + 1):
        try:
            r = requests.post(
                url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=(30, 120),  # (연결, 청크 간 최대 간격)
                stream=True,
            )
        except (requests.ConnectionError, requests.Timeout) as e:
            if attempt >= len(_BUSY_WAITS):
                raise ProviderBusy(f"{label} 연결 실패(재시도 소진): {str(e)[:120]}")
            time.sleep(_busy_wait(type("R", (), {"headers": {}})(), attempt))
            continue
        if r.status_code in _BUSY_STATUS:
            if attempt >= len(_BUSY_WAITS):
                raise ProviderBusy(f"{label} 서비스 과부하 {r.status_code} (재시도 {attempt}회 소진): "
                                   f"{r.text[:150]}")
            wait = _busy_wait(r, attempt)
            print(f"  · {label} 서비스 과부하({r.status_code}) — {wait:.0f}초 뒤 재시도 ({attempt + 1}/{len(_BUSY_WAITS)})")
            r.close()
            time.sleep(wait)
            continue
        break
    if r.status_code == 401:
        raise RuntimeError(f"{label} 인증 실패 (401). {key_name}가 유효한지 확인하세요.")
    if not r.ok:
        raise RuntimeError(f"{label} API 오류 {r.status_code}: {r.text[:400]}")

    # SSE 스트림 누적
    # Moonshot 응답에 charset이 없으면 requests가 ISO-8859-1로 디코딩해 한글이 깨진다.
    r.encoding = "utf-8"
    chunks = []
    finish = None
    for raw in r.iter_lines(decode_unicode=True):
        if not raw or not raw.startswith("data:"):
            continue
        payload_line = raw[5:].strip()
        if payload_line == "[DONE]":
            break
        try:
            evt = json.loads(payload_line)
        except json.JSONDecodeError:
            continue
        ch = (evt.get("choices") or [{}])[0]
        delta = ch.get("delta") or {}
        piece = delta.get("content")
        if piece:
            chunks.append(piece)
        if ch.get("finish_reason"):
            finish = ch["finish_reason"]
    text = "".join(chunks)
    if not text.strip():
        raise RuntimeError(f"{label} 스트림에서 본문을 받지 못했습니다.")
    text = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text)   # 모델이 코드펜스로 감싸 보낸 경우
    try:
        post = json.loads(text)
    except json.JSONDecodeError as e:
        if finish == "length":
            raise RuntimeError(
                f"{label} 응답이 출력 한도에 걸려 잘렸습니다 (finish_reason=length). "
                "기사가 너무 길거나 조사 브리프가 큰 경우입니다. "
                "기사를 줄이거나 조사를 끄고 다시 시도하세요."
            )
        raise RuntimeError(f"{label} 응답을 JSON으로 해석 실패: {e}\n원문 앞부분: {text[:400]}")

    # 필수 필드 방어적 검증 (스키마 강제가 안 되므로)
    for k in ("title", "slug", "tags", "sections", "images"):
        if k not in post:
            raise RuntimeError(f"{label} 응답에 필수 필드 '{k}'가 없습니다.")
    post.setdefault("source_line", "")
    post["category"] = category_ko
    post["model"] = model
    return post
