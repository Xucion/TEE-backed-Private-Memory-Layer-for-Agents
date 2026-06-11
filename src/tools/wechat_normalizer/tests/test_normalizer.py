from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[3]
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from tools.wechat_normalizer.llm_contract import (
    build_extraction_payload,
    build_weekly_summary_payload,
)
from tools.wechat_normalizer.activity_extractor import (
    build_group_payload,
    extract_activities_from_jsonl,
    iter_group_payloads,
    normalize_activity_response,
)
from tools.wechat_normalizer.activity_summary import (
    build_activity_summary,
    merge_activities,
)
from tools.wechat_normalizer.summary_renderer import render_summary_html
from tools.build_wechat_activity_report import build_report
from tools.wechat_normalizer.media import inspect_media
from tools.wechat_normalizer.normalizer import (
    _activity_features,
    _sanitize_url,
    normalize_export,
)
from tools.wechat_normalizer.preferences import (
    profile_from_memories,
    score_for_profile,
)
from tools.wechat_normalizer.wechat_export_api import (
    WeChatExportApiError,
    WeChatExportResult,
    WECHAT_EXPORT_TOOL_SCHEMA,
    call_wechat_export_tool,
    extract_zip_safely,
)


TOOLS_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_EXPORT = TOOLS_ROOT / "wechatOutput" / "wechat_chat_xunxu_2026-06-07_json"


class WeChatNormalizerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        """为全部测试预先执行一次样例导出标准化。"""
        cls.result = normalize_export(SAMPLE_EXPORT)

    def test_sample_counts_and_types(self) -> None:
        """验证样例消息、转发子项和媒体的统计数量。"""
        report = self.result.report
        self.assertEqual(report.source_messages, 7)
        self.assertEqual(report.forwarded_messages_expanded, 0)
        self.assertEqual(report.normalized_messages, 7)
        self.assertEqual(report.local_media_found, 3)
        self.assertEqual(report.media_not_analyzed, 3)
        self.assertEqual(report.missing_media, 0)
        self.assertEqual(report.context_groups, 3)
        self.assertEqual(report.unknown_types, 0)
        self.assertEqual(report.message_types["link"], 1)
        self.assertEqual(report.message_types["image"], 3)

    def test_output_does_not_copy_internal_identifiers(self) -> None:
        """验证规范化输出不会泄露微信内部账号和文件标识。"""
        serialized = "\n".join(
            json.dumps(message.to_dict(), ensure_ascii=False)
            for message in self.result.messages
        )
        self.assertNotIn("wxid_", serialized)
        self.assertNotIn("fileId", serialized)
        self.assertNotIn("cdnthumburl", serialized)

    def test_sensitive_link_ticket_is_redacted(self) -> None:
        """验证邀请链接中的敏感 ticket 参数已被遮蔽。"""
        sanitized, host = _sanitize_url(
            "https://support.weixin.qq.com/a?ticket=secret&utm_source=x&ok=1"
        )
        self.assertEqual(host, "support.weixin.qq.com")
        self.assertEqual(
            sanitized,
            "https://support.weixin.qq.com/a?ticket=%5BREDACTED%5D&ok=1",
        )

    def test_media_dimensions_without_content_analysis(self) -> None:
        """验证本地图片只记录尺寸而不分析内容。"""
        local_media = [
            attachment
            for message in self.result.messages
            for attachment in message.media
            if attachment.relative_path
        ]
        self.assertEqual(len(local_media), 3)
        self.assertTrue(all(item.width and item.height for item in local_media))
        self.assertTrue(
            all(item.analysis_status == "not_analyzed" for item in local_media)
        )
        self.assertTrue(all(item.parser_hint is None for item in local_media))

    def test_result_is_deterministic(self) -> None:
        """验证相同输入重复标准化会得到完全一致的输出。"""
        second = normalize_export(SAMPLE_EXPORT)
        first_json = [
            json.dumps(message.to_dict(), ensure_ascii=False, sort_keys=True)
            for message in self.result.messages
        ]
        second_json = [
            json.dumps(message.to_dict(), ensure_ascii=False, sort_keys=True)
            for message in second.messages
        ]
        self.assertEqual(first_json, second_json)

    def test_current_sample_has_no_forwarded_items(self) -> None:
        """验证转发子消息同时保留原始时间和进入当前会话的时间。"""
        forwarded = [
            message
            for message in self.result.messages
            if message.source_kind == "forwarded_item"
        ]
        self.assertEqual(forwarded, [])

    def test_personalization_keeps_mandatory_only_constraint(self) -> None:
        """验证仅关注强制任务的偏好会抑制可选活动推荐。"""
        profile = profile_from_memories(
            [
                "我喜欢参加娱乐活动",
                "我关心参加开展的比赛",
                "我只需要知道必须完成的任务，其他活动不关心。",
            ]
        )
        optional_competition = score_for_profile(
            {
                "candidate_score": 0.5,
                "tags": ["competition"],
                "mandatory_signal": False,
            },
            profile,
        )
        required_task = score_for_profile(
            {
                "candidate_score": 0.4,
                "tags": ["administrative"],
                "mandatory_signal": True,
            },
            profile,
        )
        self.assertFalse(optional_competition["recommended"])
        self.assertTrue(required_task["recommended"])

    def test_candidate_scoring_prefers_generic_structure_over_category_words(self) -> None:
        """领域类别词只提供弱信号，通用动作和指令结构决定候选性。"""
        category_only = _activity_features("篮球", "text", False)
        structured = _activity_features(
            "请各位在明天前填写相关信息。",
            "text",
            False,
        )
        self.assertLess(category_only["candidate_score"], 0.3)
        self.assertGreaterEqual(structured["candidate_score"], 0.3)
        self.assertTrue(structured["action_signal"])
        self.assertTrue(structured["mandatory_signal"])

    def test_llm_payload_contains_only_required_message_fields(self) -> None:
        """验证逐条 LLM 请求包含提取所需的最小消息字段。"""
        payload = build_extraction_payload(self.result.messages[0].to_dict())
        self.assertIn("instruction", payload)
        self.assertIn("output_schema", payload)
        self.assertEqual(
            payload["message"]["message_id"],
            self.result.messages[0].message_id,
        )
        self.assertIn("llm_text", payload["message"])

    def test_weekly_summary_payload_accepts_preference_memories(self) -> None:
        """验证周报请求能够携带最小化后的用户偏好记忆。"""
        payload = build_weekly_summary_payload(
            [{"title": "示例比赛", "mandatory": False}],
            week_start="2026-06-01",
            week_end="2026-06-07",
            preference_memories=["我关心参加开展的比赛"],
        )
        self.assertEqual(payload["week"]["start"], "2026-06-01")
        self.assertEqual(payload["preference_memories"], ["我关心参加开展的比赛"])
        self.assertIn("mandatory_tasks", payload["output_schema"])

    def test_media_path_traversal_is_rejected(self) -> None:
        """验证媒体解析会拒绝逃逸导出根目录的路径。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            attachment = inspect_media(
                Path(temp_dir),
                "../outside.jpg",
                "image",
            )
        self.assertEqual(attachment.analysis_status, "failed")
        self.assertIn("escapes export directory", attachment.warnings[0])

    def test_images_share_time_group_with_nearby_messages(self) -> None:
        """验证图片依靠时间分组与相邻文本建立上下文关系。"""
        image_messages = [
            message
            for message in self.result.messages
            if message.message_type == "image" and message.media
        ]
        text_groups = {
            message.context_group_id
            for message in self.result.messages
            if message.message_type == "text"
        }
        self.assertTrue(image_messages)
        self.assertTrue(
            all(message.context_group_id in text_groups for message in image_messages)
        )
        self.assertTrue(
            all(
                "不分析图片内容" in message.llm_text
                for message in image_messages
                if message.media[0].analysis_status == "not_analyzed"
            )
        )

    def test_group_activity_payload_carries_related_images(self) -> None:
        """验证 group_activity_payload_carries_related_images 的行为符合预期。"""
        group_id = next(
            message.context_group_id
            for message in self.result.messages
            if message.message_type == "image"
        )
        group_records = [
            message.to_dict()
            for message in self.result.messages
            if message.context_group_id == group_id
        ]
        payload = build_group_payload(group_id, group_records)
        self.assertTrue(payload["messages"])
        self.assertTrue(payload["images"])
        self.assertIn("不要分析图片内容", payload["instruction"])

    def test_activity_response_normalization_keeps_evidence_and_images(self) -> None:
        """验证 activity_response_normalization_keeps_evidence_and_images 的行为符合预期。"""
        group_id = next(
            message.context_group_id
            for message in self.result.messages
            if message.message_type == "image"
        )
        group_records = [
            message.to_dict()
            for message in self.result.messages
            if message.context_group_id == group_id
        ]
        payload = build_group_payload(group_id, group_records)
        evidence_id = payload["messages"][0]["message_id"]
        image_id = payload["images"][0]["message_id"]
        activities = normalize_activity_response(
            {
                "activities": [
                    {
                        "title": "空调折旧费缴纳",
                        "kind": "mandatory_task",
                        "mandatory": True,
                        "deadline": None,
                        "location": None,
                        "required_action": "扫码缴纳空调折旧费",
                        "confidence": 0.8,
                        "evidence_message_ids": [evidence_id],
                        "evidence_quote": payload["messages"][0]["text"][:40],
                        "related_image_message_ids": [image_id],
                    }
                ]
            },
            payload,
        )
        self.assertEqual(len(activities), 1)
        self.assertEqual(activities[0]["evidence_message_ids"], [evidence_id])
        self.assertEqual(activities[0]["related_images"][0]["message_id"], image_id)
        self.assertGreaterEqual(
            activities[0]["related_images"][0]["association_confidence"],
            0.55,
        )
        self.assertTrue(
            activities[0]["related_images"][0]["association_reason"]
        )
        self.assertIn(
            activities[0]["related_images"][0]["association_role"],
            {
                "registration_qr",
                "form_or_document",
                "poster",
                "supporting_image",
                "unresolved",
            },
        )

    def test_candidate_windows_are_bounded_during_continuous_chat(self) -> None:
        """连续聊天中的候选窗口不能因重叠而无限扩大。"""
        records = []
        for index in range(30):
            records.append(
                {
                    "message_id": f"msg_{index}",
                    "conversation_id": "conv_1",
                    "context_group_id": "group_1",
                    "occurred_at": f"2026-06-07T10:{index:02d}:00+08:00",
                    "occurred_at_local": f"2026-06-07T10:{index:02d}:00+08:00",
                    "source_index": index,
                    "message_type": "text",
                    "text": f"连续聊天消息 {index}",
                    "activity_features": {
                        "candidate_score": 0.8 if index % 4 == 0 else 0.0,
                    },
                    "media": [],
                }
            )
        payloads = list(iter_group_payloads(records))
        self.assertGreater(len(payloads), 1)
        self.assertTrue(
            all(len(payload["messages"]) <= 13 for payload in payloads)
        )

    def test_candidate_window_keeps_distant_nearby_context(self) -> None:
        """候选事项可以召回超出旧五分钟分组但仍在有界窗口内的补充。"""
        records = [
            {
                "message_id": "location",
                "conversation_id": "conv_1",
                "context_group_id": "group_1",
                "occurred_at": "2026-06-07T10:00:00+08:00",
                "occurred_at_local": "2026-06-07T10:00:00+08:00",
                "source_index": 1,
                "message_type": "text",
                "text": "大家交到这里。",
                "activity_features": {"candidate_score": 0.0},
                "media": [],
            },
            {
                "message_id": "deadline",
                "conversation_id": "conv_1",
                "context_group_id": "group_2",
                "occurred_at": "2026-06-07T10:17:00+08:00",
                "occurred_at_local": "2026-06-07T10:17:00+08:00",
                "source_index": 2,
                "message_type": "text",
                "text": "请在今天下午三点前提交。",
                "activity_features": {"candidate_score": 0.8},
                "media": [],
            },
        ]
        payload = list(iter_group_payloads(records))[0]
        self.assertEqual(
            [message["message_id"] for message in payload["messages"]],
            ["location", "deadline"],
        )
        self.assertEqual(
            payload["source_context_group_ids"],
            ["group_1", "group_2"],
        )

    def test_adjacent_ranges_do_not_merge_across_large_time_gap(self) -> None:
        """索引相邻但时间相隔很远的候选消息必须属于不同窗口。"""
        records = [
            {
                "message_id": "day_1",
                "conversation_id": "conv_1",
                "context_group_id": "group_1",
                "occurred_at": "2026-06-07T10:00:00+08:00",
                "occurred_at_local": "2026-06-07T10:00:00+08:00",
                "source_index": 1,
                "message_type": "text",
                "text": "请填写信息。",
                "activity_features": {"candidate_score": 0.8},
                "media": [],
            },
            {
                "message_id": "day_2",
                "conversation_id": "conv_1",
                "context_group_id": "group_2",
                "occurred_at": "2026-06-08T10:00:00+08:00",
                "occurred_at_local": "2026-06-08T10:00:00+08:00",
                "source_index": 2,
                "message_type": "text",
                "text": "请提交材料。",
                "activity_features": {"candidate_score": 0.8},
                "media": [],
            },
        ]
        payloads = list(iter_group_payloads(records))
        self.assertEqual(len(payloads), 2)
        self.assertEqual(
            [[message["message_id"] for message in payload["messages"]] for payload in payloads],
            [["day_1"], ["day_2"]],
        )

    def test_activity_response_does_not_attach_distant_group_images(self) -> None:
        """粗粒度上下文组中的图片仍必须接近证据消息。"""
        payload = {
            "context_group_id": "group_test",
            "messages": [
                {
                    "message_id": "text_1",
                    "occurred_at_local": "2026-06-07T22:36:58+08:00",
                    "text": "请提交医保报销材料。",
                    "title": None,
                    "url": None,
                    "llm_text": "",
                }
            ],
            "images": [
                {
                    "message_id": "image_1",
                    "occurred_at_local": "2026-06-07T22:38:24+08:00",
                    "relative_path": "media/images/image_1.jpg",
                }
            ],
        }
        activities = normalize_activity_response(
            {
                "activities": [
                    {
                        "title": "医保报销材料提交",
                        "evidence_message_ids": ["text_1"],
                        "evidence_quote": "请提交医保报销材料",
                        "related_image_message_ids": ["image_1"],
                    }
                ]
            },
            payload,
        )
        self.assertEqual(activities[0]["related_images"], [])

    def test_activity_response_infers_only_nearby_images(self) -> None:
        """缺少图片 ID 时只能从短时间窗口内推断附件。"""
        payload = {
            "context_group_id": "group_test",
            "messages": [
                {
                    "message_id": "text_1",
                    "occurred_at_local": "2026-06-07T22:38:24+08:00",
                    "text": "点击二维码缴纳空调折旧费。",
                    "title": None,
                    "url": None,
                    "llm_text": "",
                }
            ],
            "images": [
                {
                    "message_id": "near_image",
                    "occurred_at_local": "2026-06-07T22:38:25+08:00",
                    "relative_path": "media/images/near.jpg",
                },
                {
                    "message_id": "far_image",
                    "occurred_at_local": "2026-06-07T22:40:50+08:00",
                    "relative_path": "media/images/far.jpg",
                },
            ],
        }
        activities = normalize_activity_response(
            {
                "activities": [
                    {
                        "title": "空调折旧费缴纳",
                        "evidence_message_ids": ["text_1"],
                        "evidence_quote": "点击二维码缴纳空调折旧费",
                    }
                ]
            },
            payload,
        )
        self.assertEqual(
            [image["message_id"] for image in activities[0]["related_images"]],
            ["near_image"],
        )

    def test_image_reference_can_extend_association_window(self) -> None:
        """明确媒体引用可在消息距离很近时扩展到数分钟，而非固定三十秒。"""
        payload = {
            "context_group_id": "group_test",
            "messages": [
                {
                    "message_id": "text_1",
                    "context_order": 1,
                    "sender_id": "sender_1",
                    "occurred_at_local": "2026-06-07T10:08:00+08:00",
                    "text": "具体操作见上面的附件。",
                    "title": None,
                    "url": None,
                    "llm_text": "",
                }
            ],
            "images": [
                {
                    "message_id": "image_1",
                    "context_order": 0,
                    "sender_id": "sender_1",
                    "occurred_at_local": "2026-06-07T10:00:00+08:00",
                    "relative_path": "media/images/image_1.jpg",
                }
            ],
        }
        activities = normalize_activity_response(
            {
                "activities": [
                    {
                        "title": "操作通知",
                        "evidence_message_ids": ["text_1"],
                        "evidence_quote": "具体操作见上面的附件",
                    }
                ]
            },
            payload,
        )
        self.assertEqual(
            [image["message_id"] for image in activities[0]["related_images"]],
            ["image_1"],
        )
        self.assertEqual(
            activities[0]["related_images"][0]["association_role"],
            "form_or_document",
        )

    def test_optional_signup_is_not_forced_mandatory(self) -> None:
        """验证 optional_signup_is_not_forced_mandatory 的行为符合预期。"""
        payload = {
            "context_group_id": "group_test",
            "messages": [
                {
                    "message_id": "msg_1",
                    "text": "欢迎全体师生积极报名飞盘比赛，比赛时间为6月11日-6月13日。",
                    "title": None,
                    "url": None,
                    "llm_text": "",
                }
            ],
            "images": [],
        }
        activities = normalize_activity_response(
            {
                "activities": [
                    {
                        "title": "飞盘比赛报名",
                        "kind": "mandatory_task",
                        "mandatory": True,
                        "start_time": "2026-06-11T00:00:00+08:00",
                        "start_date": "2026-06-11",
                        "end_date": "2026-06-13",
                        "evidence_message_ids": ["msg_1"],
                        "evidence_quote": "欢迎全体师生积极报名飞盘比赛",
                        "confidence": 0.9,
                    }
                ]
            },
            payload,
        )
        self.assertEqual(len(activities), 1)
        self.assertFalse(activities[0]["mandatory"])
        self.assertEqual(activities[0]["kind"], "activity")
        self.assertIsNone(activities[0]["start_time"])
        self.assertEqual(activities[0]["start_date"], "2026-06-11")
        self.assertEqual(activities[0]["end_date"], "2026-06-13")
        self.assertEqual(
            activities[0]["evidence_quote"],
            "欢迎全体师生积极报名飞盘比赛",
        )

    def test_optional_self_service_action_is_not_mandatory(self) -> None:
        """“想要参加并自行填写”属于可选表达，不因填写动作变成强制任务。"""
        payload = {
            "context_group_id": "group_test",
            "messages": [
                {
                    "message_id": "msg_1",
                    "text": "想要参加的同学自行填写在线表格。",
                    "title": None,
                    "url": None,
                    "llm_text": "",
                }
            ],
            "images": [],
        }
        activities = normalize_activity_response(
            {
                "activities": [
                    {
                        "title": "在线活动登记",
                        "kind": "mandatory_task",
                        "mandatory": True,
                        "evidence_message_ids": ["msg_1"],
                        "evidence_quote": "想要参加的同学自行填写在线表格",
                    }
                ]
            },
            payload,
        )
        self.assertFalse(activities[0]["mandatory"])
        self.assertEqual(activities[0]["kind"], "activity")

    def test_explicit_obligation_remains_mandatory(self) -> None:
        """通用义务表达应覆盖提交、填写等动作，而不依赖领域词。"""
        payload = {
            "context_group_id": "group_test",
            "messages": [
                {
                    "message_id": "msg_1",
                    "text": "相关人员需要在本周内提交纸质材料。",
                    "title": None,
                    "url": None,
                    "llm_text": "",
                }
            ],
            "images": [],
        }
        activities = normalize_activity_response(
            {
                "activities": [
                    {
                        "title": "纸质材料提交",
                        "kind": "mandatory_task",
                        "mandatory": True,
                        "evidence_message_ids": ["msg_1"],
                        "evidence_quote": "相关人员需要在本周内提交纸质材料",
                    }
                ]
            },
            payload,
        )
        self.assertTrue(activities[0]["mandatory"])
        self.assertEqual(activities[0]["kind"], "mandatory_task")

    def test_ambiguous_relative_deadline_is_not_assumed_to_be_past(self) -> None:
        """缺少上午/下午且模型给出已过去时刻时应保留待确认状态。"""
        payload = {
            "context_group_id": "group_test",
            "messages": [
                {
                    "message_id": "msg_1",
                    "occurred_at_local": "2026-06-11T10:07:52+08:00",
                    "text": "今天 3:00 前提交纸质版。",
                    "title": None,
                    "url": None,
                    "llm_text": "",
                }
            ],
            "images": [],
        }
        activities = normalize_activity_response(
            {
                "activities": [
                    {
                        "title": "纸质版提交",
                        "kind": "mandatory_task",
                        "mandatory": True,
                        "deadline": "2026-06-11T03:00:00+08:00",
                        "evidence_message_ids": ["msg_1"],
                        "evidence_quote": "今天 3:00 前提交纸质版",
                    }
                ]
            },
            payload,
        )
        self.assertIsNone(activities[0]["deadline"])
        self.assertIn(
            "截止时刻缺少上午/下午信息",
            activities[0]["missing_information"],
        )

    def test_activity_extractor_writes_jsonl_with_fake_model(self) -> None:
        """验证 activity_extractor_writes_jsonl_with_fake_model 的行为符合预期。"""
        class FakeResponse:
            content = json.dumps(
                {
                    "activities": [
                        {
                            "title": "医保报销材料提交",
                            "kind": "mandatory_task",
                            "mandatory": True,
                            "deadline": "2026-06-17T17:00:00+08:00",
                            "location": "A8-312",
                            "required_action": "提交纸质报销材料并填写信息",
                            "confidence": 0.9,
                            "evidence_message_ids": [],
                        }
                    ]
                },
                ensure_ascii=False,
            )

        class FakeModel:
            def invoke(self, messages):
                """调用当前函数的核心逻辑。"""
                payload = json.loads(messages[0].content)
                first_message_id = payload["messages"][0]["message_id"]
                data = json.loads(FakeResponse.content)
                data["activities"][0]["evidence_message_ids"] = [first_message_id]
                response = FakeResponse()
                response.content = json.dumps(data, ensure_ascii=False)
                return response

        with tempfile.TemporaryDirectory() as temp_dir:
            normalized_path = Path(temp_dir) / "normalized.jsonl"
            output_path = Path(temp_dir) / "extracted_activities.jsonl"
            with normalized_path.open("w", encoding="utf-8", newline="\n") as handle:
                for message in self.result.messages:
                    handle.write(json.dumps(message.to_dict(), ensure_ascii=False))
                    handle.write("\n")

            count = extract_activities_from_jsonl(
                normalized_path,
                output_path,
                chat_model=FakeModel(),
            )

            rows = [
                json.loads(line)
                for line in output_path.read_text(encoding="utf-8").splitlines()
            ]
        self.assertGreaterEqual(count, 1)
        self.assertEqual(rows[0]["schema_version"], "wechat-extracted-activity/v1")
        self.assertIn("evidence_message_ids", rows[0])

    def test_activity_extractor_links_supplement_to_existing_activity(self) -> None:
        """后续窗口只能通过已有 activity_id 归入同一内部线程。"""
        class FakeResponse:
            content = ""

        class FakeModel:
            def __init__(self) -> None:
                self.calls = 0

            def invoke(self, messages):
                """按两次调用模拟新事项和后续补充。"""
                payload = json.loads(messages[0].content)
                message_id = payload["messages"][0]["message_id"]
                self.calls += 1
                if self.calls == 1:
                    activity = {
                        "title": "设备登记",
                        "kind": "mandatory_task",
                        "mandatory": True,
                        "relation_type": "new",
                        "evidence_message_ids": [message_id],
                        "evidence_quote": payload["messages"][0]["text"],
                    }
                else:
                    existing_id = payload["existing_activities"][0]["activity_id"]
                    activity = {
                        "title": "登记地点补充",
                        "kind": "mandatory_task",
                        "mandatory": True,
                        "relation_type": "supplement",
                        "related_activity_id": existing_id,
                        "location": "A楼",
                        "evidence_message_ids": [message_id],
                        "evidence_quote": payload["messages"][0]["text"],
                    }
                response = FakeResponse()
                response.content = json.dumps(
                    {"activities": [activity]},
                    ensure_ascii=False,
                )
                return response

        records = [
            {
                "message_id": "msg_1",
                "conversation_id": "conv_1",
                "context_group_id": "group_1",
                "occurred_at": "2026-06-07T10:00:00+08:00",
                "occurred_at_local": "2026-06-07T10:00:00+08:00",
                "source_index": 1,
                "message_type": "text",
                "text": "请完成设备登记。",
                "activity_features": {"candidate_score": 0.8},
                "media": [],
            },
            {
                "message_id": "msg_2",
                "conversation_id": "conv_1",
                "context_group_id": "group_2",
                "occurred_at": "2026-06-08T10:00:00+08:00",
                "occurred_at_local": "2026-06-08T10:00:00+08:00",
                "source_index": 2,
                "message_type": "text",
                "text": "登记地点补充为A楼。",
                "activity_features": {"candidate_score": 0.8},
                "media": [],
            },
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "normalized.jsonl"
            output_path = Path(temp_dir) / "activities.jsonl"
            input_path.write_text(
                "\n".join(json.dumps(record, ensure_ascii=False) for record in records)
                + "\n",
                encoding="utf-8",
            )
            count = extract_activities_from_jsonl(
                input_path,
                output_path,
                chat_model=FakeModel(),
            )
            rows = [
                json.loads(line)
                for line in output_path.read_text(encoding="utf-8").splitlines()
            ]
        self.assertEqual(count, 2)
        self.assertEqual(rows[0]["thread_id"], rows[1]["thread_id"])
        self.assertEqual(rows[1]["related_activity_id"], rows[0]["activity_id"])
        self.assertNotIn("topic_key", rows[0])
        self.assertNotIn("topic_key", rows[1])

    def test_activity_extractor_collapses_same_evidence_parent_and_child(self) -> None:
        """同一证据的总事项与包含性子步骤只写入一条。"""
        class FakeResponse:
            content = ""

        class FakeModel:
            def invoke(self, messages):
                """返回同一证据上的包含性重复结果。"""
                payload = json.loads(messages[0].content)
                message_id = payload["messages"][0]["message_id"]
                response = FakeResponse()
                response.content = json.dumps(
                    {
                        "activities": [
                            {
                                "title": "考试安排",
                                "kind": "mandatory_task",
                                "mandatory": True,
                                "evidence_message_ids": [message_id],
                            },
                            {
                                "title": "考试安排准考证盖章",
                                "kind": "mandatory_task",
                                "mandatory": True,
                                "required_action": "统一盖章",
                                "evidence_message_ids": [message_id],
                            },
                        ]
                    },
                    ensure_ascii=False,
                )
                return response

        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "normalized.jsonl"
            output_path = Path(temp_dir) / "activities.jsonl"
            input_path.write_text(
                json.dumps(
                    {
                        "message_id": "msg_1",
                        "conversation_id": "conv_1",
                        "context_group_id": "group_1",
                        "occurred_at": "2026-06-07T10:00:00+08:00",
                        "occurred_at_local": "2026-06-07T10:00:00+08:00",
                        "source_index": 1,
                        "message_type": "text",
                        "text": "考试当天参加考试，准考证统一盖章。",
                        "activity_features": {"candidate_score": 0.8},
                        "media": [],
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            count = extract_activities_from_jsonl(
                input_path,
                output_path,
                chat_model=FakeModel(),
            )
            rows = [
                json.loads(line)
                for line in output_path.read_text(encoding="utf-8").splitlines()
            ]
        self.assertEqual(count, 1)
        self.assertEqual(rows[0]["title"], "考试安排准考证盖章")
        self.assertEqual(rows[0]["required_action"], "统一盖章")

    def test_activity_summary_merges_duplicates_and_images(self) -> None:
        """验证 activity_summary_merges_duplicates_and_images 的行为符合预期。"""
        duplicate_a = {
            "schema_version": "wechat-extracted-activity/v1",
            "context_group_id": "group_1",
            "title": "医保报销材料提交",
            "kind": "mandatory_task",
            "mandatory": True,
            "deadline": "2026-06-17T17:00:00+08:00",
            "registration_url": "https://docs.qq.com/sheet/example",
            "evidence_message_ids": ["msg_1"],
            "related_images": [{"message_id": "img_1", "relative_path": "a.jpg"}],
            "missing_information": [],
            "confidence": 0.8,
        }
        duplicate_b = {
            **duplicate_a,
            "evidence_message_ids": ["msg_2"],
            "related_images": [{"message_id": "img_2", "relative_path": "b.jpg"}],
            "confidence": 0.9,
        }
        merged = merge_activities([duplicate_a, duplicate_b])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["evidence_message_ids"], ["msg_1", "msg_2"])
        self.assertEqual(
            [image["message_id"] for image in merged[0]["related_images"]],
            ["img_1", "img_2"],
        )
        self.assertEqual(merged[0]["confidence"], 0.9)

    def test_activity_summary_merges_explicit_thread_across_time_groups(self) -> None:
        """只有显式线程关系可以稳定跨时间组合并。"""
        base = {
            "schema_version": "wechat-extracted-activity/v1",
            "kind": "mandatory_task",
            "mandatory": True,
            "registration_url": None,
            "related_images": [],
            "missing_information": [],
            "confidence": 0.8,
        }
        merged = merge_activities(
            [
                {
                    **base,
                    "activity_id": "activity_1",
                    "thread_id": "activity_1",
                    "context_group_id": "group_1",
                    "title": "填写审核信息",
                    "evidence_message_ids": ["msg_1"],
                    "evidence_quote": "请填写相关信息",
                },
                {
                    **base,
                    "activity_id": "activity_2",
                    "thread_id": "activity_1",
                    "relation_type": "supplement",
                    "related_activity_id": "activity_1",
                    "context_group_id": "group_2",
                    "title": "提交审核材料",
                    "evidence_message_ids": ["msg_2"],
                    "evidence_quote": "纸质材料交到办公室",
                },
            ]
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual(
            merged[0]["source_context_group_ids"],
            ["group_1", "group_2"],
        )
        self.assertEqual(
            merged[0]["evidence_quotes"],
            ["请填写相关信息", "纸质材料交到办公室"],
        )

    def test_topic_key_does_not_merge_unrelated_activities(self) -> None:
        """旧数据中的同名主题键不能作为事项身份。"""
        merged = merge_activities(
            [
                {
                    "topic_key": "共享主题",
                    "context_group_id": "group_1",
                    "title": "设备维护预约",
                    "summary": "预约设备维护",
                    "mandatory": False,
                    "evidence_message_ids": ["msg_1"],
                    "related_images": [],
                    "missing_information": [],
                    "confidence": 0.8,
                },
                {
                    "topic_key": "共享主题",
                    "context_group_id": "group_2",
                    "title": "会议签到提醒",
                    "summary": "参加会议时签到",
                    "mandatory": True,
                    "evidence_message_ids": ["msg_2"],
                    "related_images": [],
                    "missing_information": [],
                    "confidence": 0.8,
                },
            ]
        )
        self.assertEqual(len(merged), 2)

    def test_activity_summary_classifies_sections(self) -> None:
        """验证 activity_summary_classifies_sections 的行为符合预期。"""
        summary = build_activity_summary(
            [
                {
                    "context_group_id": "group_1",
                    "title": "医保报销材料提交",
                    "kind": "mandatory_task",
                    "mandatory": True,
                    "deadline": "2026-06-17T17:00:00+08:00",
                    "registration_url": None,
                    "missing_information": [],
                    "evidence_message_ids": ["msg_1"],
                    "related_images": [],
                    "confidence": 0.9,
                },
                {
                    "context_group_id": "group_2",
                    "title": "飞盘比赛报名",
                    "kind": "activity",
                    "mandatory": False,
                    "deadline": "2026-06-10T22:00:00+08:00",
                    "registration_url": "https://docs.qq.com/form/example",
                    "missing_information": [],
                    "evidence_message_ids": ["msg_2"],
                    "related_images": [],
                    "confidence": 0.8,
                },
                {
                    "context_group_id": "group_3",
                    "title": "空调折旧费缴纳通知",
                    "kind": "announcement",
                    "mandatory": True,
                    "deadline": None,
                    "registration_url": None,
                    "missing_information": ["deadline"],
                    "evidence_message_ids": ["msg_3"],
                    "related_images": [],
                    "confidence": 0.7,
                },
            ]
        )
        self.assertEqual(summary["counts"]["merged_activities"], 3)
        self.assertEqual(len(summary["mandatory_tasks"]), 2)
        self.assertEqual(len(summary["recommended_activities"]), 1)
        self.assertEqual(len(summary["incomplete_items"]), 0)
        identities = []
        for section in (
            "mandatory_tasks",
            "recommended_activities",
            "incomplete_items",
            "other_activities",
            "cancelled_or_updated",
        ):
            identities.extend(
                item["evidence_message_ids"][0]
                for item in summary[section]
            )
        self.assertEqual(len(identities), len(set(identities)))

    def test_summary_renderer_embeds_related_images(self) -> None:
        """验证 summary_renderer_embeds_related_images 的行为符合预期。"""
        image_message = next(
            message
            for message in self.result.messages
            if message.message_type == "image" and message.media
        )
        summary = build_activity_summary(
            [
                {
                    "context_group_id": image_message.context_group_id,
                    "title": "空调折旧费缴纳通知",
                    "kind": "mandatory_task",
                    "mandatory": True,
                    "deadline": None,
                    "registration_url": None,
                    "missing_information": ["deadline"],
                    "evidence_message_ids": ["msg_1"],
                    "evidence_quote": "缴费的方式见共享文档的二维码",
                    "related_images": [
                        {
                            **image_message.media[0].to_dict(),
                            "message_id": image_message.message_id,
                            "occurred_at_local": image_message.occurred_at_local,
                        }
                    ],
                    "confidence": 0.7,
                }
            ]
        )
        html_text = render_summary_html(summary, SAMPLE_EXPORT)
        self.assertIn("data:image/jpeg;base64,", html_text)
        self.assertIn("空调折旧费缴纳通知", html_text)

    def test_pipeline_skip_extract_reuses_existing_activities(self) -> None:
        """验证 pipeline_skip_extract_reuses_existing_activities 的行为符合预期。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_export = Path(temp_dir) / "export"
            shutil.copytree(SAMPLE_EXPORT, temp_export)

            extracted_path = temp_export / "extracted_activities.jsonl"
            extracted_path.write_text(
                json.dumps(
                    {
                        "schema_version": "wechat-extracted-activity/v1",
                        "context_group_id": self.result.messages[0].context_group_id,
                        "title": "医保报销材料提交",
                        "kind": "mandatory_task",
                        "mandatory": True,
                        "deadline": "2026-06-17T17:00:00+08:00",
                        "registration_url": None,
                        "missing_information": [],
                        "evidence_message_ids": [self.result.messages[0].message_id],
                        "related_images": [],
                        "confidence": 0.9,
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            result = build_report(
                temp_export,
                skip_extract=True,
                make_pdf=False,
            )

            self.assertFalse(result["llm_called"])
            self.assertEqual(result["extract_mode"], "skipped")
            self.assertTrue((temp_export / "weekly_activity_summary.json").is_file())
            self.assertTrue((temp_export / "weekly_activity_summary.html").is_file())

    def test_api_export_result_can_feed_pipeline(self) -> None:
        """验证 api_export_result_can_feed_pipeline 的行为符合预期。"""
        import tools.build_wechat_activity_report as report_builder

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_export = Path(temp_dir) / "api_export"
            shutil.copytree(SAMPLE_EXPORT, temp_export)

            extracted_path = temp_export / "extracted_activities.jsonl"
            extracted_path.write_text(
                json.dumps(
                    {
                        "schema_version": "wechat-extracted-activity/v1",
                        "context_group_id": self.result.messages[0].context_group_id,
                        "title": "API 导出后复用提取结果",
                        "kind": "mandatory_task",
                        "mandatory": True,
                        "deadline": None,
                        "registration_url": None,
                        "missing_information": [],
                        "evidence_message_ids": [self.result.messages[0].message_id],
                        "related_images": [],
                        "confidence": 0.9,
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            original = report_builder.export_wechat_chat

            def fake_export(_request):
                """提供测试用的替身实现。"""
                return WeChatExportResult(
                    export_id="export123",
                    status="done",
                    zip_path=Path(temp_dir) / "api_export.zip",
                    export_dir=temp_export,
                    job={"exportId": "export123", "status": "done", "zipReady": True},
                )

            try:
                report_builder.export_wechat_chat = fake_export
                result = report_builder.build_report_from_wechat_api(
                    api_base="http://127.0.0.1:10392",
                    account="wxid_account",
                    usernames=["wxid_contact"],
                    start_time=1780761600,
                    end_time=1780847999,
                    export_name="wechat_chat_xunxu_2026-06-07_json",
                    output_root=Path(temp_dir),
                    skip_extract=True,
                    make_pdf=False,
                )
            finally:
                report_builder.export_wechat_chat = original

            self.assertFalse(result["llm_called"])
            self.assertEqual(result["wechat_export"]["export_id"], "export123")
            self.assertEqual(result["input"], str(temp_export.resolve()))
            self.assertTrue((temp_export / "weekly_activity_summary.html").is_file())

    def test_api_export_zip_path_traversal_is_rejected(self) -> None:
        """验证 api_export_zip_path_traversal_is_rejected 的行为符合预期。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            zip_path = root / "bad.zip"
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr("../evil.txt", "bad")

            with self.assertRaises(WeChatExportApiError):
                extract_zip_safely(zip_path, root / "out")

    def test_wechat_export_tool_facade_returns_structured_result(self) -> None:
        """验证 wechat_export_tool_facade_returns_structured_result 的行为符合预期。"""
        import tools.wechat_normalizer.wechat_export_api as export_api

        original = export_api.export_wechat_chat

        def fake_export(request):
            """提供测试用的替身实现。"""
            self.assertEqual(request.api_base, "http://127.0.0.1:10392")
            self.assertEqual(request.usernames, ["wxid_a6aq0g1v2g7f22"])
            return WeChatExportResult(
                export_id="export456",
                status="done",
                zip_path=Path("out.zip"),
                export_dir=Path("out"),
                job={"exportId": "export456", "status": "done"},
            )

        try:
            export_api.export_wechat_chat = fake_export
            result = call_wechat_export_tool(
                {
                    "api_base": "http://127.0.0.1:10392",
                    "account": "wxid_3own0jvr3p9k12",
                    "usernames": ["wxid_a6aq0g1v2g7f22"],
                    "start_time": 1780761600,
                    "end_time": 1780847999,
                    "export_name": "wechat_chat_xunxu_2026-06-07_json",
                    "output_root": "src/tools/wechatOutput",
                }
            )
        finally:
            export_api.export_wechat_chat = original

        self.assertEqual(WECHAT_EXPORT_TOOL_SCHEMA["name"], "export_wechat_chat")
        self.assertEqual(result["export_id"], "export456")
        self.assertEqual(result["status"], "done")


if __name__ == "__main__":
    unittest.main()
