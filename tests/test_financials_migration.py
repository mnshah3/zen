"""A database built before quarter_span_days existed must survive the upgrade.

The second audit of 2026-09-24 found that rebuilding into an existing 41-column
financials table deleted every row and then failed on the column count, leaving
the table empty, and that the daily updater's upsert would crash the same way.
"""

from __future__ import annotations

from datetime import date, datetime

import duckdb
import pandas as pd

from zen.data import financials as fin


def _old_schema() -> str:
    # The table as it was before 2026-09-24: every column except quarter_span_days.
    return fin.SCHEMA.replace("has_balance_sheet BOOLEAN,\n    xbrl_url VARCHAR,",
                              "has_balance_sheet BOOLEAN,\n    xbrl_url VARCHAR,") \
                     .replace(", quarter_span_days INTEGER", "") \
                     .replace("quarter_span_days INTEGER,", "")


def _row(url: str) -> dict:
    r = {c: None for c in fin.COLUMNS}
    r.update(symbol="ABC", period_end=date(2025, 3, 31), broadcast_dt=datetime(2025, 5, 20),
             consolidated=True, revenue=100.0, xbrl_url=url, has_balance_sheet=False,
             quarter_span_days=90)
    return r


def _old_table(con):
    con.execute(_old_schema())
    cols = [r[0] for r in con.execute("DESCRIBE financials").fetchall()]
    assert "quarter_span_days" not in cols, "fixture did not build the old schema"


def test_upsert_into_an_old_table(tmp_path):
    con = duckdb.connect()
    _old_table(con)
    added = fin.upsert(con, pd.DataFrame([_row("u1")]).reindex(columns=fin.COLUMNS))
    assert added == 1
    assert con.execute("SELECT quarter_span_days FROM financials").fetchone()[0] == 90


def test_rebuild_into_an_old_table_keeps_rows(tmp_path):
    fin.write_parquet(pd.DataFrame([_row("u1"), _row("u2")]).reindex(columns=fin.COLUMNS), tmp_path)
    con = duckdb.connect()
    _old_table(con)
    con.execute("INSERT INTO financials (symbol, period_end, broadcast_dt, consolidated, xbrl_url) "
                "VALUES ('OLD', DATE '2024-03-31', TIMESTAMP '2024-05-01', true, 'old')")
    n = fin.rebuild_from_parquet(con, tmp_path)
    assert n == 2


def test_failed_rebuild_leaves_the_table_as_it_was(tmp_path, monkeypatch):
    fin.write_parquet(pd.DataFrame([_row("u1")]).reindex(columns=fin.COLUMNS), tmp_path)
    con = duckdb.connect()
    fin.ensure_schema(con)
    con.execute("INSERT INTO financials (symbol, period_end, broadcast_dt, consolidated, xbrl_url) "
                "VALUES ('KEEP', DATE '2024-03-31', TIMESTAMP '2024-05-01', true, 'keep')")
    monkeypatch.setattr(fin, "_COLS_SQL", fin._COLS_SQL + ", no_such_column")
    try:
        fin.rebuild_from_parquet(con, tmp_path)
    except Exception:
        pass
    assert con.execute("SELECT count(*) FROM financials").fetchone()[0] == 1, \
        "a failed rebuild emptied the table"
