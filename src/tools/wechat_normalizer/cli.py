from __future__ import annotations

import argparse
import json
from pathlib import Path

from .normalizer import normalize_export, write_result
from .preferences import profile_from_memories


def build_parser() -> argparse.ArgumentParser:
    """构建微信导出标准化工具的命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        description="Normalize a JSON WeChat export for downstream LLM extraction."
    )
    parser.add_argument(
        "--input",
        required=True,
        type=Path,
        help="Root directory containing manifest.json and conversations/.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output JSONL path; defaults to <input>/normalized_messages.jsonl.",
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="Optional normalization report path.",
    )
    parser.add_argument(
        "--timezone-offset",
        default="+08:00",
        help="Local timezone offset used for display timestamps (default: +08:00).",
    )
    parser.add_argument(
        "--user-memory",
        action="append",
        default=[],
        help="Optional preference memory used to preview recommendation scores.",
    )
    return parser


def main() -> None:
    """执行微信导出标准化并输出 JSONL 与统计报告。"""
    args = build_parser().parse_args()
    input_path = args.input.resolve()
    output_path = (
        args.output.resolve()
        if args.output
        else input_path / "normalized_messages.jsonl"
    )
    preference_profile = (
        profile_from_memories(args.user_memory)
        if args.user_memory
        else None
    )

    result = normalize_export(
        input_path,
        timezone_offset=args.timezone_offset,
        preference_profile=preference_profile,
    )
    write_result(result, output_path, args.report)

    print(
        json.dumps(
            {
                "output": str(output_path),
                "normalized_messages": result.report.normalized_messages,
                "forwarded_messages_expanded": (
                    result.report.forwarded_messages_expanded
                ),
                "media_not_analyzed": (
                    result.report.media_not_analyzed
                ),
                "context_groups": result.report.context_groups,
                "warnings": result.report.warnings,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
