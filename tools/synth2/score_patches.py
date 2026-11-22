#!/usr/bin/env python3
"""Score the selector against synthetic patches on the EC2 box.

For each patch:
  1. apply it to a fresh copy of the sqlglot tree
  2. run the full suite -> failing set; truth = failing_with_patch - baseline_failing
  3. run selector.py (BR_REPO=patched tree, BR_DATA=one-patch dir) -> selected test ids
  4. miss = any truth id NOT in selected ids

Everything runs under ~/work. Uses all cores for the pytest runs.

Usage: score_patches.py PATCH_DIR BASELINE_JSON OUT_JSON
Env:   PYTHONPATH(selector repo not needed here), BR_... set per-call internally
"""
import sys, os, json, shutil, subprocess, tempfile, time

HOME = os.path.expanduser("~")
REPO = os.path.join(HOME, "repo")          # selector code
SQ_CLEAN = os.path.join(HOME, "sq")        # pristine sqlglot tree
VENV_PY = os.path.join(HOME, "venv/bin/python")
RUN_TRUTH = os.path.join(REPO, "tools/synth2/run_truth.py")

def full_suite(sq_tree):
    """Return set of failing test ids for a given sqlglot tree."""
    out = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
    env = dict(os.environ)
    env["PYTHONPATH"] = sq_tree
    env["SKIP_INTEGRATION"] = "0"
    env["BR_TESTS_JSON"] = os.path.join(HOME, "work", "tests.json")
    subprocess.run([VENV_PY, RUN_TRUTH, out], env=env, check=True,
                   stdout=subprocess.DEVNULL)
    data = json.load(open(out))
    os.unlink(out)
    return {k for k, v in data["results"].items() if v == "fail"}

def selector_select(patch_path, tests_json):
    """Run selector.py on one patch; return set of selected test ids.

    The selector must run against the CLEAN sqlglot tree (SQ_CLEAN): analyze.Change
    diffs the patch text against that tree, exactly as the real scorer does. The
    patched tree is only used for the pytest truth run, never for the selector.
    """
    data_dir = tempfile.mkdtemp(prefix="brdata_")
    os.makedirs(os.path.join(data_dir, "commits"))
    cid = os.path.basename(patch_path)[:-6]
    shutil.copy(patch_path, os.path.join(data_dir, "commits", cid + ".patch"))
    shutil.copy(tests_json, os.path.join(data_dir, "tests.json"))
    tests = json.load(open(tests_json))["tests"]
    num2id = {t["number"]: t["id"] for t in tests}
    env = dict(os.environ)
    env["BR_REPO"] = SQ_CLEAN
    env["BR_DATA"] = data_dir
    env["PYTHONPATH"] = SQ_CLEAN
    env["SKIP_INTEGRATION"] = "0"
    proc = subprocess.run([VENV_PY, os.path.join(REPO, "selector.py")],
                          env=env, capture_output=True, text=True, cwd=SQ_CLEAN)
    shutil.rmtree(data_dir, ignore_errors=True)
    selected = set()
    crashed = proc.returncode != 0
    line_for_cid = None
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("commit") == cid:
            line_for_cid = obj
            for worker in obj["workers"]:
                for num in worker:
                    selected.add(num2id.get(num))
    return selected, crashed, (line_for_cid is None), proc.stderr[-1500:]

def main():
    patch_dir, baseline_json, out_json = sys.argv[1], sys.argv[2], sys.argv[3]
    baseline = {k for k, v in json.load(open(baseline_json))["results"].items() if v == "fail"}
    manifest = json.load(open(os.path.join(patch_dir, "manifest.json")))
    tests_json = os.path.join(HOME, "work", "tests.json")
    results = []
    for entry in manifest:
        name = entry["name"]
        patch_path = os.path.join(patch_dir, name + ".patch")
        t0 = time.time()
        # fresh patched tree
        tree = tempfile.mkdtemp(prefix=f"sq_{name}_")
        shutil.rmtree(tree); shutil.copytree(SQ_CLEAN, tree)
        ap = subprocess.run(["git", "apply", "--unsafe-paths", "-p1",
                             "--directory", tree, patch_path],
                            capture_output=True, text=True)
        applied = ap.returncode == 0
        if not applied:
            # try patch(1)
            ap2 = subprocess.run(["patch", "-p1", "-d", tree, "-i", patch_path],
                                 capture_output=True, text=True)
            applied = ap2.returncode == 0
        rec = {"name": name, "cat": entry["cat"], "behavior": entry["behavior"],
               "applied": applied}
        if not applied:
            rec["error"] = "patch did not apply: " + ap.stderr[-300:]
            results.append(rec)
            shutil.rmtree(tree, ignore_errors=True)
            print(f"[{name}] PATCH FAILED TO APPLY")
            continue
        failing = full_suite(tree)
        truth = failing - baseline
        selected, crashed, no_line, serr = selector_select(patch_path, tests_json)
        misses = sorted(t for t in truth if t not in selected)
        rec.update({
            "n_truth": len(truth),
            "truth": sorted(truth),
            "n_selected": len(selected),
            "selector_crashed": crashed,
            "selector_no_line": no_line,
            "n_misses": len(misses),
            "misses": misses,
            "seconds": round(time.time() - t0, 1),
        })
        if crashed or no_line:
            rec["selector_stderr"] = serr
        results.append(rec)
        shutil.rmtree(tree, ignore_errors=True)
        flag = "MISS" if misses else ("CRASH" if crashed else "ok")
        print(f"[{name}] truth={len(truth)} sel={len(selected)} misses={len(misses)} {flag} ({rec['seconds']}s)")
    with open(out_json, "w") as f:
        json.dump(results, f, indent=2)
    total_miss = sum(r.get("n_misses", 0) for r in results)
    crashes = sum(1 for r in results if r.get("selector_crashed"))
    print(f"\n=== {len(results)} patches, {total_miss} total missed tests, {crashes} selector crashes ===")

if __name__ == "__main__":
    main()
