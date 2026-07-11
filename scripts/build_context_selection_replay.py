from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from context_selection_replay import (
    ContextSelectionReplayError,
    PRIVATE_ROOT_RELATIVE,
    build_private_review_pool,
)


def _private_root(repo_root: Path) -> Path:
    data_root = (repo_root / "data").resolve()
    private_root = (repo_root / PRIVATE_ROOT_RELATIVE).resolve()
    try:
        private_root.relative_to(data_root)
    except ValueError as exc:
        raise ContextSelectionReplayError("private root escaped ignored data directory") from exc
    data_writable = private_root.exists() and os.access(private_root, os.W_OK)
    data_writable = data_writable or (not private_root.exists() and os.access(data_root, os.W_OK))
    if data_writable:
        git = shutil.which("git")
        if git is not None:
            ignored = subprocess.run(
                [git, "check-ignore", "--quiet", "--", PRIVATE_ROOT_RELATIVE.as_posix()],
                cwd=repo_root,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if ignored.returncode != 0:
                raise ContextSelectionReplayError("private root is not ignored by Git")
        else:
            ignore_lines = {
                line.strip()
                for line in (repo_root / ".gitignore").read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            }
            if "data/" not in ignore_lines and "/data/" not in ignore_lines:
                raise ContextSelectionReplayError("private root ignore contract is missing")
        return private_root

    data_home = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local/share")).resolve()
    fallback = (data_home / "aigan-context-selection-v1").resolve()
    try:
        fallback.relative_to(repo_root.resolve())
    except ValueError:
        return fallback
    raise ContextSelectionReplayError("private fallback cannot be inside the repository")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build the local-only #119 human-review pool from retained runtime evidence."
    )
    parser.add_argument("--lookback-days", type=int, default=30)
    parser.add_argument("--recent-limit", type=int, default=80)
    parser.add_argument("--reply-depth", type=int, default=8)
    parser.add_argument("--historical-output-window-seconds", type=int, default=180)
    args = parser.parse_args()
    if not 1 <= args.lookback_days <= 365:
        parser.error("--lookback-days must be between 1 and 365")
    if not 10 <= args.recent_limit <= 500:
        parser.error("--recent-limit must be between 10 and 500")
    if not 1 <= args.reply_depth <= 32:
        parser.error("--reply-depth must be between 1 and 32")
    if not 30 <= args.historical_output_window_seconds <= 300:
        parser.error("--historical-output-window-seconds must be between 30 and 300")

    repo_root = Path(__file__).resolve().parents[1]
    try:
        manifest = build_private_review_pool(
            database_path=repo_root / "data/aigan.sqlite3",
            private_root=_private_root(repo_root),
            lookback_days=args.lookback_days,
            recent_limit=args.recent_limit,
            reply_depth=args.reply_depth,
            historical_output_window_seconds=args.historical_output_window_seconds,
        )
    except ContextSelectionReplayError as exc:
        print(f"context-selection replay build failed: {exc}", file=sys.stderr)
        return 2
    # This is the final private-data boundary: row corruption and schema drift
    # must fail type-only instead of escaping through a traceback with payloads.
    except Exception as exc:
        print(f"context-selection replay build failed: {type(exc).__name__}", file=sys.stderr)
        return 2
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
