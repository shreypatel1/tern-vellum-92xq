#!/usr/bin/env python3
"""Run the full sqlglot test suite and record per-test pass/fail.

Outcomes follow pytest's rules, which match the repo harness: a test counts as
FAILING if it errors or fails (including a failing subtest, unexpected success,
or timeout). skip / xfail / xpass->(treated by pytest as xpassed, non-failing
unless strict) do not count as failing.

We avoid extra plugins: use the builtin junit-xml reporter, which records one
<testcase> per test with <failure>/<error> children, and parse that.

Usage:
    run_truth.py OUT_JSON [pytest args...]
Env: PYTHONPATH must include the sqlglot tree; SKIP_INTEGRATION=0.
"""
import subprocess, sys, os, json, xml.etree.ElementTree as ET, tempfile

def run(out_json, extra):
    repo_sq = os.environ["PYTHONPATH"].split(os.pathsep)[0]
    tests_dir = os.path.join(repo_sq, "tests")
    xml = tempfile.NamedTemporaryFile(suffix=".xml", delete=False).name
    # 8 workers (not all 16) keeps peak memory in check: a pathological patch
    # (e.g. copy() returning self) can make many tests balloon and OOM-kill xdist
    # workers if 16 run at once. A short per-test timeout bounds hangs, and an
    # overall subprocess timeout guarantees the batch never stalls on one patch.
    workers = min(8, os.cpu_count() or 2)
    per_test_timeout = os.environ.get("BR_TRUTH_TEST_TIMEOUT", "45")
    overall_timeout = float(os.environ.get("BR_TRUTH_OVERALL_TIMEOUT", "300"))
    cmd = [sys.executable, "-m", "pytest", tests_dir,
           "-p", "xdist", "-n", str(workers),
           "-q", "--no-header",
           "-o", "junit_family=xunit2",
           "--junit-xml", xml,
           "--timeout", per_test_timeout,
           ] + list(extra)
    env = dict(os.environ)
    env.setdefault("SKIP_INTEGRATION", "0")
    timed_out = False
    try:
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True,
                              timeout=overall_timeout)
        rc = proc.returncode
        serr_tail = proc.stderr[-2000:]
        sout_tail = proc.stdout[-2000:]
    except subprocess.TimeoutExpired as e:
        timed_out = True
        rc = -1
        serr_tail = (e.stderr or b"")[-2000:] if isinstance(e.stderr, bytes) else str(e.stderr)[-2000:]
        sout_tail = (e.stdout or b"")[-2000:] if isinstance(e.stdout, bytes) else str(e.stdout)[-2000:]
    # parse junit (may be partial/empty if timed out)
    results = {}
    # Known real test ids (from the index) let us expand a module collection
    # error to that module's tests. Loaded from tests.json next to this run.
    all_ids = []
    tj = os.environ.get("BR_TESTS_JSON")
    if tj and os.path.exists(tj):
        all_ids = [t["id"] for t in json.load(open(tj))["tests"]]
    by_module = {}
    for tid in all_ids:
        mod = tid.split("::")[0]  # tests/dialects/test_x.py
        by_module.setdefault(mod, []).append(tid)

    tree = ET.parse(xml)
    for tc in tree.iter("testcase"):
        file = tc.get("classname", "")
        name = tc.get("name", "")
        is_err = any(c.tag == "error" for c in tc)
        is_fail = any(c.tag in ("failure", "error") for c in tc)
        parts = file.split(".")
        if len(parts) < 3 or parts[0] != "tests" or not name:
            # Collection/import error: classname empty, name is a dotted module
            # path like tests.dialects.test_athena. If this testcase is an <error>,
            # the whole module failed -> every test in it is failing.
            if is_err and name:
                mod = name.replace(".", "/") + ".py"
                for tid in by_module.get(mod, []):
                    results[tid] = "fail"
            continue
        cls = parts[-1]
        modpath = "/".join(parts[:-1]) + ".py"
        tid = f"{modpath}::{cls}::{name}"
        results[tid] = "fail" if is_fail else "pass"
    os.unlink(xml)
    summary = {
        "returncode": proc.returncode,
        "n": len(results),
        "n_fail": sum(1 for v in results.values() if v == "fail"),
        "results": results,
        "stdout_tail": proc.stdout[-2000:],
        "stderr_tail": proc.stderr[-2000:],
    }
    with open(out_json, "w") as f:
        json.dump(summary, f)
    print(f"collected {summary['n']} tests, {summary['n_fail']} failing -> {out_json}")
    return 0

if __name__ == "__main__":
    sys.exit(run(sys.argv[1], sys.argv[2:]))
