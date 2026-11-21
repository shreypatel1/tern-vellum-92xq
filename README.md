# Blast Radius selector

Picks the sqlglot tests a patch can break and splits them over up to 8 workers.

## How it works

1. **Coverage map (built offline from the pinned commit, committed as `index.bin`).**
   Every test runs in its own forked process with `sys.monitoring` line events on, so each
   test's set of executed sqlglot/tests lines, the files it opens and the expression classes it
   instantiates are recorded without cross-test cache effects. Process pools in the executor and
   optimizer tests are replaced by an in-process executor that still pickles arguments and results,
   so code reached through pickling (serde) is attributed correctly. `tools/` holds the builders.
2. **Patch analysis.** Each diff is applied in memory and classified with the AST:
   comment-only, function-body-only (which functions), or structural (module/class level). The
   reach of a change is the set of tests that executed the changed statements; for constant
   assignments it is the tests that ran code reading that name; for expression classes it is the
   tests that built one; for data files it is the tests that opened them.
3. **Run or include.** For each commit a small cost model compares running reachable tests inside
   a forked child (patched function code objects are swapped into the live functions, or the
   patched modules are re-imported for structural edits) against simply scheduling them. Tests that
   are expensive to run are scheduled instead; tests that are cheap are run and only failures are
   scheduled. `is`/`==` rewrites against `None` or enum members are treated as behaviour-preserving
   and only get a cheap sentinel run.
4. **Packing.** Longest-processing-time packing for every worker count 1..8, keeping the one that
   minimises busiest worker + 2 s per worker.

## Layout

- `selector.py` entry point, `analyze.py` reach, `patchinfo.py` diff + AST classification,
  `harness.py` unittest runner matching pytest's pass/fail rules.
- `tools/build_maps.py`, `tools/names.py`, `tools/build_index.py` rebuild `index.bin`;
  `tools/build_importcalls.py` then adds which functions run during import, and
  `tools/build_clsuse.py` adds, per test, the dialect classes it touched (and the
  tokenizer/parser/generator classes only reachable through them), used to narrow edits
  to one dialect's constants and to widen structural edits to pure-configuration dialects.

MIT licensed.
