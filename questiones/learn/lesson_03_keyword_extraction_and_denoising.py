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

你的任务是从用户查询中提取检索关键词，并删除不影响检索意图的口语噪声。
你只改写查询，不回答用户问题。

可以删除的内容：
- 礼貌用语，例如“麻烦”“请帮我”“谢谢”。
- 无检索意义的语气和情绪，例如“认真看看”“到底”“急死我了”。
- 重复表达和不影响检索条件的冗余描述。

必须保留的内容：
- 核心实体、产品名、技术名词和业务术语。
- 否定和排除条件，例如“不”“不要”“不能”“排除”“除了”。
- 时间、日期、版本号、数字、单位和范围。
- 比较关系、排序要求、地域、状态等过滤条件。
- 用户原查询中明确出现的限制，禁止自行补充不存在的条件。

字段说明：
- cleaned_query：去噪后仍保持原始检索意图的简洁查询，不是关键词的机械拼接。
- keywords：适合 BM25、倒排索引或混合检索的核心词组，按重要性排序。
- removed_noise：从原查询中删除的无检索意义片段；没有则返回空数组。
- preserved_constraints：必须保留的限制条件，例如“不使用云服务”“Python 3.11”“最近 30 天”。
- confidence：对本次提取和去噪正确性的置信度，范围为 0 到 1。

规则：
1. cleaned_query 不能为空。
2. keywords 只包含原查询中的概念，不生成同义词或扩展词。
3. 不要把否定词从 cleaned_query 中删除。
4. removed_noise 只能记录实际删除的内容，不能包含仍在 cleaned_query 中的核心条件。
5. 没有明显噪声时，cleaned_query 可以与原查询相同。
6. 只输出一个 JSON 对象，不要输出 Markdown 或解释。

JSON 格式：
{
  "cleaned_query": "去噪后的查询",
  "keywords": ["关键词1", "关键词2"],
  "removed_noise": ["删除的噪声片段"],
  "preserved_constraints": ["保留的关键限制"],
  "confidence": 0.0
}

示例一，删除礼貌和情绪噪声：
原查询：麻烦帮我认真看看公司报销到底需要准备哪些材料，谢谢
输出：
{
  "cleaned_query": "公司报销需要准备哪些材料",
  "keywords": ["公司报销", "材料"],
  "removed_noise": ["麻烦帮我", "认真看看", "到底", "谢谢"],
  "preserved_constraints": [],
  "confidence": 0.98
}

示例二，保留否定、版本和时间条件：
原查询：请帮我找最近 30 天 Python 3.11 中不使用 Redis 的缓存实现
输出：
{
  "cleaned_query": "最近 30 天 Python 3.11 中不使用 Redis 的缓存实现",
  "keywords": ["Python 3.11", "Redis", "缓存实现"],
  "removed_noise": ["请帮我找"],
  "preserved_constraints": ["最近 30 天", "Python 3.11", "不使用 Redis"],
  "confidence": 0.97
}
"""


class ChatModel(Protocol):
    def invoke(self, messages: list[Any]) -> Any:
        """调用聊天模型。"""


@dataclass(frozen=True)
class KeywordRewriteResult:
    original_query: str
    cleaned_query: str
    keywords: list[str]
    removed_noise: list[str]
    preserved_constraints: list[str]
    confidence: float


def build_denoising_prompt(query: str) -> str:
    """构造关键词提取与去噪请求。"""
    normalized_query = str(query or "").strip()
    if not normalized_query:
        raise ValueError("query 不能为空")
    return f"请处理下面的原查询：\n\n原查询：{normalized_query}"


def _parse_json_object(text: str) -> dict[str, Any]:
    """从纯 JSON 或 Markdown 代码块中解析一个 JSON 对象。"""
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
    return payload


def _string_list(payload: dict[str, Any], field: str) -> list[str]:
    """读取字符串数组，并清理空值和重复值。"""
    raw_items = payload.get(field, [])
    if not isinstance(raw_items, list):
        raise ValueError(f"{field} 必须是数组")

    result: list[str] = []
    for item in raw_items:
        value = str(item or "").strip()
        if value and value not in result:
            result.append(value)
    return result


def parse_denoising_response(text: str, original_query: str) -> KeywordRewriteResult:
    """解析并校验模型输出。"""
    payload = _parse_json_object(text)
    cleaned_query = str(payload.get("cleaned_query") or "").strip()
    if not cleaned_query:
        raise ValueError("cleaned_query 不能为空")

    keywords = _string_list(payload, "keywords")
    removed_noise = _string_list(payload, "removed_noise")
    preserved_constraints = _string_list(payload, "preserved_constraints")

    if not keywords:
        raise ValueError("keywords 至少需要包含一个关键词")

    try:
        confidence = float(payload.get("confidence", 0.0))
    except (TypeError, ValueError) as exc:
        raise ValueError("confidence 必须是数字") from exc

    return KeywordRewriteResult(
        original_query=original_query.strip(),
        cleaned_query=cleaned_query,
        keywords=keywords,
        removed_noise=removed_noise,
        preserved_constraints=preserved_constraints,
        confidence=max(0.0, min(confidence, 1.0)),
    )


def create_tongyi_model(model_name: str | None = None) -> ChatTongyi:
    """创建通义千问模型。"""
    if not os.getenv("DASHSCOPE_API_KEY", "").strip():
        raise EnvironmentError("请先设置环境变量 DASHSCOPE_API_KEY")

    return ChatTongyi(
        model=model_name or os.getenv("TONGYI_MODEL", "qwen-turbo"),
        temperature=0,
    )


def extract_keywords_and_denoise(
    query: str,
    *,
    llm: ChatModel | None = None,
    model_name: str | None = None,
) -> KeywordRewriteResult:
    """调用 Tongyi 提取关键词并删除查询噪声。"""
    normalized_query = str(query or "").strip()
    model = llm or create_tongyi_model(model_name)
    messages = [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=build_denoising_prompt(normalized_query)),
    ]

    response = model.invoke(messages)
    content = getattr(response, "content", response)
    if not isinstance(content, str):
        raise TypeError("模型响应 content 必须是字符串")
    return parse_denoising_response(content, normalized_query)


def main() -> None:
    parser = argparse.ArgumentParser(description="学习：关键词提取与 Query 去噪")
    parser.add_argument(
        "--query",
        default="麻烦帮我认真看看公司报销到底需要准备哪些材料，谢谢",
        help="需要处理的原始查询",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Tongyi 模型名，默认读取 TONGYI_MODEL 或使用 qwen-turbo",
    )
    args = parser.parse_args()

    result = extract_keywords_and_denoise(
        args.query,
        model_name=args.model,
    )

    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    print(f"\n向量检索 Query：{result.cleaned_query}")
    print(f"关键词检索 Query：{' '.join(result.keywords)}")


if __name__ == "__main__":
    main()
