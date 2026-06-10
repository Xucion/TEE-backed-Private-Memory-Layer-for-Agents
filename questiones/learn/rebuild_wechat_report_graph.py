from typing import TypedDict
import re
from langgraph.graph import END, START, StateGraph
TIME_LABELS = (
    "今天",
    "昨天",
    "前天",
    "本周",
    "上周",
    "近一周",
)
CONTACTS = {
    "技术交流群": "wxid_group_001",
    "张三": "wxid_zhangsan",
    "李四": "wxid_lisi",
}
class ReportState(TypedDict, total=False):
    user_message: str

    handled: bool
    target_name: str
    range_label: str

    username: str
    report_path: str

    reply: str
    error: str

REPORT_KEYWORDS = (
    "活动报告",
    "活动总结",
    "活动汇总",
    "微信报告",
)

def route_intent_node(state: ReportState) -> dict:
    message = state["user_message"]

    handled = any(
        keyword in message
        for keyword in REPORT_KEYWORDS
    )

    if handled:
        return {
            "handled": True,
            "error": "",
        }

    return {
        "handled": False,
        "reply": "",
        "error": "",
    }

def route_after_intent(state: ReportState) -> str:
    if state["handled"]:
        return "parse_request"

    return "final_response"

def parse_requset_node(state: ReportState) -> dict:
    message = state["user_message"]

    range_label = next(
        (label for label in TIME_LABELS if label in message),
        "",
    )
    match = re.search(
        r"生成(.+?)(?:今天|昨天|前天|本周|上周|近一周)的?(?:聊天内容)?的?活动(?:总结|报告|汇总)",
        message,
    )

    target_name = match.group(1).strip() if match else ""

    if not target_name:
        return {
            "error": "没有识别出联系人或群名",
            "replay": "请告诉我需要总结哪个联系人或群聊",
        }

    if not range_label:
        return {
            "target_name": target_name,
            "error": "没有识别出时间范围",
            "reply": "请告诉我需要总结哪个时间范围。",
        }

    return {
        "target_name": target_name,
        "range_label": range_label,
        "error": "",
    }

def route_after_parse(state: ReportState) -> str:
    if state.get("error"):
        return "final_response"

    return "resolve_contact"

def resolve_contact_node(state: ReportState) -> dict:
    target_name = state["target_name"]

    username = CONTACTS.get(target_name)

    if not username:
        return {
            "error": f"没有找到联系人或群聊: {target_name}",
            "reply": "请确认联系人或群聊名称是否正确。",
        }

    return {
        "username": username,
        "error": "",
    }

def route_after_contact(state: ReportState) -> str:
    if state.get("error"):
        return "final_response"

    return "build_report"

def build_report_node(state: ReportState) -> dict:
    target_name = state["target_name"]
    username = state["username"]
    range_label = state["range_label"]

    report_path = (
        f"output/{username}_{range_label}_activity_report.html"
    )

    print(
        f"正在生成报告：对象={target_name}，"
        f"时间={range_label}，"
        f"username={username}"
    )

    return {
        "report_path": report_path,
        "error": "",
    }

def final_response_node(state: ReportState) -> dict:
    if not state.get("handled"):
        return {
            "reply": "",
        }

    if state.get("error"):
        return {
            "reply": state.get(
                "reply",
                f"报告生成失败：{state['error']}",
            )
        }

    return {
        "reply": (
            f"已生成“{state['target_name']}”"
            f"{state['range_label']}的活动报告："
            f"{state['report_path']}"
        )
    }


builder = StateGraph(ReportState)

builder.add_node("route_intent", route_intent_node)
builder.add_node("parse_request", parse_request_node)
builder.add_node("resolve_contact", resolve_contact_node)
builder.add_node("build_report", build_report_node)
builder.add_node("final_response", final_response_node)
builder.add_edge(START, "route_intent")
builder.add_condition_edge(
    "route_intent",
    route_after_intent,
    {
        "parse_request": "parse_request",
        "final_response": "final_response",
    }
)
builder.add_conditional_edges(
    "parse_request",
    route_after_parse,
    {
        "resolve_contact": "resolve_contact",
        "final_response": "final_response",
    },
)
builder.add_conditional_edges(
    "resolve_contact",
    route_after_contact,
    {
        "build_report": "build_report",
        "final_response": "final_response",
    },
)
builder.add_edge("build_report", "final_response")
builder.add_edge("final_response", END)
graph = builder.compile()

if __name__ == "__main__":
    test_messages = [
        "帮我生成技术交流群近一周的聊天内容活动总结",
        "帮我生成技术交流群的活动总结",
        "你好，请介绍一下 LangGraph",
    ]

    for message in test_messages:
        print("\n" + "=" * 60)
        print(f"用户输入：{message}")

        final_state = graph.invoke({
            "user_message": message,
        })

        print(f"最终回复：{final_state.get('reply')}")
        print(f"最终状态：{final_state}")