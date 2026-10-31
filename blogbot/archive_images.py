"""역사 글용 실제 유물·회화·지도 사진 — 위키미디어 공용(Wikimedia Commons).

역사 글에서는 AI가 재현한 장면보다 박물관 유물·고지도·옛 회화 같은 '진짜 자료'가
신뢰도가 높다. 사진 1장(슬롯)마다:
1) gpt-5.6-luna가 문단을 읽고 아카이브용 영어 검색어를 만든다 (슬롯 전체를 한 번에)
2) 위키미디어 공용을 검색해 사용 가능한 라이선스만 남긴다 (CC0·퍼블릭도메인·CC BY·CC BY-SA)
3) Luna가 후보 썸네일과 파일 설명을 함께 보고 가장 맞는 것을 고르고 캡션을 쓴다
4) 맞는 자료가 없는 슬롯은 None → 호출측이 기존 방식(AI 생성/스톡)으로 채운다

키가 필요 없고 무료다. 위키미디어 정책상 설명적인 User-Agent를 보낸다.
"""

from __future__ import annotations

import concurrent.futures as cf
import html
import json
import os
import re
import threading
from pathlib import Path

import requests

from . import config

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
# Wikimedia 등은 User-Agent에 연락처를 쓰라고 요구한다. 공개 저장소에 개인 주소를 두지 않으려고 .env의 CONTACT_EMAIL에서 읽는다.
_CONTACT = os.getenv("CONTACT_EMAIL", "")
UA = {"User-Agent": "blogbot/1.0 (personal Naver blog image search" + (f"; contact {_CONTACT}" if _CONTACT else "") + ")"}
OPENAI = "https://api.openai.com/v1/responses"

# 사진 출처를 글에 표기하지 않으므로(2026-10-26 결정) 표기 의무가 없는 CC0·퍼블릭도메인만 쓴다.
# (CC BY / CC BY-SA는 저작자 표시가 필수라 제외. NC·ND·공정이용도 제외.)
_OK_LICENSE = re.compile(r"^(cc0|public domain|pd\b|pdm|no restrictions)", re.I)
_BAD_LICENSE = re.compile(r"nc|nd|fair use|non-free", re.I)
MIN_WIDTH = 700   # 블로그 본문 폭(약 770px)에 맞는 최소 크기


def available() -> bool:
    return bool(config.OPENAI_API_KEY)


def _strip(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


def _luna(instructions: str, content) -> dict:
    r = requests.post(OPENAI, timeout=90, headers={
        "Authorization": f"Bearer {config.OPENAI_API_KEY}", "Content-Type": "application/json"},
        json={"model": config.OPENAI_PROMPT_MODEL, "reasoning": {"effort": "none"},
              "instructions": instructions, "input": content,
              "text": {"format": {"type": "json_object"}}})
    if r.status_code != 200:
        raise RuntimeError(f"{config.OPENAI_PROMPT_MODEL} HTTP {r.status_code}: {r.text[:150]}")
    d = r.json()
    text = next(c["text"] for o in d.get("output", []) if o.get("type") == "message"
                for c in o.get("content", []) if c.get("type") == "output_text")
    return json.loads(text)


def plan_queries(title: str, slots: list) -> list:
    """슬롯별 문단 → 슬롯별 위키미디어 검색어 목록 (Luna 1회 호출)."""
    numbered = "\n\n".join(f"[{i}] {s[:700]}" for i, s in enumerate(slots))
    out = _luna(
        "You find authentic historical images on Wikimedia Commons for a Korean history blog. "
        "For each numbered paragraph, propose 3 SHORT English search queries (2-3 words each) "
        "that would match real museum artifacts, historical paintings, maps, manuscripts, tomb "
        "murals, ruins or historic sites related to that paragraph. Wikimedia search is literal, "
        "so broad beats specific: 'Goryeo celadon', 'Liao dynasty mural', 'Khitan horseman', "
        "'Goryeo armor'. Never use dashes or event names that are unlikely to be file titles. "
        "Also give 6 broad queries for the whole post (dynasty/people/era names + artifact, "
        "painting, map, ruins).",
        "Return JSON {\"queries\": [[q1,q2,q3], ...], \"broad\": [q1..q6]} with one list per "
        "paragraph, same order.\n"
        f"Post title: {title}\n\n{numbered}",
    )
    clean = lambda q: re.sub(r"[\u2010-\u2015\-–—]", " ", q).strip()
    qs = out.get("queries") or []
    broad = [clean(q) for q in (out.get("broad") or []) if isinstance(q, str) and q.strip()][:6]
    per = [[clean(q) for q in (qs[i] if i < len(qs) else []) if isinstance(q, str) and q.strip()][:3]
           for i in range(len(slots))]
    return per, broad


def search(query: str, limit: int = 10) -> list:
    """위키미디어 공용 검색 → 사용 가능한 라이선스·크기의 후보."""
    try:
        r = requests.get(COMMONS_API, headers=UA, timeout=30, params={
            "action": "query", "format": "json", "generator": "search",
            "gsrsearch": f"{query} filetype:bitmap", "gsrnamespace": 6, "gsrlimit": limit,
            "prop": "imageinfo", "iiprop": "url|size|extmetadata", "iiurlwidth": 1280})
        pages = (r.json().get("query") or {}).get("pages", {})
    except Exception:
        return []
    out = []
    for p in pages.values():
        ii = (p.get("imageinfo") or [{}])[0]
        md = ii.get("extmetadata", {})
        lic = _strip(md.get("LicenseShortName", {}).get("value", ""))
        if not lic or _BAD_LICENSE.search(lic) or not _OK_LICENSE.search(lic):
            continue
        w, h = ii.get("width", 0), ii.get("height", 0)
        if w < MIN_WIDTH or not h or not (0.5 <= w / h <= 2.8):
            continue
        thumb = ii.get("thumburl") or ii.get("url")
        if not thumb:
            continue
        out.append({
            "title": p.get("title", "")[5:].rsplit(".", 1)[0],
            "desc": _strip(md.get("ImageDescription", {}).get("value", ""))[:300],
            "artist": _strip(md.get("Artist", {}).get("value", ""))[:60],
            "license": lic,
            "url": thumb,
            "page": ii.get("descriptionurl", ""),
        })
    return out


def _pick(section_text: str, cands: list) -> tuple:
    """Luna가 썸네일과 설명을 보고 고른다 → (index 또는 None, 캡션)."""
    content = [{"type": "input_text", "text": (
        "Return JSON {\"best\": index or -1, \"caption\": \"Korean caption\"}.\n"
        "Pick the candidate image that best illustrates this paragraph of a Korean history blog. "
        "It must actually relate to the same people, period, place or objects; if none truly "
        "fits, answer -1. The image's CENTURY must match the paragraph's period — beware of "
        "homonym dynasties (e.g. Jurchen Jin 金 1115-1234 vs Jin 晉 265-420 / 'Wei, Jin, "
        "Nanbeichao'; Later Liang; Northern vs Southern Song). If the file description shows a "
        "different century, reject it. If the image shows people, they must be Korean/Chinese/Japanese (Northeast Asian) — "
        "reject images of other peoples (answer -1). "
        "Write the caption in Korean only — no English words — (~다 style, "
        "max 25 chars, a plain description only: no credit, no artist, no license) describing what "
        "the image really is, based on its file title/description (do not guess a different "
        "dynasty or object).\n\nParagraph:\n" + section_text[:900])}]
    for i, c in enumerate(cands):
        content.append({"type": "input_text",
                        "text": f"[{i}] {c['title']} — {c['desc'][:200]}"})
        content.append({"type": "input_image", "image_url": c["url"], "detail": "low"})
    out = _luna("You are a careful picture editor for a history magazine.",
                [{"role": "user", "content": content}])
    best = out.get("best", -1)
    if not isinstance(best, int) or not (0 <= best < len(cands)):
        return None, ""
    return best, (out.get("caption") or "").strip()


def _short_artist(artist: str) -> str:
    """'naturalearthdata.com, offered to the Public Domain per…' 같은 긴 설명을 이름만 남긴다."""
    a = re.split(r"[,;(\n]| - | – ", artist or "")[0].strip()
    a = re.sub(r"^(by|photo by|flickr user)\s+", "", a, flags=re.I).strip()
    if not a or len(a) > 30 or a.lower() in ("unknown", "anonymous", "unknown author") \
            or "public domain" in a.lower() or a.lower().startswith("http"):
        return ""
    return a


def credit_of(c: dict) -> str:
    who = _short_artist(c["artist"])
    lic = c["license"]
    if re.match(r"(cc0|public domain|pd)", lic, re.I):
        return f"사진: {who + ' · ' if who else ''}퍼블릭 도메인 (Wikimedia Commons)"
    return f"사진: {who or '작자 미상'} / {lic} (Wikimedia Commons)"


def find_for_slots(title: str, slots: list, dests: list, log=None) -> list:
    """슬롯별 [meta dict 또는 None]. meta: caption·credit·credit_url·file."""
    def _emit(m):
        if log:
            try:
                log(m)
            except Exception:
                pass

    try:
        queries, broad = plan_queries(title, slots)
    except Exception as e:
        _emit(f"  ⚠ 아카이브 검색어 생성 실패({str(e)[:60]}) — 위키미디어 건너뜀")
        return [None] * len(slots)

    used: set = set()
    used_lock = threading.Lock()

    def one(i):
        cands, seen = [], set()
        # 문단별 검색어 → 부족하면 글 전체의 넓은 검색어로 보충
        for q in queries[i] + broad:
            if len(cands) >= 10:
                break
            for c in search(q):
                if c["url"] in seen or c["url"] in used:
                    continue
                seen.add(c["url"])
                cands.append(c)
        if not cands:
            return i, None, "후보 없음"
        best, caption = _pick(slots[i], cands[:10])
        if best is None:
            return i, None, f"후보 {len(cands[:10])}장 중 맞는 자료 없음"
        c = cands[best]
        with used_lock:
            if c["url"] in used:  # 다른 슬롯이 먼저 고른 경우
                return i, None, "중복"
            used.add(c["url"])
        r = requests.get(c["url"], headers=UA, timeout=60)
        r.raise_for_status()
        Path(dests[i]).write_bytes(r.content)
        return i, {"caption": caption or c["title"], "credit": credit_of(c),
                   "credit_url": c["page"], "remote_url": c["url"],
                   "archive_title": c["title"], "license": c["license"]}, ""

    results = [None] * len(slots)
    with cf.ThreadPoolExecutor(max_workers=3) as pool:
        for fut in cf.as_completed([pool.submit(one, i) for i in range(len(slots))]):
            try:
                i, meta, why = fut.result()
            except Exception:
                continue
            results[i] = meta
            if meta:
                _emit(f"  ✓ 위키미디어 {i + 1}: {meta['archive_title'][:40]} ({meta['license']})")
            else:
                _emit(f"  · 위키미디어 {i + 1}: {why} → 다른 방식으로 채움")
    return results
