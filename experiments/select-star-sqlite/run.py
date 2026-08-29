#!/usr/bin/env python3
"""Is SELECT * actually slower?

The rule "never write SELECT *" is repeated in code review with almost no
numbers behind it, and the usual justification -- that the database reads data
it does not need -- turns out to be the wrong reason on the most common shape
of table.

This experiment separates three costs that the folklore runs together:

1. **Engine-side decoding.** Work SQLite does to find and decode stored columns.
2. **Client-side materialisation.** Work Python does turning result columns into
   objects. Charged per *result* column, whether or not it was stored separately.
3. **Access-path selection.** Whether the projection lets the planner satisfy the
   query from an index alone, or forces it back to the table b-tree.

The key control is comparing N *distinct* columns against the *same* column
repeated N times. Both return N result columns, but the second reads only one
stored column. If the two cost the same, the cost is client-side, not storage.

Run: python3 experiments/select-star-sqlite/run.py
"""

import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from bench import Suite  # noqa: E402


ROWS = 50_000
TEXT_LEN = 200
BIG_TEXT_LEN = 4_000       # forces the row onto overflow pages
NARROW_COLS = 10

# The overflow table carries 4KB per row, so 50k rows would be a 205MB working
# set on an 8GB laptop. At that size the measurement stops being about column
# decoding and starts being about page-cache eviction: the same benchmark came
# back with a 23% spread, which is not publishable. 20k rows is an 82MB working
# set that stays resident, and the ratio it reports matches the noisy larger run.
OVERFLOW_ROWS = 20_000

# Reading tens of megabytes per query is inherently noisier than a narrow scan,
# so the overflow and covering-index cases get more trials.
IO_TRIALS = 15


def build_narrow(path):
    """id + 10 INTEGER columns. The common shape: everything fits on a page."""
    c = sqlite3.connect(path)
    cols = ", ".join(f"c{i} INTEGER" for i in range(NARROW_COLS))
    c.execute(f"CREATE TABLE t (id INTEGER PRIMARY KEY, {cols})")
    c.executemany(
        "INSERT INTO t VALUES (" + ",".join("?" * (NARROW_COLS + 1)) + ")",
        [tuple([i] + [i + j for j in range(NARROW_COLS)]) for i in range(ROWS)],
    )
    c.commit()
    return c


def build_wide(path):
    """id, a, five 200-byte TEXT columns, z. `z` sits after the text payload."""
    c = sqlite3.connect(path)
    text_cols = ", ".join(f"t{i} TEXT" for i in range(5))
    c.execute(f"CREATE TABLE t (id INTEGER PRIMARY KEY, a INTEGER, {text_cols}, z INTEGER)")
    payload = "x" * TEXT_LEN
    c.executemany(
        "INSERT INTO t VALUES (" + ",".join("?" * 8) + ")",
        [tuple([i, i] + [payload] * 5 + [i]) for i in range(ROWS)],
    )
    c.commit()
    return c


def build_overflow(path):
    """One 4KB TEXT column, so every row spills onto overflow pages."""
    c = sqlite3.connect(path)
    cols = ", ".join(f"c{i} INTEGER" for i in range(5))
    c.execute(f"CREATE TABLE t (id INTEGER PRIMARY KEY, {cols}, body TEXT)")
    payload = "x" * BIG_TEXT_LEN
    c.executemany(
        "INSERT INTO t VALUES (" + ",".join("?" * 7) + ")",
        [tuple([i] + [i + j for j in range(5)] + [payload]) for i in range(OVERFLOW_ROWS)],
    )
    c.commit()
    return c


def dbstat(conn):
    """Exact bytes occupied per b-tree. Cache-independent, unlike timing."""
    return {name: size for name, size in
            conn.execute("SELECT name, sum(pgsize) FROM dbstat GROUP BY name").fetchall()}


def plan(conn, sql):
    return "; ".join(r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql).fetchall())


def main():
    tmp = Path(tempfile.mkdtemp(prefix="select-star-"))
    try:
        narrow = build_narrow(tmp / "narrow.db")
        wide = build_wide(tmp / "wide.db")
        over = build_overflow(tmp / "overflow.db")

        s = Suite(
            slug="select-star-sqlite",
            question="How much does SELECT * cost versus naming the columns you need?",
            globals={"narrow": narrow, "wide": wide, "over": over},
        )
        s.record("rows", ROWS)
        s.record("overflow_rows", OVERFLOW_ROWS)
        s.record("sqlite_version", sqlite3.sqlite_version)
        s.record("page_size", narrow.execute("PRAGMA page_size").fetchone()[0])
        s.record("narrow_db_pages", narrow.execute("PRAGMA page_count").fetchone()[0])
        s.record("wide_db_pages", wide.execute("PRAGMA page_count").fetchone()[0])
        over_pages = over.execute("PRAGMA page_count").fetchone()[0]
        page_size = narrow.execute("PRAGMA page_size").fetchone()[0]
        s.record("overflow_db_pages", over_pages)
        s.record("overflow_db_mb", round(over_pages * page_size / 1e6, 1))

        # -- 1. Where does the cost actually live? ---------------------------
        # N distinct columns vs the same column repeated N times. Same number of
        # result columns; wildly different numbers of stored columns read.
        print("\n-- narrow table: distinct columns vs one column repeated --")
        all_cols = ["id"] + [f"c{i}" for i in range(NARROW_COLS)]
        for n in (1, 2, 3, 5, 8, 11):
            distinct = ", ".join(all_cols[:n])
            repeated = ", ".join(["c0"] * n)
            s.run(f"{n} distinct columns", f"narrow.execute('SELECT {distinct} FROM t').fetchall()",
                  n_columns=n, kind="distinct")
            s.run(f"{n} copies of one column", f"narrow.execute('SELECT {repeated} FROM t').fetchall()",
                  n_columns=n, kind="repeated")

        # -- 1b. Splitting engine-side decoding from client-side materialising -
        # sum() forces SQLite to decode every named column but returns one row,
        # so Python materialises nothing. The same columns handed back as a
        # result set pay both costs. The difference between the two slopes is
        # the price of crossing into Python.
        print("\n-- engine-side decode vs handing columns back to Python --")
        for n in (2, 10):
            expr = " + ".join(f"c{i}" for i in range(n))
            cols = ", ".join(f"c{i}" for i in range(n))
            s.run(f"{n} columns decoded in-engine (sum)",
                  f"narrow.execute('SELECT sum({expr}) FROM t').fetchall()", n_columns=n)
            s.run(f"{n} columns returned to Python",
                  f"narrow.execute('SELECT {cols} FROM t').fetchall()", n_columns=n)

        # -- 1c. rowid is not stored in the record, and later columns are
        # reached by walking the record header, so position should cost a little.
        print("\n-- column position within the row --")
        s.run("SELECT id (the rowid)", "narrow.execute('SELECT id FROM t').fetchall()")
        s.run("SELECT c0 (first stored column)", "narrow.execute('SELECT c0 FROM t').fetchall()")
        s.run("SELECT c9 (last stored column)", "narrow.execute('SELECT c9 FROM t').fetchall()")

        # -- 2. Does SQLite decode columns nobody consumes? ------------------
        print("\n-- same scan, results discarded inside SQLite --")
        s.run("count(*) over 2 columns", "narrow.execute('SELECT count(*) FROM (SELECT c0, c1 FROM t)').fetchall()")
        s.run("count(*) over SELECT *", "narrow.execute('SELECT count(*) FROM (SELECT * FROM t)').fetchall()")

        # -- 3. Payload width -----------------------------------------------
        print("\n-- wide table: 5 x 200-byte TEXT columns --")
        s.run("wide: SELECT a", "wide.execute('SELECT a FROM t').fetchall()")
        s.run("wide: SELECT z (after the text)", "wide.execute('SELECT z FROM t').fetchall()")
        s.run("wide: SELECT a, z", "wide.execute('SELECT a, z FROM t').fetchall()")
        s.run("wide: SELECT *", "wide.execute('SELECT * FROM t').fetchall()")

        # -- 4. Overflow pages ------------------------------------------------
        print("\n-- overflow table: one 4KB TEXT column per row --")
        s.run("overflow: SELECT c0, c1", "over.execute('SELECT c0, c1 FROM t').fetchall()", trials=IO_TRIALS)
        s.run("overflow: SELECT *", "over.execute('SELECT * FROM t').fetchall()", trials=IO_TRIALS)

        # -- 5. The access path: covering index --------------------------------
        # This is the only case where the projection changes what the planner
        # can do, rather than just how much it hands back.
        over.execute("CREATE INDEX idx_c0_c1 ON t (c0, c1)")
        over.commit()
        rng = "WHERE c0 BETWEEN 1000 AND 11000"
        covered = f"SELECT c0, c1 FROM t {rng}"
        uncovered = f"SELECT * FROM t {rng}"
        s.record("plan_covered", plan(over, covered))
        s.record("plan_uncovered", plan(over, uncovered))
        s.record("matching_rows", over.execute(f"SELECT count(*) FROM t {rng}").fetchone()[0])

        print("\n-- covering index, 10k-row range query --")
        s.run("covered: SELECT c0, c1", f"over.execute({covered!r}).fetchall()", trials=IO_TRIALS)
        s.run("uncovered: SELECT *", f"over.execute({uncovered!r}).fetchall()", trials=IO_TRIALS)

        stats = dbstat(over)
        s.record("bytes_table_overflow", stats.get("t"))
        s.record("bytes_index_c0_c1", stats.get("idx_c0_c1"))

        # -- 6. A single row, which is what most queries actually fetch --------
        print("\n-- point lookup by primary key --")
        s.run("point lookup: 2 columns", "narrow.execute('SELECT c0, c1 FROM t WHERE id = 25000').fetchall()")
        s.run("point lookup: SELECT *", "narrow.execute('SELECT * FROM t WHERE id = 25000').fetchall()")

        # -- derived: marginal cost of one extra result column -----------------
        one = s.get("1 distinct columns").ns_per_op
        eleven = s.get("11 distinct columns").ns_per_op
        per_col_per_row = (eleven - one) / 10 / ROWS
        s.record("ns_per_extra_column_per_row", round(per_col_per_row, 1))

        # The headline decomposition: 8 extra columns, decoded only, versus the
        # same 8 columns turned into Python objects.
        engine = (s.get("10 columns decoded in-engine (sum)").ns_per_op
                  - s.get("2 columns decoded in-engine (sum)").ns_per_op) / 8 / ROWS
        client = (s.get("10 columns returned to Python").ns_per_op
                  - s.get("2 columns returned to Python").ns_per_op) / 8 / ROWS
        s.record("ns_per_column_per_row_engine_side", round(engine, 1))
        s.record("ns_per_column_per_row_client_side", round(client, 1))
        s.record("client_to_engine_ratio", round(client / engine, 1))

        s.save()

        print("\n-- summary (ms per query) --")
        for r in s.results:
            print(f"  {r.name:<34} {r.ns_per_op / 1e6:9.2f} ms   spread {r.spread_pct:5.1f}%")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
