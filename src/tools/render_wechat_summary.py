from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from tools.wechat_normalizer.summary_renderer import render_summary_files


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render weekly WeChat activity summary as shareable HTML/PDF."
    )
    parser.add_argument(
        "--input",
        required=True,
        type=Path,
        help="weekly_activity_summary.json produced by summarize_wechat_activities.py.",
    )
    parser.add_argument(
        "--html-output",
        type=Path,
        help="Output HTML path; defaults to <input>.html.",
    )
    parser.add_argument(
        "--pdf-output",
        type=Path,
        help="Output PDF path; defaults to <input>.pdf.",
    )
    parser.add_argument(
        "--no-pdf",
        action="store_true",
        help="Only write HTML and skip PDF export.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    result = render_summary_files(
        args.input,
        html_path=args.html_output,
        pdf_path=args.pdf_output,
        make_pdf=not args.no_pdf,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
