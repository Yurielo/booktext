#!/bin/bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PYTHON="${BOOKTEXT_TEST_PYTHON:-python3}"
exec "$PYTHON" "$ROOT/tools/owned_workspace.py" -- "$PYTHON" -m unittest discover -s tests -v "$@"
