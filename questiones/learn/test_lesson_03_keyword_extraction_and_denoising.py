import json
import unittest
from types import SimpleNamespace

from lesson_03_keyword_extraction_and_denoising import (
    extract_keywords_and_denoise,
    parse_denoising_response,
)


class FakeTongyi:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.messages = []

    def invoke(self, messages):
        self.messages = messages
        return SimpleNamespace(
            content=json.dumps(self.payload, ensure_ascii=False)
        )


class KeywordDenoisingTests(unittest.TestCase):
    def test_removes_politeness_and_extracts_keywords(self) -> None:
        llm = FakeTongyi(
            {
                "cleaned_query": "公司报销需要准备哪些材料",
                "keywords": ["公司报销", "材料"],
                "removed_noise": ["麻烦帮我", "认真看看", "到底", "谢谢"],
                "preserved_constraints": [],
                "confidence": 0.98,
            }
        )

        result = extract_keywords_and_denoise(
            "麻烦帮我认真看看公司报销到底需要准备哪些材料，谢谢",
            llm=llm,
        )

        self.assertEqual(result.cleaned_query, "公司报销需要准备哪些材料")
        self.assertEqual(result.keywords, ["公司报销", "材料"])
        self.assertIn("麻烦帮我", result.removed_noise)
        self.assertIn("不要把否定词", llm.messages[0].content)

    def test_preserves_negation_version_and_time(self) -> None:
        llm = FakeTongyi(
            {
                "cleaned_query": "最近 30 天 Python 3.11 中不使用 Redis 的缓存实现",
                "keywords": ["Python 3.11", "Redis", "缓存实现"],
                "removed_noise": ["请帮我找"],
                "preserved_constraints": [
                    "最近 30 天",
                    "Python 3.11",
                    "不使用 Redis",
                ],
                "confidence": 1.2,
            }
        )

        result = extract_keywords_and_denoise(
            "请帮我找最近 30 天 Python 3.11 中不使用 Redis 的缓存实现",
            llm=llm,
        )

        self.assertIn("不使用 Redis", result.cleaned_query)
        self.assertEqual(
            result.preserved_constraints,
            ["最近 30 天", "Python 3.11", "不使用 Redis"],
        )
        self.assertEqual(result.confidence, 1.0)

    def test_rejects_empty_keywords(self) -> None:
        response = json.dumps(
            {
                "cleaned_query": "Redis 持久化",
                "keywords": [],
                "removed_noise": [],
                "preserved_constraints": [],
                "confidence": 0.8,
            },
            ensure_ascii=False,
        )

        with self.assertRaisesRegex(ValueError, "至少需要包含一个关键词"):
            parse_denoising_response(response, "Redis 持久化")


if __name__ == "__main__":
    unittest.main()
