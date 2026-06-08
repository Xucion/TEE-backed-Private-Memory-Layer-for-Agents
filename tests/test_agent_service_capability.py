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
        """初始化当前对象。"""
        self.values: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        # 输入 Redis key；输出已保存字符串或 None；作用是模拟 history 读取。
        """获取当前函数的核心逻辑。"""
        return self.values.get(key)

    def setex(self, key: str, ttl: int, value: str) -> None:
        # 输入 key、TTL 和 value；输出无返回值；作用是模拟带 TTL 的 history 写入。
        """模拟 Redis setex 写入并记录 TTL。"""
        self.values[key] = value


class FakeLlm:
    def invoke(self, messages):
        # 输入 LangChain messages；输出固定回复；作用是避免测试调用外部 LLM。
        """调用当前函数的核心逻辑。"""
        return SimpleNamespace(content="测试回复")


def _assert(condition: bool, message: str) -> None:
    # 输入断言条件和消息；输出无返回值；作用是提供脚本式测试失败信息。
    """在测试中断言条件成立。"""
    if not condition:
        raise AssertionError(message)


def test_agent_service_capability() -> None:
    # 输入无显式参数；输出无返回值；作用是验证 AgentService 只使用 capability 而不依赖用户 key。
    """验证 agent_service_capability 的行为符合预期。"""
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
    original_tool = agent_runtime.try_handle_wechat_activity_report

    try:
        def fake_retrieve(token: str, query: str, top_k: int, threshold: float) -> str:
            # 输入 capability 和检索参数；输出空上下文；作用是捕获 Agent 使用的 token。
            """提供测试用的替身实现。"""
            retrieved_capabilities.append(token)
            return ""

        def fake_store(token: str, memories: list[dict]) -> int:
            # 输入 capability 和记忆；输出存储数量；作用是捕获后台存储使用的 token。
            """提供测试用的替身实现。"""
            stored_capabilities.append(token)
            return len(memories)

        agent_runtime.retrieve_context_with_capability = fake_retrieve
        agent_runtime.store_memories_with_capability = fake_store
        agent_runtime.extract_memories = lambda messages: [{"content": "用户喜欢测试"}]
        agent_runtime.try_handle_wechat_activity_report = lambda message, **kwargs: None

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
            """模拟拒绝访问的测试路径。"""
            raise VaultApiError("capability 无效")

        agent_runtime.retrieve_context_with_capability = reject_retrieve
        degraded = service.generate_reply(capability, "conversation-2", "你好")
        _assert(degraded["reply"] == "测试回复", "vault failure did not degrade to LLM chat")
        _assert(degraded["memory_context_used"] is False, "degraded chat should not use memory context")

        print("agent service capability OK")
    finally:
        agent_runtime.retrieve_context_with_capability = original_retrieve
        agent_runtime.store_memories_with_capability = original_store
        agent_runtime.extract_memories = original_extract
        agent_runtime.try_handle_wechat_activity_report = original_tool


def test_agent_service_wechat_report_tool_route() -> None:
    """验证 agent_service_wechat_report_tool_route 的行为符合预期。"""
    service = AgentService("redis://unused", "unused")
    fake_redis = FakeRedis()
    service._redis = fake_redis
    service._llm = FakeLlm()

    original_tool = agent_runtime.try_handle_wechat_activity_report

    try:
        calls: list[str] = []

        def fake_tool(message: str, **kwargs) -> str | None:
            """提供测试用的替身实现。"""
            calls.append(message)
            if "微信活动报告" in message:
                return "微信活动报告已生成。\n- HTML：report.html"
            return None

        agent_runtime.try_handle_wechat_activity_report = fake_tool

        result = service.generate_reply(
            "c" * 43,
            "conversation-tool",
            "为 account=wxid_a，username=wxid_b，生成 2026-06-07 的微信活动报告",
        )

        _assert(result["reply"].startswith("微信活动报告已生成"), "tool reply was not returned")
        _assert(result["memory_context_used"] is False, "tool route should not use memory context")
        _assert(len(calls) == 1, "tool route was not checked")
        _assert(bool(fake_redis.values), "tool route did not save conversation history")
    finally:
        agent_runtime.try_handle_wechat_activity_report = original_tool


def test_agent_service_without_vault_capability_degrades_to_chat() -> None:
    """验证 agent_service_without_vault_capability_degrades_to_chat 的行为符合预期。"""
    service = AgentService("redis://unused", "unused")
    fake_redis = FakeRedis()
    service._redis = fake_redis
    service._llm = FakeLlm()

    original_retrieve = agent_runtime.retrieve_context_with_capability
    original_tool = agent_runtime.try_handle_wechat_activity_report

    try:
        calls: list[str] = []

        def fake_retrieve(token: str, query: str, top_k: int, threshold: float) -> str:
            """提供测试用的替身实现。"""
            calls.append(token)
            return "should not happen"

        agent_runtime.retrieve_context_with_capability = fake_retrieve
        agent_runtime.try_handle_wechat_activity_report = lambda message, **kwargs: None

        result = service.generate_reply(None, "conversation-no-vault", "你好")

        _assert(result["reply"] == "测试回复", "no-vault chat did not return LLM reply")
        _assert(result["memory_context_used"] is False, "no-vault chat should not use memory context")
        _assert(calls == [], "no-vault chat should not call vault retrieval")
        _assert(bool(fake_redis.values), "no-vault chat did not save short-term history")
    finally:
        agent_runtime.retrieve_context_with_capability = original_retrieve
        agent_runtime.try_handle_wechat_activity_report = original_tool


if __name__ == "__main__":
    test_agent_service_capability()
    test_agent_service_wechat_report_tool_route()
    test_agent_service_without_vault_capability_degrades_to_chat()
