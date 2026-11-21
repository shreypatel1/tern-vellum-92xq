"""Compress raw per-test maps into an inverted index. usage: build_index.py TESTS_JSON RAW_PREFIX OUT"""
import json, glob, sys, pickle, zlib
TJ, RAW, OUT = sys.argv[1:4]
tests = json.load(open(TJ))["tests"]
idx = {t["id"]: t["number"] for t in tests}
inv = {}      # (file, line) -> bitset int
opened = {}
inst = {}
cpu = [0.0] * len(tests)
basefail = []
for f in glob.glob(RAW + ".[0-9]"):
    for l in open(f):
        d = json.loads(l)
        n = idx[d["id"]]
        bit = 1 << n
        cpu[n] = d["cpu"]
        if d["failed"]:
            basefail.append(d["id"])
        for fn, lines in d["lines"].items():
            m = inv.setdefault(fn, {})
            for ln in lines:
                m[ln] = m.get(ln, 0) | bit
        for k in d.get("inst", ()):
            inst[k] = inst.get(k, 0) | bit
        for fn in d["opened"]:
            opened[fn] = opened.get(fn, 0) | bit
sets = []
setid = {}
def sid(b):
    if b not in setid:
        setid[b] = len(sets); sets.append(b)
    return setid[b]
lines = {fn: {ln: sid(b) for ln, b in m.items()} for fn, m in inv.items()}
op = {fn: sid(b) for fn, b in opened.items()}
import os
NJ = RAW.rsplit("/", 1)[0] + "/names.json"
names = {}
runtime_only = []
NJD = json.load(open(NJ))
for n, fl in NJD["funcs"].items():
    b = 0
    all_exec = True
    for fn, ln in fl:
        x = inv.get(fn, {}).get(ln, 0)
        if not x:
            all_exec = False
        b |= x
    if b:
        names[n] = sid(b)
    if all_exec and not NJD["body_loads"].get(n):
        runtime_only.append(n)
imports = json.load(open(NJ.replace(".json", "_imports.json")))
import os as _os
exprclasses = json.load(open(RAW + ".exprclasses")) if _os.path.exists(RAW + ".exprclasses") else []
importtime = {k: sorted(v) for k, v in json.load(open(RAW + ".importtime")).items()} if _os.path.exists(RAW + ".importtime") else {}
data = {"importtime": importtime, "exprclasses": exprclasses, "inst": {k: sid(b) for k, b in inst.items()}, "runtime_only": runtime_only, "names": names, "imports": imports, "ids": [t["id"] for t in tests], "cpu": cpu, "basefail": basefail, "sets": sets, "lines": lines, "opened": op}
raw = pickle.dumps(data, protocol=5)
open(OUT, "wb").write(zlib.compress(raw, 9))
print("files", len(lines), "entries", sum(len(m) for m in lines.values()), "sets", len(sets), "bytes", len(raw), "->", len(zlib.compress(raw, 9)))
