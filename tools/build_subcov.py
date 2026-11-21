"""Per-query coverage for the TPC-H/TPC-DS executor tests. usage: build_subcov.py REPO OUT NPROC"""
import json, os, sys
if __name__ != "__main__":
    raise SystemExit
REPO, OUT, NP = sys.argv[1], sys.argv[2], int(sys.argv[3])
sys.path.insert(0, REPO); sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(REPO); os.environ["SKIP_INTEGRATION"] = "0"
import harness, importlib, pkgutil
import sqlglot
for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
    if not m.name.endswith("__main__"):
        try: importlib.import_module(m.name)
        except Exception: pass
TESTS = {
    "tests/test_executor.py::TestExecutor::test_execute_tpcds": "tpcds_sqls",
    "tests/test_executor.py::TestExecutor::test_execute_tpch": "tpch_sqls",
    "tests/test_executor.py::TestExecutor::test_optimized_tpch": "tpch_sqls",
}
harness.import_all(list(TESTS))
harness.install_inline_pools()
cls, _ = harness.get_class(next(iter(TESTS)))
cls.setUpClass()
SRC = os.path.join(REPO, "sqlglot") + os.sep
TST = os.path.join(REPO, "tests") + os.sep
import gc; gc.collect(); gc.freeze()

def child(tid, attr, i, wfd):
    lines = set()
    blines = set()
    phase = [False]
    mon = sys.monitoring; TOOL = 3
    mon.use_tool_id(TOOL, "cov")
    import sqlglot.executor as E
    from sqlglot.executor.python import PythonExecutor
    _plan, _exe = E.Plan, PythonExecutor.execute
    def plan(*a, **k):
        phase[0] = True
        mon.restart_events()
        return _plan(*a, **k)
    def exe(self, *a, **k):
        try:
            return _exe(self, *a, **k)
        finally:
            phase[0] = False
            mon.restart_events()
    E.Plan = plan
    PythonExecutor.execute = exe
    def on_line(code, ln):
        fn = code.co_filename
        if fn.startswith(SRC) or fn.startswith(TST):
            (blines if phase[0] else lines).add((fn, ln))
        return mon.DISABLE
    def on_start(code, off):
        fn = code.co_filename
        if fn.startswith(SRC) or fn.startswith(TST):
            (blines if phase[0] else lines).add((fn, -code.co_firstlineno))
        return mon.DISABLE
    mon.register_callback(TOOL, mon.events.LINE, on_line)
    mon.register_callback(TOOL, mon.events.PY_START, on_start)
    full = getattr(cls, attr)
    setattr(cls, attr, [full[i]])
    mon.set_events(TOOL, mon.events.LINE | mon.events.PY_START)
    meth = tid.rsplit("::", 1)[1]
    r = harness.Result()
    cls(meth).run(r)
    mon.set_events(TOOL, 0)
    out = {}
    for fn, ln in lines | blines:
        out.setdefault(os.path.relpath(fn, REPO), []).append(ln)
    bout = {}
    for fn, ln in blines:
        bout.setdefault(os.path.relpath(fn, REPO), []).append(ln)
    b = json.dumps({"id": tid, "i": i, "failed": r.bad, "lines": out, "blines": bout}).encode()
    while b:
        n = os.write(wfd, b); b = b[n:]

def run_one(tid, attr, i):
    sys.stdout.flush(); sys.stderr.flush()
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        try: child(tid, attr, i, w)
        except BaseException:
            import traceback; traceback.print_exc()
        os._exit(0)
    os.close(w)
    chunks = []
    while True:
        b = os.read(r, 1 << 20)
        if not b: break
        chunks.append(b)
    os.close(r); os.waitpid(pid, 0)
    return json.loads(b"".join(chunks))

jobs = [(tid, attr, i) for tid, attr in TESTS.items() for i in range(len(getattr(cls, attr)))]
def worker(idx):
    fd = os.open(f"{OUT}.{idx}", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    for j, job in enumerate(jobs):
        if j % NP != idx: continue
        b = (json.dumps(run_one(*job)) + "\n").encode()
        while b:
            n = os.write(fd, b); b = b[n:]
    os.close(fd)
sys.stdout.flush(); sys.stderr.flush()
pids = []
for k in range(NP):
    p = os.fork()
    if p == 0:
        try: worker(k)
        finally: os._exit(0)
    pids.append(p)
for p in pids: os.waitpid(p, 0)
