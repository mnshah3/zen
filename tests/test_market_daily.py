"""zen.data.market_daily: delivery positions, bulk and block deals, FII/DII flows and surveillance,
read the way NSE serves them (the samples are lines of the 16-19 October 2026 files)."""

from __future__ import annotations

from datetime import date, datetime

import duckdb
import pandas as pd
import pytest

from zen.data import market_daily as md

MTO = """Security Wise Delivery Position - Compulsory Rolling Settlement
10,MTO,16102026,2213873385,0003417
Trade Date <16-OCT-2026>,Settlement Type <N>
Record Type,Sr No,Name of Security,Quantity Traded,Deliverable Quantity(gross across client level),% of Deliverable Quantity to Traded Quantity
20,1,1003IIFL29,NC,115,115,100.00
20,1326,HAVELLS,EQ,723675,329647,45.55
20,9,SOMEBE,BE,1000,1000,100.00
"""


def test_an_mto_file_gives_each_equity_line_with_its_trade_date():
    df = md.parse_mto(MTO)
    assert list(df["symbol"]) == ["HAVELLS", "SOMEBE"]                  # bonds and other series left out
    h = df.iloc[0]
    assert h["date"] == date(2026, 10, 16) and h["traded_qty"] == 723675 and h["deliv_qty"] == 329647 and h["deliv_pct"] == 45.55
    with pytest.raises(ValueError):
        md.parse_mto("no header\n20,1,X,EQ,1,1,100")


def test_delivery_is_stored_by_month_and_takes_volume_from_prices(tmp_path, monkeypatch):
    monkeypatch.setattr(md, "DELIVERY_DIR", tmp_path / "delivery")
    for d in ("DEALS_DIR", "FLOWS_DIR", "SURV_DIR"):
        monkeypatch.setattr(md, d, tmp_path / d)
    md.write_delivery(md.parse_mto(MTO))
    md.write_delivery(md.parse_mto(MTO))                                 # read twice: stored once
    stored = pd.read_parquet(tmp_path / "delivery" / "2026" / "2026-10.parquet")
    assert list(stored.columns) == md.DELIVERY_STORED and len(stored) == 2
    con = duckdb.connect()
    con.execute("CREATE TABLE prices (date DATE, symbol VARCHAR, series VARCHAR, volume BIGINT)")
    con.execute("INSERT INTO prices VALUES ('2026-10-16', 'HAVELLS', 'EQ', 723675)")
    assert md.rebuild(con) == 2
    row = con.execute("SELECT traded_qty, deliv_qty, deliv_pct FROM delivery WHERE symbol = 'HAVELLS'").fetchone()
    assert row == (723675, 329647, 45.55)
    assert md.stored_delivery_days(tmp_path / "delivery") == {date(2026, 10, 16)}


def test_deals_flows_and_surveillance_are_read_as_served():
    now = datetime(2026, 10, 19, 19, 45)
    bulk = ("Date,Symbol,Security Name,Client Name,Buy/Sell,Quantity Traded,Trade Price / Wght. Avg. Price,Remarks\n"
            "16-OCT-2026,AROGRANITE,Aro Granite Industries Li,ORION STOCKS LTD,BUY,\"70,889\",24.91,-\n")
    d = md.parse_deals(bulk, "bulk", now).iloc[0]
    assert d["date"] == date(2026, 10, 16) and d["qty"] == 70889 and d["price"] == 24.91 and d["side"] == "BUY" and pd.isna(d["remarks"])
    none = "Date,Symbol,Security Name,Client Name,Buy/Sell,Quantity Traded,Trade Price / Wght. Avg. Price\nNO RECORDS,,,,,,\n"
    assert md.parse_deals(none, "block", now).empty
    f = md.parse_fii_dii([{"buyValue": "17201.8", "category": "DII", "date": "16-Oct-2026", "netValue": "-3661.28", "sellValue": "20863.08"}], now)
    assert f.iloc[0]["net_cr"] == -3661.28 and f.iloc[0]["date"] == date(2026, 10, 16)
    asm = md.parse_asm({"longterm": {"data": [{"symbol": "A2ZINFRA", "asmSurvIndicator": "Stage I", "survCode": "LTASM - I (13)"}]},
                        "shortterm": {"data": []}}, now)
    gsm = md.parse_gsm([{"symbol": "AGSTRA", "gsmStage": "LVIII", "survDesc": "ASM IBC Stage I and GSM Stage 0"}], now)
    assert list(asm["list"]) == ["ASM long term"] and asm.iloc[0]["stage"] == "Stage I" and gsm.iloc[0]["list"] == "GSM"


def test_yearly_files_keep_one_copy_of_a_row_read_twice(tmp_path):
    now = datetime(2026, 10, 19, 19, 45)
    f = md.parse_fii_dii([{"buyValue": "1", "category": "DII", "date": "16-Oct-2026", "netValue": "1", "sellValue": "0"}], now)
    md.write_yearly(f, tmp_path, ["date", "category"], "date", md.FLOW_COLUMNS)
    md.write_yearly(f.assign(observed_dt=pd.Timestamp(2026, 10, 19, 20, 0)), tmp_path, ["date", "category"], "date", md.FLOW_COLUMNS)
    out = pd.read_parquet(tmp_path / "2026.parquet")
    assert len(out) == 1 and out.iloc[0]["observed_dt"] == pd.Timestamp(2026, 10, 19, 20, 0)
