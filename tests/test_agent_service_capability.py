import sys
from pathlib import Path
from types import SimpleNamespace


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))


from interface.vault_api import VaultApiError
from untrusted import agent_runtime
from untrusted.agent_runtime import AgentService, AgentServiceError


class FakeRedis:
    def __init__(self) -> None:
        # 输入无显式参数；输出 fake Redis；作用是保存测试中的短期 history。
        self.values: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        # 输入 Redis key；输出已保存字符串或 None；作用是模拟 history 读取。
        return self.values.get(key)

    def setex(self, key: str, ttl: int, value: str) -> None:
        # 输入 key、TTL 和 value；输出无返回值；作用是模拟带 TTL 的 history 写入。
        self.values[key] = value


class FakeLlm:
    def invoke(self, messages):
        # 输入 LangChain messages；输出固定回复；作用是避免测试调用外部 LLM。
        return SimpleNamespace(content="测试回复")


def _assert(condition: bool, message: str) -> None:
    # 输入断言条件和消息；输出无返回值；作用是提供脚本式测试失败信息。
    if not condition:
        raise AssertionError(message)


def test_agent_service_capability() -> None:
    # 输入无显式参数；输出无返回值；作用是验证 AgentService 只使用 capability 而不依赖用户 key。
    service = AgentService("redis://unused", "unused")
    fake_redis = FakeRedis()
    service._redis = fake_redis
    service._llm = FakeLlm()

    capability = "c" * 43
    retrieved_capabilities: list[str] = []
    stored_capabilities: list[str] = []

    original_retrieve = agent_runtime.retrieve_context_with_capability
    original_store = agent_runtime.store_memories_with_capability
    original_extract = agent_runtime.extract_memories

    try:
        def fake_retrieve(token: str, query: str, top_k: int, threshold: float) -> str:
            # 输入 capability 和检索参数；输出空上下文；作用是捕获 Agent 使用的 token。
            retrieved_capabilities.append(token)
            return ""

        def fake_store(token: str, memories: list[dict]) -> int:
            # 输入 capability 和记忆；输出存储数量；作用是捕获后台存储使用的 token。
            stored_capabilities.append(token)
            return len(memories)

        agent_runtime.retrieve_context_with_capability = fake_retrieve
        agent_runtime.store_memories_with_capability = fake_store
        agent_runtime.extract_memories = lambda messages: [{"content": "用户喜欢测试"}]

        result = service.generate_reply(capability, "conversation-1", "你好")
        _assert(result["reply"] == "测试回复", "unexpected LLM reply")
        _assert(retrieved_capabilities == [capability], "retrieve did not use request capability")
        _assert(
            all(capability not in key for key in fake_redis.values),
            "raw capability leaked into Redis value",
        )
        _assert(
            all(capability not in key for key in fake_redis.values.keys()),
            "raw capability leaked into Redis key",
        )

        service.store_memory_background(capability, "我喜欢测试")
        _assert(stored_capabilities == [capability], "store did not use request capability")

        def reject_retrieve(token: str, query: str, top_k: int, threshold: float) -> str:
            # 输入 capability 和检索参数；输出无；作用是模拟无效或过期 capability。
            raise VaultApiError("capability 无效")

        agent_runtime.retrieve_context_with_capability = reject_retrieve
        try:
            service.generate_reply(capability, "conversation-2", "你好")
        except AgentServiceError:
            pass
        else:
            raise AssertionError("invalid capability did not reject chat")

        print("agent service capability OK")
    finally:
        agent_runtime.retrieve_context_with_capability = original_retrieve
        agent_runtime.store_memories_with_capability = original_store
        agent_runtime.extract_memories = original_extract


if __name__ == "__main__":
    test_agent_service_capability()
