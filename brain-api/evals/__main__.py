"""CLI entrypoint: ``python -m evals`` from the ``brain-api`` directory."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from evals.runner import DATASET_DIR, SUITES, format_report, run_suites


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evals",
        description="Run the extraction-quality eval suites against the live LLMs.",
    )
    parser.add_argument(
        "--suite",
        action="append",
        choices=sorted(SUITES),
        help="suite(s) to run (repeatable); default: all",
    )
    parser.add_argument(
        "--dataset-dir", type=Path, default=DATASET_DIR, help="override the dataset directory"
    )
    parser.add_argument("--json", action="store_true", help="emit a JSON report to stdout")
    args = parser.parse_args()

    reports = asyncio.run(run_suites(args.suite, args.dataset_dir))
    if args.json:
        print(json.dumps([r.as_dict() for r in reports], indent=2))
    else:
        print(format_report(reports))
    return 0 if all(r.passed for r in reports) else 1


if __name__ == "__main__":
    sys.exit(main())
