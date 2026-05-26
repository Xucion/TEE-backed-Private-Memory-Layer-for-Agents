import os
import json
from langchain_community.chat_models import ChatTongyi
from langchain_core.messages import HumanMessage, BaseMessage

EXTRACTOR_PROMPT = """
分析以下用户原话，提取值得长期记忆的用户信息。
只从“用户原话”中提取客观事实或明确偏好，不要从助手回复、建议、推断或临时任务中提取。
不要保存一次性请求，例如“帮我推荐饮食”“我想喝粥”，除非用户明确表达这是长期偏好。
如果没有值得记忆的信息，返回空列表。
content 字段必须是规范化后的稳定事实：
- 使用第三人称“用户...”陈述，不要使用“我...”。
- 去掉“呀、啊、吧、呢、哦、啦”等语气词。
- 只做用户原话支持的轻量规范化，不要补充用户没有表达的新事实。
- 例如“我喜欢喝粥呀”可输出为“用户喜欢喝粥”；如果用户只说“我有糖尿”，不要擅自扩写为确诊糖尿病以外的信息。

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
QUESTION_MARKERS = {"?", "？", "吗", "么", "什么", "怎么", "怎么办", "是否", "知道", "记得"}
REQUEST_MARKERS = {"帮我", "为我", "给我", "推荐", "建议", "做一下", "制定"}
META_MEMORY_MARKERS = {"你知道", "你记得", "我喜欢吃什么", "我喜欢什么"}
TRAILING_PARTICLES = ("呀", "啊", "吧", "呢", "哦", "啦", "哈")


def _canonicalize_content(content: str) -> str:
    content = content.strip()
    while content.endswith(TRAILING_PARTICLES):
        content = content[:-1].strip()

    if content.startswith("我的"):
        return "用户的" + content[len("我的"):]
    if content.startswith("我"):
        return "用户" + content[len("我"):]

    if not content.startswith("用户"):
        return f"用户{content}"
    return content


def _looks_like_user_fact(content: str) -> bool:
    if any(marker in content for marker in META_MEMORY_MARKERS):
        return False
    if any(marker in content for marker in QUESTION_MARKERS):
        return False
    if any(marker in content for marker in REQUEST_MARKERS):
        return False
    return True


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
        content = _canonicalize_content(content)
        if not _looks_like_user_fact(content):
            continue

        category = mem.get("category", "other")
        sensitivity = mem.get("sensitivity", "low")
        source = mem.get("source")

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

llm = ChatTongyi(model=os.getenv("TONGYI_MODEL", "qwen-turbo"))
def extract_memories(conversation: list[BaseMessage]) -> list[dict]:

    
    # Only user-authored text is eligible for long-term memory extraction.
    user_lines = []
    for msg in conversation:
        if isinstance(msg, HumanMessage):
            user_lines.append(f"用户: {msg.content}")

    if not user_lines:
        return []
    
    prompt = EXTRACTOR_PROMPT.format(conversation="\n".join(user_lines))
    try:
        response = llm.invoke([HumanMessage(content=prompt)])
        text = response.content.strip()
        text = text.replace("```json", "").replace("```", "").strip()
        data = json.loads(text)
        
        return _normalize_memories(data)
    except json.JSONDecodeError:
        print(f"[Extractor] JSON解析失败: {response.content}")
        return []
    except Exception as exc:
        print(f"[Extractor] 记忆提取失败: {exc}")
        return []
