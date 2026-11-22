#!/bin/bash
# Run selector.py on ONE patch with BR_REPO pointing at the CLEAN sqlglot tree
# (the way the real scorer runs it). Prints selected count + full selector log.
# Usage: sel_one.sh PATCH_FILE
set -e
PATCH="$1"
HOME_DIR="$HOME"
D=$(mktemp -d)
mkdir -p "$D/commits"
cp "$PATCH" "$D/commits/"
cp "$HOME_DIR/work/tests.json" "$D/tests.json"
export PYTHONPATH="$HOME_DIR/sq"
export BR_REPO="$HOME_DIR/sq"
export BR_DATA="$D"
export SKIP_INTEGRATION=0
cd "$HOME_DIR/sq"
echo "=== selector stdout (selected counts) ==="
"$HOME_DIR/venv/bin/python" "$HOME_DIR/repo/selector.py" 2>"$D/err.txt" \
  | "$HOME_DIR/venv/bin/python" -c 'import sys,json
for l in sys.stdin:
    o=json.loads(l)
    print(o["commit"], "selected", sum(len(w) for w in o["workers"]))'
echo "=== selector stderr (log) ==="
cat "$D/err.txt"
rm -rf "$D"
