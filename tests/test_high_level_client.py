import sys
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from client.agent_client import AgentClientError, ConfidentialAgentClient
from client.vault_client import ProvisionedVaultAccess


class FakeProvisioningClient:
    def __init__(self) -> None:
        self.calls = 0

    def provision(self, user_id: str, user_key: bytes) -> ProvisionedVaultAccess:
        self.calls += 1
        return ProvisionedVaultAccess(
            user_id=user_id,
            user_key=user_key,
            capability=f"capability-{self.calls}",
            expires_in=3600,
        )


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def test_high_level_client() -> None:
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


if __name__ == "__main__":
    test_high_level_client()
