import os
import json
from langchain_community.chat_models import ChatTongyi
from langchain_core.messages import HumanMessage, AIMessage, BaseMessage

EXTRACTOR_PROMPT = """
分析以下对话，提取值得长期记忆的用户信息。
只提取客观事实，不要推断，不要捏造。
如果没有值得记忆的信息，返回空列表。

输出严格遵循以下JSON格式，不要输出任何其他内容：
{{
  "memories": [
    {{
      "content": "记忆内容",
      "category": "health | preference | business | other",
      "sensitivity": "high | low"
    }}
  ]
}}

对话内容：
{conversation}
"""

def extract_memories(conversation: list[BaseMessage]) -> list[dict]:
    llm = ChatTongyi(model=os.getenv("TONGYI_MODEL", "qwen-turbo"))
    
    # 格式化对话
    conv_text = ""
    for msg in conversation:
        if isinstance(msg, HumanMessage):
            conv_text += f"用户: {msg.content}\n"
        elif isinstance(msg, AIMessage):
            conv_text += f"助手: {msg.content}\n"
    
    prompt = EXTRACTOR_PROMPT.format(conversation=conv_text)
    response = llm.invoke([HumanMessage(content=prompt)])
    
    try:
        text = response.content.strip()
        text = text.replace("```json", "").replace("```", "").strip()
        data = json.loads(text)
        
        # 兼容两种返回格式
        if isinstance(data, list):
            return data
        elif isinstance(data, dict):
            return data.get("memories", [])
        else:
            return []
    except json.JSONDecodeError:
        print(f"[Extractor] JSON解析失败: {response.content}")
        return []