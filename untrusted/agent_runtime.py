import json
import logging
import os
import threading
from typing import Any

import redis
from langchain_community.chat_models import ChatTongyi
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from interface.vault_api import (
    VaultApiError,
    open_user_vault_session,
    secure_retrieve_user_context,
    secure_store_user_memories,
)
from untrusted.memory_extractor import extract_memories


SYSTEM_PROMPT = "你是一位乐于助人的助手。回答请保持清晰简洁。"

REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
HISTORY_TTL_SECONDS = int(os.getenv("CHAT_HISTORY_TTL_SECONDS", "86400"))

logger = logging.getLogger(__name__)

redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
llm = ChatTongyi(model=os.getenv("TONGYI_MODEL", "qwen-turbo"))

_VAULT_SESSIONS: dict[str, dict[str, Any]] = {}
_VAULT_SESSION_LOCK = threading.Lock()


class AgentRuntimeError(Exception):
    pass


def _history_key(user_id: str, session_id: str) -> str:
    return f"chat:{user_id}:{session_id}:history"


def _load_history(user_id: str, session_id: str) -> list[dict[str, str]]:
    raw = redis_client.get(_history_key(user_id, session_id))
    if not raw:
        return []

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Redis history JSON invalid: user_id=%s session_id=%s", user_id, session_id)
        return []

    if not isinstance(data, list):
        return []

    clean_history = []
    for item in data:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role in {"user", "assistant"} and isinstance(content, str):
            clean_history.append({"role": role, "content": content})

    return clean_history


def _save_history(user_id: str, session_id: str, history: list[dict[str, str]]) -> None:
    redis_client.setex(
        _history_key(user_id, session_id),
        HISTORY_TTL_SECONDS,
        json.dumps(history, ensure_ascii=False),
    )


def _to_langchain_messages(history: list[dict[str, str]]):
    messages = [SystemMessage(content=SYSTEM_PROMPT)]

    for item in history:
        if item["role"] == "user":
            messages.append(HumanMessage(content=item["content"]))
        elif item["role"] == "assistant":
            messages.append(AIMessage(content=item["content"]))

    return messages


def _get_vault_session(user_id: str) -> dict[str, Any] | None:
    with _VAULT_SESSION_LOCK:
        session = _VAULT_SESSIONS.get(user_id)
        if session is not None:
            return session

        user_key = os.getenv("USER_MEMORY_KEY")

        try:
            session = open_user_vault_session(user_id, user_key)
        except VaultApiError:
            logger.exception("Vault session setup failed: user_id=%s", user_id)
            return None

        _VAULT_SESSIONS[user_id] = session
        return session


def generate_reply(user_id: str, session_id: str, user_message: str) -> dict[str, Any]:
    user_message = user_message.strip()
    if not user_message:
        raise AgentRuntimeError("message cannot be empty")

    vault_session = _get_vault_session(user_id)

    memory_context = ""
    if vault_session is not None:
        try:
            memory_context = secure_retrieve_user_context(
                vault_session,
                user_id,
                user_message,
                top_k=3,
                threshold=0.4,
            )
        except VaultApiError:
            logger.exception("Vault retrieve failed; continuing without memory")

    history = _load_history(user_id, session_id)
    messages = _to_langchain_messages(history)

    if memory_context:
        messages.insert(
            1,
            SystemMessage(
                content=(
                    "以下是经过隐私最小化处理后的用户长期记忆。"
                    "只能在回答当前问题时使用，不要主动复述这些隐私信息："
                    f"{memory_context}"
                )
            ),
        )

    messages.append(HumanMessage(content=user_message))

    try:
        response = llm.invoke(messages)
    except Exception as exc:
        logger.exception("LLM invocation failed")
        raise AgentRuntimeError("LLM invocation failed") from exc

    reply = str(response.content)

    history.append({"role": "user", "content": user_message})
    history.append({"role": "assistant", "content": reply})
    _save_history(user_id, session_id, history)

    return {
        "reply": reply,
        "memory_context_used": bool(memory_context),
    }


def store_memory_background(user_id: str, user_message: str) -> None:
    try:
        vault_session = _get_vault_session(user_id)
        if vault_session is None:
            return

        memories = extract_memories([HumanMessage(content=user_message)])
        if memories:
            secure_store_user_memories(vault_session, user_id, memories)
    except Exception:
        logger.exception("Background memory storage failed: user_id=%s", user_id)