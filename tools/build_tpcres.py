"""Baseline Python-executor results of the TPC executor-test queries (hash + cpu per query)."""
import hashlib
import os
import pickle
import resource
import sys


def _cpu():
    r = resource.getrusage(resource.RUSAGE_SELF)
    return r.ru_utime + r.ru_stime


def result_hash(table):
    if table is None:
        return "NONE"
    return hashlib.sha1(repr((list(table.columns), list(table.rows))).encode("utf-8", "surrogatepass")).hexdigest()[:16]


def load():
    """Import the executor test module and build its tables (class setup)."""
    import harness
    tid = "tests/test_executor.py::TestExecutor::test_execute_tpch"
    harness.import_all([tid])
    cls, _ = harness.get_class(tid)
    cls.setUpClass()
    return cls


def run(cls, which, idxs):
    import tests.test_executor as T
    from sqlglot import parse_one
    schema = T.TPCH_SCHEMA if which == "tpch" else T.TPCDS_SCHEMA
    tables = cls.tpch_tables if which == "tpch" else cls.tpcds_tables
    sqls = cls.tpch_sqls if which == "tpch" else cls.tpcds_sqls
    T.initializer(schema, tables)
    out = {}
    for i in sorted(idxs):
        args, sql, _ = sqls[i]
        t = _cpu()
        try:
            e = pickle.loads(pickle.dumps(parse_one(sql)))
            tb = T.mp_execute(e, args)
            h = result_hash(pickle.loads(pickle.dumps(tb)))
        except BaseException as ex:  # noqa
            if ex.__class__.__name__ == "_Timeout":
                raise
            h = "ERR:" + ex.__class__.__name__
        out[i] = [h, round(_cpu() - t, 4)]
    return out


def df_hash(df):
    s = repr(list(df.columns)) + repr([str(d) for d in df.dtypes]) + df.to_csv(index=False)
    return hashlib.sha1(s.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def duck(cls, which, kind, idxs):
    """Hash DuckDB's result for transpile(sql) (kind 'sql') or transpile(optimized) ('opt')."""
    from sqlglot import transpile
    conn = cls.tpch_conn if which == "tpch" else cls.tpcds_conn
    sqls = cls.tpch_sqls if which == "tpch" else cls.tpcds_sqls
    out = {}
    for i in sorted(idxs):
        _, sql, optimized = sqls[i]
        t = _cpu()
        try:
            h = df_hash(conn.execute(transpile(sql if kind == "sql" else optimized, write="duckdb")[0]).fetchdf())
        except BaseException as ex:  # noqa
            if ex.__class__.__name__ == "_Timeout":
                raise
            h = "ERR:" + ex.__class__.__name__
        out[i] = [h, round(_cpu() - t, 4)]
    return out


if __name__ == "__main__":
    import json
    repo = sys.argv[1]
    sys.path.insert(0, repo)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    os.chdir(repo)
    os.environ["SKIP_INTEGRATION"] = "0"
    cls = load()
    n = {"tpch": len(cls.tpch_sqls), "tpcds": len(cls.tpcds_sqls)}
    res = {w: run(cls, w, range(n[w])) for w in ("tpch", "tpcds")}
    res["duck"] = {"tpch:sql": duck(cls, "tpch", "sql", range(n["tpch"])), "tpch:opt": duck(cls, "tpch", "opt", range(n["tpch"])),
                   "tpcds:sql": duck(cls, "tpcds", "sql", range(n["tpcds"]))}
    json.dump(res, open(sys.argv[2], "w"))
    for w, m in list(res.items())[:2] + list(res["duck"].items()):
        print(w, len(m), round(sum(v[1] for v in m.values()), 2), sum(v[0].startswith("ERR") for v in m.values()), file=sys.stderr)
