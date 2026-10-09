"""NSE's event calendar (zen/data/events.py)."""

from __future__ import annotations

from datetime import date, datetime

import duckdb

from zen.data import events

ROWS = [{"symbol": "AAA", "company": "Aaa Ltd", "purpose": "Financial Results", "bm_desc": "To approve results",
         "date": "16-Oct-2026"},
        {"symbol": "BBB", "company": "Bbb Ltd", "purpose": "Fund Raising", "bm_desc": "To consider fund raising",
         "date": "12-Oct-2026"},
        {"symbol": "", "date": "12-Oct-2026"}, {"symbol": "CCC", "date": "not a date"}]


def test_parse_flags_results_and_drops_bad_rows():
    df = events.parse(ROWS, datetime(2026, 10, 9, 15))
    assert list(df["symbol"]) == ["AAA", "BBB"] and list(df["is_results"]) == [True, False]


def test_merge_keeps_the_first_sighting(tmp_path):
    first = events.parse(ROWS[:1], datetime(2026, 10, 9, 15))
    later = events.parse(ROWS[:2], datetime(2026, 10, 10, 15))
    m = events.merge(events.merge(None, first), later)
    aaa = m[m["symbol"] == "AAA"].iloc[0]
    assert aaa["observed_dt"] == datetime(2026, 10, 9, 15) and aaa["last_seen_dt"] == datetime(2026, 10, 10, 15)
    assert m[m["symbol"] == "BBB"].iloc[0]["observed_dt"] == datetime(2026, 10, 10, 15)
    p = tmp_path / "events.parquet"
    events.write(m, p)
    con = duckdb.connect()
    assert events.rebuild(con, p) == 2
    # as of the 9th, only what was listed by the end of the 9th
    assert list(events.upcoming(con, date(2026, 10, 9), days=30)["symbol"]) == ["AAA"]
    assert list(events.upcoming(con, date(2026, 10, 10), days=30)["symbol"]) == ["BBB", "AAA"]
    assert list(events.upcoming(con, date(2026, 10, 10), days=30, results_only=True)["symbol"]) == ["AAA"]
