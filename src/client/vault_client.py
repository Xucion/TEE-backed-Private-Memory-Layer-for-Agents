import json
import secrets
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from common.sim_secure_channel import (
    SecureChannelError,
    decrypt_json,
    derive_channel_key,
    encrypt_json,
    generate_x25519_keypair,
    public_key_from_b64,
    public_key_to_b64,
)
from cryptography.fernet import Fernet
from interface.vault_api import VaultApiError, verify_attestation


class VaultClientError(Exception):
    """客户端验证 vault、建立信道或注入 key 失败。"""


@dataclass(frozen=True)
class ProvisionedVaultAccess:
    user_id: str
    user_key: bytes
    capability: str
    expires_in: int


class VaultProvisioningClient:
    def __init__(self, api_base_url: str, timeout_seconds: float = 10.0) -> None:
        # 输入 FastAPI 地址和超时；输出客户端实例；作用是配置只经 relay 的 provisioning 客户端。
        """初始化当前对象。"""
        self._api_base_url = api_base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        # 输入 HTTP 路径和 JSON 对象；输出响应对象；作用是调用 FastAPI relay 而不泄露本地信道密钥。
        """向服务端发送 JSON 请求并返回响应。"""
        request = urllib.request.Request(
            f"{self._api_base_url}{path}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, json.JSONDecodeError) as exc:
            raise VaultClientError(f"FastAPI relay 请求失败: {path}") from exc

        if not isinstance(data, dict):
            raise VaultClientError("FastAPI relay 返回了非对象响应")
        return data

    def provision(
        self,
        user_id: str,
        user_key: bytes | str | None = None,
    ) -> ProvisionedVaultAccess:
        # 输入 user_id 和可选 Fernet key；输出 capability；作用是在客户端完成 RA 验证和端到端 key 注入。
        """注入当前函数的核心逻辑。"""
        normalized_key = self._normalize_user_key(user_key)
        nonce = secrets.token_urlsafe(24)
        client_private_key, client_public_key = generate_x25519_keypair()
        client_pubkey_b64 = public_key_to_b64(client_public_key)

        handshake = self._post_json(
            "/vault/handshake",
            {
                "nonce": nonce,
                "client_pubkey": client_pubkey_b64,
            },
        )

        try:
            quote = verify_attestation(handshake, nonce)
            session_id = handshake.get("session_id")
            vault_pubkey_b64 = handshake.get("vault_pubkey")
            if not isinstance(session_id, str) or not session_id:
                raise VaultClientError("Vault 返回了无效 session_id")
            if session_id != quote.get("session_id"):
                raise VaultClientError("session_id 未绑定到 quote")
            if vault_pubkey_b64 != quote.get("vault_pubkey"):
                raise VaultClientError("vault_pubkey 未绑定到 quote")

            vault_pubkey = public_key_from_b64(vault_pubkey_b64, "vault_pubkey")
            shared_secret = client_private_key.exchange(vault_pubkey)
            channel_key = derive_channel_key(shared_secret, quote, client_pubkey_b64)
            envelope = encrypt_json(
                channel_key,
                session_id,
                {
                    "action": "provision_user_key",
                    "user_id": user_id,
                    "user_key": normalized_key.decode("ascii"),
                },
            )
            encrypted_response = self._post_json(
                "/vault/provision",
                {
                    "session_id": session_id,
                    **envelope,
                },
            )
            response = decrypt_json(
                channel_key,
                session_id,
                encrypted_response.get("nonce"),
                encrypted_response.get("ciphertext"),
            )
        except (VaultApiError, SecureChannelError, ValueError) as exc:
            raise VaultClientError("Vault attestation 或 provisioning 验证失败") from exc

        capability = response.get("capability")
        expires_in = response.get("expires_in")
        response_user_id = response.get("user_id")
        if response.get("status") != "ok":
            raise VaultClientError("Vault provisioning 未返回成功状态")
        if not isinstance(response_user_id, str) or response_user_id != user_id:
            raise VaultClientError("Vault provisioning user_id 不匹配")
        if not isinstance(capability, str) or not capability:
            raise VaultClientError("Vault provisioning 未返回 capability")
        if not isinstance(expires_in, int) or expires_in <= 0:
            raise VaultClientError("Vault provisioning 未返回有效过期时间")

        return ProvisionedVaultAccess(
            user_id=response_user_id,
            user_key=normalized_key,
            capability=capability,
            expires_in=expires_in,
        )

    @staticmethod
    def _normalize_user_key(user_key: bytes | str | None) -> bytes:
        # 输入可选 Fernet key；输出合法 key 字节串；作用是由客户端生成或校验长期记忆密钥。
        """校验并返回客户端使用的 Fernet key。"""
        if user_key is None:
            return Fernet.generate_key()
        if isinstance(user_key, str):
            try:
                normalized_key = user_key.encode("ascii")
            except UnicodeEncodeError as exc:
                raise VaultClientError("user_key 必须是 ASCII Fernet key") from exc
        elif isinstance(user_key, bytes):
            normalized_key = user_key
        else:
            raise VaultClientError("user_key 必须是 bytes、str 或 None")

        try:
            Fernet(normalized_key)
        except (TypeError, ValueError) as exc:
            raise VaultClientError("user_key 不是有效 Fernet key") from exc
        return normalized_key
