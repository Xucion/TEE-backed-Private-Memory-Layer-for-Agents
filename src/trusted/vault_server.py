import base64
import json
import logging
import os
import secrets
import socketserver
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Support running this file directly via `python src/trusted/vault_server.py`.
SOURCE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

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
from cryptography.hazmat.primitives import serialization
from trusted.memory_retriever import retrieve
from trusted.memory_store import list_memories, forget_memories, store_memories
from trusted.user_key_manager import UserKeyError, get_user_key, provision_user_key


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_REQUEST_BYTES = 1024 * 1024

# 模拟 RA 的固定身份声明。真实 SGX 环境会由 quote measurement 替代。
SIM_RA_PROTOCOL_VERSION = "sim-ra-v1"
SIM_RA_MODE = "SIMULATED_RA_ONLY"
SIM_RA_VAULT_ID = "confidential-agent-memory-vault"
SIM_RA_MEASUREMENT = "confidential-agent-memory-vault-dev-v1"
MAX_NONCE_CHARS = 128
SIM_RA_PRIVATE_KEY_FILE = SOURCE_ROOT / "trusted" / "sim_ra_private_key.pem"
SESSION_TTL_SECONDS = 300

CAPABILITY_TTL_SECONDS = 3600
MAX_ACTIVE_SESSIONS = 10000
MAX_ACTIVE_CAPABILITIES = 10000
_CAPABILITIES: dict[str, dict[str, Any]] = {}
_CAPABILITY_LOCK = threading.Lock()

_STORE_LOCK = threading.Lock()
_SESSION_LOCK = threading.Lock()
_SESSIONS: dict[str, dict[str, Any]] = {}

VALID_MEMORY_TYPES = {
    "preference",
    "profile",
    "health",
    "project",
    "instruction",
    "other",
}

VALID_MEMORY_SENSITIVITIES = {
    "high",
    "low",
}

VALID_MEMORY_PREDICATES = {
    "likes",
    "dislikes",
    "has_goal",
    "has_health_condition",
    "prefers_response_style",
    "works_on_project",
    "lives_in",
    "works_at",
    "has_job_search_status",
    "prefers_language",
    "stated_fact",
}

VALID_MEMORY_SOURCES = {
    "user",
    "system",
    "tool",
    "admin",
}

PREDICATE_DEFAULT_SLOTS = {
    "lives_in": "profile.current_city",
    "works_at": "profile.current_company",
    "has_job_search_status": "profile.job_search_status",
    "prefers_language": "instruction.response_language",
}

PREDICATE_ALLOWED_SLOTS = {
    "works_on_project": {"project.primary_project"},
}

logging.basicConfig(
    level=os.getenv("VAULT_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [VaultServer] %(message)s",
)
logger = logging.getLogger(__name__)


class VaultError(Exception):
    """预期的请求或验证失败，将返回给 vault 客户端。"""


def _load_sim_ra_private_key():
    # 输入无显式参数；输出模拟 RA 私钥对象；作用是加载用于签名 quote 的开发私钥。
    key_data = SIM_RA_PRIVATE_KEY_FILE.read_bytes()
    return serialization.load_pem_private_key(key_data, password=None)


def _sign_quote(quote: dict[str, Any]) -> str:
    # 输入 quote 对象；输出 base64 签名；作用是用模拟 RA 私钥签名身份声明。
    private_key = _load_sim_ra_private_key()
    signature = private_key.sign(canonical_json(quote))
    return base64.b64encode(signature).decode("ascii")


def _success(data: dict[str, Any] | None = None) -> dict[str, Any]:
    # 输入可选响应数据；输出成功响应对象；作用是统一 vault 成功返回格式。
    return {"ok": True, "data": data or {}}


def _failure(message: str) -> dict[str, Any]:
    # 输入错误消息；输出失败响应对象；作用是统一 vault 错误返回格式。
    return {"ok": False, "error": message}


def _coerce_positive_int(value: Any, default: int, max_value: int) -> int:
    # 输入待校验值、默认值和上限；输出正整数；作用是规范 top_k 等正整数参数。
    if value is None:
        return default
    if not isinstance(value, int) or value <= 0 or value > max_value:
        raise VaultError(f"期望正整数，且不超过 {max_value}")
    return value


def _coerce_threshold(value: Any, default: float) -> float:
    # 输入待校验阈值和默认值；输出 0 到 1 的浮点数；作用是规范检索阈值参数。
    if value is None:
        return default
    if not isinstance(value, (int, float)):
        raise VaultError("期望数值类型的阈值")
    threshold = float(value)
    if threshold < 0.0 or threshold > 1.0:
        raise VaultError("期望阈值介于 0 和 1 之间")
    return threshold


def _normalize_slot_part(value: Any) -> str:
    # 输入任意槽位片段；输出小写下划线文本；作用是规范 memory slot 组成部分。
    return "_".join(str(value or "").strip().lower().split())


def _normalize_memory_slot(predicate: str, object_value: str, raw_slot: Any) -> str | None:
    # 输入谓词、对象和原始 slot；输出规范 slot 或 None；作用是确定互斥事实槽位。
    slot = None
    if raw_slot is not None:
        slot = _normalize_slot_part(raw_slot) or None

    if predicate in {"likes", "dislikes"}:
        object_part = _normalize_slot_part(object_value)
        if object_part:
            return f"preference.like_dislike:{object_part}"
        return None

    default_slot = PREDICATE_DEFAULT_SLOTS.get(predicate)
    if default_slot:
        return default_slot

    allowed_slots = PREDICATE_ALLOWED_SLOTS.get(predicate, set())
    if slot in allowed_slots:
        return slot

    return None


def _validate_nonce(value: Any) -> str:
    # 输入任意 nonce 值；输出合法 nonce 字符串；作用是校验模拟 RA 和握手随机数。
    if not isinstance(value, str):
        raise VaultError("attest 操作需要提供字符串 nonce")

    nonce = value.strip()
    if not nonce:
        raise VaultError("attest 操作需要提供非空 nonce")
    if len(nonce) > MAX_NONCE_CHARS:
        raise VaultError(f"nonce 长度不能超过 {MAX_NONCE_CHARS}")

    return nonce


def _build_simulated_quote(nonce: str) -> dict[str, str]:
    # 输入 nonce；输出模拟 quote；作用是构造开发环境中的 vault 身份声明。
    return {
        "protocol_version": SIM_RA_PROTOCOL_VERSION,
        "mode": SIM_RA_MODE,
        "vault_id": SIM_RA_VAULT_ID,
        "measurement": SIM_RA_MEASUREMENT,
        "nonce": nonce,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _get_session_key(session_id: Any) -> bytes:
    # 输入 session_id；输出信道密钥；作用是读取并校验未过期的安全信道 session。
    if not isinstance(session_id, str) or not session_id:
        raise VaultError("需要有效的 session_id")

    now = time.time()
    with _SESSION_LOCK:
        session = _SESSIONS.get(session_id)
        if not session:
            logger.warning("安全信道 session 查询失败: session_id=%s", str(session_id)[:8])
            raise VaultError("安全信道 session 不存在或已过期")

        if now - float(session["created_at"]) > SESSION_TTL_SECONDS:
            _SESSIONS.pop(session_id, None)
            logger.warning("安全信道 session 已过期: session_id=%s", session_id[:8])
            raise VaultError("安全信道 session 已过期")

        return session["channel_key"]


def _get_bound_session_user_id(session_id: str) -> str:
    # 输入 session_id；输出 session 绑定的 user_id；作用是阻止安全会话切换到其他用户。
    _get_session_key(session_id)
    with _SESSION_LOCK:
        session = _SESSIONS.get(session_id)
        if session is None:
            raise VaultError("安全信道 session 不存在或已过期")
        user_id = session.get("user_id")
        if not isinstance(user_id, str) or not user_id:
            raise VaultError("安全信道 session 尚未绑定用户")
        return user_id


def _assert_session_can_bind_user(session_id: str, requested_user_id: Any) -> None:
    # 输入 session_id 和待注入 user_id；输出无返回值；作用是在写 key 前阻止已绑定 session 切换用户。
    _get_session_key(session_id)
    if not isinstance(requested_user_id, str) or not requested_user_id.strip():
        raise VaultError("user_id 必须是非空字符串")
    normalized_user_id = requested_user_id.strip()

    with _SESSION_LOCK:
        session = _SESSIONS.get(session_id)
        if session is None:
            raise VaultError("安全信道 session 不存在或已过期")
        bound_user_id = session.get("user_id")
        if bound_user_id is not None and bound_user_id != normalized_user_id:
            raise VaultError("session 已绑定其他用户")


def _purge_expired_capabilities(now: float | None = None) -> None:
    # 输入可选当前时间；输出无返回值；作用是清理过期 capability 并限制内存增长。
    current_time = now or time.time()
    with _CAPABILITY_LOCK:
        expired_tokens = [
            token
            for token, record in _CAPABILITIES.items()
            if current_time >= float(record["expires_at"])
        ]
        for token in expired_tokens:
            _CAPABILITIES.pop(token, None)


def _decrypt_secure_payload(session_id: str, request: dict[str, Any]) -> dict[str, Any]:
    # 输入 session_id 和加密请求；输出明文 payload；作用是解密并校验安全信道请求。
    logger.info("开始解密安全信道请求: session_id=%s", session_id[:8])
    try:
        payload = decrypt_json(
            _get_session_key(session_id),
            session_id,
            request.get("nonce"),
            request.get("ciphertext"),
        )
    except SecureChannelError as exc:
        raise VaultError(str(exc)) from exc
    logger.info(
        "安全信道请求解密成功: session_id=%s payload_action=%s",
        session_id[:8],
        payload.get("action") or payload.get("type"),
    )
    return payload


def _encrypt_secure_payload(session_id: str, payload: dict[str, Any]) -> dict[str, str]:
    # 输入 session_id 和明文响应；输出加密 envelope；作用是加密安全信道响应。
    logger.info(
        "加密安全信道响应: session_id=%s payload_keys=%s",
        session_id[:8],
        sorted(payload.keys()),
    )
    try:
        return encrypt_json(_get_session_key(session_id), session_id, payload)
    except SecureChannelError as exc:
        raise VaultError(str(exc)) from exc


def _legacy_plaintext_allowed() -> bool:
    # 输入环境变量；输出是否允许明文请求；作用是控制旧版 store/retrieve 兼容入口。
    return os.getenv("VAULT_ALLOW_LEGACY_PLAINTEXT", "").lower() in {"1", "true", "yes"}


def _get_request_user_key(request: dict[str, Any]) -> tuple[str, bytes]:
    # 输入请求对象；输出规范 user_id 和用户 key；作用是从内存密钥表解析请求所属用户。
    user_id = request.get("user_id")
    try:
        user_key = get_user_key(user_id)
    except UserKeyError as exc:
        raise VaultError(str(exc)) from exc

    normalized_user_id = str(user_id).strip()
    logger.info("已获取用户记忆密钥: user_id=%s", normalized_user_id)
    return normalized_user_id, user_key


def _validate_memory(mem: Any) -> dict[str, Any]:
    # 输入任意记忆对象；输出规范化记忆字典；作用是校验和收敛 vault 接收的 memory 字段。
    if not isinstance(mem, dict):
        raise VaultError("每条记忆必须是一个对象")

    content = str(mem.get("content", "")).strip()
    memory_type = str(mem.get("memory_type", "other")).strip()
    sensitivity = str(mem.get("sensitivity", "low")).strip()
    subject = str(mem.get("subject", "user")).strip()
    predicate = str(mem.get("predicate", "stated_fact")).strip()
    object_value = str(mem.get("object", "")).strip()
    value = mem.get("value", True)
    slot = mem.get("slot")
    source = str(mem.get("source", "user")).strip()
    confidence = mem.get("confidence", 0.8)

    if not content:
        raise VaultError("记忆内容不能为空")
    if memory_type not in VALID_MEMORY_TYPES:
        memory_type = "other"
    if sensitivity not in VALID_MEMORY_SENSITIVITIES:
        sensitivity = "low"
    if predicate not in VALID_MEMORY_PREDICATES:
        predicate = "stated_fact"
    if source not in VALID_MEMORY_SOURCES:
        source = "user"
    if subject != "user":
        subject = "user"
    if not object_value:
        object_value = content
    slot = _normalize_memory_slot(predicate, object_value, slot)
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = 0.8
    confidence = min(1.0, max(0.0, confidence))

    if memory_type == "health":
        sensitivity = "high"
    return {
        "content": content,
        "memory_type": memory_type,
        "sensitivity": sensitivity,
        "subject": subject,
        "predicate": predicate,
        "object": object_value,
        "value": value,
        "slot": slot,
        "confidence": confidence,
        "source": source,
    }

def _handle_list_memories_data(request: dict[str, Any]) -> dict[str, Any]:
    user_id, user_key = _get_request_user_key(request)

    status = request.get("status")
    if status is not None:
        status = str(status).strip()
        if status not in {"active", "superseded", "forgotten", "expired"}:
            raise VaultError(f"不支持的 memory status: {status}")

    with _STORE_LOCK:
        memories = list_memories(user_id, user_key, status=status)

    return {
        "memories": memories,
        "memory_count": len(memories),
    }

def _handle_store_data(request: dict[str, Any]) -> dict[str, Any]:
    # 输入明文 store payload；输出 stored_count 数据；作用是校验并写入用户记忆。
    user_id, user_key = _get_request_user_key(request)
    raw_memories = request.get("memories")
    if not isinstance(raw_memories, list):
        raise VaultError("store 操作需要提供 memories 列表")

    logger.info("开始处理 store 请求: user_id=%s memory_count=%d", user_id, len(raw_memories))
    memories = [_validate_memory(mem) for mem in raw_memories]
    with _STORE_LOCK:
        stored_count = store_memories(user_id, user_key, memories)
    logger.info("store 请求完成: user_id=%s stored_count=%d", user_id, stored_count)
    return {"stored_count": stored_count}

def _handle_forget_data(request: dict[str, Any]) -> dict[str, Any]:
    user_id, user_key = _get_request_user_key(request)

    memory_id = str(request.get("memory_id", "")).strip()
    memory_ids = request.get("memory_ids")

    if memory_id:
        target_ids = [memory_id]
    elif isinstance(memory_ids, list):
        target_ids = [str(item).strip() for item in memory_ids if str(item).strip()]
    else:
        raise VaultError("forget 操作需要提供 memory_id 或 memory_ids")

    if not target_ids:
        raise VaultError("forget 操作需要至少一个有效 memory_id")

    with _STORE_LOCK:
        forgotten_count = forget_memories(user_id, user_key, target_ids)

    return {"forgotten_count": forgotten_count}

def _minimize_memories(memories: list[dict[str, Any]]) -> str:
    # 输入检索结果列表；输出最小化上下文文本；作用是避免高敏感记忆原文出 vault。
    type_hints = {
        "health": "用户有健康相关的限制。提供保守的、注重安全的指导。",
        "project": "用户有重要的项目背景。回复相关话题时需要谨慎。",
        "preference": "用户有特定的偏好。在不暴露敏感细节的前提下考虑这些偏好。",
        "profile": "用户有个人背景信息。谨慎使用，仅在相关时参考。",
        "instruction": "用户有长期交互偏好。按该偏好调整回复方式。",
        "other": "用户有特殊的背景信息。谨慎使用，仅在相关时参考。",
    }

    minimized: list[str] = []
    for mem in memories:
        if mem.get("sensitivity") == "high":
            minimized.append(type_hints.get(str(mem.get("memory_type")), type_hints["other"]))
            continue

        content = str(mem.get("content", "")).strip()
        if content:
            minimized.append(content)

    deduped = list(dict.fromkeys(minimized))
    return " ".join(deduped)


def _handle_retrieve_data(request: dict[str, Any]) -> dict[str, Any]:
    # 输入明文 retrieve payload；输出上下文和数量；作用是检索并最小化用户记忆。
    user_id, user_key = _get_request_user_key(request)
    query = str(request.get("query", "")).strip()
    if not query:
        raise VaultError("retrieve 操作需要提供非空的查询内容")

    top_k = _coerce_positive_int(request.get("top_k"), default=3, max_value=20)
    threshold = _coerce_threshold(request.get("threshold"), default=0.4)
    logger.info(
        "开始处理 retrieve 请求: user_id=%s query_chars=%d top_k=%d threshold=%.3f",
        user_id,
        len(query),
        top_k,
        threshold,
    )
    with _STORE_LOCK:
        memories = retrieve(user_id, user_key, query, top_k=top_k, threshold=threshold)
    memory_context = _minimize_memories(memories)
    logger.info(
        "retrieve 请求完成: user_id=%s retrieved_count=%d context_chars=%d",
        user_id,
        len(memories),
        len(memory_context),
    )

    return {
        "memory_context": memory_context,
        "retrieved_count": len(memories),
    }


def handle_request(request: dict[str, Any]) -> dict[str, Any]:
    # 输入 vault 请求对象；输出统一响应对象；作用是分发 attest、握手、加密请求和兼容明文请求。
    action = request.get("action")

    if action == "attest":
        nonce = _validate_nonce(request.get("nonce"))
        logger.info("收到模拟 RA attest 请求: nonce_chars=%d", len(nonce))
        quote = _build_simulated_quote(nonce)
        signature = _sign_quote(quote)
        logger.info(
            "模拟 RA quote 已签名: measurement=%s signature_chars=%d",
            quote["measurement"],
            len(signature),
        )
        return _success(
            {
                "quote": quote,
                "signature": signature,
            }
        )

    if action == "handshake_start":
        nonce = _validate_nonce(request.get("nonce"))
        client_pubkey_b64 = request.get("client_pubkey")
        logger.info(
            "收到安全信道握手请求: nonce_chars=%d client_pubkey_chars=%d",
            len(nonce),
            len(client_pubkey_b64) if isinstance(client_pubkey_b64, str) else -1,
        )
        try:
            client_pubkey = public_key_from_b64(client_pubkey_b64, "client_pubkey")
        except SecureChannelError as exc:
            raise VaultError(str(exc)) from exc

        vault_private_key, vault_public_key = generate_x25519_keypair()
        vault_pubkey_b64 = public_key_to_b64(vault_public_key)
        session_id = secrets.token_urlsafe(24)

        quote = _build_simulated_quote(nonce)
        quote["session_id"] = session_id
        quote["vault_pubkey"] = vault_pubkey_b64
        signature = _sign_quote(quote)

        shared_secret = vault_private_key.exchange(client_pubkey)
        channel_key = derive_channel_key(shared_secret, quote, str(client_pubkey_b64))

        with _SESSION_LOCK:
            expired_session_ids = [
                existing_session_id
                for existing_session_id, session in _SESSIONS.items()
                if time.time() - float(session["created_at"]) > SESSION_TTL_SECONDS
            ]
            for expired_session_id in expired_session_ids:
                _SESSIONS.pop(expired_session_id, None)
            if len(_SESSIONS) >= MAX_ACTIVE_SESSIONS:
                raise VaultError("安全信道 session 数量已达上限")
            _SESSIONS[session_id] = {
                "channel_key": channel_key,
                "created_at": time.time(),
            }

        logger.info(
            "安全信道握手完成: session_id=%s vault_pubkey_chars=%d ttl_seconds=%d",
            session_id[:8],
            len(vault_pubkey_b64),
            SESSION_TTL_SECONDS,
        )
        return _success(
            {
                "session_id": session_id,
                "vault_pubkey": vault_pubkey_b64,
                "quote": quote,
                "signature": signature,
            }
        )

    if action == "secure_ping":
        session_id = request.get("session_id")
        if not isinstance(session_id, str):
            raise VaultError("secure_ping 需要 session_id")

        logger.info("收到 secure_ping: session_id=%s", session_id[:8])
        payload = _decrypt_secure_payload(session_id, request)
        if payload.get("type") != "ping":
            raise VaultError("secure_ping 明文类型无效")

        logger.info("secure_ping 解密成功，准备返回 pong: session_id=%s", session_id[:8])
        return _success(
            _encrypt_secure_payload(
                session_id,
                {
                    "type": "pong",
                    "message": str(payload.get("message", "")),
                },
            )
        )

    if action == "secure_provision_user_key":
        session_id = request.get("session_id")
        if not isinstance(session_id, str):
            raise VaultError("secure_provision_user_key 需要 session_id")

        logger.info("收到加密用户密钥注入请求: session_id=%s", session_id[:8])
        payload = _decrypt_secure_payload(session_id, request)
        if payload.get("action") != "provision_user_key":
            raise VaultError("安全信道明文 action 无效")

        _assert_session_can_bind_user(session_id, payload.get("user_id"))
        try:
            user_id = provision_user_key(
                payload.get("user_id"),
                payload.get("user_key"),
            )
        except UserKeyError as exc:
            raise VaultError(str(exc)) from exc

        logger.info("用户密钥注入完成: session_id=%s user_id=%s", session_id[:8], user_id)
        capability = _bind_user_and_issue_capability(session_id, user_id)

        return _success(
            _encrypt_secure_payload(
                session_id,
                {
                    "status": "ok",
                    "user_id": user_id,
                    "capability": capability,
                    "expires_in": CAPABILITY_TTL_SECONDS,
                },
            )
        )

    if action == "capability_request":
        operation = str(request.get("operation", ""))
        user_id = _resolve_capability(
            request.get("capability"),
            operation,
        )

        bound_request = {
            **request,
            "user_id": user_id,
        }

        if operation == "retrieve":
            data = _handle_retrieve_data(bound_request)
        elif operation == "store":
            data = _handle_store_data(bound_request)
        elif operation == "list_memories":
            data = _handle_list_memories_data(bound_request)
        elif operation == "forget":
            data = _handle_forget_data(bound_request)
        else:
            raise VaultError("不支持的 capability operation")

        return _success(data)


    if action == "secure_request":
        session_id = request.get("session_id")
        if not isinstance(session_id, str):
            raise VaultError("secure_request 需要 session_id")

        logger.info("收到 secure_request: session_id=%s", session_id[:8])
        payload = _decrypt_secure_payload(session_id, request)
        payload["user_id"] = _get_bound_session_user_id(session_id)
        inner_action = payload.get("action")
        logger.info("secure_request 内部操作: session_id=%s inner_action=%s", session_id[:8], inner_action)
        if inner_action == "store":
            data = _handle_store_data(payload)
        elif inner_action == "retrieve":
            data = _handle_retrieve_data(payload)
        elif inner_action == "forget":
            data = _handle_forget_data(payload)
        elif inner_action == "list_memories":
            data = _handle_list_memories_data(payload)
        else:
            raise VaultError(f"secure_request 不支持的内部操作: {inner_action}")

        logger.info("secure_request 处理完成: session_id=%s inner_action=%s", session_id[:8], inner_action)
        return _success(_encrypt_secure_payload(session_id, data))

    if action == "store":
        if not _legacy_plaintext_allowed():
            raise VaultError("明文 store 已禁用，请使用 secure_request")
        return _success(_handle_store_data(request))

    if action == "retrieve":
        if not _legacy_plaintext_allowed():
            raise VaultError("明文 retrieve 已禁用，请使用 secure_request")
        return _success(_handle_retrieve_data(request))

    raise VaultError(f"不支持的操作: {action}")


class VaultRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        # 输入 socket 单行 JSON 请求；输出 socket JSON 响应；作用是处理一个 vault 客户端连接。
        try:
            raw = self.rfile.readline(MAX_REQUEST_BYTES + 1)
            if len(raw) > MAX_REQUEST_BYTES:
                raise VaultError("请求过大")
            if not raw:
                return

            request = json.loads(raw.decode("utf-8"))
            if not isinstance(request, dict):
                raise VaultError("请求必须是 JSON 对象")

            response = handle_request(request)
        except json.JSONDecodeError as exc:
            logger.warning("无效的 JSON 请求: %s", exc)
            response = _failure("无效的 JSON 请求")
        except VaultError as exc:
            logger.warning("请求被拒绝: %s", exc)
            response = _failure(str(exc))
        except Exception:
            logger.exception("未处理的 vault 服务器错误")
            response = _failure("vault 服务器内部错误")

        self.wfile.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))


class ThreadedVaultServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def run_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    # 输入监听地址和端口；输出无返回值；作用是启动多线程 vault TCP 服务。
    with ThreadedVaultServer((host, port), VaultRequestHandler) as server:
        logger.info("Vault 服务器正在监听 %s:%s", host, port)
        server.serve_forever()


def _bind_user_and_issue_capability(session_id: str, user_id: str) -> str:
    # 输入 session_id 和 user_id；输出 bearer capability；作用是绑定会话用户并授予 Agent 数据面权限。
    _get_session_key(session_id)
    with _SESSION_LOCK:
        session = _SESSIONS.get(session_id)
        if session is None:
            raise VaultError("session 不存在")

        bound_user_id = session.get("user_id")
        if bound_user_id is not None and bound_user_id != user_id:
            raise VaultError("session 已绑定其他用户")

        session["user_id"] = user_id

    capability = secrets.token_urlsafe(32)

    _purge_expired_capabilities()
    with _CAPABILITY_LOCK:
        if len(_CAPABILITIES) >= MAX_ACTIVE_CAPABILITIES:
            raise VaultError("capability 数量已达上限")
        _CAPABILITIES[capability] = {
            "user_id": user_id,
            "expires_at": time.time() + CAPABILITY_TTL_SECONDS,
            "scopes": {"store", "retrieve"},
        }

    return capability


def _resolve_capability(token: Any, required_scope: str) -> str:
    # 输入 capability 和所需权限；输出绑定 user_id；作用是校验 bearer token、期限和 scope。
    if not isinstance(token, str) or not token:
        raise VaultError("缺少 capability")

    _purge_expired_capabilities()
    with _CAPABILITY_LOCK:
        record = _CAPABILITIES.get(token)

        if record is None:
            raise VaultError("capability 无效")

        if required_scope not in record["scopes"]:
            raise VaultError("capability 权限不足")

        return str(record["user_id"])

def main() -> None:
    # 输入环境变量中的监听配置；输出无返回值；作用是作为 vault server 命令行入口。
    host = os.getenv("VAULT_HOST", DEFAULT_HOST)
    port = int(os.getenv("VAULT_PORT", str(DEFAULT_PORT)))
    run_server(host=host, port=port)


if __name__ == "__main__":
    main()
