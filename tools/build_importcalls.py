"""Record which sqlglot functions/lambdas are called while everything is imported, keyed by
(file, first line), and store them in index.bin as "importcalls".
usage: build_importcalls.py REPO TESTS_JSON INDEX"""
import json, os, pickle, sys, zlib, importlib, pkgutil
REPO, TJ, INDEX = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, REPO); sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.chdir(REPO); os.environ["SKIP_INTEGRATION"] = "0"
import harness
tests = json.load(open(TJ))["tests"]
SRC = os.path.join(REPO, "sqlglot") + os.sep
calls = set()
mon = sys.monitoring
mon.use_tool_id(4, "impcalls")
def on_start(code, off):
    if code.co_filename.startswith(SRC):
        calls.add((code.co_filename, code.co_firstlineno))
    return mon.DISABLE
mon.register_callback(4, mon.events.PY_START, on_start)
mon.set_events(4, mon.events.PY_START)
import sqlglot
for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
    try: importlib.import_module(m.name)
    except Exception as e: print("skip", m.name, e, file=sys.stderr)
harness.import_all([t["id"] for t in tests])
mon.set_events(4, 0)
out = {}
for fn, ln in calls:
    out.setdefault(os.path.relpath(fn, REPO), []).append(ln)
d = pickle.loads(zlib.decompress(open(INDEX, "rb").read()))
d["importcalls"] = {k: sorted(v) for k, v in out.items()}
open(INDEX, "wb").write(zlib.compress(pickle.dumps(d, protocol=5), 9))
print("files", len(out), "code objects", len(calls), file=sys.stderr)
