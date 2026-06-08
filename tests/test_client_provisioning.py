import secrets
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))


from client.vault_client import VaultProvisioningClient
from trusted import user_key_manager, vault_server
from untrusted import api_server
from untrusted.api_server import HandshakeRequest, ProvisionEnvelope


def _assert(condition: bool, message: str) -> None:
    # 输入断言条件和消息；输出无返回值；作用是提供脚本式测试失败信息。
    """在测试中断言条件成立。"""
    if not condition:
        raise AssertionError(message)


def _reset_state(user_id: str) -> None:
    # 输入测试 user_id；输出无返回值；作用是清理 provisioning 测试产生的进程内状态。
    """重置测试中的 vault 安全状态。"""
    with vault_server._SESSION_LOCK:
        vault_server._SESSIONS.clear()
    with vault_server._CAPABILITY_LOCK:
        vault_server._CAPABILITIES.clear()
    with user_key_manager._USER_KEYS_LOCK:
        user_key_manager._USER_KEYS.pop(user_id, None)


def test_client_provisioning() -> None:
    # 输入无显式参数；输出无返回值；作用是贯通 Client SDK、FastAPI relay 和 Vault provisioning。
    """验证 client_provisioning 的行为符合预期。"""
    user_id = f"client_test_{secrets.token_hex(4)}"
    client = VaultProvisioningClient("http://unused")
    original_relay = api_server.relay_vault_request

    def in_process_relay(request: dict[str, Any]) -> dict[str, Any]:
        # 输入 relay 请求；输出 vault data；作用是在测试进程内替代真实 socket transport。
        """在进程内转发测试请求。"""
        response = vault_server.handle_request(request)
        if response.get("ok") is not True:
            raise AssertionError(response.get("error"))
        return response["data"]

    def in_process_post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
        # 输入 HTTP 路径和 payload；输出 endpoint 响应；作用是替代真实 HTTP 网络。
        """在进程内转发测试请求。"""
        if path == "/vault/handshake":
            return api_server.relay_handshake(HandshakeRequest(**payload))
        if path == "/vault/provision":
            return api_server.relay_provision(ProvisionEnvelope(**payload))
        raise AssertionError(f"unexpected path: {path}")

    _reset_state(user_id)
    try:
        api_server.relay_vault_request = in_process_relay
        client._post_json = in_process_post
        access = client.provision(user_id)

        _assert(access.user_id == user_id, "client received wrong user_id")
        _assert(
            vault_server._resolve_capability(access.capability, "retrieve") == user_id,
            "client capability was not registered in vault",
        )
        _assert(access.expires_in == vault_server.CAPABILITY_TTL_SECONDS, "wrong capability TTL")
        print("client provisioning OK")
    finally:
        api_server.relay_vault_request = original_relay
        _reset_state(user_id)


if __name__ == "__main__":
    test_client_provisioning()
