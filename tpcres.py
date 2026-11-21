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



def check_exec(cls, which, i, base_hash, duck_same):
    """One subtest of test_execute_tpc*, as the test runs it, for query i with the live code.
    The Python result is compared to the pinned result hash first: equal and the DuckDB side
    unchanged means the subtest passes as it did at the pinned commit. Otherwise the subtest's
    own comparison (assert_frame_equal against DuckDB) decides. Returns True if it passes."""
    import tests.test_executor as T
    from sqlglot import parse_one, transpile
    schema = T.TPCH_SCHEMA if which == "tpch" else T.TPCDS_SCHEMA
    tables = cls.tpch_tables if which == "tpch" else cls.tpcds_tables
    sqls = cls.tpch_sqls if which == "tpch" else cls.tpcds_sqls
    conn = cls.tpch_conn if which == "tpch" else cls.tpcds_conn
    T.initializer(schema, tables)
    args, sql, _ = sqls[i]
    try:
        e = pickle.loads(pickle.dumps(parse_one(sql)))
        tb = pickle.loads(pickle.dumps(T.mp_execute(e, args)))
    except BaseException as ex:  # noqa
        if ex.__class__.__name__ == "_Timeout":
            raise
        return False
    if tb is None:
        return True
    if duck_same and result_hash(tb) == base_hash:
        return True
    try:
        import numpy as np
        import pandas as pd
        from pandas.testing import assert_frame_equal
        a = conn.execute(transpile(sql, write="duckdb")[0]).fetchdf()
        b = pd.DataFrame(((np.nan if c is None else c for c in r) for r in tb.rows), columns=tb.columns)
        assert_frame_equal(a, b, check_dtype=False, check_index_type=False)
        return True
    except BaseException as ex:  # noqa
        if ex.__class__.__name__ == "_Timeout":
            raise
        return False


def check_opt(cls, i):
    """One subtest of test_optimized_tpch, as the test runs it, with the live code."""
    from sqlglot import transpile
    from pandas.testing import assert_frame_equal
    conn = cls.tpch_conn
    _, sql, optimized = cls.tpch_sqls[i]
    try:
        a = conn.execute(transpile(sql, write="duckdb")[0]).fetchdf()
        b = conn.execute(transpile(optimized, write="duckdb")[0]).fetchdf()
        import tests.test_executor as T
        T.TestExecutor.rename_anonymous(None, b, a)
        assert_frame_equal(a, b)
        return True
    except BaseException as ex:  # noqa
        if ex.__class__.__name__ == "_Timeout":
            raise
        return False


_MCONN = {}


def mconn(which):
    """A DuckDB connection with the TPC tables materialized (single thread), and the fixture pairs."""
    if which not in _MCONN:
        import duckdb
        import tests.test_executor as T
        from tests.helpers import load_sql_fixture_pairs
        schema = T.TPCH_SCHEMA if which == "tpch" else T.TPCDS_SCHEMA
        d = T.DIR_TPCH if which == "tpch" else T.DIR_TPCDS
        conn = duckdb.connect()
        conn.execute("SET threads=1")
        for table, columns in schema.items():
            conn.execute(
                f"CREATE TABLE {table} AS SELECT * FROM READ_CSV('{d}{table}.csv.gz', delim='|', header=True, columns={columns})")
        fx = "optimizer/tpc-h/tpc-h.sql" if which == "tpch" else "optimizer/tpc-ds/tpc-ds.sql"
        _MCONN[which] = (conn, list(load_sql_fixture_pairs(fx)))
    return _MCONN[which]


def mhash(conn, which, kind, i, sqls):
    """Result hash of transpile(sql or optimized, duckdb) for query i on the materialized tables."""
    from sqlglot import transpile
    _, sql, optimized = sqls[i]
    try:
        return df_hash(conn.execute(transpile(sql if kind == "sql" else optimized, write="duckdb")[0]).fetchdf())
    except BaseException as ex:  # noqa
        if ex.__class__.__name__ == "_Timeout":
            raise
        return "ERR:" + ex.__class__.__name__

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
