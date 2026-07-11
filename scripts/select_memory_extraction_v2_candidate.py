#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memory_extraction_selection_v2 import (
    build_selection_attestation,
    current_clean_source_commit,
    selection_attestation_sha256,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a matrix-wide aggregate-only v2 development attestation."
    )
    parser.add_argument("reports", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if (
        not args.output.is_absolute()
        or output.is_relative_to(ROOT.resolve())
        or not output.parent.is_dir()
        or not os.access(output.parent, os.W_OK | os.X_OK)
        or output.exists()
    ):
        parser.error("--output must be a new writable external absolute path")
    reports = []
    for path in args.reports:
        resolved = path.resolve()
        if (
            not path.is_absolute()
            or resolved.is_relative_to(ROOT.resolve())
            or not resolved.is_file()
        ):
            parser.error("report inputs must be existing external absolute files")
        try:
            reports.append(json.loads(resolved.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            parser.error(f"cannot read aggregate report: {path.name}: {exc}")
    try:
        source_commit = current_clean_source_commit(ROOT)
        attestation = build_selection_attestation(
            reports,
            source_commit=source_commit,
        )
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        parser.error(str(exc))
    encoded = (
        json.dumps(attestation, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    try:
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        parser.error("attestation output already exists")
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    print(selection_attestation_sha256(attestation))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
