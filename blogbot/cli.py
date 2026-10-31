"""blogbot 명령줄 인터페이스."""

from __future__ import annotations

import sys
import time
import webbrowser
from pathlib import Path

import click

from . import builder, config, generator, images


@click.group()
def main():
    """뉴욕타임스 기사 → 한글 블로그 자동 작성 도구."""


@main.command()
@click.option("--category", "-c", default="life",
              type=click.Choice(list(config.CATEGORIES)), help="글감 카테고리")
@click.option("--limit", "-n", default=10, help="표시할 개수")
def topics(category, limit):
    """NYT RSS에서 글감 후보(제목+요약)를 가져온다."""
    import feedparser

    count = 0
    for feed_url in config.NYT_FEEDS[category]:
        feed = feedparser.parse(feed_url)
        for e in feed.entries:
            if count >= limit:
                return
            count += 1
            click.echo(f"\n[{count}] {e.title}")
            click.echo(f"    {getattr(e, 'summary', '')[:200]}")
            click.echo(f"    {e.link}")


_MODEL_IDS = [m[0] for m in config.AVAILABLE_MODELS]


@main.command()
@click.option("--category", "-c", default="life",
              type=click.Choice(list(config.CATEGORIES)), help="카테고리")
@click.option("--file", "-f", "file_", type=click.Path(exists=True),
              help="영문 기사 텍스트 파일 (없으면 stdin)")
@click.option("--note", default="", help="추가 요청사항 (예: '제목에 커피 포함')")
@click.option("--model", "-m", default=None,
              type=click.Choice(_MODEL_IDS),
              help=f"사용할 Claude 모델 (기본: {config.MODEL})")
@click.option("--no-research", is_flag=True,
              help="공신력 자료 추가 조사(웹 검색) 건너뛰기")
@click.option("--open", "open_", is_flag=True, help="완료 후 미리보기 열기")
def new(category, file_, note, model, no_research, open_):
    """영문 기사 텍스트로 블로그 글 초안을 생성한다."""
    if file_:
        text = Path(file_).read_text(encoding="utf-8")
    else:
        click.echo("기사 텍스트를 붙여넣고 Ctrl-D(macOS/리눅스)로 종료하세요:")
        text = sys.stdin.read()
    if len(text.strip()) < 200:
        sys.exit("기사 텍스트가 너무 짧습니다 (200자 이상 필요).")

    draft_dir = _process(text, category, note, model, research=not no_research)
    if open_:
        webbrowser.open(f"file://{draft_dir / 'preview.html'}")


def _process(text: str, category: str, note: str = "", model: str | None = None,
             research: bool = True) -> Path:
    used_model = config.enforce_model(model or config.MODEL)

    research_brief = None
    if research:
        click.echo("🔎 공신력 자료 조사 중... (Kimi Search Pro, 보통 1~2분)")
        try:
            from . import research as research_mod
            research_brief, research_model = research_mod.research(
                text, config.CATEGORIES.get(category, category)
            )
            click.echo(f"✓ 조사 완료 ({research_model} · {len(research_brief)}자)" if research_brief
                       else "⚠ 조사 결과 없음 — 원문만으로 진행")
        except Exception as e:
            click.echo(f"⚠ 조사 실패({e}) — 원문만으로 진행")

    click.echo(f"✍  글 생성 중... (모델: {used_model})")
    post = generator.generate_post(text, category, note, model=used_model,
                                   research_brief=research_brief)

    if post.get("fallback_from"):
        click.echo(f"↪ {post['fallback_reason']} → {post['model']}로 자동 전환")

    # Kimi 계열은 긴 프롬프트에서 한글 음절이 깨질 수 있어 교정 패스를 거친다
    # (자동 전환된 경우도 포함 — 실제 사용된 모델 기준)
    if config.is_kimi_model(post.get("model", used_model)):
        click.echo("✏️  한글 오탈자 교정 중...")
        try:
            from . import proofread as proofread_mod
            post, n_fixed = proofread_mod.proofread_post(post)
            click.echo(f"✓ 교정 완료 ({n_fixed}개 필드 수정)" if n_fixed
                       else "✓ 교정 완료 (수정 없음)")
        except Exception as e:
            click.echo(f"⚠ 교정 건너뜀 ({e})")
    click.echo(f"✓ 제목: {post['title']}")

    draft_dir = builder.make_draft_dir(post)
    click.echo("🖼  이미지 검색 중...")
    metas = images.fetch_images_for_post(post, draft_dir)

    builder.build_draft(post, metas, draft_dir)
    click.echo(f"✓ 초안 저장: {draft_dir}")
    click.echo(f"  미리보기: {draft_dir / 'preview.html'}")
    click.echo(f"  발행:     python -m blogbot publish '{draft_dir}'")
    return draft_dir


@main.command()
@click.argument("draft", type=click.Path(exists=True))
def preview(draft):
    """초안 미리보기를 브라우저로 연다."""
    webbrowser.open(f"file://{Path(draft).resolve() / 'preview.html'}")


@main.command()
def login():
    """네이버 로그인 세션을 저장한다 (최초 1회)."""
    from . import naver
    naver.login()


@main.command("news-login")
def news_login():
    """뉴스 사이트(NYT 등) 로그인 세션을 저장한다 (최초 1회).

    브라우저가 열리면 NYT 등에 로그인한 뒤 창을 닫으세요. 이후 서버가 그 세션으로
    전체 본문을 가져옵니다 (폰에서 자동 작성 시에도 활용).
    """
    from . import news
    click.echo("브라우저가 열립니다. NYT 등에 로그인 후 창을 닫으세요...")
    news.login()
    if news.has_session():
        click.echo("✓ 로그인 세션 저장 완료")
    else:
        click.echo("⚠ 세션이 감지되지 않았습니다. 다시 시도해 주세요.")


@main.command()
@click.argument("draft", type=click.Path(exists=True))
@click.option("--auto", is_flag=True,
              help="발행 버튼까지 자동으로 누른다 (기본: 입력만 하고 대기)")
def publish(draft, auto):
    """초안을 네이버 블로그 에디터에 자동 입력한다."""
    from . import naver
    naver.publish(Path(draft).resolve(), auto_publish=auto)


@main.command()
@click.option("--port", default=8765, help="포트 (기본 8765)")
@click.option("--no-browser", is_flag=True, help="브라우저 자동 열기 끄기")
@click.option("--lan", is_flag=True,
              help="같은 Wi-Fi의 스마트폰·태블릿에서도 접속 가능하게 함 (0.0.0.0 바인딩)")
def web(port, no_browser, lan):
    """웹 GUI 대시보드를 연다 (글감→생성→미리보기→발행)."""
    from .web import run_server
    host = "0.0.0.0" if lan else "127.0.0.1"
    run_server(port=port, host=host, open_browser=not no_browser)


@main.command()
@click.option("--interval", default=30, help="inbox 확인 주기(초)")
@click.option("--auto-publish", is_flag=True, help="생성 후 네이버 발행까지 자동")
@click.option("--model", "-m", default=None,
              type=click.Choice(_MODEL_IDS),
              help=f"사용할 Claude 모델 (기본: {config.MODEL})")
def auto(interval, auto_publish, model):
    """inbox/ 폴더를 감시하며 기사 파일이 들어오면 자동으로 글을 생성한다.

    파일 규칙:
      inbox/economy/*.txt  → 경제
      inbox/history/*.txt  → 역사
      inbox/life/*.txt     → 라이프
      inbox/*.txt          → 라이프(기본)
    처리된 파일은 inbox/processed/로 이동한다.
    """
    config.PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    for cat in config.CATEGORIES:
        (config.INBOX_DIR / cat).mkdir(parents=True, exist_ok=True)
    click.echo(f"👀 inbox 감시 시작: {config.INBOX_DIR} (주기 {interval}초, Ctrl-C로 종료)")

    while True:
        try:
            for path in sorted(config.INBOX_DIR.rglob("*")):
                if path.suffix.lower() not in (".txt", ".md"):
                    continue
                if config.PROCESSED_DIR in path.parents:
                    continue
                category = path.parent.name if path.parent.name in config.CATEGORIES else "life"
                click.echo(f"\n📄 새 기사 발견: {path.name} (카테고리: {category})")
                try:
                    text = path.read_text(encoding="utf-8")
                    if len(text.strip()) < 200:
                        click.echo("  ⚠ 너무 짧아서 건너뜀")
                    else:
                        draft_dir = _process(text, category, model=model)
                        if auto_publish:
                            from . import naver
                            naver.publish(draft_dir, auto_publish=True)
                except Exception as e:
                    click.echo(f"  ✗ 처리 실패: {e}")
                dest = config.PROCESSED_DIR / path.name
                path.rename(dest)
            time.sleep(interval)
        except KeyboardInterrupt:
            click.echo("\n종료합니다.")
            break


if __name__ == "__main__":
    main()
