"""생성된 글 + 이미지를 초안 폴더(HTML/마크다운)로 조립한다."""

import datetime
import json
import re
from pathlib import Path

import markdown as md

from . import config

# 네이버 스마트에디터에 붙여넣었을 때 깔끔하게 들어가도록 단순한 인라인 스타일만 사용
_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
  body {{ font-family: -apple-system, 'Apple SD Gothic Neo', sans-serif;
         max-width: 720px; margin: 40px auto; line-height: 2.2; font-size: 17px;
         color: #222; padding: 0 16px; }}
  h1 {{ font-size: 1.6em; line-height: 1.4; }}
  h2 {{ font-size: 1.45em; margin-top: 2em; }}
  img {{ max-width: 100%; border-radius: 6px; display: block; margin: 8px auto; }}
  figure {{ margin: 2em 0; text-align: center; }}
  figcaption {{ font-size: 0.85em; color: #888; margin-top: 6px; }}
  p {{ margin: 0 0 1.4em; }}
  .hl {{ margin: 2em 0; padding: 0 12px; text-align: center; font-size: 1.45em; line-height: 1.7;
         font-weight: bold; color: #1b7a3d; }}
  .notice {{ margin-top: 1.5em; color: #999; font-size: 0.8em; }}
  .tags {{ margin-top: 3em; color: #3b8; font-size: 0.9em; }}
  .source {{ margin-top: 2.5em; color: #888; font-size: 0.92em; border-top: 1px solid #eee; padding-top: 1em; }}
  .copy-bar {{ text-align: center; margin-bottom: 28px; }}
  .copy-bar .guide {{ font-size: 0.95em; color: #555; margin: 0 0 14px; }}
  .copy-bar button {{ font-size: 1.1em; padding: 14px 28px; border: none; border-radius: 8px;
    background: #2db400; color: #fff; cursor: pointer; width: 100%; max-width: 420px; }}
  .copy-bar button:active {{ opacity: 0.85; }}
  figure a {{ display: block; }}
  .img-actions {{ margin-top: 10px; }}
  .img-save-btn {{ display: inline-block; font-size: 0.95em; padding: 10px 20px;
    border-radius: 8px; background: #eee; color: #222; text-decoration: none; }}
  .img-save-btn:active {{ opacity: 0.8; }}
  .img-hint {{ font-size: 0.8em; color: #999; margin-top: 8px; }}
</style>
</head>
<body>
<h1>{title}</h1>
{body}
<p class="tags">{tags}</p>
</body>
</html>
"""

_COPY_WIDGET_TEMPLATE = """<div class="copy-bar">
  <p class="guide">① 전체 복사 → ② 이미지 저장 → ③ 네이버 글쓰기에 붙여넣기 + 이미지 추가</p>
  <button id="copyAllBtn" type="button">\U0001F4CB 전체 복사</button>
</div>
<script>
(function() {{
  var copyText = {copy_text_json};
  var btn = document.getElementById('copyAllBtn');
  var defaultLabel = btn.textContent;
  function showCopied() {{
    btn.textContent = '✅ 복사됨!';
    setTimeout(function() {{ btn.textContent = defaultLabel; }}, 2000);
  }}
  function fallbackCopy(text) {{
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.top = '-1000px';
    ta.style.left = '-1000px';
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    try {{
      document.execCommand('copy');
      showCopied();
    }} catch (e) {{
      alert('자동 복사에 실패했습니다. 직접 선택해 복사해주세요.');
    }}
    document.body.removeChild(ta);
  }}
  btn.addEventListener('click', function() {{
    if (navigator.clipboard && navigator.clipboard.writeText) {{
      navigator.clipboard.writeText(copyText).then(showCopied, function() {{
        fallbackCopy(copyText);
      }});
    }} else {{
      fallbackCopy(copyText);
    }}
  }});
}})();
</script>
"""


def _slugify(s: str) -> str:
    s = re.sub(r"[^a-z0-9-]+", "-", s.lower()).strip("-")
    return s or "post"


_NUM_RE = re.compile(r"\d[\d,\.]*")


def clean_highlights(post: dict) -> dict:
    """강조 문구를 다듬은 사본을 돌려준다. 숫자가 있거나 너무 길거나 짧은 문구는 버린다."""
    out = dict(post)
    secs = []
    for sec in post.get("sections", []):
        sec = dict(sec)
        hl = (sec.get("highlight") or "").strip().strip("\"'“”‘’").strip()
        body = (sec.get("body_md") or "") + (sec.get("heading") or "")
        # 강조 문구는 기억에 남는 짧은 한 줄이어야 한다: 숫자 없음(지어낸 수치 방지) · 8~45자
        if not sec.get("heading", "").strip() or len(hl) < 8 or len(hl) > 45 or _NUM_RE.search(hl):
            hl = ""
        sec["highlight"] = hl
        secs.append(sec)
    out["sections"] = secs
    return out


def split_first_para(body_md: str) -> tuple:
    """본문을 (첫 문단, 나머지)로 나눈다. 강조 문구는 첫 문단 뒤에 놓는다."""
    paras = [p for p in (body_md or "").split("\n\n") if p.strip()]
    if len(paras) < 2:
        return (body_md or "").strip(), ""
    return paras[0].strip(), "\n\n".join(paras[1:]).strip()


def has_ai_images(image_metas) -> bool:
    return bool(config.AI_IMAGE_NOTICE) and any(m.get("ai_generated") for m in image_metas or [])


def _plain_copy_text(post: dict, image_metas: list[dict]) -> str:
    """미리보기 '전체 복사' 버튼용 — 제목+본문+태그를 서식 없는 순수 텍스트로."""
    imgs_by_section: dict[int, list[dict]] = {}
    for m in image_metas:
        imgs_by_section.setdefault(m["after_section"], []).append(m)

    parts = [post.get("title", "")]
    for i, sec in enumerate(post.get("sections", [])):
        if sec.get("heading", "").strip():
            parts.append(sec["heading"])
        first, rest = split_first_para(sec.get("body_md", ""))
        parts.append(first)
        if sec.get("highlight"):
            parts.append(f"\u201c{sec['highlight']}\u201d")
        parts.append(rest)
        for m in imgs_by_section.get(i, []):
            parts.append(f"[이미지: {m.get('caption', '')}]")

    source_line = (post.get("source_line") or "").strip()
    if source_line:
        parts.append(source_line)
    if has_ai_images(image_metas):
        parts.append(config.AI_NOTICE_TEXT)

    tags = " ".join("#" + "".join(str(t).split()) for t in post.get("tags", []))
    if tags:
        parts.append(tags)

    return "\n\n".join(p for p in parts if p and p.strip())


def make_draft_dir(post: dict) -> Path:
    """drafts/<날짜>-<슬러그>/ 폴더를 만들어 반환."""
    today = datetime.date.today().isoformat()
    slug = _slugify(post.get("slug", ""))
    draft_dir = config.DRAFTS_DIR / f"{today}-{slug}"
    draft_dir.mkdir(parents=True, exist_ok=True)
    return draft_dir


def build_draft(post: dict, image_metas: list[dict], draft_dir: Path) -> Path:
    """post dict + 이미지 메타 → 초안 파일들(preview.html, post.md, post.json) 생성."""
    post = clean_highlights(post)
    imgs_by_section: dict[int, list[dict]] = {}
    for m in image_metas:
        imgs_by_section.setdefault(m["after_section"], []).append(m)

    html_parts = []
    md_parts = [f"# {post['title']}\n"]
    for i, sec in enumerate(post["sections"]):
        if sec["heading"].strip():
            html_parts.append(f"<h2>{sec['heading']}</h2>")
            md_parts.append(f"\n## {sec['heading']}\n")
        first, rest = split_first_para(sec["body_md"])
        html_parts.append(md.markdown(first))
        md_parts.append(first + "\n")
        if sec.get("highlight"):
            html_parts.append(f'<p class="hl">\u201c{sec["highlight"]}\u201d</p>')
            md_parts.append(f"\n> **\u201c{sec['highlight']}\u201d**\n")
        if rest:
            html_parts.append(md.markdown(rest))
            md_parts.append("\n" + rest + "\n")
        for m in imgs_by_section.get(i, []):
            rel = Path(m["file"]).relative_to(draft_dir) if str(
                draft_dir) in m["file"] else Path(m["file"])
            html_parts.append(
                f'<figure>'
                f'<a href="{rel}" target="_blank" rel="noopener"><img src="{rel}" alt="{m["caption"]}"></a>'
                f"<figcaption>{m['caption']}</figcaption>"
                f'<div class="img-actions">'
                f'<a class="img-save-btn" href="{rel}" download="{rel.name}">⬇️ 이미지 저장</a>'
                f'<p class="img-hint">저장이 안 되면 이미지를 길게 눌러 \'사진에 저장\'하세요</p>'
                f'</div>'
                f'</figure>'
            )
            md_parts.append(f"\n![{m['caption']}]({rel})\n*{m['caption']}*\n")

    source_line = (post.get("source_line") or "").strip()
    if source_line:
        html_parts.append(f'<p class="source">{source_line}</p>')
        md_parts.append(f"\n{source_line}\n")
    if has_ai_images(image_metas):
        html_parts.append(f'<p class="notice">{config.AI_NOTICE_TEXT}</p>')
        md_parts.append(f"\n{config.AI_NOTICE_TEXT}\n")

    tags = " ".join("#" + "".join(str(t).split()) for t in post.get("tags", []))
    html = _HTML_TEMPLATE.format(
        title=post["title"], body="\n".join(html_parts), tags=tags
    )

    copy_text_json = json.dumps(_plain_copy_text(post, image_metas), ensure_ascii=False)
    copy_text_json = copy_text_json.replace("</", "<\\/")  # </script> 조기 종료 방지
    copy_widget = _COPY_WIDGET_TEMPLATE.format(copy_text_json=copy_text_json)
    html = html.replace("<body>\n", "<body>\n" + copy_widget, 1)

    (draft_dir / "preview.html").write_text(html, encoding="utf-8")
    (draft_dir / "post.md").write_text("\n".join(md_parts) + f"\n\n{tags}\n", encoding="utf-8")
    post_meta = dict(post)
    post_meta["image_files"] = image_metas
    (draft_dir / "post.json").write_text(
        json.dumps(post_meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return draft_dir
