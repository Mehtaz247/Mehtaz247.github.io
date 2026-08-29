---
title: Is SELECT * actually slower?
description: Naming your columns instead of selecting all of them is one of the most repeated rules in code review. Measured on SQLite, the rule is right, but almost every reason given for it is wrong.
verdict: Yes — but not because the database reads more data. Roughly 90% of the cost is your client turning columns into objects, and the only case where the projection changes what the engine touches is when it defeats a covering index. That case cost 6.8x here.
date: 2026-08-29
tags: [SQLite, SQL, performance, benchmarks]
experiment: select-star-sqlite
hardware: Apple M1 MacBook Air (8 cores, 8 GB), macOS 26.6.2
software: CPython 3.13.1 (arm64), SQLite 3.48.0
method: Warm-cache queries against file-backed SQLite databases, minimum of 9 trials, results fully consumed with fetchall()
---

"Don't use `SELECT *`" is one of the few pieces of performance advice that
survives every code review, at every company, in every language. The rule is
sound. The reason usually given for it — *the database has to read data you
don't need* — is, for the most common shape of table, simply false.

So this post measures where the cost of `SELECT *` actually goes. The answer
turns out to be somewhere most people are not looking, and it changes which
queries are worth rewriting.

## The setup

Three SQLite tables, all file-backed, all queried warm:

| Table | Shape | Size |
|---|---|--:|
| `narrow` | `id` + 10 `INTEGER` columns, 50,000 rows | 2.0 MB |
| `wide` | `id`, `a`, five 200-byte `TEXT` columns, `z`, 50,000 rows | 68 MB |
| `overflow` | `id` + 5 `INTEGER` + one 4 KB `TEXT`, 20,000 rows | 82 MB |

Every query is fully consumed with `fetchall()`, because a query whose rows are
never read has not really been run.

## `SELECT *` is slower, and the effect is large

On the narrow table — ten small integer columns, the shape of most tables in
most applications — widening the projection scales the query almost linearly:

| Columns selected | Time | vs. 1 column |
|---|--:|--:|
| 1 | 10.87 ms | 1.00x |
| 2 | 16.09 ms | 1.48x |
| 3 | 20.18 ms | 1.86x |
| 5 | 29.25 ms | 2.69x |
| 8 | 44.18 ms | 4.06x |
| 11 (`SELECT *`) | 56.79 ms | 5.22x |

Five times slower. That looks like a decisive win for the rule, and it is — but
the interesting question is *what* is five times slower, because the obvious
answer is wrong.

## Where the cost is not

Here is the control that settles it. Compare selecting N *distinct* columns
against selecting the *same* column N times. Both return N columns per row.
The second reads only one column out of storage.

| Columns | N distinct columns | Same column, N times |
|---|--:|--:|
| 1 | 10.87 ms | 11.58 ms |
| 2 | 16.09 ms | 15.99 ms |
| 3 | 20.18 ms | 20.47 ms |
| 5 | 29.25 ms | 29.50 ms |
| 8 | 44.18 ms | 42.63 ms |
| 11 | 56.79 ms | 56.71 ms |

They are the same curve. At 11 columns the two differ by 0.1%. Reading ten
different columns out of the stored row costs no more than reading one column
ten times, which means the per-column cost is not being paid at the storage
layer at all.

The second control makes the same point from the other direction. Run the
identical full scan, but discard the results inside SQLite so nothing crosses
back into Python:

```sql
SELECT count(*) FROM (SELECT c0, c1 FROM t);   -- 0.304 ms
SELECT count(*) FROM (SELECT *      FROM t);   -- 0.304 ms
```

Identical to each other, and **187 times faster** than the same scan with the
rows returned. SQLite's planner drops columns that nothing consumes, so the
width of the projection stops mattering entirely.

## Where the cost is

Splitting the two halves apart directly: `sum()` forces SQLite to decode every
named column but returns a single row, so the client materialises nothing. The
same columns returned as a result set pay both costs.

| | 2 columns | 10 columns | Marginal cost per column, per row |
|---|--:|--:|--:|
| Decoded in-engine (`sum()`) | 2.10 ms | 6.34 ms | **10.6 ns** |
| Returned to Python | 16.42 ms | 52.88 ms | **91.1 ns** |

**An extra column costs 10.6 ns per row inside SQLite and 91.1 ns per row by the
time it reaches your program — a factor of 8.6.** And the 10.6 ns is an
overestimate, because it includes eight extra additions that the other row does
not perform.

Nearly all of what people attribute to "the database reading more data" is
CPython allocating an object per value and stuffing it into a tuple.

The reason is structural. SQLite is a **row store**: the row lives contiguously
inside a b-tree page, and scanning the table means reading those pages whether
you want one column from each row or all of them. Naming two columns does not
let SQLite read less of the file. It only lets it skip the decode step and,
much more importantly, skip handing the values to you.

This is where the folklore comes from, and why it is misapplied. "Select fewer
columns and the database reads less data" is *true* of columnar stores —
Parquet, ClickHouse, BigQuery, Redshift — where each column is a separate run of
bytes and an unread column is genuinely never touched. It is a columnar mental
model applied to a row store.

Two small confirmations that the row is being walked serially, both real but
tiny: `SELECT id` (the rowid, which is the b-tree key and is not stored in the
record at all) beats `SELECT c0` by 12 ns per row, and the last column of the
row costs 10.4 ns per row more than the first, which is the price of stepping
through the record header to reach it.

## Payload size matters, and it matters client-side too

On the `wide` table, the extra columns are 200-byte strings rather than small
integers:

| Query | Time |
|---|--:|
| `SELECT a` | 24.03 ms |
| `SELECT z` (the column after the text) | 24.72 ms |
| `SELECT a, z` | 29.45 ms |
| `SELECT *` | 74.27 ms |

`SELECT *` is 2.5x `SELECT a, z`, and the six extra columns cost about 150 ns
per row each against 92 ns for integers. The rows still fit on a page, so no
extra I/O is involved — a bigger string is simply a bigger object to allocate
and copy. The client-side cost scales with the bytes you ask for.

Note also that reaching `z`, which sits *after* a kilobyte of text, costs only
14 ns per row more than reaching `a`. Skipping over a column is nearly free;
returning it is not.

## The one case that is genuinely about I/O

When a value is too big for its page, SQLite spills it onto **overflow pages**,
which are chained off the row and read only if that column is required. This is
the one place where the projection changes how much of the file is touched:

| Query (20,000 rows, 4 KB of text each) | Time |
|---|--:|
| `SELECT c0, c1` | 21.48 ms |
| `SELECT *` | 42.49 ms |

Two times slower, and this half really is data the engine did not have to read.
If your table has a `description`, a JSON blob, or a serialised payload column,
`SELECT *` drags all of it off disk on every row.

## The case that actually matters: covering indexes

Everything so far is a constant factor. This one is a change of algorithm.

Add an index on `(c0, c1)` to the overflow table and run a range query matching
10,001 rows:

```sql
SELECT c0, c1 FROM t WHERE c0 BETWEEN 1000 AND 11000;   --  3.19 ms
SELECT *      FROM t WHERE c0 BETWEEN 1000 AND 11000;   -- 21.83 ms
```

**6.8x.** And the query plans say exactly why:

```
SEARCH t USING COVERING INDEX idx_c0_c1 (c0>? AND c0<?)
SEARCH t USING INDEX idx_c0_c1 (c0>? AND c0<?)
```

When the projection is contained in the index, SQLite answers the query from the
index alone and never opens the table. Ask for one more column and it must
follow every index entry back to the table b-tree — 10,001 extra random
descents, each landing on a 4 KB row.

The structural version of this is better than the timing, because it does not
depend on caches or hardware at all. `dbstat` reports the exact size of each
b-tree:

| Structure | Bytes |
|---|--:|
| Index `idx_c0_c1` | 260 KB |
| Table `t` | 82.1 MB |

The covering query can answer itself from **260 KB**. Adding one unneeded column
puts an 82 MB structure back in play — 308 times as much data live in the
query. That ratio holds on any machine, warm or cold.

This is the real content of the rule. `SELECT *` does not merely return more; it
silently revokes the planner's best option.

## And the case where it does not matter at all

Most queries in most applications fetch one row by primary key:

| Query | Time |
|---|--:|
| `SELECT c0, c1 FROM t WHERE id = ?` | 7.83 µs |
| `SELECT * FROM t WHERE id = ?` | 9.08 µs |

The difference is **1.26 microseconds**. Both are dominated by the fixed cost of
preparing and stepping a statement through Python's driver, not by the nine
extra columns. Rewriting this query is worth nothing.

## What to do with this

Ranked by how much it actually buys you:

1. **Name your columns when an index could cover the query.** This is the only
   item on the list that changes the plan rather than the constant factor, and
   it was worth 6.8x here — unbounded in general, because it scales with table
   size while the covering scan does not. If you take one thing from this post:
   check `EXPLAIN QUERY PLAN` for `COVERING INDEX`, and notice when a stray
   column removes it.
2. **Never `SELECT *` a table with a large `TEXT` or `BLOB` column you don't
   need.** Overflow pages are real I/O, and it was 2x here on rows of only 4 KB.
3. **Name your columns on large result sets** — tens of thousands of rows and
   up. Roughly 92 ns per column per row, so nine unneeded columns over 50,000
   rows is about 41 ms. Real, but a constant factor, and invisible below a few
   thousand rows.
4. **Don't bother rewriting single-row lookups.** 1.26 µs. Spend the review
   comment on something else.
5. **Stop justifying the rule with "the database reads more data."** On a row
   store it reads the same pages either way. Say "it can defeat a covering
   index," which is true, and which points at the queries actually worth fixing.

The corollary worth sitting with: if most of the cost of a wide result set is
your client building objects, then that cost is charged per row returned,
whatever the projection. Fetching 50,000 rows to use ten of them is a much
bigger mistake than fetching ten columns to use two, and no code review rule
catches it.

> [!NOTE]
> These numbers are SQLite on one M1 laptop with a warm page cache, driven by
> CPython 3.13.1. Three limits worth naming. **The client-side figure is
> CPython's**; a driver in Go or Rust allocates more cheaply and would shift the
> 8.6x split, though not the direction. **There is no network here** — against
> Postgres or MySQL, `SELECT *` also pays wire bytes for every unneeded column,
> a cost this experiment cannot see and which likely dominates. **And a warm
> cache flatters every row-store scan equally**; on cold storage the overflow
> and covering-index results would grow, not shrink. The covering-index byte
> counts are the one result here that is independent of all three.

## Method

Each query is compiled into a timing loop and run at least nine times, with the
minimum reported: timing noise on a general-purpose OS is additive, so the
fastest observed run is the best estimate of true cost. Every figure in this
post came from a single run of
[`run.py`](https://github.com/Mehtaz247/Mehtaz247.github.io/blob/main/experiments/select-star-sqlite/run.py),
and the raw output including medians and spreads is committed in
[`results.json`](https://github.com/Mehtaz247/Mehtaz247.github.io/blob/main/experiments/select-star-sqlite/results.json).
Spreads were under 3% for every measurement except `wide: SELECT *` (6.7%), and
nothing above 15% is cited here.

The overflow table is 20,000 rows rather than 50,000 on purpose. At 50,000 rows
it is a 205 MB working set on an 8 GB laptop, and the benchmark came back with a
23% spread — that measurement was of page-cache eviction, not of column
decoding, and it was not publishable. At 20,000 rows the working set stays
resident, the spread falls to 4%, and the ratio it reports (1.98x) agrees with
the noisy larger run (2.14x).

Databases are built once per run and queried warm, so no figure includes the
cost of first-touch disk reads. Garbage collection is disabled during
measurement. The benchmark harness has
[its own test suite](https://github.com/Mehtaz247/Mehtaz247.github.io/blob/main/experiments/lib/selftest.py),
which passes on the machine that produced this table.
