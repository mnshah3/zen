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


def test_bank_results_are_point_in_time(tmp_path, monkeypatch):
    import duckdb
    from zen.data import bank_results as br
    db = tmp_path / "bank.duckdb"
    con = duckdb.connect(str(db))
    br.ensure_schema(con)
    rows = [("XB", "2026-03-31", "2026-05-01 18:00", False, 100e7, 40e7, 30e7, 1.0, 0.02, 0.005, "u1"),
            ("XB", "2026-06-30", "2026-07-20 18:00", False, 110e7, 44e7, 33e7, 1.1, 0.018, 0.004, "u2")]
    for sym, pe, bd, cons, ie, ix, np_, eps, g, n, url in rows:
        con.execute("INSERT INTO bank_results (symbol, period_end, broadcast_dt, consolidated, interest_earned, "
                    "interest_expended, profit_reported, eps_basic, gross_npa_pct, net_npa_pct, has_figures, xbrl_url) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, true, ?)", [sym, pe, bd, cons, ie, ix, np_, eps, g, n, url])
    con.close()
    monkeypatch.setattr(api, "DB", db)
    early = api.bank_results("xb", "2026-07-01")
    assert [q["period_end"] for q in early["quarters"]] == ["2026-03-31"]
    late = api.bank_results("XB")["quarters"][0]
    assert late["net_interest_income_cr"] == 66.0 and late["net_profit_cr"] == 33.0 and late["gross_npa_pct"] == 1.8


def test_balance_sheet_and_cash_flow_are_point_in_time(tmp_path, monkeypatch):
    import duckdb
    from zen.data import statements as st
    db = tmp_path / "st.duckdb"
    con = duckdb.connect(str(db))
    st.ensure_schema(con)
    rows = [  # symbol, period end, broadcast, days, ppe, cash, equity, cfo, capex, intangibles, cfi, cff, url
        ("XCO", "2025-09-30", "2025-11-10 18:00", 183, 400e7, 50e7, 900e7, 60e7, 25e7, None, -30e7, -20e7, "u1"),
        ("XCO", "2026-03-31", "2026-05-12 18:00", 365, 440e7, 54e7, 990e7, 130e7, 70e7, 6e7, -80e7, -46e7, "u2"),
        ("XCO", "2026-03-31", "2026-06-01 09:00", 365, 441e7, 54e7, 990e7, 131e7, 70e7, 6e7, -80e7, -47e7, "u3"),   # revised
    ]
    for sym, pe, bd, days, ppe, cash, eq, cfo, capex, intan, cfi, cff, url in rows:
        con.execute("INSERT INTO statements (symbol, period_end, broadcast_dt, consolidated, format, fy_end, ytd_days, "
                    "ppe, cash, equity, cfo, capex_ppe, capex_intangibles, cfi, cff, cf_adds_up, cash_ties, has_detail, "
                    "has_cash_flow, xbrl_url) VALUES (?, ?, ?, true, 'indas', '2026-03-31', ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                    "true, true, true, true, ?)", [sym, pe, bd, days, ppe, cash, eq, cfo, capex, intan, cfi, cff, url])
    con.close()
    monkeypatch.setattr(api, "DB", db)
    early = api.cash_flow("xco", "2026-01-15")
    assert [(p["period_end"], p["months"]) for p in early["periods"]] == [("2025-09-30", 6)]
    assert early["periods"][0]["capex_cr"] == 25.0 and early["periods"][0]["free_cash_flow_cr"] == 35.0
    assert "capex_intangibles_cr" not in early["periods"][0]                 # a line the filing does not tag
    before_revision = api.cash_flow("XCO", "2026-05-31")["periods"][0]
    assert before_revision["cfo_cr"] == 130.0 and before_revision["months"] == 12
    now = api.cash_flow("XCO")
    assert now["basis"] == "consolidated" and [p["period_end"] for p in now["periods"]] == ["2026-03-31", "2025-09-30"]
    latest = now["periods"][0]
    assert latest["cfo_cr"] == 131.0 and latest["capex_cr"] == 76.0 and latest["free_cash_flow_cr"] == 55.0
    assert latest["adds_up"] is True and latest["financial_year_end"] == "2026-03-31"
    bs = api.balance_sheet("XCO")
    assert bs["format"] == "indas" and bs["periods"][0]["ppe_cr"] == 441.0 and bs["periods"][1]["cash_cr"] == 50.0
    assert api.balance_sheet("XCO", "2025-10-01")["periods"] == [] and api.cash_flow("NOPE")["periods"] == []


def test_statement_tools_say_so_when_the_table_is_not_built(tmp_path, monkeypatch):
    import duckdb
    db = tmp_path / "empty.duckdb"
    duckdb.connect(str(db)).close()
    monkeypatch.setattr(api, "DB", db)
    assert "update_statements" in api.cash_flow("XCO")["note"] and "update_statements" in api.balance_sheet("XCO")["note"]


def test_shareholding_carries_the_split_of_the_filing_chosen(tmp_path, monkeypatch):
    import duckdb
    from zen.data import shareholding as sh, shareholding_detail as detail
    db = tmp_path / "sh.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE prices (date DATE)")
    con.execute("INSERT INTO prices VALUES ('2026-10-09')")
    sh.ensure_schema(con)
    detail.ensure_schema(con)
    for rid, pe, bd, prom in (("1", "2026-03-31", "2026-04-15 18:00", 50.0), ("2", "2026-06-30", "2026-07-15 18:00", 51.0),
                              ("3", "2026-06-30", "2026-08-01 10:00", 51.5)):               # 3 revises 2
        con.execute("INSERT INTO shareholding (symbol, period_end, promoter_pct, public_pct, broadcast_dt, record_id) "
                    "VALUES ('XCO', ?, ?, ?, ?, ?)", [pe, prom, 100 - prom, bd, rid])
    for rid, fii, agrees in (("1", 20.0, True), ("2", 21.0, True), ("3", 99.0, False)):     # 3 does not match its listing
        con.execute("INSERT INTO shareholding_detail (record_id, symbol, fii_pct, dii_pct, mf_pct, shareholders, agrees, "
                    "has_split) VALUES (?, 'XCO', ?, 15.0, 9.0, 1000, ?, true)", [rid, fii, agrees])
    con.close()
    monkeypatch.setattr(api, "DB", db)
    before = api.shareholding("xco", "2026-07-20")
    assert [(r["period_end"], r["promoter_pct"], r["fii_pct"]) for r in before] == [("2026-06-30", 51.0, 21.0),
                                                                                  ("2026-03-31", 50.0, 20.0)]
    now = api.shareholding("XCO")
    # the revision is the filing chosen; its document disagrees with its listing, so no split is shown for it
    assert now[0]["promoter_pct"] == 51.5 and now[0]["fii_pct"] is None and now[1]["fii_pct"] == 20.0
    assert now[1]["dii_pct"] == 15.0 and now[1]["shareholders"] == 1000.0
