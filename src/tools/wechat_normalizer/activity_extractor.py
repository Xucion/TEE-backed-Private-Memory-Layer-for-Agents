from __future__ import annotations

import json
import os
import re
from collections import defaultdict
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
12. 输出严格 JSON，不输出 Markdown。

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
MANDATORY_SIGNALS = (
    "必须",
    "务必",
    "请于",
    "截止",
    "提交",
    "填写",
    "完成",
    "不得",
    "统一",
    "缴费",
    "缴纳",
)
OPTIONAL_SIGNALS = (
    "欢迎",
    "感兴趣",
    "自愿",
    "积极报名",
    "积极参加",
    "可参加",
    "诚邀",
    "招募",
    "报名中",
)
DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:T00:00:00(?:[+-]\d{2}:\d{2}|Z)?)?$")


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
    """按时间段迭代发送给 LLM 的活动提取载荷。"""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        context_group_id = record.get("context_group_id")
        if isinstance(context_group_id, str) and context_group_id:
            groups[context_group_id].append(record)

    ordered_groups = sorted(
        groups.items(),
        key=lambda item: (
            min(
                (
                    str(record.get("occurred_at") or "")
                    for record in item[1]
                ),
                default="",
            ),
            item[0],
        ),
    )
    for context_group_id, group_records in ordered_groups:
        group_records = sorted(
            group_records,
            key=lambda item: (
                item.get("occurred_at") or "",
                item.get("source_index", 0),
                item.get("sequence", 0) or 0,
                item.get("message_id") or "",
            ),
        )
        max_score = max(
            _candidate_score(record)
            for record in group_records
        )
        has_text = any(_message_text(record) for record in group_records)
        if not has_text:
            continue
        if not include_all and max_score < minimum_candidate_score:
            continue
        yield build_group_payload(context_group_id, group_records)


def build_group_payload(
    context_group_id: str,
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    """构造单个时间段发送给 LLM 的载荷。"""
    messages = []
    images = []
    for record in records:
        message_id = str(record.get("message_id") or "")
        if not message_id:
            continue

        if _message_text(record):
            messages.append(
                {
                    "message_id": message_id,
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
    with output_jsonl.open("w", encoding="utf-8", newline="\n") as handle:
        for payload in payloads:
            response_text = _invoke_llm(llm, payload)
            data = _parse_llm_json(response_text)
            for activity in normalize_activity_response(data, payload):
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

        related_image_ids = [
            item
            for item in _string_list(raw.get("related_image_message_ids"))
            if item in image_by_message_id
        ]
        if not related_image_ids and image_by_message_id:
            related_image_ids = sorted(image_by_message_id)

        kind = _optional_string(raw.get("kind")) or "other"
        if kind not in VALID_KINDS:
            kind = "other"

        update_type = _optional_string(raw.get("update_type"))
        if update_type not in VALID_UPDATE_TYPES:
            update_type = None

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
                "confidence": confidence,
                "evidence_message_ids": evidence_ids,
                "evidence_quote": evidence_quote,
                "missing_information": _string_list(raw.get("missing_information")),
                "related_images": [
                    image_by_message_id[message_id]
                    for message_id in related_image_ids
                ],
            }
        )
    return normalized


def _build_chat_model(model_name: str | None = None) -> ChatTongyi:
    """构建活动提取使用的聊天模型。"""
    if not os.getenv("DASHSCOPE_API_KEY"):
        raise EnvironmentError("Missing DASHSCOPE_API_KEY. Set it before extraction.")
    return ChatTongyi(model=model_name or os.getenv("TONGYI_MODEL", "qwen-turbo"))


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
    has_mandatory_signal = any(signal in evidence for signal in MANDATORY_SIGNALS)
    has_optional_signal = any(signal in evidence for signal in OPTIONAL_SIGNALS)
    if has_optional_signal and re.search(r"(比赛|活动|报名)", evidence):
        if not re.search(r"(必须|务必|不得|统一参加|强制)", evidence):
            return False
    if has_optional_signal and not has_mandatory_signal:
        return False
    if has_optional_signal and not _has_strong_mandatory_action(evidence):
        return False
    if has_mandatory_signal:
        return True
    if kind == "mandatory_task":
        return False
    return raw_mandatory and has_mandatory_signal


def _has_strong_mandatory_action(text: str) -> bool:
    """判断指定条件是否成立。"""
    return bool(
        re.search(r"(必须|务必|不得|请于|截止)", text)
        or re.search(r"(提交|填写|完成|缴费|缴纳)", text)
    )


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
