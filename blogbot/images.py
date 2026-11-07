"""Pexels/Unsplash 무료 스톡 이미지 검색 → 비전 모델 선별 → 다운로드.

개선된 흐름:
1) 검색어로 후보 이미지 여러 장(최대 8)을 가져온다
2) 비전 모델(Haiku)이 실제 이미지들을 보고, 해당 문단 내용과 가장 어울리는 것을 고른다
3) 선택한 이미지를 보고 문맥에 맞는 설명 캡션을 새로 쓴다
4) 어떤 후보도 맞지 않으면 대안 검색어(alt_query_en)로 재시도
비전 선별이 실패하면 기존 방식(첫 결과 + 계획 캡션)으로 폴백한다.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import requests

from . import config


# ─────────────────────────────────────────────────────────────
# 후보 검색
# ─────────────────────────────────────────────────────────────

def search_candidates(query: str, limit: int = 8) -> list:
    """검색어로 후보 이미지 목록을 가져온다 — 연결된 무료 사진 API를 모두 활용.

    순서: Pexels → Pixabay. 앞에서 부족한 만큼 다음 곳에서 채운다.
    Unsplash는 쓰지 않는다 — API 약관이 '이미지 URL 핫링크만 허용, 내려받아 재호스팅 금지'인데
    blogbot은 사진을 내려받아 네이버에 올리므로 약관 위반이 된다 (2026-10-05 확인).
    각 후보: {url(원본급), thumb(저해상 미리보기), alt, credit, credit_url}
    """
    out: list = []
    for fn in (_pexels, _pixabay):
        if len(out) >= limit:
            break
        try:
            out.extend(fn(query, limit - len(out)))
        except Exception:
            continue
    return [c for c in out if c.get("thumb")][:limit]


def _pexels(query: str, n: int) -> list:
    if not config.PEXELS_API_KEY:
        return []
    r = requests.get("https://api.pexels.com/v1/search",
                     headers={"Authorization": config.PEXELS_API_KEY},
                     params={"query": query, "per_page": n, "orientation": "landscape"}, timeout=20)
    r.raise_for_status()
    return [{"url": p["src"]["large"], "thumb": p["src"].get("tiny") or p["src"].get("small"),
             "alt": p.get("alt") or "", "credit": f"사진: {p['photographer']} (Pexels)",
             "credit_url": p["url"]} for p in r.json().get("photos", [])]


def _unsplash(query: str, n: int) -> list:
    if not config.UNSPLASH_ACCESS_KEY:
        return []
    r = requests.get("https://api.unsplash.com/search/photos", timeout=20,
                     params={"query": query, "per_page": n, "orientation": "landscape",
                             "client_id": config.UNSPLASH_ACCESS_KEY})
    r.raise_for_status()
    return [{"url": p["urls"]["regular"], "thumb": p["urls"].get("thumb") or p["urls"].get("small"),
             "alt": p.get("alt_description") or "", "credit": f"사진: {p['user']['name']} (Unsplash)",
             "credit_url": p["links"]["html"]} for p in r.json().get("results", [])]


_PIXABAY_CACHE = config.ROOT / ".cache" / "pixabay"


def _pixabay(query: str, n: int) -> list:
    """Pixabay — 약관상 검색 결과를 24시간 캐시해야 하고, 이미지는 내려받아 써야 한다(핫링크 금지)."""
    if not config.PIXABAY_API_KEY:
        return []
    import hashlib
    import time as _t

    _PIXABAY_CACHE.mkdir(parents=True, exist_ok=True)
    cache = _PIXABAY_CACHE / (hashlib.sha1(query.encode()).hexdigest() + ".json")
    if cache.exists() and _t.time() - cache.stat().st_mtime < 24 * 3600:
        hits = json.loads(cache.read_text(encoding="utf-8"))
    else:
        r = requests.get("https://pixabay.com/api/", timeout=20, params={
            "key": config.PIXABAY_API_KEY, "q": query[:100], "image_type": "photo",
            "orientation": "horizontal", "min_width": 1200, "safesearch": "true", "per_page": 20})
        r.raise_for_status()
        hits = r.json().get("hits", [])
        cache.write_text(json.dumps(hits), encoding="utf-8")
    return [{"url": h["largeImageURL"], "thumb": h.get("webformatURL") or h.get("previewURL"),
             "alt": h.get("tags", ""), "credit": f"사진: {h.get('user', 'Pixabay')} (Pixabay)",
             "credit_url": h.get("pageURL", "")} for h in hits[:n]]


# ─────────────────────────────────────────────────────────────
# 비전 선별: 문단과 가장 어울리는 이미지 선택 + 문맥 캡션 작성
# ─────────────────────────────────────────────────────────────

def pick_best_image(section_text: str, candidates: list) -> tuple:
    """(선택 인덱스 또는 None, 문맥 캡션 또는 None) 반환.

    Haiku 비전으로 실제 이미지들을 보고 판단한다. 실패 시 (None, None).
    """
    from . import llm_guard

    if not candidates or not llm_guard.anthropic_available():
        return None, None
    try:
        import anthropic

        client = anthropic.Anthropic(timeout=40.0, max_retries=1)
        content = [
            {
                "type": "text",
                "text": (
                    "아래는 블로그 글의 한 문단과, 그 문단 뒤에 넣을 후보 이미지들이다.\n"
                    "1) 문단 내용과 의미가 가장 잘 어울리는 이미지 하나를 골라라.\n"
                    "   어떤 후보도 문단과 맞지 않으면 -1.\n"
                    "   한국 블로그이므로 사람이 보이는 이미지는 한국·중국·일본 등 동북아시아 사람만 허용한다.\n"
                    "   서양인 등 다른 인종이 보이는 후보는 제외하라(사람이 없는 이미지는 허용).\n"
                    "2) 선택한 이미지에 보이는 것을 간략히 설명하는 한국어 캡션 한 문장을 써라\n"
                    "   (~다 체, 25자 내외, 설명만. 출처·촬영자·사진 사이트 이름은 쓰지 마라).\n\n"
                    f"<문단>\n{section_text[:900]}\n</문단>\n"
                ),
            }
        ]
        for j, c in enumerate(candidates[:8]):
            content.append({"type": "text", "text": f"[후보 {j}]"})
            content.append(
                {"type": "image", "source": {"type": "url", "url": c["thumb"]}}
            )
        content.append(
            {
                "type": "text",
                "text": 'JSON만 출력: {"best": <인덱스 또는 -1>, "caption": "<캡션>"}',
            }
        )
        resp = client.messages.create(
            model=config.IMAGE_PICK_MODEL,
            max_tokens=300,
            messages=[{"role": "user", "content": content}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return None, None
        data = json.loads(m.group(0))
        best = data.get("best", -1)
        caption = (data.get("caption") or "").strip()
        if not isinstance(best, int) or best < 0 or best >= len(candidates):
            return None, None
        return best, (caption or None)
    except Exception as e:
        if llm_guard.is_credit_error(e):
            llm_guard.mark_credit_out("Anthropic 크레딧 부족")
        return None, None


# ─────────────────────────────────────────────────────────────
# 다운로드 & 파이프라인
# ─────────────────────────────────────────────────────────────

def download(url: str, dest: Path) -> Path:
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    dest.write_bytes(r.content)
    return dest


def _section_context(post: dict, idx: int) -> str:
    """after_section 인덱스의 섹션 텍스트(제목+본문)를 문맥으로 뽑는다."""
    sections = post.get("sections", [])
    if 0 <= idx < len(sections):
        sec = sections[idx]
        heading = (sec.get("heading") or "").strip()
        body = (sec.get("body_md") or "").strip()
        return (heading + "\n" + body).strip()
    return post.get("title", "")


def limit_plans(plans: list, n: int = None) -> list:
    """사진 계획이 상한(config.MAX_IMAGES)보다 많으면 글 전체에 고르게 퍼지도록 n장만 고른다."""
    n = n or config.MAX_IMAGES
    if len(plans) <= n:
        return plans
    plans = sorted(plans, key=lambda x: x.get("after_section", 0))
    idx = sorted({round(i * (len(plans) - 1) / (n - 1)) for i in range(n)}) if n > 1 else [0]
    return [plans[i] for i in idx]


def fetch_images_for_post(post: dict, draft_dir: Path, log=None, provider: str = None) -> list:
    """post['images'] 계획대로 사진을 준비한다. 결과 메타 리스트 반환.

    provider: 'openai'(AI 생성, 기본) | 'stock'(무료 사진 검색·선별). None이면 설정값.
    AI 생성이 실패한 장은 그 장만 스톡 사진으로 대신한다.
    log: 진행 메시지를 받을 콜러블(선택) — GUI 실행 현황에 과정을 노출.
    """
    img_dir = draft_dir / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    plans = limit_plans(list(post.get("images", [])))
    provider = config.resolve_image_provider(provider)

    def _emit(msg: str):
        print(msg)
        if log:
            try:
                log(msg)
            except Exception:
                pass

    results: dict = {}

    # 역사 글: 위키미디어 공용의 실제 유물·회화·지도를 먼저 찾는다 (무료, 사실성 ↑)
    if config.HISTORY_USE_WIKIMEDIA and _is_history(post) and plans:
        from . import archive_images

        if archive_images.available():
            _emit(f"  · 역사 글 — 위키미디어 공용에서 실제 유물·회화·지도 {len(plans)}장 찾는 중...")
            slots = [_section_context(post, pl.get("after_section", 0)) for pl in plans]
            dests = [img_dir / f"img{i + 1}.jpg" for i in range(len(plans))]
            for i, meta in enumerate(archive_images.find_for_slots(
                    post.get("title", ""), slots, dests, log=_emit)):
                if meta:
                    meta.update({"file": str(dests[i]),
                                 "after_section": plans[i].get("after_section", 0)})
                    results[i] = meta
            _emit(f"  · 위키미디어 {len(results)}/{len(plans)}장 확보"
                  + (" — 나머지는 다른 방식으로" if len(results) < len(plans) else ""))

    engines = []
    if provider == "openai":
        engines = ["openai"] + (["cloudflare"] if config.cloudflare_ready() else [])
    elif provider == "cloudflare":
        engines = ["cloudflare"]
    for engine in engines:
        if len(results) >= len(plans):
            break
        from . import llm_guard
        if llm_guard.provider_blocked(engine):
            _emit(f"  ⚠ {llm_guard.provider_status(engine)['reason']} — {engine} 건너뜀 (다음 방식으로)")
            continue
        results.update(_ai_fill(post, plans, img_dir, results, engine, _emit))

    for i, plan in enumerate(plans):
        if i in results:
            continue
        meta = _stock_one(post, plan, img_dir / f"img{i + 1}.jpg", _emit)
        if meta:
            results[i] = meta
    return [results[i] for i in sorted(results)]


def _ai_fill(post, plans, img_dir, results, engine, _emit) -> dict:
    """아직 비어 있는 칸을 AI로 생성. {index: meta} 반환."""
    from . import ai_images

    got: dict = {}
    category = post.get("category", "life")
    todo = [i for i in range(len(plans)) if i not in results]
    jobs = [(_section_context(post, plans[i].get("after_section", 0)), post.get("title", ""),
             category, plans[i], img_dir / f"img{i + 1}.jpg", post.get("audience", "women")) for i in todo]
    model = config.CF_IMAGE_MODEL.split("/")[-1] if engine == "cloudflare" else config.OPENAI_IMAGE_MODEL
    _emit(f"  · AI 사진 {len(jobs)}장 생성 중 ({model}, 동시 3장)")
    total, n_ai = 0.0, 0
    for i, res in zip(todo, ai_images.make_many(jobs, engine=engine)):
        pl = plans[i]
        if isinstance(res, Exception):
            _emit(f"  ⚠ AI 사진 {i + 1} 실패({str(res)[:90]}) — 다음 방식으로 대체")
            continue
        total += res["cost"]
        n_ai += 1
        got[i] = {
            "file": str(img_dir / f"img{i + 1}.jpg"),
            "after_section": pl.get("after_section", 0),
            "caption": res["caption"],
            "credit": ai_images.credit_for(engine),
            "credit_url": "",
            "remote_url": "",
            "ai_generated": True,
        }
        _emit(f"  ✓ AI 사진 저장: img{i + 1}.jpg ({res['seconds']:.0f}초)")
    if n_ai:
        _emit(f"  · AI 사진 {n_ai}장 " + ("무료 할당 사용 (Cloudflare)" if engine == "cloudflare"
                                        else f"비용 약 ${total:.3f}"))
    return got


def _is_history(post: dict) -> bool:
    return post.get("category") in ("history", config.CATEGORIES.get("history", "역사"))


def _korean_bias(q: str) -> str:
    """스톡 검색어에 'Korean'을 덧붙여 한국·동아시아 인물 사진이 먼저 나오게 한다."""
    return q if re.search(r"korea|asian|seoul|japan|chin", q, re.I) else f"{q} Korean"


def _stock_one(post: dict, plan: dict, path: Path, _emit) -> dict:
    """무료 스톡 사진 1장: 검색 → 비전 선별 → 다운로드. 실패하면 None."""
    from . import llm_guard

    context = _section_context(post, plan.get("after_section", 0))
    queries = [_korean_bias(q) for q in (plan.get("query_en"), plan.get("alt_query_en")) if q]

    chosen = None
    vision_caption = None
    fallback = None  # 비전 선별 실패 시 쓸 첫 후보
    vision_on = llm_guard.anthropic_available()

    for q in queries:
        candidates = search_candidates(q)
        if not candidates:
            continue
        if fallback is None:
            fallback = candidates[0]
        if not vision_on:
            # 크레딧 부족 등으로 비전 선별 불가 — 판정 없이 첫 결과 사용 (대안 검색 낭비 방지)
            chosen = candidates[0]
            _emit(f"  · 비전 선별 생략 → 첫 검색 결과 사용 ({q})")
            break
        best, caption = pick_best_image(context, candidates)
        if best is not None:
            chosen = candidates[best]
            vision_caption = caption
            _emit(f"  ✓ 이미지 선별: 후보 {len(candidates)}장 중 {best + 1}번째 채택 ({q})")
            break
        _emit(f"  · '{q}' 후보 {len(candidates)}장 모두 문단과 부적합 → 대안 검색")

    if chosen is None:
        if fallback is None:
            _emit(f"  ⚠ 이미지를 찾지 못함: {' / '.join(queries) or '(검색어 없음)'}")
            return None
        chosen = fallback  # 폴백: 첫 후보 + 계획 캡션
        _emit("  ⚠ 딱 맞는 사진이 없어 첫 검색 결과 사용 — 발행 전 확인 권장")

    try:
        download(chosen["url"], path)
    except Exception as e:
        _emit(f"  ⚠ 다운로드 실패: {e}")
        return None
    _emit(f"  ✓ 이미지 저장: {path.name}")
    return {
        "file": str(path),
        "after_section": plan.get("after_section", 0),
        "caption": vision_caption or plan.get("caption", ""),
        "credit": chosen["credit"],
        "credit_url": chosen["credit_url"],
        "remote_url": chosen["url"],
    }
