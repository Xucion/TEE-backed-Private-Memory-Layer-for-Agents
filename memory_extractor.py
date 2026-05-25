import os
import json
from langchain_community.chat_models import ChatTongyi
from langchain_core.messages import HumanMessage, BaseMessage

EXTRACTOR_PROMPT = """
分析以下用户原话，提取值得长期记忆的用户信息。
只从“用户原话”中提取客观事实或明确偏好，不要从助手回复、建议、推断或临时任务中提取。
不要保存一次性请求，例如“帮我推荐饮食”“我想喝粥”，除非用户明确表达这是长期偏好。
如果没有值得记忆的信息，返回空列表。

输出严格遵循以下JSON格式，不要输出任何其他内容：
{{
  "memories": [
    {{
      "content": "记忆内容",
      "category": "health | preference | business | other",
      "sensitivity": "high | low",
      "source": "user"
    }}
  ]
}}

用户原话：
{conversation}
"""

VALID_CATEGORIES = {"health", "preference", "business", "other"}
VALID_SENSITIVITIES = {"high", "low"}


def _normalize_memories(data) -> list[dict]:
    if isinstance(data, dict):
        memories = data.get("memories", [])
    elif isinstance(data, list):
        memories = data
    else:
        return []

    normalized = []
    for mem in memories:
        if not isinstance(mem, dict):
            continue

        content = str(mem.get("content", "")).strip()
        if not content:
            continue

        category = mem.get("category", "other")
        sensitivity = mem.get("sensitivity", "low")
        source = mem.get("source", "user")

        if category not in VALID_CATEGORIES:
            category = "other"
        if sensitivity not in VALID_SENSITIVITIES:
            sensitivity = "low"
        if source != "user":
            continue

        normalized.append(
            {
                "content": content,
                "category": category,
                "sensitivity": sensitivity,
            }
        )

    return normalized

def extract_memories(conversation: list[BaseMessage]) -> list[dict]:
    llm = ChatTongyi(model=os.getenv("TONGYI_MODEL", "qwen-turbo"))
    
    # Only user-authored text is eligible for long-term memory extraction.
    user_lines = []
    for msg in conversation:
        if isinstance(msg, HumanMessage):
            user_lines.append(f"用户: {msg.content}")

    if not user_lines:
        return []
    
    prompt = EXTRACTOR_PROMPT.format(conversation="\n".join(user_lines))
    response = llm.invoke([HumanMessage(content=prompt)])
    
    try:
        text = response.content.strip()
        text = text.replace("```json", "").replace("```", "").strip()
        data = json.loads(text)
        
        return _normalize_memories(data)
    except json.JSONDecodeError:
        print(f"[Extractor] JSON解析失败: {response.content}")
        return []
