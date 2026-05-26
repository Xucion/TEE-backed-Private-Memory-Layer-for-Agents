import json
import logging
import os
import socketserver
import sys
import threading
from pathlib import Path
from typing import Any

# Support running this file directly via `python trusted/vault_server.py`.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from trusted.memory_retriever import retrieve
from trusted.memory_store import store_memories


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_REQUEST_BYTES = 1024 * 1024

_STORE_LOCK = threading.Lock()

logging.basicConfig(
    level=os.getenv("VAULT_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [VaultServer] %(message)s",
)
logger = logging.getLogger(__name__)


class VaultError(Exception):
    """预期的请求或验证失败，将返回给 vault 客户端。"""


def _success(data: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"ok": True, "data": data or {}}


def _failure(message: str) -> dict[str, Any]:
    return {"ok": False, "error": message}


def _coerce_positive_int(value: Any, default: int, max_value: int) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or value <= 0 or value > max_value:
        raise VaultError(f"期望正整数，且不超过 {max_value}")
    return value


def _coerce_threshold(value: Any, default: float) -> float:
    if value is None:
        return default
    if not isinstance(value, (int, float)):
        raise VaultError("期望数值类型的阈值")
    threshold = float(value)
    if threshold < 0.0 or threshold > 1.0:
        raise VaultError("期望阈值介于 0 和 1 之间")
    return threshold


def _validate_memory(mem: Any) -> dict[str, str]:
    if not isinstance(mem, dict):
        raise VaultError("每条记忆必须是一个对象")

    content = str(mem.get("content", "")).strip()
    category = str(mem.get("category", "other")).strip()
    sensitivity = str(mem.get("sensitivity", "low")).strip()

    if not content:
        raise VaultError("记忆内容不能为空")
    if category not in {"health", "preference", "business", "other"}:
        raise VaultError(f"不支持的记忆分类: {category}")
    if sensitivity not in {"high", "low"}:
        raise VaultError(f"不支持的记忆敏感度: {sensitivity}")

    return {
        "content": content,
        "category": category,
        "sensitivity": sensitivity,
    }


def _minimize_memories(memories: list[dict[str, Any]]) -> str:
    category_hints = {
        "health": "用户有健康相关的限制。提供保守的、注重安全的指导。",
        "business": "用户有重要的业务背景。回复相关话题时需要谨慎。",
        "preference": "用户有特定的偏好。在不暴露敏感细节的前提下考虑这些偏好。",
        "other": "用户有特殊的背景信息。谨慎使用，仅在相关时参考。",
    }

    minimized: list[str] = []
    for mem in memories:
        if mem.get("sensitivity") == "high":
            minimized.append(category_hints.get(str(mem.get("category")), category_hints["other"]))
            continue

        content = str(mem.get("content", "")).strip()
        if content:
            minimized.append(content)

    deduped = list(dict.fromkeys(minimized))
    return " ".join(deduped)


def handle_request(request: dict[str, Any]) -> dict[str, Any]:
    action = request.get("action")
    if action == "store":
        raw_memories = request.get("memories")
        if not isinstance(raw_memories, list):
            raise VaultError("store 操作需要提供 memories 列表")

        memories = [_validate_memory(mem) for mem in raw_memories]
        with _STORE_LOCK:
            stored_count = store_memories(memories)

        return _success({"stored_count": stored_count})

    if action == "retrieve":
        query = str(request.get("query", "")).strip()
        if not query:
            raise VaultError("retrieve 操作需要提供非空的查询内容")

        top_k = _coerce_positive_int(request.get("top_k"), default=3, max_value=20)
        threshold = _coerce_threshold(request.get("threshold"), default=0.4)
        with _STORE_LOCK:
            memories = retrieve(query, top_k=top_k, threshold=threshold)
        memory_context = _minimize_memories(memories)

        return _success(
            {
                "memory_context": memory_context,
                "retrieved_count": len(memories),
            }
        )

    raise VaultError(f"不支持的操作: {action}")


class VaultRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        try:
            raw = self.rfile.readline(MAX_REQUEST_BYTES + 1)
            if len(raw) > MAX_REQUEST_BYTES:
                raise VaultError("请求过大")
            if not raw:
                return

            request = json.loads(raw.decode("utf-8"))
            if not isinstance(request, dict):
                raise VaultError("请求必须是 JSON 对象")

            response = handle_request(request)
        except json.JSONDecodeError as exc:
            logger.warning("无效的 JSON 请求: %s", exc)
            response = _failure("无效的 JSON 请求")
        except VaultError as exc:
            logger.warning("请求被拒绝: %s", exc)
            response = _failure(str(exc))
        except Exception:
            logger.exception("未处理的 vault 服务器错误")
            response = _failure("vault 服务器内部错误")

        self.wfile.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))


class ThreadedVaultServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def run_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    with ThreadedVaultServer((host, port), VaultRequestHandler) as server:
        logger.info("Vault 服务器正在监听 %s:%s", host, port)
        server.serve_forever()


def main() -> None:
    host = os.getenv("VAULT_HOST", DEFAULT_HOST)
    port = int(os.getenv("VAULT_PORT", str(DEFAULT_PORT)))
    run_server(host=host, port=port)


if __name__ == "__main__":
    main()
