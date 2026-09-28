"""Synthetic tests of jobs/compare_runs.py, the two-run comparator.

Each test writes two small run folders in the agreed REQUIRED OUTPUTS format
by hand, changes one thing in the second, and checks that the comparator finds
exactly that, in the right class. No archive and no real run is read.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from jobs import compare_runs as cr

NAV_COLS = cr.REQUIRED["nav.csv"]
MEASURES = cr.MEASURES
GROUPS = cr.GROUPS


def _write_run(root: Path, *, ew_start: float = 1.0, alias: bool = False) -> Path:
    """A two-date, three-stock run folder in the agreed formats."""
    root.mkdir(parents=True, exist_ok=True)
    Ds = ["2019-02-15", "2019-06-03"]
    syms = ["AAA", "BBB", "CCC"]
    pd.DataFrame([(D, s) for D in Ds for s in syms], columns=["D", "symbol"]).to_csv(
        root / "universe.csv", index=False)
    rows = []
    for D in Ds:
        for i, s in enumerate(syms):
            r = {"D": D, "symbol": s}
            for j, m in enumerate(MEASURES):
                r[m] = None if m == "roce" else 0.1 * (i + 1) + 0.01 * j
                r[(f"{m}_pct" if alias else f"pct_{m}")] = None if m == "roce" else (i + 1) / 3
            for g in GROUPS:
                r[(g if alias else f"grp_{g}")] = (i + 1) / 3
            r["composite"] = (i + 1) / 3
            r["rank"] = 3 - i
            r["ticker"] = s
            rows.append(r)
    pd.DataFrame(rows).to_csv(root / "ranks.csv", index=False)
    pd.DataFrame({"D": Ds, "trend_c1": ["True", "False"], "trend_c2": ["False", "False"],
                  "trend_c3": ["False", "False"], "cash_fraction": [0.2, 0.0],
                  "n_universe": [3, 3], "n_excluded_hard_filters": [1, 0],
                  "n_excluded_no_balance_sheet": [0, 0], "n_ranked_on_roce": [0, 0],
                  "n_held": [2, 2], "n_held_unlabelled_sector": [0, 0]}
                 ).to_csv(root / "decisions.csv", index=False)
    pd.DataFrame([
        ("2019-02-15", "CCC", "CCC", 1, "IT", 0.02, 0.4, "new", ""),
        ("2019-02-15", "BBB", "BBB", 2, "Power", 0.03, 0.4, "new", ""),
        ("2019-06-03", "CCC", "CCC", 1, "IT", 0.02, 0.5, "kept", ""),
        ("2019-06-03", "AAA", "AAA", 3, "Chemicals", 0.025, 0.5, "new", ""),
        ("2019-06-03", "BBB", "BBB", 30, "Power", None, 0.0, "sold", "outside_band"),
    ], columns=cr.REQUIRED["holdings.csv"][:2] + ["ticker", "rank", "sector", "sigma",
                                                  "target_weight", "status", "sold_reason"]
    ).to_csv(root / "holdings.csv", index=False)
    pd.DataFrame([
        ("2019-02-15", "CCC", "CCC", "buy", 10.0, 100.0, 1000.0, 2.0, "tranche1", "2019-02-15", "2019-02-15"),
        ("2019-02-15", "BBB", "BBB", "buy", 5.0, 200.0, 1000.0, 2.0, "tranche1", "2019-02-15", "2019-02-15"),
        ("2019-03-18", "CCC", "CCC", "buy", 9.0, 111.0, 999.0, 1.998, "tranche2", "2019-03-18", "2019-02-15"),
        ("2019-03-18", "BBB", "BBB", "buy", 0.0, None, 0.0, 0.0, "cancelled", "2019-03-18", "2019-02-15"),
        ("2019-06-03", "BBB", "BBB", "sell", 5.0, 210.0, 1050.0, 2.1, "rebalance", "2019-06-03", "2019-06-03"),
    ], columns=cr.REQUIRED["fills.csv"]).to_csv(root / "fills.csv", index=False)
    nav = pd.DataFrame({"date": ["2019-02-15", "2019-02-15", "2019-02-18", "2019-06-03"],
                        "mark": ["open", "close", "close", "open"]})
    path = [1.0, 0.99, 1.01, 1.05]
    for c in NAV_COLS[2:]:
        nav[c] = path
    nav["strategy"] = [500000.0 * x for x in path]
    nav["universe_ew"] = [ew_start * x for x in path]
    nav.to_csv(root / "nav.csv", index=False)
    pos = [{"D": "2019-02-15", "symbol": "CCC", "V": 3000.0, "close_before_D": 99.0,
            "avg_price": 105.2, "ratio": 105.2 / 99.0, "tranches_filled": 2},
           {"D": "2019-02-15", "symbol": "BBB", "V": 3000.0, "close_before_D": 198.0,
            "avg_price": 200.0, "ratio": 200.0 / 198.0, "tranches_filled": 1}]
    if alias:
        pos = [{"D": p["D"], "symbol": p["symbol"], "V": p["V"],
                "ref_close_adjusted": p["close_before_D"],
                "avg_fill_price_adjusted": p["avg_price"], "relative_price": p["ratio"],
                "tranches_filled": p["tranches_filled"]} for p in pos]
        a1 = {"v_weighted_relative_price": 1.04, "n_new_positions": 2, "n_with_a_fill": 2,
              "n_with_no_fill": 0, "positions": pos}
    else:
        a1 = {"v_weighted_ratio": 1.04, "n_positions": 2, "n_with_fills": 2,
              "n_without_fills": 0, "positions": pos}
    (root / "a1.json").write_text(json.dumps(a1))
    return root


def _cmp(a: Path, b: Path, **kw) -> tuple[pd.DataFrame, dict]:
    c = cr.Comparison(a, b, **kw).run()
    return c.frame(), c.summary()


def _edit(path: Path, fn) -> None:
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    fn(df)
    df.to_csv(path, index=False)


@pytest.fixture
def runs(tmp_path):
    return _write_run(tmp_path / "a"), _write_run(tmp_path / "b")


def test_identical_runs_have_no_difference(runs):
    d, s = _cmp(*runs)
    assert d.empty and s["identical_within_tolerance"] and s["n_substantive"] == 0
    assert s["files"]["ranks.csv"]["rows_joined"] == 6
    assert s["files"]["fills.csv"]["rows_joined"] == 5


def test_a_rupee_benchmark_and_an_index_at_one_compare_equal(tmp_path):
    a = _write_run(tmp_path / "a", ew_start=500000.0)
    b = _write_run(tmp_path / "b", ew_start=1.0)
    c = cr.Comparison(a, b).run()
    assert c.frame().empty
    assert any("universe_ew" in n and "starting values differ" in n for n in c.notes)


def test_nav_tolerance_is_relative_and_levels_are_masked(runs):
    a, b = runs
    _edit(b / "nav.csv", lambda df: df.__setitem__(
        "nifty500", [str(float(x) * (1 + 1e-12)) for x in df["nifty500"]]))
    d, _ = _cmp(a, b)
    assert d.empty                                    # 1e-12 relative is inside 1e-9
    _edit(b / "nav.csv", lambda df: df.__setitem__(
        "strategy", [df["strategy"][0]] + [str(float(x) * (1 + 1e-6)) for x in df["strategy"][1:]]))
    d, s = _cmp(a, b)
    nav = d[d["file"] == "nav.csv"]
    assert list(nav["column"].unique()) == ["strategy"] and len(nav) == 3
    assert (nav["class"] == "value").all()
    assert nav["rel_diff"].between(0.9e-6, 1.1e-6).all()
    assert (nav["a"] == "(masked)").all() and (nav["b"] == "(masked)").all()
    assert s["files"]["nav.csv"]["first_divergence"]["strategy"] == "2019-02-15|close"
    d2, _ = _cmp(a, b, show_levels=True)
    assert (d2[d2["file"] == "nav.csv"]["a"] != "(masked)").all()


def test_other_spellings_of_ranks_and_a1_are_matched(tmp_path):
    a = _write_run(tmp_path / "a")
    b = _write_run(tmp_path / "b", alias=True)
    c = cr.Comparison(a, b).run()
    assert c.frame().empty
    assert any("margin_pct -> pct_margin" in n for n in c.notes)
    assert "pct_margin" in c.stats["ranks.csv"]["columns_compared"]
    assert "grp_quality" in c.stats["ranks.csv"]["columns_compared"]
    assert "ratio" in c.stats["a1.json:positions"]["columns_compared"]


def test_a_measure_difference_and_a_missing_universe_row_are_found(runs):
    a, b = runs

    def f(df):
        i = df.index[(df["D"] == "2019-06-03") & (df["symbol"] == "BBB")][0]
        df.loc[i, "mom_12_1"] = str(float(df.loc[i, "mom_12_1"]) + 1e-3)
        df.loc[i, "rank"] = "1"
    _edit(b / "ranks.csv", f)
    _edit(b / "universe.csv", lambda df: df.drop(df.index[-1], inplace=True))
    d, s = _cmp(a, b)
    r = d[d["file"] == "ranks.csv"]
    assert set(r["column"]) == {"mom_12_1", "rank"}
    assert (r["key"] == "2019-06-03|BBB").all()
    u = d[d["file"] == "universe.csv"]
    assert list(u["class"]) == ["only_in_a"] and list(u["key"]) == ["2019-06-03|CCC"]
    assert s["n_substantive"] == 3


def test_decisions_booleans_compare_by_meaning(runs):
    a, b = runs
    _edit(b / "decisions.csv", lambda df: df.__setitem__("trend_c1", ["true", "True"]))
    d, _ = _cmp(a, b)
    dd = d[d["file"] == "decisions.csv"]
    assert len(dd) == 1 and dd.iloc[0]["key"] == "2019-06-03" and dd.iloc[0]["column"] == "trend_c1"


def test_holdings_label_and_blank_classes(runs):
    a, b = runs

    def f(df):
        i = df.index[df["status"] == "sold"][0]
        df.loc[i, "sold_reason"] = "rank_outside_24"
        df.loc[i, "sigma"] = "0.031"
    _edit(b / "holdings.csv", f)
    d, s = _cmp(a, b)
    h = d[d["file"] == "holdings.csv"].set_index("column")
    assert h.loc["sold_reason", "class"] == "label"
    assert h.loc["sigma", "class"] == "blank_vs_value"
    assert s["files"]["holdings.csv"]["by_class"] == {"label": 1, "blank_vs_value": 1}

    def g(df):
        df.loc[df["status"] == "sold", "sold_reason"] = "left_universe"
    _edit(b / "holdings.csv", g)
    d, _ = _cmp(a, b)
    assert d[(d["file"] == "holdings.csv") & (d["column"] == "sold_reason")]["class"].tolist() == ["value"]


def test_fills_are_paired_by_order_not_by_date(runs):
    a, b = runs

    def f(df):
        i = df.index[df["kind"] == "tranche2"][0]
        df.loc[i, "date"] = "2019-03-19"
        df.loc[i, "price_adjusted"] = "112.0"
        j = df.index[df["kind"] == "cancelled"][0]
        df.loc[j, "value"] = "1000.0"
    _edit(b / "fills.csv", f)
    d, s = _cmp(a, b)
    fl = d[d["file"] == "fills.csv"].set_index("column")
    assert set(fl.index) == {"date", "price_adjusted", "value"}
    assert fl.loc["date", "key"] == "2019-02-15|CCC|tranche2|buy|0"
    assert fl.loc["value", "key"] == "2019-02-15|BBB|cancelled|buy|0"
    assert s["files"]["fills.csv"]["rows_joined"] == 5


def test_a1_headline_and_positions(runs):
    a, b = runs
    j = json.loads((b / "a1.json").read_text())
    j["v_weighted_ratio"] = 1.05
    j["positions"][0]["ratio"] *= 1.01
    (b / "a1.json").write_text(json.dumps(j))
    d, _ = _cmp(a, b)
    x = d[d["file"].str.startswith("a1.json")]
    assert set(zip(x["file"], x["column"])) == {("a1.json:headline", "v_weighted"),
                                                ("a1.json:positions", "ratio")}


def test_a_missing_required_column_is_reported(runs):
    a, b = runs
    _edit(b / "holdings.csv", lambda df: df.drop(columns=["sigma"], inplace=True))
    d, s = _cmp(a, b)
    m = d[d["class"] == "missing_column"]
    assert list(zip(m["file"], m["column"], m["a"], m["b"])) == [
        ("holdings.csv", "sigma", "present", "absent")]
    assert s["n_substantive"] == 1


def test_cli_writes_the_report_and_sets_the_exit_code(runs, tmp_path):
    a, b = runs
    out = tmp_path / "report"
    assert cr.main([str(a), str(b), "--out", str(out)]) == 0
    assert {"report.md", "differences.csv", "summary.json"} <= {p.name for p in out.iterdir()}
    _edit(b / "decisions.csv", lambda df: df.__setitem__("n_universe", ["3", "4"]))
    assert cr.main([str(a), str(b), "--out", str(out), "--label-a", "p", "--label-b", "c"]) == 1
    text = (out / "report.md").read_text(encoding="utf-8")
    assert "# Run comparison: p vs c" in text and "n_universe" in text
    s = json.loads((out / "summary.json").read_text())
    assert s["files"]["decisions.csv"]["by_column"] == {"n_universe": 1}
