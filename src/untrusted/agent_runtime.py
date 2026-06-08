import hashlib
import json
import logging

import redis
from langchain_community.chat_models import ChatTongyi
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from interface.vault_api import (
    VaultApiError,
    retrieve_context_with_capability,
    store_memories_with_capability,
)
from untrusted.memory_extractor import extract_memories
from untrusted.wechat_activity_report_tool import try_handle_wechat_activity_report


SYSTEM_PROMPT = "你是一位乐于助人的助手。回答请保持清晰简洁。"

logger = logging.getLogger(__name__)


class AgentServiceError(Exception):
    """Agent 生成回复或访问依赖失败。"""


class AgentService:
    def __init__(
        self,
        redis_url: str,
        model_name: str,
        history_ttl_seconds: int = 86400,
    ) -> None:
        """初始化当前对象。"""
        self._redis_url = redis_url
        self._model_name = model_name
        self._history_ttl_seconds = history_ttl_seconds

        self._redis: redis.Redis | None = None
        self._llm: ChatTongyi | None = None

    def start(self) -> None:
        """初始化 Redis 和 LLM；应在 FastAPI 启动阶段调用。"""
        redis_client = redis.Redis.from_url(
            self._redis_url,
            decode_responses=True,
        )
        try:
            redis_client.ping()
            llm = ChatTongyi(model=self._model_name)
        except Exception:
            redis_client.close()
            raise

        self._redis = redis_client
        self._llm = llm
        logger.info("Agent service started")

    def close(self) -> None:
        """释放 Redis 连接和聊天 LLM 引用。"""
        if self._redis is not None:
            self._redis.close()
            self._redis = None

        self._llm = None

        logger.info("Agent service stopped")

    def _require_redis(self) -> redis.Redis:
        """返回已初始化的 Redis 客户端。"""
        if self._redis is None:
            raise AgentServiceError("Agent service has not started")
        return self._redis

    def _require_llm(self) -> ChatTongyi:
        """返回已初始化的聊天 LLM。"""
        if self._llm is None:
            raise AgentServiceError("Agent service has not started")
        return self._llm

    def _capability_subject(self, capability: str | None) -> str:
        # 输入 bearer capability；输出不可逆短指纹；作用是隔离 Redis history 而不保存 capability 原文。
        """把 capability 转为 Redis 隔离用的不可逆指纹。"""
        token = capability or "no-vault"
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _history_key(self, capability: str | None, session_id: str) -> str:
        # 输入 capability 和会话 ID；输出 Redis key；作用是按 capability 身份隔离短期对话历史。
        """生成短期会话历史的 Redis key。"""
        return f"chat:{self._capability_subject(capability)}:{session_id}:history"

    def _load_history(
        self,
        capability: str | None,
        session_id: str,
    ) -> list[dict[str, str]]:
        # 输入 capability 和会话 ID；输出清洗后的历史；作用是从 Redis 读取当前用户短期对话。
        """从 Redis 加载并清洗短期会话历史。"""
        redis_client = self._require_redis()
        raw = redis_client.get(self._history_key(capability, session_id))

        if not raw:
            return []

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(
                "Invalid Redis history: capability_subject=%s session_id=%s",
                self._capability_subject(capability)[:12],
                session_id,
            )
            return []

        if not isinstance(data, list):
            return []

        history = []
        for item in data:
            if not isinstance(item, dict):
                continue

            role = item.get("role")
            content = item.get("content")

            if role in {"user", "assistant"} and isinstance(content, str):
                history.append({"role": role, "content": content})

        return history

    def _save_history(
        self,
        capability: str | None,
        session_id: str,
        history: list[dict[str, str]],
    ) -> None:
        # 输入 capability、会话 ID 和历史；输出无返回值；作用是带 TTL 保存短期对话。
        """把短期会话历史按 TTL 写入 Redis。"""
        redis_client = self._require_redis()
        redis_client.setex(
            self._history_key(capability, session_id),
            self._history_ttl_seconds,
            json.dumps(history, ensure_ascii=False),
        )

    def _build_messages(
        self,
        history: list[dict[str, str]],
        user_message: str,
        memory_context: str,
    ) -> list:
        """组装发送给聊天模型的系统消息、历史和用户消息。"""
        messages = [SystemMessage(content=SYSTEM_PROMPT)]

        if memory_context:
            messages.append(
                SystemMessage(
                    content=(
                        "以下是经过隐私最小化处理后的用户长期记忆。"
                        "只能在回答当前问题时使用，不要主动复述这些隐私信息："
                        f"{memory_context}"
                    )
                )
            )

        for item in history:
            if item["role"] == "user":
                messages.append(HumanMessage(content=item["content"]))
            else:
                messages.append(AIMessage(content=item["content"]))

        messages.append(HumanMessage(content=user_message))
        return messages

    def generate_reply(
        self,
        capability: str | None,
        session_id: str,
        user_message: str,
    ) -> dict[str, object]:
        # 输入 capability、会话 ID 和用户消息；输出回复结果；作用是检索记忆、调用 LLM 并保存短期历史。
        """生成助手回复并更新短期会话历史。"""
        user_message = user_message.strip()
        if not user_message:
            raise AgentServiceError("message cannot be empty")

        history = self._load_history(capability, session_id)

        tool_reply = try_handle_wechat_activity_report(
            user_message,
            conversation_history=history,
        )
        if tool_reply is not None:
            history.extend(
                [
                    {"role": "user", "content": user_message},
                    {"role": "assistant", "content": tool_reply},
                ]
            )
            self._save_history(capability, session_id, history)
            return {
                "reply": tool_reply,
                "memory_context_used": False,
            }

        memory_context = ""
        if capability:
            try:
                memory_context = retrieve_context_with_capability(
                    capability,
                    user_message,
                    top_k=3,
                    threshold=0.4,
                )
            except VaultApiError:
                logger.exception("Vault capability retrieval failed; continuing without memory context")
                memory_context = ""

        messages = self._build_messages(
            history,
            user_message,
            memory_context,
        )

        try:
            response = self._require_llm().invoke(messages)
        except Exception as exc:
            logger.exception("LLM invocation failed")
            raise AgentServiceError("LLM invocation failed") from exc

        reply = str(response.content)

        history.extend(
            [
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": reply},
            ]
        )
        self._save_history(capability, session_id, history)

        return {
            "reply": reply,
            "memory_context_used": bool(memory_context),
        }

    def store_memory_background(
        self,
        capability: str | None,
        user_message: str,
    ) -> None:
        # 输入 capability 和用户消息；输出无返回值；作用是后台抽取并以 capability 写入长期记忆。
        """在后台从用户原话抽取并写入长期记忆。"""
        if not capability:
            return
        try:
            memories = extract_memories(
                [HumanMessage(content=user_message)]
            )

            if memories:
                store_memories_with_capability(
                    capability,
                    memories,
                )
        except Exception:
            logger.exception("Background memory storage failed")
