from __future__ import annotations

import json
import os
import re
import hashlib
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Protocol

from langchain_community.chat_models import ChatTongyi
from langchain_core.messages import HumanMessage


ACTIVITY_GROUP_INSTRUCTION = """
你负责从同一个微信聊天时间段中提取活动、通知或必须完成的任务。
规则：
1. 只使用 messages 中明确出现的文字信息，不猜测时间、地点、报名方式或主办方。
2. images 只表示该时间段包含图片附件；不要分析图片内容，不要根据图片文件名、尺寸、hash 或存在性推测事实。
3. 如果图片和文字时间相近，可以把图片作为 related_images 返回，但事实依据仍必须来自文字消息。
4. 普通闲聊或无可执行事项时返回 {"activities": []}。
5. 每个活动必须包含 evidence_message_ids，且 ID 必须来自输入 messages。
6. 相对日期以消息 occurred_at_local 为基准；无法确定时返回 null，并把缺失项写入 missing_information。
7. 每个活动必须包含 evidence_quote，摘录能支撑标题、时间、地点、动作、是否必须的原文短句。
8. mandatory=true 只用于必须、务必、要求、请于、提交、填写、完成、不得、统一、全体同学等强制任务；欢迎、感兴趣、自愿、积极报名、招募、报名中属于可选活动，mandatory=false。
9. 如果原文只有日期，不要补 00:00:00；输出 YYYY-MM-DD。只有原文明确给出时刻时，才输出 ISO-8601 日期时间。
10. 如果原文是日期范围，输出 start_date 和 end_date；不要只保留开始日期。
11. related_images 只能表示同时间段附件，不得作为 title、deadline、location、required_action、mandatory 的证据。
12. 先判断每条消息是新事项、补充、更正、催办还是普通讨论；不要把同一事项的补充消息拆成多个活动。
13. existing_activities 是之前已经抽取的事项。只有当前消息明确补充、催办、更正或取消其中一项时，才填写 related_activity_id；禁止根据某个领域名称自行创建或复用主题键。
14. 同一条证据中的总事项和子步骤应合并成一个活动，不要分别输出。
15. 输出严格 JSON，不输出 Markdown。

示例：
- “欢迎全体师生积极报名飞盘比赛” => mandatory=false, kind="activity"
- “请6月17日17:00前提交材料” => mandatory=true, kind="mandatory_task"
""".strip()

ACTIVITY_GROUP_SCHEMA: dict[str, Any] = {
    "activities": [
        {
            "title": "string",
            "summary": "string|null",
            "kind": "activity|mandatory_task|announcement|update|other",
            "category_tags": ["string"],
            "mandatory": "boolean",
            "start_date": "YYYY-MM-DD|null",
            "end_date": "YYYY-MM-DD|null",
            "start_time": "ISO-8601 string|null",
            "deadline": "ISO-8601 string|null",
            "location": "string|null",
            "required_action": "string|null",
            "registration_url": "string|null",
            "eligibility": "string|null",
            "update_type": "new|correction|postponed|cancelled|null",
            "relation_type": "new|supplement|reminder|correction|postponed|cancelled",
            "related_activity_id": "existing activity_id|null",
            "confidence": "number from 0 to 1",
            "evidence_message_ids": ["string"],
            "evidence_quote": "string",
            "missing_information": ["string"],
            "related_image_message_ids": ["string"],
        }
    ]
}

VALID_KINDS = {"activity", "mandatory_task", "announcement", "update", "other"}
VALID_UPDATE_TYPES = {"new", "correction", "postponed", "cancelled", None}
VALID_RELATION_TYPES = {
    "new",
    "supplement",
    "reminder",
    "correction",
    "postponed",
    "cancelled",
}
OPTIONAL_SIGNALS = (
    "欢迎",
    "感兴趣",
    "自愿",
    "有意",
    "愿意",
    "想要",
    "积极报名",
    "积极参加",
    "可参加",
    "可以参加",
    "可自行",
    "可以自行",
    "自行填写",
    "自行报名",
    "自行参加",
    "诚邀",
    "招募",
    "报名中",
)
DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:T00:00:00(?:[+-]\d{2}:\d{2}|Z)?)?$")
CONTEXT_WINDOW_SECONDS = 30 * 60
CONTEXT_CANDIDATE_BOUNDARY_SECONDS = 8 * 60
CONTEXT_WINDOW_MAX_RECORDS = 13
CONTEXT_WINDOW_MAX_TEXT_CHARS = 6000
RELATED_IMAGE_MAX_DISTANCE_SECONDS = 15 * 60
RELATED_IMAGE_MAX_MESSAGE_DISTANCE = 5
RELATED_IMAGE_MIN_CONFIDENCE = 0.55
RELATED_IMAGE_BEST_SCORE_MARGIN = 0.15
MEDIA_REFERENCE_RE = re.compile(
    r"(见(?:上|下)?图|如图|如下|上面|下面|刚才|刚发|附件|图片|截图|"
    r"二维码|扫码|海报|文件|文档|表格)"
)
OPTIONAL_EXPRESSION_RE = re.compile(
    r"(?:欢迎|自愿|有意|感兴趣|愿意|想要|希望).{0,12}(?:参加|报名|填写|领取|加入)"
    r"|(?:可|可以|如有需要).{0,8}(?:自行)?(?:参加|报名|填写|选择|领取|加入)"
    r"|自行(?:参加|报名|填写|选择|领取|加入)"
)
STRONG_MANDATORY_RE = re.compile(
    r"(必须|务必|不得|禁止|应当|一定|须|请于|需在|需要在|最迟|截止|统一由|"
    r"统一(?:提交|收集|办理|参加|盖章)|"
    r"需要.{0,16}(?:提交|填写|完成|缴纳|办理|确认|反馈|执行)|"
    r"请.{0,16}(?:提交|填写|完成|缴纳|办理|确认|反馈)|"
    r"(?:提交|填写|完成|缴纳|办理|确认|反馈).{0,12}(?:前|以内|之内)|"
    r"(?:前|以内|之内).{0,8}(?:提交|填写|完成|缴纳|办理|确认|反馈))"
)
AMBIGUOUS_RELATIVE_TIME_RE = re.compile(
    r"(今天|今日|明天|明日|当天).{0,12}(?<!上午)(?<!下午)(?<!晚上)(?<!中午)"
    r"(?<!凌晨)(?<!早上)(?<!早晨)(\d{1,2})\s*(?::|点)"
)


class ChatModel(Protocol):
    def invoke(self, messages: list[HumanMessage]) -> Any:
        """调用当前函数的核心逻辑。"""
        ...


def load_normalized_messages(path: Path) -> list[dict[str, Any]]:
    """从 JSONL 读取规范化后的微信消息。"""
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError(f"line {line_number} in {path} is not an object")
            records.append(record)
    return records


def iter_group_payloads(
    records: Iterable[dict[str, Any]],
    *,
    minimum_candidate_score: float = 0.3,
    include_all: bool = False,
) -> Iterable[dict[str, Any]]:
    """围绕候选消息构建有界上下文窗口，避免把时间段当作事项边界。"""
    conversations: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        conversation_id = str(record.get("conversation_id") or "conversation_unknown")
        conversations[conversation_id].append(record)

    for conversation_id, conversation_records in sorted(conversations.items()):
        ordered = sorted(
            conversation_records,
            key=lambda item: (
                item.get("occurred_at") or "",
                item.get("source_index", 0),
                item.get("sequence", 0) or 0,
                item.get("message_id") or "",
            ),
        )
        anchors = [
            index
            for index, record in enumerate(ordered)
            if _message_text(record)
            and (include_all or _candidate_score(record) >= minimum_candidate_score)
        ]
        ranges = [
            _context_window_range(
                ordered,
                anchor,
                candidate_boundary_score=minimum_candidate_score,
            )
            for anchor in anchors
        ]
        for start, end in _merge_overlapping_ranges(ranges, ordered):
            window_records = ordered[start:end]
            if not any(_message_text(record) for record in window_records):
                continue
            window_id = _window_id(conversation_id, window_records)
            yield build_group_payload(window_id, window_records)


def _context_window_range(
    records: list[dict[str, Any]],
    anchor_index: int,
    *,
    candidate_boundary_score: float,
) -> tuple[int, int]:
    """为候选锚点构建受时间、消息数和文本长度约束的上下文窗口。"""
    anchor_time = _record_timestamp(records[anchor_index])
    selected = {anchor_index}
    text_chars = len(_message_text(records[anchor_index]))
    next_index = {-1: anchor_index - 1, 1: anchor_index + 1}
    active = {-1: True, 1: True}

    while any(active.values()) and len(selected) < CONTEXT_WINDOW_MAX_RECORDS:
        made_progress = False
        for direction in (-1, 1):
            if not active[direction]:
                continue
            index = next_index[direction]
            if not 0 <= index < len(records):
                active[direction] = False
                continue
            if len(selected) >= CONTEXT_WINDOW_MAX_RECORDS:
                break
            current_time = _record_timestamp(records[index])
            if (
                anchor_time is not None
                and current_time is not None
                and abs((current_time - anchor_time).total_seconds())
                > CONTEXT_WINDOW_SECONDS
            ):
                active[direction] = False
                continue
            if (
                anchor_time is not None
                and current_time is not None
                and _candidate_score(records[index]) >= candidate_boundary_score
                and abs((current_time - anchor_time).total_seconds())
                > CONTEXT_CANDIDATE_BOUNDARY_SECONDS
            ):
                active[direction] = False
                continue
            current_chars = len(_message_text(records[index]))
            if text_chars + current_chars > CONTEXT_WINDOW_MAX_TEXT_CHARS:
                active[direction] = False
                continue
            selected.add(index)
            text_chars += current_chars
            next_index[direction] += direction
            made_progress = True
        if not made_progress:
            break

    return min(selected), max(selected) + 1


def _merge_overlapping_ranges(
    ranges: list[tuple[int, int]],
    records: list[dict[str, Any]],
) -> list[tuple[int, int]]:
    """在窗口预算内合并重叠范围，避免连续候选形成无限上下文。"""
    merged: list[list[int]] = []
    for start, end in sorted(ranges):
        if not merged:
            merged.append([start, end])
            continue
        combined_start = merged[-1][0]
        combined_end = max(merged[-1][1], end)
        combined_records = records[combined_start:combined_end]
        combined_chars = sum(len(_message_text(record)) for record in combined_records)
        can_merge = (
            start < merged[-1][1]
            and len(combined_records) <= CONTEXT_WINDOW_MAX_RECORDS
            and combined_chars <= CONTEXT_WINDOW_MAX_TEXT_CHARS
        )
        if can_merge:
            merged[-1][1] = combined_end
        elif [start, end] != merged[-1]:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def _window_id(
    conversation_id: str,
    records: list[dict[str, Any]],
) -> str:
    """根据会话和窗口边界生成稳定标识。"""
    boundary_ids = [
        str(records[0].get("message_id") or ""),
        str(records[-1].get("message_id") or ""),
    ]
    digest = hashlib.sha256(
        "\x1f".join([conversation_id, *boundary_ids]).encode("utf-8")
    ).hexdigest()
    return f"window_{digest[:24]}"


def build_group_payload(
    context_group_id: str,
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    """构造单个时间段发送给 LLM 的载荷。"""
    messages = []
    images = []
    source_context_group_ids = []
    for context_order, record in enumerate(records):
        message_id = str(record.get("message_id") or "")
        if not message_id:
            continue
        source_group_id = str(record.get("context_group_id") or "")
        if source_group_id and source_group_id not in source_context_group_ids:
            source_context_group_ids.append(source_group_id)

        if _message_text(record):
            messages.append(
                {
                    "message_id": message_id,
                    "context_order": context_order,
                    "source_index": record.get("source_index"),
                    "sender_id": record.get("sender_id"),
                    "sender_role": record.get("sender_role"),
                    "message_type": record.get("message_type"),
                    "occurred_at_local": record.get("occurred_at_local"),
                    "observed_at_local": record.get("observed_at_local"),
                    "title": record.get("title"),
                    "text": record.get("text"),
                    "url": record.get("url"),
                    "llm_text": record.get("llm_text"),
                    "activity_features": record.get("activity_features", {}),
                }
            )

        for media in record.get("media", []) or []:
            if not isinstance(media, dict):
                continue
            if media.get("kind") != "image":
                continue
            images.append(
                {
                    "message_id": message_id,
                    "context_order": context_order,
                    "source_index": record.get("source_index"),
                    "sender_id": record.get("sender_id"),
                    "sender_role": record.get("sender_role"),
                    "occurred_at_local": record.get("occurred_at_local"),
                    "relative_path": media.get("relative_path"),
                    "mime_type": media.get("mime_type"),
                    "size_bytes": media.get("size_bytes"),
                    "sha256": media.get("sha256"),
                    "width": media.get("width"),
                    "height": media.get("height"),
                    "analysis_status": media.get("analysis_status"),
                    "warnings": media.get("warnings", []),
                }
            )

    return {
        "instruction": ACTIVITY_GROUP_INSTRUCTION,
        "output_schema": ACTIVITY_GROUP_SCHEMA,
        "context_group_id": context_group_id,
        "source_context_group_ids": source_context_group_ids,
        "messages": messages,
        "images": images,
    }


def extract_activities_from_jsonl(
    normalized_jsonl: Path,
    output_jsonl: Path,
    *,
    model_name: str | None = None,
    minimum_candidate_score: float = 0.3,
    include_all: bool = False,
    dry_run_payloads: Path | None = None,
    chat_model: ChatModel | None = None,
) -> int:
    """提取活动列表、from、jsonl。"""
    records = load_normalized_messages(normalized_jsonl)
    payloads = list(
        iter_group_payloads(
            records,
            minimum_candidate_score=minimum_candidate_score,
            include_all=include_all,
        )
    )

    if dry_run_payloads is not None:
        dry_run_payloads.parent.mkdir(parents=True, exist_ok=True)
        with dry_run_payloads.open("w", encoding="utf-8", newline="\n") as handle:
            for payload in payloads:
                handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
                handle.write("\n")
        return 0

    llm = chat_model or _build_chat_model(model_name)
    output_jsonl.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    known_activities: dict[str, dict[str, Any]] = {}
    with output_jsonl.open("w", encoding="utf-8", newline="\n") as handle:
        for payload in payloads:
            model_payload = {
                **payload,
                "existing_activities": _recent_activity_context(known_activities),
            }
            response_text = _invoke_llm(llm, model_payload)
            data = _parse_llm_json(response_text)
            normalized = normalize_activity_response(data, model_payload)
            normalized = _collapse_same_evidence_activities(normalized)
            normalized = _assign_activity_threads(normalized, known_activities)
            for activity in normalized:
                handle.write(json.dumps(activity, ensure_ascii=False, sort_keys=True))
                handle.write("\n")
                written += 1
    return written


def normalize_activity_response(
    data: Any,
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    """校验并规范化 LLM 返回的活动列表。"""
    raw_activities = _raw_activities(data)
    message_by_id = {
        str(message["message_id"]): message
        for message in payload.get("messages", [])
        if message.get("message_id")
    }
    allowed_message_ids = set(message_by_id)
    image_by_message_id = {
        str(image["message_id"]): image
        for image in payload.get("images", [])
        if image.get("message_id")
    }

    normalized: list[dict[str, Any]] = []
    for raw in raw_activities:
        if not isinstance(raw, dict):
            continue
        title = _optional_string(raw.get("title"))
        if not title:
            continue

        evidence_ids = [
            item
            for item in _string_list(raw.get("evidence_message_ids"))
            if item in allowed_message_ids
        ]
        if not evidence_ids:
            continue

        requested_image_ids = [
            item
            for item in _string_list(raw.get("related_image_message_ids"))
            if item in image_by_message_id
        ]
        related_image_matches = _related_image_matches(
            evidence_ids,
            requested_image_ids,
            message_by_id,
            image_by_message_id,
        )

        kind = _optional_string(raw.get("kind")) or "other"
        if kind not in VALID_KINDS:
            kind = "other"

        update_type = _optional_string(raw.get("update_type"))
        if update_type not in VALID_UPDATE_TYPES:
            update_type = None
        relation_type = _optional_string(raw.get("relation_type")) or "new"
        if relation_type not in VALID_RELATION_TYPES:
            relation_type = "new"
        related_activity_id = _optional_string(raw.get("related_activity_id"))

        confidence = _float_in_range(raw.get("confidence"), default=0.7)
        evidence_text = _evidence_text(evidence_ids, message_by_id)
        evidence_quote = _optional_string(raw.get("evidence_quote"))
        if evidence_quote and evidence_quote not in evidence_text:
            evidence_quote = None

        mandatory = _normalize_mandatory(
            raw_mandatory=bool(raw.get("mandatory")),
            kind=kind,
            evidence_text=evidence_text,
            evidence_quote=evidence_quote,
        )
        if mandatory:
            if kind == "activity":
                kind = "mandatory_task"
        elif kind == "mandatory_task":
            kind = "activity"

        start_date = _normalize_date_only(raw.get("start_date"))
        end_date = _normalize_date_only(raw.get("end_date"))
        start_time = _normalize_datetime_or_date(raw.get("start_time"))
        deadline = _normalize_datetime_or_date(raw.get("deadline"))
        missing_information = _string_list(raw.get("missing_information"))
        deadline, missing_information = _validate_deadline(
            deadline,
            evidence_text,
            evidence_ids,
            message_by_id,
            missing_information,
        )
        inferred_start_date, inferred_end_date = _infer_date_range(
            evidence_text,
            evidence_ids,
            message_by_id,
        )
        start_date = start_date or inferred_start_date
        end_date = end_date or inferred_end_date
        if start_time and DATE_ONLY_RE.fullmatch(start_time):
            start_date = start_date or start_time[:10]
            start_time = None

        normalized.append(
            {
                "schema_version": "wechat-extracted-activity/v1",
                "context_group_id": payload["context_group_id"],
                "source_context_group_ids": list(
                    payload.get("source_context_group_ids", [])
                ),
                "title": title,
                "summary": _optional_string(raw.get("summary")),
                "kind": kind,
                "category_tags": _string_list(raw.get("category_tags")),
                "mandatory": mandatory,
                "start_date": start_date,
                "end_date": end_date,
                "start_time": start_time,
                "deadline": deadline,
                "location": _optional_string(raw.get("location")),
                "required_action": _optional_string(raw.get("required_action")),
                "registration_url": _optional_string(raw.get("registration_url")),
                "eligibility": _optional_string(raw.get("eligibility")),
                "update_type": update_type,
                "relation_type": relation_type,
                "related_activity_id": related_activity_id,
                "confidence": confidence,
                "evidence_message_ids": evidence_ids,
                "evidence_quote": evidence_quote,
                "missing_information": missing_information,
                "related_images": [
                    {
                        **image_by_message_id[message_id],
                        "association_confidence": score,
                        "association_reason": "；".join(reasons),
                        "association_role": role,
                    }
                    for message_id, score, reasons, role in related_image_matches
                ],
            }
        )
    return normalized


def _recent_activity_context(
    known_activities: dict[str, dict[str, Any]],
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """提供最近事项的最小结构，供模型判断补充关系。"""
    values = list(known_activities.values())[-limit:]
    return [
        {
            "activity_id": activity["activity_id"],
            "title": activity.get("title"),
            "summary": activity.get("summary"),
            "required_action": activity.get("required_action"),
            "deadline": activity.get("deadline"),
            "location": activity.get("location"),
            "evidence_quote": activity.get("evidence_quote"),
        }
        for activity in values
    ]


def _assign_activity_threads(
    activities: list[dict[str, Any]],
    known_activities: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """根据显式关联生成内部线程 ID，不使用领域主题名称。"""
    assigned = []
    for activity in activities:
        activity_id = _activity_id(activity)
        relation_type = activity.get("relation_type") or "new"
        related_activity_id = activity.get("related_activity_id")
        related = known_activities.get(str(related_activity_id or ""))
        if relation_type != "new" and related is not None:
            thread_id = str(related.get("thread_id") or related["activity_id"])
        else:
            thread_id = activity_id
            related_activity_id = None
            relation_type = "new"

        activity["activity_id"] = activity_id
        activity["thread_id"] = thread_id
        activity["relation_type"] = relation_type
        activity["related_activity_id"] = related_activity_id
        known_activities[activity_id] = activity
        assigned.append(activity)
    return assigned


def _activity_id(activity: dict[str, Any]) -> str:
    """根据来源证据和窗口生成稳定活动 ID。"""
    evidence_ids = sorted(_string_list(activity.get("evidence_message_ids")))
    seed = "\x1f".join(
        [
            str(activity.get("context_group_id") or ""),
            *evidence_ids,
            str(activity.get("title") or ""),
        ]
    )
    return f"activity_{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:24]}"


def _collapse_same_evidence_activities(
    activities: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """合并同一证据上具有包含关系的总事项和子步骤。"""
    collapsed: list[dict[str, Any]] = []
    for activity in activities:
        match_index = next(
            (
                index
                for index, existing in enumerate(collapsed)
                if _same_evidence(existing, activity)
                and _titles_related(
                    str(existing.get("title") or ""),
                    str(activity.get("title") or ""),
                )
            ),
            None,
        )
        if match_index is None:
            collapsed.append(activity)
            continue
        collapsed[match_index] = _merge_extracted_activity(
            collapsed[match_index],
            activity,
        )
    return collapsed


def _same_evidence(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """判断两个活动是否由完全相同的一组消息支撑。"""
    left_ids = set(_string_list(left.get("evidence_message_ids")))
    right_ids = set(_string_list(right.get("evidence_message_ids")))
    return bool(left_ids) and left_ids == right_ids


def _titles_related(left: str, right: str) -> bool:
    """使用包含关系和字符二元组判断标题是否描述同一事项层级。"""
    left_key = _normalize_relation_text(left)
    right_key = _normalize_relation_text(right)
    if not left_key or not right_key:
        return False
    if left_key in right_key or right_key in left_key:
        return True
    left_terms = _character_ngrams(left_key)
    right_terms = _character_ngrams(right_key)
    overlap = left_terms & right_terms
    return bool(overlap) and len(overlap) / min(len(left_terms), len(right_terms)) >= 0.5


def _normalize_relation_text(value: str) -> str:
    """移除标题中的通用动作和格式噪声，仅用于包含性比较。"""
    text = re.sub(r"\s+", "", value.lower())
    text = re.sub(r"(提交|填写|完成|办理|参加|报名|通知|任务|要求)", "", text)
    return re.sub(r"[^\w\u4e00-\u9fff]", "", text)


def _character_ngrams(value: str, size: int = 2) -> set[str]:
    """生成字符 n-gram，兼容中英文短标题。"""
    if len(value) < size:
        return {value} if value else set()
    return {value[index:index + size] for index in range(len(value) - size + 1)}


def _merge_extracted_activity(
    left: dict[str, Any],
    right: dict[str, Any],
) -> dict[str, Any]:
    """合并同一证据产生的包含性活动。"""
    merged = json.loads(json.dumps(left, ensure_ascii=False))
    for key in (
        "summary",
        "start_date",
        "end_date",
        "start_time",
        "deadline",
        "location",
        "required_action",
        "registration_url",
        "eligibility",
        "update_type",
        "evidence_quote",
    ):
        if merged.get(key) in (None, "", []):
            merged[key] = right.get(key)
    if len(str(right.get("title") or "")) > len(str(merged.get("title") or "")):
        merged["title"] = right.get("title")
    merged["mandatory"] = bool(merged.get("mandatory")) or bool(right.get("mandatory"))
    merged["confidence"] = max(
        _float_in_range(merged.get("confidence"), default=0.0),
        _float_in_range(right.get("confidence"), default=0.0),
    )
    for key in (
        "category_tags",
        "evidence_message_ids",
        "missing_information",
        "related_images",
    ):
        merged[key] = _unique_values(
            list(merged.get(key, []) or []) + list(right.get(key, []) or [])
        )
    return merged


def _unique_values(values: list[Any]) -> list[Any]:
    """按 JSON 表示稳定去重任意列表值。"""
    result = []
    seen = set()
    for value in values:
        marker = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if marker not in seen:
            result.append(value)
            seen.add(marker)
    return result


def _related_image_matches(
    evidence_ids: list[str],
    requested_image_ids: list[str],
    message_by_id: dict[str, dict[str, Any]],
    image_by_message_id: dict[str, dict[str, Any]],
) -> list[tuple[str, float, list[str], str]]:
    """按时间、消息距离、发送者和文字引用信号关联图片。"""
    evidence_messages = [
        message_by_id[message_id]
        for message_id in evidence_ids
        if message_id in message_by_id
    ]
    if not evidence_messages:
        return []

    related: list[tuple[float, str, list[str], str]] = []
    requested_set = set(requested_image_ids)
    candidates = sorted(image_by_message_id)
    for message_id in candidates:
        image = image_by_message_id[message_id]
        best_score = 0.0
        best_reasons: list[str] = []
        best_role = "unresolved"
        for evidence in evidence_messages:
            score, reasons = _image_association(
                evidence,
                image,
                requested=message_id in requested_set,
            )
            if score > best_score:
                best_score = score
                best_reasons = reasons
                best_role = _image_association_role(_message_text(evidence))
        if best_score >= RELATED_IMAGE_MIN_CONFIDENCE:
            related.append((best_score, message_id, best_reasons, best_role))
    if not related:
        return []
    highest = max(score for score, _, _, _ in related)
    return [
        (message_id, score, reasons, role)
        for score, message_id, reasons, role in sorted(
            related,
            key=lambda item: (-item[0], item[1]),
        )
        if score >= highest - RELATED_IMAGE_BEST_SCORE_MARGIN
    ]


def _image_association(
    evidence: dict[str, Any],
    image: dict[str, Any],
    *,
    requested: bool,
) -> tuple[float, list[str]]:
    """计算图片与单条证据消息的通用关联置信度。"""
    evidence_time = _parse_local_timestamp(evidence.get("occurred_at_local"))
    image_time = _parse_local_timestamp(image.get("occurred_at_local"))
    if evidence_time is None or image_time is None:
        return 0.0, []
    if evidence_time.date() != image_time.date():
        return 0.0, []
    seconds = abs((image_time - evidence_time).total_seconds())
    if seconds > RELATED_IMAGE_MAX_DISTANCE_SECONDS:
        return 0.0, []

    evidence_order = _optional_int(evidence.get("context_order"))
    image_order = _optional_int(image.get("context_order"))
    if evidence_order is None or image_order is None:
        message_distance = RELATED_IMAGE_MAX_MESSAGE_DISTANCE
    else:
        message_distance = abs(image_order - evidence_order)
    if message_distance > RELATED_IMAGE_MAX_MESSAGE_DISTANCE:
        return 0.0, []

    reasons = []
    if seconds <= 30:
        score = 0.45
    elif seconds <= 90:
        score = 0.35
    elif seconds <= 5 * 60:
        score = 0.25
    else:
        score = 0.1
    reasons.append(f"相隔{int(seconds)}秒")

    if message_distance <= 1:
        score += 0.25
    elif message_distance <= 3:
        score += 0.15
    else:
        score += 0.05
    reasons.append(f"相隔{message_distance}条消息")

    evidence_text = _message_text(evidence)
    if MEDIA_REFERENCE_RE.search(evidence_text):
        score += 0.25
        reasons.append("相邻文字包含媒体引用表达")
    if (
        evidence.get("sender_id")
        and evidence.get("sender_id") == image.get("sender_id")
    ):
        score += 0.1
        reasons.append("发送者一致")
    if requested:
        score += 0.1
        reasons.append("模型标记为相关附件")
    return round(min(1.0, score), 3), reasons


def _image_association_role(evidence_text: str) -> str:
    """仅根据邻近文字给图片标记通用用途，不推断图片内容。"""
    if (
        re.search(r"(二维码|扫码)", evidence_text)
        and re.search(r"(报名|参加|加入|进群|登记)", evidence_text)
    ):
        return "registration_qr"
    if re.search(r"(附件|文件|文档|表格|材料)", evidence_text):
        return "form_or_document"
    if re.search(r"(海报|宣传图|通知图)", evidence_text):
        return "poster"
    if MEDIA_REFERENCE_RE.search(evidence_text):
        return "supporting_image"
    return "unresolved"


def _parse_local_timestamp(value: Any) -> datetime | None:
    """解析规范化消息使用的 ISO 本地时间戳。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        for pattern in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"):
            try:
                return datetime.strptime(text, pattern)
            except ValueError:
                continue
        return None


def _record_timestamp(record: dict[str, Any]) -> datetime | None:
    """读取记录的首选时间戳。"""
    return _parse_local_timestamp(
        record.get("occurred_at_local") or record.get("occurred_at")
    )


def _build_chat_model(model_name: str | None = None) -> ChatTongyi:
    """构建活动提取使用的聊天模型。"""
    if not os.getenv("DASHSCOPE_API_KEY"):
        raise EnvironmentError("Missing DASHSCOPE_API_KEY. Set it before extraction.")
    return ChatTongyi(model=model_name or os.getenv("TONGYI_MODEL", "qwen-max"))


def _invoke_llm(llm: ChatModel, payload: dict[str, Any]) -> str:
    """调用llm。"""
    prompt = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    response = llm.invoke([HumanMessage(content=prompt)])
    content = getattr(response, "content", response)
    if not isinstance(content, str):
        raise TypeError("LLM response content is not a string")
    return content


def _parse_llm_json(text: str) -> Any:
    """解析llm、JSON 数据。"""
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"(\{.*\}|\[.*\])", cleaned, flags=re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(1))


def _raw_activities(data: Any) -> list[Any]:
    """从 LLM 响应中取出原始活动列表。"""
    if isinstance(data, dict):
        activities = data.get("activities", [])
        return activities if isinstance(activities, list) else []
    if isinstance(data, list):
        return data
    return []


def _candidate_score(record: dict[str, Any]) -> float:
    """计算文本时间段进入 LLM 的候选分数。"""
    try:
        return float(record.get("activity_features", {}).get("candidate_score", 0.0))
    except (TypeError, ValueError):
        return 0.0


def _message_text(record: dict[str, Any]) -> str:
    """提取规范化消息的文本内容。"""
    parts = [
        record.get("title"),
        record.get("text"),
        record.get("url"),
    ]
    return "\n".join(str(part) for part in parts if part)


def _optional_string(value: Any) -> str | None:
    """把可选值规范化为字符串。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _string_list(value: Any) -> list[str]:
    """把输入值规范化为字符串列表。"""
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        text = _optional_string(item)
        if text:
            result.append(text)
    return result


def _evidence_text(
    evidence_ids: list[str],
    message_by_id: dict[str, dict[str, Any]],
) -> str:
    """生成活动证据的可读文本。"""
    parts = []
    for message_id in evidence_ids:
        message = message_by_id.get(message_id)
        if not message:
            continue
        for key in ("title", "text", "url", "llm_text"):
            value = _optional_string(message.get(key))
            if value:
                parts.append(value)
    return "\n".join(parts)


def _normalize_mandatory(
    *,
    raw_mandatory: bool,
    kind: str,
    evidence_text: str,
    evidence_quote: str | None,
) -> bool:
    """规范化mandatory。"""
    evidence = "\n".join(part for part in (evidence_quote, evidence_text) if part)
    has_optional_signal = (
        any(signal in evidence for signal in OPTIONAL_SIGNALS)
        or bool(OPTIONAL_EXPRESSION_RE.search(evidence))
    )
    has_strong_mandatory_signal = bool(STRONG_MANDATORY_RE.search(evidence))
    if has_optional_signal and not has_strong_mandatory_signal:
        return False
    if has_strong_mandatory_signal:
        return True
    if kind == "mandatory_task":
        return False
    return raw_mandatory and has_strong_mandatory_signal


def _validate_deadline(
    deadline: str | None,
    evidence_text: str,
    evidence_ids: list[str],
    message_by_id: dict[str, dict[str, Any]],
    missing_information: list[str],
) -> tuple[str | None, list[str]]:
    """拒绝把已经过去的模糊相对时刻确定为截止时间。"""
    if not deadline:
        return deadline, missing_information
    deadline_time = _parse_local_timestamp(deadline)
    if deadline_time is None:
        return deadline, missing_information
    evidence_time = next(
        (
            parsed
            for message_id in evidence_ids
            if (
                parsed := _parse_local_timestamp(
                    message_by_id.get(message_id, {}).get("occurred_at_local")
                )
            )
        ),
        None,
    )
    ambiguous_match = AMBIGUOUS_RELATIVE_TIME_RE.search(evidence_text)
    if (
        evidence_time is not None
        and ambiguous_match
        and deadline_time.date() == evidence_time.date()
        and deadline_time < evidence_time
    ):
        return None, _unique_strings(
            [*missing_information, "截止时刻缺少上午/下午信息"]
        )
    return deadline, missing_information


def _unique_strings(values: list[str]) -> list[str]:
    """保持顺序地去重字符串。"""
    result = []
    seen = set()
    for value in values:
        text = str(value).strip()
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return result


def _optional_int(value: Any) -> int | None:
    """把可选值转换为整数。"""
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _normalize_date_only(value: Any) -> str | None:
    """规范化只包含日期的字符串。"""
    text = _optional_string(value)
    if not text:
        return None
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})(?:T00:00:00(?:[+-]\d{2}:\d{2}|Z)?)?", text)
    return match.group(1) if match else text


def _normalize_datetime_or_date(value: Any) -> str | None:
    """规范化日期或日期时间字符串。"""
    text = _optional_string(value)
    if not text:
        return None
    match = DATE_ONLY_RE.fullmatch(text)
    if match:
        return text[:10]
    return text


def _infer_date_range(
    evidence_text: str,
    evidence_ids: list[str],
    message_by_id: dict[str, dict[str, Any]],
) -> tuple[str | None, str | None]:
    """根据活动时间推断日期范围。"""
    match = re.search(
        r"(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*[-–—至到]\s*(?:(\d{1,2})\s*月\s*)?(\d{1,2})\s*日",
        evidence_text,
    )
    if not match:
        return None, None
    year = _evidence_year(evidence_ids, message_by_id)
    if not year:
        return None, None
    start_month = int(match.group(1))
    start_day = int(match.group(2))
    end_month = int(match.group(3) or start_month)
    end_day = int(match.group(4))
    return (
        f"{year}-{start_month:02d}-{start_day:02d}",
        f"{year}-{end_month:02d}-{end_day:02d}",
    )


def _evidence_year(
    evidence_ids: list[str],
    message_by_id: dict[str, dict[str, Any]],
) -> int | None:
    """从证据消息中推断年份。"""
    for message_id in evidence_ids:
        occurred_at_local = _optional_string(
            message_by_id.get(message_id, {}).get("occurred_at_local")
        )
        if occurred_at_local and re.match(r"^\d{4}-", occurred_at_local):
            return int(occurred_at_local[:4])
    return None


def _float_in_range(value: Any, *, default: float) -> float:
    """校验数值是否在指定范围内。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return round(max(0.0, min(1.0, number)), 3)
