import os
import json
import threading
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

memory_type 规则：
- preference：长期偏好，例如喜欢、不喜欢、偏好。
- profile：用户身份、背景、长期目标。
- health：健康限制或医疗相关事实。
- project：用户长期项目、代码库、研究方向。
- instruction：用户希望助手长期遵守的交互偏好。
- other：确实值得记忆但无法归类的事实。

结构化字段规则：
- subject 当前固定为 "user"。
- predicate 用英文小写 snake_case。
- object 使用短语，不要整句。
- value 用简单 JSON 值，例如 true、false、字符串或数字。
- confidence 只表示这条候选来自用户原话的明确程度，范围 0 到 1。
- slot 表示这条 memory 是否占用互斥事实槽位；可共存事实填 null。
- 当前支持的 slot 示例：
  - profile.current_city：当前居住城市。
  - profile.current_company：当前公司或组织。
  - profile.job_search_status：当前求职状态。
  - project.primary_project：当前主要项目。
  - instruction.response_language：当前回答语言偏好。
  - preference.like_dislike:对象：同一对象的喜欢/不喜欢互斥。
- 多目标、多项目、多个回答风格偏好通常可以共存，slot 应为 null。
- 不要从助手建议或模型推测中生成 memory。

输出严格遵循以下JSON格式，不要输出任何其他内容：
{{
  "memories": [
    {{
      "content": "规范化后的长期事实",
      "memory_type": "preference | profile | health | project | instruction | other",
      "sensitivity": "high | low",
      "subject": "user",
      "predicate": "likes | dislikes | has_goal | has_health_condition | prefers_response_style | works_on_project | lives_in | works_at | has_job_search_status | prefers_language | stated_fact",
      "object": "事实对象",
      "value": true,
      "slot": null,
      "confidence": 0.8,
      "source": "user"
    }}
  ]
}}

用户原话：
{conversation}
"""

VALID_SENSITIVITIES = {"high", "low"}
QUESTION_MARKERS = {"?", "？", "吗", "么", "什么", "怎么", "怎么办", "是否", "知道", "记得"}
REQUEST_MARKERS = {"帮我", "为我", "给我", "推荐", "建议", "做一下", "制定"}
META_MEMORY_MARKERS = {"你知道", "你记得", "我喜欢吃什么", "我喜欢什么"}
TRAILING_PARTICLES = ("呀", "啊", "吧", "呢", "哦", "啦", "哈")

VALID_MEMORY_TYPES = {
    "preference",
    "profile",
    "health",
    "project",
    "instruction",
    "other",
}
VALID_PREDICATES = {
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

def _canonicalize_content(content: str) -> str:
    # 输入原始记忆文本；输出第三人称规范化文本；作用是清理语气词并统一“用户”表述。
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
    # 输入规范化文本；输出是否像长期用户事实；作用是过滤问题、临时请求和元记忆询问。
    if any(marker in content for marker in META_MEMORY_MARKERS):
        return False
    if any(marker in content for marker in QUESTION_MARKERS):
        return False
    if any(marker in content for marker in REQUEST_MARKERS):
        return False
    return True


def _normalize_memories(data) -> list[dict]:
    # 输入 LLM JSON 数据；输出校验后的记忆列表；作用是规范字段并丢弃不可信候选。
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

        sensitivity = mem.get("sensitivity", "low")
        source = mem.get("source")
        memory_type = mem.get("memory_type", "other")
        subject = mem.get("subject", "user")
        predicate = mem.get("predicate", "stated_fact")
        object_value = str(mem.get("object", "")).strip()
        value = mem.get("value", True)
        slot = mem.get("slot")
        confidence = mem.get("confidence", 0.8)

        if sensitivity not in VALID_SENSITIVITIES:
            sensitivity = "low"
        if source != "user":
            continue
        if memory_type not in VALID_MEMORY_TYPES:
            memory_type = "other"
        if predicate not in VALID_PREDICATES:
            predicate = "stated_fact"
        if subject != "user":
            subject = "user"
        if slot is not None:
            slot = str(slot).strip() or None
        try:
            confidence = float(confidence)
        except (TypeError, ValueError):
            confidence = 0.8

        confidence = min(1.0, max(0.0, confidence))

        normalized.append(
            {
                "content": content,
                "memory_type": memory_type,
                "sensitivity": sensitivity,
                "subject": subject,
                "predicate": predicate,
                "object": object_value,
                "value": value,
                "slot": slot,
                "confidence": confidence,
                "source": "user",
            }
        )

    return normalized

_EXTRACTOR_LLM: ChatTongyi | None = None
_EXTRACTOR_LLM_LOCK = threading.Lock()


def _get_extractor_llm() -> ChatTongyi:
    # 输入环境中的模型配置；输出共享 extractor LLM；作用是避免模块导入时提前初始化外部客户端。
    global _EXTRACTOR_LLM
    if _EXTRACTOR_LLM is not None:
        return _EXTRACTOR_LLM

    with _EXTRACTOR_LLM_LOCK:
        if _EXTRACTOR_LLM is None:
            _EXTRACTOR_LLM = ChatTongyi(
                model=os.getenv("TONGYI_MODEL", "qwen-turbo")
            )
        return _EXTRACTOR_LLM


def extract_memories(conversation: list[BaseMessage]) -> list[dict]:
    # 输入对话消息列表；输出可存储记忆列表；作用是仅从用户消息中调用 LLM 抽取长期事实。

    
    # Only user-authored text is eligible for long-term memory extraction.
    user_lines = []
    for msg in conversation:
        if isinstance(msg, HumanMessage):
            user_lines.append(f"用户: {msg.content}")

    if not user_lines:
        return []
    
    prompt = EXTRACTOR_PROMPT.format(conversation="\n".join(user_lines))
    try:
        response = _get_extractor_llm().invoke([HumanMessage(content=prompt)])
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
