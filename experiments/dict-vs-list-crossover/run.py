#!/usr/bin/env python3
"""At what size does a dict beat a list?

"Use a set, lists are O(n)" is repeated whenever a membership test shows up in
review, and the standard rebuttal -- "not for small n, a linear scan over a
short contiguous array is cache-friendly and a hash lookup has a fixed cost" --
is repeated just as often. Both sides are quoting asymptotics at each other.
This experiment finds the number.

It measures four things the argument usually runs together:

1. **The lookup crossover.** Sweep container size and find where the scan stops
   being cheaper than the hash lookup, for a hit and for a miss separately.
   A miss is the list's worst case and is what most membership tests do.

2. **Why the intuition exists.** `list.__contains__` short-circuits on identity
   before comparing values, via `PyObject_RichCompareBool`. When the probe is
   literally the same object as the element -- which it is for any small int,
   for interned strings, and whenever keys are shared rather than parsed --
   the scan gets much cheaper. Every other measurement here uses probes that
   are equal but *not* identical, because that is what a key read from a file,
   a socket or a database looks like.

3. **What the keys are.** `str` caches its hash on the object; `tuple` does
   not and rehashes on every lookup. The scan's per-element comparison differs
   by type too. Both ends of the comparison move.

4. **Whether you built the container.** Construction is charged once and
   amortised over lookups, and the asymptotic argument ignores it entirely.
   The compiler complicates this: a container literal whose members are all
   constants is folded at compile time, so it is not built at runtime at all.

Run: python3 experiments/dict-vs-list-crossover/run.py
"""

import dis
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from bench import Suite  # noqa: E402
from chart import log_line_chart, write as write_chart  # noqa: E402

HERE = Path(__file__).resolve().parent
CHART = HERE.parents[1] / "assets" / "charts" / "dict-vs-list-crossover.svg"


# Log-ish sweep, dense at the small end where the crossover was expected.
SIZES = [1, 2, 3, 4, 6, 8, 12, 16, 24, 32, 48, 64, 128, 256, 1024, 10_000]

# Values start above CPython's small-integer cache (-5..256) so no comparison
# in the scan can be settled by the interned-singleton identity shortcut.
INT_BASE = 1_000_000


def int_keys(n):
    return [INT_BASE + i * 7 for i in range(n)]


def str_keys(n):
    return [f"key_{i:07d}" for i in range(n)]


def tuple_keys(n):
    return [(INT_BASE + i, i * 3) for i in range(n)]


def distinct_int(v):
    """An int equal to v but a different object, so `is` cannot short-circuit."""
    out = int(str(v))
    assert out == v and out is not v, "int probe was interned; raise INT_BASE"
    return out


def distinct_str(v):
    out = (v + "!")[:-1]
    assert out == v and out is not v, "str probe was interned"
    return out


def distinct_tuple(v):
    out = tuple(list(v))
    assert out == v and out is not v
    return out


def opcodes(source):
    """The bytecode the compiler emits for an expression, as a compact string."""
    buf = io.StringIO()
    dis.dis(compile(source, "<x>", "eval"), file=buf)
    names = []
    for line in buf.getvalue().split("\n"):
        for tok in line.split():
            if tok.isupper() and len(tok) > 3 and tok.replace("_", "").isalpha():
                names.append(tok)
                break
    return " ".join(names)


def render_chart():
    """Draw the crossover figure from the committed results.

    Reads `results.json` rather than in-memory state, so the published figure
    and the published table cannot disagree: both come from the same file, and
    `--chart-only` regenerates the figure from a run without repeating it.
    """
    data = json.loads((HERE / "results.json").read_text())
    by_name = {r["name"]: r for r in data["results"]}
    sizes = data["facts"]["sizes"]

    def pts(container, kind):
        return [(n, by_name[f"{container} n={n} {kind}"]["ns_per_op"]) for n in sizes]

    svg = log_line_chart(
        [
            {"label": "list, miss", "points": pts("list", "miss"), "color": 0},
            {"label": "list, hit", "points": pts("list", "hit"), "color": 0, "dashed": True},
            {"label": "set, miss", "points": pts("set ", "miss"), "color": 1, "label_dy": 10},
            {"label": "set, hit", "points": pts("set ", "hit"), "color": 1, "label_dy": -9,
             "dashed": True},
        ],
        title="Membership test cost against container size, list versus set",
        x_label="elements in the container",
        y_label="time per membership test",
    )
    write_chart(svg, CHART)


def crosscheck_timeit():
    """Reproduce the headline slope with stdlib `timeit` instead of our harness.

    Every number on this blog comes from one harness, which makes a systematic
    error in that harness invisible from the inside. `timeit` is an independent
    implementation of the same idea, so if the two agree on the per-element cost
    of the scan, the figure is a property of CPython and not of our timing code.

    It is run the same way -- many short windows, minimum reported -- because
    timeit's own defaults take few long samples, and on a loaded machine that
    measures the scheduler.

    Run: python3 experiments/dict-vs-list-crossover/run.py --crosscheck
    """
    import timeit

    setup = """
data = [1_000_000 + i * 7 for i in range(N)]
lst = data
k = int(str(999_999))
"""
    REPEAT, NUMBER = 101, 20_000
    base = min(timeit.repeat("pass", "", number=NUMBER, repeat=REPEAT)) / NUMBER * 1e9
    small, large = 2, 64
    out = {}
    for n in (small, large):
        t = min(timeit.repeat("k in lst", setup.replace("N", str(n)),
                              number=NUMBER, repeat=REPEAT)) / NUMBER * 1e9
        out[n] = t - base
        print(f"timeit: list n={n} miss  {out[n]:8.2f} ns (net of a {base:.2f} ns empty loop)")

    slope = (out[large] - out[small]) / (large - small)
    ours = json.loads((HERE / "results.json").read_text())["facts"]["list_ns_per_element_miss"]
    print(f"\ntimeit  ns per element scanned: {slope:.2f}")
    print(f"harness ns per element scanned: {ours:.2f}")
    print(f"disagreement: {abs(slope - ours) / ours * 100:.1f}%")

    path = HERE / "crosscheck.json"
    path.write_text(json.dumps({
        "note": "Independent check of the scan slope using stdlib timeit.",
        "timeit_ns_per_element": round(slope, 2),
        "harness_ns_per_element": ours,
        "disagreement_pct": round(abs(slope - ours) / ours * 100, 1),
        "timeit_raw_ns": {str(k): round(v, 2) for k, v in out.items()},
        "empty_loop_ns": round(base, 2),
        "repeat": REPEAT, "number": NUMBER,
    }, indent=2) + "\n")
    print(f"wrote {path}")


def main():
    if "--chart-only" in sys.argv:
        render_chart()
        return
    if "--crosscheck" in sys.argv:
        crosscheck_timeit()
        return

    s = Suite(
        slug="dict-vs-list-crossover",
        question="At what container size does a hash lookup beat a linear scan?",
    )
    s.record("python", sys.version.split()[0])
    s.record("sizes", SIZES)
    s.record("int_base", INT_BASE)

    # -- 1. The sweep ------------------------------------------------------
    # Hit is probed at the midpoint, which is the mean scan length for a
    # uniformly random hit. Miss scans everything.
    print("\n-- membership: list vs set vs dict, integer keys --")
    for n in SIZES:
        data = int_keys(n)
        g = {"lst": data, "st": set(data), "dct": dict.fromkeys(data),
             "hit": distinct_int(data[n // 2]), "miss": distinct_int(INT_BASE - 1)}
        for kind in ("hit", "miss"):
            s.run(f"list n={n} {kind}", f"{kind} in lst", globals=g, n=n, kind=kind, container="list")
            s.run(f"set  n={n} {kind}", f"{kind} in st", globals=g, n=n, kind=kind, container="set")
            s.run(f"dict n={n} {kind}", f"{kind} in dct", globals=g, n=n, kind=kind, container="dict")

    # -- 2. The identity shortcut, which is where the intuition comes from --
    # Same list, same position, three probes: the element itself, an equal but
    # distinct object, and a small int (always a shared singleton in CPython).
    print("\n-- identity vs equality in the scan --")
    for n in (8, 64):
        data = int_keys(n)
        small = list(range(n))
        g = {"lst": data, "same": data[n // 2], "other": distinct_int(data[n // 2]),
             "small_lst": small, "small_probe": (n // 2) * 1}
        assert g["small_probe"] is small[n // 2], "small ints stopped being interned"
        s.run(f"list n={n} hit, identical probe", "same in lst", globals=g, n=n, probe="identical")
        s.run(f"list n={n} hit, equal probe", "other in lst", globals=g, n=n, probe="equal")
        s.run(f"list n={n} hit, small ints", "small_probe in small_lst", globals=g, n=n, probe="small-int")

    # -- 3. Does the key type move the answer? -----------------------------
    print("\n-- key type --")
    makers = {"int": (int_keys, distinct_int), "str": (str_keys, distinct_str),
              "tuple": (tuple_keys, distinct_tuple)}
    for n in (8, 64):
        for tname, (make, distinct) in makers.items():
            data = make(n)
            g = {"lst": data, "st": set(data), "hit": distinct(data[n // 2])}
            s.run(f"list {tname} n={n} hit", "hit in lst", globals=g, n=n, key_type=tname, container="list")
            s.run(f"set  {tname} n={n} hit", "hit in st", globals=g, n=n, key_type=tname, container="set")

    # A tuple container scans like a list; `x in (a, b)` is the commoner form.
    print("\n-- tuple container vs list container --")
    for n in (4, 16):
        data = int_keys(n)
        g = {"lst": data, "tup": tuple(data), "miss": distinct_int(INT_BASE - 1)}
        s.run(f"list n={n} miss (control)", "miss in lst", globals=g, n=n)
        s.run(f"tuple n={n} miss", "miss in tup", globals=g, n=n)

    # -- 4. Construction, which the asymptotic argument ignores ------------
    print("\n-- building the container --")
    for n in (4, 16, 64, 1024):
        g = {"data": int_keys(n)}
        s.run(f"build set n={n}", "set(data)", globals=g, n=n, op="build")
        s.run(f"build dict n={n}", "dict.fromkeys(data)", globals=g, n=n, op="build")
        s.run(f"copy list n={n}", "list(data)", globals=g, n=n, op="build")

    # -- 5. Literals, where the compiler changes the answer ----------------
    print("\n-- literal membership tests --")
    s.record("bytecode_list_literal_const", opcodes("x in [1, 3, 5, 7, 9]"))
    s.record("bytecode_tuple_literal_const", opcodes("x in (1, 3, 5, 7, 9)"))
    s.record("bytecode_set_literal_const", opcodes("x in {1, 3, 5, 7, 9}"))
    s.record("bytecode_list_literal_vars", opcodes("x in [a, b, c, d, e]"))
    s.record("bytecode_set_literal_vars", opcodes("x in {a, b, c, d, e}"))

    gv = {"x": distinct_int(INT_BASE - 1)}
    lit = ", ".join(str(INT_BASE + i * 7) for i in range(5))
    s.run("x in [const, ...] (5, miss)", f"x in [{lit}]", globals=gv, n=5, form="literal")
    s.run("x in (const, ...) (5, miss)", f"x in ({lit})", globals=gv, n=5, form="literal")
    s.run("x in {const, ...} (5, miss)", f"x in {{{lit}}}", globals=gv, n=5, form="literal")

    gvar = dict(zip("abcde", int_keys(5)))
    gvar["x"] = distinct_int(INT_BASE - 1)
    s.run("x in [a, b, c, d, e] (miss)", "x in [a, b, c, d, e]", globals=gvar, n=5, form="variable")
    s.run("x in {a, b, c, d, e} (miss)", "x in {a, b, c, d, e}", globals=gvar, n=5, form="variable")

    # -- 6. Memory, which does not depend on this machine's speed ----------
    for n in (8, 64, 1024, 10_000):
        data = int_keys(n)
        s.record(f"bytes_list_n{n}", sys.getsizeof(data))
        s.record(f"bytes_set_n{n}", sys.getsizeof(set(data)))
        s.record(f"bytes_dict_n{n}", sys.getsizeof(dict.fromkeys(data)))

    # -- Derived: where the curves actually cross --------------------------
    def series(container, kind):
        return {r.extra["n"]: r.ns_per_op for r in s.results
                if r.extra.get("container") == container and r.extra.get("kind") == kind
                and r.extra.get("key_type") is None}

    for kind in ("hit", "miss"):
        lst, st = series("list", kind), series("set", kind)
        below = [n for n in SIZES if lst[n] <= st[n]]
        above = [n for n in SIZES if lst[n] > st[n]]
        s.record(f"largest_n_where_list_wins_{kind}", max(below) if below else None)
        s.record(f"smallest_n_where_set_wins_{kind}", min(above) if above else None)

        # Model the scan as `fixed + slope * elements compared`, where a
        # midpoint hit compares about n/2 elements and a miss compares all n.
        #
        # The slope comes from the two largest sizes, where the per-element
        # term dwarfs everything else, and the intercept from the smallest,
        # where it does not. A least-squares fit over the whole sweep would be
        # dominated by n=10000 and returned a *negative* intercept -- which is
        # not a fixed cost, it is an artefact. The residual below says whether
        # the two-point model actually describes the sweep it was not fitted on.
        elems = (lambda n: n / 2) if kind == "hit" else (lambda n: float(n))
        n1, n2 = SIZES[-2], SIZES[-1]
        slope = (lst[n2] - lst[n1]) / (elems(n2) - elems(n1))
        fixed = lst[SIZES[0]] - slope * elems(SIZES[0])
        predicted = {n: fixed + slope * elems(n) for n in SIZES}
        residual = max(abs(predicted[n] - lst[n]) / lst[n] for n in SIZES) * 100

        set_cost = sum(st.values()) / len(st)
        s.record(f"list_ns_per_element_{kind}", round(slope, 2))
        s.record(f"list_fixed_ns_{kind}", round(fixed, 1))
        s.record(f"list_model_max_residual_pct_{kind}", round(residual, 1))
        s.record(f"set_ns_min_{kind}", round(min(st.values()), 1))
        s.record(f"set_ns_max_{kind}", round(max(st.values()), 1))
        s.record(f"set_mean_ns_{kind}", round(set_cost, 1))
        s.record(f"crossover_elements_{kind}", round((set_cost - fixed) / slope, 2))
        s.record(f"scan_slowdown_at_10000_{kind}", round(lst[10_000] / st[10_000], 1))

    # Break-even: how many lookups justify building the set at all.
    for n in (4, 16, 64, 1024):
        build = s.get(f"build set n={n}").ns_per_op
        saved = series("list", "miss")[n] - series("set", "miss")[n]
        s.record(f"breakeven_lookups_n{n}", round(build / saved, 1) if saved > 0 else None)

    s.save()
    render_chart()

    print("\n-- crossover summary (ns) --")
    lh, sh, lm, sm = series("list", "hit"), series("set", "hit"), series("list", "miss"), series("set", "miss")
    print(f"{'n':>7} {'list hit':>10} {'set hit':>9} {'list miss':>11} {'set miss':>10}")
    for n in SIZES:
        print(f"{n:>7} {lh[n]:>10.1f} {sh[n]:>9.1f} {lm[n]:>11.1f} {sm[n]:>10.1f}")


if __name__ == "__main__":
    main()
