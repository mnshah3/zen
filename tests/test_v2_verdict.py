"""jobs/v2_verdict.py: A1's adoption, A2's five rules and safeguard 2.

Synthetic inputs whose answers are known, then read-only checks of the written
verdict and the v2 runs' measurement files against their sources, when they
exist. Nothing is simulated and nothing is written outside pytest's folders.
"""

from __future__ import annotations

import json
import re
from itertools import combinations
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from jobs import baseline_report as br
from jobs import libcheck_v1 as lc
from jobs import v2_verdict as vv

ROOT = Path(__file__).resolve().parents[1]
BACKTEST = ROOT / "data" / "backtest"
VERDICT = BACKTEST / "v2_verdict.json"


# ---------------------------------------------------------------- A1
def test_a1_holds_needs_all_three_and_is_strict_only_on_the_entry_price():
    assert vv.a1_holds(1.04, 1.03, 0.20, 0.20, -0.30, -0.30) == (True, True, True)
    assert vv.a1_pick((True, True, True)) == "b"
    assert vv.a1_holds(1.03, 1.03, 0.20, 0.21, -0.30, -0.29)[0] is False   # equal is not lower
    assert vv.a1_holds(1.04, 1.03, 0.20, 0.19, -0.30, -0.29) == (True, False, True)
    assert vv.a1_holds(1.04, 1.03, 0.20, 0.21, -0.30, -0.31) == (True, True, False)
    for h in [(False, True, True), (True, False, True), (True, True, False)]:
        assert vv.a1_pick(h) == "a"
    assert vv.a1_holds(float("nan"), 1.0, 0.2, 0.3, -0.3, -0.2)[0] is False  # no positions


# ---------------------------------------------------------------- A2
def _v(**kw):
    base = dict(max_drawdown=-0.30, sortino=1.9, calmar=0.9, iima_alpha_annual=0.05,
                iima_alpha_t=1.2, turnover_annual_incl_initial=1.9,
                capacity_p95_share_of_turnover=0.011)
    base.update(kw)
    return base


MONTHS = {"n": 82, "first": "2019-03", "last": "2025-12"}


def test_a2_rules_at_their_boundaries():
    v1, bars = _v(), {"max_drawdown": -0.30 + 0.05}
    # exactly 5 points, equal alpha, t, turnover and capacity pass; Sortino and Calmar must rise
    edge = _v(max_drawdown=-0.30 + 0.05, sortino=1.91, calmar=0.91)
    assert all(r["passes"] for r in vv.a2_rules(v1, edge, bars, MONTHS, MONTHS))
    cases = {1: dict(max_drawdown=-0.2501), 2: dict(sortino=1.9), 3: dict(iima_alpha_t=1.19),
             4: dict(turnover_annual_incl_initial=1.9001),
             5: dict(capacity_p95_share_of_turnover=0.0111)}
    for rule, change in cases.items():
        rules = vv.a2_rules(v1, {**edge, **change}, bars, MONTHS, MONTHS)
        assert [r["rule"] for r in rules if not r["passes"]] == [rule], rule
    r2 = vv.a2_rules(v1, {**edge, "calmar": 0.89}, bars, MONTHS, MONTHS)[1]
    assert r2["parts"]["sortino"]["passes"] and not r2["parts"]["calmar"]["passes"]
    assert not r2["passes"]
    rule1 = vv.a2_rules(v1, edge, bars, MONTHS, MONTHS)[0]["parts"]["max_drawdown"]
    assert rule1["improvement_points"] == pytest.approx(5.0)


def test_a2_rules_refuse_other_months_and_a_wrong_bar():
    with pytest.raises(AssertionError, match="same months"):
        vv.a2_rules(_v(), _v(), {"max_drawdown": -0.25}, MONTHS, {**MONTHS, "n": 81})
    with pytest.raises(AssertionError, match="rule 1's bar"):
        vv.a2_rules(_v(), _v(), {"max_drawdown": -0.26}, MONTHS, MONTHS)


# ---------------------------------------------------------------- CSCV
def test_blocks_are_contiguous_and_cover_every_return():
    B = vv.blocks(1872)
    assert len(B) == 10 and [len(b) for b in B] == [188, 188] + [187] * 8
    assert np.array_equal(np.concatenate(B), np.arange(1872))
    with pytest.raises(ValueError):
        vv.blocks(9)


def test_growth_and_drawdown_of_a_return_sequence():
    assert vv.growth(np.array([0.1, -0.1])) == pytest.approx(-0.01)
    assert vv.max_drawdown(np.array([-0.1, 0.05])) == pytest.approx(-0.1)   # the start is a peak
    assert vv.max_drawdown(np.array([0.1, -0.1, 0.2, -0.5])) == pytest.approx(
        1.1 * 0.9 * 1.2 * 0.5 / (1.1 * 0.9 * 1.2) - 1)
    assert vv.max_drawdown(np.array([0.1, 0.1])) == 0.0


def _positions(entry: float, k: int = 10) -> pd.DataFrame:
    return pd.DataFrame({"block": np.repeat(np.arange(k), 2), "ratio": entry, "V": 1.0})


def _block_returns(per_block: list[float], size: int = 20) -> np.ndarray:
    return np.repeat(np.asarray(per_block, float), size)


def test_cscv_has_252_splits_and_no_overfitting_when_one_variant_wins_everywhere():
    ra, rb = _block_returns([-0.001] * 10), _block_returns([0.001] * 10)
    s, df = vv.cscv(ra, rb, _positions(1.01), _positions(0.99))
    assert s["splits"] == len(df) == 252 == len(list(combinations(range(10), 5)))
    assert s["picked_b_on_training"] == 252 and s["pbo"] == 0.0 and s["worse_splits"] == 0
    parts = (df["train"] + "-" + df["test"]).str.split("-")
    assert set(df["train"].str.split("-").map(len)) == {5}
    assert parts.map(sorted).map("".join).eq("0123456789").all()
    s, _ = vv.cscv(rb, ra, _positions(0.99), _positions(1.01))          # now (a) wins everywhere
    assert s["picked_b_on_training"] == 0 and s["pbo"] == 0.0


def test_cscv_counts_a_pick_the_test_half_reverses():
    """(b) cheaper everywhere, rising in blocks 0-4 and falling in 5-9; (a) flat. Only the
    training half {0..4} picks (b), and its test half {5..9} picks (a); only {5..9} picks
    (a) with a test half, {0..4}, that picks (b). Two reversals in 252."""
    ra = _block_returns([0.0] * 10)
    rb = _block_returns([0.001] * 5 + [-0.001] * 5)
    s, df = vv.cscv(ra, rb, _positions(1.01), _positions(0.99))
    assert s["picked_b_on_training"] == 1 and s["worse_splits"] == 2
    assert s["pbo"] == pytest.approx(2 / 252)
    assert set(df.loc[df["worse"], "train"]) == {"0-1-2-3-4", "5-6-7-8-9"}
    assert s["pbo_when_b_picked"] == 1.0


def test_position_blocks_use_the_return_ending_at_the_decision_close():
    days = pd.bdate_range("2020-01-01", periods=20)
    t = [days[0] + lc.OPEN_AT] + [d + lc.CLOSE_AT for d in days[:-1]] + [days[-1] + lc.OPEN_AT]
    clock = pd.DataFrame({"mark": ["open"] + ["close"] * 19 + ["open"], "strategy": 1.0},
                         index=pd.DatetimeIndex(t))
    a1 = {"positions": [{"D": str(days[0].date()), "ratio": 1.0, "V": 1.0},
                        {"D": str(days[5].date()), "ratio": 1.1, "V": 2.0},
                        {"D": str(days[9].date()), "ratio": None, "V": 1.0}]}
    pos = vv.position_blocks(clock, a1)                 # 20 returns: blocks of two
    assert pos["return_index"].tolist() == [0, 5] and pos["block"].tolist() == [0, 2]
    with pytest.raises(ValueError, match="no close mark"):
        vv.position_blocks(clock, {"positions": [{"D": "2021-06-01", "ratio": 1.0, "V": 1.0}]})


# ---------------------------------------------------------------- windows
def test_windows_start_at_every_mark_and_end_three_years_later():
    days = pd.bdate_range("2019-01-01", "2022-03-31")
    idx = pd.DatetimeIndex([d + lc.CLOSE_AT for d in days])
    v2 = pd.Series(np.linspace(100, 200, len(idx)), index=idx)
    v1 = v2.copy()
    v1.iloc[10] = v1.iloc[9] * 0.8                        # one dip of 20% in v1
    w = vv.windows(v2, v1)
    last = days[-1]
    assert len(w) == int((days + pd.DateOffset(years=3) <= last).sum())
    r = w.iloc[0]
    j = int(days.searchsorted(days[0] + pd.DateOffset(years=3), side="right")) - 1
    assert r["end"] == idx[j]
    yrs = lc.years_between(idx[0], idx[j])
    assert r["v2_cagr"] == pytest.approx((v2.iloc[j] / v2.iloc[0]) ** (1 / yrs) - 1)
    assert r["v2_max_drawdown"] == 0.0
    assert r["v1_max_drawdown"] == pytest.approx(-0.2)
    s = vv.windows_summary(w)
    assert s["windows"] == len(w) and s["share_v2_drawdown_shallower"] > 0
    with pytest.raises(AssertionError, match="same clock"):
        vv.windows(v2, v1.iloc[1:])


def test_factor_verdict_is_a_tie_when_the_no_overnight_reading_disagrees():
    assert vv._verdict_sign(0.01, 0.02) == "beat"
    assert vv._verdict_sign(-0.01, None) == "did not beat"
    assert vv._verdict_sign(0.001, -0.001) == "tie"


# ---------------------------------------------------------------- the written verdict
def _walk(node, path=""):
    if isinstance(node, dict):
        if {"value", "source"} <= set(node) and isinstance(node["source"], str):
            yield path, node
        for k, v in node.items():
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk(v, f"{path}[{i}]")


@pytest.mark.skipif(not VERDICT.exists(), reason="data/backtest/v2_verdict.json not written yet")
def test_every_figure_of_the_verdict_is_the_number_its_source_holds():
    rep = json.loads(VERDICT.read_text(encoding="utf-8"))
    n = 0
    for where, fig in _walk(rep):
        m = re.fullmatch(r"(data/backtest/[^:]+\.json): (\S+)", fig["source"])
        if not m:
            continue
        doc = json.loads((ROOT / m.group(1)).read_text(encoding="utf-8"))
        assert br.pick(doc, m.group(2)) == fig["value"], where
        n += 1
    assert n > 100
    for f, sha in rep["sources"].items():                 # the verdict is not stale
        assert br.sha256(ROOT / f) == sha, f


@pytest.mark.skipif(not VERDICT.exists(), reason="data/backtest/v2_verdict.json not written yet")
def test_the_verdict_follows_from_its_rules():
    rep = json.loads(VERDICT.read_text(encoding="utf-8"))
    a1, a2 = rep["a1_adoption"], rep["a2_verdict"]
    assert a1["adopted"] == ("b" if all(r["holds"] for r in a1["rules"]) else "a")
    assert a2["variant"] == a1["adopted"]
    assert a2["all_pass"] == all(r["passes"] for r in a2["rules"])
    for r in a2["rules"]:
        assert r["passes"] == all(p["passes"] for p in r["parts"].values())
    s = rep["safeguard_2"]
    assert s["cscv_a1_choice"]["splits"] == 252
    assert s["cscv_a1_choice"]["whole_period_pick"] == a1["adopted"]
    w = pd.read_csv(ROOT / s["windows_3y"]["file"])
    assert len(w) == s["windows_3y"]["windows"]
    splits = pd.read_csv(ROOT / s["cscv_a1_choice"]["splits_file"])
    assert splits["worse"].mean() == pytest.approx(s["cscv_a1_choice"]["pbo"])
    md = (BACKTEST / "v2_verdict.md").read_text(encoding="utf-8")
    assert a2["verdict"][1:] in md


@pytest.mark.parametrize("run", ["v2a", "v2b"])
def test_real_v2_measurements_match_their_sources(run):
    """Read-only: each v2 run's report quotes its jobs' numbers, carries no bar for a later
    challenger, and its monkey test ran on the v2 engine reproducing the run to 1e-9."""
    d = BACKTEST / run
    if not (d / "baseline_report.json").exists():
        pytest.skip(f"{run}: the measurement jobs have not been run")
    rep = json.loads((d / "baseline_report.json").read_text(encoding="utf-8"))
    assert rep["engine"] == "v2"
    for name, fig in rep["a2_acceptance_quantities"].items():
        file, key = fig["source"].split(": ")
        assert br.pick(json.loads((d / file).read_text()), key) == fig["value"], name
        assert "bar_for_v2" not in fig
    ver = json.loads((d / "verify.json").read_text())
    assert ver["monkey_test"]["engine"].startswith("v2")
    assert ver["monkey_test"]["replay_worst_rel"] <= 1e-9
    assert ver["monkey_test"]["persistent"]["draws"] == ver["monkey_test"]["fresh"]["draws"] == 500
    assert ver["capacity_all_trades"]["check_against_ranks"]["worst_rel"] <= 1e-9
