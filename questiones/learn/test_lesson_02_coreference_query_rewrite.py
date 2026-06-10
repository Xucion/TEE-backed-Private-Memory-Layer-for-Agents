import json
import unittest
from types import SimpleNamespace

from lesson_02_coreference_query_rewrite import rewrite_query


class FakeTongyi:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.messages = []

    def invoke(self, messages):
        self.messages = messages
        return SimpleNamespace(
            content=json.dumps(self.payload, ensure_ascii=False)
        )


class QueryRewriteTests(unittest.TestCase):
    def test_resolves_pronoun_from_history(self) -> None:
        llm = FakeTongyi(
            {
                "standalone_query": "Redis 支持什么持久化方式？",
                "resolved_references": [
                    {"reference": "它", "resolved_to": "Redis"}
                ],
                "used_history": True,
                "confidence": 0.98,
                "needs_clarification": False,
                "clarification_question": "",
            }
        )

        result = rewrite_query(
            [
                {"role": "user", "content": "请介绍 Redis。"},
                {"role": "assistant", "content": "Redis 是内存数据存储。"},
            ],
            "它支持什么持久化方式？",
            llm=llm,
        )

        self.assertEqual(result.standalone_query, "Redis 支持什么持久化方式？")
        self.assertTrue(result.used_history)
        self.assertFalse(result.needs_clarification)
        self.assertIn("只能使用对话历史", llm.messages[0].content)
        self.assertIn(
            "需要澄清时，向用户提出的简短、具体问题",
            llm.messages[0].content,
        )
        self.assertIn(
            "needs_clarification 为 true 时",
            llm.messages[0].content,
        )

    def test_preserves_current_query_when_reference_is_ambiguous(self) -> None:
        llm = FakeTongyi(
            {
                "standalone_query": "它们有什么区别？",
                "resolved_references": [],
                "used_history": True,
                "confidence": 0.3,
                "needs_clarification": True,
                "clarification_question": "你想比较 Redis 和哪个数据库？",
            }
        )

        result = rewrite_query(
            [
                {"role": "user", "content": "我们讨论了 Redis、MySQL 和 PostgreSQL。"},
            ],
            "它们有什么区别？",
            llm=llm,
        )

        self.assertEqual(result.standalone_query, "它们有什么区别？")
        self.assertTrue(result.needs_clarification)
        self.assertEqual(
            result.clarification_question,
            "你想比较 Redis 和哪个数据库？",
        )


if __name__ == "__main__":
    unittest.main()
