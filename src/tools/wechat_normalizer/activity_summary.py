from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable


SUMMARY_SCHEMA_VERSION = "wechat-activity-summary/v1"


def load_extracted_activities(path: Path) -> list[dict[str, Any]]:
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
    merged = merge_activities(activities)
    mandatory_tasks = [
        activity
        for activity in merged
        if activity.get("mandatory") is True
        and activity.get("update_type") != "cancelled"
    ]
    optional = [
        activity
        for activity in merged
        if activity.get("mandatory") is not True
        and activity.get("update_type") != "cancelled"
    ]
    incomplete_items = [
        activity
        for activity in merged
        if activity.get("missing_information")
        and activity.get("update_type") != "cancelled"
    ]
    cancelled_or_updated = [
        activity
        for activity in merged
        if activity.get("update_type") in {"correction", "postponed", "cancelled"}
    ]

    recommended_activities = [
        activity
        for activity in optional
        if _activity_confidence(activity) >= 0.55
        or bool(activity.get("deadline"))
        or bool(activity.get("start_date"))
    ]
    recommended_ids = {_activity_identity(activity) for activity in recommended_activities}
    other_activities = [
        activity
        for activity in optional
        if _activity_identity(activity) not in recommended_ids
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
    buckets: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for activity in activities:
        if not isinstance(activity, dict):
            continue
        key = _dedupe_key(activity)
        existing = buckets.get(key)
        if existing is None:
            buckets[key] = _copy_activity(activity)
            continue
        buckets[key] = _merge_activity(existing, activity)
    return sorted(buckets.values(), key=_sort_key)


def _dedupe_key(activity: dict[str, Any]) -> tuple[str, str, str, str]:
    title = _normalize_key_text(activity.get("title"))
    deadline = _normalize_key_text(activity.get("deadline"))
    registration_url = _normalize_key_text(activity.get("registration_url"))
    context_group_id = _normalize_key_text(activity.get("context_group_id"))
    return title, deadline, registration_url, context_group_id


def _copy_activity(activity: dict[str, Any]) -> dict[str, Any]:
    copied = json.loads(json.dumps(activity, ensure_ascii=False))
    copied["evidence_message_ids"] = _unique_strings(
        copied.get("evidence_message_ids", [])
    )
    copied["category_tags"] = _unique_strings(copied.get("category_tags", []))
    copied["missing_information"] = _unique_strings(
        copied.get("missing_information", [])
    )
    copied["related_images"] = _unique_images(copied.get("related_images", []))
    return copied


def _merge_activity(
    left: dict[str, Any],
    right: dict[str, Any],
) -> dict[str, Any]:
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
    return merged


def _prefer_value(current: Any, candidate: Any) -> Any:
    if current not in (None, "", []):
        return current
    return candidate


def _unique_strings(values: Any) -> list[str]:
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


def _unique_images(values: Any) -> list[dict[str, Any]]:
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
    try:
        return float(activity.get("confidence", 0.0))
    except (TypeError, ValueError):
        return 0.0


def _activity_identity(activity: dict[str, Any]) -> tuple[str, str, str, str]:
    return _dedupe_key(activity)


def _normalize_key_text(value: Any) -> str:
    text = _optional_string(value) or ""
    return re.sub(r"\s+", " ", text).strip().lower()


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
