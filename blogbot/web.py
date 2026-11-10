"""blogbot 웹 GUI — 로컬 대시보드 (Flask).

실행: python -m blogbot web
브라우저에서 글감 조회 → 기사 붙여넣기 → 글 생성 → 미리보기 → 네이버 발행.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

import re
import socket
from html.parser import HTMLParser

import requests
from flask import Flask, Response, jsonify, request, send_from_directory

from . import builder, config, generator, images

app = Flask(__name__)


# Chrome 확장(chrome-extension://…)에서 오는 요청을 허용한다.
# 로컬 개발용이므로 모든 오리진 허용 — 서버가 127.0.0.1에만 바인딩되어 안전.
@app.after_request
def _cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


@app.route("/api/<path:_>", methods=["OPTIONS"])
def _cors_preflight(_):
    return ("", 204)

# ---------------------------------------------------------------- 작업 관리
JOBS: dict = {}  # id -> {type, status, log[], result, created}
_JOBS_LOCK = threading.Lock()


def _new_job(job_type: str) -> str:
    job_id = uuid.uuid4().hex[:8]
    with _JOBS_LOCK:
        JOBS[job_id] = {
            "id": job_id,
            "type": job_type,
            "status": "running",
            "log": [],
            "result": None,
            "created": time.time(),
        }
    return job_id


def _log(job_id: str, msg: str):
    with _JOBS_LOCK:
        JOBS[job_id]["log"].append(msg)


def _finish(job_id: str, status: str, result=None):
    with _JOBS_LOCK:
        JOBS[job_id]["status"] = status
        JOBS[job_id]["result"] = result


# ---------------------------------------------------------------- API
class _ArticleExtractor(HTMLParser):
    """<article>, <main>, 또는 <p> 태그의 텍스트만 뽑는 간단한 파서."""

    def __init__(self):
        super().__init__()
        self.depth_article = 0
        self.in_p = False
        self.skip_depth = 0  # script/style 등 무시
        self.paragraphs: list = []
        self._buf: list = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "svg"):
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag in ("article", "main"):
            self.depth_article += 1
        if tag == "p" and self.depth_article > 0:
            self.in_p = True

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg"):
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if self.skip_depth:
            return
        if tag == "p" and self.in_p:
            text = "".join(self._buf).strip()
            if len(text) > 20:
                self.paragraphs.append(text)
            self._buf = []
            self.in_p = False
        if tag in ("article", "main"):
            self.depth_article = max(0, self.depth_article - 1)

    def handle_data(self, data):
        if self.skip_depth or not self.in_p:
            return
        self._buf.append(data)


# 서버 자동 fetch가 무의미하고(강력 봇 차단) 반복 시도가 공인 IP를 봇으로
# 플래그시키는 사이트들 — 서버 요청을 아예 하지 않고 즉시 빈 결과 반환.
# (이 사이트들은 Chrome 확장 / 모바일 공유 / 붙여넣기 흐름으로만 처리)
_FETCH_BLOCKLIST = (
    "nytimes.com", "wsj.com", "washingtonpost.com",
    "ft.com", "bloomberg.com", "economist.com",
)


def _is_blocklisted(url: str) -> bool:
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in _FETCH_BLOCKLIST)


def _fetch_article_text(url: str) -> tuple[str, str]:
    """URL에서 기사 본문을 뽑아 (제목, 본문) 반환. 실패하면 예외.

    1단계: requests로 직접 시도 (빠름)
    2단계: 짧으면 저장된 로그인 프로필로 headless 브라우저 재시도
    단, NYT 등 강력 차단 사이트(_FETCH_BLOCKLIST)는 서버 요청을 아예 하지 않는다
    (성공 못 하면서 공인 IP만 봇으로 플래그되므로).
    """
    # 강력 차단 사이트는 서버 fetch를 시도조차 하지 않음 → 빈 결과 (호출측이 다른 흐름)
    if _is_blocklisted(url):
        return "", ""

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        )
    }

    def _parse(html: str) -> tuple[str, str]:
        title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
        title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else ""
        ex = _ArticleExtractor()
        ex.feed(html)
        return title, "\n\n".join(ex.paragraphs).strip()

    # 1단계: requests 직접 요청 (빠름)
    direct_err = None
    try:
        r = requests.get(url, headers=headers, timeout=20, allow_redirects=True)
        r.raise_for_status()
        title, body = _parse(r.text)
        # 충분히 길면 그대로 사용
        if len(body) >= 300:
            return title, body
        # 짧으면(페이월 프리뷰 등) 로그인 세션 브라우저로 재시도
    except Exception as e:
        direct_err = e
        title, body = "", ""

    # 2단계: 저장된 뉴스 로그인 프로필로 headless 브라우저 fetch (NYT 등 우회)
    from . import news
    try:
        html = news.fetch_html(url)
        t2, b2 = _parse(html)
        # 더 긴 쪽을 채택
        if len(b2) >= len(body):
            title, body = (t2 or title), b2
    except Exception as pw_err:
        if not body:  # 1단계도 실패했으면 에러
            raise RuntimeError(
                f"직접 요청 실패: {direct_err} / 로그인 브라우저 재시도 실패: {pw_err}"
            ) from pw_err

    return title, body


@app.post("/api/fetch-article")
def api_fetch_article():
    data = request.get_json(force=True)
    url = (data.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return jsonify({"error": "URL이 올바르지 않습니다."}), 400
    try:
        title, body = _fetch_article_text(url)
    except Exception as e:
        return jsonify({"error": f"본문을 가져오지 못했습니다: {e}"}), 502
    # 짧아도 에러로 막지 않고 paywalled 플래그와 함께 반환 (호출측이 요약과 합쳐 생성)
    from . import news
    return jsonify(
        {
            "title": title,
            "body": body,
            "paywalled": len(body) < 300,
            "has_login": news.has_session(),
        }
    )


@app.post("/api/news-login")
def api_news_login():
    """뉴스 사이트 로그인 창을 연다 (Mac에서 한 번, 세션 저장)."""
    job_id = _new_job("news-login")

    def run():
        try:
            from . import news
            _log(job_id, "브라우저가 열립니다. NYT 등 뉴스 사이트에 로그인하세요.")
            _log(job_id, "로그인 후 브라우저 창을 닫으면 세션이 저장됩니다.")
            news.login()
            if news.has_session():
                _log(job_id, "✓ 로그인 세션 저장 완료")
                _finish(job_id, "done")
            else:
                _log(job_id, "⚠ 세션이 감지되지 않았습니다. 다시 시도해 주세요.")
                _finish(job_id, "error")
        except Exception as e:
            _log(job_id, f"✗ 실패: {e}")
            _finish(job_id, "error")

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"job": job_id})


@app.get("/api/topics")
def api_topics():
    import feedparser

    category = request.args.get("category", "life")
    limit = int(request.args.get("limit", 12))
    items = []
    for feed_url in config.NYT_FEEDS.get(category, []):
        feed = feedparser.parse(feed_url)
        for e in feed.entries:
            if len(items) >= limit:
                break
            items.append(
                {
                    "title": e.title,
                    "summary": getattr(e, "summary", "")[:300],
                    "link": e.link,
                }
            )
    return jsonify(items)


def _run_generation(job_id: str, text: str, category: str, note: str,
                    used_model: str, do_research: bool, source_label: str = "",
                    image_provider: str = None, force_proofread: bool = False,
                    audience: str = "women", creative: bool = False, seo_hint: str = "",
                    gen_mode: str = "", excerpts: list = None, source_ref: str = ""):
    """조사 → 글 생성 → 이미지 선별 → 초안 저장. (스레드에서 실행)

    Anthropic 크레딧이 부족하면 조사·비전 선별·교정은 생략하고,
    생성은 Kimi(FALLBACK_MODEL)로 자동 전환해 작업이 멈추지 않게 한다.
    creative=True: 사연 각색 글 — 이야기를 새로 지으므로 자료 대조 사실 검증은 하지 않는다.
    gen_mode='pann': 판 인기글 — 발췌문(excerpts)을 인용 블록으로 넣고 사회적 관점으로 해설한다. source_ref는 출처 주소.
    """
    from . import llm_guard

    try:
        if not llm_guard.anthropic_available() and config.ANTHROPIC_API_KEY:
            _log(job_id, "⚠ Anthropic 크레딧 부족 감지 — Claude 단계는 생략하고 Kimi로 진행합니다")

        research_brief = None
        if do_research:
            _log(job_id, "🔎 공신력 자료 조사 중... (Kimi Search Pro, 보통 1~2분)")
            try:
                from . import research as research_mod
                research_brief, research_model = research_mod.research(
                    text, config.CATEGORIES.get(category, category),
                    log=lambda m: _log(job_id, m),
                )
                if research_brief:
                    _log(job_id, f"✓ 조사 완료 ({research_model} · {len(research_brief)}자 브리프)")
                else:
                    _log(job_id, "⚠ 조사 결과 없음 — 원문만으로 진행")
            except llm_guard.CreditExhausted:
                _log(job_id, "⚠ 조사 생략 (Anthropic 크레딧 부족) — 원문만으로 진행")
            except Exception as e:
                _log(job_id, f"⚠ 조사 실패({str(e)[:80]}) — 원문만으로 진행")

        prefix = f"{source_label} · " if source_label else ""
        _log(job_id, f"✍ {prefix}글 생성 중... (모델: {used_model})")
        post = generator.generate_post(
            text, category, note, model=used_model, research_brief=research_brief, audience=audience,
            mode=gen_mode or ("story" if creative else "feature"),
        )
        if post.get("expand_skipped"):
            _log(job_id, f"  · {post['expand_skipped']}")
        if post.get("expanded_from"):
            _log(job_id, f"  · 분량 확장: {post['expanded_from']}자 → {generator.body_chars(post)}자")
        if post.get("fallback_from"):
            _log(job_id, f"↪ {post['fallback_reason']} → {post['model']}로 자동 전환해 생성 완료")

        # Kimi 계열은 긴 프롬프트에서 한글 음절이 깨질 수 있어 교정 패스를 거친다.
        # (자동 전환된 경우도 포함 — 실제 사용된 모델 기준)
        if config.is_kimi_model(post.get("model", used_model)) or force_proofread:
            _log(job_id, "✏️ 한글 오탈자 교정 중...")
            try:
                from . import proofread as proofread_mod
                post, n_fixed = proofread_mod.proofread_post(post)
                _log(job_id, f"✓ 교정 완료 ({n_fixed}개 필드 수정)" if n_fixed
                     else "✓ 교정 완료 (수정 없음)")
            except llm_guard.CreditExhausted:
                _log(job_id, "⚠ 교정 생략 (Anthropic 크레딧 부족) — 발행 전 오타를 직접 확인하세요")
            except Exception as e:
                _log(job_id, f"⚠ 교정 건너뜀 ({str(e)[:60]})")

        # 판 인기글: 교정이 인용문을 고쳐 쓰지 않았는지 확인해 원본 발췌문으로 되돌린다
        if gen_mode == "pann":
            from . import stories as stories_mod
            fixed, dropped, kept = stories_mod.restore_excerpts(post, excerpts or [])
            _log(job_id, f"  ✓ 발췌문 {kept}/{len(excerpts or [])}개 본문 인용 (원문 복구 {fixed}곳 · 지어낸 인용 삭제 {dropped}곳)")
            post["source_line"] = (config.PANN_NOTICE + (f" 출처: {source_ref}" if source_ref else "")).strip()

        # 자료 대조 사실 검증 — 자료에 없는 연도·수치·장소·기간을 고치거나 지운다 (각색 이야기는 제외)
        if creative:
            _log(job_id, "  · 각색 이야기라 자료 대조 사실 검증은 건너뜁니다")
        else:
            _log(job_id, "🔍 사실 검증 중... (글을 원문·조사 자료와 대조)")
            try:
                from . import factcheck
                post, fixes = factcheck.check(post, text, research_brief)
                if fixes:
                    _log(job_id, f"  ✓ 자료에 없는 사실 {len(fixes)}곳 수정")
                    for f in fixes[:5]:
                        _log(job_id, f"    · {f['problem'][:70]}")
                else:
                    _log(job_id, "  ✓ 자료와 어긋나는 사실 없음")
            except Exception as e:
                _log(job_id, f"  ⚠ 사실 검증 건너뜀({str(e)[:80]}) — 발행 전 수치·연도를 확인하세요")
        # 검색 노출·체류시간 구성: 제목(앵커 + 후킹) · 30초 요약 · 표 · 질문 · 모바일 문단 (각색 이야기는 문단만)
        if creative:
            from . import seo
            seo.split_paragraphs(post)
        else:
            _log(job_id, "🔧 검색용 구성 정리 중... (제목·30초 요약·표·마무리 질문)")
            try:
                from . import seo
                post = seo.finalize(post, hint=seo_hint, log=lambda m: _log(job_id, m))
            except Exception as e:
                _log(job_id, f"  ⚠ 검색용 구성 건너뜀({str(e)[:80]})")
        _log(job_id, f"✓ 제목: {post['title']}")
        draft_dir = builder.make_draft_dir(post)
        provider = config.resolve_image_provider(image_provider)
        if config.HISTORY_USE_WIKIMEDIA and post.get("category") == config.CATEGORIES.get("history"):
            _log(job_id, "🏛 역사 사진 찾는 중... (위키미디어 실제 자료 우선, 부족하면 "
                         + ("AI 생성)" if provider == "openai" else "무료 사진)"))
        elif provider == "openai":
            _log(job_id, "🎨 AI 사진 생성 중... (OpenAI, 보통 20~40초)")
        elif provider == "cloudflare":
            _log(job_id, "🎨 AI 사진 생성 중... (Cloudflare FLUX 무료, 보통 20~40초)")
        elif llm_guard.anthropic_available():
            _log(job_id, "🖼 이미지 검색·선별 중... (비전 모델이 문단과 대조)")
        else:
            _log(job_id, "🖼 이미지 검색 중... (크레딧 부족으로 비전 선별 생략 — 첫 결과 사용)")
        metas = images.fetch_images_for_post(
            post, draft_dir, log=lambda m: _log(job_id, m), provider=provider
        )
        _log(job_id, f"✓ 이미지 {len(metas)}장 저장")
        builder.build_draft(post, metas, draft_dir)
        _log(job_id, f"✓ 초안 저장: {draft_dir.name}")
        _finish(job_id, "done", {"draft": draft_dir.name, "title": post["title"]})
    except llm_guard.CreditExhausted as e:
        _log(job_id, f"✗ {e}")
        _finish(job_id, "error")
    except Exception as e:
        _log(job_id, f"✗ 실패: {e}")
        _finish(job_id, "error")


@app.post("/api/generate")
def api_generate():
    data = request.get_json(force=True)
    text = (data.get("text") or "").strip()
    category = data.get("category", "life")
    note = (data.get("note") or "").strip()
    model = config.enforce_model((data.get("model") or "").strip()) if data.get("model") else None  # 상위 Claude 요청은 Haiku로
    do_research = bool(data.get("research", True))
    valid_models = {m[0] for m in config.AVAILABLE_MODELS}
    if model and model not in valid_models:
        return jsonify({"error": f"지원하지 않는 모델: {model}"}), 400
    if len(text) < 200:
        return jsonify({"error": "기사 텍스트가 너무 짧습니다 (200자 이상)."}), 400

    used_model = config.enforce_model(model or config.MODEL)
    job_id = _new_job("generate")
    threading.Thread(
        target=_run_generation,
        args=(job_id, text, category, note, used_model, do_research, "",
              data.get("images")),
        daemon=True,
    ).start()
    return jsonify({"job": job_id})


def _run_topic_generation(job_id: str, query: str, category: str, note: str, used_model: str,
                          image_provider: str = None, force_proofread: bool = False,
                          audience: str = "women", story: dict = None):
    """검색어 → 자료 수집(Kimi Search Pro) → 기존 글쓰기 파이프라인. (스레드에서 실행)

    story: 독자 사연의 익명 요약({"summary": ...}). 있으면 자료 조사 없이 사연의 뼈대로
    인물·장면을 새로 지은 '재미있는 각색 이야기'를 쓴다(2026-10-31 사용자 지정, 해설 방식 폐지)."""
    from . import research as research_mod
    from . import stories as stories_mod

    if story and stories_mod.is_commentary(story):
        ex = story.get("excerpts") or []
        _log(job_id, f"📰 판 인기글을 발췌({len(ex)}개)하고 사회적 관점으로 해설합니다 (자료 조사·각색 없음)")
        full_note = stories_mod.commentary_note(query, bool(ex))
        if note:
            full_note = f"{full_note}\n{note}".strip()
        _run_generation(job_id, stories_mod.commentary_block(story), category, full_note, used_model,
                        do_research=False, source_label=f"판 인기글 해설 · 「{query[:20]}」",
                        image_provider=image_provider, force_proofread=force_proofread,
                        audience=audience, creative=True, gen_mode="pann", excerpts=ex,
                        source_ref=story.get("ref", ""))
        return
    if story:
        _log(job_id, "🎭 사연을 각색한 이야기로 씁니다 (자료 조사·사실 검증 없음)")
        plan = stories_mod.adapt_plan(story, audience)
        _log(job_id, "  ✓ 각색 설계(인물·장면·반전) 완성" if plan else "  ⚠ 각색 설계 실패 — 설계 없이 씁니다")
        full_note = stories_mod.story_note(query)
        if note:
            full_note = f"{full_note}\n{note}".strip()
        _run_generation(job_id, stories_mod.story_block(story) + plan, category, full_note, used_model,
                        do_research=False, source_label=f"사연 각색 · 「{query[:20]}」",
                        image_provider=image_provider, force_proofread=force_proofread,
                        audience=audience, creative=True)
        return
    try:
        _log(job_id, f"🔎 검색어 「{query}」로 자료 수집 중... (Kimi Search Pro, 보통 30초~1분)")
        dossier, sources, how = research_mod.topic_dossier(
            query, config.CATEGORIES.get(category, category), log=lambda m: _log(job_id, m)
        )
        _log(job_id, f"✓ 자료집 완성 ({how} · {len(dossier)}자)")
    except Exception as e:
        _log(job_id, f"✗ 자료 수집 실패: {str(e)[:200]}")
        _finish(job_id, "error")
        return
    topic_instr = research_mod.topic_note(query, sources)
    full_note = f"{topic_instr}\n{note}".strip() if note else topic_instr
    # 자료집이 이미 '조사 결과'이므로 추가 조사는 하지 않는다
    _run_generation(job_id, dossier, category, full_note, used_model,
                    do_research=False, source_label=f"검색어 「{query[:20]}」",
                    image_provider=image_provider, force_proofread=force_proofread, audience=audience,
                    seo_hint=query)
    # 수치 대조용으로 수집한 자료 목록을 초안 폴더에 남긴다 (미리보기에서만 보임)
    draft = ((JOBS.get(job_id) or {}).get("result") or {}).get("draft")
    if draft and sources:
        try:
            (config.DRAFTS_DIR / draft / "sources.json").write_text(json.dumps(
                {"query": query, "sources": [
                    {k: s_.get(k, "") for k in ("title", "site", "url", "date", "authority")}
                    for s_ in sources]}, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            pass


@app.post("/api/generate-topic")
def api_generate_topic():
    """기사 없이 검색어(주제)만으로 글을 쓴다."""
    data = request.get_json(force=True)
    query = (data.get("query") or "").strip()
    category = data.get("category", "life")
    note = (data.get("note") or "").strip()
    model = config.enforce_model((data.get("model") or "").strip()) if data.get("model") else None
    if len(query) < 2:
        return jsonify({"error": "검색어를 2자 이상 입력하세요."}), 400
    if len(query) > 200:
        return jsonify({"error": "검색어가 너무 깁니다 (200자 이하)."}), 400
    used_model = config.enforce_model(model or config.MODEL)
    job_id = _new_job("generate")
    threading.Thread(
        target=_run_topic_generation,
        args=(job_id, query, category, note, used_model, data.get("images")),
        daemon=True,
    ).start()
    return jsonify({"job": job_id})


def _analyze_story(data: dict):
    """요청의 사연 원문을 익명 요약으로. (info, error 응답) 중 하나를 돌려준다."""
    from . import stories as stories_mod

    try:
        return stories_mod.analyze(data.get("text") or "", data.get("audience") or "women"), None
    except stories_mod.Unsuitable as e:
        return None, (jsonify({"error": f"블로그 소재로 쓰지 않는 사연입니다: {e}"}), 400)
    except ValueError as e:
        return None, (jsonify({"error": str(e)}), 400)
    except Exception as e:
        return None, (jsonify({"error": f"사연 요약 실패: {str(e)[:120]}"}), 500)


def _run_story_job(job_id: str, info: dict, category: str, note: str, used_model: str, image_provider):
    _run_topic_generation(job_id, info["query"], category, note, used_model, image_provider, story=info)


@app.post("/api/generate-story")
def api_generate_story():
    """사용자가 붙여넣은 사연 → 익명 요약 → 자료 조사 → 글 생성."""
    data = request.get_json(force=True)
    info, err = _analyze_story(data)
    if err:
        return err
    category = data.get("category") if data.get("category") in config.CATEGORIES else info["category"]
    note = (data.get("note") or "").strip()
    if info.get("angle"):
        note = f"관점: {info['angle']} {note}".strip()
    model = config.enforce_model((data.get("model") or "").strip()) if data.get("model") else None
    used_model = config.enforce_model(model or config.MODEL)
    job_id = _new_job("generate")
    _log(job_id, f"📝 사연을 익명으로 요약했습니다 → 주제: {info['query']}")
    threading.Thread(target=_run_story_job,
                     args=(job_id, info, category, note, used_model, data.get("images")),
                     daemon=True).start()
    return jsonify({"job": job_id, "query": info["query"]})


@app.get("/api/stories")
def api_stories_list():
    from . import stories as stories_mod

    return jsonify(stories_mod.all_items())


@app.post("/api/stories")
def api_stories_add():
    """사연을 익명 요약해 자동 예약 대기열에 넣는다 (원문은 저장하지 않음)."""
    from . import stories as stories_mod

    data = request.get_json(force=True)
    info, err = _analyze_story(data)
    if err:
        return err
    if data.get("category") in config.CATEGORIES:
        info["category"] = data["category"]
    return jsonify(stories_mod.add(info, data.get("ref") or ""))


@app.post("/api/pann/sync")
def api_pann_sync():
    """구글 드라이브의 네이트 판 인기글 파일에서 새 글을 가져와 사연 대기열에 넣는다."""
    from . import pann_feed

    try:
        return jsonify(pann_feed.sync())
    except Exception as e:
        return jsonify({"error": f"판 인기글 가져오기 실패: {str(e)[:120]}"}), 500


@app.get("/api/pann/status")
def api_pann_status():
    from . import pann_feed

    return jsonify(pann_feed.status())


@app.delete("/api/stories/<story_id>")
def api_stories_delete(story_id):
    from . import stories as stories_mod

    return jsonify({"ok": stories_mod.remove(story_id)})


@app.post("/api/from-browser")
def api_from_browser():
    """Chrome 확장에서 활성 탭의 본문을 받아 글을 생성한다.

    확장이 브라우저 로그인 세션을 그대로 활용해 본문을 뽑으므로,
    서버 IP가 봇으로 차단당하는 NYT 같은 사이트에서도 동작한다.
    """
    data = request.get_json(force=True)
    text = (data.get("body") or "").strip()
    url = (data.get("url") or "").strip()
    src_title = (data.get("title") or "").strip()
    category = data.get("category", "life")
    model = config.enforce_model((data.get("model") or "").strip()) if data.get("model") else None  # 상위 Claude 요청은 Haiku로
    note = (data.get("note") or "").strip()
    do_research = bool(data.get("research", True))

    if model:
        valid = {m[0] for m in config.AVAILABLE_MODELS}
        if model not in valid:
            return jsonify({"error": f"지원하지 않는 모델: {model}"}), 400
    if len(text) < 200:
        return jsonify({"error": "본문이 너무 짧습니다 (200자 이상)."}), 400

    used_model = config.enforce_model(model or config.MODEL)
    job_id = _new_job("generate")

    # 원문 출처가 어디인지 프롬프트에 살짝 얹어 재구성 시 자연스럽게 언급되게 함
    context_note = note
    if src_title or url:
        prefix = f"[원문 참고: {src_title}{f' — {url}' if url else ''}]"
        context_note = f"{prefix}\n{note}" if note else prefix

    threading.Thread(
        target=_run_generation,
        args=(job_id, text, category, context_note, used_model, do_research,
              "브라우저 확장에서 받음", data.get("images")),
        daemon=True,
    ).start()
    return jsonify({"job": job_id})


@app.get("/api/jobs")
def api_jobs():
    """모든 작업 요약 (실행 현황 탭용). 최신순."""
    with _JOBS_LOCK:
        items = sorted(JOBS.values(), key=lambda j: j["created"], reverse=True)
        # 로그 전체를 보내지 않고 요약만; 상세는 개별 조회로
        return jsonify(
            [
                {
                    "id": j["id"],
                    "type": j["type"],
                    "status": j["status"],
                    "created": j["created"],
                    "log_lines": len(j["log"]),
                    "last_log": (j["log"][-1] if j["log"] else ""),
                    "result": j.get("result"),
                }
                for j in items
            ]
        )


@app.get("/api/jobs/<job_id>")
def api_job(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "no such job"}), 404
    return jsonify(job)


@app.get("/api/drafts")
def api_drafts():
    result = []
    if config.DRAFTS_DIR.exists():
        for d in sorted(config.DRAFTS_DIR.iterdir(), reverse=True):
            pj = d / "post.json"
            if not pj.exists():
                continue
            try:
                meta = json.loads(pj.read_text(encoding="utf-8"))
            except Exception:
                continue
            result.append(
                {
                    "name": d.name,
                    "title": meta.get("title", d.name),
                    "category": meta.get("category", ""),
                    "images": len(meta.get("image_files", [])),
                    "tags": meta.get("tags", []),
                }
            )
    return jsonify(result)


@app.get("/api/draft/<name>")
def api_draft(name):
    """모바일 발행 화면용 초안 내용 (제목·본문 텍스트·태그·이미지 URL)."""
    if "/" in name or ".." in name:
        return jsonify({"error": "bad name"}), 400
    pj = config.DRAFTS_DIR / name / "post.json"
    if not pj.exists():
        return jsonify({"error": "초안을 찾을 수 없습니다."}), 404
    post = json.loads(pj.read_text(encoding="utf-8"))

    imgs_by_section: dict = {}
    for m in post.get("image_files", []):
        imgs_by_section.setdefault(m["after_section"], []).append(m)

    parts = []
    for i, sec in enumerate(post.get("sections", [])):
        if sec.get("heading", "").strip():
            parts.append(sec["heading"])
        parts.append(sec.get("body_md", ""))
        for m in imgs_by_section.get(i, []):
            parts.append(f"[여기에 이미지: {m.get('caption','')}]")
    body = "\n\n".join(p for p in parts if p.strip())
    src = (post.get("source_line") or "").strip()
    if src:
        body += "\n\n" + src

    from pathlib import Path as _P
    images = [
        {
            "url": f"/preview/{name}/images/{_P(m['file']).name}",
            "caption": m.get("caption", ""),
        }
        for m in post.get("image_files", [])
    ]
    return jsonify(
        {
            "title": post.get("title", ""),
            "body": body,
            "tags": post.get("tags", []),
            "images": images,
            "naver_blog_id": config.NAVER_BLOG_ID,
        }
    )


@app.get("/preview/<name>/")
@app.get("/preview/<name>/<path:filename>")
def preview(name, filename="preview.html"):
    # 디렉토리 탈출 방지: drafts 바로 아래 폴더명만 허용
    if "/" in name or ".." in name:
        return "bad name", 400
    if filename == "preview.html":
        path = config.DRAFTS_DIR / name / "preview.html"
        if not path.exists():
            return "not found", 404
        html = path.read_text(encoding="utf-8")
        # 기존 초안까지 모두 적용되도록 제공 시점에 '네이버 바로 발행' 바를 끼워 넣는다
        html = html.replace("<body>\n", "<body>\n" + _PUBLISH_BAR.replace("__DRAFT__", name)
                            + _sources_panel(config.DRAFTS_DIR / name), 1)
        return Response(html, mimetype="text/html")
    return send_from_directory(config.DRAFTS_DIR / name, filename)


def _sources_panel(draft_dir) -> str:
    """수집 자료 링크·사실 검증 내역을 접이식으로 보여준다 (발행 본문에는 포함되지 않음)."""
    return _factcheck_panel(draft_dir) + _collected_panel(draft_dir)


def _factcheck_panel(draft_dir) -> str:
    from html import escape
    try:
        fixes = json.loads((draft_dir / "post.json").read_text(encoding="utf-8")).get("factcheck") or []
    except Exception:
        return ""
    if not fixes:
        return ""
    items = "".join(
        f'<li><b>{escape(f.get("problem", ""))}</b><br><s style="color:#b00">{escape(f["quote"])}</s>'
        f'<br>→ {escape(f["fix"]) if f.get("fix") else "<i>삭제</i>"}</li>' for f in fixes)
    return (f'<details style="margin:0 0 12px;font-size:.88em;background:#fff8e6;border-radius:8px;padding:10px 14px">'
            f'<summary style="cursor:pointer">🔍 사실 검증에서 자료와 맞지 않아 고친 문장 {len(fixes)}곳 '
            f'(블로그에는 올라가지 않음)</summary><ol style="margin:8px 0 0;padding-left:20px;line-height:1.6">'
            f'{items}</ol></details>')


def _collected_panel(draft_dir) -> str:
    from html import escape
    try:
        d = json.loads((draft_dir / "sources.json").read_text(encoding="utf-8"))
    except Exception:
        return ""
    items = "".join(
        f'<li><a href="{escape(x["url"])}" target="_blank" rel="noopener">{escape(x["title"] or x["url"])}</a>'
        f' <span style="color:#999">— {escape(x["site"])}</span></li>'
        for x in d.get("sources", []) if x.get("url"))
    return (f'<details style="margin:0 0 24px;font-size:.88em;background:#f7f7f7;border-radius:8px;padding:10px 14px">'
            f'<summary style="cursor:pointer">🔎 검색어 「{escape(d.get("query",""))}」로 수집한 자료 '
            f'{len(d.get("sources", []))}건 — 수치 확인용 (블로그에는 올라가지 않음)</summary>'
            f'<ol style="margin:8px 0 0;padding-left:20px;line-height:1.7">{items}</ol></details>')


@app.get("/api/published/<name>")
def api_published(name):
    if "/" in name or ".." in name:
        return jsonify({"error": "bad name"}), 400
    from . import naver
    return jsonify({"published": naver.published_info(config.DRAFTS_DIR / name)})


_EDIT_HTML = (Path(__file__).parent / "edit_ui.html").read_text(encoding="utf-8")

_PUBLISH_BAR = """<div id="nvbar" style="position:sticky;top:0;z-index:50;background:#fff;
  padding:12px 0 14px;margin:-40px 0 24px;border-bottom:1px solid #eee;text-align:center">
  <div style="display:flex;gap:6px;justify-content:center;flex-wrap:wrap;max-width:640px;margin:0 auto 10px">
    <select id="nvCat" title="네이버 블로그 카테고리" style="flex:1;min-width:150px;padding:9px;border-radius:8px;border:1px solid #ddd;font-size:.9em">
      <option value="">📂 카테고리 불러오는 중...</option></select>
    <select id="nvTopic" title="네이버 주제" style="flex:1;min-width:150px;padding:9px;border-radius:8px;border:1px solid #ddd;font-size:.9em">
      <option value="">🏷 주제</option></select>
  </div>
  <div style="display:flex;gap:8px;justify-content:center;max-width:640px;margin:0 auto">
    <a href="/edit/__DRAFT__/" style="padding:15px 18px;border-radius:10px;border:1px solid #ccc;
      color:#333;text-decoration:none;font-weight:700;white-space:nowrap">✏️ 수정</a>
    <button id="nvGo" type="button" style="font-size:1.1em;padding:15px 28px;border:none;border-radius:10px;
      background:#03c75a;color:#fff;cursor:pointer;flex:1;max-width:420px;font-weight:700">
      🚀 네이버 블로그에 바로 발행</button>
  </div>
  <div id="nvMsg" style="font-size:.88em;color:#666;margin-top:8px">
    누르면 서식·사진·캡션·태그와 선택한 카테고리·주제로 네이버에 발행됩니다 (약 1분)</div>
  <pre id="nvLog" style="display:none;text-align:left;font-size:.8em;background:#f6f6f6;border-radius:8px;
    padding:10px;margin:10px auto 0;max-width:640px;white-space:pre-wrap;max-height:180px;overflow:auto"></pre>
</div>
<script>
(function(){
  var DRAFT = "__DRAFT__";
  var btn = document.getElementById('nvGo'), msg = document.getElementById('nvMsg'),
      log = document.getElementById('nvLog');
  var catSel = document.getElementById('nvCat'), topicSel = document.getElementById('nvTopic');
  function esc(t){ return String(t).replace(/[&<>"]/g, function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); }
  fetch('/api/naver-options?draft=' + encodeURIComponent(DRAFT)).then(function(r){ return r.json(); }).then(function(o){
    var sug = o.suggest || {};
    var cats = o.categories || [];
    catSel.innerHTML = '<option value="">📂 카테고리: 자동(분야별 기본값)</option>' + cats.map(function(c){
      return '<option value="' + c.id + '"' + (c.id === sug.category_id ? ' selected' : '') + '>' +
        (c.parent ? '\u00a0\u00a0└ ' : '') + esc(c.name) + '</option>'; }).join('') +
      (cats.length ? '' : '<option disabled>목록 없음 — 대시보드 ③에서 불러오기</option>');
    var html = '<option value="">🏷 주제: 자동(분야별 기본값)</option>';
    (o.topics || []).forEach(function(gt){ var g = gt[0];
      html += '<optgroup label="' + esc(g) + '">' + gt[1].map(function(t){
        return '<option' + (t === sug.topic ? ' selected' : '') + '>' + esc(t) + '</option>'; }).join('') + '</optgroup>';
    });
    html += '<option' + (sug.topic === o.no_topic ? ' selected' : '') + '>' + esc(o.no_topic || '주제 선택 안 함') + '</option>';
    topicSel.innerHTML = html;
  });
  function done(url){
    btn.textContent = '✓ 발행됨 · 블로그 글 보기';
    btn.style.background = '#1a73e8'; btn.disabled = false;
    btn.onclick = function(){ window.open(url, 'blogbotBlog'); };
    msg.innerHTML = '<a href="' + url + '" target="blogbotBlog">' + url + '</a> · ' +
      '<a href="#" id="nvAgain" style="color:#999">다시 발행</a>';
    document.getElementById('nvAgain').onclick = function(e){ e.preventDefault();
      if (confirm('이미 발행한 글입니다. 같은 글을 한 번 더 발행할까요?')) start(true); };
  }
  function poll(job){
    fetch('/api/jobs/' + job).then(function(r){ return r.json(); }).then(function(j){
      log.style.display = 'block'; log.textContent = (j.log || []).join('\\n'); log.scrollTop = 1e9;
      if (j.status === 'running') return setTimeout(function(){ poll(job); }, 1500);
      var url = j.result && j.result.url;
      if (j.status === 'done' && url) return done(url);
      btn.disabled = false; btn.textContent = '🚀 다시 시도'; btn.style.background = '#e8590c';
      msg.textContent = '발행하지 못했습니다 — 아래 기록을 확인하세요.';
    }).catch(function(){ setTimeout(function(){ poll(job); }, 3000); });
  }
  function start(force){
    btn.disabled = true; btn.textContent = '⏳ 네이버에 발행 중...'; btn.style.background = '#888';
    msg.textContent = 'Mac에서 네이버 에디터가 열려 자동으로 입력·발행합니다. 이 페이지를 그대로 두세요.';
    fetch('/api/publish', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({draft: DRAFT, auto: true, force: !!force,
        category_id: catSel.value, topic: topicSel.value})})
    .then(function(r){ return r.json().then(function(d){ return [r.status, d]; }); })
    .then(function(x){
      if (x[0] === 409 && x[1].url) return done(x[1].url);
      if (!x[1].job) throw new Error(x[1].error || '요청 실패');
      poll(x[1].job);
    }).catch(function(e){ btn.disabled = false; btn.textContent = '🚀 다시 시도'; msg.textContent = '오류: ' + e.message; });
  }
  btn.onclick = function(){ start(false); };
  fetch('/api/published/' + DRAFT).then(function(r){ return r.json(); })
    .then(function(d){ if (d.published && d.published.url) done(d.published.url); });
})();
</script>
"""


_publish_lock = threading.Lock()  # 네이버 프로필은 한 번에 한 브라우저만 (발행·로그인 공용)


# ---------------------------------------------------------------- 이미지 API 연결 상태
_IMAGE_SOURCES = (
    ("pexels", "Pexels", lambda: bool(config.PEXELS_API_KEY)),
    ("pixabay", "Pixabay", lambda: bool(config.PIXABAY_API_KEY)),
    ("cloudflare", "Cloudflare", config.cloudflare_ready),
    ("openai", "OpenAI", lambda: bool(config.OPENAI_API_KEY)),
    ("wikimedia", "위키미디어", lambda: True),
)


def _check_source(key: str) -> tuple:
    """(ok, 설명). 비용이 들지 않는 요청으로만 확인한다 (이미지 생성 안 함)."""
    try:
        if key == "pexels":
            r = requests.get("https://api.pexels.com/v1/search", timeout=15,
                             headers={"Authorization": config.PEXELS_API_KEY},
                             params={"query": "test", "per_page": 1})
        elif key == "unsplash":
            r = requests.get("https://api.unsplash.com/search/photos", timeout=15,
                             params={"query": "test", "per_page": 1,
                                     "client_id": config.UNSPLASH_ACCESS_KEY})
        elif key == "pixabay":
            r = requests.get("https://pixabay.com/api/", timeout=15,
                             params={"key": config.PIXABAY_API_KEY, "q": "test", "per_page": 3})
        elif key == "cloudflare":
            r = requests.get(
                f"https://api.cloudflare.com/client/v4/accounts/{config.CLOUDFLARE_ACCOUNT_ID}"
                "/ai/models/search", timeout=15, params={"per_page": 1},
                headers={"Authorization": f"Bearer {config.CLOUDFLARE_API_TOKEN}"})
        elif key == "openai":
            r = requests.get("https://api.openai.com/v1/models", timeout=15,
                             headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"})
        elif key == "wikimedia":
            from . import archive_images
            r = requests.get(archive_images.COMMONS_API, headers=archive_images.UA, timeout=15,
                             params={"action": "query", "meta": "siteinfo", "format": "json"})
        else:
            return False, "알 수 없음"
    except Exception as e:
        return False, f"연결 실패: {str(e)[:60]}"
    if r.status_code == 200:
        return True, "정상"
    hint = {401: "키가 틀렸거나 만료됨", 403: "권한 없음 (토큰 권한 확인)", 429: "요청 한도 초과",
            400: "요청 형식 오류 (키 값 확인)"}.get(r.status_code, "")
    return False, f"HTTP {r.status_code} {hint}".strip()


@app.get("/api/image-sources")
def api_image_sources():
    """키 설정 여부만 (빠름). check=1이면 실제로 각 API에 무료 요청을 보내 확인."""
    live = request.args.get("check") == "1"
    out = []
    for key, label, configured in _IMAGE_SOURCES:
        item = {"id": key, "label": label, "configured": configured()}
        if live and item["configured"]:
            item["ok"], item["detail"] = _check_source(key)
        elif not item["configured"]:
            item["ok"], item["detail"] = False, "키 없음"
        out.append(item)
    return jsonify(out)


# ---------------------------------------------------------------- 발행 카테고리·주제
def _draft_blog_category(draft_dir) -> str:
    try:
        return json.loads((draft_dir / "post.json").read_text(encoding="utf-8")).get("category", "")
    except Exception:
        return ""


_TOPIC_SUGGEST = {"경제": "비즈니스·경제", "역사": "교육·학문", "라이프": "건강·의학",
                  "AI·테크": "IT·컴퓨터", "사회·문화": "사회·정치"}


def _match_category(blog_cat: str, categories: list) -> str:
    """글 분야 → 네이버 카테고리 id, 이름으로 짝짓는다 (예: 경제→경제·투자, 라이프→건강·라이프).

    네이버에서 카테고리 이름을 바꿔도 분야 단어가 들어 있으면 자동으로 따라간다.
    """
    if not blog_cat:
        return ""
    tops = [c for c in categories if not c["parent"]]
    for c in tops:  # 1) 이름이 같으면 우선
        if c["name"] == blog_cat:
            return c["id"]
    words = [w for w in blog_cat.replace("·", " ").split() if w]
    for c in categories:  # 2) 분야 단어가 들어 있는 카테고리 (상위 먼저)
        if any(w in c["name"] for w in words):
            return c["id"]
    return ""


def _suggest_for(blog_cat: str, opts: dict) -> dict:
    """이 글 분야의 기본 선택값. 마지막 선택을 기억하되, 지워진 카테고리는 이름 매칭으로 대체."""
    cats = opts.get("categories", [])
    ids = {c["id"] for c in cats}
    saved = dict((opts.get("defaults") or {}).get(blog_cat) or {})
    if saved.get("category_id") not in ids:  # 비었거나 지워진 카테고리
        saved["category_id"] = _match_category(blog_cat, cats)
    if not saved.get("topic"):
        saved["topic"] = _TOPIC_SUGGEST.get(blog_cat, "")
    return {"category_id": saved.get("category_id", ""), "topic": saved.get("topic", "")}


def _resolve_publish_target(draft_dir, category_id, topic):
    """요청값 검증 + 자동 기본값 적용. 명시적으로 고른 값은 이 분야의 기본값으로 저장."""
    from . import naver
    opts = naver.load_options()
    blog_cat = _draft_blog_category(draft_dir)
    valid_ids = {c["id"] for c in opts.get("categories", [])}
    valid_topics = {t for ts in naver.NAVER_TOPICS.values() for t in ts} | {naver.NO_TOPIC}
    category_id = str(category_id or "").strip()
    topic = str(topic or "").strip()
    if category_id and category_id not in valid_ids:
        category_id = ""
    if topic and topic not in valid_topics:
        topic = ""
    if category_id or topic:
        if blog_cat:
            naver.save_default(blog_cat, category_id, topic)
        return category_id or None, topic or None
    sug = _suggest_for(blog_cat, opts)
    return sug.get("category_id") or None, sug.get("topic") or None


@app.get("/api/naver-options")
def api_naver_options():
    """발행 화면용: 내 블로그 카테고리 + 네이버 주제 목록 + (draft 지정 시) 추천 선택값."""
    from . import naver
    opts = naver.load_options()
    out = {
        "categories": opts.get("categories", []),
        # dict를 그대로 주면 Flask가 키를 가나다순으로 정렬해 네이버 화면 순서가 깨진다
        "topics": [[g, ts] for g, ts in naver.NAVER_TOPICS.items()],
        "no_topic": naver.NO_TOPIC,
        "fetched_at": opts.get("fetched_at", 0),
    }
    name = request.args.get("draft", "")
    if name and "/" not in name and ".." not in name:
        blog_cat = _draft_blog_category(config.DRAFTS_DIR / name)
        out["blog_category"] = blog_cat
        out["suggest"] = _suggest_for(blog_cat, opts)
    return jsonify(out)


@app.post("/api/naver-options/refresh")
def api_naver_options_refresh():
    """네이버 글쓰기 화면을 열어 카테고리 목록을 다시 읽는다 (발행하지 않음)."""
    job_id = _new_job("login")

    def run():
        from . import naver
        if _publish_lock.locked():
            _log(job_id, "↻ 열려 있는 발행 창을 정상 종료시키는 중...")
            naver.request_cancel()
        if not _publish_lock.acquire(timeout=120):
            _log(job_id, "✗ 발행 창이 닫히지 않습니다. Mac에서 그 창을 닫고 다시 시도하세요.")
            _finish(job_id, "error")
            return
        try:
            _log(job_id, "📂 네이버 카테고리 목록을 읽는 중... (발행하지 않음)")
            cats = naver.fetch_categories()
            _log(job_id, f"✓ 카테고리 {len(cats)}개 저장")
            _finish(job_id, "done", {"count": len(cats)})
        except SystemExit as e:
            _log(job_id, f"✗ {e}")
            _finish(job_id, "error")
        except Exception as e:
            _log(job_id, f"✗ 실패: {str(e)[:200]}")
            _finish(job_id, "error")
        finally:
            _publish_lock.release()

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"job": job_id})


# ---------------------------------------------------------------- 초안 수정
@app.get("/api/draft/<name>/raw")
def api_draft_raw(name):
    """수정 화면용 원본 구조 (제목·섹션·사진·태그·출처)."""
    if "/" in name or ".." in name:
        return jsonify({"error": "bad name"}), 400
    pj = config.DRAFTS_DIR / name / "post.json"
    if not pj.exists():
        return jsonify({"error": "초안을 찾을 수 없습니다."}), 404
    post = json.loads(pj.read_text(encoding="utf-8"))
    from . import naver
    return jsonify({
        "title": post.get("title", ""),
        "sections": [{"heading": s_.get("heading", ""), "body_md": s_.get("body_md", "")}
                     for s_ in post.get("sections", [])],
        "images": [{"file": Path(m["file"]).name, "caption": m.get("caption", ""),
                    "credit": m.get("credit", ""), "after_section": m.get("after_section", 0)}
                   for m in post.get("image_files", [])],
        "tags": post.get("tags", []),
        "source_line": post.get("source_line", ""),
        "category": post.get("category", ""),
        "categories": list(config.CATEGORIES.values()),
        "published": naver.published_info(config.DRAFTS_DIR / name),
    })


@app.post("/api/draft/<name>/save")
def api_draft_save(name):
    """수정 내용을 저장하고 미리보기·마크다운을 다시 만든다."""
    if "/" in name or ".." in name:
        return jsonify({"error": "bad name"}), 400
    draft_dir = config.DRAFTS_DIR / name
    pj = draft_dir / "post.json"
    if not pj.exists():
        return jsonify({"error": "초안을 찾을 수 없습니다."}), 404
    data = request.get_json(force=True)
    post = json.loads(pj.read_text(encoding="utf-8"))

    title = (data.get("title") or "").strip()
    sections = data.get("sections")
    if not title:
        return jsonify({"error": "제목이 비어 있습니다."}), 400
    if not isinstance(sections, list) or not sections:
        return jsonify({"error": "본문이 비어 있습니다."}), 400
    old_secs = post.get("sections", [])

    def _keep_highlight(i, x):
        # 편집 화면은 강조 문구를 보내지 않는다 → 같은 위치·같은 소제목이면 기존 문구를 유지
        if "highlight" in x:
            return str(x.get("highlight") or "").strip()
        if i < len(old_secs) and (old_secs[i].get("heading") or "").strip() == str(x.get("heading", "")).strip():
            return str(old_secs[i].get("highlight") or "").strip()
        return ""

    sections = [{"heading": str(x.get("heading", "")).strip(),
                 "body_md": str(x.get("body_md", "")).strip(),
                 "highlight": _keep_highlight(i, x)} for i, x in enumerate(sections)]
    if not any(x["body_md"] for x in sections):
        return jsonify({"error": "본문이 비어 있습니다."}), 400

    # 사진: 파일명으로 기존 메타를 찾아 캡션·위치만 바꾼다 (목록에서 빠진 사진은 글에서 제외)
    by_file = {Path(m["file"]).name: m for m in post.get("image_files", [])}
    metas = []
    for im in data.get("images", []):
        m = by_file.get(str(im.get("file", "")))
        if not m:
            continue
        m = dict(m)
        # 사진 경로를 이 초안 폴더 기준으로 고정 (폴더를 옮기거나 복사해도 미리보기가 깨지지 않게)
        m["file"] = str(draft_dir / "images" / Path(m["file"]).name)
        m["caption"] = str(im.get("caption", m.get("caption", ""))).strip()
        try:
            m["after_section"] = max(0, min(int(im.get("after_section", 0)), len(sections) - 1))
        except (TypeError, ValueError):
            pass
        metas.append(m)

    tags = data.get("tags", post.get("tags", []))
    if isinstance(tags, str):
        tags = [t for t in re.split(r"[#,\s]+", tags) if t]
    post.update({
        "title": title,
        "sections": sections,
        "tags": [str(t).strip() for t in tags if str(t).strip()][:30],
        "source_line": str(data.get("source_line", post.get("source_line", ""))).strip(),
        "edited_at": int(time.time()),
    })
    if data.get("category") in config.CATEGORIES.values():
        post["category"] = data["category"]
    post.pop("image_files", None)
    builder.build_draft(post, metas, draft_dir)
    return jsonify({"ok": True, "chars": sum(len(x["body_md"]) for x in sections)})


@app.get("/edit/<name>/")
def edit_page(name):
    if "/" in name or ".." in name or not (config.DRAFTS_DIR / name / "post.json").exists():
        return "초안을 찾을 수 없습니다.", 404
    return Response(_EDIT_HTML.replace("__DRAFT__", name), mimetype="text/html")


@app.post("/api/login")
def api_login():
    job_id = _new_job("login")

    def run():
        from . import naver

        # 로그인 창도 같은 프로필을 쓰므로 열려 있는 발행 창을 먼저 정상 종료
        if _publish_lock.locked():
            _log(job_id, "↻ 열려 있는 발행 창을 정상 종료시키는 중...")
            naver.request_cancel()
        if not _publish_lock.acquire(timeout=120):
            _log(job_id, "✗ 발행 창이 닫히지 않습니다. Mac에서 그 창을 닫고 다시 시도하세요.")
            _finish(job_id, "error")
            return
        try:
            _log(job_id, "Mac에 브라우저가 열립니다. '로그인 상태 유지'를 체크하고 로그인하세요.")
            naver.login()
            st = naver.session_status()
            if st["ok"] and st["persistent"]:
                _log(job_id, "✓ 로그인 세션 저장 완료 (로그인 상태 유지)")
            elif st["ok"]:
                _log(job_id, "⚠ 로그인됐지만 '로그인 상태 유지'가 꺼져 있어 곧 만료됩니다 — 체크 후 다시 로그인하세요")
            else:
                _log(job_id, "⚠ 로그인 세션이 확인되지 않습니다 — 다시 시도하세요")
            _finish(job_id, "done")
        except SystemExit as e:
            _log(job_id, str(e))
            _finish(job_id, "error")
        except Exception as e:
            _log(job_id, f"✗ 실패: {e}")
            _finish(job_id, "error")
        finally:
            _publish_lock.release()

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"job": job_id})


@app.post("/api/publish")
def api_publish():
    data = request.get_json(force=True)
    name = data.get("draft", "")
    auto = bool(data.get("auto"))
    if "/" in name or ".." in name:
        return jsonify({"error": "bad name"}), 400
    draft_dir = config.DRAFTS_DIR / name
    if not (draft_dir / "post.json").exists():
        return jsonify({"error": "초안을 찾을 수 없습니다."}), 404

    if auto and not data.get("force"):
        from . import naver as _nv
        info = _nv.published_info(draft_dir)
        if info:
            return jsonify({"error": "already_published", "url": info.get("url")}), 409
        resv = _nv.reserved_info(draft_dir)
        if resv:
            return jsonify({"error": "already_reserved", "at": resv.get("at")}), 409

    # 카테고리·주제: 값이 없으면('자동') 이 글 분야의 마지막 선택(기본값)을 쓴다
    category_id, topic = _resolve_publish_target(draft_dir, data.get("category_id"),
                                                 data.get("topic"))
    job_id = _new_job("publish")

    def run():
        from . import naver

        # 같은 프로필로 브라우저를 두 개 띄우면 프로필이 깨진다("프로필을 여는 동안 문제").
        # 이전 발행 창이 열려 있으면 정상 종료시킨 뒤 이어서 진행한다.
        if _publish_lock.locked():
            _log(job_id, "↻ 이전 발행 창이 열려 있어 정상 종료시키는 중...")
            naver.request_cancel()
        if not _publish_lock.acquire(timeout=120):
            _log(job_id, "✗ 이전 발행 창이 닫히지 않습니다. Mac에서 그 창을 닫고 다시 시도하세요.")
            _finish(job_id, "error")
            return
        try:
            # 세션이 없으면 브라우저를 띄우기 전에 바로 안내 (쿠키 DB만 확인)
            if not naver.session_status()["ok"]:
                _log(job_id, "✗ " + naver.LOGIN_HELP)
                _finish(job_id, "error")
                return
            _log(job_id, "브라우저를 열어 에디터에 입력합니다...")
            res = naver.publish(draft_dir, auto_publish=auto, log=lambda m: _log(job_id, m),
                                category_id=category_id, topic=topic)
            _log(job_id, "■ 취소됨 (새 요청으로 대체)" if res == "cancelled" else "✓ 완료")
            info = naver.published_info(draft_dir) or {}
            _finish(job_id, "done", {"draft": name, "url": info.get("url")})
        except SystemExit as e:
            _log(job_id, str(e))
            _finish(job_id, "error")
        except Exception as e:
            _log(job_id, f"✗ 실패: {e}")
            _finish(job_id, "error")
        finally:
            _publish_lock.release()

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"job": job_id})


# ─────────────────────────────────────────────────────────────
# 자동 예약 발행: 소재 발굴 → 생성 → 네이버 예약발행 (평균 13분 간격)
# ─────────────────────────────────────────────────────────────

def _autopilot_generate(query: str, category: str, angle: str, used_model: str, image_provider,
                        audience: str = "women", story_id: str = None):
    from . import stories as stories_mod

    story = stories_mod.get(story_id) if story_id else None
    if story_id and not story:
        raise RuntimeError("대기열에서 사연을 찾을 수 없습니다.")
    label = config.AUDIENCES.get(audience, config.AUDIENCES["women"])["label"]
    note = f"독자는 {label}이다." + (f" 관점: {angle}" if angle else "")
    job_id = _new_job("generate")
    # 자동 예약은 사람이 검수하지 않으므로 교정을 항상 돌린다 (Haiku도 드물게 오타를 낸다)
    _run_topic_generation(job_id, query, category, note, used_model, image_provider,
                          force_proofread=True, audience=audience, story=story)
    job = JOBS.get(job_id) or {}
    draft = (job.get("result") or {}).get("draft")
    if job.get("status") != "done" or not draft:
        raise RuntimeError((job.get("log") or ["생성 실패"])[-1][:140])
    return draft


def _autopilot_publish(draft_name: str, slot):
    """예약 발행 등록. 잠금을 기다리는 사이 시각이 임박하면 가장 가까운 유효 격자로 미룬다."""
    import datetime as _dt
    from . import autopilot, naver

    draft_dir = config.DRAFTS_DIR / draft_name
    cat_id, topic = _resolve_publish_target(draft_dir, None, None)
    job_id = _new_job("publish")
    for attempt in (1, 2):
        # 사용자가 직접 발행 중이면 취소시키지 않고 끝나길 기다린다
        if not _publish_lock.acquire(timeout=1800):
            _finish(job_id, "error")
            raise RuntimeError("발행 창이 오래 사용 중이라 대기 시간을 넘겼습니다.")
        try:
            floor = _dt.datetime.now() + _dt.timedelta(minutes=autopilot.MIN_LEAD_MIN - 3)
            if slot < floor:
                slot = autopilot.ceil_grid(floor + _dt.timedelta(minutes=3))
            if not naver.session_status()["ok"]:
                raise RuntimeError(naver.LOGIN_HELP)
            _log(job_id, f"예약 발행 등록: {slot:%m/%d %H:%M}")
            out = {}
            res = naver.publish(draft_dir, auto_publish=True, log=lambda m: _log(job_id, m),
                                category_id=cat_id, topic=topic, reserve_at=slot, reserve_out=out)
            slot = out.get("at", slot)
        except SystemExit as e:
            _finish(job_id, "error")
            raise RuntimeError(str(e))
        except Exception as e:
            _log(job_id, f"✗ 실패: {e}")
            _finish(job_id, "error")
            raise
        finally:
            _publish_lock.release()
        if res != "cancelled":
            _finish(job_id, "done", {"draft": draft_name, "reserved": slot.strftime("%Y-%m-%d %H:%M")})
            return slot
        _log(job_id, "사용자의 다른 발행 요청으로 취소됨 — 다시 시도합니다")
    _finish(job_id, "error")
    raise RuntimeError("발행이 두 번 취소되어 중단했습니다.")


@app.post("/api/autopilot")
def api_autopilot_start():
    from . import autopilot, naver

    data = request.get_json(force=True) or {}
    cats = [c for c in (data.get("categories") or list(config.CATEGORIES)) if c in config.CATEGORIES]
    if not cats:
        return jsonify({"error": "분야를 하나 이상 고르세요."}), 400
    try:
        count = int(data.get("count") or 6)
    except (TypeError, ValueError):
        return jsonify({"error": "편수가 올바르지 않습니다."}), 400
    if not 1 <= count <= autopilot.MAX_COUNT:
        return jsonify({"error": f"편수는 1~{autopilot.MAX_COUNT}편입니다."}), 400
    if not naver.session_status()["ok"]:
        return jsonify({"error": naver.LOGIN_HELP}), 400
    if not config.NAVER_BLOG_ID:
        return jsonify({"error": "NAVER_BLOG_ID가 설정돼 있지 않습니다."}), 400
    first_at = None
    raw_at = str(data.get("first_at") or "").strip()
    if raw_at:       # 첫 예약 시각 직접 지정 (브라우저 datetime-local: 2026-11-07T21:30)
        import datetime as _dt
        try:
            first_at = _dt.datetime.fromisoformat(raw_at)
        except ValueError:
            return jsonify({"error": "첫 예약 시각 형식이 올바르지 않습니다."}), 400
        now = _dt.datetime.now()
        earliest = autopilot.first_slot(0)
        if first_at < earliest:
            return jsonify({"error": f"첫 예약 시각은 지금보다 최소 {autopilot.MIN_LEAD_MIN}분 뒤여야 합니다 "
                                     f"(가장 빠른 시각 {earliest:%m/%d %H:%M})."}), 400
        if first_at > now + _dt.timedelta(days=14):
            return jsonify({"error": "첫 예약 시각은 14일 이내로 지정하세요."}), 400
    # 글쓰기 모델: 비우면 기본 모델. 고른 모델의 API 키가 없으면 시작하지 않는다.
    req_model = str(data.get("model") or "").strip()
    if req_model and req_model not in {m[0] for m in config.AVAILABLE_MODELS}:
        return jsonify({"error": f"지원하지 않는 모델: {req_model}"}), 400
    used_model = config.enforce_model(req_model or config.MODEL)
    if config.is_exaone_model(used_model) and not config.EXAONE_API_KEY:
        return jsonify({"error": "EXAONE_API_KEY가 설정돼 있지 않아 EXAONE으로 쓸 수 없습니다."}), 400
    if config.is_kimi_model(used_model) and not config.KIMI_API_KEY:
        return jsonify({"error": "KIMI_API_KEY가 설정돼 있지 않아 Kimi로 쓸 수 없습니다."}), 400
    image_provider = data.get("images")
    try:
        info = autopilot.start(
            count, cats,
            generate_fn=lambda q, c, a, aud, sid=None: _autopilot_generate(q, c, a, used_model, image_provider, aud, sid),
            publish_fn=_autopilot_publish,
            interval=int(data.get("interval") or autopilot.DEFAULT_INTERVAL),
            start_in_min=int(data.get("start_in_min") or autopilot.DEFAULT_START_MIN),
            audience=data.get("audience") or None,
            fill=bool(data.get("fill")),
            first_at=first_at,
        )
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 409
    info["model"] = used_model
    info["model_label"] = dict(config.AVAILABLE_MODELS).get(used_model, used_model).split(" · ")[0]
    return jsonify(info)


# ── 판 인기글 2시간 자동 작성: 새 스냅샷이 생기면 판 사연 한 편을 쓰고 예약 발행한다 ──
_PANN_TICK_SEC = 120


def _pann_auto_tick():
    """한 번 점검. 조건이 맞으면 autopilot을 '판 사연 1편' 모드로 시작한다."""
    from . import autopilot, naver, pann_feed

    due, why = pann_feed.auto_due()
    if not due:
        pann_feed.auto_save(message=why if isinstance(why, str) else "")
        return
    if autopilot.snapshot().get("running"):
        pann_feed.auto_save(message="자동 예약이 진행 중이라 끝나면 이어서 씁니다")
        return
    if not config.NAVER_BLOG_ID or not naver.session_status()["ok"]:
        pann_feed.auto_save(message="네이버 로그인이 필요합니다")
        return
    used_model = config.enforce_model(config.MODEL)
    try:
        autopilot.start(
            1, list(config.CATEGORIES),
            generate_fn=lambda q, c, a, aud, sid=None: _autopilot_generate(q, c, a, used_model, None, aud, sid),
            publish_fn=_autopilot_publish,
            only_stories="pann",
        )
    except RuntimeError:
        return   # 그사이 다른 자동 예약이 시작됨 — 다음 점검에서 다시
    import datetime as _dt
    pann_feed.auto_save(last_cycle=time.time(), last_snapshot=why,
                        message=f"{_dt.datetime.now():%m-%d %H:%M} 판 사연 한 편 작성 시작")


def _pann_auto_loop():
    time.sleep(60)
    while True:
        try:
            _pann_auto_tick()
        except Exception as e:
            try:
                from . import pann_feed

                pann_feed.auto_save(message=f"점검 오류: {str(e)[:80]}")
            except Exception:
                pass
        time.sleep(_PANN_TICK_SEC)


@app.get("/api/pann/auto")
def api_pann_auto_get():
    from . import pann_feed

    st = pann_feed.auto_state()
    due, why = pann_feed.auto_due()
    st["due"] = bool(due)
    st["errors"] = pann_feed.unreadable()
    return jsonify(st)


@app.post("/api/pann/auto")
def api_pann_auto_set():
    from . import pann_feed

    data = request.get_json(force=True) or {}
    return jsonify(pann_feed.auto_save(enabled=bool(data.get("enabled"))))


@app.get("/api/autopilot")
def api_autopilot_status():
    from . import autopilot

    return jsonify(autopilot.snapshot())


@app.post("/api/autopilot/stop")
def api_autopilot_stop():
    from . import autopilot

    autopilot.stop()
    return jsonify({"ok": True})


def _lan_ip():
    """맥의 LAN IP (같은 Wi-Fi에서 폰이 접속할 주소)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def _tailscale_info():
    """Tailscale이 설치·실행 중이면 (ip, hostname.magic-dns) 반환.

    - Wi-Fi/네트워크가 바뀌어도 유효한 사설 주소를 제공
    - 본인 계정 기기끼리만 접속 가능 (공개되지 않음)
    """
    import shutil, subprocess

    # 1순위: 네트워크 인터페이스에서 Tailscale CGNAT IP(100.64.0.0/10) 직접 탐지.
    #        launchd(GUI 세션 밖)에서도 동작하며, CLI-데몬 통신 실패에 영향받지 않음.
    def _ip_from_interfaces():
        try:
            out = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=3)
            for m in re.finditer(r"inet (\d{1,3}(?:\.\d{1,3}){3})", out.stdout):
                ip = m.group(1)
                octs = [int(x) for x in ip.split(".")]
                # Tailscale CGNAT: 100.64.0.0 ~ 100.127.255.255
                if octs[0] == 100 and 64 <= octs[1] <= 127:
                    return ip
        except Exception:
            pass
        return None

    ip = _ip_from_interfaces()

    cli = None
    if shutil.which("tailscale"):
        cli = shutil.which("tailscale")
    elif Path("/Applications/Tailscale.app").exists():
        cli = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"

    # 2순위: 인터페이스에서 못 찾았으면 CLI 시도 (GUI 세션에서 실행 시)
    if not ip and cli:
        try:
            ip_out = subprocess.run([cli, "ip", "-4"], capture_output=True, text=True, timeout=3)
            cand = ip_out.stdout.strip().split("\n")[0].strip() if ip_out.returncode == 0 else ""
            if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", cand):
                ip = cand
        except Exception:
            pass

    if not ip:
        return None

    # MagicDNS 호스트명 (선택) — CLI가 되면 붙이고, 안 되면 IP만으로 충분
    magic = ""
    if cli:
        try:
            status = subprocess.run([cli, "status", "--json"], capture_output=True, text=True, timeout=3)
            if status.returncode == 0:
                import json as _json
                data = _json.loads(status.stdout)
                magic = (data.get("Self") or {}).get("DNSName", "").rstrip(".")
        except Exception:
            pass
    return {"ip": ip, "hostname": magic}


# 서버가 어떤 host로 바인딩됐는지 저장 (LAN URL 표시용)
_bind_host = "127.0.0.1"

_kimi_bal_cache = {"t": 0.0, "val": None}


def _kimi_balance():
    """Kimi(Moonshot) 잔액(USD). 2분 캐시. 실패 시 None."""
    if not config.KIMI_API_KEY:
        return None
    now = time.time()
    if now - _kimi_bal_cache["t"] < 120:
        return _kimi_bal_cache["val"]
    val = None
    try:
        r = requests.get(
            f"{config.KIMI_BASE_URL.rstrip('/')}/users/me/balance",
            headers={"Authorization": f"Bearer {config.KIMI_API_KEY}"},
            timeout=8,
        )
        if r.ok:
            val = round(float(r.json()["data"]["available_balance"]), 2)
    except Exception:
        val = None
    _kimi_bal_cache.update(t=now, val=val)
    return val


@app.get("/api/usage-stats")
def api_usage_stats():
    """조사(검색) 단계 API 사용 집계 — Claude·Kimi 호출 수, 보강 발생률, Claude 추정 비용."""
    from . import usage_log
    try:
        days = max(1, min(int(request.args.get("days", 7)), 60))
    except ValueError:
        days = 7
    return jsonify(usage_log.stats(days))


@app.post("/api/provider-reset")
def api_provider_reset():
    """OpenAI·Cloudflare 오류 표시를 지운다 (충전 후 다음 호출에서 다시 판정)."""
    from . import llm_guard
    name = (request.get_json(silent=True) or {}).get("name", "")
    if name not in ("openai", "cloudflare"):
        return jsonify({"error": "bad name"}), 400
    llm_guard.note_provider_ok(name)
    return jsonify({"ok": True})


@app.post("/api/anthropic-recheck")
def api_anthropic_recheck():
    """충전 후 즉시 재확인: 초소형 호출로 크레딧 상태를 검사하고 차단기를 갱신."""
    from . import llm_guard
    import anthropic as _a
    if not config.ANTHROPIC_API_KEY:
        return jsonify({"ok": False, "reason": "ANTHROPIC_API_KEY 없음"})
    try:
        _a.Anthropic().messages.create(
            model="claude-haiku-4-5", max_tokens=1,
            messages=[{"role": "user", "content": "ping"}],
        )
        llm_guard.mark_ok()
        return jsonify({"ok": True})
    except Exception as e:
        if llm_guard.is_credit_error(e):
            llm_guard.mark_credit_out("Anthropic 크레딧 부족")
            return jsonify({"ok": False, "reason": "크레딧 부족"})
        return jsonify({"ok": False, "reason": str(e)[:120]})


@app.get("/api/config")
def api_config():
    port = request.host.split(':')[-1] if ':' in request.host else '8765'
    lan_url = None
    tailscale_url = None
    tailscale_hostname_url = None
    if _bind_host in ("0.0.0.0", "::"):
        ip = _lan_ip()
        if ip:
            lan_url = f"http://{ip}:{port}"
        ts = _tailscale_info()
        if ts:
            tailscale_url = f"http://{ts['ip']}:{port}"
            if ts.get("hostname"):
                tailscale_hostname_url = f"http://{ts['hostname']}:{port}"
    return jsonify(
        {
            "model": config.MODEL,
            "models": [{"id": m[0], "label": m[1]} for m in config.AVAILABLE_MODELS],
            "naver_blog_id": config.NAVER_BLOG_ID,
            "anthropic_key": bool(config.ANTHROPIC_API_KEY and len(config.ANTHROPIC_API_KEY) > 20),
            "pexels_key": bool(config.PEXELS_API_KEY),
            "kimi_key": bool(config.KIMI_API_KEY),
            "exaone_key": bool(config.EXAONE_API_KEY),
            "kimi_balance": _kimi_balance(),
            "anthropic": __import__("blogbot.llm_guard", fromlist=["status"]).status(),
            "provider_status": {
                n: __import__("blogbot.llm_guard", fromlist=["x"]).provider_status(n)
                for n in ("openai", "cloudflare")},
            "fallback_model": config.FALLBACK_MODEL,
            "image_provider": config.IMAGE_PROVIDER,
            "image_providers": [{"id": k, "label": v} for k, v in config.IMAGE_PROVIDERS],
            "openai_key": bool(config.OPENAI_API_KEY),
            "openai_image_model": config.OPENAI_IMAGE_MODEL,
            "news_login": __import__("blogbot.news", fromlist=["has_session"]).has_session(),
            "naver_session": __import__("blogbot.naver", fromlist=["session_status"]).session_status(),
            "categories": config.CATEGORIES,
            "lan_url": lan_url,
            "tailscale_url": tailscale_url,
            "tailscale_hostname_url": tailscale_hostname_url,
        }
    )


# ─── PWA 지원: manifest + 아이콘 ───────────────────────────
@app.get("/manifest.json")
def pwa_manifest():
    return jsonify(
        {
            "name": "blogbot",
            "short_name": "blogbot",
            "description": "블로그 자동 작성",
            "start_url": "/",
            "display": "standalone",
            "orientation": "portrait",
            "background_color": "#f6f5f1",
            "theme_color": "#2f6b4f",
            "icons": [
                {"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"},
            ],
            # Android 공유 시트에 "blogbot" 등장 → 기사 본문/URL을 받아 자동 생성
            "share_target": {
                "action": "/share-target",
                "method": "POST",
                "enctype": "multipart/form-data",
                "params": {"title": "title", "text": "text", "url": "url"},
            },
        }
    )


@app.get("/sw.js")
def service_worker():
    # 공유(POST /share-target)를 가로채 캐시에 저장하고 앱으로 리다이렉트
    js = """
self.addEventListener('install', (e) => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (event.request.method === 'POST' && url.pathname === '/share-target') {
    event.respondWith((async () => {
      try {
        const form = await event.request.formData();
        const data = {
          text: form.get('text') || '',
          title: form.get('title') || '',
          link: form.get('url') || ''
        };
        const cache = await caches.open('blogbot-share');
        await cache.put('/shared-data', new Response(JSON.stringify(data),
          { headers: { 'Content-Type': 'application/json' } }));
      } catch (e) {}
      return Response.redirect('/?shared=1', 303);
    })());
  }
});
"""
    return Response(js, mimetype="application/javascript")


@app.get("/icon.svg")
def pwa_icon():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 192 192">'
        '<rect width="192" height="192" fill="#2f6b4f" rx="42"/>'
        '<text x="50%" y="50%" font-size="120" font-family="Apple Color Emoji,Segoe UI Emoji,sans-serif" '
        'text-anchor="middle" dominant-baseline="central">🌿</text>'
        "</svg>"
    )
    return Response(svg, mimetype="image/svg+xml")


@app.get("/")
def index():
    return (Path(__file__).parent / "web_ui.html").read_text(encoding="utf-8")


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def run_server(port: int = 8765, host: str = "127.0.0.1", open_browser: bool = True):
    global _bind_host
    _bind_host = host

    # 포트가 이미 잡혀 있으면 그 서버로 브라우저만 열고 종료 (중복 실행 방지)
    if _port_in_use(port):
        print(f"⚠ 포트 {port}이(가) 이미 사용 중입니다. 기존 서버로 브라우저를 엽니다.")
        if open_browser:
            import webbrowser
            webbrowser.open(f"http://127.0.0.1:{port}")
        return
    if open_browser:
        import webbrowser

        threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    print(f"🌐 blogbot GUI: http://127.0.0.1:{port}  (Ctrl-C로 종료)")
    if host in ("0.0.0.0", "::"):
        ip = _lan_ip()
        if ip:
            print(f"📱 같은 Wi-Fi 접속:      http://{ip}:{port}")
        ts = _tailscale_info()
        if ts:
            print(f"🌍 어디서든 (Tailscale): http://{ts['ip']}:{port}")
            if ts.get("hostname"):
                print(f"    (또는): http://{ts['hostname']}:{port}")
        else:
            print("💡 어디서든 접속: Tailscale 설치 안내는 README-mobile.md 참고")
    threading.Thread(target=_pann_auto_loop, daemon=True).start()
    app.run(host=host, port=port, debug=False)
