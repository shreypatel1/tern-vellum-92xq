"""Per-test line coverage + opened files, each test in a fresh fork. usage: build_maps.py REPO TESTS_JSON OUT NPROC"""
import json, os, sys, time, importlib, pkgutil, resource
if __name__ != "__main__":
    raise SystemExit
REPO, TJ, OUT, NP = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
sys.path.insert(0, REPO); sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(REPO); os.environ["SKIP_INTEGRATION"] = "0"
import harness
tests = json.load(open(TJ))["tests"]
# record every sqlglot line that runs while modules are imported (class bodies, metaclasses,
# decorators, helpers called at class-creation time)
_SRC0 = os.path.join(REPO, "sqlglot") + os.sep
_imp_lines = set()
_mon = sys.monitoring
_mon.use_tool_id(4, "imp")
def _imp_line(code, ln):
    if code.co_filename.startswith(_SRC0):
        _imp_lines.add((code.co_filename, ln))
    return _mon.DISABLE
_mon.register_callback(4, _mon.events.LINE, _imp_line)
_mon.set_events(4, _mon.events.LINE)
import sqlglot
for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
    try: importlib.import_module(m.name)
    except Exception as e: print("skip", m.name, e, file=sys.stderr)
bad = harness.import_all([t["id"] for t in tests])
print("import errors", bad, file=sys.stderr)
_mon.set_events(4, 0)
_mon.register_callback(4, _mon.events.LINE, None)
_mon.free_tool_id(4)
_it = {}
for fn, ln in _imp_lines:
    _it.setdefault(os.path.relpath(fn, REPO), []).append(ln)
json.dump(_it, open(OUT + ".importtime", "w"))
harness.install_inline_pools()
from sqlglot.expressions import core as _c
_all, _todo = set(), [_c.Expr]
while _todo:
    k = _todo.pop()
    if k in _all: continue
    _all.add(k); _todo.extend(k.__subclasses__())
json.dump(sorted(k.__module__.replace(".", "/") + ".py::" + k.__qualname__ for k in _all if k.__module__.startswith("sqlglot.")), open(OUT + ".exprclasses", "w"))
import gc; gc.collect(); gc.freeze()
SRC = os.path.join(REPO, "sqlglot") + os.sep
TST = os.path.join(REPO, "tests") + os.sep

def child(tid, wfd):
    lines = set(); opened = set()
    mon = sys.monitoring; TOOL = 3
    mon.use_tool_id(TOOL, "cov")
    D = mon.DISABLE
    def on_line(code, ln):
        fn = code.co_filename
        if fn.startswith(SRC) or fn.startswith(TST):
            lines.add((fn, ln))
        return D
    def on_start(code, off):
        fn = code.co_filename
        if fn.startswith(SRC) or fn.startswith(TST):
            lines.add((fn, -code.co_firstlineno))
        return D
    mon.register_callback(TOOL, mon.events.LINE, on_line)
    mon.register_callback(TOOL, mon.events.PY_START, on_start)
    def audit(ev, args):
        if ev == "open" and isinstance(args[0], str):
            opened.add(os.path.abspath(args[0]))
    sys.addaudithook(audit)
    from sqlglot.expressions import core as _core
    _orig_init = _core.Expr.__init__
    inst = set()
    def _init(self, **args):
        inst.add(type(self))
        _orig_init(self, **args)
    _core.Expr.__init__ = _init
    mon.set_events(TOOL, mon.events.LINE | mon.events.PY_START)
    t0 = time.process_time()
    failed = harness.run_tests([tid], timeout_for=lambda t: 600)
    cpu = time.process_time() - t0
    mon.set_events(TOOL, 0)
    out = {}
    for fn, ln in lines:
        out.setdefault(os.path.relpath(fn, REPO), []).append(ln)
    op = sorted(os.path.relpath(p, REPO) for p in opened if p.startswith(REPO))
    _core.Expr.__init__ = _orig_init
    keys = set()
    for c in inst:
        for k in c.__mro__:
            m = getattr(k, "__module__", "") or ""
            if m.startswith("sqlglot."):
                keys.add(m.replace(".", "/") + ".py::" + k.__qualname__)
    data = json.dumps({"id": tid, "failed": bool(failed), "cpu": cpu, "lines": out, "opened": op, "inst": sorted(keys)})
    b = data.encode()
    while b:
        n = os.write(wfd, b); b = b[n:]
    os._exit(0)

def run_one(tid):
    sys.stdout.flush(); sys.stderr.flush()
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        try: child(tid, w)
        except BaseException:
            import traceback; traceback.print_exc()
        os._exit(1)
    os.close(w)
    chunks = []
    while True:
        b = os.read(r, 1 << 20)
        if not b: break
        chunks.append(b)
    os.close(r); os.waitpid(pid, 0)
    return json.loads(b"".join(chunks))

def worker(idx):
    fd = os.open(f"{OUT}.{idx}", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    for i, t in enumerate(tests):
        if i % NP != idx: continue
        b = (json.dumps(run_one(t["id"])) + "\n").encode()
        while b:
            n = os.write(fd, b); b = b[n:]
    os.close(fd)

pids = []
sys.stdout.flush(); sys.stderr.flush()
for i in range(NP):
    p = os.fork()
    if p == 0:
        worker(i); os._exit(0)
    pids.append(p)
for p in pids: os.waitpid(p, 0)
