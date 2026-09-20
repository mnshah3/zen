"""The stored parquet and the table schema must agree.

THIS TEST EXISTS BECAUSE THE SAME BUG HAS SHIPPED TWICE.

Both times the shape was identical. A column was added to the parquet and not
to the CREATE TABLE, everything kept working locally because the developer's
DuckDB file already held the table from an earlier run, and CI broke on the
next scheduled job because CI builds the database from scratch every time.

  financials    a bare *.parquet glob swept two bookkeeping files into the
                table: "41 columns but 45 values were supplied"
  announcements session_date was added to 783,510 rows and never to the
                schema: "table announcements has 10 columns but 11 values were
                supplied". Both daily workflows failed every day for four days.

A developer cannot notice this locally, because the thing that hides it -- an
existing database file -- is exactly what a working environment has. So it has
to be a test, and the test has to compare the two definitions directly rather
than relying on anyone remembering to rebuild.
"""

from __future__ import annotations

import glob

import duckdb
import pytest

from zen.data import announcements, financials, indices, store

# table -> (CREATE statement, parquet glob). Every table the daily jobs rebuild.
TABLES = {
    "prices": (store.SCHEMA, "data/daily/**/*.parquet"),
    "announcements": (announcements.SCHEMA, "data/announcements/**/*.parquet"),
    "indices": (indices.SCHEMA, "data/indices/*.parquet"),
}


def _table_columns(create_sql: str, table: str) -> list[str]:
    con = duckdb.connect()
    con.execute(create_sql)
    cols = [r[0] for r in con.execute(f"DESCRIBE {table}").fetchall()]
    con.close()
    return cols


def _parquet_columns(pattern: str) -> list[str]:
    if not glob.glob(pattern, recursive=True):
        pytest.skip(f"no parquet matching {pattern}")
    con = duckdb.connect()
    cols = [r[0] for r in con.execute(
        f"DESCRIBE SELECT * FROM read_parquet('{pattern}', union_by_name=true)"
    ).fetchall()]
    con.close()
    return cols


@pytest.mark.parametrize("table", sorted(TABLES))
def test_schema_matches_parquet(table):
    create_sql, pattern = TABLES[table]
    in_table = _table_columns(create_sql, table)
    in_parquet = _parquet_columns(pattern)

    missing = [c for c in in_parquet if c not in in_table]
    extra = [c for c in in_table if c not in in_parquet]

    assert not missing, (
        f"{table}: the parquet has columns the table does not: {missing}. "
        f"A rebuild will fail with 'N columns but M values were supplied'. "
        f"Add them to the CREATE TABLE.")
    assert not extra, (
        f"{table}: the table declares columns absent from the parquet: {extra}. "
        f"A rebuild will fail or silently misalign. Either write the column or "
        f"drop it from the schema.")


def test_financials_glob_excludes_bookkeeping():
    """The financials table reads a file LIST, never a bare glob.

    data/financials holds the filing index and the confirmed-absent ledger
    alongside the statements. A bare *.parquet glob pulled both into the table
    and the insert failed on a column count -- an error about columns that was
    really about two files having no business being read.
    """
    files = {p.stem for p in financials.statement_files()}
    assert not (files & set(financials.NON_STATEMENT)), (
        "statement_files() is returning bookkeeping parquet: "
        f"{sorted(files & set(financials.NON_STATEMENT))}")


def test_every_required_table_is_covered():
    """Whatever rebuild_db calls REQUIRED must be checked here.

    Otherwise a table can be promoted to required and drift unnoticed, which is
    how this gap opened in the first place.
    """
    from jobs.rebuild_db import REQUIRED
    unchecked = [t for t in REQUIRED
                 if t not in TABLES and t not in ("corpactions",)]
    assert not unchecked, (
        f"required tables with no parity test: {unchecked}")
