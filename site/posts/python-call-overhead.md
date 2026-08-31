---
title: What a Python function call actually costs
description: Thirty-two ways to invoke code in CPython 3.13, measured on the same machine in the same run. The expensive part of a decorator is not the decorating.
verdict: A bare call is 13ns. A no-op decorator makes it 52ns — and almost all of that is argument repacking, not decoration. Fixing the wrapper signature gets it back to 28ns.
date: 2026-08-29
tags: [Python, performance, benchmarks]
experiment: python-call-overhead
hardware: Apple M1 MacBook Air (8 cores, 8 GB), macOS 26.6.2
software: CPython 3.13.0, arm64
method: Statements inlined into the timing loop, empty loop subtracted, minimum of 9 trials
---

Everyone knows Python function calls are slow. Almost nobody knows *how* slow,
or which parts of the call are responsible, and that gap is where a lot of bad
optimisation decisions live. People strip out helper functions that cost
nothing and keep decorators that cost four times as much as the code they wrap.

So here is the table. Every row was measured in the same process, on the same
machine, in the same run, with the statement inlined directly into the timing
loop so that nothing but the operation itself sits between the two clock reads.

## The numbers

| Operation | ns/op | vs. a plain call |
|---|--:|--:|
| integer addition | 2.29 | 0.17x |
| local variable read | 2.45 | 0.18x |
| global function lookup, no call | 2.34 | 0.17x |
| attribute on a `__dict__` class | 4.24 | 0.32x |
| attribute on a `__slots__` class | 4.34 | 0.32x |
| `list.append` | 8.00 | 0.60x |
| plain dict lookup | 8.29 | 0.62x |
| `len()` on a list | 9.04 | 0.68x |
| `isinstance()` | 9.57 | 0.72x |
| **plain function, no args** | **13.4** | **1.00x** |
| lambda, no args | 13.5 | 1.01x |
| prebound method | 15.2 | 1.14x |
| `@property` | 16.7 | 1.25x |
| method call, `obj.method()` | 16.6 | 1.24x |
| 3 defaults, none passed | 18.6 | 1.39x |
| 3 positional args | 20.5 | 1.53x |
| `functools.cache` hit | 21.5 | 1.61x |
| `functools.lru_cache` hit | 23.0 | 1.72x |
| `staticmethod` | 27.6 | 2.06x |
| decorator, exact signature | 27.9 | 2.09x |
| `functools.partial` | 29.9 | 2.24x |
| 3 keyword args | 32.8 | 2.46x |
| instance `__call__` | 33.4 | 2.50x |
| `*args` / `**kwargs` | 35.9 | 2.69x |
| `classmethod` | 44.0 | 3.29x |
| `__getattr__` fallback | 44.5 | 3.33x |
| **decorator, `*args`/`**kwargs`** | **51.9** | **3.88x** |
| three stacked decorators | 135.5 | 10.13x |

Two rows that are not in the "cost of calling" story but are worth having on the
same scale:

| Operation | ns/op | vs. a plain call |
|---|--:|--:|
| `try`/`except`, nothing raised | 2.31 | 0.17x |
| `raise` + catch, preallocated exception | 51.6 | 3.86x |
| `raise` + catch, fresh exception | 89.4 | 6.68x |

## What is actually going on

**A call costs about 13 nanoseconds, and that is the floor.** Not the arguments,
not the lookup — the act of pushing a frame and coming back. For scale, that is
roughly six integer additions or three attribute reads. If a function does any
real work at all, the call is already noise. If it does nothing but return a
value, the call *is* the work.

**Arguments are not free, and keyword arguments are much less free.** Three
positional arguments add 7ns over a bare call. The same three passed by keyword
add 19ns — nearly triple. Keyword arguments have to be matched against the
function's parameter names at call time, and that matching shows up here plainly.

**The interesting result is the decorator.** A no-op decorator — the kind every
codebase has a dozen of, for logging or timing or auth — turns a 13.4ns call into
a 51.9ns call. It nearly quadruples the cost of the function it wraps, while
doing nothing.

But the reason is not what most people assume. Here are three decorators that
are identical except for how the wrapper forwards its arguments:

| Wrapper | ns/op |
|---|--:|
| `def wrapper(*args, **kwargs): return fn(*args, **kwargs)` | 51.9 |
| `def wrapper(): return fn()` | 27.9 |
| same, without `functools.wraps` | 28.3 |

Declaring the exact signature nearly halves the cost. And `functools.wraps`,
which is widely suspected of being the expensive part, is free at call time — it
runs once at decoration time, copying `__name__` and friends, and never appears
in the hot path again.

The cost of a decorator is almost entirely **argument repacking**. The generic
wrapper collects the caller's arguments into a fresh tuple and a fresh dict,
then immediately unpacks them again for the inner call. Two allocations and two
teardowns, per call, to move arguments a distance of zero.

```python
# 51.9 ns per call: builds a tuple and a dict, then throws them away
def timed(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return fn(*args, **kwargs)
    return wrapper

# 27.9 ns per call: same behaviour, no repacking
def timed(fn):
    @functools.wraps(fn)
    def wrapper(request):
        return fn(request)
    return wrapper
```

This is only available when a decorator is applied to functions of a known
shape, which — for the internal, single-purpose decorators that make up most of
any codebase — is nearly always. A general-purpose decorator published in a
library does need `*args`/`**kwargs`, and pays for it.

**Stacking is multiplicative and it adds up fast.** Three no-op decorators cost
135.5ns, a bit more than three times one. A request handler wrapped in
authentication, logging, and rate limiting has spent 135ns before the first line
of its body runs. That is still only 0.1 microseconds — irrelevant for a handler
that will touch a database — but it is not irrelevant at 10,000 calls inside a
loop, which is exactly where these get applied without anyone noticing.

**`try`/`except` is genuinely free when nothing raises.** 2.31ns, which is inside
the noise of the `y = 1` statement it wraps. CPython 3.11 moved exception
handling to a zero-cost model: the handler is a side table consulted only when
an exception actually propagates, so entering a `try` block emits no instructions
at all. The old advice to hoist `try` out of hot loops is now obsolete.

Raising is a different story. A caught exception costs 89.4ns with a fresh
instance and 51.6ns with a preallocated one — the gap being the cost of
constructing the exception object and its traceback. Exceptions as control flow
in a hot loop are about seven plain calls each.

**`lru_cache` is fast, but a dict is faster.** A cache hit costs 21.5–23.0ns
against 8.29ns for a bare dict lookup. `lru_cache` is implemented in C and is a
good default, but it is still doing real work per hit: building a key from the
arguments, checking the bound, moving the entry in the recency list. When the
key is already a single hashable value and eviction is not needed, a plain dict
is a third of the price.

**`classmethod` is the surprise at 44.0ns**, more than three times a plain call
and 60% more than a `staticmethod`. The class object has to be resolved and
bound as the first argument on every call. Alternative constructors called in a
loop are worth noticing.

**`__slots__` did not make attribute access faster.** 4.34ns against 4.24ns for
an ordinary instance dict — a difference well inside the noise. This surprises
people who reach for `__slots__` as a speed optimisation. It is a *memory*
optimisation; CPython's attribute lookup has been fast for ordinary classes for
years, and since 3.11 the specialising interpreter caches the lookup for both
shapes.

## What to do with this

Most of the time, nothing. A 13ns call is invisible next to a 200-microsecond
database round trip, and code arranged for readability is worth far more than
these differences. The numbers matter in one specific place: inside loops that
run often enough that nanoseconds multiply into something a profiler will show
you.

When you are in that place, the ranking that matters is:

1. **Remove the call from the loop entirely** if you can — hoist it, or inline it.
   Nothing else on this list saves 13ns.
2. **Fix the decorator wrappers** on anything hot. Halving a decorator's cost by
   writing the real signature is the cheapest real win here, and it is a
   mechanical change.
3. **Pass positionally** in hot paths. 19ns of the difference between keyword
   and positional is free to reclaim and costs only a little readability.
4. **Reach for a dict** instead of `lru_cache` when the key is already a single
   value and you do not need eviction.
5. **Stop worrying about `try`/`except`.** It is free now.

And do not use `__slots__` for speed. Use it for memory, which is what it is for.

> [!NOTE]
> These are figures from one machine and one interpreter: an Apple M1, CPython
> 3.13.0, arm64. The absolute nanoseconds will differ on x86, on a different
> CPython, and dramatically on PyPy, which compiles most of this away. The
> *ratios* are what travel, and even those should be re-measured before anyone
> refactors on their strength. The script is in the repository; run it on your
> own hardware and see.

## Method

Each statement is compiled directly into the body of a timing loop, the way
`timeit` does it, rather than being wrapped in a callable. That distinction is
load-bearing: measuring `lambda: x.y` would silently add a full 13ns function
call to every result, which is larger than most of the things on this list.

An empty loop is timed under identical conditions and subtracted, so each figure
is the marginal cost of the operation rather than the cost of the operation plus
a `for`. Iteration counts are calibrated so a trial runs at least 50ms, putting
timer resolution well below the noise. Each statement runs nine trials, and the
**minimum** is reported: timing noise on a general-purpose OS is additive, so the
fastest observed run is the closest estimate of the true cost. The median and
the spread are recorded in the raw JSON, and no figure here came from a run with
a spread above 15%.

That paragraph describes the harness as it stood when these numbers were taken.
On 2026-08-31 it was rewritten — many short trials instead of a few long ones,
and a convergence check on the minimum instead of a threshold on the spread —
because the old scheme was not reliable below a microsecond on a busy machine.
The reasoning is in
[At what size does a dict beat a list?](/p/dict-vs-list-crossover/). Re-running
this experiment today therefore samples differently from the run above; the
figures here have not yet been re-measured under the new scheme, and this note
will be replaced by the new numbers when they have.

Garbage collection is disabled during measurement. That is a real distortion —
GC is not free in production — but allocation rates inside a microbenchmark
loop bear no relation to a real workload, so leaving it on measures the harness
rather than the operation.

The harness has [its own test suite][selftest] which checks the properties that
must hold if the timing is sound: that an empty statement registers as free,
that ten copies of a statement cost about ten times one copy, that setup code
does not leak into the measurement, and that two runs of the same statement
agree. Those tests pass on the machine that produced this table.

[selftest]: https://github.com/Mehtaz247/Mehtaz247.github.io/blob/main/experiments/lib/selftest.py
