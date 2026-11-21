"""Blast Radius selector.

For each patch: find tests whose recorded coverage reaches the change, run the cheap ones
in-process against the patched code (forked child, function code swapped in place), and
schedule the failing ones plus any expensive reachable ones we chose not to run.
"""
import builtins
import importlib.util
import gc
import io
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.environ.get("BR_REPO", "/app/sq")
DATA = os.environ.get("BR_DATA", "/data")
sys.path.insert(0, HERE)

import analyze  # noqa: E402

T0 = time.process_time()


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# tests that we never run inline (they spawn subprocesses). test_generator_memory is run: in a
# child with a frozen heap it costs ~0.4 s CPU against 6.7 s recorded, and a real leak (>= one
# object per generator() call, checked over 1000 calls against a 1 KB bound) shows there too;
# a spurious in-process failure only schedules it.
NEVER_RUN = {
    "tests/dialects/test_dialect.py::TestDialect::test_lazy_load",
}
BLIND_CPU = float(os.environ.get("BR_BLIND_CPU", "2.5"))  # run-cost above which we include instead of running
WORKER_COST = 2000
LAST_CPU = 0.0
CPU_SCALE = float(os.environ.get("BR_CPU_SCALE", "1.0"))
HEAVY_IMPORT_MODULES = {"tests/test_executor.py", "tests/test_optimizer.py"}


def import_cost(mods):
    return 0.8 if mods & HEAVY_IMPORT_MODULES else 0.0
TEST_TIMEOUT = float(os.environ.get("BR_TIMEOUT", "60"))
EQUIV_BUDGET = float(os.environ.get("BR_EQUIV_BUDGET", "1.0"))


def pack(durs):
    """durs: {num: ms}. Returns best list of workers (<=8) minimising max load + 2s*workers."""
    if not durs:
        return []
    items = sorted(durs.items(), key=lambda kv: -kv[1])
    best = None
    top = items[0][1]
    total = sum(d for _, d in items)
    for k in range(1, min(8, len(items)) + 1):
        # no packing on k workers beats this bound, so skipping k cannot change the result
        if best is not None and max(top, -(-total // k)) + WORKER_COST * k >= best[0]:
            continue
        loads = [0] * k
        ws = [[] for _ in range(k)]
        for n, d in items:
            i = loads.index(min(loads))
            loads[i] += d
            ws[i].append(n)
        cost = max(loads) + WORKER_COST * k
        if best is None or cost < best[0]:
            best = (cost, ws)
    return best[1]


# ---------------- child-side patch application ----------------

def _code_key_map(code, out, counts, qual_fix=None):
    import types
    for c in code.co_consts:
        if isinstance(c, types.CodeType):
            q = c.co_qualname
            k = counts.get(q, 0)
            counts[q] = k + 1
            out[(q, k)] = c
            _code_key_map(c, out, counts)


def code_map(src, filename):
    top = compile(src, filename, "exec")
    out, counts = {}, {}
    _code_key_map(top, out, counts)
    return out


def swap_functions(path, old_src, new_src, funcs):
    """Replace __code__ of live function objects from `path` with the patched versions."""
    full = os.path.join(REPO, path)
    old = code_map(old_src, full)
    new = code_map(new_src, full)
    if set(old) != set(new):
        return False
    by_code = {c: k for k, c in old.items()}
    for f in funcs:
        k = by_code.get(f.__code__)
        if k is None:
            continue
        nc = new[k]
        if nc == f.__code__:
            continue
        if nc.co_freevars != f.__code__.co_freevars:
            return False
        f.__code__ = nc
    return True


class PatchedFinder:
    def __init__(self, sources):
        self.sources = sources  # modname -> (fullpath, source)

    def find_spec(self, name, path=None, target=None):
        import importlib.util
        if name in self.sources:
            full, src = self.sources[name]
            loader = PatchedLoader(full, src)
            is_pkg = full.endswith("__init__.py")
            spec = importlib.util.spec_from_file_location(
                name, full, loader=loader,
                submodule_search_locations=[os.path.dirname(full)] if is_pkg else None)
            return spec
        return None


class PatchedLoader:
    def __init__(self, full, src):
        self.full, self.src = full, src

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        code = compile(self.src, self.full, "exec")
        module.__file__ = self.full
        exec(code, module.__dict__)


def modname(path):
    p = path[:-3]
    if p.endswith("/__init__"):
        p = p[: -len("/__init__")]
    return p.replace("/", ".")


def install_redirects(data_files):
    """Serve patched contents for data files the tests open."""
    if not data_files:
        return
    real_open = builtins.open
    tmpdir = "/tmp/brdata"
    os.makedirs(tmpdir, exist_ok=True)
    redirect = {}
    for i, (path, text) in enumerate(data_files.items()):
        dst = os.path.join(tmpdir, f"{i}_{os.path.basename(path)}")
        with real_open(dst, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        redirect[os.path.join(REPO, path)] = dst

    def patched_open(file, *a, **k):
        if isinstance(file, (str, bytes, os.PathLike)):
            p = os.path.abspath(os.fsdecode(file))
            if p in redirect:
                file = redirect[p]
        return real_open(file, *a, **k)

    builtins.open = patched_open
    io.open = patched_open


FP_TESTS = {
    "tests/test_executor.py::TestExecutor::test_execute_tpcds": ("tpcds", "exec"),
    "tests/test_executor.py::TestExecutor::test_execute_tpch": ("tpch", "exec"),
    "tests/test_executor.py::TestExecutor::test_optimized_tpch": ("tpch", "opt"),
}


FP_MIN_GAIN = float(os.environ.get("BR_FP_MIN_GAIN", "3.0"))
FP_EXEC_BUDGET = float(os.environ.get("BR_FP_EXEC", "13.0"))
# recorded per-query costs (map build) over what a re-check costs here
FP_SCALE = float(os.environ.get("BR_FP_SCALE", "0.75"))
FP_LOAD = 2.5  # executor test class setup (tables + DuckDB views)


def _proc_cpu():
    import resource
    r = resource.getrusage(resource.RUSAGE_SELF)
    return r.ru_utime + r.ru_stime


def _arm(t0, budget, cap=None):
    """Stop the current step once the CPU budget is spent, or after `cap` seconds (a query that
    runs far longer than recorded will not pay off)."""
    import signal
    left = budget - (_proc_cpu() - t0)
    if left <= 0:
        raise RuntimeError("budget")
    signal.setitimer(signal.ITIMER_REAL, min(left, cap if cap is not None else left) + 0.3)


def fp_failures(fp_ids, ch, ix):
    """Decide the TPC executor tests without running them whole.

    Fingerprint the queries the change reaches (parsed+pickled expression, optimized expression,
    DuckDB transpilations). Queries the test does not execute can only fail by raising while
    being parsed/pickled. Executed queries whose optimized form moved (or whose Python execution
    path changed) are executed with the patched code right away and their result compared to the
    pinned commit's result, which the unpatched test proves equal to DuckDB's. The first mismatch
    stops the work, and so does spending more CPU than scheduling the test would save."""
    import signal
    import harness
    import tpcfp
    import tpcres
    bad = set()
    cache = {}
    loaded = []
    prev = signal.signal(signal.SIGALRM, harness._alarm)
    try:
        for tid in fp_ids:
            which, mode = FP_TESTS[tid]
            q = ch.fp_queries(tid)
            ref = ix.tpcfp.get(which, {})
            idxs = {int(i) for i in ref} if q is None else q
            if not idxs:
                continue
            now = cache.setdefault(which, {})
            res = ix.tpcres.get(which, {})
            dres = ix.tpcres.get("duck", {})
            phase_b = ch.fp_phase_b(tid)
            execq = {int(i) for i, v in res.items() if v[0] != "NONE"}

            def _c(m, i):
                v = m.get(str(i)) or m.get(i)
                return v[1] if v else 1.0

            def fpr(i):
                if i not in now:
                    now.update(tpcfp.fingerprints(which, {i}))
                return now.get(i)

            t0 = _proc_cpu()
            budget = FP_EXEC_BUDGET
            fail = False
            why = ""
            n_exec = n_duck = 0
            try:
                order = sorted(idxs)
                n_rel = len(idxs) if mode == "opt" else max(1, len(idxs & execq))
                _arm(t0, budget)
                fpr(order[0])
                if len(tpcfp._PAIRS.get(which, ())) != len(ref):
                    fail, why = True, "pairs"  # queries added/removed in the fixture
                # pass 1: fingerprint every reached query (cheap) and collect the subtests to re-run
                todo = []
                seen = 0

                def _cost(j, same):
                    if mode == "opt":
                        return FP_SCALE * (_c(dres.get(f"{which}:sql", {}), j) + _c(dres.get(f"{which}:opt", {}), j))
                    return FP_SCALE * (_c(res, j) + (0 if same else _c(dres.get(f"{which}:sql", {}), j)))
                for i in ([] if fail else order):
                    r = ref.get(str(i)) or ref.get(i)
                    _arm(t0, budget)
                    n = fpr(i)
                    if r is None or n is None:
                        fail, why = True, "nofp"; break
                    if len(n) > 3 and mode == "exec" and bool(n[3]) != (i in execq):
                        fail, why = True, "flag"; break  # fixture flipped the query's execute flag
                    if mode == "opt":
                        if n[1] != r[1] or n[2] != r[2]:
                            todo.append((i, None))
                        seen += 1
                        continue
                    if i not in execq:
                        # never compared: only an exception while parsing/pickling can fail it
                        if n[0].startswith("ERR") and n[0] != r[0]:
                            fail, why = True, "err"; break
                        continue
                    if n[0].startswith("ERR"):
                        fail, why = True, "err"; break
                    if n[0] != r[0] or n[1] != r[1] or phase_b:
                        todo.append((i, n[1] == r[1]))
                    seen += 1
                    if seen >= 10 and todo:
                        # on track to blow the budget: stop fingerprinting now
                        sofar = sum(_cost(j, same) for j, same in todo)
                        if (_proc_cpu() - t0) * n_rel / seen + FP_LOAD + sofar * n_rel / seen > budget:
                            fail, why = True, "proj"; break
                # cheapest re-checks first: more subtests settled per CPU second
                todo.sort(key=lambda t: _cost(*t))
                if not fail and todo:
                    est = sum(_cost(i, same) for i, same in todo) + (0 if loaded else FP_LOAD)
                    if (_proc_cpu() - t0) + est > budget:
                        fail, why = True, f"est{est:.1f}"
                # pass 2: re-run the subtests the test's own way, cheapest first; first failure stops
                for i, same in ([] if fail else todo):
                    if not loaded:
                        loaded.append(tpcres.load())
                    _arm(t0, budget, 3 * _cost(i, same) + 1.0)
                    if mode == "opt":
                        n_duck += 1
                        ok = tpcres.check_opt(loaded[0], i)
                    else:
                        n_exec += 1
                        ok = tpcres.check_exec(loaded[0], which, i, (res.get(str(i)) or res.get(i))[0], same)
                    if not ok:
                        fail, why = True, "result"; break
            except BaseException as e:  # noqa
                fail, why = True, "budget" if (e.__class__.__name__ == "_Timeout" or str(e) == "budget") else "crash"
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
            print(f"fp {tid.rsplit('::',1)[1]} reached={len(idxs)} exec={n_exec} duck={n_duck} fail={fail} {why} "
                  f"phaseB={phase_b} cpu={_proc_cpu() - t0:.2f}", file=sys.stderr)
            if fail:
                bad.add(tid)
    finally:
        signal.signal(signal.SIGALRM, prev)
    return bad


def state_diff(ch):
    """Import everything with the patched sources in a child; return the module-state keys
    that differ from the pinned commit's (None if unknown)."""
    base = getattr(ch.ix, "state", None)
    if not base or not base.get("snap"):
        return None
    mods = None
    if not ch.itime:
        # only module/class-level assignments changed: just the changed modules and the modules
        # that (transitively) import them can hold different state
        todo = [p for p in ch.diffpaths]
        seen = set(todo)
        while todo:
            p = todo.pop()
            for imp in ch.ix.imports.get(p, []):
                if imp not in seen:
                    seen.add(imp); todo.append(imp)
        mods = {modname(p) for p in seen if p.startswith("sqlglot/") and not p.endswith("__main__.py")}
    r, w = os.pipe()
    sys.stdout.flush(); sys.stderr.flush()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        try:
            os.dup2(2, 1)
            import signal
            signal.alarm(30)
            srcs = {}
            for p, t in ch.new_sources.items():
                if p.endswith(".py"):
                    srcs[modname(p)] = (os.path.join(REPO, p), t)
            for m in list(sys.modules):
                if m == "sqlglot" or m.startswith("sqlglot.") or m == "tests" or m.startswith("tests."):
                    del sys.modules[m]
            sys.meta_path.insert(0, PatchedFinder(srcs))
            os.environ["SKIP_INTEGRATION"] = "0"
            os.chdir(REPO)
            import state
            if mods is None:
                bad = state.import_everything(REPO)
                snap = state.snapshot()
            else:
                import importlib
                bad = list(base["bad"])
                for m in sorted(mods):
                    importlib.import_module(m)  # an import error -> no output -> unknown
                state.LAMBDA_CODE = True  # assignment-only edits: a lambda's code is part of the value
                snap = state.snapshot(mods=mods)
            b = json.dumps({"bad": bad, "snap": snap}).encode()
            while b:
                n = os.write(w, b); b = b[n:]
        except BaseException:  # noqa
            import traceback
            traceback.print_exc()
            sys.stderr.flush()
        finally:
            os._exit(0)
    os.close(w)
    chunks = []
    while True:
        b = os.read(r, 1 << 20)
        if not b:
            break
        chunks.append(b)
    os.close(r)
    _, _, ru = os.wait4(pid, 0)
    global ITIME_CPU
    ITIME_CPU += ru.ru_utime + ru.ru_stime
    try:
        d = json.loads(b"".join(chunks))
    except Exception:
        return None
    if sorted(d["bad"]) != sorted(base["bad"]):
        return None
    noisy = set(base["noisy"])
    old, new = (base["snap"] if mods is None else base["snapl"]), d["snap"]
    if mods is not None:
        # only what this partial import produced: lazily created package attributes (PEP 562)
        # and submodules nobody imported here are absent, not changed
        old = {k: v for k, v in old.items() if k.split(":", 1)[0] in mods and k in new}
    return sorted(k for k in set(old) | set(new) if k not in noisy and old.get(k) != new.get(k))


ITIME_CPU = 0.0
ALL_COST = 0.0


def run_child(ch, run_ids, funcs_by_file, wfd, timeouts=None, fp_ids=(), ix=None):
    import harness
    ok = True
    try:
        data_files = {p: t for p, t in ch.new_sources.items() if not p.endswith(".py")}
        install_redirects(data_files)
        if ch.reimport:
            # fresh import of sqlglot + tests with patched sources
            srcs = {}
            for p, t in ch.new_sources.items():
                if p.endswith(".py"):
                    srcs[modname(p)] = (os.path.join(REPO, p), t)
            for m in list(sys.modules):
                if m == "sqlglot" or m.startswith("sqlglot.") or m == "tests" or m.startswith("tests."):
                    del sys.modules[m]
            sys.meta_path.insert(0, PatchedFinder(srcs))
            harness._classes.clear()
            import importlib
            importlib.import_module("sqlglot")
            bad = harness.import_all(run_ids)
            harness.install_inline_pools()
        else:
            for p, info in ch.swap.items():
                old_src = open(os.path.join(REPO, p), encoding="utf-8").read()
                if not swap_functions(p, old_src, ch.new_sources[p], funcs_by_file.get(p, [])):
                    ok = False
        if not ok:
            os.write(wfd, b"SWAPFAIL")
            return
        tmo = timeouts or {}
        failed = set()
        if fp_ids:
            failed |= fp_failures(fp_ids, ch, ix)
        failed |= harness.run_tests(run_ids, timeout_for=lambda t: tmo.get(t, TEST_TIMEOUT))
        os.write(wfd, json.dumps(sorted(failed)).encode())
    except BaseException as e:  # noqa
        import traceback
        traceback.print_exc()
        os.write(wfd, b"CRASH")


def run_in_fork(ch, run_ids, funcs_by_file, timeouts=None, fp_ids=(), ix=None):
    r, w = os.pipe()
    sys.stdout.flush(); sys.stderr.flush()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        try:
            # stdout belongs to the parent's answers only
            os.dup2(2, 1)
            run_child(ch, run_ids, funcs_by_file, w, timeouts, fp_ids, ix)
        finally:
            os._exit(0)
    os.close(w)
    chunks = []
    while True:
        b = os.read(r, 1 << 16)
        if not b:
            break
        chunks.append(b)
    os.close(r)
    import resource
    _, _, ru = os.wait4(pid, 0)
    global LAST_CPU
    LAST_CPU = ru.ru_utime + ru.ru_stime
    out = b"".join(chunks)
    if out in (b"SWAPFAIL", b"CRASH", b""):
        return None, out.decode() or "EMPTY"
    return set(json.loads(out)), "ok"


def class_costs(ix):
    """Split measured per-test cpu into per-class setup + per-test cost."""
    setup = {}
    for i, tid in enumerate(ix.ids):
        c = tid.rsplit("::", 1)[0]
        setup[c] = min(setup.get(c, 1e9), ix.cpu[i])
    per = [CPU_SCALE * max(0.0, ix.cpu[i] - setup[tid.rsplit("::", 1)[0]] * 0.9) for i, tid in enumerate(ix.ids)]
    setup = {c: CPU_SCALE * v for c, v in setup.items()}
    return setup, per


def pack_cost(durs):
    w = pack(durs)
    if not w:
        return 0.0
    return (max(sum(durs[n] for n in x) for x in w) + WORKER_COST * len(w)) / 1000.0


def fixture_comment_only(ch):
    """True if every data-file change only touches '--' comment / blank lines."""
    for fp in ch.files:
        if fp.path.endswith(".py"):
            continue
        for _, body in fp.hunks:
            for tag, t in body:
                if tag in "+-" and t.strip() and not t.lstrip().startswith("--"):
                    return False
    return True


def enum_constants(path, exprs):
    """True if every expression evaluates (in the module's namespace) to an Enum member."""
    if not exprs:
        return True
    try:
        import enum
        import importlib
        mod = importlib.import_module(modname(path))
        for e in exprs:
            v = eval(e, dict(vars(mod)))
            if not isinstance(v, enum.Enum):
                return False
        return True
    except Exception:
        return False


def plan_commit(ch, ix, pos2num, ms, per, setup):
    """Return (run_positions, blind_positions, label)."""
    if ch is None or ch.unknown:
        return None, None, "unknown", []
    S = [p for p in analyze.bits(ch.precise) if pos2num[p] is not None]
    has_data = any(not fp.path.endswith(".py") for fp in ch.files)
    only_equiv = (not has_data and not ch.reimport and ch.swap and set(ch.swap) <= ch.equivalent
                  and all(enum_constants(p, ch.equiv_needs.get(p, ())) for p in ch.swap))
    fpc = [] if only_equiv else [p for p in S if ix.ids[p] in FP_TESTS and ch.fp_eligible(ix.ids[p])]
    ch.fpcheck = fpc
    S = [p for p in S if p not in set(fpc)]
    if not S:
        return [], [], "empty" if not fpc else "fp-only", []
    forced = [p for p in S if ix.ids[p] in NEVER_RUN or ix.ids[p] in ix.basefail]
    runnable = [p for p in S if p not in set(forced)]
    py_changed = bool(ch.swap or ch.reimport)
    if has_data and not fixture_comment_only(ch):
        q = 0.95
    else:
        q = 0.5
    ovh = 0.05 + (0.4 if ch.reimport else 0.0)

    def d(ps):
        return {pos2num[p]: ms[pos2num[p]] for p in ps}

    if only_equiv:
        # behaviour-preserving rewrite: run a cheap sentinel sample, include the rest only if it fails
        order = sorted(runnable, key=lambda p: per[p])
        rs, tot = [], 0.0
        seen_c, seen_m = set(), set()
        for p in order:
            tid = ix.ids[p]
            c = tid.rsplit("::", 1)[0]
            m = tid.split("::", 1)[0]
            add = per[p] + (setup[c] if c not in seen_c else 0) + (import_cost(seen_m | {m}) - import_cost(seen_m))
            if tot + add > EQUIV_BUDGET:
                continue
            rs.append(p); tot += add; seen_c.add(c); seen_m.add(m)
        rest = [p for p in S if p not in set(rs)]
        return rs, [], "equiv", rest

    def run_cost(rs):
        if not rs:
            return 0.0
        classes = {ix.ids[p].rsplit("::", 1)[0] for p in rs}
        mods = {ix.ids[p].split("::", 1)[0] for p in rs}
        return ovh + sum(per[p] for p in rs) + sum(setup[c] for c in classes) + import_cost(mods)

    best = None
    # candidate run sets: cheapest-first prefixes
    order = sorted(runnable, key=lambda p: per[p])
    cuts = sorted({0, len(order)} | {i for i in range(len(order) + 1) if i == 0 or per[order[i - 1]] <= BLIND_CPU})
    if os.environ.get("BR_FORCE_RUN"):
        cuts = [len(order)]
    for c in cuts:
        rs = order[:c]
        bs = forced + order[c:]
        blind_c = pack_cost(d(bs))
        # expected extra from failures among the run tests
        if rs:
            fail_extra = q * (2.0 + min(1.0, sum(ms[pos2num[p]] for p in rs) / 1000.0)) if not bs else q * 0.5
        else:
            fail_extra = 0.0
        est = run_cost(rs) + blind_c + fail_extra
        if best is None or est < best[0]:
            best = (est, rs, bs, run_cost(rs))
    rs, bs = best[1], best[2]
    if fpc and not rs:
        # checking the TPC tests costs CPU; only worth it if leaving them out shrinks the schedule
        base = {pos2num[p]: ms[pos2num[p]] for p in bs}
        with_fp = dict(base)
        with_fp.update({pos2num[p]: ms[pos2num[p]] for p in fpc})
        gain = pack_cost(with_fp) - pack_cost(base)
        if gain < FP_MIN_GAIN:
            ch.fpcheck = []
            bs = list(bs) + list(fpc)
    return rs, bs, f"est={best[0]:.2f} runest={best[3]:.2f}", []


def main():
    os.environ["SKIP_INTEGRATION"] = "0"
    tests = json.load(open(os.path.join(DATA, "tests.json")))["tests"]
    ix = analyze.Index(os.path.join(HERE, "index.bin"))
    # recorded as failing only because the map builder's line monitoring allocates under
    # tracemalloc; it passes in our harness at the pinned commit, so it can be run like any test
    ix.basefail.discard("tests/test_generator.py::TestGenerator::test_generator_memory")
    setup, per = class_costs(ix)
    num_of = {t["id"]: t["number"] for t in tests}
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    pos2num = [num_of.get(i) for i in ix.ids]
    cdir = os.path.join(DATA, "commits")
    commits = sorted(f[:-6] for f in os.listdir(cdir) if f.endswith(".patch"))
    jobs = []
    for cid in commits:
        text = open(os.path.join(cdir, cid + ".patch"), encoding="utf-8", errors="replace").read()
        try:
            ch = analyze.Change(cid, text, REPO, ix)
            if ch.itime and not ch.unknown and ch.precise.bit_count() > len(ix.ids) // 2:
                # the changed code already runs in most tests: schedule everything, skip the checks
                ch.unknown = True
                ch.notes.append("broad import-time change -> all tests")
            if (ch.itime or ch.diffpaths) and not ch.unknown:
                ch.apply_itime(state_diff(ch))
        except Exception as e:  # noqa
            log("analysis failed", cid, e)
            ch = None
        run, blind, label, onfail = plan_commit(ch, ix, pos2num, ms, per, setup)
        jobs.append((cid, ch, run, blind, label, onfail))

    log(f"analysis cpu {time.process_time() - T0:.2f}s (+import-time diffs {ITIME_CPU:.2f}s)")
    need_ids = set()
    swap_files = set()
    for cid, ch, run, blind, label, onfail in jobs:
        if run:
            need_ids.update(ix.ids[p] for p in run)
        if run or (ch is not None and getattr(ch, "fpcheck", None)):
            swap_files.update(ch.swap)
    funcs_by_file = {}
    if need_ids or swap_files:
        import harness
        import importlib
        import sqlglot  # noqa
        for p in sorted(swap_files):
            try:
                importlib.import_module(modname(p))
            except Exception as e:  # noqa
                log("import", p, e)
        log(f"swap-imports cpu {time.process_time() - T0:.2f}s")
        harness.import_all(sorted(need_ids))
        log(f"test-imports cpu {time.process_time() - T0:.2f}s")
        if any(t.startswith(("tests/test_executor.py", "tests/test_optimizer.py")) for t in need_ids):
            harness.install_inline_pools()
        if swap_files:
            import types
            fulls = {os.path.join(REPO, p): p for p in swap_files}
            for o in gc.get_objects():
                if isinstance(o, types.FunctionType):
                    p = fulls.get(o.__code__.co_filename)
                    if p:
                        funcs_by_file.setdefault(p, []).append(o)
        gc.collect()
        gc.freeze()
    log(f"setup cpu {time.process_time() - T0:.2f}s")

    all_nums = sorted(ms)
    global ALL_COST
    ALL_COST = pack_cost(ms)
    for cid, ch, run, blind, label, onfail in jobs:
        if run is None:
            sel = {n: ms[n] for n in all_nums}
            log(cid, "UNKNOWN -> all tests", ch.notes if ch else "")
        else:
            failed_pos = set()
            status = "none"
            cpu = 0.0
            fpc = getattr(ch, "fpcheck", []) if ch is not None else []
            if run or fpc:
                rid = [ix.ids[p] for p in run]
                tmo = {ix.ids[p]: max(2.0, 10 * ix.cpu[p]) for p in run}
                res, status = run_in_fork(ch, rid, funcs_by_file, tmo, [ix.ids[p] for p in fpc], ix)
                cpu = LAST_CPU
                allp = list(run) + list(fpc)
                failed_pos = set(allp) if res is None else {p for p in allp if ix.ids[p] in res}
            sel_pos = failed_pos | set(blind)
            if failed_pos and onfail:
                sel_pos |= set(onfail)
            sel = {pos2num[p]: ms[pos2num[p]] for p in sel_pos}
            log(cid, f"{label} fp={len(fpc)} run={len(run)} blind={len(blind)} failed={len(failed_pos)} status={status} cpu={cpu:.2f} notes={ch.notes}")
            if sel and len(sel) < len(all_nums) and pack_cost(sel) >= ALL_COST - 1e-9:
                # scheduling every test costs no more than this selection: free insurance
                sel = {n: ms[n] for n in all_nums}
                log(cid, "selection already costs as much as all tests -> all tests")
        print(json.dumps({"commit": cid, "workers": pack(sel)}), flush=True)
    log(f"total cpu (parent) {time.process_time() - T0:.2f}s")


if __name__ == "__main__":
    main()
