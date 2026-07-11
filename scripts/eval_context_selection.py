from __future__ import annotations

import argparse
from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from context_selection_eval import (
    canonical_report_json,
    evaluate_context_selection_fixture,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the aggregate-only offline B0/B1/C1 source-selection evaluation."
    )
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument("--bootstrap-samples", type=int, default=10_000)
    args = parser.parse_args()
    if args.bootstrap_samples < 0 or args.bootstrap_samples > 100_000:
        parser.error("--bootstrap-samples must be between 0 and 100000")
    try:
        report = evaluate_context_selection_fixture(
            args.fixture,
            bootstrap_samples=args.bootstrap_samples,
        )
    # This is the final private-data boundary: unexpected parser or report
    # failures must remain type-only instead of escaping through a traceback.
    except Exception as exc:
        print(f"context-selection evaluation failed: {type(exc).__name__}", file=sys.stderr)
        return 2
    sys.stdout.write(canonical_report_json(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
