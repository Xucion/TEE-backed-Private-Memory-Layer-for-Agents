import os
import sys
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from trusted.memory_store import load_all_memories


def _arg_or_env(index: int, env_name: str) -> str | None:
    # 输入命令行参数位置和环境变量名；输出字符串或 None；作用是优先读取参数再回退到环境变量。
    """读取命令行参数或环境变量中的配置值。"""
    if len(sys.argv) > index:
        return sys.argv[index]
    return os.getenv(env_name)


user_id = _arg_or_env(1, "USER_ID")
user_key = _arg_or_env(2, "USER_MEMORY_KEY")

if not user_id or not user_key:
    raise SystemExit(
        "用法: python3 scripts/decrypt_memories.py <user_id> <fernet_key>\n"
        "也可以设置 USER_ID 和 USER_MEMORY_KEY 环境变量。"
    )

memories = load_all_memories(user_id, user_key.encode("ascii"))
for i, mem in enumerate(memories):
    print(f"[{i+1}] {mem['content']} | 类型: {mem.get('memory_type', 'other')} | 敏感度: {mem['sensitivity']}")
    print(f"[{i+1}] {mem} ")
