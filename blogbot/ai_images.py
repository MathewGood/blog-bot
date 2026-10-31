"""OpenAI로 블로그 사진을 생성한다 (사진 공급원 'openai').

흐름(사진 1장당):
1) gpt-5.6-luna가 해당 문단을 읽고 영어 이미지 프롬프트 + 한국어 캡션을 쓴다 (≈ $0.0002)
2) gpt-image-2.5-flare가 1536x1024 사진을 그린다 (≈ $0.011, 약 10초)
여러 장은 병렬로 만든다. 실패한 장은 호출측이 무료 스톡 사진으로 대신한다.

모든 결과물에는 'AI 생성 이미지' 표시를 붙인다 — 실제 사진으로 오인되지 않게 하고,
인공지능기본법 제31조(생성형 AI 결과물 표시)의 취지에도 맞춘다.
"""

from __future__ import annotations

import base64
import concurrent.futures as cf
import json
import time
from pathlib import Path

import requests

from . import config

API = "https://api.openai.com/v1"
AI_CREDIT = "AI 생성 이미지 (OpenAI)"
CF_CREDIT = "AI 생성 이미지 (Cloudflare FLUX)"


def credit_for(engine: str) -> str:
    return CF_CREDIT if engine == "cloudflare" else AI_CREDIT

# 1M 토큰당 단가 — 비용 로그용 (2026-10 공식 가격표 기준)
_PRICE = {
    "gpt-5.6-luna": (0.20, 1.20),
    "gpt-image-2.5-flare": (5.00, 30.00),
    "gpt-image-2.5-sunburst": (5.00, 30.00),
    "gpt-image-2": (5.00, 30.00),
    "gpt-image-1.5": (5.00, 32.00),
    "gpt-image-1-mini": (2.00, 8.00),
}

_COMMON_RULES = """Hard rules for the image prompt:
- NO text, letters, numbers, signs, logos, watermarks, charts or UI anywhere in the image.
- NO identifiable real people, celebrities or politicians. Show people from behind, partially,
  at a distance, or as anonymous figures.
- EVERY person shown must look Korean or Northeast Asian (Korean, with Chinese/Japanese only when the
  topic is about those countries). NEVER show Western, South Asian, African or Middle Eastern people.
  If a scene would naturally need such people, show no people at all (objects, places, hands, scenery).
- Do not depict real news events as if they were documentary photos (no fake evidence).
- One clear subject, uncluttered composition, landscape framing."""

_STYLE = {
    "history": ("Historical reconstruction in the style of a museum-quality painting or detailed "
                "documentary illustration. Period-accurate clothing, architecture and landscape; "
                "nothing modern."),
    "economy": ("Warm lifestyle-magazine editorial photography, soft natural light, "
                "realistic, calm tones. Korean settings when relevant."),
    "life": ("Warm lifestyle-magazine editorial photography, soft natural light, "
             "bright realistic tones. Korean settings when relevant."),
    "tech": ("Clean modern technology editorial photography (Wired/GQ style): data centers, "
             "chips, devices, labs, offices; cool natural light, realistic. No logos or screens with text."),
    "society": ("Documentary-style editorial photography of everyday public life: streets, "
                "public institutions, crowds from a distance, cultural venues; realistic, natural light."),
}


def _style_key(category: str) -> str:
    """'역사' 같은 한글 카테고리명도 영문 키(history)로 맞춘다."""
    if category in _STYLE:
        return category
    for key, ko in config.CATEGORIES.items():
        if ko == category:
            return key
    return "life"


def available() -> bool:
    return bool(config.OPENAI_API_KEY)


def _headers() -> dict:
    return {"Authorization": f"Bearer {config.OPENAI_API_KEY}",
            "Content-Type": "application/json"}


def _cost(model: str, usage: dict) -> float:
    pin, pout = _PRICE.get(model, (0, 0))
    return (usage.get("input_tokens", 0) * pin + usage.get("output_tokens", 0) * pout) / 1e6


def write_spec(section_text: str, title: str, category: str, planned_caption: str = "",
               audience: str = "women") -> tuple:
    """문단 → ({prompt, caption}, 비용$). Luna 호출이 실패하면 예외."""
    style = _STYLE[_style_key(category)]
    instructions = (
        "You design one image for a section of a Korean long-form magazine blog post.\n"
        f"Visual style: {style}\n{_COMMON_RULES}\n"
        f"Target readers: {config.AUDIENCES.get(audience, config.AUDIENCES['women'])['image']} — "
        "when people appear, make them relatable to these readers.\n"
        "The caption is Korean, ~다 style, a brief plain description of what the image shows, "
        "at most 25 characters. Do not mention photographers, sources, credits, AI or the word 'photo'."
    )
    user = (
        "Return JSON {\"prompt\": \"English image prompt, 40-90 words\", "
        "\"caption\": \"Korean caption\"}.\n"
        f"Post title: {title}\n"
        f"Planned caption idea: {planned_caption}\n"
        f"Paragraph:\n{section_text[:1500]}"
    )
    r = requests.post(f"{API}/responses", headers=_headers(), timeout=60, json={
        "model": config.OPENAI_PROMPT_MODEL,
        "reasoning": {"effort": "none"},
        "instructions": instructions,
        "input": user,
        "text": {"format": {"type": "json_object"}},
    })
    if r.status_code != 200:
        raise RuntimeError(f"{config.OPENAI_PROMPT_MODEL} HTTP {r.status_code}: {r.text[:150]}")
    d = r.json()
    text = next(c["text"] for o in d.get("output", []) if o.get("type") == "message"
                for c in o.get("content", []) if c.get("type") == "output_text")
    spec = json.loads(text)
    if not spec.get("prompt"):
        raise RuntimeError("프롬프트가 비어 있음")
    return spec, _cost(config.OPENAI_PROMPT_MODEL, d.get("usage", {}))


def draw(prompt: str, dest: Path) -> float:
    """프롬프트 → JPEG 저장. 비용$ 반환. 실패하면 예외."""
    r = requests.post(f"{API}/images/generations", headers=_headers(), timeout=180, json={
        "model": config.OPENAI_IMAGE_MODEL,
        "prompt": prompt,
        "size": config.OPENAI_IMAGE_SIZE,
        "quality": config.OPENAI_IMAGE_QUALITY,
        "output_format": "jpeg",
        "n": 1,
    })
    if r.status_code != 200:
        msg = r.text[:200]
        try:
            msg = r.json()["error"]["message"][:200]
        except Exception:
            pass
        raise RuntimeError(f"{config.OPENAI_IMAGE_MODEL} HTTP {r.status_code}: {msg}")
    d = r.json()
    dest.write_bytes(base64.b64decode(d["data"][0]["b64_json"]))
    return _cost(config.OPENAI_IMAGE_MODEL, d.get("usage", {}))


def draw_cloudflare(prompt: str, dest: Path) -> float:
    """Cloudflare Workers AI(FLUX)로 그린다. 무료 할당(하루 10,000 뉴런) 안에서는 0원.

    FLUX-1-schnell은 정사각형을 돌려주므로 블로그 가로형(3:2)으로 가운데를 잘라낸다.
    """
    import subprocess

    url = (f"https://api.cloudflare.com/client/v4/accounts/{config.CLOUDFLARE_ACCOUNT_ID}"
           f"/ai/run/{config.CF_IMAGE_MODEL}")
    r = requests.post(url, timeout=120, headers={
        "Authorization": f"Bearer {config.CLOUDFLARE_API_TOKEN}"},
        json={"prompt": prompt[:2048], "steps": 8})
    if r.status_code != 200:
        msg = r.text[:200]
        try:
            msg = "; ".join(e.get("message", "") for e in r.json().get("errors", []))[:200] or msg
        except Exception:
            pass
        raise RuntimeError(f"Cloudflare HTTP {r.status_code}: {msg}")
    img = (r.json().get("result") or {}).get("image")
    if not img:
        raise RuntimeError("Cloudflare 응답에 이미지가 없음")
    dest.write_bytes(base64.b64decode(img))
    try:  # 가운데 3:2 크롭 (macOS 기본 도구 sips)
        out = subprocess.run(["sips", "-g", "pixelWidth", str(dest)], capture_output=True, text=True)
        w = int(out.stdout.strip().split()[-1])
        subprocess.run(["sips", "-c", str(int(w * 2 / 3)), str(w), str(dest)],
                       capture_output=True, timeout=30)
    except Exception:
        pass
    return 0.0


def make_one(section_text: str, title: str, category: str, plan: dict, dest: Path,
             audience: str = "women", engine: str = "openai") -> dict:
    """사진 1장 생성 → {caption, cost, seconds, engine}. 실패하면 예외."""
    t = time.time()
    try:
        if not config.OPENAI_API_KEY:
            raise RuntimeError("OpenAI 키 없음 — 계획된 검색어로 그림")
        spec, c1 = write_spec(section_text, title, category, plan.get("caption", ""), audience)
    except Exception:
        # 프롬프트 작성이 실패해도 계획된 검색어·캡션으로 그린다
        spec, c1 = {"prompt": f"{plan.get('query_en', title)}. {_STYLE[_style_key(category)]} "
                              "No text, no logos, no identifiable people. Any people shown must look Korean/Northeast Asian.",
                    "caption": plan.get("caption", "")}, 0.0
    c2 = draw_cloudflare(spec["prompt"], dest) if engine == "cloudflare" else draw(spec["prompt"], dest)
    return {"caption": (spec.get("caption") or plan.get("caption", "")).strip(),
            "cost": c1 + c2, "seconds": time.time() - t, "engine": engine}


def make_many(jobs: list, max_workers: int = 3, engine: str = "openai") -> list:
    """[(section_text, title, category, plan, dest), ...] → 같은 순서의 결과 리스트.

    각 결과는 dict(성공) 또는 Exception(실패).
    """
    out: list = [None] * len(jobs)
    with cf.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = {pool.submit(make_one, *job, engine=engine): i for i, job in enumerate(jobs)}
        for fut in cf.as_completed(futs):
            i = futs[fut]
            try:
                out[i] = fut.result()
            except Exception as e:  # 개별 실패는 호출측에서 스톡으로 대체
                out[i] = e
    return out
