from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable


SUMMARY_SCHEMA_VERSION = "wechat-activity-summary/v1"


def load_extracted_activities(path: Path) -> list[dict[str, Any]]:
    """从 JSONL 读取已提取的活动。"""
    activities: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError(f"line {line_number} in {path} is not an object")
            activities.append(record)
    return activities


def build_activity_summary(
    activities: Iterable[dict[str, Any]],
    *,
    source_path: str | None = None,
) -> dict[str, Any]:
    """把活动列表合并成周报摘要结构。"""
    merged = merge_activities(activities)
    cancelled_or_updated = [
        activity
        for activity in merged
        if activity.get("update_type") in {"correction", "postponed", "cancelled"}
    ]
    active = [
        activity
        for activity in merged
        if activity.get("update_type") not in {"correction", "postponed", "cancelled"}
    ]
    mandatory_tasks = [
        activity
        for activity in active
        if activity.get("mandatory") is True
    ]
    optional = [
        activity
        for activity in active
        if activity.get("mandatory") is not True
    ]

    recommended_activities = [
        activity
        for activity in optional
        if _activity_confidence(activity) >= 0.55
        or bool(activity.get("deadline"))
        or bool(activity.get("start_date"))
    ]
    recommended_ids = {_activity_identity(activity) for activity in recommended_activities}
    optional_remaining = [
        activity
        for activity in optional
        if _activity_identity(activity) not in recommended_ids
    ]
    incomplete_items = [
        activity
        for activity in optional_remaining
        if activity.get("missing_information")
    ]
    incomplete_ids = {_activity_identity(activity) for activity in incomplete_items}
    other_activities = [
        activity
        for activity in optional_remaining
        if _activity_identity(activity) not in incomplete_ids
    ]

    mandatory_tasks = sorted(mandatory_tasks, key=_sort_key)
    recommended_activities = sorted(recommended_activities, key=_sort_key)
    other_activities = sorted(other_activities, key=_sort_key)
    incomplete_items = sorted(incomplete_items, key=_sort_key)
    cancelled_or_updated = sorted(cancelled_or_updated, key=_sort_key)

    return {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "source": source_path,
        "counts": {
            "input_activities": len(list(activities)) if isinstance(activities, list) else None,
            "merged_activities": len(merged),
            "mandatory_tasks": len(mandatory_tasks),
            "recommended_activities": len(recommended_activities),
            "other_activities": len(other_activities),
            "incomplete_items": len(incomplete_items),
            "cancelled_or_updated": len(cancelled_or_updated),
        },
        "mandatory_tasks": mandatory_tasks,
        "recommended_activities": recommended_activities,
        "other_activities": other_activities,
        "incomplete_items": incomplete_items,
        "cancelled_or_updated": cancelled_or_updated,
    }


def write_activity_summary(
    activities_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    """把活动汇总写入 JSON 文件。"""
    activities = load_extracted_activities(activities_path)
    summary = build_activity_summary(
        activities,
        source_path=str(activities_path),
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary


def merge_activities(
    activities: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """按内部线程和通用证据关系合并活动。"""
    merged: list[dict[str, Any]] = []
    for activity in activities:
        if not isinstance(activity, dict):
            continue
        match_index = next(
            (
                index
                for index, existing in enumerate(merged)
                if _activities_should_merge(existing, activity)
            ),
            None,
        )
        if match_index is None:
            merged.append(_copy_activity(activity))
            continue
        merged[match_index] = _merge_activity(merged[match_index], activity)
    return sorted(merged, key=_sort_key)


GENERIC_TITLE_RE = re.compile(
    r"^(?:请)?(?:提交|填写|完成|办理|缴纳|确认|反馈|报名|参加)"
    r"(?:相关|这个|该|上述)?(?:材料|表格|信息|文件|事项|任务)?$"
)


def _activities_should_merge(
    left: dict[str, Any],
    right: dict[str, Any],
) -> bool:
    """判断两个结果是否属于同一事项线程，不依赖领域主题名称。"""
    left_thread = _normalize_key_text(left.get("thread_id"))
    right_thread = _normalize_key_text(right.get("thread_id"))
    if left_thread and right_thread:
        return left_thread == right_thread

    left_activity_id = _normalize_key_text(left.get("activity_id"))
    right_activity_id = _normalize_key_text(right.get("activity_id"))
    if left_activity_id and left_activity_id == right_activity_id:
        return True

    left_evidence = set(_as_list(left.get("evidence_message_ids")))
    right_evidence = set(_as_list(right.get("evidence_message_ids")))
    if left_evidence & right_evidence:
        return _titles_related(left, right)

    left_url = _normalize_key_text(left.get("registration_url"))
    right_url = _normalize_key_text(right.get("registration_url"))
    if left_url and left_url == right_url:
        return True

    title = _normalize_key_text(left.get("title"))
    right_title = _normalize_key_text(right.get("title"))
    if (
        title
        and title == right_title
        and not GENERIC_TITLE_RE.fullmatch(title)
    ):
        return True

    left_groups = set(_as_list(left.get("source_context_group_ids")))
    right_groups = set(_as_list(right.get("source_context_group_ids")))
    if not left_groups:
        left_groups = {_optional_string(left.get("context_group_id"))}
    if not right_groups:
        right_groups = {_optional_string(right.get("context_group_id"))}
    same_context = bool((left_groups - {None}) & (right_groups - {None}))
    similarity = _activity_similarity(left, right)
    return similarity >= (0.32 if same_context else 0.62)


def _titles_related(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """判断共享证据的标题是否为总事项和子步骤关系。"""
    left_title = _relation_text(left.get("title"))
    right_title = _relation_text(right.get("title"))
    if not left_title or not right_title:
        return False
    if left_title in right_title or right_title in left_title:
        return True
    return _term_overlap(
        _character_ngrams(left_title),
        _character_ngrams(right_title),
    ) >= 0.5


def _activity_similarity(left: dict[str, Any], right: dict[str, Any]) -> float:
    """使用通用文本特征估算事项关系，仅作为旧数据兼容回退。"""
    left_terms = _activity_terms(left)
    right_terms = _activity_terms(right)
    if not left_terms or not right_terms:
        return 0.0
    return _term_overlap(left_terms, right_terms)


def _activity_terms(activity: dict[str, Any]) -> set[str]:
    """从事项结构生成字符特征，并过滤通用动作词。"""
    parts = [
        activity.get("title"),
        activity.get("summary"),
        activity.get("required_action"),
        activity.get("evidence_quote"),
        *_as_list(activity.get("evidence_quotes")),
    ]
    text = _relation_text(" ".join(str(part) for part in parts if part))
    return _character_ngrams(text)


def _relation_text(value: Any) -> str:
    """移除日期、链接和通用动作词，保留可比较的上下文特征。"""
    text = str(value or "").lower()
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\d+(?:[./:-]\d+)*", "", text)
    text = re.sub(
        r"(提交|填写|完成|办理|参加|报名|联系|确认|通知|任务|要求|"
        r"相关|这个|该|上述|材料|信息|表格|文件|纸质版|电子版|同学|"
        r"需要|请于|今天|明天|本周|周内|最迟|截止)",
        "",
        text,
    )
    return re.sub(r"[^\w\u4e00-\u9fff]", "", text)


def _character_ngrams(value: str, size: int = 2) -> set[str]:
    """生成适用于中英文短文本的字符特征。"""
    if len(value) < size:
        return {value} if value else set()
    return {value[index:index + size] for index in range(len(value) - size + 1)}


def _term_overlap(left: set[str], right: set[str]) -> float:
    """计算相对较短特征集合的覆盖率。"""
    if not left or not right:
        return 0.0
    return len(left & right) / min(len(left), len(right))


def _copy_activity(activity: dict[str, Any]) -> dict[str, Any]:
    """复制活动记录以便合并。"""
    copied = json.loads(json.dumps(activity, ensure_ascii=False))
    copied["evidence_message_ids"] = _unique_strings(
        copied.get("evidence_message_ids", [])
    )
    copied["category_tags"] = _unique_strings(copied.get("category_tags", []))
    copied["missing_information"] = _unique_strings(
        copied.get("missing_information", [])
    )
    copied["related_images"] = _unique_images(copied.get("related_images", []))
    copied["source_context_group_ids"] = _unique_strings(
        _as_list(copied.get("source_context_group_ids"))
        or [copied.get("context_group_id")]
    )
    copied["evidence_quotes"] = _unique_strings(
        _as_list(copied.get("evidence_quotes"))
        + ([copied.get("evidence_quote")] if copied.get("evidence_quote") else [])
    )
    return copied


def _merge_activity(
    left: dict[str, Any],
    right: dict[str, Any],
) -> dict[str, Any]:
    """合并活动数据。"""
    merged = _copy_activity(left)
    for key in (
        "title",
        "summary",
        "kind",
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
        "activity_id",
        "thread_id",
        "relation_type",
        "related_activity_id",
    ):
        merged[key] = _prefer_value(merged.get(key), right.get(key))

    merged["mandatory"] = bool(merged.get("mandatory")) or bool(right.get("mandatory"))
    merged["confidence"] = max(
        _activity_confidence(merged),
        _activity_confidence(right),
    )
    merged["category_tags"] = _unique_strings(
        list(merged.get("category_tags", [])) + list(right.get("category_tags", []))
    )
    merged["evidence_message_ids"] = _unique_strings(
        list(merged.get("evidence_message_ids", []))
        + list(right.get("evidence_message_ids", []))
    )
    merged["missing_information"] = _unique_strings(
        list(merged.get("missing_information", []))
        + list(right.get("missing_information", []))
    )
    merged["related_images"] = _unique_images(
        list(merged.get("related_images", []))
        + list(right.get("related_images", []))
    )
    merged["source_context_group_ids"] = _unique_strings(
        _as_list(merged.get("source_context_group_ids"))
        + _as_list(right.get("source_context_group_ids"))
        + [right.get("context_group_id")]
    )
    merged["evidence_quotes"] = _unique_strings(
        _as_list(merged.get("evidence_quotes"))
        + _as_list(right.get("evidence_quotes"))
        + ([right.get("evidence_quote")] if right.get("evidence_quote") else [])
    )
    return merged


def _prefer_value(current: Any, candidate: Any) -> Any:
    """从两个候选值中选择更有信息量的一项。"""
    if current not in (None, "", []):
        return current
    return candidate


def _unique_strings(values: Any) -> list[str]:
    """保留字符串列表中的唯一非空项。"""
    if not isinstance(values, list):
        return []
    result = []
    seen = set()
    for value in values:
        text = str(value).strip() if value is not None else ""
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return result


def _as_list(value: Any) -> list[Any]:
    """把可选列表字段安全转换为列表。"""
    return list(value) if isinstance(value, list) else []


def _unique_images(values: Any) -> list[dict[str, Any]]:
    """合并并去重相关图片列表。"""
    if not isinstance(values, list):
        return []
    result = []
    seen = set()
    for value in values:
        if not isinstance(value, dict):
            continue
        key = str(value.get("message_id") or value.get("sha256") or value)
        if key in seen:
            continue
        result.append(value)
        seen.add(key)
    return result


def _sort_key(activity: dict[str, Any]) -> tuple[str, str, int, str]:
    """生成活动排序 key。"""
    primary_time = (
        _optional_string(activity.get("deadline"))
        or _optional_string(activity.get("start_time"))
        or _optional_string(activity.get("start_date"))
        or "9999-99-99"
    )
    mandatory_rank = 0 if activity.get("mandatory") else 1
    return (
        primary_time,
        _normalize_key_text(activity.get("title")),
        mandatory_rank,
        _normalize_key_text(activity.get("context_group_id")),
    )


def _activity_confidence(activity: dict[str, Any]) -> float:
    """读取活动置信度。"""
    try:
        return float(activity.get("confidence", 0.0))
    except (TypeError, ValueError):
        return 0.0


def _activity_identity(activity: dict[str, Any]) -> str:
    """生成活动合并身份标识。"""
    thread_id = _normalize_key_text(activity.get("thread_id"))
    if thread_id:
        return f"thread:{thread_id}"
    activity_id = _normalize_key_text(activity.get("activity_id"))
    if activity_id:
        return f"activity:{activity_id}"
    evidence = ",".join(sorted(str(item) for item in _as_list(activity.get("evidence_message_ids"))))
    return "|".join(
        [
            evidence,
            _normalize_key_text(activity.get("title")),
            _normalize_key_text(activity.get("context_group_id")),
        ]
    )


def _normalize_key_text(value: Any) -> str:
    """规范化用于去重的文本 key。"""
    text = _optional_string(value) or ""
    return re.sub(r"\s+", " ", text).strip().lower()


def _optional_string(value: Any) -> str | None:
    """把可选值规范化为字符串。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None
