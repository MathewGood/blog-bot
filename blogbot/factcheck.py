"""생성된 글을 '자료(원문 기사 + 조사 자료)'와 대조해 근거 없는 사실을 고친다.

Haiku는 분량을 채우거나 문장을 매끄럽게 하면서 자료에 없는 연도·수치·장소·기간을
기억에 기대 덧붙이는 경향이 있다 (2026-10-04 실측: "거란 1000년 제국",
"금 마지막 황제가 남경에서 자살" — 실제는 약 210년, 채주).

동작: gpt-5.6-luna(100만 토큰 문맥, 저렴)에 자료와 글을 함께 주고
  · 구체적 사실(연도·수치·기간·장소·인물·인과·최상급)이 자료에 없거나 어긋나는 문장을 찾아
  · 자료에 맞게 고친 문장(또는 삭제)을 받아 그대로 치환한다.
의견·해석·문체는 건드리지 않는다. OpenAI 키가 없으면 Haiku로 같은 일을 한다.
"""

from __future__ import annotations

import json
import re

import requests

from . import config

MAX_SOURCE_CHARS = 60000
MAX_ISSUES = 30

_INSTRUCTIONS = """You are a strict fact-checker for a Korean blog. Compare the POST against the SOURCES.

Flag a sentence ONLY if it states a concrete fact — a year, date, number, duration, place,
person, title, cause-and-effect, or superlative ("최초", "가장", "N년 이상") — that is NOT
supported by the SOURCES or contradicts them. Do not flag opinions, interpretation, scene-setting
description without concrete facts, or style.

For each problem return:
- "quote": the exact sentence copied verbatim from the post (must match character-for-character)
- "problem": short Korean explanation
- "fix": the same sentence rewritten in the same Korean ~다 style so that every fact in it is
  supported by the SOURCES (drop or soften the unsupported detail). Use "" to delete the sentence
  if nothing supported would remain OR if the fixed sentence would just repeat something the post
  already says (never produce the same generic sentence for several fixes).
Also check the TITLE; if it contains an unsupported fact, include it with "quote" = the title.
Return at most 30 items, most serious first."""


def _instructions() -> str:
    # 모델이 아는 것보다 최근 연도(올해 자료)를 '근거 없음'으로 오판하지 않도록 기준 시점을 알려 준다.
    return (_INSTRUCTIONS + config.time_note() +
            " Years up to the current year that appear in the SOURCES are valid; never flag a year or"
            " figure just because it is newer than what you remember — judge only against the SOURCES.")


def _sources_text(article_text: str, research_brief: str | None) -> str:
    src = (article_text or "")
    if research_brief:
        src += "\n\n[조사 자료]\n" + research_brief
    return src[:MAX_SOURCE_CHARS]


def _post_text(post: dict) -> str:
    parts = [f"TITLE: {post.get('title', '')}"]
    for i, s in enumerate(post.get("sections", [])):
        parts.append(f"[section {i}] {s.get('heading', '')}\n{s.get('body_md', '')}")
    return "\n\n".join(parts)


def _ask_luna(sources: str, post_text: str) -> list:
    r = requests.post("https://api.openai.com/v1/responses", timeout=180, headers={
        "Authorization": f"Bearer {config.OPENAI_API_KEY}", "Content-Type": "application/json"},
        json={"model": config.OPENAI_PROMPT_MODEL, "reasoning": {"effort": "low"},
              "instructions": _instructions(),
              "input": ("Return JSON {\"issues\": [{\"quote\", \"problem\", \"fix\"}]}.\n\n"
                        f"<SOURCES>\n{sources}\n</SOURCES>\n\n<POST>\n{post_text}\n</POST>"),
              "text": {"format": {"type": "json_object"}}})
    if r.status_code != 200:
        raise RuntimeError(f"{config.OPENAI_PROMPT_MODEL} HTTP {r.status_code}: {r.text[:150]}")
    d = r.json()
    text = next(c["text"] for o in d.get("output", []) if o.get("type") == "message"
                for c in o.get("content", []) if c.get("type") == "output_text")
    return json.loads(text).get("issues", [])


def _ask_haiku(sources: str, post_text: str) -> list:
    import anthropic

    client = anthropic.Anthropic(timeout=180.0, max_retries=1)
    resp = client.messages.create(
        model=config.CLAUDE_MODEL, max_tokens=8000, system=_instructions(),
        messages=[{"role": "user", "content":
                   "JSON만 출력: {\"issues\": [{\"quote\", \"problem\", \"fix\"}]}\n\n"
                   f"<SOURCES>\n{sources}\n</SOURCES>\n\n<POST>\n{post_text}\n</POST>"}])
    text = "".join(b.text for b in resp.content if b.type == "text")
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0)).get("issues", []) if m else []


def check(post: dict, article_text: str, research_brief: str | None = None) -> tuple:
    """(고친 post, 적용된 수정 목록). 자료가 없거나 실패하면 원본 그대로."""
    sources = _sources_text(article_text, research_brief)
    if len(sources) < 300:
        return post, []
    post_text = _post_text(post)
    issues = (_ask_luna if config.OPENAI_API_KEY else _ask_haiku)(sources, post_text)

    out = dict(post)
    out["sections"] = [dict(s) for s in post.get("sections", [])]
    applied = []
    for it in issues[:MAX_ISSUES]:
        quote = (it.get("quote") or "").strip()
        fix = (it.get("fix") or "").strip()
        if len(quote) < 8 or quote == fix:
            continue
        if quote == out.get("title", "").strip():
            if fix:
                out["title"] = fix
                applied.append({"quote": quote, "fix": fix, "problem": it.get("problem", "")})
            continue
        for s in out["sections"]:
            body = s.get("body_md", "")
            if quote in body:
                new = body.replace(quote, fix, 1)
                # 삭제로 생긴 겹공백·빈 줄 정리
                new = re.sub(r"[ \t]{2,}", " ", new)
                new = re.sub(r"\n{3,}", "\n\n", new).strip()
                s["body_md"] = new
                applied.append({"quote": quote, "fix": fix, "problem": it.get("problem", "")})
                break
    if applied:
        out["factcheck"] = applied
    return out, applied
