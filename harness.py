"""Run sqlglot unittest tests in-process, the way pytest decides pass/fail."""
import importlib, os, signal, sys, unittest

class _Timeout(BaseException):
    pass

def _alarm(signum, frame):
    raise _Timeout()

class Result(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.bad = False
    def addError(self, test, err):
        self.bad = True
        if err and err[0] is _Timeout:
            raise _Timeout()
    def addFailure(self, test, err): self.bad = True
    def addSubTest(self, test, subtest, err):
        if err is not None: self.bad = True
    def addUnexpectedSuccess(self, test): self.bad = True
    def addSkip(self, test, reason): pass
    def addExpectedFailure(self, test, err): pass

_classes = {}
_class_ready = {}

def get_class(test_id):
    path, cls, meth = test_id.split("::")
    mod = path[:-3].replace("/", ".")
    key = (mod, cls)
    if key not in _classes:
        m = importlib.import_module(mod)
        _classes[key] = getattr(m, cls)
    return _classes[key], meth

def import_all(test_ids):
    """Import every test module (collection). Returns list of import-error files."""
    bad = set()
    for tid in test_ids:
        f = tid.split("::")[0]
        if f in bad: continue
        try:
            get_class(tid)
        except Exception:
            bad.add(f)
    return bad

def run_tests(test_ids, timeout_for=lambda tid: 60, stop_on_fail=False):
    """Run tests grouped by class (setUpClass once per class). Returns set of failing ids."""
    failed = set()
    old = signal.signal(signal.SIGALRM, _alarm)
    by_class = {}
    for tid in test_ids:
        by_class.setdefault(tid.rsplit("::", 1)[0], []).append(tid)
    try:
        for ckey, tids in by_class.items():
            try:
                cls, _ = get_class(tids[0])
            except Exception:
                failed.update(tids); continue
            if getattr(cls, "__unittest_skip__", False):
                continue
            try:
                signal.setitimer(signal.ITIMER_REAL, 30)
                cls.setUpClass()
                signal.setitimer(signal.ITIMER_REAL, 0)
            except BaseException:
                signal.setitimer(signal.ITIMER_REAL, 0)
                failed.update(tids)
                try: cls.doClassCleanups()
                except BaseException: pass
                continue
            for tid in tids:
                meth = tid.rsplit("::", 1)[1]
                r = Result()
                try:
                    signal.setitimer(signal.ITIMER_REAL, max(0.5, float(timeout_for(tid))))
                    cls(meth).run(r)
                    signal.setitimer(signal.ITIMER_REAL, 0)
                except BaseException:
                    signal.setitimer(signal.ITIMER_REAL, 0)
                    r.bad = True
                if r.bad:
                    failed.add(tid)
                    if stop_on_fail: return failed
            try:
                signal.setitimer(signal.ITIMER_REAL, 30)
                cls.tearDownClass(); cls.doClassCleanups()
                signal.setitimer(signal.ITIMER_REAL, 0)
            except BaseException:
                signal.setitimer(signal.ITIMER_REAL, 0)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)
    return failed


class InlineExecutor:
    """Drop-in for ProcessPoolExecutor that runs work synchronously in this process."""
    PICKLE_INIT = True

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        if initializer:
            import pickle
            initializer(*(pickle.loads(pickle.dumps(initargs)) if self.PICKLE_INIT else initargs))
    def submit(self, fn, *args, **kwargs):
        import concurrent.futures as cf
        f = cf.Future()
        try:
            import pickle
            args, kwargs = pickle.loads(pickle.dumps((args, kwargs)))
            f.set_result(pickle.loads(pickle.dumps(fn(*args, **kwargs))))
        except BaseException as e:
            if isinstance(e, _Timeout):
                raise
            f.set_exception(e)
        return f
    def map(self, fn, *its, **kw):
        return map(fn, *its)
    def shutdown(self, *a, **k):
        pass
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def install_inline_pools():
    for name in ("tests.test_executor", "tests.test_optimizer"):
        m = sys.modules.get(name) or importlib.import_module(name)
        m.ProcessPoolExecutor = InlineExecutor
