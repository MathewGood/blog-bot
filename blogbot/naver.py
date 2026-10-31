"""Playwright로 네이버 블로그 글쓰기 화면에 초안을 자동 입력한다.

네이버는 공식 글쓰기 API가 사실상 없으므로 브라우저 자동화를 쓴다.
- `blogbot login` : 처음 한 번, 브라우저에서 직접 로그인 (로그인 완료를 자동 감지)
- `blogbot publish <초안폴더>` : 제목·본문·이미지를 에디터에 채워 넣음.
  기본적으로 발행 버튼은 사용자가 직접 누르고, 발행 완료를 자동 감지해 닫는다.

네이버 UI가 바뀌면 셀렉터 수정이 필요할 수 있다.
"""

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import config


def _kill_pids_by_pattern(pattern: str):
    """pgrep -f 패턴으로 프로세스를 찾아 SIGTERM → SIGKILL 순으로 종료한다."""
    try:
        out = subprocess.run(
            ["pgrep", "-f", pattern],
            capture_output=True, text=True, timeout=5,
        )
        for pid_s in out.stdout.split():
            try:
                pid = int(pid_s)
                if pid == os.getpid():
                    continue  # 자기 자신은 건드리지 않음
                os.kill(pid, signal.SIGTERM)
            except (ValueError, ProcessLookupError, PermissionError):
                pass
        time.sleep(0.8)
        # SIGTERM 후에도 살아있는 것은 SIGKILL
        out2 = subprocess.run(
            ["pgrep", "-f", pattern],
            capture_output=True, text=True, timeout=5,
        )
        for pid_s in out2.stdout.split():
            try:
                pid = int(pid_s)
                if pid == os.getpid():
                    continue
                os.kill(pid, signal.SIGKILL)
            except (ValueError, ProcessLookupError, PermissionError):
                pass
    except Exception:
        pass


def _cleanup_profile_lock():
    """이전 실행이 남긴 프로필 잠금과 유령 Chromium 프로세스를 정리한다.

    macOS Chromium은 user-data-dir이 이미 사용 중이면 새 인스턴스를 띄우지 않고
    기존 창에 요청을 넘긴 뒤 즉시 종료한다. Playwright는 이 상황을 '브라우저가
    닫혔다'는 오류로 본다.
    예방:
      (1) 이 프로필 경로를 가진 Chromium 프로세스 종료
      (2) Playwright 내장 Chromium 프로세스 전체 종료 (모바일 반복 요청으로 쌓인 창 제거)
      (3) SingletonLock 등 잠금 파일 삭제
    """
    profile = config.NAVER_PROFILE_DIR
    # (1) 이 프로필을 쓰고 있는 프로세스 종료
    if profile.exists():
        _kill_pids_by_pattern(str(profile))
    # (2) Playwright 내장 Chromium 전체 정리
    #     (blogbot 프로세스를 제외한 headless/headed Chromium 모두)
    # "playwright/driver"는 넣지 말 것: 호출 시점에 이미 떠 있는 '자기' 드라이버까지 죽여
    # launch_persistent_context가 "Connection closed while reading from the driver"로 실패한다.
    for pat in (".local-chromium", "ms-playwright"):
        _kill_pids_by_pattern(pat)
    # (3) 잠금 파일 정리
    if profile.exists():
        for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            f = profile / lock_name
            try:
                if f.exists() or f.is_symlink():
                    f.unlink()
            except Exception:
                pass


def _mark_clean_exit():
    """이전 실행이 강제 종료됐어도 '페이지를 복원하시겠습니까?' 말풍선이 뜨지 않게
    프로필 Preferences의 종료 상태를 정상으로 되돌린다."""
    pref = config.NAVER_PROFILE_DIR / "Default" / "Preferences"
    try:
        data = json.loads(pref.read_text(encoding="utf-8"))
        prof = data.setdefault("profile", {})
        if prof.get("exit_type") != "Normal" or not prof.get("exited_cleanly", True):
            prof["exit_type"] = "Normal"
            prof["exited_cleanly"] = True
            pref.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass


def _launch(playwright, headless: bool = False):
    _cleanup_profile_lock()
    _mark_clean_exit()
    return playwright.chromium.launch_persistent_context(
        str(config.NAVER_PROFILE_DIR),
        headless=headless,
        viewport={"width": 1400, "height": 900},
        args=[
            "--disable-blink-features=AutomationControlled",
            "--hide-crash-restore-bubble",
        ],
    )


# 새 발행 요청이 들어오면 이전 발행 창을 정상 종료시키기 위한 신호
_cancel = threading.Event()


class PublishCancelled(Exception):
    pass


def request_cancel():
    """진행 중인 publish()에 종료를 요청한다 (다음 확인 지점에서 창을 정상적으로 닫음)."""
    _cancel.set()


def _check_cancel():
    if _cancel.is_set():
        raise PublishCancelled("새 발행 요청이 들어와 이 발행 창을 닫았습니다.")


def _is_logged_in(ctx) -> bool:
    """네이버 로그인 세션 쿠키(NID_AUT) 존재 여부."""
    try:
        return any(c["name"] == "NID_AUT" for c in ctx.cookies("https://www.naver.com"))
    except Exception:
        return False


LOGIN_HELP = (
    "네이버 로그인이 만료되었습니다. 대시보드 ③초안·발행 탭의 [🔑 네이버 로그인]을 누르고, "
    "Mac에 열리는 창에서 '로그인 상태 유지'를 체크한 뒤 로그인하세요."
)


def session_status() -> dict:
    """브라우저를 띄우지 않고 쿠키 DB만 읽어 네이버 세션 상태를 판단한다.

    반환: {"ok": bool, "persistent": bool, "expires": epoch초 또는 None}
    NID_AUT가 '세션 쿠키'(expires 0)면 브라우저를 닫을 때 사라지므로
    '로그인 상태 유지' 없이 로그인한 것 — 다음 발행 때 다시 로그인해야 한다.
    """
    import sqlite3

    db = config.NAVER_PROFILE_DIR / "Default" / "Cookies"
    if not db.exists():
        return {"ok": False, "persistent": False, "expires": None}
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        row = con.execute(
            "select expires_utc, has_expires from cookies "
            "where name='NID_AUT' and host_key like '%naver.com' "
            "order by expires_utc desc limit 1"
        ).fetchone()
        con.close()
    except Exception:
        return {"ok": False, "persistent": False, "expires": None}
    if not row:
        return {"ok": False, "persistent": False, "expires": None}
    expires_utc, has_expires = row
    if not has_expires or not expires_utc:
        return {"ok": True, "persistent": False, "expires": None}
    exp = expires_utc / 1_000_000 - 11644473600  # Chrome epoch(1601) → Unix
    return {"ok": exp > time.time(), "persistent": True, "expires": int(exp)}



# ─────────────────────────────────────────────────────────────
# 서식 입력 도우미 (미리보기 preview.html과 같은 모양으로 올리기)
# ─────────────────────────────────────────────────────────────

# 미리보기(preview.html)와 같은 색·크기. 블로그 스킨 기본색(#777)보다 진하게 명시한다.
# font-size를 명시해야 한다: 생략하면 에디터가 커서 위치의 서식(직전 소제목·강조 문구의 19px)을
# 본문이 물려받아 글 중간부터 크기가 커진다 (2026-10-28 실측, 사진을 사이에 넣은 글에서 재현).
# 2026-10-30 사용자 지정: 전체 글자 크기를 한 단계씩 올림(본문 15→16, 소제목 19→24, 출처 13→15, 강조 21→24).
_BODY_STYLE = "color:#222222;line-height:2.2;font-size:16px"
_HEADING_STYLE = "font-size:24px;color:#222222"
_SOURCE_STYLE = "font-size:15px;color:#888888"
_HIGHLIGHT_STYLE = "font-size:24px;color:#1b7a3d;text-align:center;line-height:1.8"
_EMPTY_P = "<p><br></p>"


def hashtag(t: str) -> str:
    """네이버는 공백에서 해시태그를 끊으므로 태그 안 공백을 없앤다 ('에너지 위기' → #에너지위기)."""
    return "#" + "".join(str(t).split())


def _md_to_html(text: str) -> str:
    """본문 마크다운 → 스마트에디터가 이해하는 단순 HTML (굵게는 <b>로)."""
    import markdown as md

    html = md.markdown(text)
    html = (html.replace("<strong>", "<b>").replace("</strong>", "</b>")
                .replace("<em>", "<i>").replace("</em>", "</i>"))
    return (html.replace("<p>", f'<p style="{_BODY_STYLE}">')
                .replace("<li>", f'<li style="{_BODY_STYLE}">'))


def section_html(sec: dict, first: bool = False) -> str:
    """섹션 하나 → 소제목(h2) + 문단들. 문단 사이는 빈 줄로 띄워 가독성 유지."""
    from html import escape

    parts = []
    # 스마트에디터는 붙여넣은 조각 '끝'의 빈 문단을 버리므로, 섹션 사이 간격은
    # 다음 섹션 '앞'에 빈 문단을 둬서 만든다 (소제목 없는 섹션 포함).
    if not first:
        parts.append(_EMPTY_P)
    heading = (sec.get("heading") or "").strip()
    if heading:
        # 소제목: 게시 화면에서도 굵게 보이도록 <b>를 명시 (h2만으로는 보통 굵기로 게시됨)
        parts.append(f'<p style="{_HEADING_STYLE}"><b>{escape(heading)}</b></p>')
    hl = (sec.get("highlight") or "").strip()
    for k, para in enumerate([x for x in (sec.get("body_md") or "").split("\n\n") if x.strip()]):
        parts.append(_md_to_html(para.strip()))
        parts.append(_EMPTY_P)
        if k == 0 and hl:
            # 강조 문구: 첫 문단 뒤에 큰 글씨 따옴표 인용구로
            parts.append(f'<p style="{_HIGHLIGHT_STYLE}"><b>\u201c{escape(hl)}\u201d</b></p>')
            parts.append(_EMPTY_P)
    return "".join(parts)


def footer_html(post: dict) -> str:
    from html import escape

    parts = []
    source_line = (post.get("source_line") or "").strip()
    if source_line:
        parts.append(f'<hr><p style="{_SOURCE_STYLE}">{escape(source_line)}</p>')
    if config.AI_IMAGE_NOTICE and any(m.get("ai_generated") for m in post.get("image_files", [])):
        parts.append(f'<p style="{_SOURCE_STYLE}">{escape(config.AI_NOTICE_TEXT)}</p>')
    tags = " ".join(hashtag(t) for t in post.get("tags", []))
    if tags:
        # 색은 지정하지 않는다 — 네이버가 해시태그 링크 색으로 통일해 표시
        parts.append(f'{_EMPTY_P}<p style="font-size:16px">{escape(tags)}</p>')
    return "".join(parts)


# ─────────────────────────────────────────────────────────────
# 발행 설정 창: 태그·공개범위·카테고리·주제
# ─────────────────────────────────────────────────────────────

# 네이버 블로그 공통 '주제' 목록 (발행 창의 주제 설정, 2026-10 확인). 블로그마다 같다.
NAVER_TOPICS = {
    "엔터테인먼트·예술": ["문학·책", "영화", "미술·디자인", "공연·전시", "음악", "드라마",
                      "스타·연예인", "만화·애니", "방송"],
    "생활·노하우·쇼핑": ["일상·생각", "육아·결혼", "반려동물", "좋은글·이미지", "패션·미용",
                     "인테리어·DIY", "요리·레시피", "상품리뷰", "원예·재배"],
    "취미·여가·여행": ["게임", "스포츠", "사진", "자동차", "취미", "국내여행", "세계여행", "맛집"],
    "지식·동향": ["IT·컴퓨터", "사회·정치", "건강·의학", "비즈니스·경제", "어학·외국어", "교육·학문"],
}
NO_TOPIC = "주제 선택 안 함"
CATEGORY_CACHE = config.ROOT / "naver_options.json"


def _open_publish_panel(frame):
    """오른쪽 위 [발행]을 눌러 발행 설정 창을 연다. 확인 버튼 locator 반환."""
    confirm = frame.locator("[data-testid='seOnePublishBtn']").first
    if not confirm.is_visible():
        frame.locator("[data-click-area='tpb.publish']").first.click()
    confirm.wait_for(state="visible", timeout=15000)
    return confirm


def _read_categories(frame) -> list:
    """발행 창의 카테고리 목록 → [{id, name, parent}] (하위 카테고리는 parent에 상위 이름)."""
    btn = frame.locator("[data-click-area='tpb*i.category']").first
    btn.click()
    time.sleep(0.8)
    raw = frame.evaluate("""() => [...document.querySelectorAll('li')]
        .map(li => li.querySelector('[data-testid^=categoryItemText_]') && li)
        .filter(Boolean)
        .map(li => { const t = li.querySelector('[data-testid^=categoryItemText_]');
            return {id: t.getAttribute('data-testid').replace('categoryItemText_', ''),
                    text: t.innerText.trim(), child: li.innerText.includes('하위 카테고리')}; })""")
    cats, parent = [], ""
    for c in raw:
        name = c["text"].replace("하위 카테고리", "").strip()
        if c["child"]:
            cats.append({"id": c["id"], "name": name, "parent": parent})
        else:
            parent = name
            cats.append({"id": c["id"], "name": name, "parent": ""})
    # 목록 닫기 — 현재 선택된 항목을 다시 눌러 닫는다
    try:
        frame.locator("li input[type=radio]:checked + label").first.click(timeout=3000)
    except Exception:
        btn.click()
    time.sleep(0.5)
    return cats


def _select_category(frame, category_id: str, _emit) -> bool:
    btn = frame.locator("[data-click-area='tpb*i.category']").first
    btn.click()
    time.sleep(0.6)
    label = frame.locator(f"[data-testid='categoryItemText_{category_id}']").first
    if not label.count():
        _emit(f"  ⚠ 카테고리(id {category_id})를 찾지 못함 — 블로그 카테고리가 바뀌었다면 목록을 새로고침하세요")
        btn.click()
        return False
    label.click()
    time.sleep(0.6)
    _emit(f"  ✓ 카테고리: {btn.inner_text().replace('하위 카테고리', '').strip()}")
    return True


def _select_topic(frame, topic: str, _emit) -> bool:
    opener = frame.locator("[class*='theme1__']").first
    if not opener.count():
        _emit("  ⚠ 주제 설정 버튼을 찾지 못함 — 주제는 건너뜀")
        return False
    opener.click()
    time.sleep(1.0)
    if topic == NO_TOPIC:
        target = frame.locator("label[for='non-theme']").first
    else:
        target = frame.locator(f"label[for^='{topic}_']").first
    if not target.count():
        _emit(f"  ⚠ 주제 '{topic}'를 찾지 못함 — 주제는 건너뜀")
        frame.get_by_role("button", name="취소").last.click()
        return False
    target.click()
    time.sleep(0.3)
    frame.locator("button:visible", has_text="확인").last.click()
    time.sleep(0.6)
    _emit(f"  ✓ 주제: {opener.inner_text().strip()}")
    return True


def _prepare_publish_panel(page, frame, post, category_id, topic, _emit, reserve_at=None):
    """발행 설정 창을 열고 태그·전체공개·카테고리·주제를 채운다. 확인 버튼(또는 실패 시 None)."""
    _emit("🚀 발행 설정 채우는 중... (태그·공개범위·카테고리·주제)")
    try:
        confirm = _open_publish_panel(frame)
    except Exception as e:
        _emit(f"  ⚠ 발행 설정 창을 열지 못함({str(e)[:60]})")
        return None

    # 네이버 정식 태그 칸에 태그 등록 (본문 해시태그와 별도로 검색 노출용)
    tags = [hashtag(t)[1:] for t in post.get("tags", []) if str(t).strip()][:30]
    if tags:
        try:
            tag_input = frame.locator("#tag-input")
            tag_input.click(timeout=5000)
            for t in tags:
                # 키보드로 한 글자씩 치면 한글 조합(IME)이 끝나기 전에 Enter가 들어가
                # 앞뒤 태그가 붙은 태그가 추가로 생긴다(#임진왜란히데요시 등, 2026-10-05 실측).
                # 값을 직접 넣고(fill) Enter로 확정해 조합 과정을 아예 거치지 않는다.
                tag_input.fill(t)
                time.sleep(0.25)
                tag_input.press("Enter")
                time.sleep(0.45)
            n_reg = frame.evaluate(
                "() => { const i = document.querySelector('#tag-input');"
                " const box = i && i.closest('div') && i.closest('div').parentElement;"
                " return box ? (box.innerText.match(/#/g) || []).length : -1 }"
            )
            if n_reg == len(tags):
                _emit(f"  ✓ 태그 {len(tags)}개 등록")
            else:
                _emit(f"  ⚠ 태그 등록 수가 다릅니다 (요청 {len(tags)}개 / 등록 {n_reg}개) — 발행 후 확인하세요")
        except Exception as e:
            _emit(f"  ⚠ 태그 칸 입력 실패({str(e)[:60]}) — 본문 해시태그만 사용")
    try:
        frame.locator("#open_public").check(timeout=3000)  # 전체공개
    except Exception:
        pass
    for fn, val in ((_select_category, category_id), (_select_topic, topic)):
        if not val:
            continue
        try:
            fn(frame, val, _emit)
        except Exception as e:
            _emit(f"  ⚠ {('카테고리' if fn is _select_category else '주제')} 선택 실패({str(e)[:60]})")
    if reserve_at is not None:
        # 예약 설정이 틀린 채 확정을 누르면 '현재' 시각으로 즉시 발행돼 버린다 → 검증 실패 시 중단
        if not _set_reservation(frame, reserve_at, _emit):
            raise RuntimeError("예약 시각을 화면에 설정하지 못해 중단했습니다 (즉시 발행 방지).")
    return confirm


def _set_reservation(frame, when, _emit) -> bool:
    """발행 창에서 '예약'을 고르고 날짜·시·분을 맞춘 뒤, 화면 값을 다시 읽어 일치할 때만 True."""
    import re as _re

    want_date = when.strftime("%Y. %m. %d")
    want_h, want_m = f"{when.hour:02d}", f"{when.minute:02d}"
    try:
        frame.locator("label[for='radio_time2']").click(timeout=4000)
        time.sleep(0.6)
        date_in = frame.locator("[class*='input_date']").first
        if (date_in.input_value() or "").strip() != want_date:
            date_in.click()
            time.sleep(0.5)
            for _ in range(4):
                title = frame.locator(".ui-datepicker-title").first.inner_text(timeout=3000)
                nums = [int(x) for x in _re.findall(r"\d+", title)]
                if len(nums) >= 2 and (nums[0], nums[1]) == (when.year, when.month):
                    frame.evaluate(
                        """(d) => { const b = [...document.querySelectorAll(
                            '.ui-datepicker td:not(.ui-state-disabled) button')]
                            .find(x => x.textContent.trim() === String(d)); if (b) b.click(); }""",
                        when.day)
                    break
                frame.locator(".ui-datepicker-next").first.click(timeout=3000)
                time.sleep(0.4)
            time.sleep(0.5)
        frame.locator("select[title='예약 발행 시간 선택']").select_option(want_h)
        frame.locator("select[title='예약 발행 분 선택']").select_option(want_m)
        time.sleep(0.4)
        got = (
            frame.locator("#radio_time2").is_checked(),
            (frame.locator("[class*='input_date']").first.input_value() or "").strip(),
            frame.locator("select[title='예약 발행 시간 선택']").input_value(),
            frame.locator("select[title='예약 발행 분 선택']").input_value(),
        )
        if got == (True, want_date, want_h, want_m):
            _emit(f"  ✓ 예약 시각 설정: {want_date} {want_h}:{want_m}")
            return True
        _emit(f"  ⚠ 예약 시각 불일치: 원하는 값 {want_date} {want_h}:{want_m} / 화면 {got}")
    except Exception as e:
        _emit(f"  ⚠ 예약 시각 설정 실패({str(e)[:80]})")
    return False


def _reserve_count(frame):
    """에디터 상단 '예약 발행 N건'의 N (못 읽으면 None)."""
    import re as _re

    try:
        t = frame.locator("button[data-click-area='tpb*t.schedule']").first.inner_text(timeout=1500)
        m = _re.search(r"(\d+)", t)
        return int(m.group(1)) if m else None
    except Exception:
        return None


def _wait_reserved(page, frame, n0, timeout: float = 40.0) -> bool:
    """예약 확정 후 화면이 에디터를 벗어나거나 예약 건수가 늘면 True."""
    end = time.time() + timeout
    while time.time() < end:
        time.sleep(1.5)
        try:
            if page.is_closed():
                return True
            url = page.url
            if "Redirect=Write" not in url and "postwrite" not in url.lower():
                return True
            n = _reserve_count(frame)
            if n is not None and n0 is not None and n > n0:
                return True
        except Exception:
            return True  # 화면이 이동하며 프레임이 사라진 경우
    return False


def _record_reserved(draft_dir: Path, when, verified: bool):
    try:
        (draft_dir / "reserved.json").write_text(
            json.dumps({"at": when.strftime("%Y-%m-%d %H:%M"), "verified": bool(verified),
                        "ts": int(time.time())}, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def reserved_info(draft_dir: Path):
    try:
        return json.loads((draft_dir / "reserved.json").read_text(encoding="utf-8"))
    except Exception:
        return None


def fetch_categories() -> list:
    """네이버 글쓰기 화면을 열어 내 블로그 카테고리 목록을 읽고 캐시에 저장한다 (발행 안 함)."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        ctx = _launch(p)
        try:
            if not _is_logged_in(ctx):
                sys.exit(LOGIN_HELP)
            page = ctx.new_page()
            page.goto(f"https://blog.naver.com/{config.NAVER_BLOG_ID}?Redirect=Write")
            page.wait_for_load_state("networkidle")
            frame = page.frame(name="mainFrame") or page.main_frame
            for sel in (".se-popup-button-cancel", ".se-help-panel-close-button"):
                try:
                    frame.locator(sel).click(timeout=3000)
                except Exception:
                    pass
            _open_publish_panel(frame)
            cats = _read_categories(frame)
        finally:
            try:
                ctx.close()
            except Exception:
                pass
    data = load_options()
    data.update({"categories": cats, "fetched_at": int(time.time())})
    CATEGORY_CACHE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return cats


def load_options() -> dict:
    """캐시된 카테고리 목록 + 글 분야별 기본 선택값."""
    try:
        return json.loads(CATEGORY_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return {"categories": [], "defaults": {}, "fetched_at": 0}


def save_default(blog_category: str, category_id: str, topic: str):
    """글 분야(경제·역사·라이프)별로 마지막 선택을 기억해 다음 발행의 기본값으로 쓴다."""
    data = load_options()
    data.setdefault("defaults", {})[blog_category] = {"category_id": category_id or "",
                                                      "topic": topic or ""}
    CATEGORY_CACHE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def _published_url(page):
    """발행 뒤 이동한 글 주소를 찾는다. 아직 글쓰기 화면이면 None.

    네이버는 발행 후 글을 '새 탭'으로 열기도 하므로 같은 브라우저의 모든 탭과
    그 안의 프레임 주소를 모두 살핀다.
    """
    blog_id = re.escape(config.NAVER_BLOG_ID or "")
    urls = []
    try:
        pages = list(page.context.pages)
    except Exception:
        pages = [page]
    for pg in pages:
        try:
            urls.append(pg.url)
            urls.extend(f.url for f in pg.frames)
        except Exception:
            continue
    for u in urls:
        low = u.lower()
        if "write" in low:
            continue
        m = re.search(r"lo[gG]No=(\d+)", u) or re.search(rf"blog\.naver\.com/{blog_id}/(\d+)", u)
        if m:
            return f"https://blog.naver.com/{config.NAVER_BLOG_ID}/{m.group(1)}"
    return None


def _published_url_from_rss(title: str, since: float):
    """블로그 RSS에서 같은 제목의 글이 since 이후 올라왔는지 확인 (브라우저 상태와 무관한 보조 감지)."""
    import html as htmllib
    from email.utils import parsedate_to_datetime

    import requests

    try:
        r = requests.get(f"https://rss.blog.naver.com/{config.NAVER_BLOG_ID}.xml", timeout=10)
        r.encoding = "utf-8"
        items = re.findall(
            r"<item>.*?<title>(.*?)</title>.*?<link>(.*?)</link>.*?<pubDate>(.*?)</pubDate>",
            r.text, re.S,
        )
    except Exception:
        return None
    strip = lambda s: htmllib.unescape(re.sub(r"<!\[CDATA\[|\]\]>", "", s)).strip()
    for t, link, date in items[:5]:
        try:
            if strip(t) == title.strip() and parsedate_to_datetime(strip(date)).timestamp() >= since - 120:
                return strip(link).split("?")[0]
        except Exception:
            continue
    return None


def _record_published(draft_dir: Path, url: str):
    """초안 폴더에 발행 기록을 남긴다 (중복 발행 방지·미리보기에 '발행됨' 표시용)."""
    try:
        (draft_dir / "published.json").write_text(
            json.dumps({"url": url, "at": int(time.time())}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass


def published_info(draft_dir: Path):
    try:
        return json.loads((draft_dir / "published.json").read_text(encoding="utf-8"))
    except Exception:
        return None


def _editor_text_len(frame) -> int:
    return frame.evaluate(
        "() => { const w = document.querySelector('.se-components-wrap');"
        " return w ? w.innerText.length : 0 }"
    )


def _paste_html(page, frame, html: str) -> bool:
    """커서 위치에 HTML을 붙여넣는다. 성공 여부(본문 길이 증가)를 반환.

    1순위: 합성 paste 이벤트 — 사용자의 Mac 클립보드를 건드리지 않는다.
    2순위: 실제 클립보드 + Cmd+V.
    """
    before = _editor_text_len(frame)
    frame.evaluate(
        """(html) => {
            const dt = new DataTransfer();
            dt.setData('text/html', html);
            dt.setData('text/plain', html.replace(/<[^>]+>/g, ''));
            const sel = getSelection();
            const n = sel && sel.anchorNode;
            const target = (n && (n.nodeType === 1 ? n : n.parentElement)) || document.activeElement;
            target.dispatchEvent(new ClipboardEvent('paste',
                {clipboardData: dt, bubbles: true, cancelable: true}));
        }""",
        html,
    )
    # 긴 섹션은 에디터 처리에 몇 초 걸린다. 1초 만에 '실패'로 판정하면 늦게 들어온
    # 내용 위에 다시 입력해 문단이 중복되고 서식이 빠졌다 (2026-10-05 실측) → 최대 8초 대기.
    if _wait_grow(frame, before, 8.0):
        return True
    try:
        page.context.grant_permissions(
            ["clipboard-read", "clipboard-write"], origin="https://blog.naver.com"
        )
        frame.evaluate(
            """(html) => navigator.clipboard.write([new ClipboardItem({
                'text/html': new Blob([html], {type: 'text/html'}),
                'text/plain': new Blob([html.replace(/<[^>]+>/g, '')], {type: 'text/plain'})})])""",
            html,
        )
        if _editor_text_len(frame) > before:  # 그사이 첫 붙여넣기가 들어왔으면 중복 방지
            return True
        page.keyboard.press("Meta+v" if sys.platform == "darwin" else "Control+v")
        return _wait_grow(frame, before, 8.0)
    except Exception:
        return _editor_text_len(frame) > before


def _wait_grow(frame, before: int, timeout: float) -> bool:
    """본문 길이가 늘고 잠시 안정될 때까지 기다린다."""
    deadline = time.time() + timeout
    last = before
    while time.time() < deadline:
        time.sleep(0.4)
        n = _editor_text_len(frame)
        if n > before and n == last:
            return True
        last = n
    return _editor_text_len(frame) > before


def _type_html_fallback(page, html: str):
    """붙여넣기가 막혔을 때: 태그를 걷어내고 굵게(<b>/<h2>)만 살려 타이핑."""
    import html as htmllib

    mod = "Meta" if sys.platform == "darwin" else "Control"
    blocks = re.findall(r"<(h2|p|li|blockquote)[^>]*>(.*?)</\1>", html, re.S)
    for tag, inner in blocks:
        inner = inner.replace("<br>", "")
        for piece in re.split(r"(<b>.*?</b>)", inner):
            if not piece:
                continue
            bold = tag == "h2" or piece.startswith("<b>")
            text = htmllib.unescape(re.sub(r"<[^>]+>", "", piece))
            if not text:
                continue
            if bold:
                page.keyboard.press(f"{mod}+b")
            page.keyboard.type(text, delay=4)
            if bold:
                page.keyboard.press(f"{mod}+b")
        page.keyboard.press("Enter")


def _wait_image_uploaded(frame, timeout: float = 20.0):
    """마지막 사진 컴포넌트에 실제 이미지가 붙을 때까지 대기."""
    n0 = frame.locator(".se-component.se-image").count()
    deadline = time.time() + timeout
    while time.time() < deadline:
        imgs = frame.locator(".se-component.se-image")
        if imgs.count() >= max(n0, 1):
            src = imgs.last.locator("img").first.get_attribute("src") or ""
            if src.startswith("http"):
                time.sleep(0.8)
                return
        time.sleep(0.5)


def _set_image_caption(page, frame, text: str):
    """사진을 선택해 나타나는 '사진 설명을 입력하세요' 칸에 캡션을 넣는다."""
    img = frame.locator(".se-component.se-image").last
    img.locator(".se-image-resource, img").first.click()
    time.sleep(0.6)
    img.locator(".se-caption").click(timeout=4000)
    page.keyboard.type(text, delay=4)


# 본문(16px)·출처(15px) 문단이 굵게 되어 있으면 잘못된 것 — 소제목·강조 문구(24px)만 굵다.
# 소제목 뒤에 붙여넣은 문단이 에디터의 '현재 서식(굵게)'을 물려받는 일이 있다
# (2026-10-31 실측: 발행글 224460129231에서 3번째 섹션부터 본문 전체가 굵게 올라감).
_FIND_BOLD_JS = """() => {
  const out = [];
  document.querySelectorAll('.se-component.se-text p.se-text-paragraph').forEach(p => {
    p.removeAttribute('data-bb-bold');
    if (!p.innerText.replace(/\\u200b/g, '').trim()) return;
    const spans = [...p.querySelectorAll('span')].filter(s => s.innerText.replace(/\\u200b/g, '').trim());
    // 에디터는 굵게를 span 안의 <b>로 둔다. 본문(16)·출처(15)·13px 문단에 굵은 글씨가 있으면 잘못된 것
    const bad = spans.some(s => /\\bse-fs(16|15|13)\\b/.test(s.className) &&
      [...s.querySelectorAll('b, strong')].some(b => b.innerText.replace(/\\u200b/g, '').trim()));
    if (bad) { p.setAttribute('data-bb-bold', String(out.length)); out.push(out.length); }
  });
  return out.length;
}"""


def _unbold_body(page, frame, _emit) -> int:
    """굵게 잘못 올라간 본문·출처 문단을 찾아 굵게를 풀고, 끝내 못 푼 문단 수를 돌려준다."""
    mod = "Meta" if sys.platform == "darwin" else "Control"

    def count() -> int:
        try:
            return frame.evaluate(_FIND_BOLD_JS)
        except Exception:
            return 0

    n = count()
    if n:
        _emit(f"  · 본문 {n}개 문단이 굵게 되어 있어 일반 글씨로 되돌립니다...")
    for _ in range(2):
        if not n:
            break
        for k in range(n):
            para = frame.locator(f'p[data-bb-bold="{k}"]')
            # 일부만 굵은 문단은 첫 번째 Cmd+B가 전체 굵게로 만들 수 있어, 풀릴 때까지 최대 4번 누른다
            for _try in range(4):
                try:
                    para.first.click(click_count=3, timeout=4000)   # 문단 전체 선택
                    time.sleep(0.25)
                    page.keyboard.press(f"{mod}+b")
                    time.sleep(0.3)
                    if not para.first.evaluate(
                            "p => [...p.querySelectorAll('b, strong')].some(b => b.innerText.trim())"):
                        break
                except Exception:
                    break
        try:
            frame.locator(".se-title-text").click(timeout=2000)   # 선택을 풀고 재검사
        except Exception:
            pass
        n = count()
    return n


def _focus_end(frame):
    """문서 맨 끝 텍스트 문단에 커서를 둔다 (사진 아래 이어 쓰기)."""
    frame.locator(".se-component.se-text").last.locator("p.se-text-paragraph").last.click()


def login(timeout_sec: int = 300):
    """브라우저를 열어 사용자가 직접 로그인하게 하고, 완료를 자동 감지한다."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        ctx = _launch(p)
        if _is_logged_in(ctx):
            print("✓ 이미 로그인되어 있습니다.")
            ctx.close()
            return
        page = ctx.new_page()
        page.goto("https://nid.naver.com/nidlogin.login")
        print("열린 브라우저에서 네이버에 로그인하세요. "
              "'로그인 상태 유지'를 꼭 체크하세요. (최대 5분 대기)")
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            if _is_logged_in(ctx):
                persistent = any(
                    c["name"] == "NID_AUT" and c.get("expires", -1) > 0
                    for c in ctx.cookies("https://www.naver.com")
                )
                print("✓ 로그인 감지! 세션이 저장되었습니다.")
                if not persistent:
                    print("⚠ '로그인 상태 유지'가 꺼진 채 로그인되어, 창을 닫으면 세션이 사라집니다. "
                          "다시 로그인하면서 체크해 주세요.")
                time.sleep(2)
                ctx.close()
                return
            time.sleep(2)
        ctx.close()
        sys.exit("로그인이 감지되지 않아 종료합니다. 다시 시도해 주세요.")


def publish(draft_dir: Path, auto_publish: bool = False, wait_min: int = 15, log=None,
            category_id: str = None, topic: str = None, reserve_at=None, reserve_out: dict = None):
    """초안을 스마트에디터에 입력한다. auto_publish=True면 발행 버튼까지 누른다.

    log: 진행 메시지를 받을 콜러블(선택) — 대시보드 실행 현황에 단계별로 표시.
    """
    from playwright.sync_api import sync_playwright

    def _emit(msg: str):
        print(msg, flush=True)
        if log:
            try:
                log(msg)
            except Exception:
                pass

    if not config.NAVER_BLOG_ID:
        sys.exit("NAVER_BLOG_ID가 .env에 설정되어 있지 않습니다.")

    post = json.loads((draft_dir / "post.json").read_text(encoding="utf-8"))
    title = post["title"]

    _cancel.clear()
    result = "ok"
    with sync_playwright() as p:
        ctx = _launch(p)
        try:
            _publish_in(ctx, page_title=title, post=post, draft_dir=draft_dir,
                        auto_publish=auto_publish, wait_min=wait_min, _emit=_emit,
                        category_id=category_id, topic=topic, reserve_at=reserve_at,
                        reserve_out=reserve_out)
        except PublishCancelled as e:
            _emit(f"■ {e}")
            result = "cancelled"
        finally:
            try:
                ctx.close()  # 어떤 경우에도 브라우저를 정상 종료 (프로필 손상·복원 말풍선 방지)
            except Exception:
                pass
    return result


def _publish_in(ctx, page_title, post, draft_dir, auto_publish, wait_min, _emit,
                category_id=None, topic=None, reserve_at=None, reserve_out=None):
    """publish()의 실제 입력 단계. 브라우저 종료는 호출측 finally가 담당한다."""
    title = page_title
    if not _is_logged_in(ctx):
        sys.exit(LOGIN_HELP)

    _emit("✓ 네이버 로그인 확인 — 글쓰기 화면 여는 중...")
    page = ctx.new_page()
    page.goto(f"https://blog.naver.com/{config.NAVER_BLOG_ID}?Redirect=Write")
    page.wait_for_load_state("networkidle")

    # 글쓰기 에디터는 mainFrame iframe 안에 있을 수도, 최상위일 수도 있다
    frame = page.frame(name="mainFrame") or page.main_frame

    # "작성 중인 글이 있습니다" 팝업 → 취소(새 글)
    try:
        frame.locator(".se-popup-button-cancel").click(timeout=4000)
    except Exception:
        pass
    # 도움말 패널 닫기
    try:
        frame.locator(".se-help-panel-close-button").click(timeout=2500)
    except Exception:
        pass

    # 제목 입력
    _emit(f"✍ 제목 입력: {title}")
    frame.locator(".se-title-text").click()
    page.keyboard.type(title, delay=15)

    # 본문 영역으로 이동
    frame.locator(".se-component-content p.se-text-paragraph").last.click()

    # 미리보기와 같은 서식(소제목·굵게·캡션·구분선·출처·태그 색)을 살리기 위해
    # 서식 HTML을 에디터에 '붙여넣기'한다. 스마트에디터가 이를 자체 서식으로 변환한다.
    # (실패하면 예전 방식인 키보드 타이핑으로 자동 대체)
    def paste_html(html: str) -> bool:
        if _paste_html(page, frame, html):
            return True
        # 한 번 더: 커서를 문서 끝으로 옮기고 재시도 (포커스가 빠져 실패하는 경우가 많다)
        before = _editor_text_len(frame)
        try:
            _focus_end(frame)
        except Exception:
            pass
        if _editor_text_len(frame) == before and _paste_html(page, frame, html):
            return True
        # 이 섹션만 일반 입력으로 (다음 섹션은 다시 서식 붙여넣기를 시도한다)
        _emit("  ⚠ 이 섹션은 서식 붙여넣기 실패 — 일반 입력으로 대체 (글자색이 다를 수 있음)")
        _type_html_fallback(page, html)
        return False

    imgs_by_section: dict = {}
    for m in post.get("image_files", []):
        imgs_by_section.setdefault(m["after_section"], []).append(m)

    n_sec = len(post["sections"])
    for i, sec in enumerate(post["sections"]):
        _check_cancel()
        _emit(f"  · 본문 {i + 1}/{n_sec} 입력 중")
        paste_html(section_html(sec, first=(i == 0)))
        for m in imgs_by_section.get(i, []):
            img_path = draft_dir / "images" / Path(m["file"]).name
            if not img_path.exists():
                img_path = Path(m["file"])
            try:
                # 문서 상단 툴바의 사진 버튼 (본문에 생기는 insertMenu 버튼과 구분)
                with page.expect_file_chooser(timeout=10000) as fc:
                    frame.locator(
                        "button[data-name='image'][data-group='documentToolbar']"
                    ).first.click()
                fc.value.set_files(str(img_path))
                _wait_image_uploaded(frame)
                _set_image_caption(page, frame, m["caption"])
                _emit(f"  ✓ 사진 업로드: {img_path.name} (캡션 포함)")
            except Exception as e:
                _emit(f"  ⚠ 이미지 업로드 실패({img_path.name}): {e}")
            # 사진 아래 새 문단으로 커서 복귀 (다음 섹션 이어 쓰기)
            _focus_end(frame)

    # 출처(구분선 + 작은 회색 글씨) · 해시태그(초록) — 미리보기와 동일
    tail = footer_html(post)
    if tail:
        paste_html(tail)

    # 글자색 점검: 서식 붙여넣기가 빠진 곳은 스킨 기본색(회색)으로 보여 한 글 안에서 색이 갈린다
    try:
        n_plain = frame.evaluate("""() => [...document.querySelectorAll('.se-component.se-text p span')]
            .filter(s => { const t = s.innerText.trim();
                return t.length > 3 && !t.startsWith('#') &&
                       !(s.getAttribute('style') || '').includes('color'); }).length""")
        if n_plain:
            _emit(f"  ⚠ 글자색이 지정되지 않은 문장 조각 {n_plain}개 — 발행 전 회색 글씨가 섞였는지 확인하세요")
        else:
            _emit("  ✓ 글자색 일정 (모든 문단 서식 적용)")
    except Exception:
        pass

    # 굵기 점검: 소제목 뒤 문단이 굵게 되어 있으면 풀어 준다 (못 풀면 발행 중단)
    n_bold = _unbold_body(page, frame, _emit)
    if n_bold:
        _emit(f"  ✗ 본문 {n_bold}개 문단을 굵게에서 되돌리지 못했습니다")
        if auto_publish:
            raise RuntimeError("본문 굵기가 일정하지 않아 발행을 중단했습니다 (초안은 그대로 남아 있음).")
    else:
        _emit("  ✓ 본문 굵기 일정 (소제목·강조 문구만 굵게)")

    # 글자 크기 점검: 본문이 소제목 크기(24px)를 물려받으면 글 위·아래 크기가 달라진다
    try:
        n_big = frame.evaluate("""() => [...document.querySelectorAll('.se-component.se-text p.se-text-paragraph')]
            .filter(p => p.innerText.replace(/\\u200b/g, '').trim().length > 60)
            .map(p => { const s = p.querySelector('span') || p; return (s.className.match(/se-fs-?\\S*/) || [''])[0] })
            .filter(c => c !== 'se-fs16' && c !== 'se-fs15' && c !== 'se-fs-').length""")
    except Exception:
        n_big = 0
    if n_big:
        _emit(f"  ✗ 본문 {n_big}개 문단의 글자 크기가 다릅니다(소제목 크기가 번짐)")
        if auto_publish:
            raise RuntimeError("본문 글자 크기가 일정하지 않아 발행을 중단했습니다 (초안은 그대로 남아 있음).")
    else:
        _emit("  ✓ 글자 크기 일정 (본문 16px)")

    started = time.time()
    _check_cancel()
    if reserve_at is not None:
        # 입력에 몇 분이 걸렸으니 예약 시각이 임박했을 수 있다 → 지금 기준으로 다시 검증
        import datetime as _dt
        from .autopilot import ceil_grid

        floor = ceil_grid(_dt.datetime.now() + _dt.timedelta(minutes=13))
        if reserve_at < floor:
            _emit(f"  ⚠ 예약 시각({reserve_at:%H:%M})이 임박해 {floor:%H:%M}로 조정합니다")
            reserve_at = floor
        if reserve_out is not None:
            reserve_out["at"] = reserve_at
    confirm = _prepare_publish_panel(page, frame, post, category_id, topic, _emit, reserve_at)

    if auto_publish:
        if confirm is None:
            raise RuntimeError("발행 설정 창을 열지 못했습니다.")
        if reserve_at is not None:
            n0 = _reserve_count(frame)
            _emit(f"🕒 예약 발행 확정 누르는 중... ({reserve_at:%m/%d %H:%M})")
            confirm.click()
            ok = _wait_reserved(page, frame, n0)
            _record_reserved(draft_dir, reserve_at, ok)
            _emit(f"✓ 예약 발행 등록 완료: {reserve_at:%m/%d %H:%M}" if ok else
                  f"⚠ 예약 등록을 화면에서 확인하지 못했습니다 ({reserve_at:%m/%d %H:%M}) — "
                  "블로그의 '예약 발행' 목록을 확인하세요 (중복 방지를 위해 재시도하지 않음)")
            return
        _emit("🚀 발행 버튼 누르는 중...")
        confirm.click()
        url = None
        deadline = time.time() + 60
        last_rss = 0.0
        while time.time() < deadline and not url:
            time.sleep(2)
            url = _published_url(page)
            if not url and time.time() - last_rss > 10:
                last_rss = time.time()
                url = _published_url_from_rss(title, started)
        if not url:
            raise RuntimeError("발행 버튼은 눌렀지만 게시가 확인되지 않습니다. 블로그를 직접 확인해 주세요.")
        _record_published(draft_dir, url)
        _emit(f"✓ 발행 완료: {url}")
        return

    if confirm is not None:
        _emit("✓ 초안 입력 + 발행 설정(카테고리·주제·태그) 완료. "
              "Mac 창에서 내용을 확인하고 오른쪽 위 설정 창의 [✓ 발행]을 누르세요.")
    else:
        _emit("✓ 초안 입력 완료. 브라우저에서 내용을 확인하고 [발행] 버튼을 직접 누르세요.")
    _emit(f"  발행이 감지되면 브라우저를 자동으로 닫습니다. (최대 {wait_min}분 대기)")
    started = time.time()
    deadline = started + wait_min * 60
    published_url = None
    last_rss = 0.0
    while time.time() < deadline:
        _check_cancel()
        if page.is_closed():
            # 발행 직후 창을 닫았을 수도 있으니 RSS로 한 번 더 확인
            published_url = _published_url_from_rss(title, started)
            break
        # ① 브라우저 주소: 편집 탭은 '?Redirect=Write'로 남고 글은 새 탭에 열리기도 하므로
        #    모든 탭·프레임에서 글 번호를 찾는다.
        published_url = _published_url(page)
        # ② 보조: 블로그 RSS에 같은 제목의 새 글이 올라왔는지 (15초마다)
        if not published_url and time.time() - last_rss > 15:
            last_rss = time.time()
            published_url = _published_url_from_rss(title, started)
        if published_url:
            break
        time.sleep(3)
    if published_url:
        _record_published(draft_dir, published_url)
        _emit(f"✓ 발행 감지: {published_url}")
    else:
        _emit("발행이 감지되지 않은 채 대기가 끝났습니다. (입력 내용은 에디터에 남아 있음)")
