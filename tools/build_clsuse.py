"""Per test: which Dialect classes it touched (read an attribute of the class, or of an instance).
A constant on dialect class C can only be observed by a test that reads attributes of C or of an
instance of C (or a subclass), so this narrows the readers-of-a-name reach of such constants.
usage: build_clsuse.py REPO TESTS_JSON INDEX NPROC"""
import json, os, pickle, sys, zlib
REPO, TJ, INDEX, NP = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
sys.path.insert(0, REPO); sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.chdir(REPO); os.environ["SKIP_INTEGRATION"] = "0"
import harness
tests = json.load(open(TJ))["tests"]
import sqlglot
from sqlglot.dialects import dialect as D
import pkgutil, importlib
for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
    try: importlib.import_module(m.name)
    except Exception: pass
# dialect classes touched while test modules are imported (module-level constants): every test
# of that module counts as touching them
_imp_seen = {}
def _cls_ga(cls, name):
    _imp_seen[id(cls)] = cls
    return type.__getattribute__(cls, name)
def _inst_ga(self, name):
    c = type(self); _imp_seen[id(c)] = c
    return object.__getattribute__(self, name)
IMPORT_TOUCH = {}
for f in sorted({t["file"] for t in tests}):
    _imp_seen.clear()
    D._Dialect.__getattribute__ = _cls_ga
    D.Dialect.__getattribute__ = _inst_ga
    try:
        harness.import_all([t["id"] for t in tests if t["file"] == f])
    finally:
        del D.Dialect.__getattribute__
        del D._Dialect.__getattribute__
    ks = set()
    for c in list(_imp_seen.values()):
        for k in c.__mro__:
            if isinstance(k, D._Dialect):
                ks.add(f"{k.__module__}:{k.__qualname__}")
    if ks:
        IMPORT_TOUCH[f] = ks
print("import-time touches", {f: sorted(v)[:5] for f, v in IMPORT_TOUCH.items()}, file=sys.stderr)
harness.install_inline_pools()
import gc; gc.collect(); gc.freeze()

def key(c):
    return f"{c.__module__}:{c.__qualname__}"

def child(tid, wfd):
    seen = {}  # id -> class (the metaclass hashes by name, which would recurse into the hook)
    meta_ga = type.__getattribute__
    obj_ga = object.__getattribute__
    def cls_ga(cls, name):
        seen[id(cls)] = cls
        return meta_ga(cls, name)
    def inst_ga(self, name):
        c = type(self)
        seen[id(c)] = c
        return obj_ga(self, name)
    D._Dialect.__getattribute__ = cls_ga
    D.Dialect.__getattribute__ = inst_ga
    harness.run_tests([tid], timeout_for=lambda t: 600)
    del D.Dialect.__getattribute__
    del D._Dialect.__getattribute__
    keys = set()
    for c in list(seen.values()):
        for k in c.__mro__:
            if isinstance(k, D._Dialect):
                keys.add(key(k))
    b = json.dumps({"id": tid, "cls": sorted(keys)}).encode()
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
        return {"id": tid, "cls": None}

OUT = INDEX + ".clsuse"
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
use = {}
nall = 0
for i in range(NP):
    for line in open(f"{OUT}.{i}"):
        r = json.loads(line)
        p = pos.get(r["id"])
        if p is None: continue
        if r["cls"] is None:
            nall += 1
            r["cls"] = ["*"]
        r["cls"] = list(r["cls"]) + sorted(IMPORT_TOUCH.get(r["id"].split("::")[0], ()))
        for k in r["cls"]:
            use[k] = use.get(k, 0) | (1 << p)
d["clsuse"] = use
# for every dialect class: the sqlglot source files its behaviour is defined in (the modules of the
# classes in the MROs of the dialect, its tokenizer/parser/generator classes, plus the sqlglot modules
# those files import directly, e.g. sqlglot/typing/<dialect>.py)
import ast
def _file(modname):
    return modname.replace(".", "/") + ".py" if os.path.exists(os.path.join(REPO, modname.replace(".", "/") + ".py")) else modname.replace(".", "/") + "/__init__.py"
def _imports(path):
    out = set()
    try:
        tree = ast.parse(open(os.path.join(REPO, path)).read())
    except Exception:
        return out
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module and n.module.startswith("sqlglot"):
            out.add(_file(n.module))
            for a in n.names:
                sub = n.module + "." + a.name
                if os.path.exists(os.path.join(REPO, sub.replace(".", "/") + ".py")):
                    out.add(sub.replace(".", "/") + ".py")
        elif isinstance(n, ast.Import):
            for a in n.names:
                if a.name.startswith("sqlglot"):
                    out.add(_file(a.name))
    return out
dmods = {}
for k in use:
    if k == "*":
        continue
    mn, q = k.split(":", 1)
    mod = sys.modules.get(mn)
    obj = mod
    try:
        for part in q.split("."):
            obj = getattr(obj, part)
    except Exception:
        continue
    files = set()
    for c in (obj, getattr(obj, "tokenizer_class", None), getattr(obj, "parser_class", None),
              getattr(obj, "generator_class", None), getattr(obj, "jsonpath_tokenizer_class", None)):
        if c is None:
            continue
        for b in c.__mro__:
            m = getattr(b, "__module__", "") or ""
            if m.startswith("sqlglot."):
                files.add(_file(m))
    for f in list(files):
        files |= _imports(f)
    dmods[k] = sorted(files)
d["dialect_mods"] = dmods
# tokenizer/parser/generator classes that are only reachable through their dialects: their users are
# the users of the dialects built on them. Only for classes whose name appears in no test source and
# in no sqlglot source other than the module defining it and the dialect modules that plug it in.
import re
srcs = {}
for root in ("sqlglot", "tests"):
    for dp, _, fs in os.walk(os.path.join(REPO, root)):
        for f in fs:
            if f.endswith(".py"):
                pth = os.path.relpath(os.path.join(dp, f), REPO)
                srcs[pth] = open(os.path.join(REPO, pth), encoding="utf-8").read()
derived = {}
plugged = {}
for k in list(use):
    if k == "*":
        continue
    mn, q = k.split(":", 1)
    obj = sys.modules.get(mn)
    try:
        for part in q.split("."):
            obj = getattr(obj, part)
    except Exception:
        continue
    for an in ("tokenizer_class", "parser_class", "generator_class", "jsonpath_tokenizer_class"):
        c = getattr(obj, an, None)
        if c is None:
            continue
        for b in c.__mro__:
            m = getattr(b, "__module__", "") or ""
            if not m.startswith("sqlglot.") or m in ("sqlglot.tokens", "sqlglot.parser", "sqlglot.generator", "sqlglot.jsonpath", "sqlglot.parsers.base"):
                continue
            bk = f"{m}:{b.__qualname__}"
            if bk in use:
                continue
            derived[bk] = derived.get(bk, 0) | use[k]
            plugged.setdefault(bk, set()).add(_file(mn))
kept = 0
for bk, bits in derived.items():
    m, q = bk.split(":", 1)
    top = q.split(".")[0]
    home = _file(m)
    pat = re.compile(r"\b" + re.escape(top) + r"\b")
    ok = True
    for pth, txt in srcs.items():
        if pth == home or pth in plugged[bk]:
            continue
        if pat.search(txt):
            ok = False
            break
    if ok and "." not in q:
        use[bk] = bits
        kept += 1
d["clsuse"] = use
print("derived classes", len(derived), "kept", kept, file=sys.stderr)
open(INDEX, "wb").write(zlib.compress(pickle.dumps(d, protocol=5), 9))
print("classes", len(use), "unknown tests", nall, file=sys.stderr)
