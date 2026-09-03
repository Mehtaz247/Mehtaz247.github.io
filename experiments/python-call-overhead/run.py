#!/usr/bin/env python3
"""What does a Python function call actually cost?

Measures the marginal cost of every common way to invoke code in CPython, from
a bare local call up through descriptors, decorators and caches, so the price
of each abstraction is visible in a single table.

Every statement is inlined into the timing loop, so a reported figure is the
cost of that construct and not the cost of the harness calling it.

Run: python3 experiments/python-call-overhead/run.py
"""

import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "lib"))
from bench import Suite  # noqa: E402


SETUP = """
import functools

def plain():
    return 1

def with_args(a, b, c):
    return a

def with_defaults(a=1, b=2, c=3):
    return a

def with_kwonly(*, a, b, c):
    return a

def star_args(*args, **kwargs):
    return args

lam = lambda: 1
partial = functools.partial(with_args, 1, 2, 3)

class Obj:
    __slots__ = ("value",)
    def __init__(self):
        self.value = 1
    def method(self):
        return 1
    @staticmethod
    def static():
        return 1
    @classmethod
    def klass(cls):
        return 1
    @property
    def prop(self):
        return 1

class Slotless:
    def __init__(self):
        self.value = 1

class Dynamic:
    def __getattr__(self, name):
        return 1

class CallableObj:
    def __call__(self):
        return 1

obj = Obj()
slotless = Slotless()
dynamic = Dynamic()
callable_obj = CallableObj()
bound = obj.method

def null_decorator(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return fn(*args, **kwargs)
    return wrapper

@null_decorator
def decorated():
    return 1

@null_decorator
@null_decorator
@null_decorator
def decorated_x3():
    return 1

# Same decoration, but the wrapper declares the exact signature it forwards
# instead of repacking through *args/**kwargs.
def exact_decorator(fn):
    @functools.wraps(fn)
    def wrapper():
        return fn()
    return wrapper

@exact_decorator
def decorated_exact():
    return 1

# And the same again without functools.wraps, to price the metadata copying.
def bare_decorator(fn):
    def wrapper():
        return fn()
    return wrapper

@bare_decorator
def decorated_bare():
    return 1

@functools.lru_cache(maxsize=128)
def lru_cached(n):
    return n

@functools.cache
def simple_cached(n):
    return n

lru_cached(1)
simple_cached(1)

memo = {1: 1}
numbers = [0] * 100
scratch = []
err = ValueError("x")
"""


# The statement the cross-checks all measure. Kept deliberately trivial: the
# claim being checked is about the machine and the harness, not about Python.
CROSSCHECK_SETUP = "def plain():\n    return 1\n"
CROSSCHECK_STMT = "plain()"

TIMEIT_PROBE = r"""
import sys, timeit
setup = "def plain():\n    return 1\n"
REPEAT, NUMBER = 201, 20_000
base = min(timeit.repeat("pass", "", number=NUMBER, repeat=REPEAT)) / NUMBER * 1e9
call = min(timeit.repeat("plain()", setup, number=NUMBER, repeat=REPEAT)) / NUMBER * 1e9
print(sys.version.split()[0], round(base, 2), round(call - base, 2))
"""


def crosscheck():
    """Check this experiment's headline number against things outside the harness.

    Every figure on this blog comes from one timing harness on one laptop, which
    makes a systematic error in either invisible from the inside. On 2026-09-03
    that stopped being hypothetical: the suite came back ~2.8x slower than the
    published run with every measurement converging. These three checks are what
    established that the machine, and not the code, was responsible.

    1. stdlib `timeit`, on every CPython on this machine. An independent
       implementation of the same idea, and a second interpreter build, so
       neither our harness nor a point release can hide behind the other.
    2. Wall clock against `thread_time`, which counts only the time the thread
       was actually running on a CPU. If the two agree, the measurement is not
       losing time to preemption and the cycles themselves are slow.
    3. The machine speed reference, which says how much of this laptop's
       best-ever throughput the run is getting.

    Run: python3 experiments/python-call-overhead/run.py --crosscheck
    """
    from bench import calibrate

    out = {
        "note": ("Corroboration for 'plain function, no args' from outside the house "
                 "harness. See the post's 'What went wrong with these numbers' section."),
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "statement": CROSSCHECK_STMT,
    }

    # -- 1. stdlib timeit, on each interpreter present -----------------------
    interpreters = {}
    seen: set[str] = set()
    for exe in ("/usr/local/bin/python3", "/opt/homebrew/bin/python3", sys.executable):
        # Resolve first: these paths are symlinks into the same installs, and
        # keying the results by version string alone silently overwrites one
        # interpreter's number with another's.
        real = str(Path(exe).resolve()) if Path(exe).exists() else exe
        if not Path(exe).exists() or real in seen:
            continue
        seen.add(real)
        try:
            r = subprocess.run([exe, "-c", TIMEIT_PROBE], capture_output=True,
                               text=True, timeout=600)
            ver, base, call = r.stdout.split()
        except Exception as exc:                                  # pragma: no cover
            print(f"timeit: {exe} failed: {exc}")
            continue
        if ver.startswith("3.13"):
            interpreters[f"{ver} @ {exe}"] = {
                "version": ver, "exe": exe, "resolved": real,
                "empty_loop_ns": float(base), "plain_call_ns": float(call)}
            print(f"timeit  CPython {ver:8} {float(call):7.2f} ns  "
                  f"(empty loop {float(base):.2f} ns)  {exe}")
    out["timeit_by_interpreter"] = interpreters

    # -- 2. Wall clock against CPU time --------------------------------------
    import bench
    wall = bench.bench("wall", CROSSCHECK_STMT, setup=CROSSCHECK_SETUP)
    real_timer = time.perf_counter_ns
    try:
        bench.time.perf_counter_ns = time.thread_time_ns
        cpu = bench.bench("cpu", CROSSCHECK_STMT, setup=CROSSCHECK_SETUP)
    finally:
        bench.time.perf_counter_ns = real_timer
    out["wall_vs_cpu_time"] = {
        "wall_clock_ns": round(wall.ns_per_op, 2),
        "thread_cpu_time_ns": round(cpu.ns_per_op, 2),
        "difference_pct": round(abs(wall.ns_per_op - cpu.ns_per_op)
                                / max(wall.ns_per_op, 1e-9) * 100, 1),
        "means": ("If these agree, the timed windows are not losing time to "
                  "preemption, so a slow result means slow cycles rather than "
                  "an interrupted measurement."),
    }
    print(f"wall clock {wall.ns_per_op:7.2f} ns   thread CPU time "
          f"{cpu.ns_per_op:7.2f} ns   ({out['wall_vs_cpu_time']['difference_pct']}% apart)")

    # -- 3. How fast is this machine right now? ------------------------------
    out["machine_speed"] = calibrate(update=False)
    print(f"machine speed {out['machine_speed']['speed_ratio']:.2f}x of the "
          f"{out['machine_speed']['best_ns']:.2f} ns record")

    out["august_harness_note"] = (
        "The harness as it stood for the published run is recoverable with "
        "`git show 27abde4:experiments/lib/bench.py`. Run against this experiment on "
        "2026-09-03 it reported 48.9 ns for a plain call, against 36.2 ns from the "
        "current harness and 37.2 ns from timeit the same afternoon -- all three far "
        "from the 13.4 ns published on 2026-08-29."
    )

    path = HERE / "crosscheck.json"
    path.write_text(json.dumps(out, indent=2) + "\n")
    print(f"wrote {path}")


def main():
    if "--crosscheck" in sys.argv:
        crosscheck()
        return

    s = Suite(
        slug="python-call-overhead",
        question="What does each way of calling code in CPython actually cost?",
        setup=SETUP,
    )
    s.record("python_version", sys.version.split()[0])

    print("\n-- reference points --")
    s.run("integer addition", "y = 1 + 1")
    s.run("local variable read", "y = obj")
    s.run("global function lookup (no call)", "y = plain")

    print("\n-- calling a function --")
    s.run("plain function, no args", "plain()")
    s.run("lambda, no args", "lam()")
    s.run("3 positional args", "with_args(1, 2, 3)")
    s.run("3 defaults, none passed", "with_defaults()")
    s.run("3 keyword args", "with_kwonly(a=1, b=2, c=3)")
    s.run("*args / **kwargs", "star_args(1, 2, 3)")
    s.run("functools.partial", "partial()")

    print("\n-- methods and descriptors --")
    s.run("method call (obj.method())", "obj.method()")
    s.run("prebound method", "bound()")
    s.run("staticmethod", "Obj.static()")
    s.run("classmethod", "Obj.klass()")
    s.run("instance __call__", "callable_obj()")

    print("\n-- attribute access --")
    s.run("attribute, __slots__ class", "y = obj.value")
    s.run("attribute, __dict__ class", "y = slotless.value")
    s.run("@property", "y = obj.prop")
    s.run("__getattr__ fallback", "y = dynamic.missing")

    print("\n-- decorators --")
    s.run("one decorator (*args/**kwargs)", "decorated()")
    s.run("one decorator (exact signature)", "decorated_exact()")
    s.run("one decorator (no functools.wraps)", "decorated_bare()")
    s.run("three stacked decorators", "decorated_x3()")

    print("\n-- caches --")
    s.run("functools.lru_cache hit", "lru_cached(1)")
    s.run("functools.cache hit", "simple_cached(1)")
    s.run("plain dict lookup", "y = memo[1]")

    print("\n-- exceptions --")
    s.run("try/except, nothing raised", "try:\n    y = 1\nexcept ValueError:\n    y = 0")
    s.run("raise + catch, new exception", "try:\n    raise ValueError('x')\nexcept ValueError:\n    y = 0")
    s.run("raise + catch, preallocated", "try:\n    raise err\nexcept ValueError:\n    y = 0")

    print("\n-- builtins, for scale --")
    s.run("len() on a list", "y = len(numbers)")
    s.run("list.append", "scratch.append(1)")
    s.run("isinstance()", "y = isinstance(obj, Obj)")

    s.save()
    print("\n" + s.table(baseline="plain function, no args"))


if __name__ == "__main__":
    main()
