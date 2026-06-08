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
from tools.wechat_normalizer.wechat_export_api import (
    DEFAULT_MEDIA_KINDS,
    WeChatExportRequest,
    export_wechat_chat,
)


def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        description="Build a shareable WeChat activity report from a JSON export."
    )
    parser.add_argument(
        "--input",
        type=Path,
        help="WeChat export directory containing manifest.json and conversations/.",
    )
    parser.add_argument(
        "--wechat-api",
        default=None,
        help="WeChatDataAnalysis API base URL, for example http://127.0.0.1:10392.",
    )
    parser.add_argument(
        "--account",
        default=None,
        help="WeChatDataAnalysis account directory name used for API export.",
    )
    parser.add_argument(
        "--username",
        action="append",
        default=[],
        help="Conversation username to export. Repeat for multiple conversations.",
    )
    parser.add_argument(
        "--start-time",
        type=int,
        default=None,
        help="Export start time as Unix seconds.",
    )
    parser.add_argument(
        "--end-time",
        type=int,
        default=None,
        help="Export end time as Unix seconds.",
    )
    parser.add_argument(
        "--export-name",
        default=None,
        help="Export file/directory name; .zip suffix is optional.",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("src/tools/wechatOutput"),
        help="Local directory for downloaded and extracted API exports.",
    )
    parser.add_argument(
        "--backend-output-dir",
        default=None,
        help="Optional absolute output_dir sent to WeChatDataAnalysis backend.",
    )
    parser.add_argument(
        "--no-media",
        action="store_true",
        help="Do not ask WeChatDataAnalysis to package media files.",
    )
    parser.add_argument(
        "--privacy-mode",
        action="store_true",
        help="Enable WeChatDataAnalysis privacy_mode during API export.",
    )
    parser.add_argument(
        "--export-timeout",
        type=int,
        default=600,
        help="Seconds to wait for the WeChatDataAnalysis export job.",
    )
    parser.add_argument(
        "--export-poll-interval",
        type=float,
        default=1.0,
        help="Seconds between WeChatDataAnalysis export status polls.",
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
    """编排本地微信活动报告生成流水线。"""
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


def build_report_from_wechat_api(
    *,
    api_base: str,
    account: str | None,
    usernames: list[str],
    start_time: int | None,
    end_time: int | None,
    export_name: str | None,
    output_root: Path,
    backend_output_dir: str | None = None,
    include_media: bool = True,
    privacy_mode: bool = False,
    timeout_seconds: int = 600,
    poll_interval_seconds: float = 1.0,
    model_name: str | None = None,
    minimum_score: float = 0.3,
    include_all: bool = False,
    skip_extract: bool = False,
    dry_run_llm: bool = False,
    make_pdf: bool = True,
    timezone_offset: str = "+08:00",
    user_memories: list[str] | None = None,
) -> dict[str, Any]:
    """调用 WeChatDataAnalysis 导出会话并生成报告。"""
    export_result = export_wechat_chat(
        WeChatExportRequest(
            api_base=api_base,
            account=account,
            usernames=usernames,
            start_time=start_time,
            end_time=end_time,
            export_name=export_name,
            output_root=output_root,
            backend_output_dir=backend_output_dir,
            include_media=include_media,
            media_kinds=DEFAULT_MEDIA_KINDS,
            privacy_mode=privacy_mode,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
    )
    report = build_report(
        export_result.export_dir,
        model_name=model_name,
        minimum_score=minimum_score,
        include_all=include_all,
        skip_extract=skip_extract,
        dry_run_llm=dry_run_llm,
        make_pdf=make_pdf,
        timezone_offset=timezone_offset,
        user_memories=user_memories,
    )
    report["wechat_export"] = export_result.to_dict()
    return report


def _count_jsonl_records(path: Path) -> int:
    """统计 JSONL 文件中的记录数。"""
    with path.open("r", encoding="utf-8-sig") as handle:
        return sum(1 for line in handle if line.strip())


def main() -> None:
    """执行命令行入口。"""
    args = build_parser().parse_args()
    if args.wechat_api:
        result = build_report_from_wechat_api(
            api_base=args.wechat_api,
            account=args.account,
            usernames=args.username,
            start_time=args.start_time,
            end_time=args.end_time,
            export_name=args.export_name,
            output_root=args.output_root,
            backend_output_dir=args.backend_output_dir,
            include_media=not args.no_media,
            privacy_mode=args.privacy_mode,
            timeout_seconds=args.export_timeout,
            poll_interval_seconds=args.export_poll_interval,
            model_name=args.model,
            minimum_score=args.minimum_score,
            include_all=args.include_all,
            skip_extract=args.skip_extract,
            dry_run_llm=args.dry_run_llm,
            make_pdf=not args.no_pdf,
            timezone_offset=args.timezone_offset,
            user_memories=args.user_memory,
        )
    else:
        if args.input is None:
            raise SystemExit("Either --input or --wechat-api is required.")
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
