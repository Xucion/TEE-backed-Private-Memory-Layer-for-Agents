import base64
import json
import logging
import os
import secrets
import socket
from typing import Any

from common.sim_secure_channel import (
    SecureChannelError,
    canonical_json,
    decrypt_json,
    derive_channel_key,
    encrypt_json,
    generate_x25519_keypair,
    public_key_from_b64,
    public_key_to_b64,
)
from cryptography.exceptions import InvalidSignature
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_RESPONSE_BYTES = 1024 * 1024

# 客户端对模拟 RA quote 的预期值。流程 2 只验证模拟信任根，不代表真实 SGX。
SIM_RA_PUBLIC_KEY_FILE = os.path.join(os.path.dirname(__file__), "sim_ra_public_key.pem")
EXPECTED_SIM_RA_PROTOCOL_VERSION = "sim-ra-v1"
EXPECTED_SIM_RA_MODE = "SIMULATED_RA_ONLY"
EXPECTED_SIM_RA_VAULT_ID = "confidential-agent-memory-vault"
EXPECTED_SIM_RA_MEASUREMENT = "confidential-agent-memory-vault-dev-v1"

logger = logging.getLogger(__name__)


class VaultApiError(Exception):
    """当不受信任的应用无法完成 vault 请求时抛出。"""


def _vault_host() -> str:
    # 输入环境变量；输出 vault 主机地址；作用是读取 VAULT_HOST 或使用默认值。
    return os.getenv("VAULT_HOST", DEFAULT_HOST)


def _vault_port() -> int:
    # 输入环境变量；输出 vault 端口号；作用是读取 VAULT_PORT 或使用默认值。
    return int(os.getenv("VAULT_PORT", str(DEFAULT_PORT)))


def _send_request(request: dict[str, Any]) -> dict[str, Any]:
    # 输入明文请求对象；输出响应 data 对象；作用是通过本地 socket 发送 JSON 行协议请求。
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


def attest_vault(nonce: str) -> dict[str, Any]:
    # 输入客户端 nonce；输出 quote 和签名；作用是向 vault 请求模拟 RA 身份声明。
    logger.info("开始模拟 RA attest: nonce_chars=%d", len(nonce))
    data = _send_request(
        {
            "action": "attest",
            "nonce": nonce,
        }
    )

    quote = data.get("quote")
    if not isinstance(quote, dict):
        logger.error("Vault 服务器返回了无效的 quote: %r", quote)
        raise VaultApiError("Vault 服务器返回了无效的 quote")

    signature = data.get("signature", "")
    if not isinstance(signature, str):
        logger.error("Vault 服务器返回了无效的 signature: %r", signature)
        raise VaultApiError("Vault 服务器返回了无效的 signature")

    return {
        "quote": quote,
        "signature": signature,
    }


def store_memories(memories: list[dict[str, Any]]) -> int:
    # 输入记忆列表；输出存储数量；作用是发送旧版明文 store 请求。
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


def store_user_memories(user_id: str, memories: list[dict[str, Any]]) -> int:
    # 输入用户标识和记忆列表；输出存储数量；作用是发送带 user_id 的旧版明文 store 请求。
    data = _send_request(
        {
            "action": "store",
            "user_id": user_id,
            "memories": memories,
        }
    )
    stored_count = data.get("stored_count", 0)
    if not isinstance(stored_count, int):
        logger.error("Vault 服务器返回了无效的 stored_count: %r", stored_count)
        raise VaultApiError("Vault 服务器返回了无效的 stored_count")
    return stored_count


def retrieve_context(query: str, top_k: int = 3, threshold: float = 0.4) -> str:
    # 输入查询文本和检索参数；输出最小化记忆上下文；作用是发送旧版明文 retrieve 请求。
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


def retrieve_user_context(user_id: str, query: str, top_k: int = 3, threshold: float = 0.4) -> str:
    # 输入用户标识、查询和检索参数；输出最小化记忆上下文；作用是发送带 user_id 的明文 retrieve 请求。
    data = _send_request(
        {
            "action": "retrieve",
            "user_id": user_id,
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


def _load_sim_ra_public_key():
    # 输入无显式参数；输出模拟 RA 公钥对象；作用是加载客户端信任的 quote 验签公钥。
    with open(SIM_RA_PUBLIC_KEY_FILE, "rb") as f:
        return serialization.load_pem_public_key(f.read())


def verify_attestation(result: dict[str, Any], expected_nonce: str) -> dict[str, Any]:
    # 输入 attest 结果和预期 nonce；输出已验证 quote；作用是校验模拟 RA 签名和固定身份字段。
    quote = result.get("quote")
    signature = result.get("signature")
    logger.info("开始验证模拟 RA quote: expected_nonce_chars=%d", len(expected_nonce))

    if not isinstance(quote, dict):
        raise VaultApiError("无效的 quote")
    if not isinstance(signature, str) or not signature:
        raise VaultApiError("无效的 quote signature")

    public_key = _load_sim_ra_public_key()

    try:
        public_key.verify(base64.b64decode(signature), canonical_json(quote))
    except (InvalidSignature, ValueError) as exc:
        raise VaultApiError("模拟 RA quote 签名验证失败") from exc

    if quote.get("nonce") != expected_nonce:
        raise VaultApiError("模拟 RA nonce 不匹配")
    if quote.get("protocol_version") != EXPECTED_SIM_RA_PROTOCOL_VERSION:
        raise VaultApiError("模拟 RA protocol_version 不匹配")
    if quote.get("mode") != EXPECTED_SIM_RA_MODE:
        raise VaultApiError("模拟 RA mode 不匹配")
    if quote.get("vault_id") != EXPECTED_SIM_RA_VAULT_ID:
        raise VaultApiError("模拟 RA vault_id 不匹配")
    if quote.get("measurement") != EXPECTED_SIM_RA_MEASUREMENT:
        raise VaultApiError("模拟 RA measurement 不匹配")

    logger.info(
        "模拟 RA quote 验证通过: vault_id=%s measurement=%s mode=%s",
        quote.get("vault_id"),
        quote.get("measurement"),
        quote.get("mode"),
    )
    return quote


def open_secure_channel() -> dict[str, Any]:
    # 输入无显式参数；输出安全信道 session；作用是完成模拟 RA、X25519 握手和信道密钥派生。
    nonce = secrets.token_urlsafe(24)
    client_private_key, client_public_key = generate_x25519_keypair()
    client_pubkey_b64 = public_key_to_b64(client_public_key)
    logger.info(
        "开始建立安全信道: nonce_chars=%d client_pubkey_chars=%d",
        len(nonce),
        len(client_pubkey_b64),
    )

    data = _send_request(
        {
            "action": "handshake_start",
            "nonce": nonce,
            "client_pubkey": client_pubkey_b64,
        }
    )

    quote = verify_attestation(data, nonce)
    session_id = data.get("session_id")
    vault_pubkey_b64 = data.get("vault_pubkey")

    if not isinstance(session_id, str) or not session_id:
        raise VaultApiError("Vault 返回了无效的 session_id")
    if session_id != quote.get("session_id"):
        raise VaultApiError("session_id 未绑定到 quote")
    if vault_pubkey_b64 != quote.get("vault_pubkey"):
        raise VaultApiError("vault_pubkey 未绑定到 quote")

    try:
        vault_pubkey = public_key_from_b64(vault_pubkey_b64, "vault_pubkey")
    except SecureChannelError as exc:
        raise VaultApiError(str(exc)) from exc

    shared_secret = client_private_key.exchange(vault_pubkey)
    channel_key = derive_channel_key(shared_secret, quote, client_pubkey_b64)
    logger.info(
        "安全信道建立完成: session_id=%s vault_pubkey_chars=%d",
        session_id[:8],
        len(vault_pubkey_b64),
    )

    return {
        "session_id": session_id,
        "channel_key": channel_key,
        "quote": quote,
    }


def _send_secure_json(
    session: dict[str, Any],
    envelope_action: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    # 输入 session、外层 action 和明文 payload；输出解密响应对象；作用是发送加密 JSON 请求。
    session_id = session.get("session_id")
    channel_key = session.get("channel_key")

    if not isinstance(session_id, str):
        raise VaultApiError("无效的 session_id")
    if not isinstance(channel_key, bytes):
        raise VaultApiError("无效的 channel_key")

    encrypted = encrypt_json(channel_key, session_id, payload)
    data = _send_request(
        {
            "action": envelope_action,
            "session_id": session_id,
            **encrypted,
        }
    )

    try:
        response = decrypt_json(
            channel_key,
            session_id,
            data.get("nonce"),
            data.get("ciphertext"),
        )
    except SecureChannelError as exc:
        raise VaultApiError(str(exc)) from exc

    return response


def secure_ping(session: dict[str, Any], message: str = "hello") -> dict[str, Any]:
    # 输入安全信道 session 和消息；输出 pong 响应；作用是验证加密信道可用。
    response = _send_secure_json(
        session,
        "secure_ping",
        {
            "type": "ping",
            "message": message,
        },
    )

    if response.get("type") != "pong":
        raise VaultApiError("secure_ping 返回了无效响应")
    return response


def provision_user_key(
    session: dict[str, Any],
    user_id: str,
    user_key: bytes | str | None = None,
) -> dict[str, Any]:
    # 输入 session、user_id 和可选 key；输出注入结果；作用是通过安全信道注入用户 Fernet key。
    if user_key is None:
        user_key = Fernet.generate_key()

    if isinstance(user_key, bytes):
        user_key_text = user_key.decode("ascii")
    elif isinstance(user_key, str):
        user_key_text = user_key
    else:
        raise VaultApiError("user_key 必须是 bytes、str 或 None")

    logger.info("开始注入用户记忆密钥: user_id=%s key_chars=%d", user_id, len(user_key_text))
    response = _send_secure_json(
        session,
        "secure_provision_user_key",
        {
            "action": "provision_user_key",
            "user_id": user_id,
            "user_key": user_key_text,
        },
    )

    if response.get("status") != "ok":
        raise VaultApiError("provision_user_key 返回了无效响应")

    logger.info("用户记忆密钥注入完成: user_id=%s", response.get("user_id"))
    return {
        "user_id": response.get("user_id"),
        "user_key": user_key_text,
    }


def open_user_vault_session(
    user_id: str,
    user_key: bytes | str | None = None,
) -> dict[str, Any]:
    # 输入 user_id 和可选用户 key；输出用户 vault session；作用是建立信道并完成用户密钥注入。
    logger.info("开始初始化用户 vault session: user_id=%s", user_id)
    session = open_secure_channel()
    provisioned = provision_user_key(session, user_id, user_key)
    session["user_id"] = provisioned["user_id"]
    session["user_key"] = provisioned["user_key"]
    logger.info("用户 vault session 初始化完成: user_id=%s session_id=%s", session["user_id"], session["session_id"][:8])
    return session


def secure_list_user_memories(
    session: dict[str, Any],
    user_id: str,
    status: str | None = "active",
) -> list[dict[str, Any]]:
    payload = {
        "action": "list_memories",
        "user_id": user_id,
    }
    if status is not None:
        payload["status"] = status

    response = _send_secure_json(session, "secure_request", payload)

    memories = response.get("memories", [])
    if not isinstance(memories, list):
        raise VaultApiError("Vault 服务器返回了无效的 memories")
    return memories

def secure_store_user_memories(
    session: dict[str, Any],
    user_id: str,
    memories: list[dict[str, Any]],
) -> int:
    # 输入 session、user_id 和记忆列表；输出存储数量；作用是通过安全信道存储用户记忆。
    response = _send_secure_json(
        session,
        "secure_request",
        {
            "action": "store",
            "user_id": user_id,
            "memories": memories,
        },
    )
    stored_count = response.get("stored_count", 0)
    if not isinstance(stored_count, int):
        logger.error("Vault 服务器返回了无效的 stored_count: %r", stored_count)
        raise VaultApiError("Vault 服务器返回了无效的 stored_count")
    return stored_count


def secure_retrieve_user_context(
    session: dict[str, Any],
    user_id: str,
    query: str,
    top_k: int = 3,
    threshold: float = 0.4,
) -> str:
    # 输入 session、user_id、查询和检索参数；输出最小化上下文；作用是通过安全信道检索用户记忆。
    response = _send_secure_json(
        session,
        "secure_request",
        {
            "action": "retrieve",
            "user_id": user_id,
            "query": query,
            "top_k": top_k,
            "threshold": threshold,
        },
    )
    memory_context = response.get("memory_context", "")
    if not isinstance(memory_context, str):
        logger.error("Vault 服务器返回了无效的 memory_context: %r", memory_context)
        raise VaultApiError("Vault 服务器返回了无效的 memory_context")
    return memory_context


def secure_forget_user_memories(
    session: dict[str, Any],
    user_id: str,
    memory_ids: list[str],
) -> int:
    response = _send_secure_json(
        session,
        "secure_request",
        {
            "action": "forget",
            "user_id": user_id,
            "memory_ids": memory_ids,
        },
    )

    forgotten_count = response.get("forgotten_count", 0)
    if not isinstance(forgotten_count, int):
        raise VaultApiError("Vault 服务器返回了无效的 forgotten_count")
    return forgotten_count