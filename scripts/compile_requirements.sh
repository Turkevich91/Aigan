#!/bin/sh
set -eu

PYTHON_BIN="${PYTHON:-python3}"

"$PYTHON_BIN" -c '
import sys
if sys.version_info[:2] != (3, 12):
    raise SystemExit("requirements.txt must be compiled with Python 3.12")
'

export CUSTOM_COMPILE_COMMAND="./scripts/compile_requirements.sh"

exec "$PYTHON_BIN" -m piptools compile \
    --resolver=backtracking \
    --strip-extras \
    --generate-hashes \
    --output-file=requirements.txt \
    requirements.in
