from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from tools.wechat_normalizer.activity_extractor import extract_activities_from_jsonl
from tools.wechat_normalizer.activity_summary import write_activity_summary
from tools.wechat_normalizer.normalizer import normalize_export, write_result
from tools.wechat_normalizer.preferences import profile_from_memories
from tools.wechat_normalizer.summary_renderer import render_summary_files


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a shareable WeChat activity report from a JSON export."
    )
    parser.add_argument(
        "--input",
        required=True,
        type=Path,
        help="WeChat export directory containing manifest.json and conversations/.",
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
        help="Minimum group candidate score sent to the LLM; default filters chatter.",
    )
    parser.add_argument(
        "--include-all",
        action="store_true",
        help="Debug option: send all text groups to the LLM, including likely chatter.",
    )
    extract_group = parser.add_mutually_exclusive_group()
    extract_group.add_argument(
        "--skip-extract",
        action="store_true",
        help="Do not call the LLM; reuse existing extracted_activities.jsonl.",
    )
    extract_group.add_argument(
        "--dry-run-llm",
        action="store_true",
        help="Write LLM request payloads and stop before calling the LLM.",
    )
    parser.add_argument(
        "--no-pdf",
        action="store_true",
        help="Generate HTML but skip PDF export.",
    )
    parser.add_argument(
        "--timezone-offset",
        default="+08:00",
        help="Local timezone offset for normalization timestamps.",
    )
    parser.add_argument(
        "--user-memory",
        action="append",
        default=[],
        help="Optional preference memory used during normalization preview.",
    )
    return parser


def build_report(
    export_dir: Path,
    *,
    model_name: str | None = None,
    minimum_score: float = 0.3,
    include_all: bool = False,
    skip_extract: bool = False,
    dry_run_llm: bool = False,
    make_pdf: bool = True,
    timezone_offset: str = "+08:00",
    user_memories: list[str] | None = None,
) -> dict[str, Any]:
    export_root = export_dir.resolve()
    normalized_path = export_root / "normalized_messages.jsonl"
    normalization_report_path = export_root / "normalization_report.json"
    extracted_path = export_root / "extracted_activities.jsonl"
    dry_run_payloads_path = export_root / "activity_payloads.dryrun.jsonl"
    summary_path = export_root / "weekly_activity_summary.json"

    preference_profile = (
        profile_from_memories(user_memories)
        if user_memories
        else None
    )
    normalized = normalize_export(
        export_root,
        timezone_offset=timezone_offset,
        preference_profile=preference_profile,
    )
    write_result(
        normalized,
        normalized_path,
        normalization_report_path,
    )

    result: dict[str, Any] = {
        "input": str(export_root),
        "normalized_messages": normalized.report.normalized_messages,
        "context_groups": normalized.report.context_groups,
        "normalization_warnings": normalized.report.warnings,
        "normalized_output": str(normalized_path),
        "normalization_report": str(normalization_report_path),
        "minimum_score": minimum_score,
        "include_all": include_all,
        "llm_called": False,
    }

    if dry_run_llm:
        extract_activities_from_jsonl(
            normalized_path,
            extracted_path,
            model_name=model_name,
            minimum_candidate_score=minimum_score,
            include_all=include_all,
            dry_run_payloads=dry_run_payloads_path,
        )
        result.update(
            {
                "dry_run_payloads": str(dry_run_payloads_path),
                "stopped_after": "dry_run_llm",
            }
        )
        return result

    if skip_extract:
        if not extracted_path.is_file():
            raise FileNotFoundError(
                f"--skip-extract requires existing file: {extracted_path}"
            )
        extracted_count = _count_jsonl_records(extracted_path)
        result["extract_mode"] = "skipped"
    else:
        extracted_count = extract_activities_from_jsonl(
            normalized_path,
            extracted_path,
            model_name=model_name,
            minimum_candidate_score=minimum_score,
            include_all=include_all,
        )
        result["extract_mode"] = "llm"
        result["llm_called"] = True

    summary = write_activity_summary(extracted_path, summary_path)
    render_result = render_summary_files(
        summary_path,
        make_pdf=make_pdf,
    )
    result.update(
        {
            "extracted_activities": extracted_count,
            "extracted_output": str(extracted_path),
            "summary_output": str(summary_path),
            "summary_counts": summary["counts"],
            "html_output": render_result["html"],
            "pdf_output": render_result["pdf"],
            "pdf_created": render_result["pdf_created"],
            "pdf_error": render_result["pdf_error"],
        }
    )
    return result


def _count_jsonl_records(path: Path) -> int:
    with path.open("r", encoding="utf-8-sig") as handle:
        return sum(1 for line in handle if line.strip())


def main() -> None:
    args = build_parser().parse_args()
    result = build_report(
        args.input,
        model_name=args.model,
        minimum_score=args.minimum_score,
        include_all=args.include_all,
        skip_extract=args.skip_extract,
        dry_run_llm=args.dry_run_llm,
        make_pdf=not args.no_pdf,
        timezone_offset=args.timezone_offset,
        user_memories=args.user_memory,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
