"""구글 트렌드 '지금 인기 급상승' 목록 (한국). 트렌드 페이지가 쓰는 내부 데이터 요청을 사용한다.

공식 API가 아니라 형식이 바뀔 수 있다. 실패하면 공개 RSS(10건)로 대신한다.
기준은 트렌드 페이지의 '대한민국 · 지난 N시간 · 모든 카테고리 · 활성 항목만 · 관련성 기준'(2026-10-29 사용자 지정).
순서는 페이지에 표시되는 순서(관련성순) 그대로이며, 이미 끝난 트렌드는 빼고 1위부터 limit위까지 돌려준다.
"""

from __future__ import annotations

import json
import time

import requests

_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"}


def fetch(geo: str = "KR", limit: int = 20, hours: int = 24, active_only: bool = True) -> list:
    """[{rank, term, volume, started, related}] — 1위부터 limit위까지."""
    try:
        items = _fetch_rpc(geo, hours, active_only)
    except Exception:
        items = []
    if not items:
        items = _fetch_rss(geo)
    return [dict(it, rank=i + 1) for i, it in enumerate(items[:limit])]


def _fetch_rpc(geo: str, hours: int, active_only: bool = True) -> list:
    body = "f.req=" + requests.utils.quote(json.dumps(
        [[["i0OFE", json.dumps([None, None, geo, 0, "ko", hours, 1]), None, "generic"]]]))
    r = requests.post(
        "https://trends.google.com/_/TrendsUi/data/batchexecute?rpcids=i0OFE&hl=ko&rt=c",
        data=body, timeout=20,
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8", **_UA})
    r.raise_for_status()
    for line in r.text.splitlines():
        if '"wrb.fr"' not in line:
            continue
        rows = json.loads(json.loads(line)[0][2])[1]
        out = []
        for it in rows:
            if active_only and it[4]:  # it[4]: 종료 시각 — 값이 있으면 이미 끝난 트렌드
                continue
            started = (it[3] or [None])[0]
            out.append({"term": it[0], "volume": it[6] or 0, "started": started,
                        "related": [x for x in (it[9] or []) if x != it[0]][:4]})
        return out
    return []


def _fetch_rss(geo: str) -> list:
    import feedparser

    r = requests.get(f"https://trends.google.com/trending/rss?geo={geo}", headers=_UA, timeout=20)
    r.raise_for_status()
    out = []
    for e in feedparser.parse(r.content).entries:
        vol = "".join(ch for ch in str(e.get("ht_approx_traffic", "")) if ch.isdigit())
        out.append({"term": e.get("title", ""), "volume": int(vol or 0), "started": None,
                    "related": [e["ht_news_item_title"]] if e.get("ht_news_item_title") else []})
    return out


def hours_ago(started) -> str:
    if not started:
        return ""
    h = max(0, int((time.time() - started) // 3600))
    return f"{h}시간 전 시작" if h else "방금 시작"


_MOBILE_UA = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1"}


def daum_realtime() -> list:
    """다음 '실시간 트렌드'(베타) → [{rank, term}]. 현재 10위까지만 제공되며 01~06시에는 제한적으로 운영된다.

    다음은 예전의 실시간 검색어 서비스를 없애고 이 목록으로 대체했다. 공식 API가 아니라 검색 페이지 HTML을 읽는다.
    """
    import re

    try:
        r = requests.get("https://m.search.daum.net/search?w=tot&DA=RT1&rtmaxcoll=AIO,NNS,DNS&q=",
                         headers=_MOBILE_UA, timeout=20)
        r.encoding = "utf-8"
        seen, out = set(), []
        for rank, kw in re.findall(r'data-rank="(\d+)" data-keyword="([^"]+)"', r.text):
            if kw not in seen:
                seen.add(kw)
                out.append({"rank": int(rank), "term": kw})
        if out:
            return sorted(out, key=lambda x: x["rank"])
    except Exception:
        pass
    try:  # 예비: 첫 화면에 들어 있는 같은 목록
        r = requests.get("https://m.daum.net/", headers=_MOBILE_UA, timeout=20)
        r.encoding = "utf-8"
        kws = list(dict.fromkeys(re.findall(r'"keyword":"([^"]+)","rank":"\d+"', r.text)))
        return [{"rank": i + 1, "term": k} for i, k in enumerate(kws)]
    except Exception:
        return []
