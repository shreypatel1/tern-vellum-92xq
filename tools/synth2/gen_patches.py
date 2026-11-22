#!/usr/bin/env python3
"""Generate synthetic patches for the Chat B miss-hunt (8 categories).

Each patch is a unified diff vs the sqlglot repo root. We build them by editing
the real source in memory and diffing, so find-strings must match exactly once.
Most edits inject a behavior change that makes some tests fail; a few are pure
no-op refactors that must NOT change behavior (selector must not crash, and
ideally selects little). The scorer measures whether the selector reaches every
truly-failing test.
"""
import sys, os, difflib, json

SQ = os.environ.get("BR_REPO", os.path.expanduser("~/sq"))

def read(rel):
    with open(os.path.join(SQ, rel), encoding="utf-8") as f:
        return f.read()

def make_diff(rel, old_text, new_text):
    a = old_text.splitlines(keepends=True)
    b = new_text.splitlines(keepends=True)
    return "".join(difflib.unified_diff(a, b, fromfile=f"a/{rel}", tofile=f"b/{rel}", n=3))

SPECS = []
def spec(name, cat, edits, behavior="change"):
    SPECS.append({"name": name, "cat": cat, "edits": edits, "behavior": behavior})

C = "sqlglot/expressions/core.py"

# ===== category 1: optimizer rules / helpers / ordering =====
spec("opt_always_false_breaks", 1, [("sqlglot/optimizer/simplify.py",
    "    return is_false(expression) or is_null(expression) or is_zero(expression)",
    "    return is_false(expression) or is_zero(expression)")])
spec("opt_is_zero_wrong", 1, [("sqlglot/optimizer/simplify.py",
    "    return isinstance(expression, exp.Literal) and expression.to_py() == 0",
    "    return isinstance(expression, exp.Literal) and expression.to_py() == 1")])
spec("opt_normalize_maxdist", 1, [("sqlglot/optimizer/normalize.py",
    "def normalize(expression: exp.Expr, dnf: bool = False, max_distance: int = 128) -> exp.Expr:",
    "def normalize(expression: exp.Expr, dnf: bool = False, max_distance: int = 0) -> exp.Expr:")])

# ===== category 2: exp builder helpers =====
spec("exp_to_identifier_none", 2, [(C,
    "    if isinstance(name, Identifier):\n        identifier = maybe_copy(name, copy)",
    "    if isinstance(name, Identifier):\n        identifier = None  # SYNTH bug\n        identifier = maybe_copy(name, copy)")])
spec("exp_column_drops_table", 2, [(C,
    "        table=to_identifier(table, quoted=quoted, copy=copy),\n        db=to_identifier(db, quoted=quoted, copy=copy),",
    "        table=None,  # SYNTH bug: drop table\n        db=to_identifier(db, quoted=quoted, copy=copy),")])

# ===== category 3: dialect Generator settings + inheritance =====
spec("dialect_mysql_safe_div", 3, [("sqlglot/dialects/mysql.py",
    "    SAFE_DIVISION = True", "    SAFE_DIVISION = False")])
spec("dialect_mysql_concat_ws", 3, [("sqlglot/dialects/mysql.py",
    "    CONCAT_WS_COALESCE = True", "    CONCAT_WS_COALESCE = False")])
# parent-dialect change children inherit: edit Dialect base flag
spec("dialect_base_dpipe", 3, [("sqlglot/dialects/dialect.py",
    "    DPIPE_IS_STRING_CONCAT = True", "    DPIPE_IS_STRING_CONCAT = False")])

# ===== category 5: module-level constants / literals =====
spec("const_safe_identifier_re", 5, [(C,
    "SAFE_IDENTIFIER_RE", "SAFE_IDENTIFIER_RE_SYNTH")], behavior="maybe_crash")

# ===== category 6: Expression __eq__/__hash__/copy behavior =====
spec("expr_eq_always_false", 6, [(C,
    "        return self is other or (type(self) is type(other) and hash(self) == hash(other))",
    "        return self is other")])
spec("expr_ne_broken", 6, [(C,
    "    def __ne__(self, other: object) -> bool:\n        return not self.__eq__(other)",
    "    def __ne__(self, other: object) -> bool:\n        return self.__eq__(other)  # SYNTH bug")])

# ===== category 7: error messages / exception classes =====
spec("err_parse_text", 7, [("sqlglot/parser.py",
    'self.raise_error("Expected CTE to have alias")',
    'self.raise_error("CTE requires an alias")')])
spec("err_table_name", 7, [("sqlglot/parser.py",
    'self.raise_error(f"Expected table name but got {self._curr}")',
    'self.raise_error(f"table name required, got {self._curr}")')])

# ===== more category 1: rule ordering + helpers =====
spec("opt_rules_drop_simplify", 1, [("sqlglot/optimizer/optimizer.py",
    "    canonicalize,\n    simplify,\n)",
    "    canonicalize,\n)")])
spec("opt_rules_reorder", 1, [("sqlglot/optimizer/optimizer.py",
    "    qualify,\n    pushdown_projections,\n    normalize,",
    "    qualify,\n    normalize,\n    pushdown_projections,")])

# ===== more category 2: builders =====
spec("exp_and_becomes_or", 2, [(C,
    "    return t.cast(Condition, _combine(expressions, And, dialect, copy=copy, wrap=wrap, **opts))",
    "    return t.cast(Condition, _combine(expressions, Or, dialect, copy=copy, wrap=wrap, **opts))")])

# ===== more category 3: generator flags (affect many dialects) =====
spec("gen_null_ordering_off", 3, [("sqlglot/generator.py",
    "    NULL_ORDERING_SUPPORTED: bool | None = True",
    "    NULL_ORDERING_SUPPORTED: bool | None = False")])
spec("gen_wrap_derived_off", 3, [("sqlglot/generator.py",
    "    WRAP_DERIVED_VALUES = True", "    WRAP_DERIVED_VALUES = False")])
spec("gen_except_all_off", 3, [("sqlglot/generator.py",
    "    EXCEPT_INTERSECT_SUPPORT_ALL_CLAUSE = True",
    "    EXCEPT_INTERSECT_SUPPORT_ALL_CLAUSE = False")])
spec("dialect_base_udt_off", 3, [("sqlglot/dialects/dialect.py",
    "    SUPPORTS_USER_DEFINED_TYPES = True",
    "    SUPPORTS_USER_DEFINED_TYPES = False")])
spec("dialect_base_tablesample_pct", 3, [("sqlglot/dialects/dialect.py",
    "    TABLESAMPLE_SIZE_IS_PERCENT = False",
    "    TABLESAMPLE_SIZE_IS_PERCENT = True")])

# ===== category 4: multi-file patches =====
spec("multi_mysql_and_generator", 4, [
    ("sqlglot/dialects/mysql.py", "    SAFE_DIVISION = True", "    SAFE_DIVISION = False"),
    ("sqlglot/generator.py", "    WRAP_DERIVED_VALUES = True", "    WRAP_DERIVED_VALUES = False"),
])
spec("multi_optimizer_and_simplify", 4, [
    ("sqlglot/optimizer/optimizer.py", "    canonicalize,\n    simplify,\n)", "    canonicalize,\n)"),
    ("sqlglot/optimizer/simplify.py",
     "    return isinstance(expression, exp.Literal) and expression.to_py() == 0",
     "    return isinstance(expression, exp.Literal) and expression.to_py() == 2"),
])

# ===== category 5: helper funcs / constants =====
spec("helper_seq_get_off", 5, [("sqlglot/helper.py",
    "def seq_get(seq: Sequence[T], index: int) -> T | None:",
    "def seq_get(seq: Sequence[T], index: int) -> T | None:  # SYNTH\n    index = index + 1  # off-by-one bug")])

# ===== category 6: copy / eq / hash =====
spec("expr_copy_noop", 6, [(C,
    "    def copy(self: E) -> E:\n        return deepcopy(self)",
    "    def copy(self: E) -> E:\n        return self  # SYNTH bug: shallow")])

# ===== category 8: pure refactors (must NOT change behavior) =====
# rename a local; add a redundant intermediate. Behavior identical -> truth should be empty.
spec("refactor_always_false", 8, [("sqlglot/optimizer/simplify.py",
    "    return is_false(expression) or is_null(expression) or is_zero(expression)",
    "    result = is_false(expression) or is_null(expression) or is_zero(expression)\n    return result")],
    behavior="noop")
spec("refactor_seq_get", 8, [("sqlglot/helper.py",
    "def seq_get(seq: Sequence[T], index: int) -> T | None:",
    "def seq_get(seq: Sequence[T], index: int) -> T | None:  # pure comment, no behavior change")],
    behavior="noop")

def build():
    out_dir = sys.argv[1]
    os.makedirs(out_dir, exist_ok=True)
    manifest = []
    for s in SPECS:
        parts, ok = [], True
        for rel, find, repl in s["edits"]:
            text = read(rel)
            n = text.count(find)
            if n != 1:
                print(f"SKIP {s['name']}: find appears {n}x in {rel}", file=sys.stderr)
                ok = False
                break
            parts.append(make_diff(rel, text, text.replace(find, repl, 1)))
        if not ok:
            continue
        with open(os.path.join(out_dir, s["name"] + ".patch"), "w") as f:
            f.write("".join(parts))
        manifest.append({"name": s["name"], "cat": s["cat"], "behavior": s["behavior"]})
    with open(os.path.join(out_dir, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"wrote {len(manifest)}/{len(SPECS)} patches to {out_dir}")

if __name__ == "__main__":
    build()
