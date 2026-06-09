import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))


from untrusted.wechat_activity_report_tool import (
    ContactResolution,
    build_wechat_activity_report_graph,
    try_handle_wechat_activity_report,
)


def test_wechat_report_tool_asks_for_missing_target() -> None:
    """验证 wechat_report_tool_asks_for_missing_target 的行为符合预期。"""
    reply = try_handle_wechat_activity_report("帮我生成一周聊天内容的活动总结")
    assert reply is not None
    assert "群名或联系人名称" in reply


def test_wechat_report_tool_resolves_group_name_for_weekly_request() -> None:
    """验证 wechat_report_tool_resolves_group_name_for_weekly_request 的行为符合预期。"""
    captured = {}
    resolved = {}
    old_today = os.environ.get("WECHAT_REPORT_TODAY")
    os.environ["WECHAT_REPORT_TODAY"] = "2026-06-08"

    def fake_resolver(api_base, keyword, account):
        """提供测试用的替身实现。"""
        resolved["api_base"] = api_base
        resolved["keyword"] = keyword
        resolved["account"] = account
        return ContactResolution(
            username="123456@chatroom",
            display_name="卫星互联网研究所（25级）",
            account="wxid_3own0jvr3p9k12",
        )

    def fake_runner(**kwargs):
        """提供测试用的替身实现。"""
        captured.update(kwargs)
        return {
            "input": "src/tools/wechatOutput/export",
            "html_output": "src/tools/wechatOutput/export/weekly_activity_summary.html",
            "pdf_output": "src/tools/wechatOutput/export/weekly_activity_summary.pdf",
            "summary_output": "src/tools/wechatOutput/export/weekly_activity_summary.json",
            "llm_called": True,
        }

    try:
        reply = try_handle_wechat_activity_report(
            "帮我生成卫星互联网研究所（25级）一周聊天内容的活动总结",
            runner=fake_runner,
            contact_resolver=fake_resolver,
        )
    finally:
        if old_today is None:
            os.environ.pop("WECHAT_REPORT_TODAY", None)
        else:
            os.environ["WECHAT_REPORT_TODAY"] = old_today

    assert reply is not None
    assert reply.startswith("已为「卫星互联网研究所（25级）」生成微信活动报告")
    assert resolved["keyword"] == "卫星互联网研究所（25级）"
    assert captured["account"] == "wxid_3own0jvr3p9k12"
    assert captured["usernames"] == ["123456@chatroom"]
    assert captured["start_time"] == 1780329600
    assert captured["end_time"] == 1780934399
    assert captured["make_pdf"] is True
    assert captured["skip_extract"] is False


def test_wechat_report_tool_parses_today_relative_date() -> None:
    """验证 wechat_report_tool_parses_today_relative_date 的行为符合预期。"""
    captured = {}
    resolved = {}
    old_today = os.environ.get("WECHAT_REPORT_TODAY")
    os.environ["WECHAT_REPORT_TODAY"] = "2026-06-08"

    def fake_resolver(api_base, keyword, account):
        """提供测试用的替身实现。"""
        resolved["keyword"] = keyword
        return ContactResolution(
            username="wxid_a6aq0g1v2g7f22",
            display_name="寻徐",
            account="wxid_3own0jvr3p9k12",
        )

    def fake_runner(**kwargs):
        """提供测试用的替身实现。"""
        captured.update(kwargs)
        return {
            "input": "src/tools/wechatOutput/export",
            "html_output": "src/tools/wechatOutput/export/weekly_activity_summary.html",
            "pdf_output": "src/tools/wechatOutput/export/weekly_activity_summary.pdf",
            "summary_output": "src/tools/wechatOutput/export/weekly_activity_summary.json",
            "llm_called": True,
        }

    try:
        reply = try_handle_wechat_activity_report(
            "帮我生成和寻徐今天的聊天内容的活动总结",
            runner=fake_runner,
            contact_resolver=fake_resolver,
        )
    finally:
        if old_today is None:
            os.environ.pop("WECHAT_REPORT_TODAY", None)
        else:
            os.environ["WECHAT_REPORT_TODAY"] = old_today

    assert reply is not None
    assert "已为「寻徐」生成微信活动报告" in reply
    assert resolved["keyword"] == "寻徐"
    assert captured["start_time"] == 1780848000
    assert captured["end_time"] == 1780934399
    assert captured["usernames"] == ["wxid_a6aq0g1v2g7f22"]


def test_wechat_report_tool_parses_target_after_time() -> None:
    """验证 wechat_report_tool_parses_target_after_time 的行为符合预期。"""
    captured = {}
    resolved = {}
    old_today = os.environ.get("WECHAT_REPORT_TODAY")
    os.environ["WECHAT_REPORT_TODAY"] = "2026-06-08"

    def fake_resolver(api_base, keyword, account):
        """提供测试用的替身实现。"""
        resolved["keyword"] = keyword
        return ContactResolution(
            username="wxid_a6aq0g1v2g7f22",
            display_name="寻徐",
            account="wxid_3own0jvr3p9k12",
        )

    def fake_runner(**kwargs):
        """提供测试用的替身实现。"""
        captured.update(kwargs)
        return {
            "input": "src/tools/wechatOutput/export",
            "html_output": "src/tools/wechatOutput/export/weekly_activity_summary.html",
            "pdf_output": "",
            "summary_output": "src/tools/wechatOutput/export/weekly_activity_summary.json",
            "llm_called": True,
        }

    try:
        reply = try_handle_wechat_activity_report(
            "帮我生成今天和寻徐的聊天内容的活动总结",
            runner=fake_runner,
            contact_resolver=fake_resolver,
        )
    finally:
        if old_today is None:
            os.environ.pop("WECHAT_REPORT_TODAY", None)
        else:
            os.environ["WECHAT_REPORT_TODAY"] = old_today

    assert reply is not None
    assert "已为「寻徐」生成微信活动报告" in reply
    assert resolved["keyword"] == "寻徐"
    assert captured["start_time"] == 1780848000
    assert captured["end_time"] == 1780934399


def test_wechat_report_tool_inherits_time_from_dialogue() -> None:
    """验证 wechat_report_tool_inherits_time_from_dialogue 的行为符合预期。"""
    captured = {}
    old_today = os.environ.get("WECHAT_REPORT_TODAY")
    os.environ["WECHAT_REPORT_TODAY"] = "2026-06-08"

    def fake_resolver(api_base, keyword, account):
        """提供测试用的替身实现。"""
        assert keyword == "寻徐"
        return ContactResolution(
            username="wxid_a6aq0g1v2g7f22",
            display_name="寻徐",
            account="wxid_3own0jvr3p9k12",
        )

    def fake_runner(**kwargs):
        """提供测试用的替身实现。"""
        captured.update(kwargs)
        return {
            "input": "src/tools/wechatOutput/export",
            "html_output": "src/tools/wechatOutput/export/weekly_activity_summary.html",
            "pdf_output": "",
            "summary_output": "src/tools/wechatOutput/export/weekly_activity_summary.json",
            "llm_called": False,
        }

    history = [
        {"role": "user", "content": "帮我生成今天的聊天内容活动总结"},
        {"role": "assistant", "content": "要生成微信活动报告，还需要群名或联系人名称，例如：卫星互联网研究所（25级）。"},
    ]
    try:
        reply = try_handle_wechat_activity_report(
            "寻徐",
            conversation_history=history,
            runner=fake_runner,
            contact_resolver=fake_resolver,
        )
    finally:
        if old_today is None:
            os.environ.pop("WECHAT_REPORT_TODAY", None)
        else:
            os.environ["WECHAT_REPORT_TODAY"] = old_today

    assert reply is not None
    assert "已为「寻徐」生成微信活动报告" in reply
    assert captured["start_time"] == 1780848000
    assert captured["end_time"] == 1780934399
    assert captured["usernames"] == ["wxid_a6aq0g1v2g7f22"]


def test_wechat_report_tool_inherits_target_from_dialogue() -> None:
    """验证 wechat_report_tool_inherits_target_from_dialogue 的行为符合预期。"""
    captured = {}
    old_today = os.environ.get("WECHAT_REPORT_TODAY")
    os.environ["WECHAT_REPORT_TODAY"] = "2026-06-08"

    def fake_resolver(api_base, keyword, account):
        """提供测试用的替身实现。"""
        assert keyword == "寻徐"
        return ContactResolution(
            username="wxid_a6aq0g1v2g7f22",
            display_name="寻徐",
            account="wxid_3own0jvr3p9k12",
        )

    def fake_runner(**kwargs):
        """提供测试用的替身实现。"""
        captured.update(kwargs)
        return {
            "input": "src/tools/wechatOutput/export",
            "html_output": "src/tools/wechatOutput/export/weekly_activity_summary.html",
            "pdf_output": "",
            "summary_output": "src/tools/wechatOutput/export/weekly_activity_summary.json",
            "llm_called": False,
        }

    history = [
        {"role": "user", "content": "帮我生成和寻徐的聊天内容活动总结"},
        {"role": "assistant", "content": "要生成微信活动报告，还需要时间范围，例如：一周、本周、上周，或 2026-06-07。"},
    ]
    try:
        reply = try_handle_wechat_activity_report(
            "今天",
            conversation_history=history,
            runner=fake_runner,
            contact_resolver=fake_resolver,
        )
    finally:
        if old_today is None:
            os.environ.pop("WECHAT_REPORT_TODAY", None)
        else:
            os.environ["WECHAT_REPORT_TODAY"] = old_today

    assert reply is not None
    assert "已为「寻徐」生成微信活动报告" in reply
    assert captured["start_time"] == 1780848000
    assert captured["end_time"] == 1780934399
    assert captured["usernames"] == ["wxid_a6aq0g1v2g7f22"]


def test_wechat_report_tool_does_not_reuse_completed_request_for_greeting() -> None:
    """已完成的报告请求不应捕获无关的后续问候。"""
    history = [
        {"role": "user", "content": "帮我生成和寻徐一周聊天内容的活动总结"},
        {"role": "assistant", "content": "已为「寻徐」生成微信活动报告。"},
    ]

    reply = try_handle_wechat_activity_report(
        "你好",
        conversation_history=history,
    )

    assert reply is None


def test_wechat_report_tool_does_not_reuse_failed_request_for_greeting() -> None:
    """失败的报告尝试也属于已结束轮次，不应留下待补槽位。"""
    history = [
        {"role": "user", "content": "帮我生成和寻徐一周聊天内容的活动总结"},
        {
            "role": "assistant",
            "content": "解析微信会话失败：没有找到名称匹配「寻徐」的微信会话",
        },
    ]

    reply = try_handle_wechat_activity_report(
        "你好",
        conversation_history=history,
    )

    assert reply is None


def test_wechat_report_langgraph_exposes_expected_nodes() -> None:
    """验证 wechat_report_langgraph_exposes_expected_nodes 的行为符合预期。"""
    graph = build_wechat_activity_report_graph()
    node_names = set(graph.get_graph().nodes)
    assert "route_intent" in node_names
    assert "parse_request" in node_names
    assert "resolve_contact" in node_names
    assert "build_report" in node_names
    assert "final_response" in node_names


def test_wechat_report_tool_parses_explicit_username_request() -> None:
    """验证 wechat_report_tool_parses_explicit_username_request 的行为符合预期。"""
    captured = {}

    def fake_runner(**kwargs):
        """提供测试用的替身实现。"""
        captured.update(kwargs)
        return {
            "input": "src/tools/wechatOutput/export",
            "html_output": "src/tools/wechatOutput/export/weekly_activity_summary.html",
            "pdf_output": "",
            "summary_output": "src/tools/wechatOutput/export/weekly_activity_summary.json",
            "llm_called": False,
        }

    reply = try_handle_wechat_activity_report(
        "为 account=wxid_3own0jvr3p9k12，username=wxid_a6aq0g1v2g7f22，生成 2026-06-07 的微信活动报告，不生成 PDF",
        runner=fake_runner,
    )

    assert reply is not None
    assert "已为「wxid_a6aq0g1v2g7f22」生成微信活动报告" in reply
    assert captured["account"] == "wxid_3own0jvr3p9k12"
    assert captured["usernames"] == ["wxid_a6aq0g1v2g7f22"]
    assert captured["start_time"] == 1780761600
    assert captured["end_time"] == 1780847999
    assert captured["make_pdf"] is False


def test_wechat_report_tool_reports_pdf_failure() -> None:
    """PDF 渲染失败时回复不应声称已生成 PDF。"""
    def fake_runner(**kwargs):
        """提供返回 PDF 失败结果的测试替身。"""
        return {
            "input": "src/tools/wechatOutput/export",
            "html_output": "src/tools/wechatOutput/export/weekly_activity_summary.html",
            "pdf_output": None,
            "pdf_created": False,
            "pdf_error": "No Chrome/Edge/Chromium executable found for PDF export.",
            "summary_output": "src/tools/wechatOutput/export/weekly_activity_summary.json",
            "llm_called": False,
        }

    reply = try_handle_wechat_activity_report(
        "为 username=wxid_test，生成 2026-06-07 的微信活动报告",
        runner=fake_runner,
    )

    assert reply is not None
    assert "PDF：生成失败" in reply
    assert "PDF：src/tools" not in reply
