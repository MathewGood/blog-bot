"""독자 사연 — 사용자가 직접 고른 글(판·커뮤니티 등)을 붙여넣으면 익명으로 요약해 글감으로 쓴다.

네이트 판은 robots.txt로 자동 수집을 막아 두었으므로 사이트를 긁지 않는다(2026-10-29 사용자 결정: 사람이 고른 글만).
원문은 저장하지 않고, 익명화한 요약·주제문만 대기열(stories.json)에 둔다. 글에는 원문 문장·닉네임·지역·회사명을 쓰지 않는다.
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

JSON만 출력: {{"unsuitable":"","summary":"...","hook":"...","query":"...","category":"키","angle":"..."}}"""


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
