"""네이트 판 인기글 파일 → 독자 사연 대기열.

사용자의 별도 수집 작업이 구글 드라이브에 쌓는 두 개의 마크다운 파일을 읽는다(사이트에는 접속하지 않는다).
  · 일간(매일 06시 1~10위):  config.PANN_DAILY_FILE
  · 시간별(2시간마다 1~5위): config.PANN_HOUR_FILE   — 같은 글이 여러 스냅샷에 반복해 나온다
파일 형식: `## 수집 시각` → `### N위` → `**게시글 제목:**` / `**원문 URL:**` / 본문.

소재가 겹치지 않도록 세 겹으로 막는다.
  1. 글 번호(URL의 /talk/<번호>): 한 번 본 글은 대기열에 넣었든 건너뛰었든 다시 보지 않는다(pann_seen.json).
  2. 제목: 번호가 달라도 제목이 같은 재게시는 건너뛴다.
  3. 주제: 익명 요약한 주제문이 이미 쓴 글·대기열 사연과 비슷하면 건너뛴다.
원문 전체는 저장하지 않는다 — 대기열에는 익명 요약·주제문·사회적 쟁점과, 원문에서 확인된 짧은 발췌문(식별 정보는 ○○로 가림)만 들어간다.
글은 이 발췌문을 인용하고 사회적 관점의 생각을 덧붙이는 해설 칼럼이다(각색 이야기가 아님, 2026-11-03).
"""

from __future__ import annotations

import datetime as dt
import json
import re
import threading
import time

from . import config, stories

STATE_FILE = config.ROOT / "pann_seen.json"
MAX_AGE_H = 36        # 이보다 오래된 스냅샷은 보지 않는다(처음 연결할 때 묵은 글이 쏟아지지 않게)
MAX_ANALYZE = 8       # 한 번 가져올 때 Kimi로 요약해 보는 새 글 수 상한
MAX_QUEUED = 6        # 판 출신 사연이 대기열에 동시에 쌓일 수 있는 수
STALE_H = 48          # 대기열에서 이만큼 묵은 판 사연은 정리
MIN_GAP_MIN = 90      # 자동 작성 사이 최소 간격(분) — 2시간 주기에서 약간의 여유
MIN_EXCERPTS = 2      # 원문에서 확인된 발췌문이 이보다 적으면 해설 글감으로 쓰지 않는다
MAX_PER_RUN = 3       # 자동 예약 1회에서 판 출신 사연이 차지할 수 있는 최대 편수(직접 넣은 사연은 제한 없음)
_LOCK = threading.Lock()

_DT = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2}))?")


def _norm_title(t: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]", "", t or "").lower()


def _when(head: str, fallback: float) -> float:
    """스냅샷 제목줄에서 가장 늦은 날짜·시각을 읽는다(없으면 fallback)."""
    best = None
    for m in _DT.finditer(head):
        y, mo, d, h, mi = m.groups()
        try:
            t = dt.datetime(int(y), int(mo), int(d), int(h or 6), int(mi or 0)).timestamp()
        except ValueError:
            continue
        best = t if best is None else max(best, t)
    return best if best is not None else fallback


def parse(path, kind: str) -> list:
    """파일 하나 → 글 목록 [{id,url,title,body,rank,kind,when}] (스냅샷마다 반복 포함)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        mtime = path.stat().st_mtime
    except Exception:
        return []
    out = []
    for snap in re.split(r"(?m)^##[ \t]+", text)[1:]:
        head, _, rest = snap.partition("\n")
        when = _when(head, mtime)
        for blk in re.split(r"(?m)^###[ \t]+", rest)[1:]:
            mr = re.match(r"(\d+)\s*위", blk)
            mt = re.search(r"\*\*게시글 제목:\*\*\s*(.+?)\s*$", blk, re.M)
            mu = re.search(r"\*\*원문 URL:\*\*\s*(\S+)", blk)
            if not (mt and mu):
                continue
            body = blk[mu.end():]
            body = re.sub(r"(?m)^\s*---\s*$", "", body).strip()
            pid = re.search(r"/talk/(\d+)", mu.group(1))
            out.append({"id": pid.group(1) if pid else "t" + _norm_title(mt.group(1))[:40],
                        "url": mu.group(1), "title": mt.group(1).strip(), "body": body,
                        "rank": int(mr.group(1)) if mr else 99, "kind": kind, "when": when})
    return out


def _load() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"seen": {}, "titles": {}}


def _save(st: dict):
    # 오래 쌓이지 않게 최근 3000건만
    if len(st["seen"]) > 3000:
        keep = sorted(st["seen"].items(), key=lambda kv: kv[1].get("t", 0))[-3000:]
        st["seen"] = dict(keep)
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def candidates() -> list:
    """두 파일의 최근 글을 글 번호로 합쳐 우선순위 순으로. 같은 글이 여러 번 나오면 가장 좋은 순위·일간 쪽을 쓴다."""
    cutoff = time.time() - MAX_AGE_H * 3600
    best: dict = {}
    for kind, path in (("daily", config.PANN_DAILY_FILE), ("hour", config.PANN_HOUR_FILE)):
        for e in parse(path, kind):
            if e["when"] < cutoff or not e["body"]:
                continue
            old = best.get(e["id"])
            key = (e["kind"] != "daily", e["rank"], -e["when"])
            if old is None or key < old[0]:
                best[e["id"]] = (key, e)
    return [e for _, e in sorted(best.values(), key=lambda x: x[0])]


def queued_count() -> int:
    return sum(1 for s in stories.all_items() if s.get("pann_id"))


def sync(log=None, max_analyze: int = MAX_ANALYZE) -> dict:
    """새 인기글을 익명 요약해 사연 대기열에 넣는다. 결과 집계를 돌려준다."""
    from . import autopilot

    log = log or (lambda m: None)
    res = {"found": 0, "new": 0, "queued": 0, "dup": 0, "skipped": 0, "left": 0, "files": {}}
    res["errors"] = []
    for kind, path in (("daily", config.PANN_DAILY_FILE), ("hour", config.PANN_HOUR_FILE)):
        res["files"][kind] = path.exists()
        if path.exists():
            try:
                path.read_text(encoding="utf-8", errors="replace")
            except Exception as ex:
                # 서버 프로세스에 '구글 드라이브 폴더' 읽기 권한(전체 디스크 접근)이 없으면 PermissionError가 난다
                res["errors"].append(f"{kind}: {type(ex).__name__} {str(ex)[:60]}")
    if res["errors"]:
        log("  · 판 인기글 파일을 읽지 못했습니다 — " + "; ".join(res["errors"]))
    # 대기열에서 이틀 넘게 묵은 판 사연은 버린다(이미 한 번 본 글이라 다시 들어오지 않는다)
    for s in stories.all_items():
        if s.get("pann_id") and (time.time() - s.get("added", 0) > STALE_H * 3600 or "excerpts" not in s):
            stories.remove(s["id"])      # 묵은 것, 그리고 발췌 방식 도입 전에 요약만 해 둔 것
    with _LOCK:
        st = _load()
        seen, titles = st["seen"], st["titles"]
        cands = candidates()
        res["found"] = len(cands)
        fresh = []
        for e in cands:
            if e["id"] in seen:
                continue
            tk = _norm_title(e["title"])
            if tk and tk in titles:
                seen[e["id"]] = {"t": int(time.time()), "status": "dup", "why": "같은 제목의 글"}
                res["dup"] += 1
                continue
            fresh.append(e)
        res["new"] = len(fresh)
        room = max(0, MAX_QUEUED - queued_count())
        budget = min(max_analyze, room)
        used = None
        for e in fresh:
            if len(e["body"]) < stories.MIN_CHARS:
                seen[e["id"]] = {"t": int(time.time()), "status": "skip", "why": "본문이 짧음"}
                res["skipped"] += 1
                continue
            if budget <= 0:
                res["left"] += 1     # 다음에 다시 본다(아직 seen에 넣지 않는다)
                continue
            budget -= 1
            tk = _norm_title(e["title"])
            try:
                info = stories.analyze(e["body"], config.audience_for())
            except stories.Unsuitable as ex:
                seen[e["id"]] = {"t": int(time.time()), "status": "skip", "why": str(ex)[:60]}
                res["skipped"] += 1
                log(f"  · 판 {e['id']} 제외: {str(ex)[:50]}")
                continue
            except Exception as ex:
                log(f"  · 판 {e['id']} 요약 실패({str(ex)[:50]}) — 다음에 다시 시도")
                res["left"] += 1
                continue
            if len(info.get("excerpts") or []) < MIN_EXCERPTS:
                seen[e["id"]] = {"t": int(time.time()), "status": "skip", "why": "발췌할 만한 대목이 부족"}
                res["skipped"] += 1
                log(f"  · 판 {e['id']} 제외: 원문에서 확인된 발췌문 {len(info.get('excerpts') or [])}개")
                continue
            if used is None:
                used = autopilot.used_titles() + [s["query"] for s in stories.all_items()]
            if autopilot._too_similar(info["query"], used):
                seen[e["id"]] = {"t": int(time.time()), "status": "dup", "why": "이미 쓴 소재와 비슷"}
                titles[tk] = e["id"]
                res["dup"] += 1
                log(f"  · 판 {e['id']} 제외: 이미 쓴 소재와 비슷 — {info['query'][:30]}")
                continue
            stories.add(info, ref=e["url"], extra={"pann_id": e["id"], "pann_kind": e["kind"], "pann_rank": e["rank"]})
            used.append(info["query"])
            seen[e["id"]] = {"t": int(time.time()), "status": "queued", "why": ""}
            titles[tk] = e["id"]
            res["queued"] += 1
            log(f"  · 판 {e['kind']} {e['rank']}위 → 사연 대기열: {info['query'][:34]}")
        _save(st)
    return res


def status() -> dict:
    st = _load()
    c = {}
    for v in st["seen"].values():
        c[v.get("status", "?")] = c.get(v.get("status", "?"), 0) + 1
    latest = {}
    for kind, path in (("daily", config.PANN_DAILY_FILE), ("hour", config.PANN_HOUR_FILE)):
        err = ""
        try:
            path.read_text(encoding="utf-8", errors="replace")
        except Exception as ex:    # 서버 프로세스에 '구글 드라이브 폴더 접근' 권한이 없으면 여기서 막힌다
            err = f"{type(ex).__name__}: {str(ex)[:80]}"
        es = parse(path, kind) if path.exists() and not err else []
        latest[kind] = {"exists": path.exists(), "error": err,
                        "latest": (dt.datetime.fromtimestamp(max(e["when"] for e in es)).strftime("%m-%d %H:%M")
                                   if es else "")}
    return {"seen": c, "queued": queued_count(), "files": latest}


# ─────────────────────────────────────────────────────────────
# 2시간 자동 작성 — 새 스냅샷이 파일에 생기면 사연 한 편을 쓴다 (상태는 pann_auto.json)
# ─────────────────────────────────────────────────────────────

AUTO_FILE = config.ROOT / "pann_auto.json"


def auto_state() -> dict:
    st = {"enabled": True, "last_cycle": 0, "last_snapshot": 0, "message": ""}
    try:
        st.update(json.loads(AUTO_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass
    return st


def auto_save(**kw) -> dict:
    st = auto_state()
    st.update(kw)
    AUTO_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    return st


def unreadable() -> list:
    """읽을 수 없는 파일 목록(없거나 권한 문제)."""
    bad = []
    for kind, path in (("daily", config.PANN_DAILY_FILE), ("hour", config.PANN_HOUR_FILE)):
        try:
            path.read_text(encoding="utf-8", errors="replace")
        except Exception as ex:
            bad.append(f"{kind}: {type(ex).__name__}")
    return bad


def latest_snapshot() -> float:
    """두 파일을 통틀어 가장 최근 수집 시각(없으면 0)."""
    best = 0.0
    for kind, path in (("daily", config.PANN_DAILY_FILE), ("hour", config.PANN_HOUR_FILE)):
        for e in parse(path, kind):
            best = max(best, e["when"])
    return best


def auto_due() -> tuple:
    """(지금 한 편 쓸 때인가, 이유). 새 스냅샷이 있고 지난 작성에서 MIN_GAP_MIN분이 지났을 때."""
    st = auto_state()
    if not st["enabled"]:
        return False, "꺼짐"
    bad = unreadable()
    if bad:
        return False, "판 인기글 파일을 읽지 못함 — " + ", ".join(bad)
    snap = latest_snapshot()
    if snap <= st["last_snapshot"]:
        return False, "새 스냅샷 없음"
    if time.time() - st["last_cycle"] < MIN_GAP_MIN * 60:
        return False, "지난 작성 후 간격이 짧음"
    return True, snap
