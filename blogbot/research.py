"""글 생성 전, 기사 주제를 공신력 있는 출처에서 웹 검색으로 추가 조사한다.

1순위: Kimi(Moonshot) 최신 모델 + 내장 웹 검색($web_search) — 사용자 결정 2026-09-19
2순위: Claude Haiku 4.5 + Anthropic 웹 검색 (Kimi가 실패할 때만)
실패해도 파이프라인이 멈추지 않도록 호출측이 예외를 잡아 원문만으로 진행한다.
"""

from __future__ import annotations

import json
import re
import time

import anthropic
import requests

from . import config, kimi_search, usage_log

DEADLINE_SEC = 240  # 조사 1회 시도 상한 (브리프 3,000~4,000자 작성에 90~150초 소요)

_SYSTEM = """너는 블로그 글 작성을 돕는 사실 조사원이다.
입력된 기사·주제를 읽고, **돈과 자본의 흐름 관점**(왜 이런 일이 벌어졌고 앞으로 어디로 갈 것인가)의 분석 글을 쓸 수 있도록
보강 자료를 웹 검색으로 수집한다. 모든 현상의 원인에는 돈(투자·가격·비용·수익·임금·세금·보조금·예산·금리·환율·가계 지출)이 얽혀 있다고 보고 그 근거를 찾는다.

원칙:
- 공신력 있는 출처만 사용: 정부기관·중앙은행, 국제기구(IMF·OECD·세계은행 등), 공시·실적 자료, 학술지·대학 연구,
  공식 통계, 주요 언론사 본사 보도. 개인 블로그·커뮤니티·위키는 제외.
- 포털 재전송 기사(다음·네이버 뉴스 페이지), 병원·기업 홍보 페이지, 광고성 사이트는
  출처로 쓰지 말 것. 같은 사실을 원 기관·원 논문·원 언론사에서 찾으면 그쪽을 출처로 적는다.
- **최신 자료 우선**: 최근 12개월 이내(분기·월 단위 포함)의 수치와 보도를 먼저 쓴다. 오래된 수치는 추세 비교용으로만 쓰고 연도를 밝힌다.
- 다음 항목을 **8~12개** 수집한다. 검색은 한 번으로 끝내지 말고 주제를 나눠 **여러 번** 수행한다.
  · 사건·이슈의 최신 사실과 계기
  · **돈의 규모와 흐름**: 시장 규모, 투자·매출·실적, 가격·비용 변화, 예산·보조금·세금, 금리·환율 등 (금액·비중·증감률)
  · **누가 얻고 누가 지불하나**: 기업·정부·가계·노동자·소비자별 수익·비용 구조
  · 비교 기준: 이전 시점·다른 나라·다른 업종과의 비교 수치
  · 정책·규제·계약·일정 등 앞으로의 흐름을 정하는 요인과 **공식 전망**(기관명·발표 시점 포함)
  · 반대 견해·돈 말고 작용하는 다른 요인·한계
- 한국 관련 현황(국내 통계·정책·기관 발표·기업 사례·가계 영향)을 **3개 이상** 포함한다 — 한국 독자용 블로그이므로.
- 각 항목은 한두 문장이 아니라 **맥락까지 3~5문장**으로 적는다 (수치의 출처·기준 시점·비교 대상 포함).
- 각 항목에 출처 기관명과 기준 시점을 명시 (예: "한국은행 2026년 3분기 보고서", "IMF 2026년 10월 전망").
- 기사와 모순되는 정보를 발견하면 그것도 명시. 특정 개인·기업의 의도를 단정하는 자료는 '추정'으로 표시.

출력: 한국어 리서치 브리프, **3,000~4,000자**. 항목별로 「사실 — 출처」 형태의 문단. 서론·결론 없이 본론만.
- 검색 과정·재시도·오류에 대한 메타 코멘트는 절대 쓰지 마라. 브리프 내용만 출력한다.
- 웹 검색이 안 되면, 확실히 아는 사실만 담고 불확실한 항목은 빼라. 추측 금지."""


# 조사에 쓸 Kimi 모델 — 최신 순. 최신 모델이 웹 검색을 못 쓰면(서버 측 오류) 다음 모델로.
# (2026-09-19 실측: kimi-k3는 검색 결과를 넘기는 두 번째 요청에서 'tokenization failed'
#  400이 나 사용 불가 → kimi-k2.6 사용. k3가 고쳐지면 자동으로 k3가 쓰인다.)
KIMI_RESEARCH_MODELS = ("kimi-k3", "kimi-k2.6")
_KIMI_SKIP_SEC = 24 * 3600  # 웹 검색이 안 되는 모델은 하루 동안 건너뛴다
_kimi_skip_until: dict = {}


class _KimiSearchUnsupported(RuntimeError):
    pass


class ContentRejected(RuntimeError):
    """Moonshot 콘텐츠 필터가 기사를 거부 (중국 관련 정치·경제 주제 등)."""


# ─────────────────────────────────────────────────────────────
# 1순위: Kimi 독립 검색 API(Web Search Pro) + Kimi 작성
# ─────────────────────────────────────────────────────────────

_QUERY_SYSTEM = """너는 기사 주제를 '돈과 자본의 흐름' 관점으로 조사하기 위한 '검색어 설계자'다.
입력 기사를 읽고 한국어 검색어 6개를 만든다.

규칙:
- 1번: 이 이슈의 최신 사실·계기 (최신 연도를 넣는다)
- 2번: 돈의 규모와 흐름 — 시장 규모·투자·매출·가격·비용 통계 (공식 통계·공시 기관명을 넣으면 좋다)
- 3번: 누가 이익을 얻고 누가 비용을 지불하는가 — 기업 실적·수익 구조·가계 부담
- 4번: 한국 현황 — 국내 통계·정책·예산·세금·기관 발표
- 5번: 앞으로의 전망 — 공식 기관 전망·향후 일정·정책 방향 (최신 연도 포함)
- 6번: 반대 견해·한계·돈 말고 작용하는 다른 요인
- 각 검색어는 8~25자. 따옴표·특수문자 없이.
JSON만 출력: {"queries": ["...", "...", "...", "...", "...", "..."]}"""

_COMPOSE_SYSTEM = _SYSTEM + """

추가 지침:
- 아래 <sources>는 이미 검색해 둔 공신력 자료다. 여기 있는 사실만 사용한다.
- 각 항목 끝에 「— 출처: 기관/매체명(연도)」를 붙인다. 자료에 없는 수치는 절대 쓰지 마라.
- 자료가 서로 어긋나면 그 점을 명시한다."""


def _kimi_chat(messages: list, model: str, max_tokens: int = 16000,
               json_mode: bool = False, timeout: int = 180, purpose: str = None) -> str:
    """도구 없이 Kimi에 한 번 물어본다 (검색은 별도 REST API가 이미 수행)."""
    body = {
        "model": model,
        "messages": messages,
        "thinking": {"type": "disabled"},   # 추론을 끄면 훨씬 빠르고 안정적
        "temperature": 0.6,
        "max_tokens": max_tokens,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    try:
        r = requests.post(f"{config.KIMI_BASE_URL}/chat/completions",
                          headers={"Authorization": f"Bearer {config.KIMI_API_KEY}",
                                   "Content-Type": "application/json"},
                          json=body, timeout=(30, timeout))
    except Exception as e:
        if purpose:
            usage_log.record("kimi", purpose, model, ok=False, note=str(e)[:80])
        raise
    r.encoding = "utf-8"
    if r.status_code != 200 and purpose:
        usage_log.record("kimi", purpose, model, ok=False, note=f"HTTP {r.status_code}")
    if r.status_code == 400 and "content_filter" in r.text:
        raise ContentRejected("Kimi가 민감 주제로 판단해 거부")
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:150]}")
    data = r.json()
    if purpose:
        usage_log.record("kimi", purpose, model, True, *usage_log.kimi_usage(data))
    return (data["choices"][0]["message"].get("content") or "").strip()


def _make_queries(article_text: str, category_ko: str,
                  system: str = None, limit: int = 6) -> list:
    """검색어 6개 만들기 — Haiku 우선(Kimi는 한글 음절이 깨져 '보걳복지부' 같은 검색어를
    만드는 일이 있다), 막히면 Kimi, 그것도 안 되면 기사 첫 문장."""
    user = f"카테고리: {category_ko}\n\n<article>\n{article_text[:3000]}\n</article>"
    for attempt in ("claude", "kimi"):
        try:
            if attempt == "kimi":
                if not config.KIMI_API_KEY:
                    continue
                text = _kimi_chat(
                    [{"role": "system", "content": (system or _QUERY_SYSTEM) + config.time_note(queries=True)},
                     {"role": "user", "content": user}],
                    KIMI_RESEARCH_MODELS[-1], max_tokens=1000, json_mode=True, timeout=90,
                    purpose="queries")
            else:
                client = anthropic.Anthropic(timeout=60.0, max_retries=1)
                try:
                    resp = client.messages.create(
                        model=config.CLAUDE_MODEL, max_tokens=1000,
                        system=(system or _QUERY_SYSTEM) + config.time_note(queries=True),
                        messages=[{"role": "user", "content": user}])
                except Exception as e:
                    usage_log.record("claude", "queries", config.CLAUDE_MODEL, ok=False, note=str(e)[:80])
                    raise
                usage_log.record("claude", "queries", config.CLAUDE_MODEL, True, *usage_log.claude_usage(resp))
                text = "".join(b.text for b in resp.content if b.type == "text")
            m = re.search(r"\{.*\}", text, re.S)
            queries = json.loads(m.group(0))["queries"]
            queries = [q.strip() for q in queries if isinstance(q, str) and q.strip()]
            if queries:
                return queries[:limit]
        except Exception:
            continue
    # 최후: 기사 앞부분 단어로 대충 만든다
    head = re.sub(r"\s+", " ", article_text[:120]).strip()
    return [head[:40]] if head else []


def _gather(queries: list, category_ko: str, min_sources: int, log=None, **kw) -> list:
    """분야별 최신성(time_window)·공신력 도메인(sites)을 걸어 모으고, 너무 적으면 제한 없이 재검색."""
    days = kimi_search.RECENT_DAYS.get(category_ko)
    sites = kimi_search.TRUSTED_SITES.get(category_ko)
    sources = kimi_search.gather(queries, log=log, days=days, sites=sites, **kw)
    if (days or sites) and len(sources) < min_sources:
        if log:
            log(f"  · 기간·도메인 제한 결과 {len(sources)}건뿐 — 제한 없이 재검색")
        sources = kimi_search.gather(queries, log=log, **kw)
    return sources


def _research_with_search_api(article_text: str, category_ko: str, log=None) -> tuple:
    """Web Search Pro로 공신력 자료를 모으고, 그 자료로만 브리프를 쓴다."""
    queries = _make_queries(article_text, category_ko)
    if not queries:
        raise RuntimeError("검색어 생성 실패")
    sources = _gather(queries, category_ko, 3, log=log)
    if len(sources) < 3:
        raise RuntimeError(f"공신력 자료 부족({len(sources)}건)")
    if log:
        log(f"  · 공신력 자료 {len(sources)}건 확보 "
            f"({', '.join(dict.fromkeys(s['site'][:12] for s in sources[:5]))} 등)")

    user = (f"카테고리: {category_ko}\n\n<article>\n{article_text[:5000]}\n</article>\n\n"
            f"<sources>\n{kimi_search.sources_block(sources)}\n</sources>\n\n"
            "위 자료로 리서치 브리프를 작성하라.")
    # 작성은 Kimi 우선, 막히면 Haiku
    for model in (KIMI_RESEARCH_MODELS[-1], config.CLAUDE_MODEL):
        try:
            if model.startswith("kimi"):
                if not config.KIMI_API_KEY:
                    continue
                brief = _kimi_chat(
                    [{"role": "system", "content": _COMPOSE_SYSTEM + config.time_note()},
                     {"role": "user", "content": user}], model, timeout=240, purpose="brief")
            else:
                client = anthropic.Anthropic(timeout=180.0, max_retries=1)
                try:
                    resp = client.messages.create(
                        model=model, max_tokens=8000, system=_COMPOSE_SYSTEM + config.time_note(),
                        messages=[{"role": "user", "content": user}])
                except Exception as e:
                    usage_log.record("claude", "brief", model, ok=False, note=str(e)[:80])
                    raise
                usage_log.record("claude", "brief", model, True, *usage_log.claude_usage(resp))
                brief = "".join(b.text for b in resp.content if b.type == "text").strip()
            brief = _strip_meta(brief)
            ok, why = _brief_ok(brief)
            if ok:
                return brief, f"{model}+search_pro"
            if log:
                log(f"  · {model} 브리프 부실({why}) — 다음 모델로")
        except ContentRejected as e:
            if log:
                log(f"  · {model}: {e} — 다음 모델로")
        except Exception as e:
            if log:
                log(f"  · {model} 작성 실패({str(e)[:70]}) — 다음 모델로")
    raise RuntimeError("브리프 작성 실패")


# ─────────────────────────────────────────────────────────────
# 검색어(주제)만으로 글쓰기 — 기사 없이 자료를 직접 모아 '원문' 역할을 하는 자료집을 만든다
# ─────────────────────────────────────────────────────────────

_TOPIC_QUERY_SYSTEM = """너는 '돈과 자본의 흐름' 관점의 분석 글을 쓰기 위한 '검색어 설계자'다.
사용자가 준 주제 하나로, 서로 다른 각도의 한국어 검색어 8개를 만든다. 모든 현상의 원인에는 돈이 있다고 보고 그 근거를 겨냥한다.

1번: 이슈의 최신 사실·계기 (최신 연도 포함)
2번: 돈의 규모·흐름 — 시장 규모·투자·매출·가격·비용 (공식 통계·공시·국제기구)
3번: 누가 얻고 누가 지불하나 — 기업 실적·수익 구조·정부 예산·가계 부담
4번: 한국 공식 통계 (통계청·한국은행·관련 부처·공공기관)
5번: 정책·규제·세금·보조금·제도 현황
6번: 구체적 사례 — 기업·산업·지역의 최신 실적·투자·계약
7번: 쟁점·반대 견해·돈 말고 작용하는 다른 요인
8번: 앞으로의 전망 — 공식 기관 전망·향후 일정 (최신 연도 포함)
- 각 검색어는 8~25자. 따옴표·특수문자 없이.
JSON만 출력: {"queries": ["...", ...8개]}"""

TOPIC_MIN_SOURCES = 4
TOPIC_MIN_DOSSIER = 5000  # 이보다 얇은 자료집은 웹 검색으로 보강


def topic_dossier(query: str, category_ko: str, log=None) -> tuple:
    with usage_log.session("topic", query):
        return _topic_dossier(query, category_ko, log)


def _topic_dossier(query: str, category_ko: str, log=None) -> tuple:
    """검색어 → (글쓰기 재료가 될 자료집 텍스트, 출처 목록, 조사 방식).

    1순위 Kimi Search Pro로 공신력 자료를 모으고, 부족하면 Haiku 웹 검색 브리프로 대신한다.
    """
    def _note(msg):
        if log:
            try:
                log(msg)
            except Exception:
                pass

    header = f"[주제] {query}\n[카테고리] {category_ko}\n\n"
    if config.KIMI_API_KEY:
        queries = _make_queries(f"주제: {query}", category_ko,
                                system=_TOPIC_QUERY_SYSTEM, limit=8)
        _note(f"  · 검색어 {len(queries)}개 설계: " + " / ".join(q[:14] for q in queries[:4]) + " …")
        sources = _gather(queries, category_ko, TOPIC_MIN_SOURCES, log=_note, per_query=5, max_sources=16)
        if len(sources) >= TOPIC_MIN_SOURCES:
            sites = list(dict.fromkeys(s["site"] for s in sources if s.get("site")))
            _note(f"  · 공신력 자료 {len(sources)}건 확보 ({', '.join(x[:12] for x in sites[:5])} 등)")
            dossier = header + kimi_search.sources_block(sources)
            how = "kimi search_pro"
            # 자료집이 얇으면 장문을 쓰다 사실을 지어내기 쉽다 → Haiku 웹 검색으로 보강
            if len(dossier) < TOPIC_MIN_DOSSIER:
                _note(f"  · 자료집 {len(dossier):,}자로 얇음 — Haiku 웹 검색으로 보강 중...")
                try:
                    extra = _research_with_claude(
                        purpose="boost", article_text=
                        f"다음 주제를 돈과 자본의 흐름 관점(원인·누가 얻고 지불하나·앞으로의 방향)으로 분석하는 장문 글을 쓸 수 있도록 "
                        f"최신 자료를 폭넓게 조사하라. 금액·비중·증감률은 출처와 기준 시점과 함께 정확히.\n주제: {query}", category_ko=category_ko)
                    if extra and len(extra) >= 400:
                        dossier += "\n\n[보강 조사 — 웹 검색 브리프]\n" + extra
                        how += " + haiku web_search"
                        _note(f"  · 보강 완료 (+{len(extra):,}자)")
                except Exception as e:
                    _note(f"  · 보강 실패({str(e)[:60]}) — 있는 자료로만 진행")
            return dossier, sources, how
        _note(f"  · 공신력 자료가 {len(sources)}건뿐 — Haiku 웹 검색으로 보강")
    brief = _research_with_claude(
        f"다음 주제를 돈과 자본의 흐름 관점(원인·누가 얻고 지불하나·앞으로의 방향)으로 분석하는 장문 글을 쓸 수 있도록 "
        f"최신 자료를 폭넓게 조사하라. 금액·비중·증감률은 출처와 기준 시점과 함께 정확히.\n주제: {query}",
        category_ko)
    if not brief or len(brief) < 400:
        raise RuntimeError("주제에 대한 공신력 자료를 충분히 찾지 못했습니다.")
    return header + brief, [], config.CLAUDE_MODEL + " web_search"


def topic_note(query: str, sources: list) -> str:
    """생성 모델에게 줄 추가 지시 — 기사가 아니라 자료집으로 쓴다는 점, 돈의 흐름 분석 관점, 출처 표기 방식."""
    sites = list(dict.fromkeys(s["site"] for s in sources if s.get("site")))[:5]
    src = ", ".join(sites) if sites else "조사 자료의 주요 기관·매체"
    return (
        f"이번 글은 특정 기사가 아니라 검색어 「{query}」로 모은 자료집(<article> 안의 [1], [2]… 자료)을 "
        "바탕으로 쓴다. 자료 여러 개를 엮어 하나의 흐름 있는 분석 글로 재구성하라. "
        "관점은 **돈과 자본의 흐름**이다: 왜 이런 일이 벌어졌는지(원인)를 돈·가격·비용·수익·정책 자금의 움직임으로 설명하고, "
        "누가 얻고 누가 지불하는지, 앞으로 어떤 방향으로 갈지(조건부 시나리오)를 해석한다. "
        "자료에 있는 최신 수치만 쓰고 출처·기준 시점을 문장 안에 밝힌다. 자료에 없는 사실·수치는 쓰지 않는다. "
        f"source_line은 반드시 '참고 자료: {src}' 형식으로 쓴다 (원문 기사·저자 표기 금지)."
    )


def research(article_text: str, category_ko: str, log=None) -> tuple:
    with usage_log.session("article", article_text[:60]):
        return _research(article_text, category_ko, log)


def _research(article_text: str, category_ko: str, log=None) -> tuple:
    """기사 → (리서치 브리프 또는 None, 사용한 모델명).

    Kimi 최신 모델부터 시도하고, 모두 실패하면 Haiku로 조사한다.
    log: 전환 사유를 알리는 콜러블(선택).
    """
    errors = []

    def _note(msg):
        if log:
            try:
                log(msg)
            except Exception:
                pass

    # 1순위: Kimi Web Search Pro(공신력 자료 선별) + 브리프 작성
    if config.KIMI_API_KEY:
        try:
            return _research_with_search_api(article_text, category_ko, log=_note)
        except Exception as e:
            errors.append(f"search_pro: {str(e)[:120]}")
            _note(f"  · 검색 API 조사 실패({str(e)[:70]}) — 모델 내장 검색으로 전환")
    if config.KIMI_API_KEY:
        for km in KIMI_RESEARCH_MODELS:
            if _kimi_skip_until.get(km, 0) > time.time():
                continue
            try:
                brief = _research_with_kimi(article_text, category_ko, km)
                if brief:
                    return brief, km
                errors.append(f"{km}: 빈 결과")
            except ContentRejected as e:
                errors.append(f"{km}: {e}")
                _note(f"  · {km}: {e} — Haiku로 조사합니다")
            except _KimiSearchUnsupported as e:
                _kimi_skip_until[km] = time.time() + _KIMI_SKIP_SEC
                errors.append(f"{km}: {e}")
                _note(f"  · {km} 웹 검색 미지원 — 다음 모델로 (24시간 건너뜀)")
            except Exception as e:  # 잔액 부족·장애 등 — 다음 모델, 그다음 Haiku
                errors.append(f"{km}: {str(e)[:200]}")
                kind = "응답 지연" if "timed out" in str(e) else str(e)[:120]
                _note(f"  · {km} 조사 실패({kind}) — 다음 모델로")
    try:
        return _research_with_claude(article_text, category_ko), config.CLAUDE_MODEL
    except Exception as e:
        errors.append(f"{config.CLAUDE_MODEL}: {str(e)[:80]}")
        raise RuntimeError(" / ".join(errors)) from e


def research_topic(article_text: str, category_ko: str, model: str = None) -> str:
    """이전 호출 방식 호환용 — 브리프만 반환."""
    return research(article_text, category_ko)[0]


def _user_msg(article_text: str, category_ko: str) -> str:
    return f"""카테고리: {category_ko}

아래 기사를 **돈과 자본의 흐름 관점**(원인·누가 얻고 지불하나·앞으로의 방향)으로 분석할 수 있도록, 보강 자료를 최신 순으로 웹 검색해 리서치 브리프를 작성하라.

<article>
{article_text[:6000]}
</article>"""


def _research_with_kimi(article_text: str, category_ko: str, model: str) -> str:
    """(1순위) Kimi 내장 웹 검색. 결과가 부실하면 한 번 더 시도하고, 그래도 안 되면 예외."""
    last = ""
    for _attempt in range(2):
        try:
            brief = _kimi_search_once(article_text, category_ko, model)
        except ContentRejected:
            raise
        ok, why = _brief_ok(brief)
        if ok:
            return brief
        last = why
    raise RuntimeError(f"조사 결과 부실({last})")


_FINISH_NUDGE = ("지금까지의 검색 결과만으로 리서치 브리프를 완성하라. 더 검색하겠다는 말은 하지 말고 "
                 "「사실 — 출처」 항목들만 한국어로 출력하라.")


def _kimi_search_once(article_text: str, category_ko: str, model: str) -> str:
    headers = {
        "Authorization": f"Bearer {config.KIMI_API_KEY}",
        "Content-Type": "application/json",
    }
    messages = [
        {"role": "system", "content": _SYSTEM + config.time_note()},
        {"role": "user", "content": _user_msg(article_text, category_ko)},
    ]
    tools = [{"type": "builtin_function", "function": {"name": "$web_search"}}]
    deadline = time.time() + DEADLINE_SEC
    nudged = False
    for _ in range(10):  # 검색 여러 번 + 최종 작성
        if time.time() > deadline:
            raise TimeoutError(f"조사 시간 상한 {DEADLINE_SEC}초 초과")
        r = requests.post(
            f"{config.KIMI_BASE_URL}/chat/completions",
            headers=headers,
            # 추론(thinking)을 끄면 최종 작성이 125초→35초로 줄고 실패가 사라졌다(실측 2026-09-19).
            # 추론을 끈 경우 Moonshot은 temperature 0.6만 허용한다.
            json={"model": model, "messages": messages, "tools": tools,
                  "thinking": {"type": "disabled"}, "temperature": 0.6,
                  "max_tokens": 16000},
            timeout=(30, 240),
        )
        r.encoding = "utf-8"
        if r.status_code != 200:
            usage_log.record("kimi", "kimi_websearch", model, ok=False, note=f"HTTP {r.status_code}")
        if r.status_code == 400 and "tokenization failed" in r.text:
            raise _KimiSearchUnsupported("웹 검색 결과 처리 불가(서버 측 오류)")
        if r.status_code == 400 and "content_filter" in r.text:
            # Moonshot(중국) 정책상 민감 주제로 판정된 기사 — 재시도해도 같으므로 바로 폴백
            raise ContentRejected("Kimi가 민감 주제로 판단해 거부")
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:120]}")
        data = r.json()
        choice = data["choices"][0]
        msg = choice["message"]
        usage_log.record("kimi", "kimi_websearch", model, True, *usage_log.kimi_usage(data),
                         searches=len(msg.get("tool_calls") or []))
        if choice.get("finish_reason") == "tool_calls" and msg.get("tool_calls"):
            messages.append(msg)
            for tc in msg["tool_calls"]:
                # $web_search는 Moonshot 서버가 검색을 수행하므로 인자를 그대로 돌려준다
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "name": tc["function"]["name"],
                    "content": tc["function"]["arguments"],
                })
            continue
        content = _strip_meta((msg.get("content") or "").strip())
        # "검색하겠습니다"만 말하고 멈췄거나 잘린 경우 → 한 번 재촉해서 마무리시킨다
        if not nudged and (choice.get("finish_reason") == "length" or len(content) < 400):
            nudged = True
            if msg.get("content"):
                messages.append({"role": "assistant", "content": msg["content"]})
            messages.append({"role": "user", "content": _FINISH_NUDGE})
            continue
        return content
    raise RuntimeError("검색 반복 한도 초과")


def _strip_meta(text: str) -> str:
    """앞머리의 '검색한 결과를 바탕으로 작성합니다' 류 예고 문장을 걷어낸다."""
    import re

    for _ in range(3):
        m = re.match(r"^[^\n①1\-*#]{0,200}?(검색|조사|작성|브리프|search)[^\n]{0,120}?(니다|다|\.)[.!]?\s*", text)
        if not m:
            break
        text = text[m.end():].lstrip()
    return text


def _brief_ok(brief: str) -> tuple:
    """브리프 품질 최소 기준: 충분한 길이, 깨진 글자 없음, 한국어 위주."""
    if not brief:
        return False, "빈 결과"
    if len(brief) < 400:
        return False, f"너무 짧음 {len(brief)}자"
    if "\ufffd" in brief:
        return False, "깨진 글자 포함"
    hangul = sum(1 for ch in brief if "\uac00" <= ch <= "\ud7a3")
    if hangul < len(brief) * 0.3:
        return False, "한국어 비율 낮음"
    return True, ""


def _research_with_claude(article_text: str, category_ko: str, model: str = None,
                          purpose: str = "fallback") -> str:
    """(2순위) Haiku + Anthropic 웹 검색. Anthropic 크레딧이 부족하면 CreditExhausted."""
    from . import llm_guard

    if not llm_guard.anthropic_available():
        raise llm_guard.CreditExhausted("Anthropic 크레딧 부족(차단 중) — 조사 생략")
    # 스트리밍 사용: 웹 검색+사고가 포함된 요청은 비스트리밍이면 수 분간 응답이 없다가
    # 끝나 멈춘 것처럼 보인다(실측 539초 vs 스트리밍 118초). timeout은 이벤트 간 무응답 한도.
    client = anthropic.Anthropic(timeout=60.0, max_retries=1)
    used_model = config.enforce_model(model or config.RESEARCH_MODEL)

    user_msg = f"""카테고리: {category_ko}

아래 기사를 **돈과 자본의 흐름 관점**(원인·누가 얻고 지불하나·앞으로의 방향)으로 분석할 수 있도록, 보강 자료를 최신 순으로 웹 검색해 리서치 브리프를 작성하라.

<article>
{article_text[:6000]}
</article>"""

    messages = [{"role": "user", "content": user_msg}]
    response = None
    deadline = time.time() + DEADLINE_SEC
    tot = [0, 0, 0]   # 입력·출력 토큰, 웹 검색 횟수 (pause_turn 재개 포함 합계)
    try:
        return _claude_search_loop(client, used_model, user_msg, messages, deadline, tot)
    except Exception as e:
        usage_log.record("claude", purpose, used_model, False, *tot, note=str(e)[:80])
        tot = None
        raise
    finally:
        if tot is not None:
            usage_log.record("claude", purpose, used_model, True, *tot)


def _claude_search_loop(client, used_model, user_msg, messages, deadline, tot):
    from . import llm_guard

    response = None
    # 서버 도구는 pause_turn으로 중단될 수 있으므로 재개 루프
    for _ in range(4):
        if time.time() > deadline:
            break  # 전체 조사 시간 상한 — 지금까지 모은 결과로 진행
        try:
            with client.messages.stream(
                model=used_model,
                max_tokens=8000,
                # Haiku 4.5는 adaptive thinking·effort 파라미터를 지원하지 않는다(400 오류)
                # 20260209 버전은 결과 필터링용 코드 실행을 수십 회 끼워 넣어 82~467초로
                # 들쭉날쭉했다. 기본 버전은 38~51초로 일정하고 브리프 품질도 동등(실측).
                tools=[{
                    "type": "web_search_20250305",
                    "name": "web_search",
                    "max_uses": 4,
                }],
                system=_SYSTEM + config.time_note(),
                messages=messages,
            ) as stream:
                for _ev in stream:
                    if time.time() > deadline:
                        raise TimeoutError(f"조사 시간 상한 {DEADLINE_SEC}초 초과")
                response = stream.get_final_message()
            for i, v in enumerate(usage_log.claude_usage(response)):
                tot[i] += v
        except anthropic.BadRequestError as e:
            llm_guard.check_and_raise(e)
        if response.stop_reason == "pause_turn":
            messages = [
                {"role": "user", "content": user_msg},
                {"role": "assistant", "content": response.content},
            ]
            continue
        break

    if response is None:
        return None
    # 검색 전에 쓴 "검색하겠습니다" 같은 예고 문장은 버리고, 마지막 검색 결과 이후의 본문만 쓴다
    blocks = list(response.content)
    last_tool = max((i for i, b in enumerate(blocks) if b.type.endswith("tool_result")), default=-1)
    text = "\n".join(b.text for b in blocks[last_tool + 1:] if b.type == "text").strip()
    return text or None
