"""NSE's index constituent lists (zen.data.index_members)."""

from datetime import datetime

import duckdb
import pytest

from zen.data import index_members as im

CSV = ("﻿Company Name,Industry,Symbol,Series,ISIN Code\n"
       "Aaa Ltd.,Financial Services,AAA,EQ,INE000A01011\n"
       "Bbb Ltd.,Information Technology,BBB,EQ,INE000B01012\n")
T1, T2 = datetime(2026, 10, 26, 21, 30), datetime(2026, 10, 27, 21, 30)


def test_a_list_is_read_with_its_index_and_the_time_it_was_read():
    df = im.parse(CSV, "Nifty 50", T1)
    assert list(df["symbol"]) == ["AAA", "BBB"] and set(df["index_name"]) == {"Nifty 50"}
    assert df["company"].iloc[0] == "Aaa Ltd." and df["isin"].iloc[1] == "INE000B01012"
    assert (df["observed_dt"] == T1).all()
    with pytest.raises(ValueError):
        im.parse("Symbol,Weight\nAAA,1\n", "Nifty 50", T1)


def test_a_list_is_stored_again_only_when_it_changes(tmp_path):
    assert im.write([im.parse(CSV, "Nifty 50", T1)], tmp_path) == 1
    assert im.write([im.parse(CSV, "Nifty 50", T2)], tmp_path) == 0           # the same members: nothing new
    changed = CSV.replace("Bbb Ltd.,Information Technology,BBB", "Ccc Ltd.,Healthcare,CCC")
    assert im.write([im.parse(changed, "Nifty 50", T2)], tmp_path) == 1
    df = im.stored(tmp_path)
    assert len(df) == 4 and im.latest(df) == {"Nifty 50": {"AAA", "CCC"}}


def test_rebuild_loads_every_stored_version(tmp_path, monkeypatch):
    im.write([im.parse(CSV, "Nifty 50", T1), im.parse(CSV, "Nifty IT", T1)], tmp_path)
    monkeypatch.setattr(im, "DIR", tmp_path)
    con = duckdb.connect()
    assert im.rebuild(con) == 4
    assert con.execute("SELECT count(DISTINCT index_name) FROM index_members").fetchone()[0] == 2
