"""뉴스 사이트 로그인 세션을 저장해두고, 그 세션으로 기사 본문을 가져온다.

NYT 같은 로그인 페이월 사이트는 서버가 직접 fetch하면 짧은 프리뷰만 받는다.
사용자가 Mac에서 한 번 로그인해 두면(login), 그 쿠키가 .news_profile/에 저장되고,
이후 fetch_html()이 headless로 같은 프로필을 써서 전체 본문을 가져온다.
(네이버 발행 프로필과 동일한 launch_persistent_context 패턴)
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

from . import config

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def _kill_pids_by_pattern(pattern: str):
    """pgrep -f 패턴으로 프로세스를 찾아 SIGTERM → SIGKILL 순으로 종료한다."""
    try:
        out = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True, timeout=5)
        pids = [int(p) for p in out.stdout.split() if p.isdigit() and int(p) != os.getpid()]
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        if pids:
            time.sleep(0.8)
            out2 = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True, timeout=5)
            for p in out2.stdout.split():
                try:
                    pid = int(p)
                    if pid != os.getpid():
                        os.kill(pid, signal.SIGKILL)
                except (ValueError, ProcessLookupError, PermissionError):
                    pass
    except Exception:
        pass


def _cleanup_profile_lock():
    """이전 실행이 남긴 프로필 잠금(SingletonLock 등)과 유령 프로세스 정리."""
    profile = config.NEWS_PROFILE_DIR
    if profile.exists():
        _kill_pids_by_pattern(str(profile))
    for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        f = profile / lock_name
        try:
            if f.exists() or f.is_symlink():
                f.unlink()
        except Exception:
            pass


_LOGIN_MARKER = "._logged_in"


def has_session() -> bool:
    """사용자가 명시적으로 로그인을 완료했는지 판단.

    단순 fetch만 해도 쿠키 DB는 생기므로, login()이 완료 시 남기는 마커 파일로 판단한다.
    """
    return (config.NEWS_PROFILE_DIR / _LOGIN_MARKER).exists()


def login(timeout_sec: int = 600, start_url: str = "https://www.nytimes.com/"):
    """헤디드 브라우저를 열어 사용자가 뉴스 사이트에 직접 로그인하게 한다.

    사용자가 창을 닫거나 timeout이 되면 종료. 쿠키는 프로필에 자동 저장된다.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        _cleanup_profile_lock()
        ctx = p.chromium.launch_persistent_context(
            str(config.NEWS_PROFILE_DIR),
            headless=False,
            viewport={"width": 1200, "height": 820},
            user_agent=_UA,
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(start_url, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass
        # 사용자가 로그인 후 창을 닫을 때까지(또는 timeout) 대기
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            try:
                if not ctx.pages:  # 모든 탭이 닫힘 = 사용자가 종료
                    break
            except Exception:
                break
            time.sleep(2)
        try:
            ctx.close()
        except Exception:
            pass
        # 로그인 완료 마커 (사용자가 창을 닫으면 로그인했다고 간주)
        try:
            config.NEWS_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
            (config.NEWS_PROFILE_DIR / _LOGIN_MARKER).write_text("1", encoding="utf-8")
        except Exception:
            pass


def fetch_html(url: str, timeout_ms: int = 30000) -> str:
    """저장된 로그인 프로필로 headless 브라우저를 띄워 기사 HTML을 가져온다."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        _cleanup_profile_lock()
        ctx = p.chromium.launch_persistent_context(
            str(config.NEWS_PROFILE_DIR),
            headless=True,
            user_agent=_UA,
            locale="en-US",
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            try:
                page.wait_for_load_state("networkidle", timeout=3500)
            except Exception:
                pass
            html = page.content()
            return html
        finally:
            try:
                ctx.close()
            except Exception:
                pass
