import json
import os
import re
import secrets
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet

from client.vault_client import (
    ProvisionedVaultAccess,
    VaultClientError,
    VaultProvisioningClient,
)


DEFAULT_API_URL = "http://127.0.0.1:8000"
DEFAULT_TIMEOUT_SECONDS = 30.0
CAPABILITY_REFRESH_MARGIN_SECONDS = 30
_USER_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


class AgentClientError(Exception):
    """高级客户端无法初始化身份或完成聊天请求。"""


class ConfidentialAgentClient:
    def __init__(
        self,
        user_id: str,
        api_base_url: str = DEFAULT_API_URL,
        session_id: str | None = None,
        key_file: str | Path | None = None,
        user_key: bytes | str | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.user_id = self._validate_user_id(user_id)
        self.api_base_url = api_base_url.rstrip("/")
        self.session_id = session_id or f"conversation-{secrets.token_hex(8)}"
        self.timeout_seconds = timeout_seconds
        self.key_file = Path(key_file).expanduser() if key_file else self.default_key_file(self.user_id)

        self.user_key = self._resolve_user_key(user_key)
        self._provisioning_client = VaultProvisioningClient(
            self.api_base_url,
            timeout_seconds=timeout_seconds,
        )
        self._access: ProvisionedVaultAccess | None = None
        self._capability_expires_at = 0.0

    @staticmethod
    def _validate_user_id(user_id: str) -> str:
        normalized = str(user_id).strip()
        if not _USER_ID_RE.fullmatch(normalized):
            raise AgentClientError(
                "user_id 只能包含字母、数字、下划线、点和短横线，长度不超过 128"
            )
        return normalized

    @staticmethod
    def default_key_file(user_id: str) -> Path:
        # 每个用户使用独立文件，避免把长期密钥写入项目目录或 shell 历史。
        return (
            Path.home()
            / ".config"
            / "confidential-agent-memory-vault"
            / "keys"
            / f"{user_id}.key"
        )

    def _resolve_user_key(self, user_key: bytes | str | None) -> bytes:
        if user_key is not None:
            return VaultProvisioningClient._normalize_user_key(user_key)

        if self.key_file.exists():
            try:
                key = self.key_file.read_bytes().strip()
            except OSError as exc:
                raise AgentClientError(f"无法读取用户密钥文件: {self.key_file}") from exc
            try:
                return VaultProvisioningClient._normalize_user_key(key)
            except VaultClientError as exc:
                raise AgentClientError(f"用户密钥文件无效: {self.key_file}") from exc

        return self._write_new_key_file(Fernet.generate_key())

    def _write_new_key_file(self, key: bytes) -> bytes:
        try:
            self.key_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.key_file.parent, 0o700)
            descriptor = os.open(
                self.key_file,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "wb") as key_handle:
                key_handle.write(key + b"\n")
            return key
        except FileExistsError:
            try:
                existing_key = self.key_file.read_bytes().strip()
                return VaultProvisioningClient._normalize_user_key(existing_key)
            except (OSError, VaultClientError) as exc:
                raise AgentClientError(f"无法复用用户密钥文件: {self.key_file}") from exc
        except OSError as exc:
            raise AgentClientError(f"无法保存用户密钥文件: {self.key_file}") from exc

    def _post_json(
        self,
        path: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        request_headers = {"Content-Type": "application/json"}
        request_headers.update(headers or {})
        request = urllib.request.Request(
            f"{self.api_base_url}{path}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=request_headers,
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                error_data = json.loads(exc.read().decode("utf-8"))
                detail = str(error_data.get("detail", "")).strip()
            except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
                pass
            message = f"Agent 请求失败，HTTP {exc.code}"
            if detail:
                message = f"{message}: {detail}"
            raise AgentClientError(message) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise AgentClientError(f"无法连接 Agent 服务: {self.api_base_url}") from exc
        except json.JSONDecodeError as exc:
            raise AgentClientError("Agent 返回了无效 JSON") from exc

        if not isinstance(data, dict):
            raise AgentClientError("Agent 返回了非对象响应")
        return data

    def provision(self, force: bool = False) -> ProvisionedVaultAccess:
        now = time.monotonic()
        if (
            not force
            and self._access is not None
            and now < self._capability_expires_at
        ):
            return self._access

        try:
            access = self._provisioning_client.provision(
                self.user_id,
                self.user_key,
            )
        except VaultClientError as exc:
            raise AgentClientError(
                f"Vault 初始化失败；请确认服务已启动，并确认密钥文件正确: {self.key_file}"
            ) from exc

        self._access = access
        usable_seconds = max(
            1,
            access.expires_in - CAPABILITY_REFRESH_MARGIN_SECONDS,
        )
        self._capability_expires_at = time.monotonic() + usable_seconds
        return access

    def chat(self, message: str) -> str:
        message = message.strip()
        if not message:
            raise AgentClientError("消息不能为空")

        for attempt in range(2):
            access = self.provision(force=attempt > 0)
            try:
                response = self._post_json(
                    "/chat",
                    {
                        "session_id": self.session_id,
                        "message": message,
                    },
                    {
                        "X-Vault-Capability": access.capability,
                    },
                )
            except AgentClientError:
                if attempt == 0:
                    continue
                raise

            reply = response.get("reply")
            if not isinstance(reply, str):
                raise AgentClientError("Agent 返回了无效 reply")
            return reply

        raise AgentClientError("Agent 请求失败")
