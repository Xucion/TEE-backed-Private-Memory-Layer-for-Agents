from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


SCHEMA_VERSION = "wechat-normalized-message/v1"


@dataclass
class MediaAttachment:
    kind: str
    relative_path: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    width: int | None = None
    height: int | None = None
    parser_hint: str | None = None
    analysis_status: str = "not_analyzed"
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """将媒体附件转换为移除空值后的字典。"""
        return _without_none(asdict(self))


@dataclass
class NormalizedMessage:
    message_id: str
    conversation_id: str
    source_kind: str
    source_index: int
    source_message_id: str
    parent_message_id: str | None
    message_type: str
    raw_type: str
    occurred_at: str | None
    occurred_at_local: str | None
    observed_at: str | None
    observed_at_local: str | None
    sequence: int | None
    sender_role: str
    sender_id: str | None
    text: str | None
    title: str | None
    url: str | None
    url_host: str | None
    media: list[MediaAttachment]
    context_group_id: str
    activity_features: dict[str, Any]
    llm_text: str
    processing_status: str = "success"
    warnings: list[str] = field(default_factory=list)
    personalization_preview: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """将规范化消息转换为带 Schema 版本的字典。"""
        data = asdict(self)
        data["schema_version"] = SCHEMA_VERSION
        return _without_none(data)


@dataclass
class NormalizationReport:
    schema_version: str
    source_conversations: int = 0
    source_messages: int = 0
    forwarded_messages_expanded: int = 0
    normalized_messages: int = 0
    message_types: dict[str, int] = field(default_factory=dict)
    local_media_found: int = 0
    media_not_analyzed: int = 0
    missing_media: int = 0
    context_groups: int = 0
    unknown_types: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """将标准化统计报告转换为字典。"""
        return asdict(self)


def _without_none(value: Any) -> Any:
    """递归移除字典中的空值并保留列表结构。"""
    if isinstance(value, dict):
        return {
            key: _without_none(item)
            for key, item in value.items()
            if item is not None
        }
    if isinstance(value, list):
        return [_without_none(item) for item in value]
    return value
