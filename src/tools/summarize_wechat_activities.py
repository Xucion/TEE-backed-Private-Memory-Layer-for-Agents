from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from tools.wechat_normalizer.activity_summary import write_activity_summary


def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        description="Merge extracted WeChat activities into a structured summary."
    )
    parser.add_argument(
        "--input",
        required=True,
        type=Path,
        help="extracted_activities.jsonl produced by extract_wechat_activities.py.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output JSON path; defaults to <input-dir>/weekly_activity_summary.json.",
    )
    return parser


def main() -> None:
    """执行命令行入口。"""
    args = build_parser().parse_args()
    input_path = args.input.resolve()
    output_path = (
        args.output.resolve()
        if args.output
        else input_path.with_name("weekly_activity_summary.json")
    )
    summary = write_activity_summary(input_path, output_path)
    print(
        json.dumps(
            {
                "input": str(input_path),
                "output": str(output_path),
                "counts": summary["counts"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
