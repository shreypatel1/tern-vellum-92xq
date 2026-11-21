"""CPU cost of running each TPC executor-test query alone. usage: build_fpcost.py REPO OUT.json NPROC"""
import json, os, sys
if __name__ != "__main__":
    raise SystemExit
REPO, OUT, NP = sys.argv[1], sys.argv[2], int(sys.argv[3])
sys.path.insert(0, REPO); sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(REPO); os.environ["SKIP_INTEGRATION"] = "0"
import harness
TESTS = {
    "tests/test_executor.py::TestExecutor::test_execute_tpcds": "tpcds_sqls",
    "tests/test_executor.py::TestExecutor::test_execute_tpch": "tpch_sqls",
    "tests/test_executor.py::TestExecutor::test_optimized_tpch": "tpch_sqls",
}
harness.import_all(list(TESTS))
harness.install_inline_pools()
cls, _ = harness.get_class(next(iter(TESTS)))
cls.setUpClass()
if os.environ.get("T1"):
    cls.tpch_conn.execute("SET threads=1"); cls.tpcds_conn.execute("SET threads=1")
import gc; gc.collect(); gc.freeze()

def one(tid, attr, i):
    sys.stdout.flush(); sys.stderr.flush()
    pid = os.fork()
    if pid == 0:
        try:
            full = getattr(cls, attr)
            setattr(cls, attr, [full[i]])
            cls(tid.rsplit("::", 1)[1]).run(harness.Result())
        finally:
            os._exit(0)
    _, _, ru = os.wait4(pid, 0)
    return ru.ru_utime + ru.ru_stime

jobs = [(t, a, i) for t, a in TESTS.items() for i in range(len(getattr(cls, a)))]
res = {}
r, w = os.pipe()
pids = []
for k in range(NP):
    p = os.fork()
    if p == 0:
        os.close(r)
        out = [(t, i, one(t, a, i)) for j, (t, a, i) in enumerate(jobs) if j % NP == k]
        b = json.dumps(out).encode() + b"\n"
        while b:
            n = os.write(w, b); b = b[n:]
        os._exit(0)
    pids.append(p)
os.close(w)
data = b""
while True:
    b = os.read(r, 1 << 20)
    if not b: break
    data += b
for p in pids: os.waitpid(p, 0)
for line in data.splitlines():
    for t, i, c in json.loads(line):
        res.setdefault(t, {})[i] = round(c, 4)
json.dump(res, open(OUT, "w"))
for t, m in res.items():
    print(t, len(m), round(sum(m.values()), 2), sorted(m.values())[-5:])
