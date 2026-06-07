from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass


MAX_RECORD_XML_CHARS = 5 * 1024 * 1024


@dataclass
class ForwardedItem:
    index: int
    data_id: str
    raw_type: str
    message_type: str
    create_time: int | None
    local_time_text: str | None
    sender_source: str | None
    text: str | None
    media_checksum: str | None


def parse_forwarded_record(xml_text: str) -> tuple[list[ForwardedItem], list[str]]:
    """解析微信合并转发 XML 并返回标准化子消息及警告。"""
    warnings: list[str] = []
    if len(xml_text) > MAX_RECORD_XML_CHARS:
        return [], ["forwarded record XML exceeds size limit"]

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        return [], [f"forwarded record XML parse failed: {exc}"]

    if root.tag != "recordinfo":
        warnings.append(f"unexpected forwarded record root: {root.tag}")

    items: list[ForwardedItem] = []
    for index, node in enumerate(root.findall("./datalist/dataitem")):
        raw_type = str(node.attrib.get("datatype", "unknown"))
        message_type = {
            "1": "text",
            "2": "image",
        }.get(raw_type, "unknown")
        create_time = _to_int(node.findtext("srcMsgCreateTime"))
        sender_source = node.findtext("./dataitemsource/hashusername")

        items.append(
            ForwardedItem(
                index=index,
                data_id=str(node.attrib.get("dataid", "")),
                raw_type=raw_type,
                message_type=message_type,
                create_time=create_time,
                local_time_text=_clean_text(node.findtext("sourcetime")),
                sender_source=_clean_text(sender_source),
                text=_clean_text(node.findtext("datadesc")),
                media_checksum=_clean_text(
                    node.findtext("fullmd5") or node.findtext("thumbfullmd5")
                ),
            )
        )

    if not items:
        warnings.append("forwarded record contains no data items")
    return items, warnings


def _to_int(value: str | None) -> int | None:
    """将可选字符串安全转换为整数。"""
    try:
        return int(value) if value else None
    except (TypeError, ValueError):
        return None


def _clean_text(value: str | None) -> str | None:
    """清理空字符和多余空白并返回可选文本。"""
    if value is None:
        return None
    cleaned = " ".join(value.replace("\x00", "").split())
    return cleaned or None
