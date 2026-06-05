import secrets
import sys
import time
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from common.sim_secure_channel import (
    decrypt_json,
    derive_channel_key,
    encrypt_json,
    generate_x25519_keypair,
    public_key_from_b64,
    public_key_to_b64,
)
from interface.vault_api import verify_attestation
from trusted import user_key_manager, vault_server
from trusted.user_key_manager import UserKeyError, provision_user_key
from trusted.vault_server import VaultError


def _assert(condition: bool, message: str) -> None:
    # 输入断言条件和消息；输出无返回值；作用是为脚本式测试提供明确失败信息。
    if not condition:
        raise AssertionError(message)


def _reset_security_state(user_id: str) -> None:
    # 输入测试 user_id；输出无返回值；作用是清理进程内 session、capability 和用户 key。
    with vault_server._SESSION_LOCK:
        vault_server._SESSIONS.clear()
    with vault_server._CAPABILITY_LOCK:
        vault_server._CAPABILITIES.clear()
    with user_key_manager._USER_KEYS_LOCK:
        user_key_manager._USER_KEYS.pop(user_id, None)


def _provision_through_protocol(user_id: str, user_key: bytes) -> tuple[str, str, bytes]:
    # 输入 user_id 和用户 key；输出 capability、session_id 和 channel_key；作用是模拟客户端完整 RA 注入流程。
    nonce = secrets.token_urlsafe(24)
    client_private_key, client_public_key = generate_x25519_keypair()
    client_pubkey_b64 = public_key_to_b64(client_public_key)

    handshake_response = vault_server.handle_request(
        {
            "action": "handshake_start",
            "nonce": nonce,
            "client_pubkey": client_pubkey_b64,
        }
    )
    _assert(handshake_response.get("ok") is True, "handshake failed")
    handshake = handshake_response["data"]
    quote = verify_attestation(handshake, nonce)
    session_id = handshake["session_id"]
    vault_pubkey = public_key_from_b64(handshake["vault_pubkey"], "vault_pubkey")
    shared_secret = client_private_key.exchange(vault_pubkey)
    channel_key = derive_channel_key(shared_secret, quote, client_pubkey_b64)

    envelope = encrypt_json(
        channel_key,
        session_id,
        {
            "action": "provision_user_key",
            "user_id": user_id,
            "user_key": user_key.decode("ascii"),
        },
    )
    provision_response = vault_server.handle_request(
        {
            "action": "secure_provision_user_key",
            "session_id": session_id,
            **envelope,
        }
    )
    _assert(provision_response.get("ok") is True, "provisioning failed")
    provisioned = decrypt_json(
        channel_key,
        session_id,
        provision_response["data"]["nonce"],
        provision_response["data"]["ciphertext"],
    )
    capability = provisioned.get("capability")
    _assert(isinstance(capability, str) and bool(capability), "missing capability")
    return capability, session_id, channel_key


def test_capability_security() -> None:
    # 输入无显式参数；输出无返回值；作用是验证 capability 绑定、scope、过期和 key 覆盖保护。
    user_id = f"capability_test_{secrets.token_hex(4)}"
    user_key = Fernet.generate_key()
    _reset_security_state(user_id)

    original_retrieve_handler = vault_server._handle_retrieve_data
    captured_requests: list[dict[str, Any]] = []

    try:
        capability, session_id, channel_key = _provision_through_protocol(user_id, user_key)
        _assert(
            vault_server._resolve_capability(capability, "retrieve") == user_id,
            "capability did not resolve to provisioned user",
        )

        try:
            vault_server._resolve_capability(capability, "forget")
        except VaultError:
            pass
        else:
            raise AssertionError("agent capability must not receive forget scope")

        def capture_retrieve(request: dict[str, Any]) -> dict[str, Any]:
            # 输入 vault retrieve 请求；输出测试响应；作用是捕获最终绑定的 user_id。
            captured_requests.append(dict(request))
            return {"memory_context": "", "retrieved_count": 0}

        vault_server._handle_retrieve_data = capture_retrieve
        capability_response = vault_server.handle_request(
            {
                "action": "capability_request",
                "operation": "retrieve",
                "capability": capability,
                "user_id": "attacker_selected_user",
                "query": "test",
            }
        )
        _assert(capability_response.get("ok") is True, "capability request failed")
        _assert(captured_requests[-1]["user_id"] == user_id, "capability user binding was bypassed")

        secure_envelope = encrypt_json(
            channel_key,
            session_id,
            {
                "action": "retrieve",
                "user_id": "attacker_selected_user",
                "query": "test",
            },
        )
        secure_response = vault_server.handle_request(
            {
                "action": "secure_request",
                "session_id": session_id,
                **secure_envelope,
            }
        )
        _assert(secure_response.get("ok") is True, "secure request failed")
        decrypt_json(
            channel_key,
            session_id,
            secure_response["data"]["nonce"],
            secure_response["data"]["ciphertext"],
        )
        _assert(captured_requests[-1]["user_id"] == user_id, "secure session user binding was bypassed")

        other_user_id = f"other_{secrets.token_hex(4)}"
        second_provision_envelope = encrypt_json(
            channel_key,
            session_id,
            {
                "action": "provision_user_key",
                "user_id": other_user_id,
                "user_key": Fernet.generate_key().decode("ascii"),
            },
        )
        try:
            vault_server.handle_request(
                {
                    "action": "secure_provision_user_key",
                    "session_id": session_id,
                    **second_provision_envelope,
                }
            )
        except VaultError:
            pass
        else:
            raise AssertionError("session switched to another user")
        with user_key_manager._USER_KEYS_LOCK:
            _assert(
                other_user_id not in user_key_manager._USER_KEYS,
                "rejected user switch still provisioned the other user's key",
            )

        try:
            provision_user_key(user_id, Fernet.generate_key())
        except UserKeyError:
            pass
        else:
            raise AssertionError("different key overwrote an existing user key")

        with vault_server._CAPABILITY_LOCK:
            vault_server._CAPABILITIES[capability]["expires_at"] = time.time() - 1
        try:
            vault_server._resolve_capability(capability, "retrieve")
        except VaultError:
            pass
        else:
            raise AssertionError("expired capability remained usable")

        print("capability security OK")
    finally:
        vault_server._handle_retrieve_data = original_retrieve_handler
        _reset_security_state(user_id)


if __name__ == "__main__":
    test_capability_security()
