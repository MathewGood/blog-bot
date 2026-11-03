"""Kimi(Moonshot) 독립 검색 API 래퍼 — Web Search Pro / Basic / Fetch.

2026-09 Moonshot이 공개한 REST API를 직접 쓴다. 모델의 내장 $web_search 도구와 달리
- 페이지 본문에서 질문과 관련된 대목(chunks)만 돌려주고
- 출처 신뢰도 등급(authority)을 함께 준다.
덕분에 토큰이 적게 들고, 공신력 있는 출처만 걸러 쓰기 쉽다.

authority 등급은 S > A > B > UK(미상) 순이지만, 실측해 보면 YouTube가 A,
질병관리청이 UK로 나오는 등 우리 기준과 어긋난다. 그래서 등급은 보조 신호로만 쓰고
도메인 정책(_domain_rank)으로 최종 판단한다.
"""

from __future__ import annotations

import concurrent.futures as cf
import datetime
import re

import requests

from . import config

SEARCH_URL = config.KIMI_BASE_URL.rstrip("/") + "/tools/search"
SEARCH_PRO_URL = config.KIMI_BASE_URL.rstrip("/") + "/tools/search_pro"
FETCH_URL = config.KIMI_BASE_URL.rstrip("/") + "/tools/fetch"

# 공신력 우선순위(높을수록 좋음). 3=공공·학술, 2=주요 언론·국제기구, 1=그 외 허용
_GOOD_PATTERNS = (
    (3, r"\.go\.kr|\.gov$|\.gov/|\.gov\.|kdca|kostat|bok\.or\.kr|nhis|mohw|molit|moef"),
    (3, r"\.ac\.kr|\.edu|pubmed|ncbi\.nlm\.nih\.gov|doi\.org|sciencedirect|springer|nature\.com|thelancet|bmj\.com|jamanetwork|koreamed|kiss\.kstudy|scienceon|riss\.kr|dbpia"),
    (3, r"who\.int|oecd\.org|imf\.org|worldbank\.org|un\.org|iea\.org|ilo\.org|eia\.gov|ecb\.europa|federalreserve\.gov"),
    (2, r"yna\.co\.kr|연합뉴스|kbs\.co\.kr|mbc\.co\.kr|sbs\.co\.kr|chosun\.com|joongang\.co\.kr|donga\.com|hani\.co\.kr|khan\.co\.kr|mk\.co\.kr|hankyung\.com|edaily\.co\.kr|yonhapnews"),
    (2, r"reuters\.com|apnews\.com|bloomberg\.com|ft\.com|wsj\.com|nytimes\.com|economist\.com|bbc\.co|cnbc\.com|tradingeconomics|ceicdata"),
    (2, r"\.or\.kr|\.org$|\.org/"),
)
# 인용하면 안 되는 곳 (개인 블로그·커뮤니티·위키·영상·홍보)
_BAD_PATTERN = re.compile(
    r"youtube\.com|youtu\.be|tistory\.com|blog\.naver|blogspot|brunch\.co\.kr|"
    r"namu\.wiki|나무위키|wikipedia|위키백과|cafe\.naver|dcinside|fmkorea|clien|"
    r"pinterest|facebook|instagram|twitter|x\.com|windows\.net|slideshare|"
    r"reddit\.com|quora\.com|medium\.com|threads\.net|tiktok|kin\.naver|지식in|"
    r"velog\.io|egloos|daum\.net/cafe|cafe\.daum|ppomppu|theqoo|instiz|"
    r"hidoc|health\.chosun\.com/ad|ad\.|/ads/",
    re.I,
)
_AUTHORITY_BONUS = {"S": 2, "A": 1, "B": 0, "UK": 0}


def _domain_rank(item: dict) -> int:
    """이 출처를 브리프에 써도 되는지 점수로 환산. 0이면 제외."""
    hay = f"{item.get('url','')} {item.get('site_name','')}".lower()
    if _BAD_PATTERN.search(hay):
        return 0
    for score, pat in _GOOD_PATTERNS:
        if re.search(pat, hay, re.I):
            return score + _AUTHORITY_BONUS.get(item.get("authority"), 0)
    return 1 + _AUTHORITY_BONUS.get(item.get("authority"), 0)


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {config.KIMI_API_KEY}",
        "Content-Type": "application/json",
    }


# 분야별 공신력 도메인(sites 지정용, API 한도 5개). 역사는 최신성·기관 제한이 맞지 않아 두지 않는다.
TRUSTED_SITES = {
    "경제": ["bok.or.kr", "kostat.go.kr", "moef.go.kr", "imf.org", "oecd.org"],
    "라이프": ["kdca.go.kr", "mohw.go.kr", "nhis.or.kr", "who.int", "kostat.go.kr"],
    "AI·테크": ["msit.go.kr", "nia.or.kr", "oecd.org", "nature.com", "iea.org"],
    "사회·문화": ["kostat.go.kr", "mohw.go.kr", "mogef.go.kr", "kihasa.re.kr", "oecd.org"],
}
# 분야별 자료 최신성(일). 역사는 기간 제한 없음.
RECENT_DAYS = {"경제": 365, "라이프": 730, "AI·테크": 365, "사회·문화": 730}


def time_window(days: int):
    """최근 days일 → API의 time_window 객체. days가 없으면 None(제한 없음)."""
    if not days:
        return None
    today = datetime.date.today()
    return {"start": (today - datetime.timedelta(days=int(days))).isoformat(),
            "end": today.isoformat()}


def _body(query: str, days=None, sites=None, limit=None) -> dict:
    body = {"text_query": query}
    tw = time_window(days)
    if tw:
        body["time_window"] = tw
    if sites:
        body["sites"] = list(sites)[:5]  # API 한도: 최대 5개(OR 결합)
    if limit:
        body["limit"] = max(1, min(int(limit), 20))
    return body


def search_pro(query: str, timeout: int = 60, days=None, sites=None, limit=None) -> list:
    """질문과 관련된 페이지 '대목'을 돌려준다. 실패하면 빈 리스트.

    days: 최근 N일 안에 게시된 글만 / sites: 이 도메인(최대 5개)에서만 / limit: 1~20건.
    """
    try:
        r = requests.post(SEARCH_PRO_URL, headers=_headers(),
                          json=_body(query, days, sites, limit), timeout=timeout)
        if r.status_code != 200:
            return []
        r.encoding = "utf-8"
        return r.json().get("search_results", []) or []
    except Exception:
        return []


def search_basic(query: str, timeout: int = 30) -> list:
    try:
        r = requests.post(SEARCH_URL, headers=_headers(),
                          json={"text_query": query}, timeout=timeout)
        if r.status_code != 200:
            return []
        r.encoding = "utf-8"
        return r.json().get("search_results", []) or []
    except Exception:
        return []


def fetch_page(url: str, timeout: int = 45) -> str:
    """URL 본문을 마크다운으로 가져온다 (검색 대목이 너무 짧을 때 보강용)."""
    try:
        r = requests.post(FETCH_URL, headers=_headers(),
                          json={"url": url}, timeout=timeout)
        if r.status_code != 200:
            return ""
        r.encoding = "utf-8"
        return (r.json().get("markdown") or "").strip()
    except Exception:
        return ""


def _chunk_text(item: dict) -> str:
    chunks = item.get("chunks") or []
    text = " ".join((c.get("text") or "").strip() for c in chunks).strip()
    return text or (item.get("snippet") or "").strip()


def gather(queries: list, min_rank: int = 2, per_query: int = 5,
           max_sources: int = 14, log=None, days=None, sites=None, sites_for: int = 2) -> list:
    """여러 질의를 병렬 검색해 공신력 순으로 정리한 자료 목록을 만든다.

    days: 모든 질의를 최근 N일 글로 제한. sites: 앞의 sites_for개 질의는 해당 공신력 도메인에서
    한 번 더 검색해 합친다(호출은 성공한 건만 과금).
    반환 항목: {title, site, url, date, authority, rank, text}
    """
    results: list = []
    seen_urls: set = set()

    jobs = [(q, None) for q in queries]
    if sites:
        jobs += [(q, sites) for q in queries[:sites_for]]
    if not jobs:
        return []

    with cf.ThreadPoolExecutor(max_workers=min(len(jobs), 6)) as pool:
        fetched = pool.map(lambda j: search_pro(j[0], days=days, sites=j[1]), jobs)
        for (query, only_sites), items in zip(jobs, fetched):
            kept = 0
            for it in items[:per_query]:
                url = it.get("url", "")
                if not url or url in seen_urls:
                    continue
                rank = _domain_rank(it)
                if rank < min_rank:
                    continue
                text = _chunk_text(it)
                if len(text) < 60:
                    continue
                seen_urls.add(url)
                results.append({
                    "title": it.get("title", ""),
                    "site": it.get("site_name", ""),
                    "url": url,
                    "date": it.get("date", ""),
                    "authority": it.get("authority", ""),
                    "rank": rank,
                    "text": text[:2500],
                    "query": query,
                })
                kept += 1
            if log:
                tag = " [공신력 도메인]" if only_sites else ""
                log(f"  · 검색 '{query[:28]}'{tag} → 채택 {kept}건")

    # 공신력 순, 같은 등급 안에서는 최신 연도 자료 먼저(날짜가 없으면 작년으로 간주)
    this_year = datetime.date.today().year

    def _year(x):
        m = re.match(r"(20\d{2})", x.get("date") or "")
        return int(m.group(1)) if m else this_year - 1

    results.sort(key=lambda x: (-x["rank"], -_year(x)))
    return results[:max_sources]


def sources_block(sources: list) -> str:
    """모델 프롬프트에 넣을 자료 블록."""
    out = []
    for i, s in enumerate(sources, 1):
        head = f"[{i}] {s['title']} — {s['site']}"
        if s.get("date"):
            head += f" ({s['date']})"
        out.append(f"{head}\n{s['url']}\n{s['text']}")
    return "\n\n".join(out)
