from __future__ import annotations

import hashlib
import mimetypes
import struct
from pathlib import Path

from .models import MediaAttachment


def inspect_media(
    export_root: Path,
    relative_path: str,
    kind: str,
) -> MediaAttachment:
    """校验本地媒体并收集哈希和尺寸，不分析图片内容。"""
    normalized_path = _normalize_relative_path(relative_path)
    attachment = MediaAttachment(kind=kind, relative_path=normalized_path)

    try:
        absolute_path = _resolve_inside(export_root, normalized_path)
    except ValueError as exc:
        attachment.analysis_status = "failed"
        attachment.warnings.append(str(exc))
        return attachment

    if not absolute_path.is_file():
        attachment.analysis_status = "missing"
        attachment.warnings.append("media file does not exist in export")
        return attachment

    attachment.size_bytes = absolute_path.stat().st_size
    attachment.sha256 = _sha256_file(absolute_path)
    attachment.mime_type = (
        mimetypes.guess_type(absolute_path.name)[0] or "application/octet-stream"
    )

    dimensions = _read_image_dimensions(absolute_path)
    if dimensions is not None:
        attachment.width, attachment.height = dimensions

    attachment.analysis_status = "not_analyzed"
    return attachment


def remote_media(
    kind: str,
    checksum: str | None,
    parser_hint: str = "remote_forwarded_media",
) -> MediaAttachment:
    """构造导出中缺失的远程转发媒体占位信息。"""
    warnings = ["forwarded media was not copied into the export"]
    return MediaAttachment(
        kind=kind,
        sha256=None,
        parser_hint=parser_hint,
        analysis_status="missing",
        warnings=warnings,
    )


def _resolve_inside(root: Path, relative_path: str) -> Path:
    """将相对路径解析到导出根目录内并阻止路径穿越。"""
    resolved_root = root.resolve()
    candidate = (resolved_root / relative_path).resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("media path escapes export directory") from exc
    return candidate


def _normalize_relative_path(path: str) -> str:
    """将不同平台的媒体相对路径统一为正斜杠形式。"""
    return str(Path(path.replace("\\", "/"))).replace("\\", "/")


def _sha256_file(path: Path) -> str:
    """分块计算指定文件的 SHA-256 摘要。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_image_dimensions(path: Path) -> tuple[int, int] | None:
    """从 PNG 或 JPEG 文件头读取图片宽高。"""
    with path.open("rb") as handle:
        header = handle.read(32)
        if header.startswith(b"\x89PNG\r\n\x1a\n") and len(header) >= 24:
            return struct.unpack(">II", header[16:24])
        if header.startswith(b"\xff\xd8"):
            return _read_jpeg_dimensions(handle)
    return None


def _read_jpeg_dimensions(handle) -> tuple[int, int] | None:
    """遍历 JPEG 段并读取首个有效帧的宽高。"""
    handle.seek(2)
    while True:
        marker_start = handle.read(1)
        if not marker_start:
            return None
        if marker_start != b"\xff":
            continue
        marker = handle.read(1)
        while marker == b"\xff":
            marker = handle.read(1)
        if not marker or marker in {b"\xd8", b"\xd9"}:
            continue

        size_bytes = handle.read(2)
        if len(size_bytes) != 2:
            return None
        segment_size = struct.unpack(">H", size_bytes)[0]
        if segment_size < 2:
            return None

        marker_value = marker[0]
        if marker_value in {
            0xC0,
            0xC1,
            0xC2,
            0xC3,
            0xC5,
            0xC6,
            0xC7,
            0xC9,
            0xCA,
            0xCB,
            0xCD,
            0xCE,
            0xCF,
        }:
            payload = handle.read(5)
            if len(payload) != 5:
                return None
            height, width = struct.unpack(">HH", payload[1:5])
            return width, height
        handle.seek(segment_size - 2, 1)
