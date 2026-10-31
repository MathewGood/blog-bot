"""생성된 글의 한글 오탈자만 교정하는 패스 (Haiku, 저렴·빠름).

kimi-k3 등 일부 모델이 긴 프롬프트에서 한글 음절을 깨뜨리는 문제
("따륾면"→"따르면", "묵너진"→"무너진")를 잡는다.
문체·어휘·문장 구조·내용·수치는 절대 바꾸지 않도록 지시하며,
실패 시 원문을 그대로 반환해 파이프라인을 멈추지 않는다.
"""

from __future__ import annotations

import json

import anthropic

from . import config

_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "heading": {"type": "string"},
                    "body_md": {"type": "string"},
                },
                "required": ["heading", "body_md"],
                "additionalProperties": False,
            },
        },
        "captions": {"type": "array", "items": {"type": "string"}},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "sections", "captions", "tags"],
    "additionalProperties": False,
}

_SYSTEM = """너는 한국어 교정자다. 입력된 블로그 글에서 다음만 고친다:
- 깨진 한글 음절 (예: "따륾면"→"따르면", "묵너진"→"무너진", "가은데"→"가운데")
- **실제로 존재하지 않는 단어** (예: "울동화"→"운동화", "낮아가려면"→"낮아지려면",
  "물가가 끈기면"→"물가가 끈질기면", "경제 낙부"→"경제 논리")
- 명백한 조사·어미 오류, 활용형 오류

작업 방법: 모든 문장을 단어 단위로 훑어라. 한국어 사전에 없는 단어나
문맥상 말이 되지 않는 표현이 보이면, 문맥이 요구하는 올바른 단어로 고친다.
확신이 서지 않으면 그대로 둔다 (추측으로 의미를 바꾸지 않는다).

절대 바꾸지 않는 것:
- 문체(~다 체), 어휘 선택, 문장 구조, 문단 구성
- 내용, 수치, 고유명사, 출처 표기
- 마크다운 서식(**강조**, 줄바꿈)

고칠 것이 없는 필드는 원문 그대로 반환한다. 입력과 동일한 JSON 구조로만 답한다."""

_PROOFREAD_MODEL = "claude-haiku-4-5"


def proofread_post(post: dict) -> tuple:
    """post의 한글 오탈자를 교정해 (교정된 post, 수정된 필드 수) 반환.

    실패하거나 구조가 어긋나면 원본을 그대로 반환한다.
    Anthropic 크레딧이 부족하면 llm_guard.CreditExhausted를 던진다.
    """
    from . import llm_guard

    if not llm_guard.anthropic_available():
        raise llm_guard.CreditExhausted("Anthropic 크레딧 부족(차단 중) — 교정 생략")

    payload = {
        "title": post.get("title", ""),
        "sections": [
            {"heading": s.get("heading", ""), "body_md": s.get("body_md", "")}
            for s in post.get("sections", [])
        ],
        "captions": [im.get("caption", "") for im in post.get("images", [])],
        "tags": list(post.get("tags", [])),
    }

    client = anthropic.Anthropic(timeout=120.0, max_retries=1)
    try:
        resp = client.messages.create(
            model=_PROOFREAD_MODEL,
            max_tokens=32000,
            system=_SYSTEM,
            output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
            messages=[
                {
                    "role": "user",
                    "content": "다음 글을 교정하라:\n"
                    + json.dumps(payload, ensure_ascii=False),
                }
            ],
        )
    except anthropic.BadRequestError as e:
        llm_guard.check_and_raise(e)
    text = next(b.text for b in resp.content if b.type == "text")
    fixed = json.loads(text)

    # 구조 검증 — 개수가 어긋나면 교정 결과를 버리고 원본 유지
    if (
        len(fixed.get("sections", [])) != len(payload["sections"])
        or len(fixed.get("captions", [])) != len(payload["captions"])
        or len(fixed.get("tags", [])) != len(payload["tags"])
    ):
        return post, 0

    changes = 0
    out = dict(post)

    if fixed["title"] != payload["title"]:
        changes += 1
    out["title"] = fixed["title"]

    new_sections = []
    for orig, new in zip(post.get("sections", []), fixed["sections"]):
        sec = dict(orig)
        if new["heading"] != sec.get("heading", "") or new["body_md"] != sec.get("body_md", ""):
            changes += 1
        sec["heading"] = new["heading"]
        sec["body_md"] = new["body_md"]
        new_sections.append(sec)
    out["sections"] = new_sections

    new_images = []
    for orig, cap in zip(post.get("images", []), fixed["captions"]):
        im = dict(orig)
        if cap != im.get("caption", ""):
            changes += 1
        im["caption"] = cap
        new_images.append(im)
    out["images"] = new_images

    if fixed["tags"] != payload["tags"]:
        changes += 1
    out["tags"] = fixed["tags"]

    return out, changes
