from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from langchain_community.chat_models import ChatTongyi
from langchain_core.messages import HumanMessage, SystemMessage


SYSTEM_PROMPT = """你是企业检索系统中的 Query Rewrite 组件。

你的任务是把“当前问题”改写成不依赖对话上下文也能理解的独立查询。

规则：
1. 只做指代消解和上下文补全，不要回答问题。
2. 只能使用对话历史和当前问题中明确出现的信息，禁止猜测。
3. 保留否定词、时间、数字、版本、产品名等关键约束。
4. 当前问题已经完整时，保持原意，不要增加无关关键词。
5. 如果存在多个可能的指代对象，不能确定时应要求澄清。
6. 只输出一个 JSON 对象，不要输出 Markdown。

字段说明：
- standalone_query：可以脱离历史独立理解的查询。需要澄清时，原样返回当前问题。
- resolved_references：本次完成的指代映射，例如“它”解析为“Redis”；没有则返回空数组。
- used_history：改写是否实际使用了对话历史中的信息。
- confidence：对改写正确性的置信度，范围为 0 到 1。
- needs_clarification：是否因为指代不明确或必要信息缺失而不能安全改写。
- clarification_question：需要澄清时，向用户提出的简短、具体问题；不需要澄清时必须为空字符串。

字段联动规则：
- needs_clarification 为 true 时，clarification_question 不能为空，并且 standalone_query 必须保留当前问题。
- needs_clarification 为 false 时，clarification_question 必须为 ""。
- 不要为了避免澄清而自行选择一个可能的指代对象。

JSON 格式：
{
  "standalone_query": "改写后的独立查询；需要澄清时保留当前问题",
  "resolved_references": [
    {"reference": "原指代表达", "resolved_to": "历史中的明确对象"}
  ],
  "used_history": true,
  "confidence": 0.0,
  "needs_clarification": false,
  "clarification_question": ""
}

示例一，指代明确：
历史：用户询问 Redis，助手介绍了 Redis。
当前问题：它支持什么持久化方式？
输出：
{
  "standalone_query": "Redis 支持什么持久化方式？",
  "resolved_references": [
    {"reference": "它", "resolved_to": "Redis"}
  ],
  "used_history": true,
  "confidence": 0.98,
  "needs_clarification": false,
  "clarification_question": ""
}

示例二，指代不明确：
历史：用户同时讨论了 Redis、MySQL 和 PostgreSQL。
当前问题：它有什么优点？
输出：
{
  "standalone_query": "它有什么优点？",
  "resolved_references": [],
  "used_history": true,
  "confidence": 0.2,
  "needs_clarification": true,
  "clarification_question": "你想了解 Redis、MySQL 还是 PostgreSQL 的优点？"
}
"""


class ChatModel(Protocol):
    def invoke(self, messages: list[Any]) -> Any:
        """调用聊天模型。"""


@dataclass(frozen=True)
class RewriteResult:
    standalone_query: str
    resolved_references: list[dict[str, str]]
    used_history: bool
    confidence: float
    needs_clarification: bool
    clarification_question: str


def format_history(history: list[dict[str, str]], max_messages: int = 8) -> str:
    """清洗并格式化最近的对话，限制发送给模型的上下文长度。"""
    lines: list[str] = []
    role_names = {"user": "用户", "assistant": "助手"}

    for item in history[-max_messages:]:
        role = item.get("role")
        content = str(item.get("content") or "").strip()
        if role not in role_names or not content:
            continue
        lines.append(f"{role_names[role]}：{content}")

    return "\n".join(lines) if lines else "（无对话历史）"


def build_rewrite_prompt(history: list[dict[str, str]], current_query: str) -> str:
    """构造一次 Query Rewrite 请求。"""
    query = str(current_query or "").strip()
    if not query:
        raise ValueError("current_query 不能为空")

    return (
        "请根据下面的信息改写当前问题。\n\n"
        f"对话历史：\n{format_history(history)}\n\n"
        f"当前问题：\n{query}"
    )


def parse_rewrite_response(text: str, current_query: str) -> RewriteResult:
    """解析并校验模型返回的 JSON，阻止不完整结果进入检索链路。"""
    cleaned = str(text or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not match:
            raise ValueError("模型没有返回可解析的 JSON") from None
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise ValueError("模型返回的 JSON 格式无效") from exc

    if not isinstance(payload, dict):
        raise ValueError("模型返回值必须是 JSON 对象")

    standalone_query = str(payload.get("standalone_query") or "").strip()
    needs_clarification = bool(payload.get("needs_clarification", False))
    clarification_question = str(payload.get("clarification_question") or "").strip()

    if not standalone_query:
        raise ValueError("standalone_query 不能为空")
    if needs_clarification and not clarification_question:
        raise ValueError("需要澄清时必须提供 clarification_question")

    raw_references = payload.get("resolved_references", [])
    if not isinstance(raw_references, list):
        raise ValueError("resolved_references 必须是数组")

    resolved_references: list[dict[str, str]] = []
    for item in raw_references:
        if not isinstance(item, dict):
            continue
        reference = str(item.get("reference") or "").strip()
        resolved_to = str(item.get("resolved_to") or "").strip()
        if reference and resolved_to:
            resolved_references.append(
                {"reference": reference, "resolved_to": resolved_to}
            )

    try:
        confidence = float(payload.get("confidence", 0.0))
    except (TypeError, ValueError) as exc:
        raise ValueError("confidence 必须是数字") from exc

    return RewriteResult(
        standalone_query=current_query.strip() if needs_clarification else standalone_query,
        resolved_references=resolved_references,
        used_history=bool(payload.get("used_history", False)),
        confidence=max(0.0, min(confidence, 1.0)),
        needs_clarification=needs_clarification,
        clarification_question=clarification_question,
    )


def create_tongyi_model(model_name: str | None = None) -> ChatTongyi:
    """创建通义千问模型，密钥由 DashScope SDK 从环境变量读取。"""
    if not os.getenv("DASHSCOPE_API_KEY", "").strip():
        raise EnvironmentError("请先设置环境变量 DASHSCOPE_API_KEY")

    return ChatTongyi(
        model=model_name or os.getenv("TONGYI_MODEL", "qwen-turbo"),
        temperature=0,
    )


def rewrite_query(
    history: list[dict[str, str]],
    current_query: str,
    *,
    llm: ChatModel | None = None,
    model_name: str | None = None,
) -> RewriteResult:
    """使用 Tongyi 将上下文相关问题改写为独立查询。"""
    model = llm or create_tongyi_model(model_name)
    messages = [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=build_rewrite_prompt(history, current_query)),
    ]
    response = model.invoke(messages)
    content = getattr(response, "content", response)
    if not isinstance(content, str):
        raise TypeError("模型响应 content 必须是字符串")
    return parse_rewrite_response(content, current_query)


def main() -> None:
    parser = argparse.ArgumentParser(description="学习：指代消解与上下文补全")
    parser.add_argument(
        "--query",
        default="它支持什么持久化方式？",
        help="当前需要改写的问题",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Tongyi 模型名，默认读取 TONGYI_MODEL 或使用 qwen-turbo",
    )
    args = parser.parse_args()

    history = [
        {"role": "user", "content": "请介绍一下 Redis。"},
        {
            "role": "assistant",
            "content": "Redis 是一个内存数据存储，可用作缓存、数据库和消息中间件。",
        },
    ]

    print("对话历史：")
    print(format_history(history))
    print(f"\n当前问题：{args.query}")

    result = rewrite_query(history, args.query, model_name=args.model)

    print("\n改写结果：")
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))

    if result.needs_clarification:
        print(f"\n下一步：向用户提问：{result.clarification_question}")
    else:
        print(f"\n下一步：使用该 Query 检索：{result.standalone_query}")


if __name__ == "__main__":
    main()
