from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .forwarded import ForwardedItem, parse_forwarded_record
from .media import (
    inspect_media,
    remote_media,
)
from .models import NormalizationReport, NormalizedMessage
from .preferences import PreferenceProfile, score_for_profile


MESSAGE_TYPE_MAP = {
    "1": "text",
    "3": "image",
    "10000": "system",
    "21474836529": "link",
    "81604378673": "forwarded_bundle",
}

TRACKING_QUERY_KEYS = {
    "scene",
    "srcid",
    "mpshare",
    "sharer_shareinfo",
    "sharer_shareinfo_first",
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
}

SENSITIVE_QUERY_KEYS = {
    "access_token",
    "auth",
    "authkey",
    "code",
    "key",
    "signature",
    "sign",
    "ticket",
    "token",
}

ACTIVITY_KEYWORDS = {
    "competition": ("比赛", "竞赛", "大赛", "赛题", "挑战杯", "获奖"),
    "entertainment": ("娱乐", "文艺", "音乐", "电影", "演出", "联谊", "聚会", "游戏"),
    "sports": ("体育", "运动会", "篮球", "足球", "羽毛球", "乒乓球"),
    "academic": ("讲座", "学术", "论坛", "研讨", "论文", "课题", "报告会"),
    "career": ("招聘", "实习", "就业", "宣讲"),
    "volunteer": ("志愿", "公益"),
    "administrative": ("缴费", "材料", "表格", "登记", "统计", "名单"),
}

MANDATORY_KEYWORDS = (
    "必须",
    "务必",
    "要求",
    "请于",
    "截止",
    "提交",
    "填写",
    "完成",
    "不得",
    "统一参加",
    "全体同学",
)

DATE_KEYWORDS = (
    "截止",
    "报名",
    "时间",
    "日期",
    "本周",
    "下周",
    "今天",
    "明天",
    "周一",
    "周二",
    "周三",
    "周四",
    "周五",
    "周六",
    "周日",
)

CONTEXT_GROUP_GAP_SECONDS = 5 * 60

# Override mojibake literals above. These strings drive activity detection and
# must remain readable UTF-8 because exported WeChat content is Chinese text.
ACTIVITY_KEYWORDS = {
    "competition": ("比赛", "竞赛", "大赛", "赛题", "挑战杯", "获奖"),
    "entertainment": ("娱乐", "文艺", "音乐", "电影", "演出", "联谊", "聚会", "游戏"),
    "sports": ("体育", "运动会", "篮球", "足球", "羽毛球", "乒乓球"),
    "academic": ("讲座", "学术", "论坛", "研讨", "论文", "课题", "报告会"),
    "career": ("招聘", "实习", "就业", "宣讲"),
    "volunteer": ("志愿", "公益"),
    "administrative": ("缴费", "材料", "表格", "登记", "统计", "名单", "报销", "审核"),
}

MANDATORY_KEYWORDS = (
    "必须",
    "务必",
    "要求",
    "请于",
    "截止",
    "提交",
    "填写",
    "完成",
    "不得",
    "统一参加",
    "全体同学",
)

DATE_KEYWORDS = (
    "截止",
    "报名",
    "时间",
    "日期",
    "本周",
    "下周",
    "今天",
    "明天",
    "周一",
    "周二",
    "周三",
    "周四",
    "周五",
    "周六",
    "周日",
)

GENERIC_ACTION_RE = re.compile(
    r"(提交|填写|完成|办理|缴纳|确认|反馈|收集|领取|携带|联系|参加|报名|"
    r"加入|签到|签字|报送|登记|申请|预约|投票)"
)
GENERIC_EVENT_RE = re.compile(
    r"(通知|提醒|安排|要求|事项|将于|定于|举行|召开|开展|开始|开赛|"
    r"招募|征集|开放|延期|取消|更正|修改|变更|补充)"
)
GENERIC_AUDIENCE_RE = re.compile(
    r"(@所有人|@All|各位|全体|相关人员|同学们|老师们|成员们|参与者)"
)
GENERIC_DIRECTIVE_RE = re.compile(
    r"(必须|务必|不得|应当|一定|须|请于|最迟|截止|请.{0,16}"
    r"(?:提交|填写|完成|办理|缴纳|确认|反馈|参加|报名)|"
    r"需要.{0,16}(?:提交|填写|完成|办理|缴纳|确认|反馈|参加|报名))"
)


@dataclass
class NormalizationResult:
    messages: list[NormalizedMessage]
    report: NormalizationReport


def normalize_export(
    export_dir: Path,
    *,
    timezone_offset: str = "+08:00",
    preference_profile: PreferenceProfile | None = None,
) -> NormalizationResult:
    """读取微信导出目录并生成稳定排序后的规范化消息集合。"""
    export_root = export_dir.resolve()
    local_timezone = _parse_timezone_offset(timezone_offset)
    report = NormalizationReport(schema_version="wechat-normalization-report/v1")

    _validate_export(export_root, report)
    all_messages: list[NormalizedMessage] = []

    conversation_dirs = sorted((export_root / "conversations").glob("*"))
    for conversation_dir in conversation_dirs:
        if not conversation_dir.is_dir():
            continue
        meta_path = conversation_dir / "meta.json"
        messages_path = conversation_dir / "messages.json"
        if not meta_path.is_file() or not messages_path.is_file():
            report.warnings.append(
                f"skipped incomplete conversation directory: {conversation_dir.name}"
            )
            continue

        meta = _load_json_object(meta_path)
        message_document = _load_json_object(messages_path)
        raw_messages = message_document.get("messages", [])
        if not isinstance(raw_messages, list):
            report.warnings.append(
                f"messages field is not a list: {conversation_dir.name}"
            )
            continue

        report.source_conversations += 1
        report.source_messages += len(raw_messages)
        conversation_source = str(
            meta.get("username")
            or message_document.get("conversationUsername")
            or conversation_dir.name
        )
        conversation_id = _stable_id("conv", conversation_source)

        sorted_raw = sorted(
            enumerate(raw_messages),
            key=lambda item: _raw_sort_key(item[1], item[0]),
        )
        groups = _assign_context_groups(sorted_raw, conversation_id)

        for normalized_index, (source_index, raw) in enumerate(sorted_raw):
            if not isinstance(raw, dict):
                report.warnings.append(
                    f"ignored non-object message at {conversation_dir.name}:{source_index}"
                )
                continue

            context_group_id = groups[source_index]
            top_message = _normalize_top_level_message(
                raw=raw,
                source_index=source_index,
                normalized_index=normalized_index,
                conversation_id=conversation_id,
                context_group_id=context_group_id,
                export_root=export_root,
                local_timezone=local_timezone,
                preference_profile=preference_profile,
            )
            all_messages.append(top_message)

            if top_message.message_type == "forwarded_bundle":
                children, warnings = _normalize_forwarded_children(
                    raw=raw,
                    source_index=source_index,
                    parent=top_message,
                    conversation_id=conversation_id,
                    context_group_id=context_group_id,
                    local_timezone=local_timezone,
                    preference_profile=preference_profile,
                )
                top_message.warnings.extend(warnings)
                if warnings and top_message.processing_status == "success":
                    top_message.processing_status = "partial"
                report.forwarded_messages_expanded += len(children)
                all_messages.extend(children)

    all_messages.sort(key=_normalized_sort_key)
    _update_report(report, all_messages)
    return NormalizationResult(messages=all_messages, report=report)


def write_result(
    result: NormalizationResult,
    output_path: Path,
    report_path: Path | None = None,
) -> None:
    """将规范化消息写入 JSONL 并将统计信息写入报告文件。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="\n") as handle:
        for message in result.messages:
            handle.write(
                json.dumps(message.to_dict(), ensure_ascii=False, sort_keys=True)
            )
            handle.write("\n")

    target_report = report_path or output_path.with_name("normalization_report.json")
    target_report.write_text(
        json.dumps(result.report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )


def _normalize_top_level_message(
    *,
    raw: dict[str, Any],
    source_index: int,
    normalized_index: int,
    conversation_id: str,
    context_group_id: str,
    export_root: Path,
    local_timezone: timezone,
    preference_profile: PreferenceProfile | None,
) -> NormalizedMessage:
    """将一条顶层微信原始消息转换为统一消息模型。"""
    raw_type = str(raw.get("type", "unknown"))
    message_type = MESSAGE_TYPE_MAP.get(raw_type, "unknown")
    source_identity = _first_nonempty(
        raw.get("serverId"),
        raw.get("id"),
        raw.get("localId"),
        f"index:{source_index}",
    )
    source_message_id = _stable_id("source", str(source_identity))
    message_id = _stable_id(
        "msg",
        conversation_id,
        source_message_id,
        raw_type,
        str(source_index),
    )
    occurred_at, occurred_at_local = _format_time(
        _to_int(raw.get("createTime")),
        local_timezone,
    )
    sender_source = _first_nonempty(
        raw.get("senderUsername"),
        raw.get("fromUsername"),
        raw.get("from"),
    )
    sender_id = _stable_id("sender", str(sender_source)) if sender_source else None
    sender_role = "self" if bool(raw.get("isSent")) else "other"

    text = _clean_text(raw.get("content"))
    if message_type == "image" and text in {"[图片]", "[image]"}:
        text = None
    title = _clean_text(raw.get("title"))
    url, url_host = _sanitize_url(raw.get("url"))
    media = _top_level_media(raw, export_root)
    warnings = [
        warning
        for attachment in media
        for warning in attachment.warnings
    ]

    activity_features = _activity_features(
        " ".join(
            part
            for part in [
                title,
                text,
            ]
            if part
        ),
        message_type,
        bool(media),
    )
    llm_text = _build_llm_text(
        message_type=message_type,
        occurred_at_local=occurred_at_local,
        observed_at_local=occurred_at_local,
        title=title,
        text=text,
        url=url,
        url_host=url_host,
        media=media,
    )
    preview = (
        score_for_profile(activity_features, preference_profile)
        if preference_profile is not None
        else None
    )

    status = "success"
    if any(
        item.analysis_status in {"missing", "failed"}
        for item in media
    ):
        status = "partial"
    if message_type == "unknown":
        status = "partial"
        warnings.append(f"unmapped WeChat message type: {raw_type}")

    return NormalizedMessage(
        message_id=message_id,
        conversation_id=conversation_id,
        source_kind="top_level",
        source_index=source_index,
        source_message_id=source_message_id,
        parent_message_id=None,
        message_type=message_type,
        raw_type=raw_type,
        occurred_at=occurred_at,
        occurred_at_local=occurred_at_local,
        observed_at=occurred_at,
        observed_at_local=occurred_at_local,
        sequence=_to_int(raw.get("sortSeq")),
        sender_role=sender_role,
        sender_id=sender_id,
        text=text,
        title=title,
        url=url,
        url_host=url_host,
        media=media,
        context_group_id=context_group_id,
        activity_features=activity_features,
        llm_text=llm_text,
        processing_status=status,
        warnings=warnings,
        personalization_preview=preview,
    )


def _normalize_forwarded_children(
    *,
    raw: dict[str, Any],
    source_index: int,
    parent: NormalizedMessage,
    conversation_id: str,
    context_group_id: str,
    local_timezone: timezone,
    preference_profile: PreferenceProfile | None,
) -> tuple[list[NormalizedMessage], list[str]]:
    """展开合并转发 XML 并规范化其中的所有子消息。"""
    record_xml = raw.get("recordItem")
    if not isinstance(record_xml, str) or not record_xml.strip():
        return [], ["forwarded bundle has no recordItem XML"]

    items, warnings = parse_forwarded_record(record_xml)
    children = [
        _normalize_forwarded_item(
            item=item,
            source_index=source_index,
            parent=parent,
            conversation_id=conversation_id,
            context_group_id=context_group_id,
            local_timezone=local_timezone,
            preference_profile=preference_profile,
        )
        for item in items
    ]
    return children, warnings


def _normalize_forwarded_item(
    *,
    item: ForwardedItem,
    source_index: int,
    parent: NormalizedMessage,
    conversation_id: str,
    context_group_id: str,
    local_timezone: timezone,
    preference_profile: PreferenceProfile | None,
) -> NormalizedMessage:
    """将单条合并转发子项转换为统一消息模型。"""
    source_message_id = _stable_id(
        "forwarded-source",
        parent.source_message_id,
        item.data_id or str(item.index),
    )
    message_id = _stable_id(
        "msg",
        conversation_id,
        parent.message_id,
        source_message_id,
    )
    occurred_at, occurred_at_local = _format_time(
        item.create_time,
        local_timezone,
    )
    sender_id = (
        _stable_id("sender", item.sender_source)
        if item.sender_source
        else None
    )
    media = (
        [remote_media("image", item.media_checksum)]
        if item.message_type == "image"
        else []
    )
    activity_features = _activity_features(
        item.text or "",
        item.message_type,
        bool(media),
    )
    llm_text = _build_llm_text(
        message_type=item.message_type,
        occurred_at_local=occurred_at_local or item.local_time_text,
        observed_at_local=parent.observed_at_local,
        title=None,
        text=item.text,
        url=None,
        url_host=None,
        media=media,
    )
    preview = (
        score_for_profile(activity_features, preference_profile)
        if preference_profile is not None
        else None
    )
    child_warnings = [
        warning
        for attachment in media
        for warning in attachment.warnings
    ]
    if item.message_type == "unknown":
        child_warnings.append(
            f"unmapped forwarded message type: {item.raw_type}"
        )

    return NormalizedMessage(
        message_id=message_id,
        conversation_id=conversation_id,
        source_kind="forwarded_item",
        source_index=source_index,
        source_message_id=source_message_id,
        parent_message_id=parent.message_id,
        message_type=item.message_type,
        raw_type=f"forwarded:{item.raw_type}",
        occurred_at=occurred_at,
        occurred_at_local=occurred_at_local or item.local_time_text,
        observed_at=parent.observed_at,
        observed_at_local=parent.observed_at_local,
        sequence=item.index,
        sender_role="forwarded_participant",
        sender_id=sender_id,
        text=item.text,
        title=None,
        url=None,
        url_host=None,
        media=media,
        context_group_id=context_group_id,
        activity_features=activity_features,
        llm_text=llm_text,
        processing_status="partial" if media or child_warnings else "success",
        warnings=child_warnings,
        personalization_preview=preview,
    )


def _top_level_media(
    raw: dict[str, Any],
    export_root: Path,
) -> list:
    """解析顶层消息的离线媒体列表并生成附件信息。"""
    offline_media = raw.get("offlineMedia")
    if not isinstance(offline_media, list):
        return []

    attachments = []
    for item in offline_media:
        if not isinstance(item, dict):
            continue
        relative_path = item.get("path")
        if not relative_path:
            continue
        attachments.append(
            inspect_media(
                export_root=export_root,
                relative_path=str(relative_path),
                kind=str(item.get("kind") or "file"),
            )
        )
    return attachments


def _activity_features(
    text: str,
    message_type: str,
    has_media: bool,
) -> dict[str, Any]:
    """根据关键词、日期和媒体信号计算活动候选特征。"""
    normalized = text.lower()
    tags = sorted(
        tag
        for tag, keywords in ACTIVITY_KEYWORDS.items()
        if any(keyword.lower() in normalized for keyword in keywords)
    )
    mandatory = any(keyword in text for keyword in MANDATORY_KEYWORDS)
    has_date_signal = any(keyword in text for keyword in DATE_KEYWORDS) or bool(
        re.search(r"\d{1,2}\s*[月./-]\s*\d{1,2}\s*[日号]?", text)
    )

    score = 0.0
    if tags:
        score += min(0.5, 0.22 + 0.1 * len(tags))
    if mandatory:
        score += 0.3
    if has_date_signal:
        score += 0.2
    if message_type == "link":
        score += 0.08
    if has_media:
        score += 0.05

    return {
        "candidate_score": round(min(1.0, score), 3),
        "is_activity_candidate": score >= 0.3,
        "tags": tags,
        "mandatory_signal": mandatory,
        "date_signal": has_date_signal,
    }


def _build_llm_text(
    *,
    message_type: str,
    occurred_at_local: str | None,
    observed_at_local: str | None,
    title: str | None,
    text: str | None,
    url: str | None,
    url_host: str | None,
    media: list,
) -> str:
    """把规范化字段组合为供 LLM 逐条提取的紧凑文本。"""
    lines = [
        f"消息类型: {message_type}",
        f"原始时间: {occurred_at_local or 'unknown'}",
    ]
    if observed_at_local and observed_at_local != occurred_at_local:
        lines.append(f"当前会话收到时间: {observed_at_local}")
    if title:
        lines.append(f"标题: {title}")
    if text:
        lines.append(f"正文: {text}")
    if url:
        lines.append(f"链接: {url}")
    elif url_host:
        lines.append(f"链接域名: {url_host}")

    for index, attachment in enumerate(media, start=1):
        description = [
            f"媒体{index}",
            attachment.kind,
            attachment.parser_hint or "unknown",
            attachment.analysis_status,
        ]
        lines.append("媒体: " + " | ".join(description))
        if attachment.analysis_status == "not_analyzed":
            lines.append(
                "媒体内容: [不分析图片内容，请结合同一 context_group_id "
                "内的相邻文本理解]"
            )
        elif attachment.analysis_status == "missing":
            lines.append("媒体提取文本: [导出中缺少该媒体]")
        elif attachment.analysis_status == "failed":
            lines.append("媒体提取文本: [媒体解析失败，需人工检查]")
    return "\n".join(lines)


def _activity_features(
    text: str,
    message_type: str,
    has_media: bool,
) -> dict[str, Any]:
    """使用通用语言结构计算候选分数，领域词只用于弱分类。"""
    normalized = text.lower()
    tags = sorted(
        tag
        for tag, keywords in ACTIVITY_KEYWORDS.items()
        if any(keyword.lower() in normalized for keyword in keywords)
    )
    mandatory = bool(GENERIC_DIRECTIVE_RE.search(text))
    has_date_signal = any(keyword in text for keyword in DATE_KEYWORDS) or bool(
        re.search(r"\d{1,2}\s*[月/-]\s*\d{1,2}\s*[日号]?", text)
    )
    has_action_signal = bool(GENERIC_ACTION_RE.search(text))
    has_event_signal = bool(GENERIC_EVENT_RE.search(text))
    has_audience_signal = bool(GENERIC_AUDIENCE_RE.search(text))

    score = 0.0
    if tags:
        score += min(0.1, 0.05 * len(tags))
    if has_action_signal:
        score += 0.22
    if has_event_signal:
        score += 0.18
    if mandatory:
        score += 0.28
    if has_date_signal:
        score += 0.2
    if has_audience_signal:
        score += 0.12
    if message_type == "link":
        score += 0.08
    if has_media:
        score += 0.05

    return {
        "candidate_score": round(min(1.0, score), 3),
        "is_activity_candidate": score >= 0.3,
        "tags": tags,
        "mandatory_signal": mandatory,
        "date_signal": has_date_signal,
        "action_signal": has_action_signal,
        "event_signal": has_event_signal,
        "audience_signal": has_audience_signal,
    }


def _build_llm_text(
    *,
    message_type: str,
    occurred_at_local: str | None,
    observed_at_local: str | None,
    title: str | None,
    text: str | None,
    url: str | None,
    url_host: str | None,
    media: list,
) -> str:
    """为单条消息的 LLM 抽取构建紧凑可读文本。"""
    lines = [
        f"消息类型: {message_type}",
        f"原始时间: {occurred_at_local or 'unknown'}",
    ]
    if observed_at_local and observed_at_local != occurred_at_local:
        lines.append(f"当前会话收到时间: {observed_at_local}")
    if title:
        lines.append(f"标题: {title}")
    if text:
        lines.append(f"正文: {text}")
    if url:
        lines.append(f"链接: {url}")
    elif url_host:
        lines.append(f"链接域名: {url_host}")

    for index, attachment in enumerate(media, start=1):
        description = [
            f"媒体{index}",
            attachment.kind,
            attachment.parser_hint or "unknown",
            attachment.analysis_status,
        ]
        lines.append("媒体: " + " | ".join(description))
        if attachment.analysis_status == "not_analyzed":
            lines.append(
                "媒体内容: [不分析图片内容，请结合同一 context_group_id "
                "内的相邻文本理解]"
            )
        elif attachment.analysis_status == "missing":
            lines.append("媒体提取文本: [导出中缺少该媒体]")
        elif attachment.analysis_status == "failed":
            lines.append("媒体提取文本: [媒体解析失败，需要人工检查]")
    return "\n".join(lines)


def _assign_context_groups(
    sorted_raw: list[tuple[int, Any]],
    conversation_id: str,
) -> dict[int, str]:
    """按消息时间间隔为相邻消息分配稳定上下文组。"""
    groups: dict[int, str] = {}
    group_seed: str | None = None
    previous_time: int | None = None

    for source_index, raw in sorted_raw:
        current_time = (
            _to_int(raw.get("createTime"))
            if isinstance(raw, dict)
            else None
        )
        if (
            group_seed is None
            or previous_time is None
            or current_time is None
            or current_time - previous_time > CONTEXT_GROUP_GAP_SECONDS
        ):
            source_identity = (
                _first_nonempty(
                    raw.get("serverId"),
                    raw.get("id"),
                    raw.get("localId"),
                    f"index:{source_index}",
                )
                if isinstance(raw, dict)
                else f"index:{source_index}"
            )
            group_seed = _stable_id(
                "group",
                conversation_id,
                str(source_identity),
            )
        groups[source_index] = group_seed
        if current_time is not None:
            previous_time = current_time
    return groups


def _raw_sort_key(raw: Any, source_index: int) -> tuple:
    """为原始消息生成时间、序列和源位置组成的稳定排序键。"""
    if not isinstance(raw, dict):
        return (0, 0, source_index)
    return (
        _to_int(raw.get("createTime")) or 0,
        _to_int(raw.get("sortSeq")) or 0,
        source_index,
    )


def _normalized_sort_key(message: NormalizedMessage) -> tuple:
    """为顶层及转发子消息生成确定性的最终排序键。"""
    timestamp = message.occurred_at or ""
    child_order = 1 if message.source_kind == "forwarded_item" else 0
    return (
        timestamp,
        message.source_index,
        child_order,
        message.sequence or 0,
        message.message_id,
    )


def _validate_export(root: Path, report: NormalizationReport) -> None:
    """校验导出目录必需文件并把兼容性问题写入报告。"""
    required = [
        root / "manifest.json",
        root / "report.json",
        root / "conversations",
    ]
    missing = [str(path.name) for path in required if not path.exists()]
    if missing:
        raise FileNotFoundError(
            f"invalid WeChat export; missing: {', '.join(missing)}"
        )

    manifest = _load_json_object(root / "manifest.json")
    if manifest.get("schemaVersion") != 1:
        report.warnings.append(
            f"untested export schema version: {manifest.get('schemaVersion')}"
        )
    if manifest.get("format") != "json":
        report.warnings.append(
            f"unexpected export format: {manifest.get('format')}"
        )

    source_report = _load_json_object(root / "report.json")
    missing_media = source_report.get("missingMedia", [])
    errors = source_report.get("errors", [])
    if isinstance(missing_media, list) and missing_media:
        report.warnings.append(
            f"source export reports {len(missing_media)} missing media files"
        )
    if isinstance(errors, list) and errors:
        report.warnings.append(
            f"source export reports {len(errors)} errors"
        )


def _update_report(
    report: NormalizationReport,
    messages: Iterable[NormalizedMessage],
) -> None:
    """根据最终消息集合更新类型和媒体处理统计。"""
    messages = list(messages)
    type_counts = Counter(message.message_type for message in messages)
    report.normalized_messages = len(messages)
    report.message_types = dict(sorted(type_counts.items()))
    report.unknown_types = type_counts.get("unknown", 0)
    report.local_media_found = sum(
        1
        for message in messages
        for media in message.media
        if media.relative_path and media.analysis_status != "missing"
    )
    report.media_not_analyzed = sum(
        1
        for message in messages
        for media in message.media
        if media.analysis_status == "not_analyzed"
    )
    report.missing_media = sum(
        1
        for message in messages
        for media in message.media
        if media.analysis_status == "missing"
    )
    report.context_groups = len(
        {message.context_group_id for message in messages}
    )


def _sanitize_url(raw_url: Any) -> tuple[str | None, str | None]:
    """规范化 HTTP 链接并移除跟踪参数和敏感令牌。"""
    if not isinstance(raw_url, str) or not raw_url.strip():
        return None, None
    url = raw_url.strip()
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None, None

    query = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        normalized_key = key.lower()
        if normalized_key in TRACKING_QUERY_KEYS:
            continue
        query.append(
            (key, "[REDACTED]")
            if normalized_key in SENSITIVE_QUERY_KEYS
            else (key, value)
        )
    sanitized = urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path,
            urlencode(query, doseq=True),
            "",
        )
    )
    return sanitized, parsed.hostname.lower()


def _format_time(
    epoch: int | None,
    local_timezone: timezone,
) -> tuple[str | None, str | None]:
    """将 Unix 时间戳格式化为 UTC 和本地 ISO 时间。"""
    if epoch is None:
        return None, None
    utc_time = datetime.fromtimestamp(epoch, timezone.utc)
    local_time = utc_time.astimezone(local_timezone)
    return (
        utc_time.isoformat().replace("+00:00", "Z"),
        local_time.isoformat(),
    )


def _parse_timezone_offset(value: str) -> timezone:
    """将正负时区偏移字符串解析为 timezone 对象。"""
    match = re.fullmatch(r"([+-])(\d{2}):(\d{2})", value)
    if not match:
        raise ValueError("timezone offset must use format +HH:MM or -HH:MM")
    sign = 1 if match.group(1) == "+" else -1
    hours = int(match.group(2))
    minutes = int(match.group(3))
    if hours > 23 or minutes > 59:
        raise ValueError("invalid timezone offset")
    return timezone(sign * timedelta(hours=hours, minutes=minutes))


def _load_json_object(path: Path) -> dict[str, Any]:
    """读取 JSON 文件并确保顶层数据为对象。"""
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"expected JSON object: {path}")
    return data


def _stable_id(prefix: str, *parts: str) -> str:
    """对多个来源字段做哈希并生成带前缀的稳定标识。"""
    digest = hashlib.sha256(
        "\x1f".join(parts).encode("utf-8", errors="replace")
    ).hexdigest()
    return f"{prefix}_{digest[:24]}"


def _clean_text(value: Any) -> str | None:
    """清除文本中的空字符、换行差异和首尾空白。"""
    if not isinstance(value, str):
        return None
    cleaned = value.replace("\x00", "").replace("\r\n", "\n").strip()
    return cleaned or None


def _to_int(value: Any) -> int | None:
    """将任意可选值安全转换为整数。"""
    try:
        return int(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


def _first_nonempty(*values: Any) -> Any:
    """返回参数列表中的第一个非空值。"""
    return next((value for value in values if value not in (None, "")), None)
