from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from tools.wechat_normalizer.llm_contract import iter_extraction_payloads


def main() -> None:
    """解析命令行参数并生成逐条 LLM 活动提取请求。"""
    parser = argparse.ArgumentParser(
        description="Build per-message LLM extraction payloads from normalized JSONL."
    )
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--minimum-score", type=float, default=0.3)
    parser.add_argument("--include-all", action="store_true")
    args = parser.parse_args()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with args.output.open("w", encoding="utf-8", newline="\n") as handle:
        for payload in iter_extraction_payloads(
            args.input,
            minimum_candidate_score=args.minimum_score,
            include_all=args.include_all,
        ):
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            handle.write("\n")
            count += 1

    print(json.dumps({"output": str(args.output), "records": count}, indent=2))


if __name__ == "__main__":
    main()
