from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class UserMemoryProvider(Protocol):
    """Future adapter point for the project's privacy-preserving memory vault."""

    def get_preference_memories(self, user_id: str) -> list[str]:
        """读取指定用户经过隐私最小化的偏好记忆。"""
        ...


@dataclass
class PreferenceProfile:
    interested_tags: set[str]
    ignored_tags: set[str]
    mandatory_only: bool
    source_memories: list[str]


TAG_KEYWORDS = {
    "competition": ("比赛", "竞赛", "大赛", "挑战杯", "赛题"),
    "entertainment": ("娱乐", "文艺", "音乐", "电影", "演出", "联谊", "聚会", "游戏"),
    "sports": ("体育", "运动会", "篮球", "足球", "羽毛球", "乒乓球"),
    "academic": ("讲座", "学术", "论坛", "研讨", "论文", "课题"),
    "career": ("招聘", "实习", "就业", "宣讲"),
    "volunteer": ("志愿", "公益"),
}


def profile_from_memories(memories: list[str]) -> PreferenceProfile:
    """从自然语言记忆中构建兴趣与强制任务偏好画像。"""
    interested: set[str] = set()
    ignored: set[str] = set()
    mandatory_only = False

    for memory in memories:
        normalized = memory.strip()
        if not normalized:
            continue
        for tag, keywords in TAG_KEYWORDS.items():
            if any(keyword in normalized for keyword in keywords):
                if any(token in normalized for token in ("不关心", "不喜欢", "忽略")):
                    ignored.add(tag)
                else:
                    interested.add(tag)
        if (
            ("只" in normalized and "必须" in normalized)
            or "只需要知道必须" in normalized
        ):
            mandatory_only = True

    return PreferenceProfile(
        interested_tags=interested,
        ignored_tags=ignored,
        mandatory_only=mandatory_only,
        source_memories=[memory for memory in memories if memory.strip()],
    )


# Override mojibake literals above so Chinese preference memories can be matched.
TAG_KEYWORDS = {
    "competition": ("比赛", "竞赛", "大赛", "挑战杯", "赛题"),
    "entertainment": ("娱乐", "文艺", "音乐", "电影", "演出", "联谊", "聚会", "游戏"),
    "sports": ("体育", "运动会", "篮球", "足球", "羽毛球", "乒乓球"),
    "academic": ("讲座", "学术", "论坛", "研讨", "论文", "课题"),
    "career": ("招聘", "实习", "就业", "宣讲"),
    "volunteer": ("志愿", "公益"),
}


def profile_from_memories(memories: list[str]) -> PreferenceProfile:
    """Build an interest profile from minimized natural-language memories."""
    interested: set[str] = set()
    ignored: set[str] = set()
    mandatory_only = False

    for memory in memories:
        normalized = memory.strip()
        if not normalized:
            continue
        for tag, keywords in TAG_KEYWORDS.items():
            if any(keyword in normalized for keyword in keywords):
                if any(token in normalized for token in ("不关心", "不喜欢", "忽略")):
                    ignored.add(tag)
                else:
                    interested.add(tag)
        if (
            ("只" in normalized and "必须" in normalized)
            or "只需要知道必须" in normalized
        ):
            mandatory_only = True

    return PreferenceProfile(
        interested_tags=interested,
        ignored_tags=ignored,
        mandatory_only=mandatory_only,
        source_memories=[memory for memory in memories if memory.strip()],
    )


def score_for_profile(
    activity_features: dict[str, Any],
    profile: PreferenceProfile,
) -> dict[str, Any]:
    """依据用户偏好画像计算消息候选的推荐分数和原因。"""
    tags = set(activity_features.get("tags", []))
    mandatory = bool(activity_features.get("mandatory_signal"))
    score = float(activity_features.get("candidate_score", 0.0))
    reasons: list[str] = []

    matched = sorted(tags & profile.interested_tags)
    ignored = sorted(tags & profile.ignored_tags)
    if matched:
        score += min(0.45, 0.2 * len(matched))
        reasons.append(f"matched interests: {', '.join(matched)}")
    if ignored:
        score -= min(0.5, 0.25 * len(ignored))
        reasons.append(f"matched ignored categories: {', '.join(ignored)}")
    if mandatory:
        score += 0.35
        reasons.append("contains a mandatory-task signal")
    if profile.mandatory_only and not mandatory:
        score -= 0.8
        reasons.append("user requested mandatory tasks only")

    return {
        "score": round(max(0.0, min(1.0, score)), 3),
        "recommended": score >= 0.55,
        "matched_tags": matched,
        "reasons": reasons,
        "profile_source_count": len(profile.source_memories),
    }
