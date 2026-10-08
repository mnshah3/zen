"""zen.data.shareholding: NSE shareholding filings and promoter pledges, point in time."""

from datetime import date, datetime

import duckdb
import pandas as pd

from zen.data import shareholding as sh

FILING = {"symbol": "ABC", "isin": "INE000A01010", "name": "ABC Limited", "date": "30-JUN-2026",
          "pr_and_prgrp": "34.66", "public_val": "64.87", "employeeTrusts": "0.47",
          "broadcastDate": "10-JUL-2026 17:58:39", "submissionDate": "10-JUL-2026", "revisedData": "N",
          "recordId": "210812", "xbrl": "https://nsearchives.nseindia.com/x.xml"}


def test_parse_shareholding():
    df = sh.parse_shareholding([FILING, {"symbol": "", "recordId": "1"}, {"symbol": "X", "recordId": "2",
                                                                          "date": "bad"}])
    assert len(df) == 1
    r = df.iloc[0]
    assert r.period_end == date(2026, 6, 30) and r.promoter_pct == 34.66 and r.public_pct == 64.87
    assert r.broadcast_dt == datetime(2026, 7, 10, 17, 58, 39) and r.record_id == "210812"


def test_point_in_time_read_takes_the_latest_filing_known_then():
    con = duckdb.connect(":memory:")
    first = dict(FILING)
    revised = dict(FILING, pr_and_prgrp="35.00", broadcastDate="20-JUL-2026 10:00:00", recordId="210900",
                   revisedData="Y")
    older = dict(FILING, date="31-MAR-2026", pr_and_prgrp="34.64", broadcastDate="17-APR-2026 14:10:56",
                 recordId="200001")
    sh.upsert_shareholding(con, sh.parse_shareholding([first, revised, older]))
    on_15 = sh.asof(con, "ABC", date(2026, 7, 15))
    assert list(on_15.promoter_pct) == [34.66, 34.64]           # the revision was not yet broadcast
    on_31 = sh.asof(con, "ABC", date(2026, 7, 31))
    assert list(on_31.promoter_pct) == [35.00, 34.64]
    assert list(sh.asof(con, "ABC", date(2026, 7, 1)).promoter_pct) == [34.64]
    assert sh.asof(duckdb.connect(":memory:"), "ABC", date(2026, 7, 31)).empty


def _pledge(pledged="28157801", t="08-Oct-2026 16:30:37"):
    return {"comName": "ABC Limited", "shp": "30-Jun-2026", "broadcastDt": t, "percPromoterHolding": "   35.13",
            "percPromoterShares": "     0.00", "percSharesPledged": "1.26", "numSharesPledged": pledged,
            "totPromoterHolding": "789537219", "totIssuedShares": "2247226523"}


def test_pledges_map_names_and_keep_only_changes():
    con = duckdb.connect(":memory:")
    snap = sh.attach_symbols(sh.parse_pledges({"data": [_pledge(), dict(_pledge(), comName="Unknown Co")]}),
                             {"ABC": "ABC Limited"})
    assert list(snap.symbol) == ["ABC"] and snap.iloc[0].promoter_holding_pct == 35.13
    assert sh.upsert_pledges(con, sh.changed_pledges(con, snap)) == 1
    same_next_day = sh.attach_symbols(sh.parse_pledges({"data": [_pledge(t="09-Oct-2026 16:30:37")]}),
                                      {"ABC": "ABC Limited"})
    assert sh.changed_pledges(con, same_next_day).empty           # the feed's refresh time alone is not news
    moved = sh.attach_symbols(sh.parse_pledges({"data": [_pledge(pledged="30000000", t="10-Oct-2026 16:30:00")]}),
                              {"ABC": "ABC Limited"})
    assert sh.upsert_pledges(con, sh.changed_pledges(con, moved)) == 1


def test_parquet_round_trip_and_optional_rebuild(tmp_path):
    df = sh.parse_shareholding([FILING])
    sh.write_shareholding(df, tmp_path / "sh")
    sh.write_shareholding(df, tmp_path / "sh")                    # idempotent
    con = duckdb.connect(":memory:")
    assert sh.rebuild_shareholding(con, tmp_path / "sh") == 1
    assert sh.rebuild_pledges(con, tmp_path / "none") == 0        # no parquet: an empty table, no error
    assert len(pd.read_parquet(tmp_path / "sh" / "2026.parquet")) == 1
