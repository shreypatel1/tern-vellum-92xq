# Blast Radius handoff (read this first)

Track: pick the sqlglot tests a patch can break and split them over up to 8 workers; scored on time with zero misses.
Board: 73b56de scored 155.4 (current final entry). Goal was < 126.
Since then on main: 1846440 (closes 3 reach holes, 0 misses on all 6 local holdout sets), 9e2e988 (about 1 s less CPU per 30 commits, 0 misses). Neither was submitted.
Worker packing is already optimal (0.01 s off the lower bound). Board charges ~2x our local CPU.
Next idea that was being verified: skip hopeless TPC executor checks up front.
Local scoring: see README.md, score.py, runlocal.sh. Never submit a build with a miss on any holdout set.
Kiro credits are scarce: open only README.md and the files you need.
