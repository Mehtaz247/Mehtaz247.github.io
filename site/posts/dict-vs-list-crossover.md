---
title: At what size does a dict beat a list?
description: Everyone knows a set is O(1) and a list is O(n), and everyone also knows that for small collections the list wins anyway. Measured on CPython 3.13, the second half is wrong from the first element.
verdict: At one element. A set beat a list on every miss measured, down to a one-element container, and on hits from two elements up — so the question worth asking is not how big the container is but whether you had to build it, which a set repays after about two lookups.
date: 2026-08-31
tags: [Python, data structures, performance, benchmarks]
experiment: dict-vs-list-crossover
hardware: Apple M1 MacBook Air (8 cores, 8 GB), macOS 26.6.2
software: CPython 3.13.1, arm64
method: Membership tests inlined into a timing loop; 151 to 1,203 short trials depending on operation cost, minimum reported; probes equal but not identical to the element they match
---

Someone writes `if user_id in allowed_ids` where `allowed_ids` is a list, and
someone else leaves the comment: *use a set, list membership is O(n)*. Then the
rebuttal, which is also standard: *not for small collections — a linear scan
over a short contiguous array is cache-friendly, and a hash lookup has a fixed
cost the scan doesn't pay. Below ten or twenty elements the list is faster.*

Both sides are quoting asymptotics at each other. Neither is quoting a number.
Here is the number, and it is much smaller than the argument assumes.

## The crossover

Container size from 1 to 10,000, sweeping `x in lst` against `x in st`, for a
**hit** — the probe is present, at the midpoint, which is the mean scan length
for a uniformly random hit — and for a **miss**, which is the list's worst case
and is what most membership tests actually do.

!figure[Both axes are logarithmic. The scan is a straight rising line because its cost is proportional to the elements it compares; the hash lookup is flat because it compares one. The lines cross at the very left edge of the chart, not in the middle of it.](assets/charts/dict-vs-list-crossover.svg)

| Elements | list, miss | set, miss | list, hit | set, hit | list vs set, miss |
|---|--:|--:|--:|--:|--:|
| 1 | 40 ns | 31 ns | 38 ns | 47 ns | 1.30x |
| 2 | 54 ns | 33 ns | 54 ns | 47 ns | 1.62x |
| 3 | 68 ns | 33 ns | 54 ns | 47 ns | 2.04x |
| 4 | 84 ns | 36 ns | 68 ns | 48 ns | 2.32x |
| 6 | 113 ns | 30 ns | 83 ns | 47 ns | 3.78x |
| 8 | 142 ns | 30 ns | 98 ns | 47 ns | 4.68x |
| 12 | 213 ns | 38 ns | 127 ns | 47 ns | 5.58x |
| 16 | 273 ns | 38 ns | 157 ns | 48 ns | 7.14x |
| 24 | 388 ns | 30 ns | 225 ns | 47 ns | 13x |
| 32 | 508 ns | 30 ns | 284 ns | 48 ns | 17x |
| 48 | 740 ns | 30 ns | 405 ns | 48 ns | 25x |
| 64 | 988 ns | 30 ns | 520 ns | 48 ns | 32x |
| 128 | 1,937 ns † | 34 ns | 1,003 ns | 46 ns | 56x |
| 256 | 3,902 ns | 35 ns | 1,964 ns | 46 ns | 112x |
| 1,024 | 16.9 µs | 35 ns | 7,963 ns | 47 ns | 482x |
| 10,000 | 151.2 µs | 34 ns | 79.9 µs | 47 ns | 4,461x |

† Three of the 135 measurements in this run did not converge to the standard
this blog publishes at, and they are marked with a dagger wherever they appear
rather than quietly dropped. Nothing below rests on any of them; the method
section explains the standard and why they failed it.

**There is no small-`n` region where the list wins a miss.** Not at ten, not at
four, not at one: a single-element list costs 40 ns to fail a membership test
and a single-element set costs 31 ns. For a hit the list wins exactly one size —
`n=1`, 38.5 ns against 47.3 ns — and loses from two elements up.

Fitting the scan as `fixed + slope × elements compared` gives **15.0 ns per
element** with a **25.2 ns** fixed cost for a miss, and 16.0 ns with 30.4 ns for
a hit. That model, fitted from the smallest and the two largest sizes only,
predicts every one of the sixteen measured points to within 9.2% on the miss
curve and 13% on the hit curve. Setting it equal to the flat hash cost puts the
crossover at **0.5 elements** for a miss and **1.0** for a hit.

A crossover below one element is a strange thing to report, so state it the
plain way: the hash lookup's fixed cost is *already lower* than the scan's fixed
cost, and every element after that is pure loss.

The dict behaves like the set throughout — 30.2 to 40.1 ns on a miss, 48.6 to
50.7 ns on a hit — so nothing below distinguishes them, and everything said
about `in set` applies to `in dict`.

## Two things that fall out of the table

The set is **more expensive on a hit than on a miss** (47 ns against 33 ns), which
is backwards from most people's mental model. It is the right way round for open
addressing: a miss usually lands on an empty slot and returns immediately,
having compared nothing, while a hit has to actually compare the key it found.
That comparison is the extra 14 ns.

And the list costs about the same on a hit and a miss at `n=1` — 38.5 ns
against 40.2 ns — as it must, since one element means one comparison either way. That
agreement is not a finding; it is a check that the two halves of the sweep were
measuring the same thing.

## So why does everyone believe in the small-`n` exception?

The obvious explanation is the identity shortcut. `list.__contains__` goes
through `PyObject_RichCompareBool`, which returns true on a pointer match before
attempting a value comparison — and in CPython small integers are shared
singletons, short strings are interned, and keys are often the same objects that
went into the container. If the folklore were measuring identity comparisons
rather than value comparisons, that would explain it.

It is not the explanation. Same list, same position, three different probes:

| Probe at index 4 of an 8-element list | Time |
|---|--:|
| A different object with an equal value | 98.8 ns |
| The element itself (identity match) | 85.0 ns |
| Small ints, `4 in list(range(8))` | 85.8 ns |

Fourteen nanoseconds. The shortcut fires once — on the one element that matches —
and the four elements before it are compared by value regardless. It buys about
one comparison's worth of the ~16 ns per element, which is a neat confirmation
that most of that per-element cost really is the comparison, and no help at all
to the list.

The actual reason the belief survives is duller, and it is worth being honest
about: at eight elements the set saves **112 nanoseconds**. Nobody has ever
noticed 112 nanoseconds. The list is not fast — it is *irrelevant*, which feels
identical from the outside until the container grows and it stops being either.

## Every key type makes it worse

The crossover above uses integers, which is the case most favourable to the
list. Hitting the midpoint of a 64-element container:

| Key type | `in list` | `in set` |
|---|--:|--:|
| `int` | 577 ns | 48 ns |
| `str` (11 chars) | 862 ns | 42 ns |
| `tuple[int, int]` | 1,120 ns † | 61 ns |

The scan gets 1.5x worse on strings and 1.9x worse on tuples, because comparing
those is more work than comparing two machine words. The hash lookup barely
moves — and on strings it gets *faster* than on integers, because `str` caches
its hash on the object, so a repeated lookup of the same key skips hashing
entirely.

Tuples are the one case where the hash side pays something real: `tuple` does
**not** cache its hash, so every lookup rehashes the whole tuple, and the set
lookup goes from 48 ns to 61 ns. Even so, the scan degrades three times faster
than the lookup does. There is no key type that rescues the list.

(A tuple *container* scans marginally faster than a list — 248.9 ns against
268.9 ns at 16 elements, about 7%. Real, reproducible, and far too small to
change any decision.)

## Where the decision actually lives

If the size crossover is at one element, then size is not the interesting
variable. Construction is. Building a set is work the list never does, and the
asymptotic argument ignores it completely:

| Elements | Build the set | Saved per miss | Lookups to break even |
|---|--:|--:|--:|
| 4 | 226 ns | 48 ns | **4.7** |
| 16 | 514 ns | 235 ns | **2.2** |
| 64 | 1,614 ns | 958 ns | **1.7** |
| 1,024 | 24.4 µs † | 16.9 µs | **1.4** |

Two lookups. Building a set from a 16-element list costs about as much as two
membership tests against that list, and everything after the second is profit.
At 1,024 elements it has repaid itself before the second lookup finishes.

This is the rule that replaces the size rule: **the question is not how many
elements you have, it is how many times you look.** Once is a wash. Twice is
already worth it. And a set that is built once at import and read forever — the
overwhelmingly common shape — is free.

The corollary is the failure mode. A set built *inside* the loop that queries it
is strictly worse than the list, at every size, and no amount of "but sets are
O(1)" saves it:

```python
for record in records:                    # build cost paid per record
    if record.id in set(allowed_ids):     # never repays: one lookup each
        ...
```

## The literal trap

Which brings up the one place where the advice reverses, and it is the form most
people actually write:

| Expression | Time |
|---|--:|
| `x in [1000000, 1000007, ...]` (5 constants) | 89.9 ns |
| `x in (1000000, 1000007, ...)` (5 constants) | 89.0 ns |
| `x in {1000000, 1000007, ...}` (5 constants) | **27.4 ns** |
| `x in [a, b, c, d, e]` (5 variables) | 150.1 ns |
| `x in {a, b, c, d, e}` (5 variables) | **277.0 ns** |

The same syntax, twice, with opposite answers. With **constant** members the set
literal is 3.3x faster than the list; with **variable** members it is 1.8x
*slower*. The bytecode says why:

```
x in [1, 3, 5, 7, 9]   ->  LOAD_NAME  LOAD_CONST  CONTAINS_OP
x in {1, 3, 5, 7, 9}   ->  LOAD_NAME  LOAD_CONST  CONTAINS_OP
x in {a, b, c, d, e}   ->  LOAD_NAME  LOAD_NAME x5  BUILD_SET  CONTAINS_OP
```

When every member is a constant the compiler folds the whole container into the
code object: the list becomes a tuple constant, the set becomes a **frozenset**
constant. Neither is built at runtime, so the frozenset gets its O(1) lookup for
nothing. When the members are variables nothing can be folded, and
`BUILD_SET` runs on every evaluation — hashing five values to answer one
question. That is the loop mistake above, spelled with braces instead of a
function call.

(And the line the disassembly above leaves out: `x in [a, b, c]` emits
`BUILD_TUPLE`, not `BUILD_LIST`. The compiler will not build a list for a
membership test even when you write one.)

## What it costs in memory

The one axis where the list wins, and it is not close:

| 10,000 integers | Bytes | vs. list |
|---|--:|--:|
| `list` | 85,176 | 1.0x |
| `dict` | 294,992 | 3.5x |
| `set` | 524,504 | 6.2x |

A hash table has to stay sparse to stay fast, and you pay for the empty slots.
The set keeps a sparser table than the dict at this size, which is why the
`dict.fromkeys()` version is smaller than the `set()` version despite doing
more. If you have a great many of these containers, that 6.2x is the real
argument for the list — not speed.

## What to do with this

1. **Default to a set for membership, at every size.** There is no small-`n`
   exception to find. The break-even is one element, and writing `{...}` instead
   of `[...]` costs nothing at the time you write it.
2. **Build it once.** This is the only part that needs thought. Hoist the set out
   of the loop, make it a module-level constant, build it beside the list rather
   than inside the query. Two lookups repay construction at sixteen elements and
   above; at four elements it takes about five, which is still not many.
3. **Write `x in {1, 2, 3}`, never `x in (1, 2, 3)`.** For constant members the
   compiler hands you a frozenset for free — 3.3x here, with no build cost and
   no downside at all.
4. **But write `x in (a, b, c)` when the members are variables.** The set literal
   builds a real set on every evaluation and was 1.8x slower than the sequence
   form. Braces are only free when the compiler can fold them.
5. **Don't refactor existing small containers for this.** At eight elements it is
   112 ns. Unless it is inside a hot loop, that is not a change worth making, and
   the diff costs more attention than the nanoseconds are worth. This is advice
   about what to write next, not what to go and fix.
6. **Keep the list when you need order, duplicates, unhashable elements, or the
   memory** — 6.2x at ten thousand integers. Those are good reasons. "It's faster
   for small `n`" is not one of them.

The thing worth carrying away is that the two sides of this argument were both
answering the wrong question. Neither the O(1) nor the O(n) tells you anything
useful at the sizes real code uses, because at those sizes both are fast enough
to be invisible. What actually decides it is a ratio the asymptotics don't
mention at all: lookups per construction. Above a handful, use a set. At one, it
never mattered.

> [!NOTE]
> Three limits worth naming. **These are CPython 3.13 numbers on one M1 laptop**;
> PyPy's JIT can eliminate a short scan entirely and would move the crossover,
> possibly a long way. **Every probe here is equal but not identical to what it
> matches** — deliberately, because keys read from a file or a socket are, but
> code that checks membership using the very objects it built the container from
> gets the identity shortcut a little more often than these numbers show, and
> the difference was 14 ns. **And nothing here is measured cold**: the containers
> are live in cache. A ten-thousand element list scanned from cold memory would
> be worse than reported, not better, which does not change any conclusion.

## Method

Every measurement is a statement inlined into a timing loop, with the empty loop
measured under identical conditions and subtracted. The minimum across trials is
reported: timing noise on a general-purpose OS is additive, so the fastest
observed run is the best estimate of true cost.

This run needed more care than the previous ones, and the reason is worth
publishing rather than hiding. **The machine was busy** — the load average on
eight cores went from 2.6 at the start of the run to 36.6 by the end, both
figures recorded in `results.json`, because the process writing this post was
running on the same laptop. At that load the median of a 30 ns measurement is
polluted while its minimum is not, so the usual "report the spread" check stops
measuring the benchmark and starts measuring the laptop: spreads here reach 290%
on measurements whose minima are reproducible to a nanosecond. A first, discarded
version of this experiment — sampled the conventional way, nine long trials —
put the same set lookup anywhere between 12 ns and 68 ns depending on which run
you asked. Those runs are not in the committed data; they are why the harness
changed.

The harness now does three things about it. It **samples by scale**: an
operation under a microsecond gets 401 short windows rather than 9 long ones,
because it only takes one clean window to see the floor and a long window is
more likely to catch a preemption. It **measures whether the minimum converged**,
by splitting the samples into interleaved halves and comparing the two minima —
if independent halves of a run agree on the floor, the floor is real, however
noisy the median. And it **re-samples anything that fails that check** at three
times the trial count. Of the 135 measurements here, 14 needed re-sampling and
**three still did not converge**: `list n=128 miss`, `list tuple n=64 hit` and
`build set n=1024`, all during the load spike at the end of the run. They carry a
dagger in the tables above, and nothing in this post depends on them — the shape
of the sweep is carried by the other fifteen sizes, the tuple-key point by the
8-element measurement, and the break-even argument by the 16- and 64-element
rows. Publishing them marked is more useful than deleting them, and both are
better than quoting them as though they were solid. The per-measurement stability
figures are in
[`results.json`](https://github.com/Mehtaz247/Mehtaz247.github.io/blob/main/experiments/dict-vs-list-crossover/results.json)
alongside the medians and the load average.

Two independent checks that this is not an artefact of the harness. Every number
on this blog comes from one piece of timing code, which makes a systematic error
in that code invisible from the inside, so the per-element scan cost was
reproduced with **stdlib `timeit`** — a different implementation, run with the
same many-short-windows methodology. It gives **15.73 ns per element against the
harness's 14.96, a disagreement of 5.1%** — larger than one would like, and the
honest reading is that the per-element cost is 15 ns give or take a nanosecond
rather than any of the decimal places above. The same 5.1% gap turned up on an
earlier pair of runs, which points at a small systematic difference in how the
two subtract loop overhead rather than at noise. That check is
[`run.py --crosscheck`](https://github.com/Mehtaz247/Mehtaz247.github.io/blob/main/experiments/dict-vs-list-crossover/run.py)
and its output is committed in `crosscheck.json`. Second, the two-point model of
the scan, fitted using only the smallest and the two largest of sixteen sizes,
predicts the thirteen sizes it was not fitted on to within 13% — which a
mismeasured curve would not do.

Garbage collection is disabled during measurement. The chart and the tables are
both generated from the same `results.json`, so they cannot disagree. The script
is
[`run.py`](https://github.com/Mehtaz247/Mehtaz247.github.io/blob/main/experiments/dict-vs-list-crossover/run.py);
the harness has
[its own test suite](https://github.com/Mehtaz247/Mehtaz247.github.io/blob/main/experiments/lib/selftest.py),
which passes on the machine that produced these numbers.
