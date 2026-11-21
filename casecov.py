"""Fixture-case filtering for the fixture-driven tests (load_sql_fixture_pairs loops).

A test that loops over the pairs of a fixture file can be run on a subset of its cases by
filtering the generator the test module imported. Each case's recorded coverage (casecov.bin,
built by tools/build_casecov.py, every case alone in a fresh fork) says which cases a function
body edit can reach; the other cases run the same code as at the pinned commit."""
import os
import pickle
import zlib

FILTER = {}  # fixture filename -> set of case indices to yield (absent: all)
LOADED = set()
MODULES = ("tests.test_optimizer", "tests.test_transpile")
_installed = []


def install():
    """Wrap load_sql_fixture_pairs in the test modules that loop over fixture files."""
    if _installed:
        return
    import importlib
    import tests.helpers as H
    orig = H.load_sql_fixture_pairs

    def load_sql_fixture_pairs(filename):
        LOADED.add(filename)
        keep = FILTER.get(filename)
        for k, pair in enumerate(orig(filename)):
            if keep is None or k in keep:
                yield pair

    for m in MODULES:
        try:
            mod = importlib.import_module(m)
        except Exception:  # noqa
            continue
        if getattr(mod, "load_sql_fixture_pairs", None) is orig:
            mod.load_sql_fixture_pairs = load_sql_fixture_pairs
    _installed.append(orig)


def load(path):
    try:
        return pickle.loads(zlib.decompress(open(path, "rb").read()))
    except Exception:  # noqa
        return {}
