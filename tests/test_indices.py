"""Index names, parsing, the derived TRI, and the parquet directory's hygiene.

The first two exist because an exact, case-sensitive name filter silently
dropped Nifty Midcap 150 and Smallcap 250 on every day, and everything before
NSE's 2015-11-09 CNX rename, without a single warning.
"""

from __future__ import annotations

import glob
import re
from datetime import date
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

from zen.data import index_tri, indices

HEADER = ("Index Name,Index Date,Open Index Value,High Index Value,"
          "Low Index Value,Closing Index Value,Points Change,Change(%),Volume,"
          "Turnover (Rs. Cr.),P/E,P/B,Div Yield\n")


@pytest.mark.parametrize("raw, want", [
    ("Nifty 500", "Nifty 500"),
    ("NIFTY 500", "Nifty 500"),
    ("CNX 500", "Nifty 500"),
    ("CNX Nifty", "Nifty 50"),
    ("  Nifty   Midcap 150 ", "Nifty Midcap 150"),
    ("NIFTY Midcap 150", "Nifty Midcap 150"),
    ("Nifty Smallcap 250", "Nifty Smallcap 250"),
    ("Nifty Midsmallcap 400", "Nifty MidSmallcap 400"),
    ("Nifty Midcap 100", "NIFTY Midcap 100"),
    ("Nifty Free Float Midcap 100", "NIFTY Midcap 100"),
    ("Nifty Full Midcap 100", None),           # a different series; does not chain
    ("Nifty Full Smallcap 100", None),
    ("Nifty500 Shariah", None),
    ("Nifty50 TR 2x Leverage", None),
])
def test_canonical_name(raw, want):
    assert indices.canonical_name(raw) == want


def test_every_alias_targets_a_kept_index():
    assert set(indices.ALIASES.values()) <= indices.KEEP


def test_parse_keeps_valuation_columns_and_nulls_dashes():
    text = HEADER + (
        "Nifty 500,18-09-2026,22709.7,22860.25,22707.35,22840.55,185.9,.82,"
        "2598378793,105249.45,22.13,3.17,.98\n"
        "Nifty Midcap 150,18-09-2026,22618.05,22915.25,22615.1,22901.85,333.0,"
        "1.48,1078743606,32384.48,28.64,4.29,.59\n"
        "Nifty 50 Futures TR Index,18-09-2026,-,-,-,25080.26,52.43,.21,-,-,-,-,-\n"
        "Nifty500 Ahimsa,18-09-2026,-,-,-,3662.93,35.33,.97,1,1,22.94,3.72,1.22\n")
    df = indices.parse_csv(text, date(2026, 9, 18))
    assert list(df.columns) == indices.COLUMNS
    assert set(df["index_name"]) == {"Nifty 500", "Nifty Midcap 150"}
    n500 = df.set_index("index_name").loc["Nifty 500"]
    assert n500["div_yield"] == pytest.approx(0.98)
    assert n500["pe"] == pytest.approx(22.13)
    assert n500["close"] == pytest.approx(22840.55)
    assert df["date"].iloc[0] == date(2026, 9, 18)
    assert all(df[c].dtype == "float64" for c in indices.NUMERIC)


def test_parse_prefers_the_canonical_spelling_over_an_alias():
    text = HEADER + (
        "CNX 500,05-01-2015,1,1,1,100,0,0,1,1,1,1,1\n"
        "Nifty 500,05-01-2015,1,1,1,200,0,0,1,1,1,1,1\n")
    df = indices.parse_csv(text, date(2015, 1, 5))
    assert len(df) == 1 and df["close"].iloc[0] == 200


def test_parse_maps_cnx_era_rows():
    text = HEADER + ("CNX 500,05-01-2015,6000,6010,5990,6005,5,.08,1,1,20,3,1.3\n"
                     "- ,05-01-2015,-,-,-,-,-,-,-,-,-,-,-\n")
    df = indices.parse_csv(text, date(2015, 1, 5))
    assert df["index_name"].tolist() == ["Nifty 500"]


def test_migrates_an_old_table_in_place():
    con = duckdb.connect()
    con.execute("""CREATE TABLE indices (date DATE NOT NULL, index_name VARCHAR
        NOT NULL, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
        points_change DOUBLE, pct_change DOUBLE, volume DOUBLE, turnover DOUBLE,
        PRIMARY KEY (date, index_name))""")
    indices.ensure_schema(con)
    cols = [r[0] for r in con.execute("DESCRIBE indices").fetchall()]
    assert cols == indices.COLUMNS


def test_tri_flat_price_accrues_the_yield():
    n = 253
    lv = pd.DataFrame({"date": pd.bdate_range("2019-01-01", periods=n).date,
                       "close": 100.0, "div_yield": 2.52})
    t = index_tri.derive(lv)
    assert t["tri"].iloc[0] == 100.0
    # 252 accruals of 2.52%/252, compounded.
    assert t["tri"].iloc[-1] == pytest.approx(100 * (1 + 0.0252 / 252) ** 252)


def test_tri_uses_yesterdays_yield_and_tracks_price():
    lv = pd.DataFrame({"date": pd.bdate_range("2019-01-01", periods=3).date,
                       "close": [100.0, 110.0, 99.0],
                       "div_yield": [0.0, 25.2, 0.0]})
    t = index_tri.derive(lv)
    # Session 1 uses session 0's yield (0): pure price move.
    assert t["tri"].iloc[1] == pytest.approx(110.0)
    # Session 2 uses session 1's yield (25.2% -> 0.1% a session).
    assert t["tri"].iloc[2] == pytest.approx(110.0 * (99 / 110 + 0.001))


def test_tri_fills_short_yield_gaps_only():
    y = [1.0] + [np.nan] * 7
    lv = pd.DataFrame({"date": pd.bdate_range("2019-01-01", periods=8).date,
                       "close": 100.0, "div_yield": y})
    t = index_tri.derive(lv)
    assert t["yield_filled"].sum() == index_tri.MAX_FILL
    assert (t["yield_used"].iloc[1 + index_tri.MAX_FILL:] == 0).all()


def test_tri_starts_after_a_long_gap():
    d = [date(2016, 7, 7)] + list(pd.bdate_range("2021-01-01", periods=5).date)
    lv = pd.DataFrame({"date": d, "close": [50.0, 100, 100, 100, 100, 100],
                       "div_yield": 1.0})
    t = index_tri.derive(lv)
    assert t["date"].iloc[0] == date(2021, 1, 1)
    assert t["tri"].iloc[0] == 100.0


def test_index_glob_holds_only_year_files():
    """rebuild_from_parquet and the parity test read data/indices/*.parquet.

    A derived or bookkeeping parquet at that level would be swept into the
    table -- the same failure the financials glob had. Derived series belong
    in data/indices/derived/.
    """
    files = glob.glob(str(indices.PARQUET_DIR / "*.parquet"))
    if not files:
        pytest.skip("no index parquet")
    bad = [Path(f).name for f in files if not re.fullmatch(r"\d{4}\.parquet", Path(f).name)]
    assert not bad, f"non-year parquet in the indices glob: {bad}"
    assert index_tri.OUT.parent != indices.PARQUET_DIR
