"""Per test: which keys of the dict constants on sqlglot classes it read.
A constant dict on a class (TYPE_MAPPING, NUMERIC_ESCAPES, TRANSFORMS, ...) is replaced, after
sqlglot is imported, by a recording subclass. A test that looked up key k reads entry k; a test
that iterated, copied, compared, measured or changed the dict counts as reading every entry.
An edit that changes some entries of such a dict can then only be seen by the tests that read
those entries (or the whole dict).
usage: build_keyuse.py REPO TESTS_JSON INDEX NPROC"""
import json, os, pickle, sys, zlib
REPO, TJ, INDEX, NP = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, REPO); sys.path.insert(0, HERE)
os.chdir(REPO); os.environ["SKIP_INTEGRATION"] = "0"
import harness
import keyuse
tests = json.load(open(TJ))["tests"]
import sqlglot
import pkgutil, importlib
for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
    try: importlib.import_module(m.name)
    except Exception: pass

REC = keyuse.REC
n_dicts = keyuse.install()
print("tracked dicts", n_dicts, file=sys.stderr)

# reads while the test modules are imported count for every test of that module
IMPORT_READ = {}
for f in sorted({t["file"] for t in tests}):
    REC.clear()
    harness.import_all([t["id"] for t in tests if t["file"] == f])
    if REC:
        IMPORT_READ[f] = {k: set(v) for k, v in REC.items()}
REC.clear()
harness.install_inline_pools()
import gc; gc.collect(); gc.freeze()


def child(tid, wfd):
    REC.clear()
    harness.run_tests([tid], timeout_for=lambda t: 600)
    out = {k: sorted(v) for k, v in REC.items()}
    b = json.dumps({"id": tid, "r": out}).encode()
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
    try:
        return json.loads(b"".join(chunks))
    except Exception:
        return {"id": tid, "r": None}


OUT = INDEX + ".keyuse"
def worker(idx):
    fd = os.open(f"{OUT}.{idx}", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    for i, t in enumerate(tests):
        if i % NP != idx: continue
        b = (json.dumps(run_one(t["id"])) + "\n").encode()
        while b:
            n = os.write(fd, b); b = b[n:]
    os.close(fd)

pids = []
for i in range(NP):
    p = os.fork()
    if p == 0:
        worker(i); os._exit(0)
    pids.append(p)
for p in pids: os.waitpid(p, 0)

d = pickle.loads(zlib.decompress(open(INDEX, "rb").read()))
pos = {t: i for i, t in enumerate(d["ids"])}
use = {}   # tag -> {keyrepr or "*": bits}
unknown = 0
for i in range(NP):
    for line in open(f"{OUT}.{i}"):
        r = json.loads(line)
        p = pos.get(r["id"])
        if p is None: continue
        if r["r"] is None:
            unknown |= 1 << p
            continue
        recs = {k: set(v) for k, v in r["r"].items()}
        for k, v in IMPORT_READ.get(r["id"].split("::")[0], {}).items():
            recs.setdefault(k, set()).update(v)
        for tag, keys in recs.items():
            m = use.setdefault(tag, {})
            for k in keys:
                m[k] = m.get(k, 0) | (1 << p)
d["keyuse"] = {"use": use, "unknown": unknown, "tags": keyuse.TAGS}
open(INDEX, "wb").write(zlib.compress(pickle.dumps(d, protocol=5), 9))
print("tags", len(use), "entries", sum(len(v) for v in use.values()), "unknown tests", bin(unknown).count("1"), file=sys.stderr)
