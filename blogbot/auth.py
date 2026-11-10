"""공개 URL(터널) 접속용 비밀번호 로그인.

설계:
- 공개 터널(Tailscale Funnel 등)은 '공개 포트'(기본 8766, 127.0.0.1에만 바인딩)로만 연결한다.
  그 포트로 들어온 요청은 예외 없이 로그인이 필요하다. 어느 포트로 들어왔는지는 서버 소켓이
  정하므로 요청 헤더로 속일 수 없다.
- 기존 주소(8765: 로컬·같은 Wi-Fi·Tailscale 사설 주소)는 이전처럼 로그인 없이 쓴다.
  단, 프록시를 거친 흔적(X-Forwarded-For 등)이 있으면 공개 요청으로 취급한다(이중 안전장치).
- 비밀번호는 PBKDF2-SHA256 해시로만 .auth.json에 저장한다(평문 저장 안 함, git 제외).
- 비밀번호가 없으면 공개 요청은 전부 차단한다(fail-closed).
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import threading
import time

from itsdangerous import BadSignature, URLSafeTimedSerializer

from . import config

AUTH_FILE = config.ROOT / ".auth.json"
PUBLIC_PORT = int(os.getenv("BLOGBOT_PUBLIC_PORT", "8766"))
COOKIE = "bb_session"
SESSION_DAYS = 30
MIN_LENGTH = 10
_ITER = 300_000

# 로그인 실패 제한: IP당 5회 실패 → 15분 잠금, 전체 30회/시간 → 15분 잠금
_MAX_FAIL_IP = 5
_MAX_FAIL_ALL = 30
_LOCK_SEC = 900
_WINDOW_SEC = 3600

_lock = threading.Lock()
_fails: dict = {}          # ip -> [실패 시각...]
_locked_until: dict = {}   # ip 또는 "*" -> 잠금 해제 시각

_FORWARD_HEADERS = ("X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "Forwarded",
                    "Cf-Connecting-Ip", "Cf-Ray", "Tailscale-Funnel-Request", "X-Real-Ip")


def _load() -> dict:
    try:
        return json.loads(AUTH_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(data: dict):
    tmp = AUTH_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(AUTH_FILE)


def has_password() -> bool:
    d = _load()
    return bool(d.get("hash") and d.get("salt"))


def _hash(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITER).hex()


def check_strength(password: str) -> str:
    """문제가 있으면 사유(한글), 괜찮으면 빈 문자열."""
    if len(password) < MIN_LENGTH:
        return f"비밀번호는 {MIN_LENGTH}자 이상이어야 합니다."
    if len(set(password)) < 5:
        return "같은 글자가 너무 많이 반복됩니다."
    if password.lower() in ("password123", "1234567890", "qwertyuiop", "blogbot1234"):
        return "너무 흔한 비밀번호입니다."
    return ""


def set_password(password: str):
    """비밀번호를 새로 정한다. 서명 키도 새로 만들어 기존 로그인은 모두 풀린다."""
    salt = secrets.token_bytes(16)
    _save({"salt": salt.hex(), "hash": _hash(password, salt), "iter": _ITER,
           "secret": secrets.token_hex(32), "set_at": int(time.time())})


def verify(password: str) -> bool:
    d = _load()
    if not (d.get("hash") and d.get("salt")):
        return False
    try:
        calc = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(d["salt"]),
                                   int(d.get("iter") or _ITER)).hex()
    except Exception:
        return False
    return hmac.compare_digest(calc, d["hash"])


def _signer():
    d = _load()
    if not d.get("secret"):
        return None
    return URLSafeTimedSerializer(d["secret"], salt="bb-session-" + d.get("hash", "")[:16])


def make_token() -> str:
    s = _signer()
    return s.dumps({"v": 1}) if s else ""


def token_ok(token: str) -> bool:
    s = _signer()
    if not s or not token:
        return False
    try:
        s.loads(token, max_age=SESSION_DAYS * 86400)
        return True
    except (BadSignature, Exception):
        return False


# ───────────────────────── 요청 분류 ─────────────────────────

def is_public(request) -> bool:
    """공개 터널을 거쳐 온 요청인가."""
    if str(request.environ.get("SERVER_PORT")) == str(PUBLIC_PORT):
        return True
    return any(request.headers.get(h) for h in _FORWARD_HEADERS)


def is_private_direct(request) -> bool:
    """터널을 거치지 않고 로컬·사설망(같은 Wi-Fi, Tailscale 100.64/10)에서 직접 온 요청인가."""
    if is_public(request):
        return False
    try:
        ip = ipaddress.ip_address((request.remote_addr or "").split("%")[0])
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip in ipaddress.ip_network("100.64.0.0/10")


def client_ip(request) -> str:
    xff = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    return xff or request.headers.get("Cf-Connecting-Ip") or request.remote_addr or "?"


def same_origin(request) -> bool:
    """상태를 바꾸는 요청의 Origin이 이 사이트인지 (다른 사이트가 대신 보내는 요청 차단)."""
    origin = request.headers.get("Origin")
    if not origin:
        return True   # 폼 전송·같은 출처 fetch 일부는 Origin이 없다 — 쿠키 SameSite=Lax가 막아 준다
    host = (request.headers.get("X-Forwarded-Host") or request.host or "").split(",")[0].strip().lower()
    try:
        from urllib.parse import urlparse

        return (urlparse(origin).netloc or "").lower() == host
    except Exception:
        return False


# ───────────────────────── 실패 제한 ─────────────────────────

def locked_for(ip: str) -> int:
    """잠겨 있으면 남은 초, 아니면 0."""
    now = time.time()
    with _lock:
        return int(max(_locked_until.get(ip, 0), _locked_until.get("*", 0)) - now) \
            if max(_locked_until.get(ip, 0), _locked_until.get("*", 0)) > now else 0


def note_fail(ip: str):
    now = time.time()
    with _lock:
        for key in (ip, "*"):
            lst = [t for t in _fails.get(key, []) if now - t < _WINDOW_SEC] + [now]
            _fails[key] = lst
            if len(lst) >= (_MAX_FAIL_ALL if key == "*" else _MAX_FAIL_IP):
                _locked_until[key] = now + _LOCK_SEC
                _fails[key] = []


def note_success(ip: str):
    with _lock:
        _fails.pop(ip, None)
        _locked_until.pop(ip, None)
