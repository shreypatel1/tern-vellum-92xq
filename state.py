"""Canonical fingerprints of module-level state (globals and class attributes) after import.
Used to tell whether a change to code that runs at import time changes anything the tests see."""
import enum
import hashlib
import re
import sys
import types

_FUNCS = (types.FunctionType, types.BuiltinFunctionType, types.MethodType, types.BuiltinMethodType,
          types.WrapperDescriptorType, types.MethodDescriptorType, types.MethodWrapperType)


def _canon(v, depth, seen):
    if v is None or isinstance(v, (bool, int, float, complex)):
        return repr(v)
    if isinstance(v, (str, bytes)):
        return repr(v)
    if isinstance(v, enum.Enum):
        return f"E:{type(v).__qualname__}.{v.name}"
    if isinstance(v, type):
        return f"T:{v.__module__}.{v.__qualname__}"
    if isinstance(v, _FUNCS):
        return f"F:{getattr(v, '__module__', '')}.{getattr(v, '__qualname__', '')}"
    if isinstance(v, types.ModuleType):
        return f"M:{v.__name__}"
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
            return type(v).__name__ + "[" + ",".join(_canon(x, d, seen) for x in v) + "]"
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


def snapshot(prefixes=("sqlglot", "tests")):
    out = {}
    for mn, mod in sorted(sys.modules.items()):
        if mod is None or not any(mn == p or mn.startswith(p + ".") for p in prefixes):
            continue
        for name, v in list(vars(mod).items()):
            if name.startswith("__") and name.endswith("__"):
                continue
            if isinstance(v, type) and v.__module__ == mn:
                for a, av in list(vars(v).items()):
                    if a in ("__dict__", "__weakref__", "__doc__", "__module__", "__qualname__"):
                        continue
                    if isinstance(av, _FUNCS):
                        continue
                    out[f"{mn}:{name}.{a}"] = _h(_canon(av, 8, set()))
                out[f"{mn}:{name}"] = _h("T:" + ",".join(b.__module__ + "." + b.__qualname__ for b in v.__mro__))
            elif isinstance(v, _FUNCS):
                out[f"{mn}:{name}"] = _h(_canon(v, 1, set()) + _canon(getattr(v, "__defaults__", None), 4, set()) + _canon(getattr(v, "__kwdefaults__", None), 4, set()))
            else:
                out[f"{mn}:{name}"] = _h(_canon(v, 8, set()))
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
    t2 = time.process_time()
    json.dump({"bad": bad, "snap": snap}, open(sys.argv[2], "w"))
    print("import", round(t1 - t, 2), "snap", round(t2 - t1, 2), "keys", len(snap), "bad", bad[:5], file=sys.stderr)
