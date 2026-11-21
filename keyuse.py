"""Entry-level reads of the constant dicts on sqlglot classes and modules (see tools/build_keyuse.py).

scan() finds every dict held by an UPPER_CASE name of a sqlglot module or of a class defined in
one, and names it by its aliases ("module:Qual.NAME"); the smallest alias is its tag. A dict
that anything else refers to (an instance, a closure, a tuple, a derived table) is left out:
reads through such a reference would not be seen.
"""
import gc
import sys
import types

REC = {}    # tag -> set of key reprs read ("*" = the whole dict, "#len" = its size)
TAGS = {}   # alias -> tag, for the dicts that are tracked


def _const(name):
    s = name.strip("_")
    return bool(s) and s.isupper() and not (name.startswith("__") and name.endswith("__"))


def krepr(k):
    import state
    return state._canon(k, 3, set())


def _holders():
    """(alias, holder namespace, name, value) for UPPER_CASE dict attributes."""
    out = []
    seen_cls = set()

    def walk_cls(mn, qual, c):
        if id(c) in seen_cls:
            return
        seen_cls.add(id(c))
        ns = c.__dict__
        for name, v in list(ns.items()):
            if type(v) is dict and _const(name):
                out.append((f"{mn}:{qual}.{name}", c, name, v))
            elif isinstance(v, type) and v.__module__ == mn and v.__qualname__ == f"{qual}.{name}":
                walk_cls(mn, f"{qual}.{name}", v)

    for mn, mod in sorted(sys.modules.items()):
        if mod is None or not (mn == "sqlglot" or mn.startswith("sqlglot.")):
            continue
        for name, v in list(vars(mod).items()):
            if type(v) is dict and _const(name):
                out.append((f"{mn}:{name}", mod, name, v))
            elif isinstance(v, type) and v.__module__ == mn and v.__qualname__ == name:
                walk_cls(mn, name, v)
    return out


def scan():
    """{id(dict): (tag, [aliases], dict)} for the dicts that only their holders refer to."""
    hs = _holders()
    by = {}
    for alias, holder, name, v in hs:
        e = by.setdefault(id(v), [v, [], set()])
        e[1].append(alias)
        e[2].add(id(holder))
    del hs, alias, holder, name, v
    out = {}
    gc.collect()
    for i, (v, aliases, holder_ids) in by.items():
        refs = [r for r in gc.get_referrers(v) if not isinstance(r, types.FrameType)]
        ok = True
        nh = 0
        for r in refs:
            if r is by or (type(r) is list and r and r[0] is v):
                continue
            if type(r) is dict and (r.get("__name__", "").startswith("sqlglot") if "__builtins__" in r
                                    else str(r.get("__module__", "")).startswith("sqlglot")):
                nh += 1
                continue
            ok = False
            break
        # a class namespace is reachable only through its mappingproxy, so count holders by name
        if ok and nh == len(holder_ids):
            out[i] = (min(aliases), sorted(aliases), v)
    return out


class _TD(dict):
    __slots__ = ("_tag",)

    def _r(self, k):
        s = REC.get(self._tag)
        if s is None:
            s = REC[self._tag] = set()
        try:
            s.add(krepr(k))
        except Exception:  # noqa
            s.add("*")

    def __getitem__(self, k):
        self._r(k); return dict.__getitem__(self, k)

    def get(self, k, d=None):
        self._r(k); return dict.get(self, k, d)

    def __contains__(self, k):
        self._r(k); return dict.__contains__(self, k)

    def __len__(self):
        self._r("#len"); return dict.__len__(self)

    def _all(self):
        REC.setdefault(self._tag, set()).add("*")


def _wrap_all(name):
    base = getattr(dict, name)

    def f(self, *a, **k):
        self._all()
        return base(self, *a, **k)
    f.__name__ = name
    return f


for _n in ("__iter__", "keys", "values", "items", "copy", "__eq__", "__ne__", "__repr__",
           "__reduce__", "__reduce_ex__", "__or__", "__ror__", "__ior__", "__reversed__",
           "__setitem__", "__delitem__", "setdefault", "pop", "popitem", "update", "clear",
           "__sizeof__", "__format__", "__str__"):
    setattr(_TD, _n, _wrap_all(_n))
_TD.__hash__ = None


def install():
    """Replace every trackable dict by a recording copy (in all its holders). Returns the count."""
    found = scan()
    holders = _holders()
    n = 0
    tds = {}
    for alias, holder, name, v in holders:
        e = found.get(id(v))
        if e is None:
            continue
        td = tds.get(id(v))
        if td is None:
            td = tds[id(v)] = _TD(v)
            td._tag = e[0]
            n += 1
            for a in e[1]:
                TAGS[a] = e[0]
        if isinstance(holder, types.ModuleType):
            setattr(holder, name, td)
        else:
            type.__setattr__(holder, name, td)
    return n


def entries(v):
    """{key repr: value hash} of a dict, None if a key has no stable repr."""
    import state
    out = {}
    lam = state.LAMBDA_CODE
    state.LAMBDA_CODE = True  # a lambda's code is part of the entry
    try:
        for k, x in dict.items(v):
            r = krepr(k)
            if "0x" in r or "object at" in r or r in out:
                return None
            out[r] = state._h(state._canon(x, 40, set()))
    except Exception:  # noqa
        return None
    finally:
        state.LAMBDA_CODE = lam
    return out


_ALIASES = {}


def aliases(v):
    """Aliases of the dict v among the current interpreter's UPPER_CASE holders."""
    if not _ALIASES:
        for alias, holder, name, x in _holders():
            _ALIASES.setdefault(id(x), []).append(alias)
        _ALIASES[None] = []
    return _ALIASES.get(id(v), [])


def resolve(key):
    """The object a state key ("module:Qual.NAME") names, in the current interpreter."""
    mn, rest = key.split(":", 1)
    obj = sys.modules.get(mn)
    if obj is None:
        return None
    for part in rest.split("."):
        try:
            obj = getattr(obj, part)
        except Exception:  # noqa
            return None
    return obj
