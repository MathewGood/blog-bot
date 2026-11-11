"""검색 노출·체류시간을 위한 마무리 단계 (제목 공식 · 30초 요약 · 비교표 · 질문 · 모바일 문단).

본문이 완성된 뒤(사실 검증·교정 이후) 한 번 호출한다. 본문 사실을 새로 만들지 않고
이미 쓰인 내용을 다시 정리해 붙이므로, 요약·표의 숫자는 본문에 있는 것만 허용한다.
LLM 호출이 실패해도 글은 그대로 두고 문단 나누기만 적용한다(파이프라인을 멈추지 않는다).
"""

from __future__ import annotations

import json
import re

from . import config

TITLE_SEP = " : "
TITLE_MAX = 52        # 제목 전체 최대 글자 수 (앵커 + 구분자 + 후킹)
ANCHOR_RANGE = (4, 22)
HOOK_RANGE = (8, 34)

# 모바일(16px)에서 한 줄 약 22자 → 3~4줄 ≈ 90자. 문장 하나가 그보다 길면 그대로 둔다.
PARA_MAX = 110
PARA_TARGET = 90

_NUM = re.compile(r"\d[\d,\.]*")

# 첫 문단(검색 결과 미리보기) 기준
INTRO_RANGE = (45, 125)
INTRO_ANCHOR_START = 24    # 앵커가 첫 문단 앞 몇 글자(공백 제외, 공백 포함 약 30자) 안에서 시작해야 하는가
_FIRST_PERSON = re.compile(r"(?<![가-힣])(저는|제가|저희|우리는|우리가|내가|나는)")


def _squash(s: str) -> str:
    """띄어쓰기·기호를 없앤 비교용 문자열."""
    return re.sub(r"[\s\W_]+", "", s or "")


def first_paragraph(post: dict) -> str:
    secs = post.get("sections") or []
    if not secs:
        return ""
    paras = [p.strip() for p in (secs[0].get("body_md") or "").split("\n\n") if p.strip()]
    return paras[0] if paras else ""


def intro_has_anchor(para: str, anchor: str) -> bool:
    """첫 문단 앞쪽에 앵커(검색 키워드)가 그대로 들어 있는가."""
    a = _squash(anchor)
    i = _squash(para).find(a) if a else -1
    return 0 <= i <= INTRO_ANCHOR_START


# ─────────────────────────────────────────────────────────────
# 1) 모바일 3행 분절 룰
# ─────────────────────────────────────────────────────────────

def _in_quote(s: str) -> bool:
    return s.count("“") > s.count("”") or s.count('"') % 2 == 1


def _plain_para(p: str) -> bool:
    return not re.match(r"\s*([-*+>|#]|\d+[.)]\s)", p)


def split_paragraphs(post: dict, max_len: int = PARA_MAX, target: int = PARA_TARGET) -> int:
    """긴 문단을 문장 경계에서 나눈다(큰따옴표 대사 중간은 자르지 않는다). 나뉜 문단 수를 반환."""
    added = 0
    for sec in post.get("sections", []):
        paras = [p for p in (sec.get("body_md") or "").split("\n\n") if p.strip()]
        out: list = []
        for p in paras:
            p = p.strip()
            if len(p) <= max_len or not _plain_para(p):
                out.append(p)
                continue
            sents = [s for s in re.split(r"(?<=[.!?…”\"])\s+", p) if s.strip()]
            cur = ""
            pieces: list = []
            for s in sents:
                if cur and not _in_quote(cur) and (len(cur) >= target or len(cur) + 1 + len(s) > max_len):
                    pieces.append(cur)
                    cur = ""
                cur += (" " if cur else "") + s
            if cur.strip():
                pieces.append(cur)
            added += max(0, len(pieces) - 1)
            out.extend(x.strip() for x in pieces)
        sec["body_md"] = "\n\n".join(out)
    return added


# ─────────────────────────────────────────────────────────────
# 2) LLM 한 번으로 제목·요약·표·질문 생성
# ─────────────────────────────────────────────────────────────

_PROMPT = """한국 네이버 블로그(독자: {audience}) 글이 완성됐다. 검색 노출과 체류시간을 위해 아래 4가지를 만든다.
본문에 없는 사실·숫자·고유명사는 절대 만들지 않는다. 본문에 이미 있는 내용을 다시 정리할 뿐이다.

[현재 제목] {title}
[현재 첫 문단] {intro_now}
[분야] {category}
[소재 키워드] {hint}
[태그] {tags}

[본문]
{body}

만들 것:
1. anchor — 검색 앵커 명사구. 독자가 네이버에 실제로 입력할 만한 핵심 검색어({a0}~{a1}자).
   본문이 다루는 대상·제도·증상·현상을 가리키는 명사구로 쓴다(예: "증여세 면제 한도", "식후 걷기 혈당", "바이낸스 해킹 사태").
   기사 속 영문 표기·약어보다 한국 독자가 실제로 검색하는 말을 고른다. [소재 키워드]가 있으면 그 키워드를 그대로 쓰거나 가장 가까운 검색어로 한다.
   조사·서술어로 끝내지 않는다. 따옴표·이모지 금지.
2. hook — 클릭을 부르는 후킹 문구({h0}~{h1}자, anchor와 합쳐 38자 안팎이 이상적). 본문의 실제 사실(조건·숫자·대비)에서 뽑는다.
   검색자가 함께 찾는 연관 낱말(방법·기준·조건·비용·이유·차이 중 본문에 맞는 것) 하나를 자연스럽게 넣는다. anchor의 낱말을 되풀이하지 않는다.
   금지: 충격·경악·난리 같은 낚시어, 물음표 남발, "내돈내산"·"직접 써 보니" 같은 필자 경험 암시, 효과·완치 보장,
   의약·건강 효능 단정. 불안을 자극하지 않는다.
3. summary — '30초 요약' 3줄. 글의 핵심 결론 3가지를 각 25~60자, ~다 체로. 독자가 얻는 정보·행동 중심. 이 3줄만 읽어도 요지를 알 수 있어야 한다.
4. table — 본문 내용을 정리한 표 1개. kind는 "compare"(비교 분석표) 또는 "checklist"(체크리스트).
   headers 2~3개, rows 3~6행, 각 칸 40자 이내. checklist면 첫 열에 점검 항목, 둘째 열에 확인할 내용을 쓴다.
   표에 쓰는 숫자는 반드시 본문에 있는 것만.
5. question — 글 끝에서 독자의 의견·경험을 묻는 질문 한 문장(60자 이내, 물음표로 끝). 본문 주제와 직결되게.
6. intro — 검색 결과 미리보기로 노출될 새 첫 문단 2문장({i0}~{i1}자, ~다 체, 관찰자 시점).
   - 첫 문장 맨 앞쪽(앞 30자 안)에 anchor를 **한 글자도 바꾸지 말고 그대로** 넣고, "anchor은/는/이/가 ~다" 꼴로 이 글이 답하는 핵심(대상·조건·결론)을 쓴다.
   - 둘째 문장에는 독자가 이 글에서 얻을 정보를 구체적으로 쓴다. 태그의 연관 낱말을 한두 개 자연스럽게 섞되 anchor는 이 문단에서 두 번 넘게 쓰지 않는다.
   - 이 문단 바로 뒤에 [현재 첫 문단]이 이어진다. 그 문단과 같은 말을 되풀이하지 말고, 이어 읽어도 어색하지 않게 쓴다.
   - 숫자·고유명사는 본문에 있는 것만. 질문·낚시어·"알아보겠습니다" 같은 정형 서두·1인칭 금지.

JSON만 출력:
{{"anchor":"","hook":"","intro":"","summary":["","",""],"table":{{"kind":"compare","title":"표 제목(20자 이내)","headers":["",""],"rows":[["",""]]}},"question":""}}"""


def _ask(prompt: str) -> str:
    """Haiku(없으면 Kimi)로 JSON 응답을 받는다. 실패하면 빈 문자열."""
    import anthropic

    from . import llm_guard

    text = ""
    if llm_guard.anthropic_available():
        try:
            client = anthropic.Anthropic(timeout=90.0, max_retries=1)
            resp = client.messages.create(model=config.CLAUDE_MODEL, max_tokens=2500,
                                          messages=[{"role": "user", "content": prompt}])
            text = "".join(b.text for b in resp.content if b.type == "text")
        except Exception as e:
            if llm_guard.is_credit_error(e):
                llm_guard.mark_credit_out("Anthropic 크레딧 부족")
    if not text and config.KIMI_API_KEY:
        from . import research

        text = research._kimi_chat([{"role": "user", "content": prompt}], config.FALLBACK_MODEL,
                                   max_tokens=2500, json_mode=True, timeout=90)
    return text or ""


def _body_text(post: dict, limit: int = 9000) -> str:
    parts = []
    for sec in post.get("sections", []):
        if (sec.get("heading") or "").strip():
            parts.append(f"## {sec['heading'].strip()}")
        parts.append((sec.get("body_md") or "").strip())
    return "\n\n".join(parts)[:limit]


_NUMKEY = re.compile(r"(\d+(?:\.\d+)?)\s*([천만억조%배]?)")
# 한글로 쓴 수량 표현(천만·두 배·수만 …)은 숫자 검사를 피해 가므로 본문에 같은 표현이 있을 때만 허용한다
_KRNUM = re.compile(r"[천백]만|수[천만십백]|[두세네] ?배|[일이삼사오육칠팔구십]{1,2}만")


def _nums(s: str) -> set:
    """(숫자, 규모 단위) 쌍 집합. '1,216만'→('1216','만'). 쉼표는 무시한다."""
    return {(n, u) for n, u in _NUMKEY.findall((s or "").replace(",", ""))}


def _unsupported(text: str, body_nums: set, body: str = "") -> list:
    """text에서 본문에 없는 숫자 목록. 단위가 붙은 숫자('1천만')는 단위까지 같아야 한다."""
    plain = {n for n, _ in body_nums}
    bad = [n + u for n, u in sorted(_nums(text)) if not ((n, u) in body_nums or (not u and n in plain))]
    bad += [w for w in _KRNUM.findall(text or "") if w not in body]
    return bad


def _supported(text: str, body_nums: set, body: str = "") -> bool:
    return not _unsupported(text, body_nums, body)


def _norm_tag(t: str) -> str:
    return re.sub(r"\s+", "", str(t)).lower()


def _clean(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().strip("\"'“”‘’")


def build_title(anchor: str, hook: str) -> str:
    anchor, hook = _clean(anchor), _clean(hook)
    if not (ANCHOR_RANGE[0] <= len(anchor) <= ANCHOR_RANGE[1]) or not (HOOK_RANGE[0] <= len(hook) <= HOOK_RANGE[1]):
        return ""
    if re.search(r"[:：]", anchor + hook) or re.search(r"[\U0001F300-\U0001FAFF]", anchor + hook):
        return ""
    title = f"{anchor}{TITLE_SEP}{hook}"
    return title if len(title) <= TITLE_MAX else ""


def _valid_intro(raw, anchor: str, body_nums: set, body: str) -> str:
    """검색용 첫 문단 검증. 통과하면 정리된 문단, 아니면 빈 문자열."""
    intro = _clean(raw)
    if not (intro and anchor and INTRO_RANGE[0] <= len(intro) <= INTRO_RANGE[1]):
        return ""
    ok = (intro_has_anchor(intro, anchor)
          and _squash(intro).count(_squash(anchor)) <= 2          # 키워드 도배 금지
          and intro.endswith((".", "다", "다.")) and "?" not in intro
          and not _FIRST_PERSON.search(intro) and "**" not in intro
          and not re.search(r"[\U0001F300-\U0001FAFF]", intro)
          and _supported(intro, body_nums, body))
    return intro if ok else ""


_INTRO_PROMPT = """한국 네이버 블로그(독자: {audience}) 글의 '검색용 첫 문단'만 새로 쓴다. 본문에 없는 사실·숫자는 만들지 않는다.

[핵심 검색어] {anchor}
[현재 첫 문단] {intro_now}
[태그] {tags}

[본문]
{body}

쓸 것: 새 첫 문단 2문장({i0}~{i1}자, ~다 체, 관찰자 시점).
- 첫 문장 맨 앞쪽(앞 30자 안)에 [핵심 검색어]를 한 글자도 바꾸지 말고 그대로 넣고, "검색어은/는/이/가 ~다" 꼴로 글이 답하는 핵심(대상·조건·결론)을 쓴다.
- 둘째 문장은 독자가 얻을 정보를 구체적으로. 태그의 연관 낱말을 한두 개 섞되 검색어는 두 번 넘게 쓰지 않는다.
- 이 문단 바로 뒤에 [현재 첫 문단]이 이어진다. 같은 말을 되풀이하지 않는다.
- 질문·낚시어·"알아보겠습니다" 같은 정형 서두·1인칭 금지.

JSON만 출력: {{"intro":""}}"""


def _intro_only(post: dict, anchor: str, intro_now: str, body: str, aud: str) -> str:
    """제목 공식은 못 맞췄어도 기존 제목의 앵커로 첫 문단만 만든다. 실패하면 빈 문자열."""
    body_nums = _nums(body)
    prompt = _INTRO_PROMPT.format(audience=aud, anchor=anchor, intro_now=intro_now[:400] or "(없음)",
                                  tags=", ".join(post.get("tags", [])), body=body,
                                  i0=INTRO_RANGE[0], i1=INTRO_RANGE[1])
    for _ in range(2):
        try:
            m = re.search(r"\{.*\}", _ask(prompt), re.S)
            intro = _valid_intro((json.loads(m.group(0)) if m else {}).get("intro"), anchor, body_nums, body)
        except Exception:
            intro = ""
        if intro:
            return intro
    return ""


def _validate(data: dict, body: str) -> dict:
    """LLM 결과를 검증해 쓸 수 있는 부분만 남긴다. 키가 없으면 해당 요소는 건너뛴다."""
    body_nums = _nums(body)
    out: dict = {}
    title = build_title(data.get("anchor"), data.get("hook"))
    bad = _unsupported(title, body_nums | _nums(data.get("title_old", "")), body + data.get("title_old", "")) if title else []
    if title and not bad:
        out["title"] = title
        out["anchor"] = _clean(data.get("anchor"))
    elif bad:
        out["_bad_title_nums"] = bad
    summ = [_clean(x) for x in (data.get("summary") or []) if _clean(x)]
    summ = [x for x in summ if 15 <= len(x) <= 80 and _supported(x, body_nums, body)]
    if len(summ) >= 3:
        out["summary"] = summ[:3]
    t = data.get("table") or {}
    headers = [_clean(h) for h in (t.get("headers") or []) if _clean(h)]
    rows = []
    for r in t.get("rows") or []:
        cells = [_clean(c) for c in r][:len(headers)]
        if len(cells) == len(headers) and all(cells) and all(len(c) <= 60 for c in cells) \
                and _supported(" ".join(cells), body_nums, body):
            rows.append(cells)
    if 2 <= len(headers) <= 3 and len(rows) >= 3:
        out["table"] = {"kind": "checklist" if t.get("kind") == "checklist" else "compare",
                        "title": _clean(t.get("title"))[:30], "headers": headers, "rows": rows[:6]}
    q = _clean(data.get("question"))
    if 8 <= len(q) <= 80 and q.endswith("?"):
        out["question"] = q
    intro = _valid_intro(data.get("intro"), _clean(data.get("anchor")), body_nums, body)
    if intro:
        out["intro"] = intro
    return out


def finalize(post: dict, hint: str = "", log=None) -> dict:
    """제목·요약·표·질문을 붙인 post를 돌려준다(원본은 수정하지 않고 사본 반환).

    문단 나누기는 항상 적용하고, 나머지는 LLM 결과가 검증을 통과한 요소만 넣는다."""
    emit = log or (lambda m: None)
    post = json.loads(json.dumps(post, ensure_ascii=False))
    n_split = split_paragraphs(post)
    if n_split:
        emit(f"  · 모바일 가독성: 긴 문단 {n_split}곳을 3~4줄 길이로 나눔")

    body = _body_text(post)
    aud = config.AUDIENCES.get(post.get("audience") or "women", config.AUDIENCES["women"])["label"]
    intro_now = first_paragraph(post)
    prompt = _PROMPT.format(audience=aud, title=post.get("title", ""), category=post.get("category", ""),
                            intro_now=intro_now[:400] or "(없음)", i0=INTRO_RANGE[0], i1=INTRO_RANGE[1],
                            hint=hint or "(없음 — 본문에서 판단)", tags=", ".join(post.get("tags", [])),
                            body=body, a0=ANCHOR_RANGE[0], a1=ANCHOR_RANGE[1], h0=HOOK_RANGE[0], h1=HOOK_RANGE[1])
    got: dict = {}
    feedback = ""
    for attempt in range(3):
        try:
            text = _ask(prompt + feedback)
            m = re.search(r"\{.*\}", text, re.S)
            data = json.loads(m.group(0)) if m else {}
        except Exception:
            data = {}
        data["title_old"] = post.get("title", "")
        got = _validate(data, body)
        # 첫 문단은 현재 문단에 앵커가 이미 있으면 새로 만들 필요가 없다
        need_intro = not (got.get("anchor") and intro_has_anchor(intro_now, got["anchor"]))
        missing = [k for k in ("title", "summary", "table", "question") if k not in got]
        if need_intro and "intro" not in got and "title" in got:
            missing.append("intro")
        if not missing:
            break
        feedback = ("\n\n[이전 시도에서 기준을 못 맞춘 요소: " + ", ".join(missing) + "] "
                    "숫자는 본문에 적힌 표기 그대로만 쓰고(어림·반올림·환산 금지), 본문에 없으면 숫자를 빼고 쓴다. "
                    "글자 수 범위도 지킨다.")
        if "intro" in missing:
            feedback += (" intro는 2문장 " + f"{INTRO_RANGE[0]}~{INTRO_RANGE[1]}자, 첫 문장 앞쪽에 anchor를 그대로 넣고, "
                         "~다 체로 끝내며, 물음표·1인칭·본문에 없는 숫자를 쓰지 않는다.")
        if "title" in missing:
            bad = got.get("_bad_title_nums")
            feedback += (f" 제목에 쓴 숫자·수량 표현 {bad}는 본문에 없다." if bad else " 제목이 글자 수·형식 기준을 못 맞췄다.") + \
                " 후킹 문구에는 숫자·수량 표현을 쓰지 말고, 본문의 대비·이유·조건으로 만든다."
    got.pop("_bad_title_nums", None)
    fb_anchor = ""
    if "title" not in got and post.get("sections"):
        old_title = post.get("title") or ""
        head = old_title.split(TITLE_SEP)[0].strip() if TITLE_SEP in old_title else ""
        if ANCHOR_RANGE[0] <= len(head) <= ANCHOR_RANGE[1] and not intro_has_anchor(intro_now, head):
            intro_fb = _intro_only(post, head, intro_now, body, aud)
            if intro_fb:
                got["intro"], fb_anchor = intro_fb, head
    if not got:
        emit("  ⚠ 검색용 구성(제목·요약·표) 생성 실패 — 문단 나누기만 적용")
        return post

    if got.get("title"):
        post["title_original"] = post.get("title", "")
        post["title"] = got["title"]
        anchor_n = _norm_tag(got["anchor"])
        tags = list(post.get("tags", []))
        # 앵커를 담은 기존 태그를 맨 앞으로(없고 짧은 한 낱말이면 새로 추가)
        hit = next((t for t in tags if _norm_tag(t) and _norm_tag(t) in anchor_n), None)
        if hit:
            tags = [hit] + [t for t in tags if t != hit]
        elif " " not in got["anchor"] and len(anchor_n) <= 10:
            tags = [anchor_n] + tags
        post["tags"] = tags
        emit(f"  ✓ 제목(앵커 + 후킹): {got['title']}")
    else:
        emit("  ⚠ 제목 공식 조건 불충족 — 원래 제목 유지")
    anchor_now = got.get("anchor") or fb_anchor
    if anchor_now:
        if intro_has_anchor(intro_now, anchor_now):
            emit(f"  ✓ 첫 문단에 검색어 '{anchor_now}' 이미 포함")
        elif got.get("intro") and post.get("sections"):
            sec0 = post["sections"][0]
            sec0["body_md"] = got["intro"] + "\n\n" + (sec0.get("body_md") or "").lstrip()
            post["intro_added"] = got["intro"]
            emit(f"  ✓ 첫 문단에 검색어 '{anchor_now}' 삽입 (장면 문단은 그 뒤로)")
        else:
            emit(f"  ⚠ 첫 문단 검색어 삽입 실패 — 기존 첫 문단 유지")
    if got.get("summary"):
        post["summary"] = got["summary"]
        emit("  ✓ 30초 요약 카드 3줄")
    else:
        emit("  ⚠ 요약 카드 생략 (본문에 없는 수치가 있거나 3줄이 안 나옴)")
    if got.get("table"):
        n = len(post.get("sections", []))
        post["table"] = dict(got["table"], after_section=max(1, min(n - 2, n // 2)) if n > 2 else 0)
        emit(f"  ✓ {'체크리스트' if got['table']['kind'] == 'checklist' else '비교 분석표'} {len(got['table']['rows'])}행 삽입")
    else:
        emit("  ⚠ 표 생략 (본문에 없는 수치가 있거나 행이 부족)")
    if got.get("question"):
        post["closing"] = {"question": got["question"]}
        emit("  ✓ 마무리 질문 1개")
    else:
        emit("  ⚠ 마무리 질문 생략")
    return post
