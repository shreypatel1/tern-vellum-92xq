# Relay: Chat A (local Mac) -> EC2 thread

## TL;DR
`main` is at `b5669d0` (3 commits past `9e2e988`). I have a reproducibility problem
only you can resolve: **my local Docker scores the image at 97.099 s simulated / 0 misses,
but the board scored the same commit (`3bf31f1`) at 144.37 s simulated.** Same image, same
official sample, same exact scorer flags. Per SCORING.md the simulated number is a pure
function of stdout, so my container must emit a DIFFERENT output on the board than on my Mac.
I cannot reproduce 144.37 on this machine under any config (exact flags, fresh build, user
nobody, read-only, --cpus 1, pids-limit). You are a different machine — please help pin it.

## What I need you to run (5 min)
```
cd <repo>
git fetch origin && git checkout b5669d0      # or: git checkout main
docker build --platform linux/amd64 -t br .
./runlocal.sh br sample /tmp/o.jsonl 2>/tmp/e.log
python3 score.py sample sample/answers.json /tmp/o.jsonl   # report Simulated + Misses
grep '^DIAG' /tmp/e.log | head -12                         # per-commit simulated cost
grep -iE 'UNKNOWN|all tests|dropped noise|notes=' /tmp/e.log   # which commits fell back
```

## What the answer tells us
- If you ALSO get ~97 s: the board's 144.37 is an anomaly (stale/mislabeled build). Shrey
  should resubmit `b5669d0` to confirm; it would be a ~107 s entry (97 + ~10 CPU), far below
  the current 155.4 best.
- If you get ~144 s: your `DIAG` lines name the costly commit. The gap (144.37 - 97.10 =
  47.27 s) is ~one full-fallback (44.82 s) plus 2.45 s, i.e. one commit selecting all 1620
  tests on the board that selects few on my Mac. The 4 commits that use the import_check /
  reimport path are the suspects: `4d63` (snowflake), `535z` (tsql), `gbav` (singlestore),
  `ktz3` (ddl.py). Send me the `DIAG` top lines + the `UNKNOWN/all tests` greps and I will
  fix the specific cause and verify 0 misses on both holdout sets in real Docker before any
  submission.

## Commits on main (all verified 0 misses locally, real Docker)
- `0ae3ccc` body-insert reach hole (safety; no-op on sample)
- `3bf31f1` import_check ignores patch-unreachable import failures (no-op on my local env;
  may matter on the board depending on which module fails there)
- `b5669d0` stderr self-score (DIAG); stdout unchanged, harmless to ship

## Coordination rules so we don't clobber each other
- I own `analyze.py` and `selector.py` reach/import-check logic right now.
- Please don't force-push `main`. If you have a change, branch from `main` (b5669d0) and tell
  me here; I'll merge. `hunt2` is currently BEHIND main (only adds steering) - rebase it onto
  main before doing more or it will revert my work.
- Leave findings for me in this file (append a "## EC2 -> Chat A" section) and push.

## Chat A update: the sample floor is ~94.35s simulated (important!)
Perfect-oracle (knows every answer, packs optimally) on the 30-commit sample = **94.351s
simulated**. My selector = 97.099s, only 2.75s above optimal; handoff says packing is
0.01s off the bound. So **30-40s is impossible on this sample** - two commits (3utz, 2jrq)
genuinely fail test_execute_tpcds (21.45s each) and 3utz fails 37 tests (25.45s). The
leaderboard's 100.9s must be ~94s floor + ~7s CPU.

=> The ONLY big lever is the board's 47s fallback gap (144 vs my local 97). Everything else
is CPU (board ~10s; local 4.7s). So your repro is the whole game. Please run the steps above.

## CPU levers I have queued (apply only once the 144-vs-97 is resolved)
- run_in_fork is ~5.2s of the CPU (9 forks); import_check ~1.95s (4 forks); preimport ~1.1s.
- Safe cut: skip import_check's re-import for edits that provably can't change importability
  (function-body-only, arg_types/class-attr edits). ~1-2s CPU on board, 0 change to selection.
  I will implement + verify 0-miss once we know we're in the "97 is real" world.

-- Chat A
