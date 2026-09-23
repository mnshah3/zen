"""The financials parquet writer must keep every revision and accept either date type.

Two bugs, both found on 2026-09-23 and both silent until they fired:

  REVISIONS. The writer de-duplicated on (symbol, period_end, consolidated) and
  kept the last row, so every write deleted the earlier versions of any revised
  filing. The database keeps every version, keyed on the document, and the
  engine uses whichever had been broadcast by each decision date. A file that
  held only the latest version would hand a backtest in November a correction
  published in January.

  DATE TYPES. A backfill wrote `period_end` as timestamps. The regular writer
  appended Python dates to that file, and pyarrow refused the mix. The next
  update would have crashed half-way, after writing to the database.
"""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd

from zen.data import financials as fin


def _row(url: str, pe, revenue: float, broadcast: datetime) -> dict:
    r = {c: None for c in fin.COLUMNS}
    r.update(symbol="ABC", company="ABC Ltd", period_end=pe, broadcast_dt=broadcast,
             consolidated=True, audited=False, revenue=revenue, xbrl_url=url)
    return r


def test_revisions_survive_a_write(tmp_path):
    first = pd.DataFrame([_row("u1", date(2025, 3, 31), 100.0, datetime(2025, 5, 20))])
    fin.write_parquet(first, tmp_path)
    revised = pd.DataFrame([_row("u2", date(2025, 3, 31), 90.0, datetime(2025, 7, 1))])
    fin.write_parquet(revised, tmp_path)
    got = pd.read_parquet(tmp_path / "2025-03.parquet")
    assert sorted(got["xbrl_url"]) == ["u1", "u2"], "an earlier version was deleted"


def test_same_document_is_not_duplicated(tmp_path):
    a = pd.DataFrame([_row("u1", date(2025, 3, 31), 100.0, datetime(2025, 5, 20))])
    fin.write_parquet(a, tmp_path)
    fin.write_parquet(a, tmp_path)
    assert len(pd.read_parquet(tmp_path / "2025-03.parquet")) == 1


def test_dates_and_timestamps_can_be_mixed(tmp_path):
    ts = pd.DataFrame([_row("u1", pd.Timestamp("2025-03-31"), 100.0, datetime(2025, 5, 20))])
    ts["period_end"] = pd.to_datetime(ts["period_end"])
    ts.to_parquet(tmp_path / "2025-03.parquet", index=False)
    as_date = pd.DataFrame([_row("u2", date(2025, 3, 31), 90.0, datetime(2025, 7, 1))])
    fin.write_parquet(as_date, tmp_path)           # used to raise ArrowTypeError
    assert len(pd.read_parquet(tmp_path / "2025-03.parquet")) == 2
