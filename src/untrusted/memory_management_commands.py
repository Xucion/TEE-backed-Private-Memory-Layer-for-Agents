import logging
import os
import sys
from pathlib import Path
from typing import Any

# Support running this file directly via
# `python src/untrusted/memory_management_commands.py`.
SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from interface.vault_api import (
    VaultApiError,
    open_user_vault_session,
    secure_forget_user_memories,
    secure_list_user_memories,
)


VALID_STATUSES = {"active", "superseded", "forgotten", "expired"}
DEFAULT_STATUS = "active"

logging.basicConfig(
    level=os.getenv("MEMORY_COMMAND_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [MemoryCommands] %(message)s",
)
logger = logging.getLogger(__name__)


def _print_help() -> None:
    """打印记忆管理命令帮助。"""
    print(
        """
Memory management commands:
  /memories              List active memories
  /memories all          List all memories
  /memories <status>     List active, superseded, forgotten, or expired memories
  /forgotten             List forgotten memories
  /forget <id> [id...]   Soft-delete one or more memories
  /help                  Show this help
  exit | quit            Exit
""".strip()
    )


def _format_memory(memory: dict[str, Any]) -> str:
    """格式化单条记忆用于终端展示。"""
    memory_id = str(memory.get("id", "")).strip() or "<missing-id>"
    content = str(memory.get("content", "")).strip()
    memory_type = str(memory.get("memory_type", "other")).strip()
    status = str(memory.get("status", "active")).strip()
    confidence = memory.get("confidence")
    evidence_count = memory.get("evidence_count", 1)
    access_count = memory.get("access_count", 0)
    updated_at = memory.get("updated_at") or memory.get("created_at") or ""

    if isinstance(confidence, (int, float)):
        confidence_text = f"{float(confidence):.2f}"
    else:
        confidence_text = str(confidence or "n/a")

    return (
        f"- id: {memory_id}\n"
        f"  status: {status}  type: {memory_type}  confidence: {confidence_text}\n"
        f"  evidence_count: {evidence_count}  access_count: {access_count}\n"
        f"  updated_at: {updated_at}\n"
        f"  content: {content}"
    )


def _print_memories(memories: list[dict[str, Any]]) -> None:
    """打印记忆列表或空结果提示。"""
    if not memories:
        print("没有找到匹配的 memory。")
        return

    for memory in memories:
        print(_format_memory(memory))


def _list_memories(session: dict[str, Any], user_id: str, status: str | None) -> None:
    """列出记忆列表。"""
    memories = secure_list_user_memories(session, user_id, status=status)
    title = "all" if status is None else status
    print(f"\n{title} memories: {len(memories)}")
    _print_memories(memories)


def _forget_memories(session: dict[str, Any], user_id: str, memory_ids: list[str]) -> None:
    """遗忘记忆列表。"""
    clean_ids = [memory_id.strip() for memory_id in memory_ids if memory_id.strip()]
    if not clean_ids:
        print("用法：/forget <memory_id> [memory_id...]")
        return

    forgotten_count = secure_forget_user_memories(session, user_id, clean_ids)
    print(f"已遗忘 {forgotten_count} 条 memory。")


def _parse_list_status(parts: list[str]) -> str | None:
    """解析list、状态。"""
    if len(parts) == 1:
        return DEFAULT_STATUS

    status = parts[1].strip().lower()
    if status == "all":
        return None
    if status in VALID_STATUSES:
        return status

    raise ValueError(f"不支持的 status: {status}")


def _open_session() -> dict[str, Any] | None:
    """打开安全会话。"""
    user_id = os.getenv("VAULT_USER_ID", "default_user")
    user_key = os.getenv("USER_MEMORY_KEY")

    if not user_key:
        print("未设置 USER_MEMORY_KEY，本次只能管理临时 key 对应的 memory。")

    try:
        session = open_user_vault_session(user_id, user_key)
    except VaultApiError as exc:
        logger.exception("无法建立 vault 安全会话")
        print(f"Vault 安全会话建立失败: {exc}")
        return None

    print(f"Vault 安全信道已建立，当前 user_id: {session['user_id']}")
    return session


def main() -> None:
    """执行命令行入口。"""
    session = _open_session()
    if session is None:
        return

    user_id = str(session["user_id"])
    _print_help()

    while True:
        command = input("\nmemory> ").strip()
        if not command:
            continue
        if command.lower() in {"exit", "quit"}:
            print("再见。")
            return

        parts = command.split()
        action = parts[0].lower()

        try:
            if action in {"/help", "help"}:
                _print_help()
            elif action in {"/memories", "memories", "/list", "list"}:
                _list_memories(session, user_id, _parse_list_status(parts))
            elif action in {"/forgotten", "forgotten"}:
                _list_memories(session, user_id, "forgotten")
            elif action in {"/forget", "forget"}:
                _forget_memories(session, user_id, parts[1:])
            else:
                print("未知命令。输入 /help 查看可用命令。")
        except (VaultApiError, ValueError) as exc:
            print(f"命令失败: {exc}")


if __name__ == "__main__":
    main()
