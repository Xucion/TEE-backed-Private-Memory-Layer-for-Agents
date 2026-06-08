from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Literal, TypedDict

try:
    from langgraph.graph import END, StateGraph
except ModuleNotFoundError:  # pragma: no cover - exercised in minimal local envs.
    END = "__end__"
    StateGraph = None

from tools.build_wechat_activity_report import build_report_from_wechat_api


REPORT_INTENT_KEYWORDS = ("活动报告", "活动汇总", "活动总结", "事项报告", "事项汇总", "微信报告")
WECHAT_KEYWORDS = ("微信", "聊天记录", "聊天内容", "群")
DEFAULT_API_BASE = "http://127.0.0.1:10392"
DEFAULT_OUTPUT_ROOT = "src/tools/wechatOutput"
LOCAL_TZ = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class ContactResolution:
    username: str
    display_name: str
    account: str | None = None


@dataclass(frozen=True)
class WeChatActivityReportToolCall:
    api_base: str
    account: str | None
    username: str
    display_name: str
    start_time: int
    end_time: int
    export_name: str
    output_root: Path
    backend_output_dir: str | None
    skip_extract: bool
    dry_run_llm: bool
    make_pdf: bool


class WeChatReportGraphState(TypedDict, total=False):
    user_message: str
    conversation_history: list[dict[str, str]]
    runner: Callable[..., dict[str, Any]]
    contact_resolver: Callable[[str, str, str | None], ContactResolution]
    handled: bool
    needs_contact_resolution: bool
    api_base: str
    account: str | None
    username: str
    display_name: str
    target_name: str
    start_time: int
    end_time: int
    range_label: str
    export_name: str
    output_root: Path
    backend_output_dir: str | None
    skip_extract: bool
    dry_run_llm: bool
    make_pdf: bool
    report_result: dict[str, Any]
    reply: str | None
    error: str | None


_REPORT_GRAPH = None


def try_handle_wechat_activity_report(
    user_message: str,
    *,
    conversation_history: list[dict[str, str]] | None = None,
    runner: Callable[..., dict[str, Any]] = build_report_from_wechat_api,
    contact_resolver: Callable[[str, str, str | None], ContactResolution] | None = None,
) -> str | None:
    """Run the WeChat report LangGraph subflow and return a reply when handled."""

    graph = build_wechat_activity_report_graph()
    state = graph.invoke(
        {
            "user_message": str(user_message or "").strip(),
            "conversation_history": conversation_history or [],
            "runner": runner,
            "contact_resolver": contact_resolver or resolve_contact_username,
        }
    )
    if not state.get("handled"):
        return None
    return str(state.get("reply") or "")


def build_wechat_activity_report_graph():
    """Build the LangGraph subgraph for conversational WeChat report requests."""

    global _REPORT_GRAPH
    if _REPORT_GRAPH is not None:
        return _REPORT_GRAPH
    if StateGraph is None:
        _REPORT_GRAPH = _FallbackWeChatReportGraph()
        return _REPORT_GRAPH

    graph = StateGraph(WeChatReportGraphState)
    graph.add_node("route_intent", _route_intent_node)
    graph.add_node("parse_request", _parse_request_node)
    graph.add_node("resolve_contact", _resolve_contact_node)
    graph.add_node("build_report", _build_report_node)
    graph.add_node("final_response", _final_response_node)

    graph.set_entry_point("route_intent")
    graph.add_conditional_edges(
        "route_intent",
        _route_after_intent,
        {
            "parse_request": "parse_request",
            "final_response": "final_response",
        },
    )
    graph.add_conditional_edges(
        "parse_request",
        _route_after_parse,
        {
            "resolve_contact": "resolve_contact",
            "build_report": "build_report",
            "final_response": "final_response",
        },
    )
    graph.add_conditional_edges(
        "resolve_contact",
        _route_after_contact,
        {
            "build_report": "build_report",
            "final_response": "final_response",
        },
    )
    graph.add_edge("build_report", "final_response")
    graph.add_edge("final_response", END)

    _REPORT_GRAPH = graph.compile()
    return _REPORT_GRAPH


class _FallbackGraphView:
    def __init__(self, nodes: list[str]) -> None:
        """初始化当前对象。"""
        self.nodes = {name: object() for name in nodes}


class _FallbackWeChatReportGraph:
    """Small fallback for environments that have not installed LangGraph yet.

    requirements.txt declares LangGraph. This class keeps imports and tests
    usable in minimal local interpreters; deployed environments with LangGraph
    installed use the real StateGraph above.
    """

    _nodes = ["route_intent", "parse_request", "resolve_contact", "build_report", "final_response"]

    def get_graph(self) -> _FallbackGraphView:
        """获取LangGraph 工作流。"""
        return _FallbackGraphView(self._nodes)

    def invoke(self, initial_state: dict[str, Any]) -> dict[str, Any]:
        """调用当前函数的核心逻辑。"""
        state: WeChatReportGraphState = dict(initial_state)
        state.update(_route_intent_node(state))
        if _route_after_intent(state) == "parse_request":
            state.update(_parse_request_node(state))
            next_node = _route_after_parse(state)
            if next_node == "resolve_contact":
                state.update(_resolve_contact_node(state))
                next_node = _route_after_contact(state)
            if next_node == "build_report":
                state.update(_build_report_node(state))
        state.update(_final_response_node(state))
        return state


def is_wechat_activity_report_request(user_message: str) -> bool:
    """判断指定条件是否成立。"""
    return _looks_like_report_request(str(user_message or "").strip())


def resolve_contact_username(api_base: str, keyword: str, account: str | None = None) -> ContactResolution:
    """Resolve a display name to a conversation username through WeChatDataAnalysis."""

    keyword = str(keyword or "").strip()
    if not keyword:
        raise ValueError("缺少群名或联系人名称。")

    attempts = [
        {"include_friends": "true", "include_groups": "false", "include_officials": "false"},
        {"include_friends": "true", "include_groups": "true", "include_officials": "false"},
    ]
    last_contacts: list[dict[str, Any]] = []
    response_account: str | None = None
    for params in attempts:
        payload = _get_contacts(api_base, keyword, account, params)
        response_account = str(payload.get("account") or "").strip() or response_account
        contacts = payload.get("contacts") if isinstance(payload, dict) else None
        if not isinstance(contacts, list):
            continue
        last_contacts = [item for item in contacts if isinstance(item, dict)]
        picked = _pick_contact(last_contacts, keyword)
        if picked is not None:
            username = str(picked.get("username") or "").strip()
            display_name = _contact_name(picked) or keyword
            if username:
                return ContactResolution(username=username, display_name=display_name, account=response_account)

    candidates = "、".join(_contact_name(item) for item in last_contacts[:5] if _contact_name(item))
    suffix = f"。候选项：{candidates}" if candidates else ""
    raise ValueError(f"没有找到名称匹配「{keyword}」的微信会话{suffix}")


def _route_intent_node(state: WeChatReportGraphState) -> dict[str, Any]:
    """判断用户消息是否应进入微信报告子图。"""
    text = str(state.get("user_message") or "").strip()
    return {
        "handled": _looks_like_report_request(text) or _has_pending_report_context(state.get("conversation_history") or []),
        "reply": None,
        "error": None,
    }


def _parse_request_node(state: WeChatReportGraphState) -> dict[str, Any]:
    """解析请求数据、工作流节点。"""
    text = str(state.get("user_message") or "").strip()
    context_text = _last_report_request_text(state.get("conversation_history") or [])
    account = _extract_named_value(text, "account", "账号") or _extract_named_value(context_text, "account", "账号")
    username = _extract_named_value(text, "username", "会话") or _extract_named_value(context_text, "username", "会话")
    api_base = (
        _extract_named_value(text, "wechat_api", "wechat-api", "api")
        or _extract_named_value(context_text, "wechat_api", "wechat-api", "api")
        or os.getenv("WECHAT_EXPORT_API_BASE", DEFAULT_API_BASE)
    )
    output_root = Path(
        _extract_named_value(text, "output_root", "output-root")
        or _extract_named_value(context_text, "output_root", "output-root")
        or os.getenv("WECHAT_REPORT_OUTPUT_ROOT", DEFAULT_OUTPUT_ROOT)
    )
    backend_output_dir = (
        _extract_named_value(text, "backend_output_dir", "backend-output-dir", "output_dir", "output-dir")
        or _extract_named_value(context_text, "backend_output_dir", "backend-output-dir", "output_dir", "output-dir")
        or os.getenv("WECHAT_EXPORT_BACKEND_OUTPUT_DIR")
    )

    start_time, end_time, range_label = _extract_time_range(text)
    if start_time is None or end_time is None:
        start_time, end_time, range_label = _extract_time_range(context_text)
    if start_time is None or end_time is None:
        return {
            "error": "要生成微信活动报告，还需要时间范围，例如：一周、本周、上周，或 2026-06-07。",
            "reply": "要生成微信活动报告，还需要时间范围，例如：一周、本周、上周，或 2026-06-07。",
        }

    target_name = _extract_target_name(text) or _extract_target_name(context_text)
    if not username and not target_name:
        reply = "要生成微信活动报告，还需要群名或联系人名称，例如：卫星互联网研究所（25级）。"
        return {"error": reply, "reply": reply}

    display_name = target_name or username or ""
    export_name = (
        _extract_named_value(text, "export_name", "export-name", "导出名")
        or f"wechat_chat_{_safe_export_part(display_name)}_{range_label}"
    )

    return {
        "api_base": api_base,
        "account": account,
        "username": username or "",
        "display_name": display_name,
        "target_name": target_name or "",
        "start_time": int(start_time),
        "end_time": int(end_time),
        "range_label": range_label,
        "export_name": export_name,
        "output_root": output_root,
        "backend_output_dir": backend_output_dir,
        "skip_extract": _has_any(text, "skip-extract", "跳过提取", "复用已有", "不调用大模型"),
        "dry_run_llm": _has_any(text, "dry-run-llm", "dry run", "只生成 payload", "只生成载荷"),
        "make_pdf": not _has_any(text, "no-pdf", "不生成 PDF", "不要 PDF"),
        "needs_contact_resolution": not bool(username),
    }


def _resolve_contact_node(state: WeChatReportGraphState) -> dict[str, Any]:
    """解析微信联系人或群名对应的 username。"""
    resolver = state.get("contact_resolver") or resolve_contact_username
    api_base = str(state.get("api_base") or DEFAULT_API_BASE)
    target_name = str(state.get("target_name") or "").strip()
    account = state.get("account")
    try:
        resolved = resolver(api_base, target_name, account)
    except Exception as exc:
        reply = f"解析微信会话失败：{exc}"
        return {"error": reply, "reply": reply}

    return {
        "username": resolved.username,
        "display_name": resolved.display_name,
        "account": account or resolved.account,
        "needs_contact_resolution": False,
    }


def _build_report_node(state: WeChatReportGraphState) -> dict[str, Any]:
    """调用报告流水线生成微信活动报告。"""
    runner = state.get("runner") or build_report_from_wechat_api
    try:
        result = runner(
            api_base=str(state["api_base"]),
            account=state.get("account"),
            usernames=[str(state["username"])],
            start_time=int(state["start_time"]),
            end_time=int(state["end_time"]),
            export_name=str(state["export_name"]),
            output_root=state["output_root"],
            backend_output_dir=state.get("backend_output_dir"),
            skip_extract=bool(state.get("skip_extract", False)),
            dry_run_llm=bool(state.get("dry_run_llm", False)),
            make_pdf=bool(state.get("make_pdf", True)),
        )
    except Exception as exc:
        reply = f"微信活动报告生成失败：{exc}"
        return {"error": reply, "reply": reply}
    return {"report_result": result}


def _final_response_node(state: WeChatReportGraphState) -> dict[str, Any]:
    """根据报告生成结果组装最终回复。"""
    if not state.get("handled"):
        return {"reply": None}
    if state.get("reply"):
        return {}

    result = state.get("report_result") or {}
    display_name = str(state.get("display_name") or state.get("username") or "目标会话")
    if state.get("dry_run_llm"):
        return {
            "reply": (
                f"已为「{display_name}」生成 LLM dry-run 请求载荷。\n"
                f"- 导出目录：{result.get('input', '')}\n"
                f"- dry-run 文件：{result.get('dry_run_payloads', '')}"
            )
        }

    lines = [
        f"已为「{display_name}」生成微信活动报告。",
        f"- 时间范围：{_format_range(int(state['start_time']), int(state['end_time']))}",
        f"- HTML：{result.get('html_output', '')}",
    ]
    pdf_output = str(result.get("pdf_output") or "")
    if pdf_output:
        lines.append(f"- PDF：{pdf_output}")
    lines.extend(
        [
            f"- 周报 JSON：{result.get('summary_output', '')}",
            f"- 导出目录：{result.get('input', '')}",
            f"- LLM 调用：{'是' if result.get('llm_called') else '否'}",
        ]
    )
    return {"reply": "\n".join(lines)}


def _route_after_intent(state: WeChatReportGraphState) -> Literal["parse_request", "final_response"]:
    """决定意图识别后的下一个节点。"""
    return "parse_request" if state.get("handled") else "final_response"


def _route_after_parse(state: WeChatReportGraphState) -> Literal["resolve_contact", "build_report", "final_response"]:
    """决定请求解析后的下一个节点。"""
    if state.get("reply") or state.get("error"):
        return "final_response"
    if state.get("needs_contact_resolution"):
        return "resolve_contact"
    return "build_report"


def _route_after_contact(state: WeChatReportGraphState) -> Literal["build_report", "final_response"]:
    """决定联系人解析后的下一个节点。"""
    if state.get("reply") or state.get("error"):
        return "final_response"
    return "build_report"


def _get_contacts(api_base: str, keyword: str, account: str | None, filters: dict[str, str]) -> dict[str, Any]:
    """获取微信联系人列表。"""
    params = {"keyword": keyword, **filters}
    if account:
        params["account"] = account
    query = urllib.parse.urlencode(params)
    url = f"{api_base.rstrip('/')}/api/chat/contacts?{query}"
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"contacts API HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"contacts API 调用失败：{exc}") from exc
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except Exception as exc:
        raise RuntimeError("contacts API 返回了无效 JSON") from exc
    if not isinstance(payload, dict) or payload.get("status") != "success":
        raise RuntimeError(f"contacts API 返回异常：{payload}")
    return payload


def _pick_contact(contacts: list[dict[str, Any]], keyword: str) -> dict[str, Any] | None:
    """从候选联系人中选择最匹配的一项。"""
    normalized_keyword = _normalize_name(keyword)
    if not contacts:
        return None

    def score(item: dict[str, Any]) -> tuple[int, int]:
        """计算当前函数的核心逻辑。"""
        names = [_contact_name(item), str(item.get("remark") or ""), str(item.get("nickname") or "")]
        normalized_names = [_normalize_name(name) for name in names if name]
        if normalized_keyword in normalized_names:
            return (0, 0)
        if any(normalized_keyword and normalized_keyword in name for name in normalized_names):
            return (1, 0)
        return (2, 0)

    ranked = sorted(contacts, key=score)
    best = ranked[0]
    return best if score(best)[0] < 2 else None


def _contact_name(item: dict[str, Any]) -> str:
    """提取联系人可展示名称。"""
    return str(item.get("displayName") or item.get("remark") or item.get("nickname") or item.get("username") or "").strip()


def _looks_like_report_request(text: str) -> bool:
    """判断文本是否像微信活动报告请求。"""
    if not text:
        return False
    has_report_intent = any(keyword in text for keyword in REPORT_INTENT_KEYWORDS)
    has_wechat_context = any(keyword in text for keyword in WECHAT_KEYWORDS)
    has_explicit_params = "username" in text or "account" in text
    return has_report_intent and (has_wechat_context or has_explicit_params)


def _has_pending_report_context(history: list[dict[str, str]]) -> bool:
    """判断指定条件是否成立。"""
    for item in reversed(history[-6:]):
        role = item.get("role")
        content = str(item.get("content") or "")
        if role == "assistant" and "要生成微信活动报告，还需要" in content:
            return True
        if role == "user" and _looks_like_report_request(content):
            return True
    return False


def _last_report_request_text(history: list[dict[str, str]]) -> str:
    """从历史中取最近一次微信报告请求。"""
    for item in reversed(history[-8:]):
        if item.get("role") != "user":
            continue
        content = str(item.get("content") or "").strip()
        if _looks_like_report_request(content):
            return content
    return ""


def _extract_target_name(text: str) -> str | None:
    """提取target、名称。"""
    explicit = _extract_named_value(text, "群名", "联系人", "会话名称")
    if explicit:
        return explicit

    segment = text
    match = re.search(r"(?:帮我|请|麻烦你)?(?:生成|汇总|总结)(.+)", text)
    if match:
        segment = match.group(1)

    target = _remove_target_noise(segment)
    target = re.sub(r"^(?:和|跟|与)", "", target).strip(" ，,。；;：:的")
    return target or None


def _remove_target_noise(text: str) -> str:
    """移除对象名称中的时间和报告关键词噪声。"""
    target = str(text or "")
    noise_patterns = [
        r"20\d{2}[-/年]\d{1,2}[-/月]\d{1,2}日?\s*(?:到|至|-|~)\s*20\d{2}[-/年]\d{1,2}[-/月]\d{1,2}日?",
        r"20\d{2}[-/年]\d{1,2}[-/月]\d{1,2}日?",
        r"最近一周",
        r"近一周",
        r"一周",
        r"本周",
        r"上周",
        r"今天",
        r"今日",
        r"昨天",
        r"昨日",
        r"前天",
        r"微信",
        r"聊天内容",
        r"聊天记录",
        r"活动(?:总结|汇总|报告)",
        r"事项(?:总结|汇总|报告)",
    ]
    for pattern in noise_patterns:
        target = re.sub(pattern, " ", target)
    target = re.sub(r"\s+", "", target)
    return target.strip(" ，,。；;：:的")


def _extract_time_range(text: str) -> tuple[int | None, int | None, str]:
    """提取time、时间范围。"""
    start_time = _extract_named_int(text, "start_time", "start-time")
    end_time = _extract_named_int(text, "end_time", "end-time")
    if start_time is not None and end_time is not None:
        return start_time, end_time, f"{start_time}_{end_time}"

    range_match = re.search(
        r"(20\d{2}[-/年]\d{1,2}[-/月]\d{1,2}日?)\s*(?:到|至|-|~)\s*(20\d{2}[-/年]\d{1,2}[-/月]\d{1,2}日?)",
        text,
    )
    if range_match:
        start_day = _parse_date_text(range_match.group(1))
        end_day = _parse_date_text(range_match.group(2))
        if start_day and end_day:
            start, _ = _day_bounds(start_day)
            _, end = _day_bounds(end_day)
            return start, end, f"{start_day.isoformat()}_{end_day.isoformat()}"

    single_day = _extract_report_date(text)
    if single_day:
        start, end = _day_bounds(single_day)
        return start, end, single_day.isoformat()

    today = _local_today()
    if _has_any(text, "今天", "今日"):
        start, end = _day_bounds(today)
        return start, end, today.isoformat()

    if _has_any(text, "昨天", "昨日"):
        day = today - timedelta(days=1)
        start, end = _day_bounds(day)
        return start, end, day.isoformat()

    if "前天" in text:
        day = today - timedelta(days=2)
        start, end = _day_bounds(day)
        return start, end, day.isoformat()

    if "上周" in text:
        this_monday = today - timedelta(days=today.weekday())
        start_day = this_monday - timedelta(days=7)
        end_day = this_monday - timedelta(days=1)
        start, _ = _day_bounds(start_day)
        _, end = _day_bounds(end_day)
        return start, end, f"{start_day.isoformat()}_{end_day.isoformat()}"

    if "本周" in text:
        start_day = today - timedelta(days=today.weekday())
        start, _ = _day_bounds(start_day)
        _, end = _day_bounds(today)
        return start, end, f"{start_day.isoformat()}_{today.isoformat()}"

    if _has_any(text, "最近一周", "近一周", "一周"):
        start_day = today - timedelta(days=6)
        start, _ = _day_bounds(start_day)
        _, end = _day_bounds(today)
        return start, end, f"{start_day.isoformat()}_{today.isoformat()}"

    return None, None, ""


def _extract_named_value(text: str, *names: str) -> str | None:
    """提取named、值。"""
    for name in names:
        pattern = rf"(?:{re.escape(name)})\s*[=:：]\s*([^\s,，;；]+)"
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip().strip("\"'")
    return None


def _extract_named_int(text: str, *names: str) -> int | None:
    """提取named、int。"""
    value = _extract_named_value(text, *names)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _extract_report_date(text: str) -> date | None:
    """提取微信活动报告、日期。"""
    match = re.search(r"(20\d{2}[-/年]\d{1,2}[-/月]\d{1,2}日?)", text)
    if not match:
        return None
    return _parse_date_text(match.group(1))


def _parse_date_text(text: str) -> date | None:
    """解析日期、文本。"""
    match = re.search(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})日?", text)
    if not match:
        return None
    year, month, day = (int(match.group(i)) for i in range(1, 4))
    return date(year, month, day)


def _day_bounds(day: date) -> tuple[int, int]:
    """计算指定日期在本地时区的起止时间戳。"""
    start = datetime.combine(day, time.min, tzinfo=LOCAL_TZ)
    end = datetime.combine(day, time.max.replace(microsecond=0), tzinfo=LOCAL_TZ)
    return int(start.timestamp()), int(end.timestamp())


def _local_today() -> date:
    """返回本地时区今天的日期。"""
    override = os.getenv("WECHAT_REPORT_TODAY", "").strip()
    if override:
        parsed = _parse_date_text(override)
        if parsed:
            return parsed
    return datetime.now(LOCAL_TZ).date()


def _format_range(start_time: int, end_time: int) -> str:
    """格式化时间戳范围。"""
    start = datetime.fromtimestamp(start_time, LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")
    end = datetime.fromtimestamp(end_time, LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")
    return f"{start} 至 {end}"


def _normalize_name(value: str) -> str:
    """规范化名称。"""
    return re.sub(r"\s+", "", str(value or "").strip()).lower()


def _safe_export_part(value: str) -> str:
    """生成适合文件名使用的导出名称片段。"""
    text = str(value or "").strip()
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f\s]+', "_", text)
    return text.strip("_") or "wechat_chat"


def _has_any(text: str, *needles: str) -> bool:
    """判断指定条件是否成立。"""
    lowered = text.lower()
    return any(needle.lower() in lowered for needle in needles)
