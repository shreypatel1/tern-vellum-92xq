"""Canonical fingerprints of module-level state (globals and class attributes) after import.
Used to tell whether a change to code that runs at import time changes anything the tests see."""
import enum
import functools
import logging
import hashlib
import re
import sys
import types

_FUNCS = (types.FunctionType, types.BuiltinFunctionType, types.MethodType, types.BuiltinMethodType,
          types.WrapperDescriptorType, types.MethodDescriptorType, types.MethodWrapperType)


LAMBDA_CODE = False  # True: lambdas written in assignments count as state (their code)


def _code(co):
    """What a code object does (bytecode, names, constants), not where it sits in the file."""
    parts = [co.co_code.hex(), ",".join(co.co_names), ",".join(co.co_varnames)]
    for c in co.co_consts:
        if isinstance(c, types.CodeType):
            parts.append(_code(c))
        elif isinstance(c, frozenset):
            parts.append("fs{" + ",".join(sorted(repr(x) for x in c)) + "}")
        else:
            parts.append(repr(c))
    return hashlib.sha1("|".join(parts).encode("utf-8", "surrogatepass")).hexdigest()[:10]


def _canon(v, depth, seen):
    if v is None or isinstance(v, (bool, int, float, complex)):
        return repr(v)
    if isinstance(v, (str, bytes)):
        return repr(v)
    if isinstance(v, enum.Enum):
        return f"E:{type(v).__qualname__}.{v.name}"
    if isinstance(v, type):
        return f"T:{v.__module__}.{v.__qualname__}"
    if isinstance(v, types.FunctionType):
        # closures made at import time by factories carry their state in cells and defaults
        s = f"F:{v.__module__}.{v.__qualname__}"
        if LAMBDA_CODE and "<lambda>" in v.__qualname__ and "<locals>" not in v.__qualname__:
            # code written inside an assignment (lambdas) or made by a factory: part of the state.
            # Plain functions are code, which per-test coverage already follows.
            s += ":" + _code(v.__code__)
        if depth > 0 and id(v) not in seen:
            seen.add(id(v))
            try:
                if v.__closure__:
                    cells = []
                    for c in v.__closure__:
                        try:
                            cells.append(_canon(c.cell_contents, depth - 1, seen))
                        except ValueError:
                            cells.append("<empty>")
                    s += "|c(" + ",".join(cells) + ")"
                if v.__defaults__:
                    s += "|d" + _canon(v.__defaults__, depth - 1, seen)
                if v.__kwdefaults__:
                    s += "|k" + _canon(v.__kwdefaults__, depth - 1, seen)
            finally:
                seen.discard(id(v))
        return s
    if isinstance(v, functools.partial):
        return "FP:" + _canon(v.func, depth, seen) + _canon(v.args, depth - 1, seen) + _canon(v.keywords, depth - 1, seen)
    if isinstance(v, _FUNCS):
        # a bound method/classmethod: __qualname__ is the underlying function's (e.g.
        # Func.from_arg_list, shared by every subclass via inheritance), so include __self__
        # so ConvertToCharset.from_arg_list and Anonymous.from_arg_list are distinguished.
        base = f"F:{getattr(v, '__module__', '')}.{getattr(v, '__qualname__', '')}"
        slf = getattr(v, "__self__", None)
        if slf is not None:
            if isinstance(slf, type):
                base += f"@{slf.__module__}.{slf.__qualname__}"
            elif isinstance(slf, types.ModuleType):
                base += f"@M:{slf.__name__}"
            else:
                base += f"@{type(slf).__module__}.{type(slf).__qualname__}"
        return base
    if isinstance(v, types.ModuleType):
        return f"M:{v.__name__}"
    if isinstance(v, logging.Logger):
        return f"L:{v.name}"  # its caches fill up at run time
    if isinstance(v, re.Pattern):
        return f"R:{v.pattern!r}:{v.flags}"
    if isinstance(v, (classmethod, staticmethod)):
        return "D:" + _canon(v.__func__, depth, seen)
    if isinstance(v, property):
        return "P:" + _canon(v.fget, depth, seen)
    if depth <= 0:
        return f"?:{type(v).__qualname__}"
    if id(v) in seen:
        return f"cycle:{type(v).__qualname__}"
    seen.add(id(v))
    try:
        d = depth - 1
        if isinstance(v, (list, tuple)):
            items = [_canon(x, d, seen) for x in v]
            if v and all(isinstance(x, type) for x in v):
                items.sort()  # isinstance() tuples: order carries no meaning (often built from sets)
            return type(v).__name__ + "[" + ",".join(items) + "]"
        if isinstance(v, (set, frozenset)):
            return type(v).__name__ + "{" + ",".join(sorted(_canon(x, d, seen) for x in v)) + "}"
        if isinstance(v, dict):
            return type(v).__name__ + "{" + ",".join(sorted(_canon(k, d, seen) + ":" + _canon(x, d, seen) for k, x in v.items())) + "}"
        dd = getattr(v, "__dict__", None)
        if isinstance(dd, dict):
            return f"O:{type(v).__module__}.{type(v).__qualname__}" + _canon(dd, d, seen)
        sl = getattr(type(v), "__slots__", None)
        if sl:
            return f"S:{type(v).__qualname__}(" + ",".join(_canon(getattr(v, s, None), d, seen) for s in sl) + ")"
        return f"?:{type(v).__qualname__}"
    finally:
        seen.discard(id(v))


def _h(s):
    return hashlib.sha1(s.encode("utf-8", "surrogatepass")).hexdigest()[:12]


def _named(c):
    """True if class c is reachable from its module by its qualified name."""
    o = sys.modules.get(c.__module__)
    for part in c.__qualname__.split("."):
        o = getattr(o, part, None) if o is not None else None
    return o is c


def _class(out, mn, name, v, depth):
    for a, av in list(vars(v).items()):
        if a in ("__dict__", "__weakref__", "__doc__", "__module__", "__qualname__"):
            continue
        if isinstance(av, _FUNCS) and not isinstance(av, types.FunctionType):
            continue
        if (isinstance(av, type) and av.__module__ == mn and depth < 3
                and av.__qualname__.startswith(v.__qualname__ + ".")):
            _class(out, mn, f"{name}.{a}", av, depth + 1)  # nested class (Dialect.Tokenizer ...)
            continue
        if isinstance(av, type) and depth < 3 and str(av.__module__).startswith("sqlglot") and not _named(av):
            # a class made at import time (type("Tokenizer", bases, {}) for a dialect without
            # its own): only reachable through this attribute, so its state is recorded here
            out[f"{mn}:{name}.{a}"] = _h(_canon(av, 40, set()))
            _class(out, mn, f"{name}.{a}", av, depth + 1)
            continue
        out[f"{mn}:{name}.{a}"] = _h(_canon(av, 40, set()))
    out[f"{mn}:{name}"] = _h("T:" + ",".join(b.__module__ + "." + b.__qualname__ for b in v.__mro__))


def snapshot(prefixes=("sqlglot", "tests"), mods=None):
    out = {}
    for mn, mod in sorted(sys.modules.items()):
        if mod is None or not any(mn == p or mn.startswith(p + ".") for p in prefixes):
            continue
        if mods is not None and mn not in mods:
            continue
        for name, v in list(vars(mod).items()):
            if name.startswith("__") and name.endswith("__"):
                continue
            if isinstance(v, type) and v.__module__ == mn:
                _class(out, mn, name, v, 0)
            elif isinstance(v, _FUNCS):
                out[f"{mn}:{name}"] = _h(_canon(v, 6, set()))
            else:
                out[f"{mn}:{name}"] = _h(_canon(v, 40, set()))
    return out


def import_everything(repo):
    import importlib
    import os
    import pkgutil
    import sqlglot
    bad = []
    for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        if m.name.endswith("__main__"):
            continue
        try:
            importlib.import_module(m.name)
        except Exception:
            bad.append(m.name)
    for root, _, files in os.walk(os.path.join(repo, "tests")):
        for f in sorted(files):
            if f.endswith(".py"):
                rel = os.path.relpath(os.path.join(root, f), repo)[:-3].replace(os.sep, ".")
                if rel.endswith("__init__"):
                    rel = rel[:-9]
                try:
                    importlib.import_module(rel)
                except BaseException:
                    bad.append(rel)
    return bad


if __name__ == "__main__":
    import json
    import os
    import time
    repo = sys.argv[1]
    sys.path.insert(0, repo)
    os.chdir(repo)
    t = time.process_time()
    bad = import_everything(repo)
    t1 = time.process_time()
    snap = snapshot()
    LAMBDA_CODE = True
    snapl = snapshot()
    t2 = time.process_time()
    json.dump({"bad": bad, "snap": snap, "snapl": snapl}, open(sys.argv[2], "w"))
    print("import", round(t1 - t, 2), "snap", round(t2 - t1, 2), "keys", len(snap), "bad", bad[:5], file=sys.stderr)
