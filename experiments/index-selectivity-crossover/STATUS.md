# index-selectivity-crossover — not yet publishable

**Status as of 2026-09-07: designed, run once, and deliberately not published.**

## What is here

`results-2026-09-07-throttled.json` is a complete run of the sweep at 200,000
rows. It is committed as evidence, not as a result, and there is intentionally
no `results.json`: nothing in this directory has been certified.

Two independent reasons it cannot be published:

1. **The machine was throttled.** `machine_speed.ratio` was 0.37 against a floor
   of 0.75, so every absolute figure reads roughly 2.7x high.
2. **34 of 68 measurements did not converge**, with stability up to 39% and
   spreads up to 90%. The crossover is the headline of this experiment, and the
   crossover sits exactly where the two curves are closest together — which is
   precisely where that much noise decides the answer.

It also predates the sampling-plan fix of the same day, so even its *converged*
millisecond measurements are suspect: at 40 trials a 55ms scan passed the
convergence check at a figure 45% above its settled floor. See the `_PLANS` note
in `experiments/lib/bench.py`.

## What survives from that run

The **structural** findings involve no timing and are unaffected by either
problem. They are read straight out of `EXPLAIN QUERY PLAN`:

- With a **bound parameter**, SQLite never abandons the index at any
  selectivity, up to and including 100% of rows.
- With a **literal** and after `ANALYZE`, it switches to a scan only at 100%.
- Without `ANALYZE` it never switches at all.
- A **covering** index is used at every selectivity, correctly — there is no
  crossover to find when the table is never touched.

The timing sweep pointed at a real crossover near 2-4% of rows for an
uncorrelated index, but that number is **not established** and must not be
quoted until a clean run reproduces it.

## What the next run needs to do

- Re-run at the current 50,000-row size, which is sized to fit the harness's
  sampling budget, on a machine at `machine_speed.ratio >= 0.75`.
- Check `unstable_results` is empty and no result is flagged `undersampled`.
- Repeat the sweep at a second table size and confirm the crossover is a
  fraction of the table rather than a row count. `run.py` assumes this and does
  not currently test it.
- Note the one place the 2026-09-07 cross-check disagreed with itself: the
  linear model fitted below 2% predicted a 14.4% crossover for the *clustered*
  table, but the index never actually lost there. If that survives a clean run
  it is a finding, not an error — per-row cost falls as selectivity rises when
  the rows are in rowid order — but it needs to be measured, not asserted.
