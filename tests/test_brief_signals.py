"""The morning brief's insider, rating and FPI section (zen.notify.signals, render._signals_section)."""

from datetime import datetime

import duckdb
import pandas as pd

from zen.notify import render, signals

NOW = datetime(2026, 10, 23, 7, 0)


def _con():
    con = duckdb.connect()
    con.execute("CREATE TABLE announcements (symbol VARCHAR, company VARCHAR)")
    con.execute("INSERT INTO announcements VALUES ('AAA', 'Aaa Limited'), ('BBB', 'Bbb Limited')")
    ins = pd.DataFrame([
        ("AAA", "2026-10-22 18:00", "Promoter Group", "Equity Shares", "Buy", "Market Purchase", 3e7),
        ("AAA", "2026-10-22 19:00", "Promoter", "Equity Shares", "Buy", "Market Purchase", 1e7),
        ("BBB", "2026-10-22 12:00", "Director", "Equity Shares", "Sell", "Market Sale", 5e6),
        ("BBB", "2026-10-22 12:00", "Promoter", "Equity Shares", "Buy", "Gift", 9e9),              # not a market trade
        ("AAA", "2026-10-20 10:00", "Promoter", "Equity Shares", "Buy", "Market Purchase", 9e9),   # before the window
    ], columns=["symbol", "broadcast_dt", "category", "instrument", "txn", "mode", "value"])
    ins["broadcast_dt"] = pd.to_datetime(ins["broadcast_dt"])
    con.register("_i", ins)
    con.execute("CREATE TABLE insider AS SELECT * FROM _i")
    rt = pd.DataFrame([("AAA", "2026-10-22 20:00", "downgrade", "downgrade", "CRISIL", "u/1.pdf"),
                       ("BBB", "2026-10-22 20:00", "reaffirmed", "reaffirmed", "ICRA", "u/2.pdf")],
                      columns=["symbol", "an_dt", "action", "actions", "agency", "url"])
    rt["an_dt"] = pd.to_datetime(rt["an_dt"])
    con.register("_r", rt)
    con.execute("CREATE TABLE ratings AS SELECT * FROM _r")
    fpi = pd.DataFrame([(pd.Timestamp("2026-10-15"), pd.Timestamp("2026-10-01"), s, v, pd.Timestamp("2026-10-22 21:30"))
                        for s, v in (("Power", 500.0), ("Realty", -800.0), ("Total", -300.0))],
                       columns=["period_end", "period_start", "sector", "net_eq_cr", "observed_dt"])
    con.register("_f", fpi)
    con.execute("CREATE TABLE fpi_sectors AS SELECT * FROM _f")
    return con


def test_the_window_and_what_counts():
    sig = signals.collect(_con(), NOW)
    assert [(x["symbol"], x["who"], round(x["net_cr"], 2), x["n"]) for x in sig["insiders"]["buys"]] == [("AAA", "Promoters", 4.0, 2)]
    assert [(x["symbol"], x["who"], round(x["net_cr"], 2)) for x in sig["insiders"]["sells"]] == [("BBB", "Directors or key managers", -0.5)]
    assert [(x["symbol"], x["action"], x["company"]) for x in sig["ratings"]] == [("AAA", "downgrade", "Aaa Limited")]   # a reaffirmation is routine
    assert sig["fpi"]["total_cr"] == -300.0 and sig["fpi"]["bought"][0]["sector"] == "Power" and sig["fpi"]["sold"][0]["sector"] == "Realty"


def test_a_report_read_before_the_window_is_not_news():
    sig = signals.collect(_con(), datetime(2026, 10, 25, 7, 0))
    assert "fpi" not in sig and not sig["insiders"]["buys"] and not sig["ratings"]


def test_the_section_is_marked_the_owners():
    html = render._signals_section(signals.collect(_con(), NOW))
    assert html.startswith('<div data-zp-owner="1"') and "Promoters bought Rs 4.00 cr (2 disclosures)" in html
    assert "CRISIL: Downgraded" in html and "1 Oct to 15 Oct" in html
    assert render._signals_section({}) == ""
