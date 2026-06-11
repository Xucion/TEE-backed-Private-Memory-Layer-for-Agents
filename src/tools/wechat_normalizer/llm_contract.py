from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator


ACTIVITY_EXTRACTION_INSTRUCTION = """
你负责从单条规范化微信消息中提取活动或必须完成的任务。

规则：
1. 只使用输入记录中明确出现的信息，不猜测时间、地点或报名要求。
2. 图片内容不会被分析。只能使用同一 context_group_id 内明确存在的文本，
   不能根据图片文件名、尺寸或存在性推测事实。
3. 普通闲聊返回 is_relevant=false。
4. 更正、延期、取消等消息必须标记 update_type。
5. 相对时间以 occurred_at_local 为基准解析；无法确定时保留原文并返回 null。
6. mandatory 表示用户必须完成，不表示所有活动都值得推荐。
7. 不生成领域主题键；后续关联使用 activity_id/thread_id。
8. 输出一个 JSON 对象，不输出 Markdown。
""".strip()


ACTIVITY_OUTPUT_SCHEMA: dict[str, Any] = {
    "is_relevant": "boolean",
    "kind": "activity|mandatory_task|announcement|update|other",
    "title": "string|null",
    "summary": "string|null",
    "category_tags": ["string"],
    "mandatory": "boolean",
    "start_time": "ISO-8601 string|null",
    "deadline": "ISO-8601 string|null",
    "location": "string|null",
    "required_action": "string|null",
    "registration_url": "string|null",
    "eligibility": "string|null",
    "update_type": "new|correction|postponed|cancelled|null",
    "confidence": "number from 0 to 1",
    "evidence_message_ids": ["string"],
    "missing_information": ["string"],
}

WEEKLY_SUMMARY_INSTRUCTION = """
你负责根据已经结构化的活动和任务生成研究生班级群周报。

要求：
1. 必须完成的任务置顶，并按截止时间排序。
2. 其余活动按用户偏好相关性排序；没有偏好时按截止时间和置信度排序。
3. 合并重复通知，保留更正、延期和取消状态。
4. 不补充输入中不存在的事实。
5. 每项保留 evidence_message_ids，便于回查原消息。
6. 明确区分：必须完成、推荐关注、其他活动、信息不完整。
7. 输出一个 JSON 对象，不输出 Markdown。
""".strip()

WEEKLY_SUMMARY_SCHEMA: dict[str, Any] = {
    "week": {
        "start": "YYYY-MM-DD",
        "end": "YYYY-MM-DD",
    },
    "mandatory_tasks": ["structured activity object"],
    "recommended_activities": ["structured activity object"],
    "other_activities": ["structured activity object"],
    "incomplete_items": ["structured activity object"],
    "brief_summary": "string",
}

# Keep prompt text in valid readable UTF-8. Mojibake here would be sent directly
# to the external LLM and make extraction unreliable.
ACTIVITY_EXTRACTION_INSTRUCTION = """
你负责从单条规范化微信消息中提取活动或必须完成的任务。
规则：
1. 只使用输入记录中明确出现的信息，不猜测时间、地点或报名要求。
2. 图片内容不会被分析，只能使用同一 context_group_id 内明确存在的文本；不能根据图片文件名、尺寸或存在性推测事实。
3. 普通闲聊返回 is_relevant=false。
4. 更正、延期、取消等消息必须标记 update_type。
5. 相对时间以 occurred_at_local 为基准解析；无法确定时保留原文并返回 null。
6. mandatory 表示用户必须完成，不表示所有活动都值得推荐。
7. 不生成领域主题键；后续关联使用 activity_id/thread_id。
8. 输出一个 JSON 对象，不输出 Markdown。
""".strip()

WEEKLY_SUMMARY_INSTRUCTION = """
你负责根据已经结构化的活动和任务生成研究生班级群周报。
要求：
1. 必须完成的任务置顶，并按截止时间排序。
2. 其余活动按用户偏好相关性排序；没有偏好时按截止时间和置信度排序。
3. 合并重复通知，保留更正、延期和取消状态。
4. 不补充输入中不存在的事实。
5. 每项保留 evidence_message_ids，便于回查原消息。
6. 明确区分：必须完成、推荐关注、其他活动、信息不完整。
7. 输出一个 JSON 对象，不输出 Markdown。
""".strip()


def build_extraction_payload(record: dict[str, Any]) -> dict[str, Any]:
    """为单条规范化消息构建与模型供应商无关的提取请求。"""
    return {
        "instruction": ACTIVITY_EXTRACTION_INSTRUCTION,
        "output_schema": ACTIVITY_OUTPUT_SCHEMA,
        "message": {
            "message_id": record["message_id"],
            "context_group_id": record["context_group_id"],
            "occurred_at_local": record.get("occurred_at_local"),
            "observed_at_local": record.get("observed_at_local"),
            "message_type": record["message_type"],
            "llm_text": record["llm_text"],
            "activity_features": record.get("activity_features", {}),
        },
    }


def iter_extraction_payloads(
    normalized_jsonl: Path,
    *,
    minimum_candidate_score: float = 0.3,
    include_all: bool = False,
) -> Iterator[dict[str, Any]]:
    """按候选分数遍历 JSONL 并生成逐条 LLM 提取请求。"""
    with normalized_jsonl.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError(
                    f"line {line_number} in {normalized_jsonl} is not an object"
                )
            score = float(
                record.get("activity_features", {}).get(
                    "candidate_score",
                    0.0,
                )
            )
            if include_all or score >= minimum_candidate_score:
                yield build_extraction_payload(record)


def build_weekly_summary_payload(
    extracted_items: list[dict[str, Any]],
    *,
    week_start: str,
    week_end: str,
    preference_memories: list[str] | None = None,
) -> dict[str, Any]:
    """为已提取并合并的活动构建个性化周报请求。"""
    return {
        "instruction": WEEKLY_SUMMARY_INSTRUCTION,
        "output_schema": WEEKLY_SUMMARY_SCHEMA,
        "week": {
            "start": week_start,
            "end": week_end,
        },
        "preference_memories": preference_memories or [],
        "items": extracted_items,
    }
