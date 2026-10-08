"""zen/research_api.py: read-only, point-in-time queries for AI assistants (zen/mcp_server.py)."""

from datetime import date, datetime

import duckdb
import pytest

from zen import research_api as api


@pytest.fixture()
def archive(tmp_path, monkeypatch):
    db = tmp_path / "zen.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE prices (date DATE, symbol VARCHAR, series VARCHAR, open DOUBLE, high DOUBLE, low DOUBLE, "
                "close DOUBLE, volume BIGINT, turnover DOUBLE)")
    con.executemany("INSERT INTO prices VALUES (?, 'ABC', 'EQ', 1, 1, 1, ?, 1, 1)",
                    [(date(2026, 8, 3), 100.0), (date(2026, 8, 4), 101.0), (date(2026, 8, 10), 105.0)])
    con.execute("CREATE TABLE financials (symbol VARCHAR, company VARCHAR, period_end DATE, consolidated BOOLEAN, "
                "broadcast_dt TIMESTAMP, revenue DOUBLE, total_income DOUBLE, ebitda DOUBLE, profit_reported DOUBLE, "
                "profit_normalised DOUBLE, eps_basic DOUBLE, quarter_span_days INTEGER)")
    con.executemany("INSERT INTO financials VALUES ('ABC', 'ABC Limited', ?, true, ?, ?, ?, ?, ?, ?, ?, 91)", [
        (date(2026, 3, 31), datetime(2026, 5, 10, 18), 1e9, 1.1e9, 2e8, 1e8, 1e8, 5.0),
        (date(2026, 6, 30), datetime(2026, 8, 7, 18), 1.2e9, 1.3e9, 3e8, 1.5e8, 1.5e8, 7.5),
        (date(2026, 6, 30), datetime(2026, 8, 20, 9), 1.2e9, 1.3e9, 3e8, 1.6e8, 1.6e8, 8.0),   # a revision
    ])
    con.execute("CREATE TABLE announcements (an_dt TIMESTAMP, symbol VARCHAR, category VARCHAR, subject VARCHAR, url VARCHAR)")
    con.executemany("INSERT INTO announcements VALUES (?, 'ABC', ?, ?, 'https://x')", [
        (datetime(2026, 8, 7, 18), "results", "Financial Result Updates: ABC Limited has informed the Exchange about results"),
        (datetime(2026, 8, 9, 10), "routine", "Trading Window: closure"),
        (datetime(2026, 8, 21, 10), "orders", "Bagging/Receiving of orders/contracts: ABC Limited has informed the Exchange about an order"),
    ])
    con.close()
    monkeypatch.setattr(api, "DB", db)
    return db


def test_results_are_point_in_time(archive):
    before = api.quarterly_results("abc", as_of="2026-08-06")
    assert [q["period_end"] for q in before["quarters"]] == ["2026-03-31"]
    first = api.quarterly_results("ABC", as_of="2026-08-10")
    assert first["quarters"][0]["net_profit_cr"] == 15.0 and first["basis"] == "consolidated"
    revised = api.quarterly_results("ABC", as_of="2026-08-20")
    assert revised["quarters"][0]["net_profit_cr"] == 16.0 and revised["quarters"][0]["eps"] == 8.0


def test_filings_window_and_materiality(archive):
    got = api.filings("ABC", since="2026-08-01", until="2026-08-20")
    assert [f["category"] for f in got] == ["results"]                 # routine left out; the 21 Aug order is after
    assert got[0]["summary"] == "Results"
    assert len(api.filings("ABC", since="2026-08-01", material_only=False)) == 3


def test_prices_stop_at_the_date_asked(archive):
    rows = api.price_history("ABC", start="2026-08-01", end="2026-08-05")["rows"]
    assert [r["date"] for r in rows] == ["2026-08-03", "2026-08-04"] and rows[-1]["close"] == 101.0


def test_missing_archive_says_how_to_build_it(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "DB", tmp_path / "none.duckdb")
    with pytest.raises(FileNotFoundError, match="rebuild_db"):
        api.price_history("ABC")
