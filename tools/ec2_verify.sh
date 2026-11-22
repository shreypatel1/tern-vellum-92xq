#!/bin/bash
# Verify the test box environment: sqlglot import, pytest collection, repo tooling.
set -e
export PYTHONPATH="$HOME/sq"
export SKIP_INTEGRATION=0
cd "$HOME/sq"

echo "=== sqlglot import ==="
"$HOME/venv/bin/python" - <<'PY'
import sqlglot
print("import ok:", sqlglot.__file__)
print("dialects:", len(list(sqlglot.dialects.Dialects)))
PY

echo "=== pytest collection count ==="
"$HOME/venv/bin/python" -m pytest "$HOME/sq/tests" -q --collect-only 2>/dev/null | tail -2

echo "=== repo tools ==="
ls "$HOME/repo/tools"

echo "=== selector smoke (needs BR_REPO/BR_DATA) ==="
echo "selector.py present:"; ls -la "$HOME/repo/selector.py"
