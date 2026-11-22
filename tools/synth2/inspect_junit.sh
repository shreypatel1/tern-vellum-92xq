#!/bin/bash
# Apply a patch to a fresh tree, run pytest junit, report how import/collection
# errors appear so truth reconstruction can map them to real test ids.
# Usage: inspect_junit.sh PATCH_FILE
set -e
PATCH="$1"
T=$(mktemp -d)
cp -a "$HOME/sq/." "$T/"
git apply --unsafe-paths -p1 --directory "$T" "$PATCH"
export PYTHONPATH="$T"
export SKIP_INTEGRATION=0
"$HOME/venv/bin/python" -m pytest "$T/tests" -p xdist -n 16 -q --no-header \
  -o junit_family=xunit2 --junit-xml "$T/j.xml" --timeout 90 >/dev/null 2>&1 || true
"$HOME/venv/bin/python" - "$T/j.xml" <<'PY'
import sys, xml.etree.ElementTree as ET
t = ET.parse(sys.argv[1])
tcs = list(t.iter("testcase"))
err = [tc for tc in tcs if any(c.tag == "error" for c in tc)]
fail = [tc for tc in tcs if any(c.tag == "failure" for c in tc)]
print("n testcases:", len(tcs))
print("n with <error>:", len(err), " n with <failure>:", len(fail))
for tc in err[:4]:
    print("  ERR classname=", repr(tc.get("classname")), "name=", repr(tc.get("name")))
for tc in fail[:4]:
    print("  FAIL classname=", repr(tc.get("classname")), "name=", repr(tc.get("name")))
PY
rm -rf "$T"
