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
    normalize_activity_response,
)
from tools.wechat_normalizer.activity_summary import (
    build_activity_summary,
    merge_activities,
)
from tools.wechat_normalizer.summary_renderer import render_summary_html
from tools.build_wechat_activity_report import build_report
from tools.wechat_normalizer.media import inspect_media
from tools.wechat_normalizer.normalizer import normalize_export, _sanitize_url
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

    def test_optional_signup_is_not_forced_mandatory(self) -> None:
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

    def test_activity_extractor_writes_jsonl_with_fake_model(self) -> None:
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

    def test_activity_summary_merges_duplicates_and_images(self) -> None:
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

    def test_activity_summary_classifies_sections(self) -> None:
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
        self.assertEqual(len(summary["incomplete_items"]), 1)

    def test_summary_renderer_embeds_related_images(self) -> None:
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
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            zip_path = root / "bad.zip"
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr("../evil.txt", "bad")

            with self.assertRaises(WeChatExportApiError):
                extract_zip_safely(zip_path, root / "out")

    def test_wechat_export_tool_facade_returns_structured_result(self) -> None:
        import tools.wechat_normalizer.wechat_export_api as export_api

        original = export_api.export_wechat_chat

        def fake_export(request):
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
