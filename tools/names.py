"""Map every name a function's code mentions -> list of (file, firstlineno). usage: names.py REPO OUT"""
import sys, os, json, importlib, pkgutil, types
REPO, OUT = sys.argv[1], sys.argv[2]
sys.path.insert(0, REPO)
import dis
res = {}
body_loads = {}
def walk(code, rel):
    is_func = bool(code.co_flags & 0x1)  # CO_OPTIMIZED: real function
    names = set()
    for ins in dis.get_instructions(code):
        if ins.opname.startswith("LOAD") or ins.opname in ("IMPORT_FROM", "IMPORT_NAME", "DELETE_ATTR", "STORE_ATTR"):
            if isinstance(ins.argval, str) and ins.argval.isidentifier():
                names.add(ins.argval)
    for c in code.co_consts:
        if isinstance(c, str) and c.isidentifier():
            names.add(c)
        elif isinstance(c, types.CodeType):
            walk(c, rel)
    if is_func:
        key = [rel, -code.co_firstlineno]
        for n in names:
            res.setdefault(n, []).append(key)
    else:
        for n in names:
            body_loads[n] = body_loads.get(n, 0) + 1
for root in ("sqlglot",):
    for dp, dn, fns in os.walk(os.path.join(REPO, root)):
        for fn in fns:
            if fn.endswith(".py"):
                full = os.path.join(dp, fn); rel = os.path.relpath(full, REPO)
                try:
                    code = compile(open(full, encoding="utf-8").read(), full, "exec")
                except SyntaxError:
                    continue
                walk(code, rel)
                # class bodies are code objects too: walk() covers methods inside them
json.dump({"funcs": res, "body_loads": body_loads}, open(OUT, "w"))
print(len(res))

# reverse import graph: module file -> files importing it directly
import ast
imp = {}
def modfile(mod):
    p = os.path.join(REPO, *mod.split("."))
    if os.path.isfile(p + ".py"): return os.path.relpath(p + ".py", REPO)
    if os.path.isfile(os.path.join(p, "__init__.py")): return os.path.relpath(os.path.join(p, "__init__.py"), REPO)
    return None
for dp, dn, fns in os.walk(os.path.join(REPO, "sqlglot")):
    for fn in fns:
        if not fn.endswith(".py"): continue
        full = os.path.join(dp, fn); rel = os.path.relpath(full, REPO)
        pkg = os.path.relpath(dp, REPO).replace(os.sep, ".")
        if fn != "__init__.py":
            pass
        try: tree = ast.parse(open(full, encoding="utf-8").read())
        except SyntaxError: continue
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    parts = pkg.split(".")
                    parts = parts[: len(parts) - node.level + 1]
                    base = ".".join(parts + ([node.module] if node.module else []))
                mods = [base] + [base + "." + a.name for a in node.names]
            for m in mods:
                f = modfile(m)
                if f and f != rel:
                    imp.setdefault(f, set()).add(rel)
json.dump({k: sorted(v) for k, v in imp.items()}, open(OUT.replace(".json", "_imports.json"), "w"))
