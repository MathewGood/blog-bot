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
- summary: 400~700자. 3인칭("한 독자", "이 사연의 주인공")으로 상황·갈등·고민의 핵심만 다시 써라.
  원문 문장을 그대로 옮기지 말고, 닉네임·실명·지역·회사·학교·정확한 나이(연령대로)·연락처 같은 식별 정보는 모두 뺀다.
  원문에 없는 사실을 만들지 말고, 사연 속 사람을 비난하거나 단정적으로 평가하지 않는다.
- query: 25~50자 주제문. 이 사연 한 건이 아니라 같은 고민을 가진 많은 사람에게 통하는 일반 주제로(검색·자료 조사 가능하게).
- category: {cats} 중 하나의 키.
- angle: 독자 {who}에게 닿는 관점 한 줄.
- unsuitable: 다음에 해당하면 그 이유를 한 줄로, 아니면 빈 문자열.
  미성년자 피해, 성범죄·폭력·학대, 자해·자살, 특정 개인·업체의 신상 폭로나 비방, 의료·법률 분쟁의 확정 판단이 핵심인 사연.
  사연이 아닌 글(시·감성 문구·고백/넋두리만 있고 상황이 없는 글, 광고·홍보, 논쟁 유도용 짧은 질문)도 같은 방식으로 이유를 쓴다.
  (이 경우 summary·query는 비워도 된다)

<story>
{text}
</story>

JSON만 출력: {{"unsuitable":"","summary":"...","query":"...","category":"키","angle":"..."}}"""


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
        config.FALLBACK_MODEL, max_tokens=2000, json_mode=True, timeout=120)
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
            "angle": str(data.get("angle") or "").strip()[:120]}


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
            "category": info["category"], "angle": info.get("angle", ""),
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
    """생성 모델에게 줄 자료집 앞머리 — 익명 요약."""
    return "[사연 요약 — 독자 사연을 익명으로 재구성]\n" + info["summary"].strip() + "\n\n"


def story_note(query: str, sources: list) -> str:
    sites = list(dict.fromkeys(s["site"] for s in sources if s.get("site")))[:4]
    src = ", ".join(["한 독자의 사연(익명 재구성)"] + sites)
    return (
        f"이번 글은 한 독자의 사연에서 출발한다. <article>의 [사연 요약]과 [배경 조사 자료]만 근거로 쓴다. "
        "구성: 첫 섹션은 사연을 3~5문장으로 소개하되 요약을 다시 풀어 쓰고(원문 문장·닉네임·지역·회사명·정확한 나이 금지), "
        "이어지는 섹션은 '같은 고민을 하는 사람이 많다'는 흐름에서 조사 자료의 통계·연구·제도로 상황을 해설한다. "
        "사연 속 사람을 비난하거나 평가하지 말고, 단정적인 처방이나 법률·의료 판단은 하지 않는다. "
        "마지막은 독자가 스스로 점검해 볼 질문이나 생활 속 선택지로 마무리한다. "
        f"주제는 「{query}」. source_line은 반드시 '참고 자료: {src}' 형식으로 쓴다."
    )
