#!/usr/bin/env python3
"""What does a Python function call actually cost?

Measures the marginal cost of every common way to invoke code in CPython, from
a bare local call up through descriptors, decorators and caches, so the price
of each abstraction is visible in a single table.

Every statement is inlined into the timing loop, so a reported figure is the
cost of that construct and not the cost of the harness calling it.

Run: python3 experiments/python-call-overhead/run.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
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


def main():
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
