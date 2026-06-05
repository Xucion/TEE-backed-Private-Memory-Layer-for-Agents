import argparse
import sys
from pathlib import Path

# Support running this file directly via `python src/client/chat_cli.py`.
SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from client.agent_client import AgentClientError, ConfidentialAgentClient


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Confidential Agent Memory Vault 用户端"
    )
    parser.add_argument(
        "--user",
        required=True,
        help="用户 ID，例如 alice",
    )
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:8000",
        help="Agent API 地址",
    )
    parser.add_argument(
        "--session",
        help="对话 session ID；不提供时自动生成",
    )
    parser.add_argument(
        "--key-file",
        help="用户 Fernet key 文件；默认保存在 ~/.config/confidential-agent-memory-vault/keys/",
    )
    return parser


def main() -> None:
    args = _build_parser().parse_args()

    try:
        client = ConfidentialAgentClient(
            user_id=args.user,
            api_base_url=args.url,
            session_id=args.session,
            key_file=args.key_file,
        )
        client.provision()
    except AgentClientError as exc:
        raise SystemExit(f"客户端初始化失败: {exc}") from exc

    print(f"已连接 Agent，user_id={client.user_id}")
    print(f"session_id={client.session_id}")
    print(f"用户密钥文件: {client.key_file}")
    print("输入 exit 或 quit 退出。")

    while True:
        try:
            message = input("\n用户: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            return

        if message.lower() in {"exit", "quit"}:
            print("再见。")
            return
        if not message:
            continue

        try:
            reply = client.chat(message)
        except AgentClientError as exc:
            print(f"请求失败: {exc}")
            continue

        print(f"助手: {reply}")


if __name__ == "__main__":
    main()
