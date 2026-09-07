#!/usr/bin/env python3
"""At what selectivity does SQLite stop using an index and scan the table?

Every planner textbook says the same thing: an index is a win when a query
matches few rows, a full scan is a win when it matches many, and somewhere in
between there is a crossover the optimiser is supposed to find. The number
usually quoted -- inherited from Postgres and Oracle folklore -- is "a few
percent".

This experiment measures two different things that are easy to confuse:

1. **The real crossover.** Where does a forced index lookup actually stop
   beating a forced table scan? Measured by running the same query twice, once
   with `INDEXED BY` and once with `NOT INDEXED`, across a selectivity sweep.
2. **The planner's crossover.** Where does SQLite *decide* to switch? Read
   directly out of `EXPLAIN QUERY PLAN`, which is a structural observation and
   involves no timing at all.

These do not have to be the same number, and the gap between them is the point
of the post.

Three controls separate the effects that the folklore runs together:

- **Row order.** An index on a column uncorrelated with rowid sends the table
  lookups to random pages; an index on a correlated column walks them nearly in
  order. Same index, same selectivity, very different cost.
- **Covering.** If every column the query needs is in the index, the table is
  never touched and there is no crossover to find.
- **Literal versus bound parameter.** The planner can only use its histograms
  if it can see the value at prepare time. `WHERE v < 5000` and `WHERE v < ?`
  are the same query to a reader and different queries to the optimiser.

Run: python3 experiments/index-selectivity-crossover/run.py
     python3 experiments/index-selectivity-crossover/run.py --chart-only
"""

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[0] / "lib"))
from bench import Suite  # noqa: E402
from chart import log_line_chart, write as write_chart  # noqa: E402

CHART = HERE.parents[1] / "assets" / "charts" / "index-selectivity-crossover.svg"

# Sized so the harness can actually sample it. The 2026-09-07 run used 200,000
# rows, which put the scan at ~55ms and the 100%-selectivity index path at
# ~1.25s; at 401 trials that is eight minutes for a single measurement, so the
# wall-clock budget cuts the trial count and the result is refused as
# undersampled. 50,000 rows keeps the whole crossover region cheap enough to
# sample properly. The crossover is expected to be a *fraction* of the table
# rather than a row count -- both access paths scale linearly in table size --
# but that is an assumption this experiment should test rather than assume, so
# a future run should repeat the sweep at a second size and compare.
ROWS = 50_000
PAD_LEN = 60          # table stays resident, so this measures b-tree work
                      # rather than disk
DISTINCT = 10_000     # distinct values of v; 5 rows per value
ROWS_PER_VALUE = ROWS // DISTINCT

# A prime stride decorrelates v from rowid without clustering anything.
SCATTER_STRIDE = 7919

# Selectivity points, as a fraction of the table. Dense between 1% and 10%
# because that is where the real crossover is expected to sit.
FRACTIONS = [0.001, 0.0025, 0.005, 0.01, 0.02, 0.03, 0.04, 0.05,
             0.08, 0.10, 0.15, 0.20, 0.30, 0.50, 1.0]

# The query needs `pad`, which is not in the index, so the index path must go
# back to the table for every matching row. That is the case where a crossover
# can exist at all.
NONCOVERING = "SELECT sum(length(pad)) FROM t {hint} WHERE v < {arg}"
# `v` and the rowid are both in the index, so this one never touches the table.
COVERING = "SELECT count(*), sum(v) FROM t {hint} WHERE v < {arg}"


def build(path, scattered):
    """200k rows. `v` is either uncorrelated with rowid, or sorted by it.

    Both tables hold exactly the same multiset of values -- 20 rows for each of
    10,000 distinct v -- so selectivity at a given threshold is identical and
    the only difference is the order the rows sit in.
    """
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v INTEGER, pad TEXT)")
    pad = "x" * PAD_LEN
    if scattered:
        rows = [(i, (i * SCATTER_STRIDE) % DISTINCT, pad) for i in range(ROWS)]
    else:
        rows = [(i, i // ROWS_PER_VALUE, pad) for i in range(ROWS)]
    c.executemany("INSERT INTO t VALUES (?,?,?)", rows)
    c.execute("CREATE INDEX iv ON t(v)")
    c.commit()
    return c


def threshold(frac):
    """The `v <` bound that matches `frac` of the table."""
    return int(round(DISTINCT * frac))


def plan(conn, sql):
    return "; ".join(r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql).fetchall())


def uses_index(plan_text):
    return "USING INDEX" in plan_text or "USING COVERING INDEX" in plan_text


def matched(conn, thr):
    """Rows actually matched. A structural count, not an estimate."""
    return conn.execute("SELECT count(*) FROM t WHERE v < ?", (thr,)).fetchone()[0]


def page_counts(conn):
    """Pages held by the table and by the index, from dbstat.

    Structural, timing-free, and the input to the cost model below.
    """
    return {name: (pages, size) for name, pages, size in conn.execute(
        "SELECT name, count(*), sum(pgsize) FROM dbstat GROUP BY name")}


def fit_line(xs, ys):
    """Least-squares slope and intercept. Used to predict a crossover from the
    low-selectivity end of the sweep and test it against the high end."""
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom
    return slope, my - slope * mx


def render_chart():
    """Draw the crossover figure from the committed results, not from memory,
    so the published chart and the published table cannot disagree."""
    data = json.loads((HERE / "results.json").read_text())
    by_name = {r["name"]: r for r in data["results"]}
    fracs = data["facts"]["fractions"]

    def pts(table, path):
        out = []
        for f in fracs:
            r = by_name.get(f"{table} {path} sel={f * 100:g}%")
            if r:
                out.append((f * 100, r["ns_per_op"] / 1e6))  # ns -> ms
        return out

    svg = log_line_chart(
        [
            {"label": "scattered, index", "points": pts("scattered", "index"), "color": 0},
            {"label": "scattered, scan", "points": pts("scattered", "scan"), "color": 0,
             "dashed": True},
            {"label": "clustered, index", "points": pts("clustered", "index"), "color": 1},
            {"label": "clustered, scan", "points": pts("clustered", "scan"), "color": 1,
             "dashed": True},
        ],
        title="Query time against selectivity: forced index versus forced table scan",
        x_label="percent of rows matched",
        y_label="query time (ms)",
    )
    write_chart(svg, CHART)
    print(f"wrote {CHART}")


def main():
    tmp = Path(tempfile.mkdtemp(prefix="index-selectivity-"))
    scattered = build(tmp / "scattered.db", scattered=True)
    clustered = build(tmp / "clustered.db", scattered=False)

    s = Suite(
        slug="index-selectivity-crossover",
        question="At what selectivity does SQLite stop using an index and scan the table instead?",
        globals={"scattered": scattered, "clustered": clustered},
    )
    s.record("rows", ROWS)
    s.record("distinct_values", DISTINCT)
    s.record("rows_per_value", ROWS_PER_VALUE)
    s.record("fractions", FRACTIONS)
    s.record("sqlite_version", sqlite3.sqlite_version)
    s.record("stat4_compiled_in", any(
        "ENABLE_STAT4" in r[0] for r in scattered.execute("PRAGMA compile_options")))
    s.record("page_size", scattered.execute("PRAGMA page_size").fetchone()[0])

    for label, conn in (("scattered", scattered), ("clustered", clustered)):
        pages = page_counts(conn)
        s.record(f"{label}_table_pages", pages.get("t", (0, 0))[0])
        s.record(f"{label}_index_pages", pages.get("iv", (0, 0))[0])
        s.record(f"{label}_table_bytes", pages.get("t", (0, 0))[1])
        s.record(f"{label}_index_bytes", pages.get("iv", (0, 0))[1])

    # -- 0. Structural: how many rows does each threshold actually match? ----
    # Verifies the sweep is measuring the selectivity it claims to.
    print("\n-- rows matched at each threshold (structural count) --")
    match_counts = {}
    for f in FRACTIONS:
        thr = threshold(f)
        n = matched(scattered, thr)
        match_counts[f] = n
        assert n == matched(clustered, thr), "tables must have identical selectivity"
    s.record("rows_matched", {str(f): match_counts[f] for f in FRACTIONS})
    s.record("selectivity_error_pct", max(
        abs(match_counts[f] - f * ROWS) / (f * ROWS) * 100 for f in FRACTIONS))

    # -- 1. The planner's decision, before and after ANALYZE ----------------
    # Pure EXPLAIN QUERY PLAN: no timing, so nothing here can be distorted by
    # a slow machine. Recorded for literal and bound-parameter forms, which the
    # optimiser treats differently.
    print("\n-- planner decisions (EXPLAIN QUERY PLAN, no timing) --")
    planner = {}
    for stats in ("no_stats", "analyzed"):
        if stats == "analyzed":
            for c in (scattered, clustered):
                c.execute("ANALYZE")
                c.commit()
        for label, conn in (("scattered", scattered), ("clustered", clustered)):
            for form in ("literal", "bound"):
                for f in FRACTIONS:
                    thr = threshold(f)
                    arg = str(thr) if form == "literal" else "?"
                    sql = NONCOVERING.format(hint="", arg=arg)
                    p = (plan(conn, sql) if form == "literal"
                         else "; ".join(r[3] for r in conn.execute(
                             "EXPLAIN QUERY PLAN " + sql, (thr,))))
                    planner[f"{stats}/{label}/{form}/{f}"] = p
                # Report the switch point: the lowest selectivity at which the
                # planner stops using the index.
                key = f"{stats}/{label}/{form}"
                switch = next((f for f in FRACTIONS
                               if not uses_index(planner[f"{key}/{f}"])), None)
                s.record(f"planner_switch/{key}",
                         "never" if switch is None else f"{switch * 100:g}%")
    s.record("planner_plans", planner)

    # The covering query, for contrast: the table is never touched.
    cov = {}
    for f in (0.001, 0.5, 1.0):
        cov[str(f)] = plan(scattered, COVERING.format(hint="", arg=threshold(f)))
    s.record("covering_plans", cov)

    # -- 2. The real crossover: force each plan and time it -----------------
    # `INDEXED BY iv` and `NOT INDEXED` take the choice away from the planner,
    # so this measures the two access paths rather than the optimiser.
    for label in ("scattered", "clustered"):
        print(f"\n-- {label}: forced index vs forced scan --")
        for f in FRACTIONS:
            thr = threshold(f)
            for path, hint in (("index", "INDEXED BY iv"), ("scan", "NOT INDEXED")):
                sql = NONCOVERING.format(hint=hint, arg="?")
                s.run(f"{label} {path} sel={f * 100:g}%",
                      f"{label}.execute({sql!r}, ({thr},)).fetchone()",
                      selectivity=f, path=path, table=label,
                      rows_matched=match_counts[f])

    # -- 3. Covering index, scattered table ---------------------------------
    # Same table, same selectivity, but the query needs nothing the index does
    # not already hold.
    print("\n-- scattered: covering query (index holds everything needed) --")
    for f in (0.01, 0.10, 0.50, 1.0):
        thr = threshold(f)
        for path, hint in (("index", "INDEXED BY iv"), ("scan", "NOT INDEXED")):
            sql = COVERING.format(hint=hint, arg="?")
            s.run(f"covering {path} sel={f * 100:g}%",
                  f"scattered.execute({sql!r}, ({thr},)).fetchone()",
                  selectivity=f, path=path, table="covering")

    out = s.save()

    # -- 4. Cross-check: predict the crossover from the low end only --------
    # Fit index cost and scan cost on selectivities at or below 2%, then solve
    # for where the two lines meet and compare against the measured switch.
    # This is a check on the timing that does not reuse the timing it checks:
    # if the measured crossover and the extrapolated one agree, the curve is
    # behaving like the linear model the cost argument assumes.
    data = json.loads(Path(out).read_text())
    by_name = {r["name"]: r for r in data["results"]}
    crosschecks = {}
    for label in ("scattered", "clustered"):
        def ms(path, f):
            return by_name[f"{label} {path} sel={f * 100:g}%"]["ns_per_op"] / 1e6

        low = [f for f in FRACTIONS if f <= 0.02]
        xs = [match_counts[f] for f in low]
        ib, ia = fit_line(xs, [ms("index", f) for f in low])
        sb, sa = fit_line(xs, [ms("scan", f) for f in low])
        predicted_rows = (ia - sa) / (sb - ib) if sb != ib else float("inf")

        measured = next((f for f in FRACTIONS if ms("index", f) > ms("scan", f)), None)
        below = [f for f in FRACTIONS if ms("index", f) <= ms("scan", f)]
        crosschecks[label] = {
            "index_fit_ms_per_matched_row": round(ib * 1000, 4),
            "index_fit_intercept_ms": round(ia, 3),
            "scan_fit_ms_per_matched_row": round(sb * 1000, 4),
            "scan_fit_intercept_ms": round(sa, 3),
            "predicted_crossover_rows": round(predicted_rows),
            "predicted_crossover_pct": round(predicted_rows / ROWS * 100, 2),
            "measured_first_loss_pct": None if measured is None else measured * 100,
            "measured_last_win_pct": (max(below) * 100) if below else None,
            "fitted_on": [f * 100 for f in low],
        }
        print(f"\n{label}: model fitted on <=2% predicts crossover at "
              f"{crosschecks[label]['predicted_crossover_pct']}% of rows; "
              f"measured between {crosschecks[label]['measured_last_win_pct']}% and "
              f"{crosschecks[label]['measured_first_loss_pct']}%")

    data["crosscheck"] = {
        "what": ("Index and scan costs fitted as straight lines on selectivities at or "
                 "below 2% only, then extrapolated to find where they meet. The "
                 "measured crossover is not used in the fit, so agreement between the "
                 "two is evidence the sweep is not an artefact of the timing code."),
        "by_table": crosschecks,
    }
    Path(out).write_text(json.dumps(data, indent=2) + "\n")
    print(f"\nrewrote {out} with crosscheck")

    render_chart()


if __name__ == "__main__":
    if "--chart-only" in sys.argv:
        render_chart()
    else:
        main()
