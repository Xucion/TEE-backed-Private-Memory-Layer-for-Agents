import os
import json
import logging
import threading
import sys
from pathlib import Path
from typing import Annotated, TypedDict
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_community.chat_models import ChatTongyi
from langgraph.graph import StateGraph, add_messages

# Support running this file directly via `python untrusted/chat_app.py`.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from interface.vault_api import VaultApiError, retrieve_context, store_memories
from untrusted.memory_extractor import extract_memories


SYSTEM_PROMPT = "你是一位乐于助人的助手。回答请保持清晰简洁。"
logger = logging.getLogger(__name__)


class ChatState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    user_input: str
    memory_context: str


def retrieve_memory(state: ChatState) -> ChatState:
    query = state["user_input"]
    
    try:
        context = retrieve_context(query, top_k=3, threshold=0.4)
    except VaultApiError:
        logger.exception("Vault retrieval failed; continuing without memory context")
        context = ""
    return {"memory_context": context}



def build_graph():
    model_name = os.getenv("TONGYI_MODEL", "qwen-turbo")
    llm = ChatTongyi(model=model_name)

    def chatbot(state: ChatState) -> ChatState:
        messages = list(state["messages"])

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

    # 异步提取并存储，不阻塞用户
def async_store(conv):
    try:
        memories = extract_memories(conv)
        store_memories(memories)
    except Exception:
        logger.exception("Asynchronous memory storage failed")

def main():
    if not os.getenv("DASHSCOPE_API_KEY"):
        raise EnvironmentError("Missing DASHSCOPE_API_KEY. Please export it before running.")

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
