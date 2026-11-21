"""Work out, for one patch, which tests could be affected and how."""
import os
import pickle
import zlib

import patchinfo


class Index:
    def __init__(self, path):
        d = pickle.loads(zlib.decompress(open(path, "rb").read()))
        self.ids = d["ids"]
        self.cpu = d["cpu"]
        self.basefail = set(d["basefail"])
        self.sets = d["sets"]
        self.lines = d["lines"]
        self.opened = d["opened"]
        self.names = d["names"]
        self.runtime_only = set(d.get("runtime_only", ()))
        self.inst = d.get("inst", {})
        self.exprclasses = set(d.get("exprclasses", ()))
        self.importtime = {k: set(v) for k, v in d.get("importtime", {}).items()}
        self.subcov = d.get("subcov", {})
        self.tpcfp = d.get("tpcfp", {})
        self.state = d.get("state", {})
        self.tpcres = d.get("tpcres", {})
        self.imports = d["imports"]

    def name_set(self, n):
        s = self.names.get(n)
        return self.sets[s] if s is not None else 0

    def line_set(self, fn, ln):
        m = self.lines.get(fn)
        if not m:
            return 0
        s = m.get(ln)
        return self.sets[s] if s is not None else 0

    def file_any(self, fn):
        m = self.lines.get(fn)
        b = 0
        if m:
            for s in set(m.values()):
                b |= self.sets[s]
        return b

    def opened_set(self, fn):
        s = self.opened.get(fn)
        return self.sets[s] if s is not None else 0


def bits(b):
    out = []
    i = 0
    while b:
        if b & 1:
            out.append(i)
        b >>= 1
        i += 1
    return out


DIFF_STRUCTURAL = os.environ.get("BR_DIFF_STRUCT", "1") == "1"
TPC_FIXTURES = {"tpch": "tests/fixtures/optimizer/tpc-h/tpc-h.sql",
                "tpcds": "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql"}


class Change:
    """Analysis of one commit."""

    def __init__(self, cid, text, repo, index):
        self.cid = cid
        self.files = patchinfo.parse(text)
        self.repo = repo
        self.ix = index
        self.cand = 0         # tests that might fail: run or include
        self.precise = 0      # subset of cand reached at statement level (for blind decisions)
        self.new_sources = {}  # path -> new text (python and data files)
        self.swap = {}        # py path -> classify result (function-only changes)
        self.reimport = set()  # py paths that need a fresh import
        self.unknown = False  # something we cannot reason about: be conservative
        self.notes = []
        self.equivalent = set()
        self.equiv_needs = {}
        self.spans = {}
        self.itime = set()
        self.it_fallback = 0
        self.diffpaths = set()
        self.it_keys = set()
        self.diff_fallback = 0
        for fp in self.files:
            self._one(fp)

    def _one(self, fp):
        ix = self.ix
        path = fp.path
        if fp.binary or fp.old is None or fp.new is None or fp.old != fp.new:
            self.unknown = True
            self.notes.append(f"odd file op {fp.old}->{fp.new}")
            if fp.old:
                self.cand |= ix.opened_set(fp.old) | ix.file_any(fp.old)
            return
        full = os.path.join(self.repo, path)
        try:
            old_text = open(full, encoding="utf-8").read()
        except Exception:
            self.unknown = True
            self.notes.append(f"missing {path}")
            return
        new_text, removed, inserts = patchinfo.apply(old_text, fp)
        self.new_sources[path] = new_text
        opened = ix.opened_set(path)
        info = patchinfo.classify_fast(old_text, new_text, removed, inserts) if path.endswith(".py") else None
        if info is not None and info["same"]:
            # comment/blank-only edit of a module: reading it (imports, linecache) changes nothing
            self.notes.append(f"comment-only {path}")
            return
        self.cand |= opened
        self.precise |= opened
        if not path.endswith(".py"):
            if not opened:
                self.notes.append(f"data file nobody opens: {path}")
            return
        if info["same"]:
            self.notes.append(f"comment-only {path}")
            return
        if info["error"] or info["structural"]:
            self.reimport.add(path)
            self.notes.append(f"structural {path}")
            reach = self._structural_reach(path, info)
            if (not info["error"] and info.get("attrs") and DIFF_STRUCTURAL
                    and not any(n.startswith("attr-reach") or n.startswith("expr-class") for n in self.notes)):
                # only module/class-level assignments changed: their whole effect is the module
                # state after import, so diff that state and follow the names that moved
                self.diffpaths.add(path)
                self.diff_fallback |= reach
                self.notes.append("state-diff pending")
                return
            self.cand |= reach
            # statement-level reach for blind decisions: also whole-file (conservative)
            self.precise |= reach
            if info["error"]:
                # import error -> every test importing it fails; treat as unknown
                self.unknown = True
            return
        lines = set(removed)
        for p in inserts:
            lines.add(p - 1)
            lines.add(p)
        spans = patchinfo.stmt_spans_tree(info["old_tree"], lines)
        it = ix.importtime.get(path, set())
        if spans & it:
            # the changed code runs while modules are imported (metaclass, class-body helper):
            # its effect is baked into classes, so treat it like a structural change
            # (decided later by diffing module state after a patched import; see apply_itime)
            self.reimport.add(path)
            self.itime.add(path)
            self.notes.append(f"import-time {path}")
            self.it_fallback |= self._structural_reach(path, dict(info, attrs=None, scopes=[]))
            mod = path[:-3].replace("/", ".")
            if mod.endswith(".__init__"):
                mod = mod[:-9]
            for q, k, firstline in info["changed_funcs"]:
                self.cand |= ix.line_set(path, -firstline)
                # the function object itself differs (new code): runtime reach covers its callers
                parts = q.split(".<locals>.")
                for j in range(1, len(parts) + 1):  # enclosing functions hold its code too
                    self.it_keys.add(f"{mod}:{'.<locals>.'.join(parts[:j])}")
            for ln in spans:
                self.precise |= ix.line_set(path, ln)
                self.cand |= ix.line_set(path, ln)
            return
        self.swap[path] = info
        self.spans[path] = spans
        eq = patchinfo.equivalent_units(info) if info.get("units") else patchinfo.equivalent_py(old_text, new_text)
        if eq:
            self.equivalent.add(path)
            self.equiv_needs[path] = set() if eq is True else set(eq)
            self.notes.append(f"equivalent {path} needs={sorted(self.equiv_needs[path])}")
        # function-level reach for running, statement-level for blind decisions
        for q, k, firstline in info["changed_funcs"]:
            self.cand |= ix.line_set(path, -firstline)
        lines = set(removed)
        for p in inserts:
            lines.add(p - 1)
            lines.add(p)
        spans = patchinfo.stmt_spans_tree(info["old_tree"], lines)
        for ln in spans:
            self.precise |= ix.line_set(path, ln)

    def _structural_reach(self, path, info):
        ix = self.ix
        scopes = info.get("scopes") or []
        keys = [f"{path}::{q}" for q in scopes]
        if scopes and all(q and k in ix.exprclasses for q, k in zip(scopes, keys)):
            # only the bodies of expression classes changed: whoever builds one (or a subclass)
            self.notes.append(f"expr-class-reach {scopes}")
            b = 0
            for k in keys:
                s = ix.inst.get(k)
                if s is not None:
                    b |= ix.sets[s]
            return b
        attrs = info.get("attrs")
        if attrs and all(a in ix.runtime_only for a in attrs):
            # only constant assignments changed, and every reader of those names is
            # ordinary runtime code that the coverage map saw: readers are the reach
            self.notes.append(f"attr-reach {attrs}")
            b = 0
            for a in attrs:
                b |= ix.name_set(a)
            return b
        reach = ix.file_any(path)
        d, base = os.path.split(path)
        parts = d.split("/")
        # same-named modules in sibling packages (dialects/x.py, parsers/x.py, generators/x.py ...)
        if len(parts) == 2 and parts[0] == "sqlglot" and base != "__init__.py":
            for sib in ("dialects", "parsers", "generators", "tokenizers", "typing", "executor", "optimizer"):
                reach |= ix.file_any(f"sqlglot/{sib}/{base}")
        # importers in the same package (e.g. dialects that subclass this one)
        for imp in ix.imports.get(path, []):
            if os.path.dirname(imp) == d and d == "sqlglot/dialects" and not imp.endswith("__init__.py"):
                reach |= ix.file_any(imp)
        limit = len(ix.ids) // 4
        for n in info.get("names", ()):
            b = ix.name_set(n)
            if b.bit_count() <= limit:
                reach |= b
        return reach

    def fp_eligible(self, tid):
        """Can the outcome of this TPC executor test be decided by fingerprints?"""
        sub = self.ix.subcov.get(tid)
        if not sub or not self.ix.tpcfp:
            return False
        pos = self.ix.ids.index(tid)
        for fp in self.files:
            path = fp.path
            if not path.endswith(".py"):
                if (self.ix.opened_set(path) >> pos) & 1 and path not in TPC_FIXTURES.values():
                    return False
                continue
            if path.startswith("tests/"):
                return False  # the test's own code (comparison, helpers) is not fingerprinted
            B = sub["blines"].get(path)
            if path in self.reimport:
                if B:
                    return False
            elif path in self.swap:
                sp = self.spans.get(path, set())
                if B and (sp & set(B)) and not self.ix.tpcres:
                    return False
                # changed lines the test ran outside any per-query run (class setup etc.)
                q = sub["lines"].get(path, {})
                if any(((self.ix.line_set(path, ln) >> pos) & 1) and ln not in q for ln in sp):
                    return False
        return True

    def apply_itime(self, keys):
        """Reach of an import-time change given the module-state keys that differ (None = unknown)."""
        ix = self.ix
        if keys is not None:
            keys = [k for k in keys if k not in self.it_keys]
        n = len(ix.ids)
        if keys is None and self.itime:
            # the patched import failed or could not be checked: anything may break
            self.notes.append("itime-unknown -> all tests")
            reach = (1 << n) - 1
        elif keys is None or len(keys) > 60:
            self.notes.append(f"diff-fallback {None if keys is None else len(keys)}")
            reach = self.it_fallback | self.diff_fallback
        else:
            self.notes.append(f"itime-diff {sorted(keys)[:6]}")
            reach = 0
            for k in keys:
                mn, rest = k.split(":", 1)
                if mn == "tests" or mn.startswith("tests."):
                    pre = mn.replace(".", "/") + ".py::"
                    b = 0
                    for i, tid in enumerate(ix.ids):
                        if tid.startswith(pre):
                            b |= 1 << i
                else:
                    b = ix.name_set(rest.rsplit(".", 1)[-1])
                    if b.bit_count() > n // 4:
                        reach = self.it_fallback | self.diff_fallback
                        self.notes.append(f"itime-broad {k}")
                        break
                reach |= b
        self.cand |= reach
        self.precise |= reach

    def fp_phase_b(self, tid):
        """True if a changed statement runs while queries are planned/executed in Python."""
        sub = self.ix.subcov[tid]
        for path, sp in self.spans.items():
            B = sub["blines"].get(path)
            if B and (sp & set(B)):
                return True
        return False

    def fp_queries(self, tid):
        """Indices of the TPC queries whose run touched a changed statement (None = all)."""
        sub = self.ix.subcov[tid]
        if self.reimport or any(fp.path in TPC_FIXTURES.values() for fp in self.files):
            return None
        m = 0
        for path, sp in self.spans.items():
            q = sub["lines"].get(path)
            if q:
                for ln in sp:
                    m |= q.get(ln, 0)
        return {i for i in range(sub["n"]) if (m >> i) & 1}
