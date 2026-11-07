"""Anthropic API 크레딧 소진을 감지하고, 반복 실패를 막는 차단기(circuit breaker).

크레딧이 떨어지면 조사·생성·이미지 선별·교정이 전부 400으로 실패한다.
한 번 감지되면 일정 시간 Anthropic 호출을 건너뛰고(쿨다운), 호출측은
Kimi로 자동 전환하거나 해당 단계를 생략한다. 쿨다운이 지나면 다시 시도한다.
"""

from __future__ import annotations

import threading
import time

from . import config

COOLDOWN_SEC = 600  # 크레딧 부족 감지 후 10분간 Anthropic 호출 생략

_lock = threading.Lock()
_blocked_until = 0.0
_last_reason = ""


class CreditExhausted(RuntimeError):
    """Anthropic 크레딧 부족으로 호출할 수 없음."""


def is_credit_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "credit balance is too low" in msg or "purchase credits" in msg


def mark_credit_out(reason: str = "크레딧 부족"):
    global _blocked_until, _last_reason
    with _lock:
        _blocked_until = time.time() + COOLDOWN_SEC
        _last_reason = reason


def mark_ok():
    global _blocked_until, _last_reason
    with _lock:
        _blocked_until = 0.0
        _last_reason = ""


def anthropic_available() -> bool:
    """키가 있고 차단(쿨다운) 중이 아니면 True."""
    if not config.ANTHROPIC_API_KEY:
        return False
    with _lock:
        return time.time() >= _blocked_until


def status() -> dict:
    with _lock:
        remaining = max(0, int(_blocked_until - time.time()))
        return {
            "ok": bool(config.ANTHROPIC_API_KEY) and remaining == 0,
            "blocked_sec": remaining,
            "reason": _last_reason if remaining else "",
        }


def check_and_raise(exc: Exception):
    """크레딧 오류면 차단기를 걸고 CreditExhausted로 바꿔 던진다. 아니면 그대로 재던짐."""
    if is_credit_error(exc):
        mark_credit_out("Anthropic 크레딧 부족")
        raise CreditExhausted(
            "Anthropic API 크레딧이 부족합니다. "
            "platform.claude.com → Billing에서 충전하세요."
        ) from exc
    raise exc


# ─────────────────────────────────────────────────────────────
# 이미지 API(OpenAI·Cloudflare) 크레딧/한도/키 오류 감지
#  - 잔액 조회 API가 없으므로, 실제 호출이 한도 오류로 실패했을 때 상태를 기록한다.
#  - 크레딧 부족이면 쿨다운 동안 해당 엔진 호출을 건너뛰고 다음 방식으로 넘어간다.
# ─────────────────────────────────────────────────────────────

PROVIDER_LABEL = {"openai": "OpenAI", "cloudflare": "Cloudflare"}
_PROVIDER_COOLDOWN = {"credit": 1800, "auth": 600}   # 초
_prov: dict = {}   # name -> {"kind": "credit"|"auth", "until": ts, "reason": str, "at": ts}


def classify_provider_error(status_code: int, text: str) -> str:
    """HTTP 오류를 'credit'(크레딧·한도 소진) / 'auth'(키 오류) / ''(그 외)로 분류."""
    t = (text or "").lower()
    if "insufficient_quota" in t or "billing_hard_limit" in t or "exceeded your current quota" in t:
        return "credit"
    if status_code in (402, 429) and any(k in t for k in ("credit", "quota", "neurons", "allocation")):
        return "credit"
    if status_code in (401, 403):
        return "auth"
    return ""


def note_provider_error(name: str, status_code: int, text: str) -> str:
    """실패 응답을 기록한다. 분류 결과('credit'/'auth'/'')를 돌려준다."""
    kind = classify_provider_error(status_code, text)
    if kind:
        now = time.time()
        label = PROVIDER_LABEL.get(name, name)
        reason = (f"{label} 크레딧(한도) 소진" if kind == "credit" else f"{label} API 키 오류")
        with _lock:
            _prov[name] = {"kind": kind, "until": now + _PROVIDER_COOLDOWN[kind],
                           "reason": reason, "at": now}
    return kind


def note_provider_ok(name: str):
    with _lock:
        _prov.pop(name, None)


def provider_blocked(name: str) -> bool:
    """크레딧 부족·키 오류로 쿨다운 중이면 True."""
    with _lock:
        st = _prov.get(name)
        return bool(st and time.time() < st["until"])


def provider_status(name: str) -> dict:
    with _lock:
        st = _prov.get(name)
        if st and time.time() < st["until"]:
            return {"ok": False, "kind": st["kind"], "reason": st["reason"],
                    "blocked_sec": int(st["until"] - time.time())}
    return {"ok": True, "kind": "", "reason": "", "blocked_sec": 0}
