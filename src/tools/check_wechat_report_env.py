from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from tools.wechat_normalizer.summary_renderer import _find_browser


DEFAULT_WECHAT_API_BASE = "http://127.0.0.1:10392"
DEFAULT_OUTPUT_ROOT = "src/tools/wechatOutput"
DEFAULT_REDIS_URL = "redis://127.0.0.1:6379/0"
DEFAULT_VAULT_HOST = "127.0.0.1"
DEFAULT_VAULT_PORT = 8765


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    detail: str


def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        description="Check environment readiness for the WeChat activity report workflow."
    )
    parser.add_argument(
        "--wechat-api",
        default=os.getenv("WECHAT_EXPORT_API_BASE", DEFAULT_WECHAT_API_BASE),
        help="WeChatDataAnalysis API base URL.",
    )
    parser.add_argument(
        "--contact-keyword",
        default=None,
        help="Optional contact keyword used to test /api/chat/contacts.",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path(os.getenv("WECHAT_REPORT_OUTPUT_ROOT", DEFAULT_OUTPUT_ROOT)),
        help="Local output root for generated report files.",
    )
    parser.add_argument(
        "--backend-output-dir",
        default=os.getenv("WECHAT_EXPORT_BACKEND_OUTPUT_DIR"),
        help="Output directory sent to the WeChatDataAnalysis backend.",
    )
    parser.add_argument(
        "--redis-url",
        default=os.getenv("REDIS_URL", DEFAULT_REDIS_URL),
        help="Redis URL used by the Agent service.",
    )
    parser.add_argument(
        "--vault-host",
        default=os.getenv("VAULT_HOST", DEFAULT_VAULT_HOST),
        help="Vault socket host.",
    )
    parser.add_argument(
        "--vault-port",
        type=int,
        default=int(os.getenv("VAULT_PORT", str(DEFAULT_VAULT_PORT))),
        help="Vault socket port.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON instead of text.",
    )
    return parser


def check_wechat_health(api_base: str) -> CheckResult:
    """检查 WeChatDataAnalysis 健康接口是否可访问。"""
    url = f"{api_base.rstrip('/')}/api/health"
    try:
        payload = _get_json(url)
    except Exception as exc:
        return CheckResult("wechat_api_health", "fail", str(exc))
    status = str(payload.get("status") or payload.get("ok") or "").strip()
    detail = json.dumps(payload, ensure_ascii=False)
    if status.lower() in {"healthy", "success", "true"} or payload.get("ok") is True:
        return CheckResult("wechat_api_health", "pass", detail)
    return CheckResult("wechat_api_health", "warn", detail)


def check_contacts_api(api_base: str, keyword: str | None) -> CheckResult:
    """用可选关键词检查联系人查询接口。"""
    if not keyword:
        return CheckResult("wechat_contacts", "skip", "未提供 --contact-keyword，跳过联系人查询。")
    params = urllib.parse.urlencode(
        {
            "keyword": keyword,
            "include_friends": "true",
            "include_groups": "true",
            "include_officials": "false",
        }
    )
    url = f"{api_base.rstrip('/')}/api/chat/contacts?{params}"
    try:
        payload = _get_json(url)
    except Exception as exc:
        return CheckResult("wechat_contacts", "fail", str(exc))
    contacts = payload.get("contacts") if isinstance(payload, dict) else None
    if isinstance(contacts, list):
        return CheckResult("wechat_contacts", "pass", f"返回 {len(contacts)} 个候选联系人。")
    return CheckResult("wechat_contacts", "warn", json.dumps(payload, ensure_ascii=False))


def check_dashscope_key() -> CheckResult:
    """检查 DashScope API key 是否已配置。"""
    if os.getenv("DASHSCOPE_API_KEY", "").strip():
        return CheckResult("dashscope_api_key", "pass", "DASHSCOPE_API_KEY 已配置。")
    return CheckResult("dashscope_api_key", "warn", "未配置 DASHSCOPE_API_KEY；LLM 活动提取会失败。")


def check_output_root(path: Path) -> CheckResult:
    """检查报告输出目录是否可写。"""
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path, prefix=".camv_check_", delete=True):
            pass
    except Exception as exc:
        return CheckResult("output_root", "fail", f"{path}: {exc}")
    return CheckResult("output_root", "pass", str(path.resolve()))


def check_backend_output_dir(value: str | None) -> CheckResult:
    """检查后端导出目录配置是否存在。"""
    if value and value.strip():
        return CheckResult(
            "backend_output_dir",
            "pass",
            "WECHAT_EXPORT_BACKEND_OUTPUT_DIR 已配置；该路径由 WeChatDataAnalysis 后端解释。",
        )
    return CheckResult(
        "backend_output_dir",
        "warn",
        "未配置 WECHAT_EXPORT_BACKEND_OUTPUT_DIR；远程导出可能使用后端默认目录。",
    )


def check_pdf_browser() -> CheckResult:
    """检查 HTML 转 PDF 依赖的浏览器是否可用。"""
    browser = _find_browser()
    if browser is None:
        return CheckResult("pdf_browser", "warn", "未找到 Chrome/Edge/Chromium；仍可生成 HTML。")
    return CheckResult("pdf_browser", "pass", str(browser))


def check_redis(redis_url: str) -> CheckResult:
    """检查 Redis 是否可连接。"""
    try:
        import redis
    except ModuleNotFoundError:
        return CheckResult("redis", "warn", "当前 Python 环境未安装 redis 包，跳过 Redis ping。")
    try:
        client = redis.Redis.from_url(redis_url, decode_responses=True)
        try:
            response = client.ping()
        finally:
            client.close()
    except Exception as exc:
        return CheckResult("redis", "fail", str(exc))
    if response:
        return CheckResult("redis", "pass", redis_url)
    return CheckResult("redis", "warn", f"Redis ping 返回异常值：{response!r}")


def check_vault_socket(host: str, port: int) -> CheckResult:
    """检查 vault socket 端口是否可连接。"""
    try:
        with socket.create_connection((host, int(port)), timeout=3):
            pass
    except Exception as exc:
        return CheckResult("vault_socket", "warn", f"{host}:{port} 不可连接：{exc}")
    return CheckResult("vault_socket", "pass", f"{host}:{port}")


def _get_json(url: str) -> dict[str, Any]:
    """读取 HTTP JSON 响应。"""
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(str(exc)) from exc
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except Exception as exc:
        raise RuntimeError("返回了无效 JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("返回的 JSON 不是对象")
    return payload


def print_text(results: list[CheckResult]) -> None:
    """以文本格式打印检查结果。"""
    labels = {"pass": "PASS", "warn": "WARN", "fail": "FAIL", "skip": "SKIP"}
    for result in results:
        label = labels.get(result.status, result.status.upper())
        print(f"[{label}] {result.name}: {result.detail}")


def main() -> None:
    """执行命令行入口。"""
    args = build_parser().parse_args()
    results = [
        check_wechat_health(args.wechat_api),
        check_contacts_api(args.wechat_api, args.contact_keyword),
        check_dashscope_key(),
        check_output_root(args.output_root),
        check_backend_output_dir(args.backend_output_dir),
        check_pdf_browser(),
        check_redis(args.redis_url),
        check_vault_socket(args.vault_host, args.vault_port),
    ]
    if args.json:
        print(json.dumps([result.__dict__ for result in results], ensure_ascii=False, indent=2))
    else:
        print_text(results)
    if any(result.status == "fail" for result in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
