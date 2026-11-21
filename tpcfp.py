"""Fingerprints of what the TPC-H/TPC-DS executor tests feed into the engines.

test_execute_tpc*: each query is parsed, pickled, optimized (as sqlglot.executor.execute does)
and then planned + executed in Python; the expected side runs transpile(sql, "duckdb") in DuckDB.
test_optimized_tpch: DuckDB runs transpile(sql) and transpile(fixture's optimized sql).
If the optimized expression and both transpiled strings are unchanged, and the change does not
touch code that runs during planning/execution, the test's outcome cannot change."""
import hashlib
import json
import pickle

FIXTURE = {"tpch": "optimizer/tpc-h/tpc-h.sql", "tpcds": "optimizer/tpc-ds/tpc-ds.sql"}


def _h(s):
    return hashlib.sha1(s.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def _safe(fn):
    try:
        return _h(fn())
    except BaseException as e:  # noqa
        if e.__class__.__name__ == "_Timeout":
            raise
        return "ERR:" + e.__class__.__name__




# A private copy of sqlglot.serde.dump at the pinned commit: the fingerprint must describe the
# expression tree itself, so a patch to serde.py (which also drives pickling) changes it only if
# the round-tripped tree changes, and a patched dump cannot hide a changed tree.
INDEX = "i"
ARG_KEY = "k"
IS_ARRAY = "a"
CLASS = "c"
TYPE = "t"
COMMENTS = "o"
META = "m"
VALUE = "v"
DATA_TYPE = "DataType.Type"


def _dump(expression):
    """
    Dump an Expr into a JSON serializable List.
    """
    exp = _exp()
    i = 0
    payloads = []
    stack = [(expression, None, None, False)]

    while stack:
        node, index, arg_key, is_array = stack.pop()

        payload = {}

        if index is not None:
            payload[INDEX] = index
        if arg_key is not None:
            payload[ARG_KEY] = arg_key
        if is_array:
            payload[IS_ARRAY] = is_array

        payloads.append(payload)

        if hasattr(node, "parent"):
            klass = node.__class__.__qualname__

            if node.__class__.__module__ != exp.__name__:
                klass = f"{node.__module__}.{klass}"

            payload[CLASS] = klass

            if node.type and node.type is not node:
                payload[TYPE] = _dump(node.type)
            if node.comments:
                payload[COMMENTS] = node.comments
            if node._meta is not None:
                payload[META] = node._meta
            if node.args:
                for k, vs in reversed(node.args.items()):
                    if type(vs) is list:
                        for v in reversed(vs):
                            stack.append((v, i, k, True))
                    elif vs is not None:
                        stack.append((vs, i, k, False))
        elif type(node) is exp.DType:
            payload[CLASS] = DATA_TYPE
            payload[VALUE] = node.value
        else:
            payload[VALUE] = node

        i += 1

    return payloads


def _exp():
    from sqlglot import expressions
    return expressions


_PAIRS = {}


def fingerprints(which, idxs=None):
    from sqlglot import parse_one, transpile, serde
    from sqlglot.optimizer import optimize
    from sqlglot.schema import ensure_schema
    from tests.helpers import load_sql_fixture_pairs
    import tests.helpers as H
    schema = H.TPCH_SCHEMA if which == "tpch" else H.TPCDS_SCHEMA
    if which not in _PAIRS:
        _PAIRS[which] = list(load_sql_fixture_pairs(FIXTURE[which]))
    pairs = _PAIRS[which]
    out = {}
    for i, (meta, sql, optimized) in enumerate(pairs):
        if idxs is not None and i not in idxs:
            continue

        execute = bool(meta.get("execute"))

        def ex():
            # the test always pickles the parsed query into the pool, executed or not
            e = pickle.loads(pickle.dumps(parse_one(sql)))
            parts = [json.dumps(_dump(e), sort_keys=True, default=str)]
            if execute:
                s = pickle.loads(pickle.dumps(schema))
                o = optimize(e, ensure_schema(s), leave_tables_isolated=True)
                parts.append(json.dumps(_dump(o), sort_keys=True, default=str))
            return "\n".join(parts)

        fe = _safe(ex)
        t1 = _safe(lambda: transpile(sql, write="duckdb")[0])
        t2 = _safe(lambda: transpile(optimized, write="duckdb")[0])
        out[i] = [fe, t1, t2, execute]
    return out


if __name__ == "__main__":
    import os, sys
    sys.path.insert(0, sys.argv[1])
    os.chdir(sys.argv[1])
    json.dump({w: fingerprints(w) for w in FIXTURE}, open(sys.argv[2], "w"))
