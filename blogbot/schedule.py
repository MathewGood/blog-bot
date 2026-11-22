"""평일 정기 자동 예약 — 정해진 시각에 자동 예약(autopilot)을 스스로 시작한다.

2026-11-22 사용자 지정:
- 월~금 06:30, 17:30에 '30~40대 직장인' 대상 10편을 시작한다.
- 첫 예약은 시작 약 40분 뒤, 이후 평균 13분 간격(네이버 10분 격자).
- 한국 공휴일(대체공휴일 포함)은 건너뛴다.

상태는 autopilot_schedule.json(git 제외)에 둔다. 실행 판단만 여기서 하고,
실제 시작은 web.py가 자동 예약 시작 경로(_autopilot_generate/_autopilot_publish)로 한다.
"""

from __future__ import annotations

import datetime as dt
import json
import threading

from . import config

STATE_FILE = config.ROOT / "autopilot_schedule.json"
WINDOW_MIN = 60          # 정각에 못 시작하면(다른 자동 예약 진행 중·서버 재시작 등) 이 시간 안에서 다시 시도
WEEKDAY_KO = "월화수목금토일"

DEFAULT = {
    "enabled": True,
    "slots": ["06:30", "17:30"],
    "weekdays": [0, 1, 2, 3, 4],      # 월~금
    "skip_holidays": True,
    "count": 10,
    "audience": "worker_young",
    "lead_min": 40,                   # 시작 → 첫 예약까지
    "interval": 13,                   # 글 사이 평균 간격(분)
    "runs": {},                       # "2026-11-23 06:30" -> {"status", "at", "message"}
}

_lock = threading.Lock()


def load() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    out = dict(DEFAULT)
    out.update({k: v for k, v in data.items() if k in DEFAULT})
    out["runs"] = dict(out.get("runs") or {})
    return out


def save(**changes) -> dict:
    with _lock:
        st = load()
        st.update(changes)
        # 기록은 최근 60건만
        runs = st.get("runs") or {}
        st["runs"] = dict(sorted(runs.items())[-60:])
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(STATE_FILE)
        return st


def mark(key: str, status: str, message: str = "") -> dict:
    st = load()
    runs = st["runs"]
    runs[key] = {"status": status, "at": dt.datetime.now().strftime("%m/%d %H:%M"), "message": message[:200]}
    return save(runs=runs)


def holiday_name(day: dt.date) -> str:
    """한국 공휴일이면 이름, 아니면 빈 문자열. 패키지가 없으면 빈 문자열(평일로 취급)."""
    try:
        import holidays

        return holidays.KR(years=day.year).get(day) or ""
    except Exception:
        return ""


def skip_reason(day: dt.date, st: dict = None) -> str:
    st = st or load()
    if day.weekday() not in st["weekdays"]:
        return f"{WEEKDAY_KO[day.weekday()]}요일"
    if st.get("skip_holidays"):
        name = holiday_name(day)
        if name:
            return f"공휴일({name})"
    return ""


def _slot_dt(day: dt.date, hhmm: str) -> dt.datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return dt.datetime.combine(day, dt.time(h, m))


def due(now: dt.datetime = None) -> tuple:
    """지금 시작해야 할 슬롯 → (key, 슬롯 시각) 또는 (None, None).

    슬롯 시각 ~ WINDOW_MIN분 사이이고, 아직 시작·건너뜀 처리가 안 된 경우만."""
    now = now or dt.datetime.now()
    st = load()
    if not st["enabled"]:
        return None, None
    for hhmm in st["slots"]:
        at = _slot_dt(now.date(), hhmm)
        key = f"{now.date().isoformat()} {hhmm}"
        if at <= now < at + dt.timedelta(minutes=WINDOW_MIN) and \
                st["runs"].get(key, {}).get("status") not in ("started", "skipped", "missed"):
            return key, at
    return None, None


def expire_missed(now: dt.datetime = None):
    """창(WINDOW_MIN)이 지나도록 시작하지 못한 오늘 슬롯을 '놓침'으로 기록한다."""
    now = now or dt.datetime.now()
    st = load()
    if not st["enabled"]:
        return
    for hhmm in st["slots"]:
        at = _slot_dt(now.date(), hhmm)
        key = f"{now.date().isoformat()} {hhmm}"
        rec = st["runs"].get(key, {})
        if now >= at + dt.timedelta(minutes=WINDOW_MIN) and rec.get("status") == "waiting":
            mark(key, "missed", rec.get("message") or "시작하지 못함")


def next_run(now: dt.datetime = None) -> tuple:
    """다음 실행 예정 (시각, 건너뛴 날 목록 문구)."""
    now = now or dt.datetime.now()
    st = load()
    skipped = []
    for d in range(0, 15):
        day = now.date() + dt.timedelta(days=d)
        why = skip_reason(day, st)
        for hhmm in sorted(st["slots"]):
            at = _slot_dt(day, hhmm)
            key = f"{day.isoformat()} {hhmm}"
            if at + dt.timedelta(minutes=WINDOW_MIN) <= now or \
                    st["runs"].get(key, {}).get("status") in ("started", "skipped", "missed"):
                continue
            if why:
                if why.startswith("공휴일"):
                    skipped.append(f"{day:%m/%d} {why}")
                break
            return at, skipped
    return None, skipped


def summary() -> dict:
    st = load()
    nxt, skipped = next_run()
    aud = config.AUDIENCES.get(st["audience"], {}).get("label", st["audience"])
    recent = sorted(st["runs"].items())[-6:]
    return {
        "enabled": st["enabled"], "slots": st["slots"], "count": st["count"], "audience": aud,
        "lead_min": st["lead_min"], "interval": st["interval"],
        "weekdays": "".join(WEEKDAY_KO[i] for i in st["weekdays"]), "skip_holidays": st["skip_holidays"],
        "next": nxt.strftime("%m/%d(%a) %H:%M").replace("(Mon)", "(월)").replace("(Tue)", "(화)")
                   .replace("(Wed)", "(수)").replace("(Thu)", "(목)").replace("(Fri)", "(금)")
                   .replace("(Sat)", "(토)").replace("(Sun)", "(일)") if nxt else "",
        "skipped_holidays": skipped[:3],
        "recent": [{"slot": k, **v} for k, v in reversed(recent)],
    }
