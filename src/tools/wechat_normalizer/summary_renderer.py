from __future__ import annotations

import argparse
import base64
import html
import json
import mimetypes
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any


def render_summary_files(
    summary_path: Path,
    *,
    html_path: Path | None = None,
    pdf_path: Path | None = None,
    make_pdf: bool = True,
) -> dict[str, Any]:
    """渲染活动汇总、files。"""
    summary_path = summary_path.resolve()
    export_root = summary_path.parent
    summary = json.loads(summary_path.read_text(encoding="utf-8-sig"))

    target_html = html_path.resolve() if html_path else summary_path.with_suffix(".html")
    target_html.write_text(
        render_summary_html(summary, export_root),
        encoding="utf-8",
        newline="\n",
    )

    target_pdf = pdf_path.resolve() if pdf_path else summary_path.with_suffix(".pdf")
    pdf_created = False
    pdf_error = None
    if make_pdf:
        try:
            pdf_created = render_pdf_with_browser(target_html, target_pdf)
        except RuntimeError as exc:
            pdf_error = str(exc)

    return {
        "html": str(target_html),
        "pdf": str(target_pdf) if pdf_created else None,
        "pdf_created": pdf_created,
        "pdf_error": pdf_error,
    }


def render_summary_html(summary: dict[str, Any], export_root: Path) -> str:
    """渲染活动汇总、html。"""
    counts = summary.get("counts", {})
    sections = [
        ("必须完成", "mandatory_tasks", "必须"),
        ("推荐关注", "recommended_activities", "可选"),
        ("信息不完整", "incomplete_items", "待确认"),
        ("其他活动", "other_activities", "其他"),
        ("更正/延期/取消", "cancelled_or_updated", "更新"),
    ]
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    body = [
        "<!doctype html>",
        '<html lang="zh-CN">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>群消息事项汇总</title>",
        f"<style>{_css()}</style>",
        "</head>",
        "<body>",
        '<main class="page">',
        "<header>",
        "<h1>群消息事项汇总</h1>",
        f'<p class="meta">生成时间：{html.escape(generated_at)} · 原始活动：{_count(counts, "input_activities")} · 合并后：{_count(counts, "merged_activities")}</p>',
        "</header>",
        '<section class="overview">',
        _stat("必须完成", counts.get("mandatory_tasks", 0)),
        _stat("推荐关注", counts.get("recommended_activities", 0)),
        _stat("信息不完整", counts.get("incomplete_items", 0)),
        _stat("其他", counts.get("other_activities", 0)),
        "</section>",
    ]

    for title, key, badge in sections:
        items = summary.get(key, [])
        if not items:
            continue
        body.extend(
            [
                f'<section class="section section-{html.escape(key)}">',
                f"<h2>{html.escape(title)}</h2>",
            ]
        )
        for item in items:
            body.append(_render_item(item, export_root, badge))
        body.append("</section>")

    body.extend(
        [
            '<footer class="footer">图片仅作为同时间段附件展示；文本事实以证据原文为准。</footer>',
            "</main>",
            "</body>",
            "</html>",
        ]
    )
    return "\n".join(body)


def render_pdf_with_browser(html_path: Path, pdf_path: Path) -> bool:
    """渲染pdf、with、browser。"""
    browser = _find_browser()
    if browser is None:
        raise RuntimeError("No Chrome/Edge/Chromium executable found for PDF export.")
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(browser),
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        f"--print-to-pdf={pdf_path}",
        str(html_path.resolve()),
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"PDF export command failed: {exc}") from exc
    if result.returncode != 0:
        raise RuntimeError(
            f"PDF export failed with exit code {result.returncode}: "
            f"{result.stderr or result.stdout}"
        )
    if not pdf_path.is_file() or pdf_path.stat().st_size <= 0:
        raise RuntimeError("PDF export command completed but no PDF file was created.")
    return True


def _render_item(item: dict[str, Any], export_root: Path, badge: str) -> str:
    """渲染item。"""
    title = _text(item.get("title")) or "未命名事项"
    rows = [
        ("截止", _text(item.get("deadline"))),
        ("日期", _date_range(item)),
        ("地点", _text(item.get("location"))),
        ("对象", _text(item.get("eligibility"))),
        ("动作", _text(item.get("required_action"))),
        ("链接", _link(item.get("registration_url"))),
        ("缺失", "、".join(_text(value) for value in item.get("missing_information", []) if _text(value))),
    ]
    evidence = _text(item.get("evidence_quote"))
    tags = item.get("category_tags", [])
    tag_html = "".join(
        f'<span class="tag">{html.escape(str(tag))}</span>'
        for tag in tags
        if str(tag).strip()
    )
    images = _render_images(item.get("related_images", []), export_root)
    details = "\n".join(
        f'<div class="detail"><dt>{html.escape(label)}</dt><dd>{value}</dd></div>'
        for label, value in rows
        if value
    )
    return "\n".join(
        [
            '<article class="item">',
            '<div class="item-head">',
            f"<h3>{html.escape(title)}</h3>",
            f'<span class="badge">{html.escape(badge)}</span>',
            "</div>",
            f'<div class="tags">{tag_html}</div>' if tag_html else "",
            f"<dl>{details}</dl>" if details else "",
            f'<blockquote>{html.escape(evidence)}</blockquote>' if evidence else "",
            images,
            "</article>",
        ]
    )


def _render_images(images: Any, export_root: Path) -> str:
    """渲染图片列表。"""
    if not isinstance(images, list) or not images:
        return ""
    rendered = []
    for image in images:
        if not isinstance(image, dict):
            continue
        src = _image_data_uri(export_root, image)
        if not src:
            continue
        meta = " · ".join(
            part
            for part in [
                _text(image.get("occurred_at_local")),
                _dimension_text(image),
                _text(image.get("message_id")),
            ]
            if part
        )
        rendered.append(
            "\n".join(
                [
                    '<figure class="image">',
                    f'<img src="{src}" alt="相关图片">',
                    f"<figcaption>{html.escape(meta)}</figcaption>" if meta else "",
                    "</figure>",
                ]
            )
        )
    if not rendered:
        return ""
    return '<div class="images">' + "\n".join(rendered) + "</div>"


def _image_data_uri(export_root: Path, image: dict[str, Any]) -> str | None:
    """把图片文件编码为 data URI。"""
    relative_path = _text(image.get("relative_path"))
    if not relative_path:
        return None
    candidate = (export_root / relative_path).resolve()
    try:
        candidate.relative_to(export_root.resolve())
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    mime_type = _text(image.get("mime_type")) or mimetypes.guess_type(candidate.name)[0]
    mime_type = mime_type or "application/octet-stream"
    data = base64.b64encode(candidate.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{data}"


def _find_browser() -> Path | None:
    """查找可用于渲染 PDF 的浏览器。"""
    configured = os.getenv("WECHAT_PDF_BROWSER", "").strip()
    if configured:
        configured_path = Path(configured).expanduser()
        if configured_path.is_file() and os.access(configured_path, os.X_OK):
            return configured_path
    names = (
        "google-chrome",
        "google-chrome-stable",
        "chrome",
        "chrome.exe",
        "microsoft-edge",
        "microsoft-edge-stable",
        "msedge",
        "msedge.exe",
        "chromium",
        "chromium-browser",
        "chromium.exe",
    )
    for name in names:
        found = shutil.which(name)
        if found:
            return Path(found)
    candidates = [
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    ]
    return next((path for path in candidates if path.is_file()), None)


def _date_range(item: dict[str, Any]) -> str | None:
    """格式化摘要中的日期范围。"""
    start = _text(item.get("start_date"))
    end = _text(item.get("end_date"))
    if start and end:
        return f"{start} 至 {end}"
    return start or end or _text(item.get("start_time"))


def _dimension_text(image: dict[str, Any]) -> str | None:
    """格式化图片尺寸文本。"""
    width = image.get("width")
    height = image.get("height")
    if width and height:
        return f"{width}x{height}"
    return None


def _link(value: Any) -> str | None:
    """渲染报告中的链接字段。"""
    text = _text(value)
    if not text:
        return None
    escaped = html.escape(text)
    return f'<a href="{escaped}">{escaped}</a>'


def _stat(label: str, value: Any) -> str:
    """渲染报告顶部的统计项。"""
    return (
        '<div class="stat">'
        f'<strong>{html.escape(str(value))}</strong>'
        f'<span>{html.escape(label)}</span>'
        "</div>"
    )


def _count(counts: dict[str, Any], key: str) -> str:
    """统计指定摘要列表的数量。"""
    return html.escape(str(counts.get(key, 0)))


def _text(value: Any) -> str | None:
    """HTML 转义并格式化文本。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _css() -> str:
    """返回活动报告页面 CSS。"""
    return """
@page { size: A4; margin: 16mm; }
* { box-sizing: border-box; }
body { margin: 0; background: #f4f6f8; color: #17202a; font: 14px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif; }
.page { max-width: 980px; margin: 0 auto; padding: 28px 18px 48px; }
header { border-bottom: 3px solid #1f6feb; padding-bottom: 14px; margin-bottom: 18px; }
h1 { margin: 0; font-size: 30px; line-height: 1.2; }
.meta { color: #607080; margin: 8px 0 0; }
.overview { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin: 18px 0 26px; }
.stat { background: #fff; border: 1px solid #d8dee8; border-radius: 8px; padding: 12px; }
.stat strong { display: block; font-size: 24px; line-height: 1; color: #1f6feb; }
.stat span { color: #526070; }
.section { margin: 24px 0; }
h2 { font-size: 20px; margin: 0 0 12px; }
.item { background: #fff; border: 1px solid #d8dee8; border-left: 5px solid #1f6feb; border-radius: 8px; padding: 16px; margin: 12px 0; break-inside: avoid; }
.section-incomplete_items .item { border-left-color: #b7791f; }
.section-recommended_activities .item { border-left-color: #2f855a; }
.item-head { display: flex; gap: 12px; align-items: flex-start; justify-content: space-between; }
h3 { font-size: 18px; margin: 0 0 8px; }
.badge { white-space: nowrap; background: #e8f1ff; color: #174ea6; border-radius: 999px; padding: 2px 10px; font-size: 12px; }
.tags { margin: 4px 0 10px; }
.tag { display: inline-block; background: #eef2f7; color: #4a5568; border-radius: 999px; padding: 2px 8px; margin: 0 6px 6px 0; font-size: 12px; }
dl { margin: 10px 0; display: grid; grid-template-columns: 1fr; gap: 6px; }
.detail { display: grid; grid-template-columns: 58px 1fr; gap: 8px; }
dt { color: #667085; }
dd { margin: 0; overflow-wrap: anywhere; }
a { color: #1f6feb; text-decoration: none; }
blockquote { margin: 12px 0 0; padding: 10px 12px; background: #f7fafc; border-left: 3px solid #a0aec0; color: #334155; }
.images { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-top: 12px; }
.image { margin: 0; border: 1px solid #e2e8f0; border-radius: 8px; overflow: hidden; background: #f8fafc; }
.image img { display: block; width: 100%; max-height: 360px; object-fit: contain; background: #fff; }
figcaption { padding: 8px; color: #667085; font-size: 12px; overflow-wrap: anywhere; }
.footer { margin-top: 28px; color: #667085; font-size: 12px; text-align: center; }
@media print {
  body { background: #fff; }
  .page { max-width: none; padding: 0; }
  .item { box-shadow: none; }
}
@media (max-width: 720px) {
  .overview { grid-template-columns: repeat(2, 1fr); }
  .item-head { display: block; }
}
"""
