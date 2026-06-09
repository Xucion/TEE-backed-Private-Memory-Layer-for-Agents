import sys
import tempfile
import urllib.error
from pathlib import Path

from cryptography.fernet import Fernet


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))


from client import agent_client
from client.agent_client import (
    DEFAULT_TIMEOUT_SECONDS,
    AgentClientError,
    ConfidentialAgentClient,
)
from client.vault_client import ProvisionedVaultAccess, VaultClientError


class FakeProvisioningClient:
    def __init__(self) -> None:
        """初始化当前对象。"""
        self.calls = 0

    def provision(self, user_id: str, user_key: bytes) -> ProvisionedVaultAccess:
        """注入当前函数的核心逻辑。"""
        self.calls += 1
        return ProvisionedVaultAccess(
            user_id=user_id,
            user_key=user_key,
            capability=f"capability-{self.calls}",
            expires_in=3600,
        )


class FailingProvisioningClient:
    def provision(self, user_id: str, user_key: bytes) -> ProvisionedVaultAccess:
        """注入当前函数的核心逻辑。"""
        raise VaultClientError("vault unavailable")


class UnexpectedProvisioningClient:
    def provision(self, user_id: str, user_key: bytes) -> ProvisionedVaultAccess:
        """显式无 vault 模式尝试 provisioning 时使测试失败。"""
        raise AssertionError("explicit no-vault mode attempted provisioning")


def _assert(condition: bool, message: str) -> None:
    """在测试中断言条件成立。"""
    if not condition:
        raise AssertionError(message)


def test_high_level_client() -> None:
    """验证 high_level_client 的行为符合预期。"""
    with tempfile.TemporaryDirectory() as temp_dir:
        key_file = Path(temp_dir) / "alice.key"
        client = ConfidentialAgentClient(
            user_id="alice",
            api_base_url="http://unused",
            session_id="conversation-1",
            key_file=key_file,
        )
        fake_provisioning = FakeProvisioningClient()
        client._provisioning_client = fake_provisioning

        seen_capabilities: list[str] = []

        def successful_post(path, payload, headers=None):
            """模拟成功的测试响应。"""
            seen_capabilities.append(headers["X-Vault-Capability"])
            return {"reply": f"收到: {payload['message']}"}

        client._post_json = successful_post
        reply = client.chat("你好")

        _assert(reply == "收到: 你好", "unexpected chat reply")
        _assert(fake_provisioning.calls == 1, "client did not provision once")
        _assert(key_file.exists(), "client did not persist user key")
        _assert(key_file.read_bytes().strip() == client.user_key, "wrong persisted key")
        _assert(seen_capabilities == ["capability-1"], "wrong capability")

        second_client = ConfidentialAgentClient(
            user_id="alice",
            api_base_url="http://unused",
            key_file=key_file,
        )
        _assert(second_client.user_key == client.user_key, "saved key was not reused")
        Fernet(second_client.user_key)

        attempts = 0

        def retrying_post(path, payload, headers=None):
            """模拟需要重试的测试响应。"""
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise AgentClientError("capability expired")
            seen_capabilities.append(headers["X-Vault-Capability"])
            return {"reply": "刷新成功"}

        client._post_json = retrying_post
        reply = client.chat("再试一次")
        _assert(reply == "刷新成功", "client did not retry chat")
        _assert(fake_provisioning.calls == 2, "client did not refresh capability")
        _assert(seen_capabilities[-1] == "capability-2", "refreshed capability not used")

        print("high-level client OK")


def test_high_level_client_no_vault_mode() -> None:
    """验证 high_level_client_no_vault_mode 的行为符合预期。"""
    with tempfile.TemporaryDirectory() as temp_dir:
        client = ConfidentialAgentClient(
            user_id="alice",
            api_base_url="http://unused",
            session_id="conversation-no-vault",
            key_file=Path(temp_dir) / "alice.key",
            allow_no_vault=True,
        )
        client._provisioning_client = FailingProvisioningClient()

        seen_headers: list[dict] = []

        def successful_post(path, payload, headers=None):
            """模拟成功的测试响应。"""
            seen_headers.append(headers or {})
            return {"reply": f"无 vault: {payload['message']}"}

        client._post_json = successful_post
        reply = client.chat("你好")

        _assert(reply == "无 vault: 你好", "no-vault client did not return chat reply")
        _assert(client._no_vault_mode is True, "client did not enter no-vault mode")
        _assert(seen_headers == [{}], "no-vault client should not send capability header")

        print("high-level client no-vault OK")


def test_high_level_client_explicit_no_vault_skips_provisioning() -> None:
    """显式无 vault 模式不应探测 provisioning 端点。"""
    with tempfile.TemporaryDirectory() as temp_dir:
        client = ConfidentialAgentClient(
            user_id="alice",
            api_base_url="http://unused",
            session_id="conversation-explicit-no-vault",
            key_file=Path(temp_dir) / "alice.key",
            no_vault=True,
        )
        client._provisioning_client = UnexpectedProvisioningClient()
        client._post_json = lambda path, payload, headers=None: {"reply": "普通聊天"}

        reply = client.chat("你好")

        _assert(reply == "普通聊天", "explicit no-vault chat failed")
        _assert(client._access is None, "explicit no-vault mode created vault access")


def test_high_level_client_reports_timeout_separately() -> None:
    """HTTP 读取超时不应被报告为连接失败。"""
    with tempfile.TemporaryDirectory() as temp_dir:
        client = ConfidentialAgentClient(
            user_id="alice",
            api_base_url="http://unused",
            key_file=Path(temp_dir) / "alice.key",
            allow_no_vault=True,
        )
        original_urlopen = agent_client.urllib.request.urlopen

        def timed_out(*args, **kwargs):
            """模拟 urllib 读取超时。"""
            raise urllib.error.URLError(TimeoutError("timed out"))

        try:
            agent_client.urllib.request.urlopen = timed_out
            try:
                client._post_json("/chat", {"session_id": "s", "message": "你好"})
            except AgentClientError as exc:
                _assert("请求超时" in str(exc), "timeout was reported as a connection failure")
                _assert(
                    f"{DEFAULT_TIMEOUT_SECONDS:g} 秒" in str(exc),
                    "timeout message omitted the configured wait",
                )
            else:
                raise AssertionError("timeout did not raise AgentClientError")
        finally:
            agent_client.urllib.request.urlopen = original_urlopen


if __name__ == "__main__":
    test_high_level_client()
    test_high_level_client_no_vault_mode()
    test_high_level_client_explicit_no_vault_skips_provisioning()
    test_high_level_client_reports_timeout_separately()
