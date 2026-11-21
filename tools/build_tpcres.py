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


if __name__ == "__main__":
    import json
    repo = sys.argv[1]
    sys.path.insert(0, repo)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    os.chdir(repo)
    os.environ["SKIP_INTEGRATION"] = "0"
    cls = load()
    res = {w: run(cls, w, range(len(cls.tpch_sqls if w == "tpch" else cls.tpcds_sqls))) for w in ("tpch", "tpcds")}
    json.dump(res, open(sys.argv[2], "w"))
    for w, m in res.items():
        print(w, len(m), round(sum(v[1] for v in m.values()), 2), sum(v[0].startswith("ERR") for v in m.values()), file=sys.stderr)
