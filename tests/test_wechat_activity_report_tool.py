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
    reply = try_handle_wechat_activity_report("帮我生成一周聊天内容的活动总结")
    assert reply is not None
    assert "群名或联系人名称" in reply


def test_wechat_report_tool_resolves_group_name_for_weekly_request() -> None:
    captured = {}
    resolved = {}
    old_today = os.environ.get("WECHAT_REPORT_TODAY")
    os.environ["WECHAT_REPORT_TODAY"] = "2026-06-08"

    def fake_resolver(api_base, keyword, account):
        resolved["api_base"] = api_base
        resolved["keyword"] = keyword
        resolved["account"] = account
        return ContactResolution(
            username="123456@chatroom",
            display_name="卫星互联网研究所（25级）",
            account="wxid_3own0jvr3p9k12",
        )

    def fake_runner(**kwargs):
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


def test_wechat_report_langgraph_exposes_expected_nodes() -> None:
    graph = build_wechat_activity_report_graph()
    node_names = set(graph.get_graph().nodes)
    assert "route_intent" in node_names
    assert "parse_request" in node_names
    assert "resolve_contact" in node_names
    assert "build_report" in node_names
    assert "final_response" in node_names


def test_wechat_report_tool_parses_explicit_username_request() -> None:
    captured = {}

    def fake_runner(**kwargs):
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
