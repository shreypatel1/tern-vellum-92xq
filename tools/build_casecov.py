"""Per-fixture-case coverage of the fixture-driven tests. usage: build_casecov.py REPO TESTS_JSON OUT NPROC

Every test in casecov.MODULES that loads fixture pairs and is worth >= MIN_SECONDS is run once
with its fixtures emptied ("base": the code outside the case loop) and once per case with only
that case left, each in a fresh fork of a parent that has imported everything but run nothing,
so a case's coverage includes the cold-cache paths it could take in any order.
Output (zlib pickle): {test_id: {"fx": {fixture: n_cases}, "base": {path: [lines]},
"m": {path: {line: {fixture: case bitmask}}}, "fail": [cases that failed alone]}}"""
import json
import os
import pickle
import sys
import zlib

MIN_SECONDS = 0.15

if __name__ != "__main__":
    raise SystemExit
REPO, TJ, OUT, NP = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO); sys.path.insert(0, HERE)
os.chdir(REPO); os.environ["SKIP_INTEGRATION"] = "0"
import harness, importlib, pkgutil  # noqa: E402
import sqlglot  # noqa: E402
import casecov  # noqa: E402
for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
    if not m.name.endswith("__main__"):
        try:
            importlib.import_module(m.name)
        except Exception:  # noqa
            pass
tests = json.load(open(TJ))["tests"]
cands = [t["id"] for t in tests if t["seconds"] >= MIN_SECONDS
         and t["id"].split("::")[0][:-3].replace("/", ".") in casecov.MODULES]
harness.import_all(cands)
harness.install_inline_pools()
casecov.install()
SRC = os.path.join(REPO, "sqlglot") + os.sep
TST = os.path.join(REPO, "tests") + os.sep
import gc  # noqa: E402
gc.collect(); gc.freeze()


def child(tid, filt, record, wfd):
    lines = set()
    mon = sys.monitoring
    TOOL = 3
    mon.use_tool_id(TOOL, "cov")

    def on_line(code, ln):
        fn = code.co_filename
        if fn.startswith(SRC) or fn.startswith(TST):
            lines.add((fn, ln))
        return mon.DISABLE

    def on_start(code, off):
        fn = code.co_filename
        if fn.startswith(SRC) or fn.startswith(TST):
            lines.add((fn, -code.co_firstlineno))
        return mon.DISABLE
    mon.register_callback(TOOL, mon.events.LINE, on_line)
    mon.register_callback(TOOL, mon.events.PY_START, on_start)
    casecov.FILTER.clear()
    casecov.FILTER.update(filt)
    casecov.LOADED.clear()
    cls, meth = harness.get_class(tid)
    cls.setUpClass()
    if record:
        mon.set_events(TOOL, mon.events.LINE | mon.events.PY_START)
    r = harness.Result()
    cls(meth).run(r)
    mon.set_events(TOOL, 0)
    out = {}
    for fn, ln in lines:
        out.setdefault(os.path.relpath(fn, REPO), []).append(ln)
    b = json.dumps({"failed": r.bad, "lines": out, "loaded": sorted(casecov.LOADED)}).encode()
    while b:
        n = os.write(wfd, b); b = b[n:]


def run_one(tid, filt, record=True):
    sys.stdout.flush(); sys.stderr.flush()
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        try:
            child(tid, filt, record, w)
        except BaseException:  # noqa
            import traceback; traceback.print_exc()
        os._exit(0)
    os.close(w)
    chunks = []
    while True:
        b = os.read(r, 1 << 20)
        if not b:
            break
        chunks.append(b)
    os.close(r); os.waitpid(pid, 0)
    return json.loads(b"".join(chunks))


def count(fx):
    return sum(1 for _ in casecov._installed[0](fx))


# which fixtures each candidate loads (one plain run, no recording)
fixtures = {}
for tid in cands:
    d = run_one(tid, {}, record=False)
    if d["loaded"] and not d["failed"]:
        fixtures[tid] = {f: count(f) for f in d["loaded"]}
print({t.rsplit("::", 1)[1]: v for t, v in fixtures.items()}, file=sys.stderr)

jobs = []
for tid, fx in fixtures.items():
    empty = {f: set() for f in fx}
    jobs.append((tid, None, None, empty))
    for f, n in fx.items():
        for k in range(n):
            jobs.append((tid, f, k, {**empty, f: {k}}))


def worker(idx):
    fd = os.open(f"{OUT}.{idx}", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    for j, (tid, f, k, filt) in enumerate(jobs):
        if j % NP != idx:
            continue
        d = run_one(tid, filt)
        b = (json.dumps({"id": tid, "f": f, "k": k, "failed": d["failed"], "lines": d["lines"]}) + "\n").encode()
        while b:
            n = os.write(fd, b); b = b[n:]
    os.close(fd)


sys.stdout.flush(); sys.stderr.flush()
pids = []
for k in range(NP):
    p = os.fork()
    if p == 0:
        try:
            worker(k)
        finally:
            os._exit(0)
    pids.append(p)
for p in pids:
    os.waitpid(p, 0)

res = {tid: {"fx": fx, "base": {}, "m": {}, "fail": []} for tid, fx in fixtures.items()}
for k in range(NP):
    for line in open(f"{OUT}.{k}"):
        d = json.loads(line)
        e = res[d["id"]]
        if d["f"] is None:
            e["base"] = {p: sorted(ls) for p, ls in d["lines"].items()}
            continue
        if d["failed"]:
            e["fail"].append((d["f"], d["k"]))
        bit = 1 << d["k"]
        for p, ls in d["lines"].items():
            mp = e["m"].setdefault(p, {})
            for ln in ls:
                fm = mp.setdefault(ln, {})
                fm[d["f"]] = fm.get(d["f"], 0) | bit
    os.unlink(f"{OUT}.{k}")
open(OUT, "wb").write(zlib.compress(pickle.dumps(res), 9))
print({t.rsplit("::", 1)[1]: (sum(e["fx"].values()), len(e["fail"])) for t, e in res.items()}, file=sys.stderr)
