import json
import logging
import os
import socket
from typing import Any


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_RESPONSE_BYTES = 1024 * 1024

logger = logging.getLogger(__name__)


class VaultApiError(Exception):
    """当不受信任的应用无法完成 vault 请求时抛出。"""


def _vault_host() -> str:
    return os.getenv("VAULT_HOST", DEFAULT_HOST)


def _vault_port() -> int:
    return int(os.getenv("VAULT_PORT", str(DEFAULT_PORT)))


def _send_request(request: dict[str, Any]) -> dict[str, Any]:
    payload = (json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8")

    try:
        with socket.create_connection(
            (_vault_host(), _vault_port()),
            timeout=DEFAULT_TIMEOUT_SECONDS,
        ) as sock:
            sock.settimeout(DEFAULT_TIMEOUT_SECONDS)
            sock.sendall(payload)

            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break

                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    raise VaultApiError("Vault 响应过大")
                if b"\n" in chunk:
                    break

        raw_response = b"".join(chunks).split(b"\n", 1)[0]
        if not raw_response:
            raise VaultApiError("Vault 服务器返回了空响应")

        response = json.loads(raw_response.decode("utf-8"))
        if not isinstance(response, dict):
            raise VaultApiError("Vault 服务器返回了非对象类型的响应")
    except (OSError, json.JSONDecodeError, VaultApiError) as exc:
        logger.exception("Vault 请求失败")
        raise VaultApiError(str(exc)) from exc

    if response.get("ok") is not True:
        error = str(response.get("error", "未知的 vault 错误"))
        logger.error("Vault 服务器拒绝了请求: %s", error)
        raise VaultApiError(error)

    data = response.get("data", {})
    if not isinstance(data, dict):
        logger.error("Vault 服务器返回了无效的 data 负载: %r", data)
        raise VaultApiError("Vault 服务器返回了无效的 data 负载")

    return data


def store_memories(memories: list[dict[str, Any]]) -> int:
    data = _send_request(
        {
            "action": "store",
            "memories": memories,
        }
    )
    stored_count = data.get("stored_count", 0)
    if not isinstance(stored_count, int):
        logger.error("Vault 服务器返回了无效的 stored_count: %r", stored_count)
        raise VaultApiError("Vault 服务器返回了无效的 stored_count")
    return stored_count


def retrieve_context(query: str, top_k: int = 3, threshold: float = 0.4) -> str:
    data = _send_request(
        {
            "action": "retrieve",
            "query": query,
            "top_k": top_k,
            "threshold": threshold,
        }
    )
    memory_context = data.get("memory_context", "")
    if not isinstance(memory_context, str):
        logger.error("Vault 服务器返回了无效的 memory_context: %r", memory_context)
        raise VaultApiError("Vault 服务器返回了无效的 memory_context")
    return memory_context