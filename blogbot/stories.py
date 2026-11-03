"""독자 사연 — 사용자가 직접 고른 글(판·커뮤니티 등)을 붙여넣으면 익명으로 요약해 글감으로 쓴다.

네이트 판은 robots.txt로 자동 수집을 막아 두었으므로 사이트를 긁지 않는다(2026-10-29 사용자 결정: 사람이 고른 글만).
원문 전체는 저장하지 않고, 익명화한 요약·주제문(+판 인기글은 짧은 발췌문 몇 개)만 대기열(stories.json)에 둔다.
글에는 닉네임·지역·회사명 같은 식별 정보를 쓰지 않는다.
판 인기글(pann_id가 있는 사연)은 각색하지 않고, 짧게 발췌한 문장과 사회적 관점의 해설로 쓴다(2026-11-03 사용자 지정).
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid

from . import config

STORIES_FILE = config.ROOT / "stories.json"
_LOCK = threading.Lock()

MIN_CHARS = 150
MAX_CHARS = 8000


class Unsuitable(Exception):
    """블로그 소재로 쓰면 안 되는 사연."""


_PROMPT = """너는 한국 네이버 블로그 편집자다. 아래 <story>는 독자가 커뮤니티에 올린 사연이다.
이를 블로그 글감으로 쓰기 위해 **익명화한 요약**과 **일반화한 주제문**을 만든다.

규칙
- summary: 500~900자. 3인칭("한 독자", "이 사연의 주인공")으로 발단 → 갈등이 커지는 과정 → 지금의 상황(결말 또는 미해결 지점)과
  인물들의 감정을 다시 써라(이 요약은 재미있는 이야기로 각색하는 뼈대가 된다).
  원문 문장을 그대로 옮기지 말고, 닉네임·실명·지역·회사·학교·정확한 나이(연령대로)·연락처 같은 식별 정보는 모두 뺀다.
  원문에 없는 사실을 만들지 말고, 사연 속 사람을 비난하거나 단정적으로 평가하지 않는다.
- excerpts: 원문에서 **그대로 베껴 온** 인상적인 문장 구간 3~4개. 각 40~140자, 합계 450자 이내. 서로 다른 대목(발단·갈등·현재 상황)에서 고르고,
  한 구간은 원문에서 이어진 문장이어야 한다(문장을 고치거나 이어 붙이지 않는다). 닉네임·실명·지역·회사·학교·연락처·URL 같은 식별 정보가 있으면
  그 부분만 "○○"로 바꾼다. 욕설·비하 표현이 있는 구간은 고르지 않는다.
- issue: 이 사연이 드러내는 **사회적 쟁점** 한 줄(25~60자). 예: "돌봄 부담이 한 사람에게 쏠리는 구조", "세대 간 기대치가 어긋나는 명절 문화".
  사연 속 개인을 탓하지 않고 구조·관행·제도·인식의 문제로 쓴다.
- hook: 각색할 때 살릴 재미·공감 포인트 한 줄(아이러니, 엇갈림, 반전, "이런 일 있지" 하는 지점).
- query: 25~50자 주제문. 이 사연 한 건이 아니라 같은 고민을 가진 많은 사람에게 통하는 일반 주제로(검색·자료 조사 가능하게).
- category: {cats} 중 하나의 키.
- angle: 독자 {who}에게 닿는 관점 한 줄.
- unsuitable: 다음에 해당하면 그 이유를 한 줄로, 아니면 빈 문자열.
  미성년자 피해, 성범죄·폭력·학대, 자해·자살, 특정 개인·업체의 신상 폭로나 비방, 의료·법률 분쟁의 확정 판단이 핵심인 사연.
  이야기가 아닌 글(시·감성 문구·고백/넋두리만 있고 상황이 없는 글, 제품·서비스 후기나 추천 요청, 정보 공유, 광고·홍보, 논쟁 유도용 짧은 질문)도
  같은 방식으로 이유를 쓴다. 사건과 인물 사이의 갈등이 있는 글만 쓴다.
  (이 경우 summary·query는 비워도 된다)

<story>
{text}
</story>

JSON만 출력: {{"unsuitable":"","summary":"...","excerpts":["..."],"issue":"...","hook":"...","query":"...","category":"키","angle":"..."}}"""


_PII = re.compile(r"https?://|www\.|@[\w.]+|\d{2,4}[-\s]?\d{3,4}[-\s]?\d{4}")
EXCERPT_MAX = 140
EXCERPT_MIN = 20


def _squash(t: str) -> str:
    return re.sub(r"\s+", "", t or "")


def verify_excerpts(raw, text: str) -> list:
    """모델이 돌려준 발췌문 중 원문에 실제로 있는 것만 남긴다('○○'로 가린 부분은 건너뛰고 나머지 조각을 대조).
    연락처·URL이 남아 있거나 길이가 맞지 않으면 버린다. 합계 450자 이내."""
    base = _squash(text)
    out, total = [], 0
    for ex in raw if isinstance(raw, list) else []:
        ex = str(ex or "").strip().strip("\"'“”‘’")
        if not (EXCERPT_MIN <= len(ex) <= EXCERPT_MAX) or _PII.search(ex):
            continue
        pieces = [_squash(x) for x in re.split(r"[○●◯]+", ex)]
        pieces = [x for x in pieces if len(x) >= 4]
        if not pieces or not all(x in base for x in pieces):
            continue
        if total + len(ex) > 450:
            break
        out.append(ex)
        total += len(ex)
    return out


def analyze(text: str, audience: str = "women") -> dict:
    """사연 원문 → {"summary","query","category","angle"}. 쓸 수 없는 사연이면 Unsuitable."""
    from . import research

    text = (text or "").strip()
    if len(text) < MIN_CHARS:
        raise ValueError(f"사연이 너무 짧습니다 ({MIN_CHARS}자 이상).")
    if not config.KIMI_API_KEY:
        raise RuntimeError("사연 요약에 KIMI_API_KEY가 필요합니다.")
    cats = ", ".join(f"{k}={v}" for k, v in config.CATEGORIES.items())
    who = config.AUDIENCES.get(audience, config.AUDIENCES["women"])["label"]
    out = research._kimi_chat(
        [{"role": "user", "content": _PROMPT.format(cats=cats, who=who, text=text[:MAX_CHARS])}],
        config.FALLBACK_MODEL, max_tokens=2500, json_mode=True, timeout=120)
    m = re.search(r"\{.*\}", out, re.S)
    data = json.loads(m.group(0)) if m else {}
    reason = str(data.get("unsuitable") or "").strip()
    if reason:
        raise Unsuitable(reason)
    summary, query = str(data.get("summary") or "").strip(), str(data.get("query") or "").strip()
    if len(summary) < 100 or len(query) < 5:
        raise RuntimeError("사연 요약에 실패했습니다. 다시 시도해 주세요.")
    cat = data.get("category") if data.get("category") in config.CATEGORIES else "life"
    return {"summary": summary, "query": query[:120], "category": cat,
            "excerpts": verify_excerpts(data.get("excerpts"), text),
            "issue": str(data.get("issue") or "").strip()[:120],
            "angle": str(data.get("angle") or "").strip()[:120],
            "hook": str(data.get("hook") or "").strip()[:160]}


# ─────────────────────────────────────────────────────────────
# 대기열 (자동 예약이 앞에서부터 가져다 쓴다)
# ─────────────────────────────────────────────────────────────

def _load() -> list:
    try:
        return json.loads(STORIES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save(items: list):
    STORIES_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")


def add(info: dict, ref: str = "", extra: dict = None) -> dict:
    item = {"id": uuid.uuid4().hex[:8], "summary": info["summary"], "query": info["query"],
            "category": info["category"], "angle": info.get("angle", ""), "hook": info.get("hook", ""),
            "excerpts": info.get("excerpts") or [], "issue": info.get("issue", ""),
            "ref": (ref or "").strip()[:300], "added": int(time.time())}
    if extra:
        item.update(extra)
    with _LOCK:
        items = _load()
        items.append(item)
        _save(items[-100:])
    return item


def all_items() -> list:
    with _LOCK:
        return _load()


def get(story_id: str):
    return next((s for s in all_items() if s["id"] == story_id), None)


def remove(story_id: str) -> bool:
    with _LOCK:
        items = _load()
        kept = [s for s in items if s["id"] != story_id]
        if len(kept) == len(items):
            return False
        _save(kept)
        return True


def story_block(info: dict) -> str:
    """생성 모델에게 줄 소재 — 익명 요약과 재미 포인트(이야기를 새로 지을 뼈대)."""
    hook = (info.get("hook") or "").strip()
    return ("[사연 요약 — 독자 사연을 익명으로 재구성한 각색용 뼈대]\n" + info["summary"].strip() + "\n"
            + (f"\n[살릴 재미 포인트] {hook}\n" if hook else "") + "\n")


def story_note(query: str, sources: list = None) -> str:
    """각색 지시 — 사연의 뼈대만 빌려 인물·장면을 새로 지은 재미있는 이야기로 쓴다(해설·통계 없음)."""
    return (
        f"이번 글은 해설이 아니라 **각색한 이야기**다. <article>의 [사연 요약]은 뼈대일 뿐이다. 상황·갈등·감정선과 [살릴 재미 포인트]는 "
        "살리되, 이름·직업·나이대·장소·소품·숫자·사건의 구체 장면은 모두 새로 지어 바꾼다. 원문 문장·닉네임·지역·회사명은 쓰지 않고 "
        "실화라고 말하지 않는다. 장면과 대사, 서술자의 능청스러운 한마디로 재미있게 풀고, 통계·연구·기관 발표·실존 인물·브랜드는 넣지 않는다. "
        "[각색 설계]가 있으면 그 설계의 인물·소품·장면 순서를 그대로 따르고, 장면마다 대사와 행동 묘사로 550~750자를 채운다. "
        f"주제 감각은 「{query}」. source_line은 빈 문자열로 둔다."
    )


_PLAN_PROMPT = """너는 인기 생활 블로그의 이야기 작가다. 아래 <story>는 익명으로 줄인 사연의 뼈대다.
이를 바탕으로 **재미있게 각색한 새 이야기의 설계도**를 만든다. 글을 쓰는 사람은 이 설계도를 그대로 따라 쓴다.

규칙
- 갈등 구조·감정선·재미 포인트는 살리되, 디테일은 전부 새로 짓는다. 인물 이름(흔한 가명), 직업, 나이대, 가족 구성,
  소품, 장소(특정 지명 없이 "작은 도시", "동네 마트" 정도), 사건의 계기와 장면을 원문과 다르게 바꾼다.
  원문의 소품·직업이 그대로면 안 된다(소품은 비슷한 성격의 다른 물건으로).
- 실존 학교·기업·브랜드·지명·인물 이름 금지. 통계·연구 인용 금지. 폭력·성적 묘사·비하 금지.
- 독자는 {who}이다. 이 독자가 "내 얘기 같다" 하고 웃게 되는 일상 장면으로 짠다.
- sections: 정확히 {n}개 장면. 첫 장면(도입)과 마지막 장면(마무리)의 heading은 ''. 나머지 heading은 짧고 궁금하게(10자 안팎).
  각 장면은 scene(누가·어디서·무슨 일이 벌어지는지 1~2문장), line(그 장면의 인상적인 대사 한 줄), gag(웃음·아이러니 포인트 한 줄).
- twist: 중반 이후의 반전 또는 엇갈림이 풀리는 순간. **한 문장으로 설명되는 단순한 것**이어야 하고, 이야기의 핵심 소품·갈등과 이어져야 한다
  (새 소품·새 사건을 갑자기 끌어들여 꼬지 않는다). ending: 여운 있는 마지막 장면과 서술자의 한마디.

<story>
{summary}
재미 포인트: {hook}
</story>

JSON만 출력: {{"title":"궁금증을 일으키는 제목 후보","characters":"등장인물 한 줄 소개들","twist":"...","ending":"...","sections":[{{"heading":"","scene":"...","line":"...","gag":"..."}}]}}"""


def adapt_plan(info: dict, audience: str = "women", n: int = 8) -> str:
    """사연 뼈대 → 각색 설계도 텍스트([각색 설계]). 실패하면 빈 문자열(설계 없이 쓴다)."""
    from . import research

    if not config.KIMI_API_KEY:
        return ""
    who = config.AUDIENCES.get(audience, config.AUDIENCES["women"])["label"]
    try:
        out = research._kimi_chat(
            [{"role": "user", "content": _PLAN_PROMPT.format(
                who=who, n=n, summary=info["summary"], hook=info.get("hook") or "-")}],
            config.FALLBACK_MODEL, max_tokens=3500, json_mode=True, timeout=150)
        m = re.search(r"\{.*\}", out, re.S)
        d = json.loads(m.group(0)) if m else {}
        secs = [x for x in d.get("sections", []) if isinstance(x, dict) and x.get("scene")]
        if len(secs) < 5:
            return ""
    except Exception:
        return ""
    lines = ["[각색 설계 — 인물·장소·소품은 모두 이 설계의 것을 쓰고, 장면 순서대로 쓴다]",
             f"제목 후보: {d.get('title', '')}", f"등장인물: {d.get('characters', '')}"]
    for i, x in enumerate(secs):
        lines.append(f"{i + 1}. 소제목「{x.get('heading', '') or '(없음)'}」 장면: {x.get('scene', '')} / 대사: {x.get('line', '')} / 웃음 포인트: {x.get('gag', '')}")
    lines += [f"반전: {d.get('twist', '')}", f"마무리: {d.get('ending', '')}"]
    return "\n".join(lines) + "\n"


# ─────────────────────────────────────────────────────────────
# 판 인기글 — 발췌 + 사회적 관점 해설
# ─────────────────────────────────────────────────────────────

def is_commentary(info: dict) -> bool:
    """판 인기글 출신 사연은 각색하지 않고 발췌 + 사회적 관점 해설로 쓴다."""
    return bool(info and info.get("pann_id"))


def commentary_block(info: dict) -> str:
    """생성 모델에게 줄 소재 — 사연 요약, 발췌문, 사회적 쟁점."""
    ex = [e for e in info.get("excerpts") or [] if e]
    lines = ["[사연 요약 — 아래 글을 익명으로 줄인 것. 글에서는 '한 커뮤니티 글' 정도로만 소개한다]", info["summary"].strip(), ""]
    if ex:
        lines.append("[발췌문 — 원문 그대로. 한 글자도 고치지 말고, 인용 블록(> )으로 본문에 넣는다]")
        lines += [f"{i + 1}. {e}" for i, e in enumerate(ex)]
        lines.append("")
    if info.get("issue"):
        lines += [f"[사회적 쟁점] {info['issue']}", ""]
    return "\n".join(lines) + "\n"


def commentary_note(query: str, has_excerpts: bool) -> str:
    """해설 지시 — 사연을 일부 인용하고 사회적 관점의 생각을 쓴다(각색·소설 아님)."""
    return (
        "이번 글은 이야기가 아니라 **사연을 일부 인용하고 사회적 관점에서 생각을 쓰는 해설 칼럼**이다. "
        + ("<article>의 [발췌문]은 한 글자도 바꾸지 말고 본문에 인용 블록(줄 맨 앞 `> `)으로 넣는다. 발췌문 외에 사연의 내용을 직접 인용하는 척 지어 쓰지 않는다. "
           if has_excerpts else "<article>에 발췌문이 없으므로 따옴표 인용 없이 사연을 한두 문단으로 요약해 소개한다. ")
        + "사연 속 인물은 이름·지역·직장 없이 '글쓴이', '시어머니'처럼 역할로만 부르고, 그들을 비난하거나 평가하지 않는다. "
        "해설은 [사회적 쟁점]을 중심으로 구조·관행·제도·세대·인식 차이의 관점에서 쓰고, 한쪽 편을 들기보다 양쪽 입장을 모두 살핀 뒤 필자의 생각을 밝힌다. "
        "통계·연구·기관 발표·법 조항·실존 인물·브랜드는 넣지 않는다(지어내기 방지). "
        f"주제 감각은 「{query}」. source_line은 빈 문자열로 둔다."
    )


def restore_excerpts(post: dict, excerpts: list) -> tuple:
    """본문의 인용 블록(`> `)을 원본 발췌문으로 되돌린다. 교정·재작성으로 바뀐 인용은 복구하고,
    발췌문과 닮지 않은(지어낸) 인용 블록은 지운다. (복구/교체한 수, 삭제한 수, 본문에 남은 발췌 수)를 돌려준다."""
    import difflib

    excerpts = [e for e in excerpts or [] if e]
    fixed = dropped = 0
    found = set()
    for sec in post.get("sections", []):
        out = []
        for p in (sec.get("body_md") or "").split("\n\n"):
            if not p.lstrip().startswith(">"):
                out.append(p)
                continue
            q = _squash(re.sub(r"(?m)^\s*>\s?", "", p))
            best, ratio = None, 0.0
            for i, e in enumerate(excerpts):
                r = difflib.SequenceMatcher(None, q, _squash(e)).ratio()
                if r > ratio:
                    best, ratio = i, r
            if best is None or ratio < 0.6 or best in found:
                dropped += 1
                continue
            found.add(best)
            if q != _squash(excerpts[best]):
                fixed += 1
            out.append("> " + excerpts[best])
        sec["body_md"] = "\n\n".join(x for x in out if x.strip())
    return fixed, dropped, len(found)
