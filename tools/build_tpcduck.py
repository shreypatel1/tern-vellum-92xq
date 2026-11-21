"""DuckDB results of the TPC queries on tables materialized from the CSVs (single thread).
usage: build_tpcduck.py REPO OUT

Reading the gzipped CSV views costs ~0.2 s per TPC-DS query; materialized tables answer the same
queries in milliseconds. The selector compares a patched transpilation's result on these tables
to the pinned one stored here."""
import json
import os
import sys

if __name__ == "__main__":
    repo = sys.argv[1]
    sys.path.insert(0, repo)
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    os.chdir(repo)
    os.environ["SKIP_INTEGRATION"] = "0"
    import tpcres
    out = {}
    for which in ("tpch", "tpcds"):
        conn, sqls = tpcres.mconn(which)
        for kind in ("sql", "opt") if which == "tpch" else ("sql",):
            out[f"{which}:{kind}"] = {str(i): tpcres.mhash(conn, which, kind, i, sqls) for i in range(len(sqls))}
    json.dump(out, open(sys.argv[2], "w"), indent=0, sort_keys=True)
