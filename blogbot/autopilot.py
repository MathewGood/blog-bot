"""소재 자동 발굴 + 네이버 예약발행 파이프라인.

흐름: 소재 발굴(구글 트렌드 4시간 1~20위 ∩ 다음 실시간 트렌드 80% + 뉴욕타임스 20%, 독자에 맞게 선별)
      → 글·사진 생성(생산 스레드) → 네이버 '예약 발행'(발행 스레드, 기본 13분 간격 슬롯)

생성과 발행을 따로 돌려 두 단계가 겹치게 한다(생성 중에 이전 글을 예약).
슬롯은 네이버 예약 UI의 분 단위(00·10·20…)에 맞춘 10분 격자 위에 둔다. 간격이 13분처럼 격자와 안 맞으면
평균이 그 간격이 되도록 가장 가까운 격자(10·20·10·10·20…분 간격)에 놓는다.
"""

from __future__ import annotations

import datetime as dt
import json
import queue
import re
import threading
import time

from . import config

STATE_FILE = config.ROOT / "autopilot_state.json"
MAX_COUNT = 20
GRID_MIN = 10              # 네이버 예약 UI의 분 선택 단위
DEFAULT_INTERVAL = 13      # 글과 글 사이 평균 간격(분) — 2026-10-30 사용자 지정
MIN_LEAD_MIN = 15          # 예약 시각은 지금부터 최소 이만큼 뒤여야 한다
DEFAULT_START_MIN = 20     # 첫 글은 지금부터 이 정도 뒤 격자 시각에

_LOCK = threading.Lock()
STOP = threading.Event()
STATE: dict = {"running": False, "items": [], "log": [], "message": ""}


# ─────────────────────────────────────────────────────────────
# 상태 · 로그
# ─────────────────────────────────────────────────────────────

def _log(msg: str):
    line = f"{dt.datetime.now():%H:%M:%S} {msg}"
    print(f"[autopilot] {msg}", flush=True)
    with _LOCK:
        STATE["log"].append(line)
        del STATE["log"][:-200]


def snapshot() -> dict:
    with _LOCK:
        return json.loads(json.dumps(STATE, ensure_ascii=False, default=str))


def _set(item: dict, **kw):
    with _LOCK:
        item.update(kw)


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"used": []}


def _remember(query: str, title: str):
    st = _load_state()
    st.setdefault("used", []).append({"query": query, "title": title, "at": int(time.time())})
    st["used"] = st["used"][-300:]
    try:
        STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def _norm(s: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]", "", (s or "").lower())


def used_titles(limit: int = 60) -> list:
    """이미 쓴 글 제목·소재 (중복 소재 방지용)."""
    out = [u.get("title") or u.get("query") or "" for u in _load_state().get("used", [])]
    try:
        for d in sorted(config.DRAFTS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)[:80]:
            pj = d / "post.json"
            if pj.exists():
                out.append(json.loads(pj.read_text(encoding="utf-8")).get("title", ""))
    except Exception:
        pass
    seen, uniq = set(), []
    for t in out:
        k = _norm(t)
        if t and k not in seen:
            seen.add(k)
            uniq.append(t)
    return uniq[:limit]


def _too_similar(query: str, used: list) -> bool:
    q = _norm(query)
    if not q:
        return True
    for u in used:
        n = _norm(u)
        if not n:
            continue
        if q in n or n in q:
            return True
        a, b = set(q[i:i + 2] for i in range(len(q) - 1)), set(n[i:i + 2] for i in range(len(n) - 1))
        if a and b and len(a & b) / max(1, min(len(a), len(b))) > 0.7:
            return True
    return False


# ─────────────────────────────────────────────────────────────
# 소재 발굴
#  · 독자 사연 대기열은 가장 후순위 — 트렌드·NYT 글이 요청 편수에 모자랄 때만 그 부족분을 맨 끝에 채운다(2026-11-03 사용자 지정).
#  · 트렌드 글(80%): 구글 4시간 1~20위(활성 항목만·모든 카테고리·관련성순)에서 한국 5 : 미국 3 : 일본 2.
#    한국은 다음 실시간과 겹친 이슈를 먼저 쓰고, 모자라면 한국 구글 상위로 채운다(2026-10-30 사용자 지정).
#  · NYT 글(20%): 뉴욕타임스 피드에서 고른 글감
# ─────────────────────────────────────────────────────────────

TREND_TOP = 20
TREND_HOURS = 4
NYT_SHARE = 0.2
SRC_BOTH, SRC_GOOGLE, SRC_NYT = "구글·다음 겹침", "구글(보충)", "NYT"
SRC_STORY = "독자 사연"
FOREIGN_GEOS = {"JP": "일본", "US": "미국"}
# 트렌드 글의 나라별 비율 (2026-10-30 사용자 지정 한국 5 : 미국 3 : 일본 2)
GEO_ORDER = ("KR", "US", "JP")
GEO_SHARE = {"KR": 5, "US": 3, "JP": 2}
GEO_LABEL = {"KR": "한국", "US": "미국", "JP": "일본"}
SRC_FOREIGN = {g: f"구글 {n}" for g, n in FOREIGN_GEOS.items()}

_AUD_DESC = {
    "women": "40~65세 여성 (건강, 가계와 노후, 부모·자녀, 일상 선택, 교양에 관심)",
    "worker": "40~65세 직장인 (출퇴근 시간대에 휴대전화로 읽음. 커리어·직장 생활, 재테크와 노후 준비, "
              "건강 관리, 업무에 쓰는 AI·기술, 경제 흐름에 관심)",
}


def split_counts(n: int) -> tuple:
    """전체 n편 → (트렌드 글 수, NYT 글 수). NYT는 20%(3편 이상일 때 최소 1편)."""
    nyt = int(n * NYT_SHARE + 0.5) if n >= 3 else 0
    return n - nyt, nyt


def split_geo(n: int) -> dict:
    """트렌드 글 n편 → 나라별 편수(최대잔여법, 한국 5 : 미국 3 : 일본 2)."""
    total = sum(GEO_SHARE.values())
    raw = {g: n * GEO_SHARE[g] / total for g in GEO_ORDER}
    out = {g: int(raw[g]) for g in GEO_ORDER}
    for g in sorted(GEO_ORDER, key=lambda x: -(raw[x] - out[x]))[:n - sum(out.values())]:
        out[g] += 1
    return out


def _trend_candidates(log, geo: str = "KR") -> list:
    """구글 트렌드(4시간 기준) 1~20위 + 각 키워드의 관련 뉴스 제목(맥락). geo: KR | JP | US."""
    from . import kimi_search, trends

    label = FOREIGN_GEOS.get(geo, "한국")
    items = trends.fetch(geo, TREND_TOP, hours=TREND_HOURS)
    if not items:
        if geo == "KR":
            raise RuntimeError("구글 트렌드 목록을 가져오지 못했습니다.")
        log(f"  · 구글 트렌드({label})를 가져오지 못해 건너뜁니다")
        return []
    for it in items:
        it["geo"] = geo
    log(f"구글 트렌드({label}, {TREND_HOURS}시간) 1~{len(items)}위: " + ", ".join(f"{i['rank']}.{i['term']}" for i in items))

    def news(it):
        if not config.KIMI_API_KEY:
            return it["related"]
        try:
            q = f"{it['term']} 최근 이슈" if geo == "KR" else f"{it['term']} latest news"
            hits = kimi_search.search_pro(q, days=14) or []
            return [h.get("title", "") for h in hits[:3] if h.get("title")] or it["related"]
        except Exception:
            return it["related"]

    import concurrent.futures as cf

    with cf.ThreadPoolExecutor(max_workers=6) as pool:
        for it, titles in zip(items, pool.map(news, items)):
            it["news"] = titles
    return items


def _find_overlap(google: list, daum: list, log) -> list:
    """구글 후보 중 다음 실시간 트렌드에도 오른 이슈 → 해당 후보에 daum 키워드를 붙여 반환.

    두 목록 모두 관련 뉴스 제목을 붙여 Kimi가 '같은 사건·같은 인물의 같은 뉴스'인지 엄격히 판정한다.
    낱말만 비슷한 것(예: 재혼 소식 ↔ 드라마 '재혼 황후')은 겹침이 아니다.
    AI를 못 쓰면 키워드가 사실상 같은 경우(길이 3자 이상 포함 관계)만 인정한다."""
    from . import kimi_search, research

    hit: dict = {}
    for g in google:
        gn = _norm(g["term"])
        for d in daum:
            dn = _norm(d["term"])
            short = min(len(gn), len(dn))
            if short >= 3 and (gn == dn or gn in dn or dn in gn):
                hit[g["rank"]] = d["term"]
                break
    if daum and config.KIMI_API_KEY:
        try:
            def dctx(d):
                try:
                    return [h.get("title", "") for h in (kimi_search.search_pro(f"{d['term']} 최근 이슈", days=14) or [])[:3]]
                except Exception:
                    return []

            import concurrent.futures as cf

            with cf.ThreadPoolExecutor(max_workers=5) as pool:
                for d, t in zip(daum, pool.map(dctx, daum)):
                    d["news"] = t
            g_lines = "\n".join(f"{g['rank']}위 | {g['term']} | 뉴스: {' / '.join(g.get('news') or g['related'])[:200]}"
                                for g in google)
            d_lines = "\n".join(f"- {d['term']} | 뉴스: {' / '.join(d.get('news', []))[:200]}" for d in daum)
            text = research._kimi_chat([{"role": "user", "content": (
                "[구글]의 급상승 검색어와 [다음] 실시간 트렌드 키워드 중, **같은 사건·같은 인물의 같은 뉴스**를 가리키는 쌍만 "
                "골라라. 각 키워드의 뉴스 제목으로 실제로 같은 이슈인지 확인하라. 표현이 달라도 같은 뉴스면 인정하지만, "
                "낱말만 같거나 막연히 비슷한 주제는 제외한다(예: 연예인의 재혼 소식과 드라마 제목 '재혼 황후'는 다른 이슈). "
                "확실하지 않으면 제외하라. 없으면 빈 배열.\n\n"
                f"[구글]\n{g_lines}\n\n[다음]\n{d_lines}\n\n"
                'JSON만 출력: {"matches":[{"google_rank":숫자,"daum":"다음 키워드","reason":"같은 뉴스 한 줄"}]}')}],
                config.FALLBACK_MODEL, max_tokens=1500, json_mode=True, timeout=90)
            m = re.search(r"\{.*\}", text, re.S)
            terms = {d["term"] for d in daum}
            for x in (json.loads(m.group(0)).get("matches", []) if m else []):
                if isinstance(x, dict) and x.get("daum") in terms:
                    hit.setdefault(int(x.get("google_rank", -1)), x["daum"])
                    log(f"  · 겹침 판정: {x.get('google_rank')}위 ↔ {x['daum']} — {str(x.get('reason', ''))[:60]}")
        except Exception as ex:
            log(f"  · 겹침 판정 AI 실패({str(ex)[:60]}) — 키워드가 같은 경우만 겹침으로 인정")
    return [dict(g, daum=hit[g["rank"]]) for g in google if g["rank"] in hit]


def _nyt_candidates(cats: list, log) -> list:
    import feedparser

    out = []
    for cat in cats:
        for feed_url in config.NYT_FEEDS.get(cat, [])[:2]:
            try:
                for e in feedparser.parse(feed_url).entries[:6]:
                    out.append({"category": cat, "title": e.get("title", ""),
                                "summary": re.sub(r"<[^>]+>", "", e.get("summary", ""))[:200]})
            except Exception as ex:
                log(f"  · NYT 피드 실패({cat}): {str(ex)[:50]}")
    return [c for c in out if c["title"]]


def _pick(cands: list, used: list, cats: list, want: int, audience: str, kind: str) -> list:
    """Kimi가 독자에게 맞는 소재를 주제문으로 고른다. kind: 'trend' | 'nyt'."""
    from . import research

    cat_names = ", ".join(f"{k}={config.CATEGORIES[k]}" for k in cats)
    who = _AUD_DESC[audience]
    if kind == "trend":
        lines = "\n".join(
            f"{FOREIGN_GEOS.get(c.get('geo'), '한국')} {c['rank']}위 | {c['term']} | 다음 실시간: {c.get('daum', '-')} | 관련: "
            f"{' / '.join(c.get('news') or c['related'])[:160]}" for c in cands)
        head = (f"아래는 구글 트렌드(한국·일본·미국, 4시간 기준) 급상승 검색어다. **이 목록 안에서만** 블로그 글 소재를 골라라"
                f"(최대 {want}개, 한 키워드는 한 번만). 독자에게 맞지 않는 키워드는 억지로 쓰지 말고 건너뛰어라 — 개수가 모자라도 된다. "
                "나라 구분 없이 순위가 높은 것을 우선하되, 일본·미국 키워드는 한국 독자가 읽을 만한 이슈일 때만 고르고 "
                "일본어·영어 키워드는 뜻을 풀어 한국어 주제문으로 써라.\n"
                "스포츠(경기·선수·구단 이슈)도 소재로 쓴다 — 경기 결과 속보를 옮기지 말고 팬·직장인의 생활, 지역 경제, 문화 현상 같은 "
                "관점의 주제문으로 바꾸고, 분야 키는 society(사회·문화)로 둔다.\n"
                "건너뛸 것: 연예인 사생활·가십, 정당 간 정치 공방, 사건사고 속보, 특정 종목 투자 권유, "
                "의미가 불분명한 단어.\n"
                "고를 때: 키워드가 가리키는 이슈를 독자의 생활·일 관점의 주제문으로 바꿔라(25~50자). 뉴스 제목을 그대로 쓰지 말고 "
                "자료 조사가 가능한 구체적 주제여야 한다.")
        key = "trend"
    else:
        lines = "\n".join(f"- [{c['category']}] {c['title']} — {c['summary']}" for c in cands[:60])
        head = (f"아래는 뉴욕타임스 최근 기사 헤드라인이다. 한국 독자에게 의미 있는 글감 {want}개를 골라라. "
                "기사를 번역하지 말고 한국 독자 관점의 주제문(25~50자)으로 다시 써라. 정치 편향·투자 종목 권유는 피하라. "
                "자료 조사가 가능한 구체적 주제여야 한다.")
        key = "title"
    prompt = (
        f"당신은 한국 네이버 블로그 편집장이다. 독자는 {who}이다.\n{head}\n"
        "아래 '이미 쓴 글'과 겹치지 마라.\n"
        f"분야 키: {cat_names}.\n\n[후보]\n{lines}\n\n[이미 쓴 글]\n" + "\n".join(f"- {t}" for t in used[:40]) +
        '\n\nJSON만 출력: {"topics":[{"trend":"원래 키워드 또는 기사 제목","category":"분야키","query":"주제문",'
        '"angle":"돈과 자본의 흐름 관점 한 줄(누가 얻고 누가 지불하나, 돈이 어디로 가는가)"}]}')
    text = research._kimi_chat([{"role": "user", "content": prompt}], config.FALLBACK_MODEL,
                               max_tokens=3000, json_mode=True, timeout=120)
    m = re.search(r"\{.*\}", text, re.S)
    data = json.loads(m.group(0)) if m else {}
    return [t for t in data.get("topics", []) if isinstance(t, dict)]


def _clean(picked: list, cats: list, used: list, seen_q: list, source) -> list:
    """source: 라벨 문자열, 또는 원래 키워드(trend)를 받아 라벨을 돌려주는 함수."""
    out, seen_t = [], set()
    for p in picked:
        q = (p.get("query") or "").strip()
        t = str(p.get("trend") or "")
        if p.get("category") not in cats or not q or t in seen_t or _too_similar(q, used + seen_q):
            continue
        seen_q.append(q)
        seen_t.add(t)
        out.append({"category": p["category"], "query": q, "angle": (p.get("angle") or "").strip(),
                    "trend": t, "source": source(t) if callable(source) else source})
    return out


def _interleave(items: list, cats: list) -> list:
    """분야가 번갈아 나오도록 섞는다."""
    buckets = {c: [i for i in items if i["category"] == c] for c in cats}
    out = []
    while any(buckets.values()):
        for c in cats:
            if buckets[c]:
                out.append(buckets[c].pop(0))
    return out


def discover_topics(n: int, categories: list, audience: str = "women", fill: bool = False, log=None,
                    only_stories: str = "") -> dict:
    """소재 발굴 → {"primary": [...n편 순서], "spare": [...실패 대비 여분]}.

    트렌드 글: 구글(4시간) 1~20위에서 한국 5 : 미국 3 : 일본 2 비율. 한국은 다음 실시간과 겹친 이슈 우선, 모자라면 한국 구글 상위로
    채운다(fill 인자는 호환용으로 남겨 두었고 항상 채운다). 나라 몫이 비면 다른 나라 소재로 채움.
    NYT 글: 전체의 20%.
    only_stories="pann": 트렌드·NYT 없이 판 인기글 사연 대기열에서만 고른다(2시간 자동 작성용)."""
    from . import trends

    log = log or _log
    cats = [c for c in categories if c in config.CATEGORIES] or list(config.CATEGORIES)
    if not config.KIMI_API_KEY:
        raise RuntimeError("소재 선별에 KIMI_API_KEY가 필요합니다.")
    used, seen_q = used_titles(), []
    trend_n, nyt_n = split_counts(n)
    # 독자 사연은 트렌드·NYT 소재가 모자랄 때만 쓴다 (아래에서 부족분만 채움)
    from . import pann_feed, stories

    # 구글 드라이브의 네이트 판 인기글 파일에서 새 글을 가져와 대기열에 넣는다(이미 본 글은 건너뜀)
    try:
        r = pann_feed.sync(log)
        log(f"판 인기글: 새 글 {r['new']}편 중 대기열 {r['queued']}편 추가 (중복·부적합 {r['dup'] + r['skipped']}편)")
    except Exception as ex:
        log(f"판 인기글 가져오기 실패({str(ex)[:60]}) — 대기열에 있는 사연만 사용")
    # 직접 넣은 사연이 먼저, 판 출신은 1회 최대 MAX_PER_RUN편
    queue = [] if only_stories else [s for s in stories.all_items() if not s.get("pann_id")]
    queue += [s for s in stories.all_items() if s.get("pann_id")][:pann_feed.MAX_PER_RUN]

    def story_items_for(k: int) -> list:
        out = []
        for st in queue[:k]:
            out.append({"category": st["category"], "query": st["query"], "angle": st.get("angle", ""),
                        "trend": "", "source": SRC_STORY, "story_id": st["id"]})
            seen_q.append(st["query"])
        return out

    if only_stories:
        items = story_items_for(n)
        log(f"독자: {config.AUDIENCES[audience]['label']} | 판 인기글 사연 {len(items)}편 (트렌드·NYT 제외)")
        return {"primary": items[:n], "spare": []}
    log(f"독자: {config.AUDIENCES[audience]['label']} | 트렌드 글 {trend_n}편 + NYT 글 {nyt_n}편 "
        f"(모자라면 독자 사연 {len(queue)}편 대기 중에서 채움)")

    trend_items = []
    if trend_n:
        google = _trend_candidates(log)
        daum = trends.daum_realtime()
        log(f"다음 실시간 트렌드({len(daum)}개): " + ", ".join(f"{d['rank']}.{d['term']}" for d in daum))
        both = _find_overlap(google, daum, log)
        log(f"겹치는 이슈 {len(both)}개: " + (", ".join(f"{b['rank']}위 {b['term']}↔{b['daum']}" for b in both) or "없음"))
        for b in both:
            b["geo"] = "KR"
        foreign = []
        for geo in FOREIGN_GEOS:
            try:
                foreign += _trend_candidates(log, geo)
            except Exception as ex:
                log(f"  · 구글 트렌드({FOREIGN_GEOS[geo]}) 실패({str(ex)[:50]}) — 건너뜁니다")
        quota = split_geo(trend_n)
        log("트렌드 글 나라별 몫: " + ", ".join(f"{GEO_LABEL[g]} {quota[g]}편" for g in GEO_ORDER))
        picked = {g: [] for g in GEO_ORDER}
        # 한국: 다음 실시간과 겹친 이슈를 먼저, 몫이 모자라면 겹치지 않은 한국 구글 상위로 채운다
        if both and quota["KR"]:
            picked["KR"] = _clean(_pick(both, used, cats, quota["KR"] + 2, audience, "trend"),
                                  cats, used, seen_q, SRC_BOTH)
        if len(picked["KR"]) < quota["KR"]:
            both_ranks = {b_["rank"] for b_ in both}
            rest = [g for g in google if g["rank"] not in both_ranks]
            for g in rest:
                g["geo"] = "KR"
            more = _clean(_pick(rest, used, cats, quota["KR"] - len(picked["KR"]) + 2, audience, "trend"),
                          cats, used, seen_q, SRC_GOOGLE)
            picked["KR"] += more
            log(f"  · 한국 겹침이 {len(both)}개뿐이라 구글 상위에서 {len(more)}개 더 골랐습니다")
        for geo in ("US", "JP"):
            cands = [f for f in foreign if f["geo"] == geo]
            if cands and quota[geo]:
                picked[geo] = _clean(_pick(cands, used, cats, quota[geo] + 2, audience, "trend"),
                                     cats, used, seen_q, SRC_FOREIGN[geo])
        for g in GEO_ORDER:
            log(f"  · {GEO_LABEL[g]} 소재 {len(picked[g])}개 선별 (몫 {quota[g]})")
        mains = {g: picked[g][:quota[g]] for g in GEO_ORDER}
        extras = {g: picked[g][quota[g]:] for g in GEO_ORDER}
        lack = trend_n - sum(len(v) for v in mains.values())
        for g in GEO_ORDER:                      # 어느 나라 몫이 비면 다른 나라의 남는 소재로 채운다
            while lack > 0 and extras[g]:
                mains[g].append(extras[g].pop(0))
                lack -= 1
        if lack > 0:
            log(f"  · 나라별 소재가 모자라 트렌드 글이 {lack}편 부족합니다")
        merged = []                              # 나라가 번갈아 나오도록 섞고, 분야도 번갈아 나오게 한다
        for k in range(max([len(v) for v in mains.values()] + [0])):
            merged += [mains[g][k] for g in GEO_ORDER if k < len(mains[g])]
        trend_items = _interleave(merged, cats) + [it for g in GEO_ORDER for it in extras[g]]

    nyt_items = []
    if nyt_n:
        try:
            nyt_items = _clean(_pick(_nyt_candidates(cats, log), used, cats, nyt_n + 1, audience, "nyt"),
                               cats, used, seen_q, SRC_NYT)
        except Exception as ex:
            log(f"  · NYT 소재 선별 실패({str(ex)[:70]}) — NYT 글은 건너뜁니다")

    t_main, t_spare = trend_items[:trend_n], trend_items[trend_n:]
    n_main, n_spare = nyt_items[:nyt_n], nyt_items[nyt_n:]
    primary = list(t_main)
    total = len(primary) + len(n_main)
    for j, it in enumerate(n_main):        # NYT 글은 중간중간 고르게 끼워 넣는다
        primary.insert(min(len(primary), int(round((j + 1) * total / (len(n_main) + 1)))), it)
    story_items = story_items_for(max(0, n - len(primary)))   # 트렌드·NYT가 모자란 만큼만 사연으로 채운다
    primary += story_items                 # 독자 사연은 가장 후순위 — 맨 끝에 배치
    log(f"소재 확정: 독자 사연 {len(story_items)}개 + 트렌드 {len(t_main)}개 + NYT {len(n_main)}개 (요청 {n}편)")
    return {"primary": primary, "spare": t_spare + n_spare}


# ─────────────────────────────────────────────────────────────
# 슬롯 계산
# ─────────────────────────────────────────────────────────────

def ceil_grid(t: dt.datetime, step: int = 10) -> dt.datetime:
    t = t.replace(second=0, microsecond=0)
    extra = (-t.minute) % step
    return t + dt.timedelta(minutes=extra)


def first_slot(start_in_min: int = DEFAULT_START_MIN, step: int = 10) -> dt.datetime:
    return ceil_grid(dt.datetime.now() + dt.timedelta(minutes=max(start_in_min, MIN_LEAD_MIN)), step)


def slot_for(i: int, start: dt.datetime, step: int, last: dt.datetime = None) -> dt.datetime:
    """i번째 글의 예약 시각. step은 평균 간격(분)이고 시각은 10분 격자에 맞춘다(13분 → 10·20·10·10·20…).
    생산이 늦어 시각이 임박·경과했으면 가장 가까운 유효 격자로 미룬다."""
    offset = int(step * i / GRID_MIN + 0.5) * GRID_MIN
    slot = start + dt.timedelta(minutes=offset)
    floor = ceil_grid(dt.datetime.now() + dt.timedelta(minutes=MIN_LEAD_MIN), GRID_MIN)
    if slot < floor:
        slot = floor
    if last is not None and slot <= last:
        slot = last + dt.timedelta(minutes=GRID_MIN)
    return slot


# ─────────────────────────────────────────────────────────────
# 실행
# ─────────────────────────────────────────────────────────────

def start(count: int, categories: list, generate_fn, publish_fn, interval: int = DEFAULT_INTERVAL,
          start_in_min: int = DEFAULT_START_MIN, audience: str = None, fill: bool = False,
          only_stories: str = "") -> dict:
    """백그라운드로 시작한다. generate_fn(query, category, angle, audience) → draft 이름,
    publish_fn(draft 이름, 예약 시각) → None (실패 시 예외).

    audience: 생략하면 지금 시각으로 정한다(06~09시·18~22시 직장인, 그 외 여성)."""
    audience = audience if audience in config.AUDIENCES else config.audience_for()
    count = max(1, min(int(count), MAX_COUNT))
    interval = max(GRID_MIN, int(interval))
    with _LOCK:
        if STATE.get("running"):
            raise RuntimeError("이미 자동 예약이 진행 중입니다.")
        STATE.clear()
        STATE.update({"running": True, "started": time.time(), "count": count, "interval": interval,
                      "categories": list(categories), "items": [], "log": [], "message": "소재 발굴 중",
                      "audience": config.AUDIENCES[audience]["label"], "fill": bool(fill),
                      "only_stories": only_stories})
    STOP.clear()
    t0 = first_slot(start_in_min)
    threading.Thread(target=_run, args=(count, categories, generate_fn, publish_fn, interval, t0, audience, fill, only_stories),
                     daemon=True).start()
    return {"first_slot": t0.strftime("%m/%d %H:%M"), "count": count, "interval": interval,
            "audience": config.AUDIENCES[audience]["label"]}


def stop():
    STOP.set()
    _log("중지 요청 — 진행 중인 단계가 끝나면 멈춥니다")


def _run(count, categories, generate_fn, publish_fn, interval, t0, audience="women", fill=False, only_stories=""):
    try:
        found = discover_topics(count, categories, audience, fill, only_stories=only_stories)
        topics, spare = found["primary"], found["spare"]
        if not topics and only_stories:
            raise RuntimeError("새로 쓸 판 인기글 사연이 없습니다 (이미 쓴 글·사연이 아닌 글·중복은 제외됨).")
        if not topics:
            raise RuntimeError("조건에 맞는 소재가 없습니다 (구글 4시간 1~20위와 다음 실시간 트렌드가 겹치는 이슈 중 "
                               "독자에게 맞는 것이 없음). '겹침이 부족하면 구글 상위로 보충'을 켜면 채울 수 있습니다.")
        n = len(topics)
        if n < count:
            _log(f"⚠ 조건에 맞는 소재가 {n}개뿐이라 {n}편만 진행합니다 (요청 {count}편)")
        _log(f"{n}편 생성·예약 시작 (첫 예약 {t0:%m/%d %H:%M}, 평균 {interval}분 간격)")
        for k, t in enumerate(topics):
            with _LOCK:
                STATE["items"].append({"i": k, "category": t["category"], "query": t["query"],
                                       "angle": t.get("angle", ""), "source": t.get("source", ""),
                                       "story_id": t.get("story_id", ""),
                                       "status": "대기", "draft": "", "title": "", "slot": "", "error": ""})
        with _LOCK:
            items = STATE["items"]
        q: queue.Queue = queue.Queue(maxsize=2)
        producer = threading.Thread(target=_produce, args=(items, spare, generate_fn, q, audience), daemon=True)
        producer.start()
        _consume(items, publish_fn, q, interval, t0)
        producer.join(timeout=5)
        done = sum(1 for i in items if i["status"] == "예약완료")
        with _LOCK:
            STATE["message"] = f"완료: {done}/{len(items)}편 예약" + (f" (요청 {count}편)" if len(items) < count else "")
    except Exception as e:
        _log(f"✗ 중단: {e}")
        with _LOCK:
            STATE["message"] = f"중단: {str(e)[:120]}"
    finally:
        with _LOCK:
            STATE["running"] = False


def _produce(items, spare, generate_fn, q: queue.Queue, audience="women"):
    fails = 0
    spare = list(spare)
    for item in items:
        while True:
            if STOP.is_set():
                q.put(None)
                return
            _set(item, status="생성중")
            _log(f"[{item['i'] + 1}] 생성 시작: {item['query']}")
            try:
                draft = generate_fn(item["query"], item["category"], item.get("angle", ""), audience,
                                    item.get("story_id"))
                title = ""
                try:
                    title = json.loads((config.DRAFTS_DIR / draft / "post.json").read_text(encoding="utf-8")).get("title", "")
                except Exception:
                    pass
                _remember(item["query"], title)
                if item.get("story_id"):
                    from . import stories
                    stories.remove(item["story_id"])  # 글이 만들어진 사연은 대기열에서 뺀다
                _set(item, status="발행대기", draft=draft, title=title)
                _log(f"[{item['i'] + 1}] 생성 완료: {title or draft}")
                fails = 0
                q.put(item)
                break
            except Exception as e:
                fails += 1
                _set(item, error=str(e)[:160])
                _log(f"[{item['i'] + 1}] ✗ 생성 실패: {str(e)[:120]}")
                if fails >= 3:
                    _set(item, status="실패")
                    _log("연속 3회 실패 — 자동 예약을 멈춥니다")
                    STOP.set()
                    q.put(None)
                    return
                same = [s for s in spare if s.get("source") == item.get("source")] or spare
                if same:
                    nxt = same[0]
                    spare.remove(nxt)
                    _set(item, query=nxt["query"], category=nxt["category"], angle=nxt.get("angle", ""),
                         source=nxt.get("source", ""), story_id=nxt.get("story_id", ""), error="")
                    _log(f"[{item['i'] + 1}] 여분 소재로 교체: {nxt['query']}")
                else:
                    _set(item, status="실패")
                    break
    q.put(None)


def _consume(items, publish_fn, q: queue.Queue, interval, t0):
    last = None
    while True:
        item = q.get()
        if item is None:
            return
        if STOP.is_set():
            _set(item, status="중지됨")
            continue
        slot = slot_for(item["i"], t0, interval, last)
        _set(item, status="예약중", slot=slot.strftime("%m/%d %H:%M"))
        _log(f"[{item['i'] + 1}] 네이버 예약 등록 중 → {slot:%m/%d %H:%M}")
        try:
            slot = publish_fn(item["draft"], slot) or slot  # 잠금 대기로 시각이 밀렸으면 실제 값
            last = slot
            _set(item, slot=slot.strftime("%m/%d %H:%M"))
            _set(item, status="예약완료")
            _log(f"[{item['i'] + 1}] ✓ 예약 완료 {slot:%m/%d %H:%M} — {item['title'][:30]}")
        except Exception as e:
            _set(item, status="예약실패", error=str(e)[:160])
            _log(f"[{item['i'] + 1}] ✗ 예약 실패: {str(e)[:120]} (중복 방지를 위해 재시도하지 않음)")
