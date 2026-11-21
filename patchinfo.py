"""Parse unified diffs and classify what each file change touches."""
import ast
import re

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class FilePatch:
    def __init__(self, old, new):
        self.old = old  # path relative to repo, or None if created
        self.new = new
        self.hunks = []  # (old_start, [(tag, text)])
        self.binary = False

    @property
    def path(self):
        return self.old or self.new


def _strip(p):
    if p == "/dev/null":
        return None
    p = p.split("\t")[0]
    if p.startswith("a/") or p.startswith("b/"):
        p = p[2:]
    return p


def parse(text):
    files = []
    cur = None
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("diff --git "):
            cur = None
            i += 1
            continue
        if ln.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            cur = FilePatch(_strip(ln[4:]), _strip(lines[i + 1][4:]))
            files.append(cur)
            i += 2
            continue
        if ln.startswith("Binary files"):
            m = re.match(r"Binary files (\S+) and (\S+) differ", ln)
            fp = FilePatch(_strip(m.group(1)) if m else None, _strip(m.group(2)) if m else None)
            fp.binary = True
            files.append(fp)
            i += 1
            continue
        m = HUNK.match(ln)
        if m and cur is not None:
            old_start = int(m.group(1))
            old_len = int(m.group(2)) if m.group(2) is not None else 1
            new_len = int(m.group(4)) if m.group(4) is not None else 1
            body = []
            i += 1
            o = n = 0
            while i < len(lines) and (o < old_len or n < new_len):
                b = lines[i]
                if b.startswith("\\"):
                    i += 1
                    continue
                tag = b[:1] if b else " "
                if tag == " ":
                    o += 1; n += 1
                elif tag == "-":
                    o += 1
                elif tag == "+":
                    n += 1
                else:
                    break
                body.append((tag, b[1:]))
                i += 1
            # swallow "\ No newline" markers
            while i < len(lines) and lines[i].startswith("\\"):
                i += 1
            cur.hunks.append((old_start, body))
            continue
        i += 1
    return files


def apply(old_text, fp):
    """Apply a FilePatch to old_text. Returns (new_text, removed_old_lines, insert_points).

    insert_points are old line numbers k meaning "inserted between old line k-1 and k"."""
    old = old_text.split("\n")
    out = []
    pos = 0  # index into old (0-based)
    removed = set()
    inserts = set()
    for old_start, body in fp.hunks:
        start = old_start - 1 if old_start > 0 else 0
        out.extend(old[pos:start])
        pos = start
        for tag, t in body:
            if tag == " ":
                out.append(old[pos]); pos += 1
            elif tag == "-":
                removed.add(pos + 1); pos += 1
            else:
                inserts.add(pos + 1)
                out.append(t)
    out.extend(old[pos:])
    return "\n".join(out), removed, inserts


# ---------- AST analysis ----------

FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _dump(node):
    return ast.dump(node, annotate_fields=False, include_attributes=False)


class _Blank(ast.NodeTransformer):
    """Blank out function/lambda bodies (keep signature, decorators, defaults)."""

    def visit_FunctionDef(self, node):
        node.decorator_list = [self.visit(d) for d in node.decorator_list]
        node.args = self.visit(node.args)
        node.body = [ast.Pass()]
        node.returns = None
        return node

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node):
        node.args = self.visit(node.args)
        node.body = ast.Constant(None)
        return node


class _P(ast.AST):
    _fields = ("x",)


def _clone(n):
    """Structural copy of an AST (what copy.deepcopy gives, without its bookkeeping)."""
    if isinstance(n, ast.AST):
        c = n.__class__.__new__(n.__class__)
        d = c.__dict__
        for k, v in n.__dict__.items():
            d[k] = _clone(v) if isinstance(v, (ast.AST, list)) else v
        return c
    if isinstance(n, list):
        return [_clone(x) if isinstance(x, (ast.AST, list)) else x for x in n]
    return n


def _functions(tree):
    """Return {key: node} for all function/lambda nodes, key = (qualname, ordinal)."""
    res = {}
    counts = {}

    def walk(node, qual):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                q = f"{qual}.{child.name}" if qual else child.name
                k = counts.get(q, 0); counts[q] = k + 1
                res[(q, k)] = child
                # decorators/defaults belong to the outer scope
                for d in child.decorator_list:
                    walk_expr(d, qual)
                walk_expr(child.args, qual)
                walk(child, q + ".<locals>")
            elif isinstance(child, ast.Lambda):
                q = f"{qual}.<lambda>" if qual else "<lambda>"
                k = counts.get(q, 0); counts[q] = k + 1
                res[(q, k)] = child
                walk_expr(child.args, qual)
                walk(child.body if False else child, q + ".<locals>")
            elif isinstance(child, ast.ClassDef):
                q = f"{qual}.{child.name}" if qual else child.name
                for d in child.decorator_list:
                    walk_expr(d, qual)
                for b in child.bases + [kw.value for kw in child.keywords]:
                    walk_expr(b, qual)
                for stmt in child.body:
                    walk_stmt(stmt, q)
            else:
                walk(child, qual)

    def walk_expr(node, qual):
        if node is not None:
            walk_wrap(node, qual)

    def walk_stmt(node, qual):
        walk_wrap(node, qual)

    def walk_wrap(node, qual):
        # treat node itself as a child of a dummy parent
        p = _P(); p.x = node
        walk(p, qual)

    walk(tree, "")
    return res


def _own_body_dump(fn):
    """Dump of a function's body with nested function bodies blanked."""
    import copy
    f = _clone(fn)
    if isinstance(f, ast.Lambda):
        body = [f.body]
    else:
        body = f.body
    b = _Blank()
    out = []
    for s in body:
        out.append(_dump(b.visit(s)))
    return "|".join(out)


def _first_line(fn):
    decs = getattr(fn, "decorator_list", None) or []
    return min([fn.lineno] + [d.lineno for d in decs])


def classify_py(old_text, new_text):
    """Classify a python change.

    Returns dict with:
      same: bool (AST identical => only comments/whitespace)
      error: bool (new source does not parse)
      structural: bool (something outside function bodies changed)
      changed_funcs: list of (qualname, ordinal, old_firstlineno or None)
    """
    try:
        old_t = ast.parse(old_text)
    except SyntaxError:
        return {"same": False, "error": True, "structural": True, "changed_funcs": []}
    try:
        new_t = ast.parse(new_text)
    except SyntaxError:
        return {"same": False, "error": True, "structural": True, "changed_funcs": []}
    if _dump(old_t) == _dump(new_t):
        return {"same": True, "error": False, "structural": False, "changed_funcs": []}
    of = _functions(old_t)
    nf = _functions(new_t)
    import copy
    ob = _dump(_Blank().visit(_clone(old_t)))
    nb = _dump(_Blank().visit(_clone(new_t)))
    structural = ob != nb or set(of) != set(nf)
    names, attrs = set(), None
    if structural:
        names, attrs = _changed_names(old_t, new_t)
        scopes = sorted(LAST_SCOPES)
    else:
        scopes = []
    changed = []
    for k in of:
        if k in nf and _own_body_dump(of[k]) != _own_body_dump(nf[k]):
            changed.append((k[0], k[1], _first_line(of[k])))
    return {"same": False, "error": False, "structural": structural, "changed_funcs": changed, "names": sorted(names), "attrs": sorted(attrs) if attrs else None, "scopes": scopes}


LAST_SCOPES = set()


def _scopes(tree):
    """{scope qualname: [blanked stmt dumps with names]} for module and class bodies."""
    import copy
    out = {}
    def visit(body, q):
        items = []
        for st in body:
            nm = set()
            if isinstance(st, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                tg = st.targets if isinstance(st, ast.Assign) else [st.target]
                for t in tg:
                    for n in ast.walk(t):
                        if isinstance(n, ast.Name): nm.add(n.id)
                        elif isinstance(n, ast.Attribute): nm.add(n.attr)
            elif isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                nm.add(st.name)
            elif isinstance(st, (ast.Import, ast.ImportFrom)):
                for a in st.names: nm.add((a.asname or a.name).split(".")[0])
            is_assign = isinstance(st, (ast.Assign, ast.AnnAssign, ast.AugAssign))
            if isinstance(st, ast.ClassDef):
                visit(st.body, f"{q}.{st.name}" if q else st.name)
                head = copy.copy(st); head.body = []
                items.append((_dump(_Blank().visit(_clone(head))), nm, False))
            else:
                items.append((_dump(_Blank().visit(_clone(st))), nm, is_assign))
        out[q] = items
    visit(tree.body, "")
    return out


def _changed_names(old_t, new_t):
    import difflib
    so, sn = _scopes(old_t), _scopes(new_t)
    names = set()
    attrs = set()
    only_assign = True
    global LAST_SCOPES
    LAST_SCOPES = set()
    for q in set(so) | set(sn):
        a, b = so.get(q, []), sn.get(q, [])
        if [x[0] for x in a] == [x[0] for x in b]:
            continue
        LAST_SCOPES.add(q)
        if q:
            names.add(q.split(".")[-1])
        sm = difflib.SequenceMatcher(a=[x[0] for x in a], b=[x[0] for x in b], autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                continue
            # a replaced assignment must keep its targets (same statement count and names)
            if tag != "replace" or (i2 - i1) != (j2 - j1):
                only_assign = False
            for x, y in zip(a[i1:i2], b[j1:j2]):
                if not (x[2] and y[2] and x[1] == y[1]):
                    only_assign = False
            for x in a[i1:i2] + b[j1:j2]:
                names |= x[1]
                if x[2]:
                    attrs |= x[1]
    return names, attrs if only_assign and attrs else None


def stmt_spans(old_text, lines):
    """Map old line numbers to the line span of the innermost statement containing them
    (header only for compound statements). Returns set of line numbers."""
    try:
        tree = ast.parse(old_text)
    except SyntaxError:
        return set(lines)
    spans = []
    for node in ast.walk(tree):
        if isinstance(node, ast.stmt):
            start = node.lineno
            end = node.end_lineno or start
            body = getattr(node, "body", None)
            if isinstance(body, list) and body and isinstance(body[0], ast.AST):
                end = min(end, body[0].lineno - 1) if body[0].lineno > start else start
            spans.append((start, end, node.end_lineno or start))
    out = set()
    for ln in lines:
        best = None
        for s, e, full_end in spans:
            if s <= ln <= full_end:
                if best is None or (s >= best[0] and full_end <= best[2]):
                    best = (s, e, full_end)
        if best is None:
            out.add(ln)
        else:
            s, e, _ = best
            if s <= ln <= e:
                out.update(range(s, e + 1))
            else:
                out.add(ln)
    return out


class _NormEq(ast.NodeTransformer):
    """Rewrite `x is C` -> `x == C` (and `is not` -> `!=`) when C is None or an
    enum-like constant (Name.UPPER). For those, identity and equality agree."""

    def __init__(self):
        self.needs_eval = set()

    def _safe(self, node):
        if isinstance(node, ast.Constant) and node.value is None:
            return True
        if isinstance(node, ast.Attribute) and node.attr.isupper() and len(node.attr) > 1:
            n = node
            while isinstance(n, ast.Attribute):
                n = n.value
            if isinstance(n, ast.Name) and n.id not in ("self", "cls"):
                self.needs_eval.add(ast.unparse(node))
                return True
        return False

    def visit_Compare(self, node):
        self.generic_visit(node)
        ops = []
        operands = [node.left] + node.comparators
        for i, op in enumerate(node.ops):
            a, b = operands[i], operands[i + 1]
            if isinstance(op, (ast.Is, ast.IsNot, ast.Eq, ast.NotEq)) and (self._safe(a) or self._safe(b)):
                op = ast.Eq() if isinstance(op, (ast.Is, ast.Eq)) else ast.NotEq()
            ops.append(op)
        node.ops = ops
        return node


def equivalent_py(old_text, new_text):
    """True if the change is only `is`<->`==` against None / enum constants."""
    try:
        na, nb = _NormEq(), _NormEq()
        a = na.visit(ast.parse(old_text))
        b = nb.visit(ast.parse(new_text))
    except SyntaxError:
        return False
    if _dump(a) != _dump(b):
        return False
    return (na.needs_eval | nb.needs_eval) or True


# ---------------- fast classification (unit based) ----------------

def _units(tree):
    """Flatten module/class bodies into units: (scope_qualname, node, start, end).
    ClassDef headers are units too (body excluded)."""
    out = []

    def visit(body, q):
        for st in body:
            if isinstance(st, ast.ClassDef):
                decs = [d.lineno for d in st.decorator_list]
                start = min([st.lineno] + decs)
                hdr_end = st.body[0].lineno - 1 if st.body else st.end_lineno
                out.append((q, ("classhead", st), start, max(start, hdr_end)))
                visit(st.body, f"{q}.{st.name}" if q else st.name)
            else:
                decs = [d.lineno for d in getattr(st, "decorator_list", [])]
                out.append((q, ("stmt", st), min([st.lineno] + decs), st.end_lineno or st.lineno))
    visit(tree.body, "")
    return out


def _unit_dump(u, blank=False):
    kind, node = u
    import copy
    if kind == "classhead":
        h = copy.copy(node)
        h.body = []
        h.decorator_list = list(node.decorator_list)
        return _dump(h)
    if blank:
        return _dump(_Blank().visit(_clone(node)))
    return _dump(node)


def _assign_targets(node):
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        tg = node.targets if isinstance(node, ast.Assign) else [node.target]
        nm = set()
        for t in tg:
            for n in ast.walk(t):
                if isinstance(n, ast.Name):
                    nm.add(n.id)
                elif isinstance(n, ast.Attribute):
                    nm.add(n.attr)
        return nm
    return None


def _no_defaults_dump(fn):
    """Dump of a def with its body blanked and argument default values removed."""
    import copy
    f = _clone(fn)
    f.args.defaults = []
    f.args.kw_defaults = []
    return _dump(_Blank().visit(f))


def _doc_stripped(unit):
    """Dump of a unit (kind, node) with every module/class/function docstring removed."""
    import copy
    node = unit[1]
    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
        return (unit[0], "DOC")  # a bare string statement: a class/module docstring or a no-op
    node = _clone(node)
    for n in ast.walk(node):
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            b = n.body
            if b and isinstance(b[0], ast.Expr) and isinstance(b[0].value, ast.Constant) and isinstance(b[0].value.value, str):
                n.body = b[1:]
    return (unit[0], _dump(node))


def classify_fast(old_text, new_text, removed, inserts):
    """Same contract as classify_py, plus 'old_tree'. Falls back to classify_py when the
    change does not line up unit-for-unit."""
    try:
        old_t = ast.parse(old_text)
    except SyntaxError:
        return {"same": False, "error": True, "structural": True, "changed_funcs": [], "names": [], "attrs": None, "scopes": []}
    try:
        new_t = ast.parse(new_text)
    except SyntaxError:
        return {"same": False, "error": True, "structural": True, "changed_funcs": [], "names": [], "attrs": None, "scopes": []}
    ou, nu = _units(old_t), _units(new_t)
    if len(ou) != len(nu) or any(a[0] != b[0] for a, b in zip(ou, nu)):
        r = classify_py(old_text, new_text)
        r["old_tree"] = old_t
        return r
    lines = set(removed) | set(inserts) | {p - 1 for p in inserts}
    touched = [i for i, u in enumerate(ou) if any(u[2] <= ln <= u[3] for ln in lines)]
    diff = [i for i in touched if _unit_dump(ou[i][1]) != _unit_dump(nu[i][1])]
    # safety: anything outside touched units must be identical (it is, unless the hunks lie)
    res = {"same": not diff, "error": False, "structural": False, "changed_funcs": [], "names": [],
           "attrs": None, "scopes": [], "old_tree": old_t, "units": (ou, nu, diff)}
    if not diff:
        return res
    if all(_doc_stripped(ou[i][1]) == _doc_stripped(nu[i][1]) for i in diff):
        # only docstrings changed: no collected test reads __doc__ (doctests are not collected)
        res["same"] = True
        res["docstring"] = True
        return res
    struct = [i for i in diff if _unit_dump(ou[i][1], True) != _unit_dump(nu[i][1], True)]
    of, nf = _functions(old_t), _functions(new_t)
    if set(of) != set(nf):
        struct = struct or diff
    if struct:
        # every structural difference is in default values of a def's arguments (bodies may differ
        # too): only calls of those functions can see it
        dflt = []
        for i in struct:
            a, b = ou[i][1][1], nu[i][1][1]
            if (ou[i][1][0] == "stmt" and isinstance(a, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and type(a) is type(b) and _no_defaults_dump(a) == _no_defaults_dump(b)):
                dflt.append((f"{ou[i][0]}.{a.name}" if ou[i][0] else a.name, _first_line(a)))
            else:
                dflt = None
                break
        if dflt:
            res["default_funcs"] = dflt
        names, attrs, scopes = set(), set(), set()
        only_assign = True
        for i in struct:
            q = ou[i][0]
            scopes.add(q)
            if q:
                names.add(q.split(".")[-1])
            a, b = ou[i][1][1], nu[i][1][1]
            ta, tb = _assign_targets(a), _assign_targets(b)
            if ta is None or tb is None or ta != tb or ou[i][1][0] != "stmt":
                only_assign = False
                for node in (a, b):
                    if hasattr(node, "name"):
                        names.add(node.name)
                    t = _assign_targets(node)
                    if t:
                        names |= t
                    if isinstance(node, (ast.Import, ast.ImportFrom)):
                        for al in node.names:
                            names.add((al.asname or al.name).split(".")[0])
            else:
                attrs |= ta
                names |= ta
        res.update(structural=True, names=sorted(names), attrs=sorted(attrs) if (only_assign and attrs) else None,
                   scopes=sorted(scopes))
        return res
    # function-body only: compare own bodies of functions inside the differing units
    spans = [(ou[i][2], ou[i][3]) for i in diff]
    changed = []
    for k, fn in of.items():
        fl = _first_line(fn)
        if not any(s <= fl and (fn.end_lineno or fl) <= e for s, e in spans):
            continue
        if _own_body_dump(fn) != _own_body_dump(nf[k]):
            changed.append((k[0], k[1], fl))
    res["changed_funcs"] = changed
    return res


def equivalent_units(info):
    """`is`<->`==` against None/ENUM only, judged on the differing units."""
    u = info.get("units")
    if not u:
        return False
    ou, nu, diff = u
    import copy
    need = set()
    for i in diff:
        na, nb = _NormEq(), _NormEq()
        a = na.visit(_clone(ou[i][1][1]))
        b = nb.visit(_clone(nu[i][1][1]))
        if _dump(a) != _dump(b):
            return False
        need |= na.needs_eval | nb.needs_eval
    return need or True


def _stmts_near(tree, lo, hi):
    """Statements whose full span overlaps [lo, hi]; skips subtrees outside the range."""
    stack = [tree]
    while stack:
        node = stack.pop()
        for child in ast.iter_child_nodes(node):
            s = getattr(child, "lineno", None)
            if s is not None:
                e = getattr(child, "end_lineno", None) or s
                if e < lo or s > hi:
                    continue
            if isinstance(child, ast.stmt):
                yield child
            stack.append(child)


def stmt_spans_tree(tree, lines):
    spans = []
    if not lines:
        return set()
    for node in _stmts_near(tree, min(lines), max(lines)):
        if True:
            start = node.lineno
            body = getattr(node, "body", None)
            full_end = node.end_lineno or start
            end = full_end
            if isinstance(body, list) and body and isinstance(body[0], ast.AST):
                end = body[0].lineno - 1 if body[0].lineno > start else start
            spans.append((start, end, full_end))
    out = set()
    for ln in lines:
        best = None
        for s, e, fe in spans:
            if s <= ln <= fe and (best is None or (s >= best[0] and fe <= best[2])):
                best = (s, e, fe)
        if best is None:
            out.add(ln)
        else:
            s, e, _ = best
            out.update(range(s, e + 1)) if s <= ln <= e else out.add(ln)
    return out


def _code_lines(code):
    """Line numbers that own at least one bytecode instruction (any nested code object)."""
    out = set()
    todo = [code]
    while todo:
        co = todo.pop()
        for _, _, ln in co.co_lines():
            if ln is not None:
                out.add(ln)
        todo.extend(c for c in co.co_consts if hasattr(c, "co_lines"))
    return out


def _segment(text, s, e, col):
    """Parse lines s..e (1-based) of text, dedented by col, keeping their line numbers."""
    lines = text.split("\n")[s - 1:e]
    if any(ln.strip() and len(ln) - len(ln.lstrip()) < col for ln in lines):
        raise ValueError("segment indentation")
    src = "\n".join(ln[col:] for ln in lines)
    tree = ast.parse(src)
    ast.increment_lineno(tree, s - 1)
    return tree


def _enclosing_def(tree, d):
    lo, hi = min(d), max(d)
    best = None
    todo = [tree]
    while todo:
        n = todo.pop()
        for c in ast.iter_child_nodes(n):
            if not isinstance(c, ast.stmt):
                continue
            end = c.end_lineno or c.lineno
            if c.lineno <= lo and hi <= end:
                if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef)) and not c.decorator_list:
                    best = c
                todo.append(c)
    return best


def exact_lines(old_text, new_text, old_tree=None):
    """Changed lines if the edit replaces whole lines in place (same line count) and every
    changed line holds bytecode both before and after: then the new code on such a line runs
    exactly when the old code on it ran (all code before it is unchanged), so per-line
    coverage of the old line is the reach. None if that does not hold."""
    a = old_text.split("\n")
    b = new_text.split("\n")
    if len(a) != len(b):
        return None
    d = {i + 1 for i, (x, y) in enumerate(zip(a, b)) if x != y}
    if not d or len(d) > 3:
        return None
    try:
        # only the innermost function holding the edit needs compiling and comparing
        fn = _enclosing_def(old_tree, d) if old_tree is not None else None
        if fn is not None:
            s, e, col = fn.lineno, fn.end_lineno, fn.col_offset
            ot, nt = _segment(old_text, s, e, col), _segment(new_text, s, e, col)
        else:
            ot, nt = ast.parse(old_text), ast.parse(new_text)
        ol = _code_lines(compile(ot, "<x>", "exec", dont_inherit=True))
        nl = _code_lines(compile(nt, "<x>", "exec", dont_inherit=True))
    except Exception:
        return None
    if not d <= ol or not d <= nl:
        return None
    # every AST difference must sit in a node that lies wholly on one changed line (so code
    # on other lines, e.g. short-circuit jumps of a reshaped and/or chain, is unchanged)
    try:
        diffs = []
        _ast_diff(ot, nt, [], diffs)
    except Exception:
        return None
    if not diffs:
        return None
    for na, nb in diffs:
        for n in (na, nb):
            ln = getattr(n, "lineno", None)
            if ln is None or ln != getattr(n, "end_lineno", None) or ln not in d:
                return None
    return d


def _located(chain):
    """Nearest ancestor with a location that is not a bare constant (constants can be folded
    into the enclosing operation, whose code may sit on another line)."""
    for n in reversed(chain):
        if hasattr(n, "lineno") and not isinstance(n, ast.Constant):
            return n
    return None


def _ast_diff(a, b, chain, out):
    if type(a) is not type(b):
        if isinstance(a, ast.AST) or isinstance(b, ast.AST):
            pa = _located(chain + ([a] if isinstance(a, ast.AST) else []))
            pb = _located(chain + ([b] if isinstance(b, ast.AST) else []))
            if isinstance(a, ast.AST) and isinstance(b, ast.AST) and hasattr(a, "lineno") and hasattr(b, "lineno") \
                    and not isinstance(a, ast.Constant) and not isinstance(b, ast.Constant):
                out.append((a, b))
            else:
                out.append((pa, pb))
        else:
            out.append((_located(chain), _located(chain)))
        return
    if isinstance(a, ast.AST):
        for f in a._fields:
            _ast_diff(getattr(a, f, None), getattr(b, f, None), chain + [a], out)
    elif isinstance(a, list):
        if len(a) != len(b):
            out.append((_located(chain), _located(chain)))
            return
        for x, y in zip(a, b):
            _ast_diff(x, y, chain, out)
    elif a != b or type(a) is not type(b):
        out.append((_located(chain), _located(chain)))
