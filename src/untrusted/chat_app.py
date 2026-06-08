import os
import json
import logging
import threading
import sys
from pathlib import Path
from typing import Annotated, Any, TypedDict
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_community.chat_models import ChatTongyi
from langgraph.graph import StateGraph, add_messages

# Support running this file directly via `python src/untrusted/chat_app.py`.
SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from interface.vault_api import (
    VaultApiError,
    open_user_vault_session,
    secure_retrieve_user_context,
    secure_store_user_memories,
)
from untrusted.memory_extractor import extract_memories
from untrusted.wechat_activity_report_tool import (
    is_wechat_activity_report_request,
    try_handle_wechat_activity_report,
)


SYSTEM_PROMPT = "你是一位乐于助人的助手。回答请保持清晰简洁。"
logging.basicConfig(
    level=os.getenv("CHAT_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [ChatApp] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)
VAULT_SESSION: dict[str, Any] | None = None


class ChatState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    user_input: str
    memory_context: str


def retrieve_memory(state: ChatState) -> ChatState:
    # 输入 LangGraph 聊天状态；输出带 memory_context 的状态片段；作用是从安全 vault 检索相关记忆。
    """从 vault 检索当前问题相关的长期记忆。"""
    query = state["user_input"]
    if is_wechat_activity_report_request(query):
        return {"memory_context": ""}
    if VAULT_SESSION is None:
        return {"memory_context": ""}

    user_id = str(VAULT_SESSION["user_id"])
    
    try:
        context = secure_retrieve_user_context(VAULT_SESSION, user_id, query, top_k=3, threshold=0.4)
    except VaultApiError:
        logger.exception("Vault retrieval failed; continuing without memory context")
        context = ""
    return {"memory_context": context}



def build_graph():
    # 输入无显式参数；输出编译后的 LangGraph 应用；作用是组装检索与聊天生成流程。
    """构建当前模块使用的 LangGraph 对话图。"""
    model_name = os.getenv("TONGYI_MODEL", "qwen-turbo")
    llm = ChatTongyi(model=model_name)

    def chatbot(state: ChatState) -> ChatState:
        # 输入 LangGraph 聊天状态；输出新增助手消息的状态片段；作用是调用通义模型生成回复。
        """调用聊天模型生成当前轮回复。"""
        messages = list(state["messages"])
        tool_reply = try_handle_wechat_activity_report(
            state["user_input"],
            conversation_history=_message_history_for_tools(messages),
        )
        if tool_reply is not None:
            return {"messages": [AIMessage(content=tool_reply)]}

        if state.get("memory_context"):
            messages.insert(
                1,
                SystemMessage(
                    content=(
                                "以下是经过隐私最小化处理后的用户长期记忆。"
                                "只能在回答当前问题时使用，不要主动复述这些隐私信息："
                                f"{state['memory_context']}"
                            )
                )
            )

        

        response = llm.invoke(messages)
        return {"messages": [response]}

    graph = StateGraph(ChatState)
    graph.add_node("retrieve_memory", retrieve_memory)
    graph.add_node("chatbot", chatbot)
    graph.set_entry_point("retrieve_memory")
    graph.add_edge("retrieve_memory", "chatbot")
    graph.set_finish_point("chatbot")
    return graph.compile()


def _message_history_for_tools(messages: list[BaseMessage]) -> list[dict[str, str]]:
    """把 LangChain 消息历史转换为工具可读格式。"""
    history: list[dict[str, str]] = []
    for message in messages:
        if isinstance(message, HumanMessage):
            history.append({"role": "user", "content": str(message.content)})
        elif isinstance(message, AIMessage):
            history.append({"role": "assistant", "content": str(message.content)})
    if history and history[-1]["role"] == "user":
        history = history[:-1]
    return history

    # 异步提取并存储，不阻塞用户
def async_store(conv):
    # 输入待抽取的用户消息列表；输出无返回值；作用是异步抽取并通过安全 vault 存储记忆。
    """异步抽取并存储当前用户消息中的长期记忆。"""
    if VAULT_SESSION is None:
        return

    try:
        memories = extract_memories(conv)
        if memories:
            secure_store_user_memories(VAULT_SESSION, str(VAULT_SESSION["user_id"]), memories)
    except Exception:
        logger.exception("Asynchronous memory storage failed")


def initialize_vault_session() -> None:
    # 输入环境变量中的用户配置；输出无返回值；作用是建立安全信道并初始化全局 vault session。
    """初始化本机开发 CLI 使用的 vault 安全会话。"""
    global VAULT_SESSION

    user_id = os.getenv("VAULT_USER_ID", "default_user")
    user_key = os.getenv("USER_MEMORY_KEY")
    logger.info(
        "准备初始化 vault 安全会话: user_id=%s user_key_source=%s",
        user_id,
        "env" if user_key else "temporary",
    )
    if not user_key:
        print("未设置 USER_MEMORY_KEY，本次运行将使用临时 per-user key；重启后无法读取旧记忆。")

    try:
        VAULT_SESSION = open_user_vault_session(user_id, user_key)
        logger.info("vault 安全会话初始化成功: user_id=%s session_id=%s", VAULT_SESSION["user_id"], VAULT_SESSION["session_id"][:8])
        print(f"Vault 安全信道已建立，当前 user_id: {VAULT_SESSION['user_id']}")
    except VaultApiError:
        VAULT_SESSION = None
        logger.exception("Vault secure session setup failed; continuing without long-term memory")
        print("Vault 安全信道建立失败，将以无长期记忆模式继续。")


def main():
    # 输入终端用户交互；输出无返回值；作用是运行命令行多轮聊天主循环。
    """执行命令行入口。"""
    if not os.getenv("DASHSCOPE_API_KEY"):
        raise EnvironmentError("Missing DASHSCOPE_API_KEY. Please export it before running.")

    logger.info("Chat app 启动，准备连接 vault")
    initialize_vault_session()
    app = build_graph()
    history: list[BaseMessage] = [SystemMessage(content=SYSTEM_PROMPT)]

    print("通义模型已准备就绪。 输入'exit'或'quit'可退出.")

    while True:
        user_input = input("\n用户: ").strip()
        if user_input.lower() in {"exit", "quit"}:
            print("再见！")
            break
        if not user_input:
            continue

        current_state = {
            "messages": history + [HumanMessage(content=user_input)],
            "user_input": user_input,
            "memory_context": "",
        }

        

        result = app.invoke(current_state)

        # 调试放这里才能看到 memory_context 是否被填充
        #print(f"memory_context: {repr(result.get('memory_context'))}")
        #print(f"messages 数量: {len(result['messages'])}")
        #for i, msg in enumerate(result['messages']):
        #    print(f"  [{i}] {msg.__class__.__name__}: {str(msg.content)[:100]}")

            
        reply = result["messages"][-1]

        if isinstance(reply, AIMessage):
            print(f"助手: {reply.content}")
        else:
            print(f"Assistant: {reply}")

        history.extend([HumanMessage(content=user_input), reply])
        threading.Thread(
            target=async_store,
            args=([HumanMessage(content=user_input)],),
            daemon=True
        ).start()

if __name__ == "__main__":
    main()
