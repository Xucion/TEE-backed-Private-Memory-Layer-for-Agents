import os
import sys

from trusted.memory_store import load_all_memories


def _arg_or_env(index: int, env_name: str) -> str | None:
    if len(sys.argv) > index:
        return sys.argv[index]
    return os.getenv(env_name)


user_id = _arg_or_env(1, "USER_ID")
user_key = _arg_or_env(2, "USER_MEMORY_KEY")

if not user_id or not user_key:
    raise SystemExit(
        "用法: python3 decrypt.py <user_id> <fernet_key>\n"
        "也可以设置 USER_ID 和 USER_MEMORY_KEY 环境变量。"
    )

memories = load_all_memories(user_id, user_key.encode("ascii"))
for i, mem in enumerate(memories):
    print(f"[{i+1}] {mem['content']} | 分类: {mem['category']} | 敏感度: {mem['sensitivity']}")
