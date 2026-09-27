"""Leak test of the v1 ranking on a truncated archive (jobs/leak_v1_all_dates.py).

The job runs all 31 decision dates of the v1 baseline and writes
data/backtest/v1_final/leak_all_dates.json. This file runs the same check at
two of them, one before and one after the in-sample end of February 2023,
and is skipped only when data/zen.duckdb is absent. At each date:

  - the production ranking (engine.build_ranks, as jobs/backtest_v1.py calls
    it) on the full archive and on an archive physically cut to what was
    knowable before D must be identical: universe, every measure, every
    percentile, the composite, the rank, the funnel and the audit trail;
  - two deliberately leaky versions of the same code path, one reading the
    filings broadcast on D and one reading D's own prices, must be caught by
    the same comparison. Both dates were chosen because many filings were
    broadcast on D itself (292 on 15 Nov 2019, 174 on 15 Feb 2024) and they
    change the ranking there, so the first variant has something to leak.
    Over all 31 dates it has something to leak at 25 (leak_all_dates.json).

The tests without the database check the pieces the verdict rests on: that
the comparison is exact, and that the truncation keeps exactly the rows
dated before D in every table and refuses a table it does not know.
"""

from __future__ import annotations

import json
from datetime import datetime

import duckdb
import numpy as np
import pandas as pd
import pytest

from jobs import leak_v1_all_dates as leak
from zen.universe import pit

DATES = ("2019-11-15", "2024-02-15")
needs_db = pytest.mark.skipif(not pit.DB_PATH.exists(), reason="data/zen.duckdb not present")


# ---------------------------------------------------------------- the pieces, no database
def _frame():
    return pd.DataFrame({"symbol": ["A", "B", "C"], "x": [0.1, np.nan, 3.0],
                         "rank": [1, 2, 3], "sector": ["Power", None, "IT"]})


def test_comparison_is_exact():
    a = _frame()
    assert leak.compare_frames(a, a.copy())["identical"]                  # NaN equals NaN
    b = a.copy()
    b.loc[2, "x"] = np.nextafter(3.0, 4.0)                                # one unit in the last place
    r = leak.compare_frames(a, b)
    assert not r["identical"] and set(r["differing_columns"]) == {"x"}
    b = a.copy()
    b.loc[1, "x"] = 0.0                                                   # NaN against a number
    assert not leak.compare_frames(a, b)["identical"]
    b = a.iloc[[1, 0, 2]].reset_index(drop=True)                          # same rows, other order
    r = leak.compare_frames(a, b)
    assert r["same_members"] and not r["same_order"] and not r["identical"]
    b = a.iloc[:2]                                                        # one member fewer
    r = leak.compare_frames(a, b)
    assert not r["identical"] and r["only_first"] == ["C"]
    b = a.assign(rank=a["rank"].astype(float))                            # same values, other type
    assert not leak.compare_frames(a, b)["identical"]
    assert leak.compare_frames(a, b, check_dtype=False)["identical"]


def _mini_archive(path, extra_table: bool = False):
    """Every table the archive has, each with rows just before, at and after D = 2024-06-03."""
    con = duckdb.connect(str(path))
    for t, (col, typ) in {"prices": ("date", "DATE"), "prices_other": ("date", "DATE"),
                          "indices": ("date", "DATE"), "corpactions": ("ex_date", "DATE"),
                          "financials": ("broadcast_dt", "TIMESTAMP"),
                          "announcements": ("an_dt", "TIMESTAMP")}.items():
        con.execute(f"CREATE TABLE {t} ({col} {typ}, tag VARCHAR)")
        if typ == "DATE":
            rows = [("2024-05-31", "before"), ("2024-06-03", "on"), ("2024-06-04", "after")]
        else:
            rows = [("2024-06-02 23:59:59", "before"), ("2024-06-03 00:00:00", "on"),
                    ("2024-06-03 09:15:00", "on"), ("2024-06-04 10:00:00", "after")]
        con.executemany(f"INSERT INTO {t} VALUES (?, ?)", rows)
    if extra_table:
        con.execute("CREATE TABLE shareholding (as_of DATE)")
    con.close()


def test_truncation_keeps_only_rows_dated_before_D(tmp_path):
    src, dest = tmp_path / "src.duckdb", tmp_path / "cut.duckdb"
    _mini_archive(src)
    info = leak.truncated_archive(src, "2024-06-03", dest)
    assert set(info) == set(leak.CUTS)
    assert leak.check_truncation(info, "2024-06-03") == []
    con = duckdb.connect(str(dest), read_only=True)
    try:
        for t in leak.CUTS:
            assert [r[0] for r in con.execute(f"SELECT tag FROM {t}").fetchall()] == ["before"], t
            assert info[t]["rows"] == 1 and info[t]["removed"] >= 2, t
    finally:
        con.close()


def test_truncation_refuses_a_table_it_does_not_cut(tmp_path):
    src = tmp_path / "src.duckdb"
    _mini_archive(src, extra_table=True)
    with pytest.raises(RuntimeError, match="shareholding"):
        leak.truncated_archive(src, "2024-06-03", tmp_path / "cut.duckdb")


def test_leaky_rewrites_move_only_the_matching_bound():
    D = pd.Timestamp("2024-06-03")

    class Rec:
        def execute(self, q, params=None):
            self.last = params
            return self

    f = leak.FilingsOnD(Rec(), D)
    f.execute("SELECT * FROM financials WHERE broadcast_dt < ?", [D.to_pydatetime()])
    assert f._con.last == [datetime(2024, 6, 4)] and f.rewrites == 1
    f.execute("SELECT * FROM announcements WHERE an_dt < ?", [D.to_pydatetime()])
    assert f._con.last == [D.to_pydatetime()] and f.rewrites == 1
    p = leak.PricesThroughD(Rec(), D)
    lo = pd.Timestamp("2023-04-10").date()
    p.execute("SELECT * FROM prices WHERE date >= ? AND date < ?", [lo, D.date()])
    assert p._con.last == [lo, pd.Timestamp("2024-06-04").date()] and p.rewrites == 1


# ---------------------------------------------------------------- the archive, two dates
@pytest.fixture(scope="module")
def reports(tmp_path_factory):
    con = pit.connect(read_only=True)
    try:
        static = pit.StaticLabels.load(con)
        tmp = tmp_path_factory.mktemp("leak_v1")
        return {D: leak.check_date(con, D, static, tmp_dir=tmp) for D in DATES}
    finally:
        con.close()


def test_the_two_dates_straddle_the_in_sample_end():
    decisions = [str(d.date()) for d in leak.decision_dates()]
    assert set(DATES) <= set(decisions)
    assert DATES[0] < "2023-02-15" < DATES[1]


@needs_db
@pytest.mark.parametrize("D", DATES)
def test_ranking_is_identical_on_the_truncated_archive(reports, D):
    rep = reports[D]
    assert rep["universe"]["full"] > 400 and rep["universe"]["full"] == rep["universe"]["truncated"]
    h = rep["honest"]
    assert h["ranks"]["identical"], h["ranks"]
    assert h["funnel"]["identical"], h["funnel"]
    assert h["data_quality"]["identical"], h["data_quality"]
    assert rep["passed"]
    # the truncated copy held nothing dated on or after D, and the cut removed rows
    assert leak.check_truncation(rep["truncated_archive"], D) == []
    for t in ("prices", "prices_other", "financials", "corpactions", "indices"):
        assert rep["truncated_archive"][t]["removed"] > 0, t


@needs_db
@pytest.mark.parametrize("D", DATES)
@pytest.mark.parametrize("variant", sorted(leak.LEAKY))
def test_a_leaky_ranking_is_caught(reports, D, variant):
    v = reports[D]["leaky"][variant]
    assert v["rewrites_full"] >= 1 and v["rewrites_truncated"] >= 1, v
    assert v["leak_changes_ranking"], v       # it read something extra, and it mattered
    assert v["caught"], v                     # and the full-vs-truncated comparison saw it
    assert v["truncated_equals_honest"], v    # on the cut archive there was nothing to leak
    if variant == "filings_on_D":
        assert reports[D]["filings_broadcast_on_D"] > 0


@needs_db
@pytest.mark.parametrize("D", DATES)
def test_the_rebuilt_ranking_is_the_committed_one(reports, D):
    c = reports[D].get("full_archive_reproduces_committed_ranks")
    if c is None:
        pytest.skip("ranks.parquet is not committed (.gitignore); nothing local to compare")
    assert c["identical"], c


# ---------------------------------------------------------------- the job's report
def test_the_committed_report_covers_every_decision_date():
    rep = json.loads((leak.RUN / "leak_all_dates.json").read_text(encoding="utf-8"))
    decisions = [str(d.date()) for d in leak.decision_dates()]
    assert len(decisions) == 31
    assert [r["D"] for r in rep["dates"]] == decisions
    s = rep["summary"]
    assert s["dates"] == s["passed"] == 31 and s["failed"] == []
    assert all(r["honest"]["identical"] for r in rep["dates"])
    assert s["full_archive_reproduces_committed_ranks"]["differ"] == []
    for name, v in s["leaky_variants"].items():
        assert v["dates"] == 31 and v["missed"] == [] and v["no_rewrite"] == [], name
        assert v["truncated_not_equal_honest"] == [], name
        assert v["caught"] == v["leak_changes_ranking"], name
    assert s["leaky_variants"]["prices_through_D"]["caught"] == 31
    assert leak.ok(s)
