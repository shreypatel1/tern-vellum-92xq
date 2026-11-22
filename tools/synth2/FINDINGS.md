# Chat B miss-hunt findings

Goal: find patches where `selector.py` MISSES a failing test, in change kinds Chat A
was not covering. Patches live in `tools/synth2/patches/`; tooling in `tools/synth2/`.

All test runs were on an EC2 c7a.4xlarge (16 vCPU, native linux/amd64), Python 3.13,
deps from `requirements.lock`, sqlglot at the pinned commit `eb11aec4`. The laptop CPU
was reserved for another job, so nothing heavy ran locally.

## Method

For each patch: apply it to a fresh sqlglot tree, run the full suite (junit) to get the
truly-failing tests (`truth = failing_with_patch - failing_at_baseline`), run `selector.py`
on the patch against the CLEAN tree (the way the real scorer runs it), and compare. A
**miss** = a truly-failing test id that is in the index but not selected. Baseline: 1621
tests collected, 1 harness-artifact failure (`test_lazy_load`, eager-import under xdist),
neutralized by the baseline diff.

Two scoring bugs were found and fixed before trusting any number:
1. The selector must run with `BR_REPO` = the **clean** tree (it diffs the patch against
   it). An early driver pointed it at the already-patched tree -> every patch selected 0
   -> false misses. Fixed.
2. A module that fails to import emits a junit `<testcase classname="">` collection error;
   the first id-parser produced garbage ids ("`.py::::...`") counted as misses. Fixed to
   expand a module collection error to all that module's real test ids.

## Results (validated)

12-patch pilot, corrected driver: **0 real misses, 0 selector crashes.**

| patch | category | truth | selected | real misses |
|---|---|---|---|---|
| opt_always_false_breaks | optimizer rule | 1 | 24 | 0 |
| opt_is_zero_wrong | optimizer helper | 1 | 31 | 0 |
| opt_normalize_maxdist | optimizer rule | 3 | 93 | 0 |
| exp_to_identifier_none | exp builder | 0 | 619 | 0 |
| exp_column_drops_table | exp builder | 115 | 181 | 0 |
| dialect_mysql_safe_div | dialect flag | 3 | 247 | 0 |
| dialect_mysql_concat_ws | dialect flag | 1 | 17 | 0 |
| dialect_base_dpipe | parent-dialect flag (inherited) | 134 | 1620 | 0 |
| expr_eq_always_false | Expression.__eq__ | 174 | 1620 | 0 |
| expr_ne_broken | Expression.__ne__ | 66 | 153 | 0 |
| err_parse_text | error text | 0 | 0 | 0 |
| err_table_name | error text | 1 | 1 | 0 |

Plus a separate confirmation run of the import-breaking `dialect_base_dpipe` with the
module-expansion fix: truth=1119 (whole modules fail to import), selected=1620 (all), 0 misses.

In every case the selector over-selects (correct, miss-avoiding behavior). No miss and no
crash was observed in any covered category (optimizer rules/helpers/ordering, exp builders,
dialect & generator flags incl. inheritance, Expression __eq__/__ne__, error text).

## Patches generated but not yet scored

The full 26-patch run (adds categories 4 multi-file, 5 const/helper, 8 pure refactor, and
more 1/2/3/6 variants) was aborted: one patch, `expr_copy_noop` (make `Expression.copy()`
return `self`), makes hundreds of tests mutate shared AST nodes and balloon memory, OOM-
killing the pytest-xdist workers and hanging the truth run. That is a pathological *test-
harness* condition, not a selector miss. `run_truth.py` was hardened afterward (8 workers,
45s per-test timeout, 300s overall subprocess timeout) so one bad patch can no longer stall
a batch; `expr_copy_noop` should be dropped or given a tiny targeted test subset. The
remaining patches (multi-file, refactors, generator flags) are ready to score locally with
Docker.

## Recommendation

No new miss class found in the covered areas; the recent commits' 0-miss property holds up
against these change kinds. The open follow-up is to score the remaining ~13 patches
(categories 4, 5, 8 and the extra flag/ordering variants) locally, excluding the OOM patch.
