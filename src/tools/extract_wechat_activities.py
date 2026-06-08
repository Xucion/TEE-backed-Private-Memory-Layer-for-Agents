from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from tools.wechat_normalizer.activity_extractor import extract_activities_from_jsonl


def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        description="Run LLM extraction over normalized WeChat messages."
    )
    parser.add_argument(
        "--input",
        required=True,
        type=Path,
        help="normalized_messages.jsonl produced by normalize_wechat_export.py.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output JSONL path; defaults to <input-dir>/extracted_activities.jsonl.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Tongyi model name; defaults to TONGYI_MODEL or qwen-turbo.",
    )
    parser.add_argument(
        "--minimum-score",
        type=float,
        default=0.3,
        help="Minimum group candidate score to send to the LLM.",
    )
    parser.add_argument(
        "--include-all",
        action="store_true",
        help="Send all text groups to the LLM regardless of candidate score.",
    )
    parser.add_argument(
        "--dry-run-payloads",
        type=Path,
        help="Write model request payloads instead of calling the LLM.",
    )
    return parser


def main() -> None:
    """执行命令行入口。"""
    args = build_parser().parse_args()
    input_path = args.input.resolve()
    output_path = (
        args.output.resolve()
        if args.output
        else input_path.with_name("extracted_activities.jsonl")
    )
    dry_run_payloads = (
        args.dry_run_payloads.resolve()
        if args.dry_run_payloads
        else None
    )

    count = extract_activities_from_jsonl(
        input_path,
        output_path,
        model_name=args.model,
        minimum_candidate_score=args.minimum_score,
        include_all=args.include_all,
        dry_run_payloads=dry_run_payloads,
    )
    print(
        json.dumps(
            {
                "input": str(input_path),
                "output": str(output_path),
                "activities": count,
                "dry_run_payloads": (
                    str(dry_run_payloads) if dry_run_payloads else None
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
