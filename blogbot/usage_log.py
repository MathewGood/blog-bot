"""조사(검색) 단계의 API 호출 기록 — 어떤 단계에서 Claude·Kimi를 몇 번, 얼마나 썼는지 측정한다.

- 호출마다 usage_log.jsonl에 한 줄씩 남긴다 (서버 재시작에도 유지, git 제외).
- session()으로 '조사 1회'(기사 조사 또는 키워드 자료집)를 묶어, 조사 1회당 Claude가
  쓰였는지·보강이 몇 번 일어났는지도 집계한다.
- 기록 실패는 조용히 무시한다 (본 작업을 절대 멈추지 않음).
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
import uuid

from . import config

LOG_FILE = config.ROOT / "usage_log.jsonl"
KEEP_DAYS = 60   # 이보다 오래된 줄은 집계 때 무시하고, 가끔 파일에서 정리한다

# 단가 (2026-10 공식 가격표). Kimi는 계정 잔액 API로 따로 보므로 횟수만 센다.
_CLAUDE_PRICE = {"claude-haiku-4-5": (1.00, 5.00)}   # $/1M (입력, 출력)
_WEB_SEARCH_PRICE = 10.0 / 1000                       # $/회

PURPOSE_LABEL = {
    "queries": "검색어 만들기",
    "search": "웹 검색(Search Pro)",
    "fetch": "페이지 본문 가져오기",
    "brief": "조사 요약 작성",
    "kimi_websearch": "Kimi 내장 검색 조사",
    "boost": "자료집 보강(웹 검색)",
    "fallback": "대체 조사(웹 검색)",
}

_lock = threading.Lock()
_local = threading.local()


@contextlib.contextmanager
def session(kind: str, label: str = ""):
    """조사 1회를 묶는다. kind: 'article'(기사 조사) | 'topic'(키워드·트렌드 자료집)."""
    prev = getattr(_local, "sid", None)
    sid = uuid.uuid4().hex[:10]
    _local.sid = sid
    _write({"ev": "session", "sid": sid, "kind": kind, "label": label[:80]})
    try:
        yield sid
    finally:
        _local.sid = prev


def current():
    """지금 스레드의 조사 묶음 id (다른 스레드로 넘길 때 사용)."""
    return getattr(_local, "sid", None)


def run_with(sid, fn, *a, **kw):
    """스레드 풀 작업에서도 같은 조사 묶음으로 기록되게 sid를 붙여 실행한다."""
    prev = getattr(_local, "sid", None)
    _local.sid = sid
    try:
        return fn(*a, **kw)
    finally:
        _local.sid = prev


def record(provider: str, purpose: str, model: str = "", ok: bool = True,
           in_tok: int = 0, out_tok: int = 0, searches: int = 0, note: str = ""):
    """API 호출 1건. provider: 'claude' | 'kimi' | 'kimi_search'."""
    _write({"ev": "call", "sid": getattr(_local, "sid", None), "provider": provider,
            "purpose": purpose, "model": model, "ok": bool(ok),
            "in": int(in_tok or 0), "out": int(out_tok or 0),
            "searches": int(searches or 0), "note": note[:120]})


def claude_usage(resp) -> tuple:
    """Anthropic 응답 → (입력 토큰, 출력 토큰, 웹 검색 횟수)."""
    try:
        u = resp.usage
        stu = getattr(u, "server_tool_use", None)
        return (u.input_tokens or 0, u.output_tokens or 0,
                (getattr(stu, "web_search_requests", 0) or 0) if stu else 0)
    except Exception:
        return 0, 0, 0


def kimi_usage(data: dict) -> tuple:
    u = (data or {}).get("usage") or {}
    return u.get("prompt_tokens", 0) or 0, u.get("completion_tokens", 0) or 0


def _write(obj: dict):
    obj["ts"] = round(time.time(), 1)
    try:
        with _lock, LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _read(since: float) -> list:
    out = []
    try:
        with LOG_FILE.open(encoding="utf-8") as f:
            for line in f:
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if o.get("ts", 0) >= since:
                    out.append(o)
    except FileNotFoundError:
        pass
    return out


def _prune():
    """KEEP_DAYS보다 오래된 줄을 파일에서 지운다 (파일이 1MB를 넘을 때만)."""
    try:
        if LOG_FILE.stat().st_size < 1_000_000:
            return
        keep = _read(time.time() - KEEP_DAYS * 86400)
        with _lock:
            LOG_FILE.write_text("".join(json.dumps(o, ensure_ascii=False) + "\n" for o in keep),
                                encoding="utf-8")
    except Exception:
        pass


def claude_cost(model: str, in_tok: int, out_tok: int, searches: int) -> float:
    pin, pout = _CLAUDE_PRICE.get(model, _CLAUDE_PRICE["claude-haiku-4-5"])
    return (in_tok * pin + out_tok * pout) / 1e6 + searches * _WEB_SEARCH_PRICE


def stats(days: int = 7) -> dict:
    """최근 N일 조사 단계 집계."""
    _prune()
    rows = _read(time.time() - days * 86400)
    sessions = {o["sid"]: o for o in rows if o.get("ev") == "session"}
    calls = [o for o in rows if o.get("ev") == "call"]

    by_provider: dict = {}
    by_purpose: dict = {}
    claude_cost_total = 0.0
    for c in calls:
        p = by_provider.setdefault(c["provider"], {"calls": 0, "fail": 0, "in": 0, "out": 0,
                                                   "searches": 0, "cost": 0.0})
        p["calls"] += 1
        p["fail"] += 0 if c.get("ok") else 1
        p["in"] += c.get("in", 0)
        p["out"] += c.get("out", 0)
        p["searches"] += c.get("searches", 0)
        if c["provider"] == "claude":
            cost = claude_cost(c.get("model", ""), c.get("in", 0), c.get("out", 0), c.get("searches", 0))
            p["cost"] += cost
            claude_cost_total += cost
        key = f'{c["provider"]}:{c["purpose"]}'
        q = by_purpose.setdefault(key, {"provider": c["provider"], "purpose": c["purpose"],
                                        "label": PURPOSE_LABEL.get(c["purpose"], c["purpose"]),
                                        "calls": 0, "fail": 0})
        q["calls"] += 1
        q["fail"] += 0 if c.get("ok") else 1

    # 조사 1회 단위: Claude가 끼었는지, 보강·대체 조사가 일어났는지
    per_sid: dict = {}
    for c in calls:
        if c.get("sid"):
            per_sid.setdefault(c["sid"], []).append(c)
    n_sess = len(sessions)
    used_claude = sum(1 for s in sessions if any(c["provider"] == "claude" and c.get("ok")
                                                 for c in per_sid.get(s, [])))
    boosted = sum(1 for s in sessions if any(c["purpose"] == "boost" for c in per_sid.get(s, [])))
    fellback = sum(1 for s in sessions if any(c["purpose"] == "fallback" for c in per_sid.get(s, [])))
    kinds: dict = {}
    for s in sessions.values():
        kinds[s.get("kind", "?")] = kinds.get(s.get("kind", "?"), 0) + 1

    # 'LLM 호출' 비중 = Claude 호출 / (Claude + Kimi 채팅 호출). Search Pro·본문 가져오기는 검색 API라 따로.
    llm_claude = by_provider.get("claude", {}).get("calls", 0)
    llm_kimi = by_provider.get("kimi", {}).get("calls", 0)
    share = round(llm_claude * 100 / (llm_claude + llm_kimi)) if (llm_claude + llm_kimi) else None
    first = min((o["ts"] for o in rows), default=None)
    return {
        "days": days,
        "since": first,
        "sessions": n_sess,
        "session_kinds": kinds,
        "sessions_with_claude": used_claude,
        "sessions_boosted": boosted,
        "sessions_fallback": fellback,
        "claude_llm_share_pct": share,
        "claude_cost_usd": round(claude_cost_total, 4),
        "claude_cost_per_session": round(claude_cost_total / n_sess, 4) if n_sess else None,
        "by_provider": by_provider,
        "by_purpose": sorted(by_purpose.values(), key=lambda x: (-x["calls"])),
    }
