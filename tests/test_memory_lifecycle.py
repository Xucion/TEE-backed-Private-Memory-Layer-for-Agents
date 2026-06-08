#new store -> fact_key merge -> embedding merge -> slot conflict supersede
#-> expiration -> active-only retrieve -> access_count touch
#-> list -> forget

import sys
from pathlib import Path
from typing import Any

import numpy as np
from cryptography.fernet import Fernet


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from trusted import memory_retriever, memory_store
from trusted.vault_server import _validate_memory


class FakeEmbeddings:
    def embed_query(self, text: str) -> list[float]:
        """返回测试用的查询向量。"""
        if "粥" in text:
            return [1.0, 0.0, 0.0, 0.0]
        if "代码" in text:
            return [0.0, 1.0, 0.0, 0.0]
        if any(token in text for token in ("北京", "上海", "城市")):
            return [0.0, 0.0, 1.0, 0.0]
        if "过期" in text:
            return [0.0, 0.0, 0.0, 1.0]
        return [0.5, 0.5, 0.5, 0.5]


def _assert(condition: bool, message: str) -> None:
    """在测试中断言条件成立。"""
    if not condition:
        raise AssertionError(message)


def _validated_memory(**fields: Any) -> dict[str, Any]:
    """构造测试用的规范化记忆。"""
    defaults = {
        "memory_type": "other",
        "sensitivity": "low",
        "subject": "user",
        "value": True,
        "confidence": 0.8,
        "source": "user",
    }
    payload = {**defaults, **fields}
    return _validate_memory(payload)


def _only_memory(memories: list[dict[str, Any]], label: str) -> dict[str, Any]:
    """返回列表中的唯一记忆记录。"""
    _assert(len(memories) == 1, f"{label}: expected 1 memory, got {len(memories)}")
    return memories[0]


def _find_by_content(memories: list[dict[str, Any]], text: str) -> dict[str, Any] | None:
    """按内容查找测试记忆。"""
    for memory in memories:
        if memory.get("content") == text:
            return memory
    return None


def _cleanup_user_file(user_id: str) -> None:
    """清理测试用户的记忆文件。"""
    memory_file = memory_store._memory_file_for_user(user_id)
    if memory_file.exists():
        memory_file.unlink()


def test_memory_lifecycle() -> None:
    """验证 memory_lifecycle 的行为符合预期。"""
    memory_store._embeddings = FakeEmbeddings()
    memory_retriever._embeddings = FakeEmbeddings()

    user_id = "lifecycle_test_user"
    user_key = Fernet.generate_key()
    _cleanup_user_file(user_id)

    try:
        # 1. New fact is stored as active schema v1 memory.
        first_preference = _validated_memory(
            content="用户喜欢喝粥",
            memory_type="preference",
            predicate="likes",
            object="粥",
        )
        changed = memory_store.store_memories(user_id, user_key, [first_preference])
        _assert(changed == 1, "initial store should write one change")

        active = memory_store.list_memories(user_id, user_key, status=memory_store.ACTIVE_STATUS)
        preference = _only_memory(active, "initial active preference")
        _assert(preference["content"] == "用户喜欢喝粥", "stored content mismatch")
        _assert(preference["status"] == memory_store.ACTIVE_STATUS, "stored memory should be active")
        _assert("embedding" not in preference, "list_memories must not expose embedding")
        _assert(preference["evidence_count"] == 1, "initial evidence_count should be 1")

        # 2. Same fact_key merges instead of appending.
        changed = memory_store.store_memories(user_id, user_key, [first_preference])
        _assert(changed == 1, "fact_key duplicate should count as one merge change")

        active = memory_store.list_memories(user_id, user_key, status=memory_store.ACTIVE_STATUS)
        preference = _only_memory(active, "after fact_key merge")
        _assert(preference["evidence_count"] == 2, "fact_key merge should increment evidence_count")
        _assert(preference["confidence"] >= 0.83, "fact_key merge should boost confidence")

        # 3. Semantic duplicate with different fact_key merges via embedding fallback.
        style_one = _validated_memory(
            content="用户喜欢代码示例",
            memory_type="instruction",
            predicate="prefers_response_style",
            object="代码示例",
        )
        style_two = _validated_memory(
            content="用户喜欢代码样例",
            memory_type="instruction",
            predicate="prefers_response_style",
            object="代码样例",
        )
        _assert(memory_store.store_memories(user_id, user_key, [style_one]) == 1, "style_one store failed")
        _assert(memory_store.store_memories(user_id, user_key, [style_two]) == 1, "style_two merge failed")

        active = memory_store.list_memories(user_id, user_key, status=memory_store.ACTIVE_STATUS)
        style_memories = [
            memory
            for memory in active
            if memory.get("predicate") == "prefers_response_style"
        ]
        style_memory = _only_memory(style_memories, "semantic duplicate style memories")
        _assert(style_memory["evidence_count"] == 2, "embedding duplicate should merge evidence")

        # 4. Slot conflict supersedes the old city fact.
        beijing = _validated_memory(
            content="用户住在北京",
            memory_type="profile",
            predicate="lives_in",
            object="北京",
        )
        shanghai = _validated_memory(
            content="用户住在上海",
            memory_type="profile",
            predicate="lives_in",
            object="上海",
        )
        _assert(beijing["slot"] == "profile.current_city", "lives_in should normalize to current_city slot")
        _assert(shanghai["slot"] == "profile.current_city", "lives_in should normalize to current_city slot")
        _assert(memory_store.store_memories(user_id, user_key, [beijing]) == 1, "beijing store failed")
        _assert(memory_store.store_memories(user_id, user_key, [shanghai]) == 1, "shanghai conflict store failed")

        active = memory_store.list_memories(user_id, user_key, status=memory_store.ACTIVE_STATUS)
        superseded = memory_store.list_memories(user_id, user_key, status=memory_store.SUPERSEDED_STATUS)
        _assert(_find_by_content(active, "用户住在上海") is not None, "new city should remain active")
        old_city = _find_by_content(superseded, "用户住在北京")
        _assert(old_city is not None, "old city should be superseded")
        _assert(old_city.get("superseded_by"), "superseded city should point to replacement")

        # 5. Expired active memory is marked expired during load/list.
        expired_goal = _validated_memory(
            content="用户有一个过期目标",
            memory_type="profile",
            predicate="has_goal",
            object="过期目标",
        )
        expired_goal["expires_at"] = "2000-01-01T00:00:00+00:00"
        _assert(memory_store.store_memories(user_id, user_key, [expired_goal]) == 1, "expired goal store failed")

        expired = memory_store.list_memories(user_id, user_key, status=memory_store.EXPIRED_STATUS)
        _assert(_find_by_content(expired, "用户有一个过期目标") is not None, "expired memory should be marked expired")

        # 6. Retrieve only returns active memories and touches access metadata.
        results = memory_retriever.retrieve(user_id, user_key, "城市", top_k=10, threshold=0.99)
        result_contents = {memory["content"] for memory in results}
        _assert("用户住在上海" in result_contents, "retrieve should return active city")
        _assert("用户住在北京" not in result_contents, "retrieve must not return superseded city")
        _assert("用户有一个过期目标" not in result_contents, "retrieve must not return expired memory")

        active_after_retrieve = memory_store.list_memories(user_id, user_key, status=memory_store.ACTIVE_STATUS)
        touched_city = _find_by_content(active_after_retrieve, "用户住在上海")
        _assert(touched_city is not None, "active city disappeared after retrieve")
        _assert(touched_city.get("access_count", 0) >= 1, "retrieve should increment access_count")
        _assert(touched_city.get("last_accessed_at") is not None, "retrieve should set last_accessed_at")

        # 7. Forget moves active memory to forgotten and hides it from active list.
        preference_id = preference["id"]
        forgotten_count = memory_store.forget_memories(user_id, user_key, [preference_id])
        _assert(forgotten_count == 1, "forget should update one active memory")

        active_after_forget = memory_store.list_memories(user_id, user_key, status=memory_store.ACTIVE_STATUS)
        forgotten = memory_store.list_memories(user_id, user_key, status=memory_store.FORGOTTEN_STATUS)
        _assert(all(memory.get("id") != preference_id for memory in active_after_forget), "forgotten memory should not be active")
        forgotten_preference = next((memory for memory in forgotten if memory.get("id") == preference_id), None)
        _assert(forgotten_preference is not None, "forgotten memory should be listed as forgotten")
        _assert(forgotten_preference.get("forgotten_reason") == "user_requested", "forgotten reason mismatch")

        print("memory lifecycle OK")
    finally:
        _cleanup_user_file(user_id)


if __name__ == "__main__":
    test_memory_lifecycle()
