"""The definitions the v3 verdict and its diagnostics compute themselves (v3 spec, section 14),
on small hand-checked inputs. No archive and no run is read."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from jobs import v3_diagnostics as dg
from jobs import v3_verdict as vv


# ---------------------------------------------------------------- 14.1
def test_primary_test_rejects_only_above_1_645_and_only_on_the_fixed_months():
    m = vv.PRIMARY_MONTHS
    assert vv.primary_test(0.08, 1.645, m)["reject_h0"] is False        # "above 1.645"
    assert vv.primary_test(0.08, 1.6451, m)["reject_h0"] is True
    assert vv.primary_test(-0.03, -0.5, m)["reject_h0"] is False
    with pytest.raises(AssertionError):
        vv.primary_test(0.08, 2.0, (69, "2020-03", "2025-11"))


# ---------------------------------------------------------------- 14.2
def _side(**kw) -> dict:
    base = {"max_drawdown": -0.40, "sortino": 1.5, "calmar": 0.6, "alpha_annual": 0.05,
            "alpha_t": 1.1, "months": (70, "2020-03", "2025-12"), "turnover_a2": 1.8,
            "capacity_p95": 0.02}
    return {**base, **kw}


def test_a2_bars_boundaries_are_the_specs():
    v1 = _side()
    # bar 1: at least five points smaller; exactly five points passes
    assert vv.a2_bars(v1, _side(max_drawdown=-0.35))[0]["passes"]
    assert not vv.a2_bars(v1, _side(max_drawdown=-0.3501))[0]["passes"]
    # bar 2: both strictly higher
    assert not vv.a2_bars(v1, _side(sortino=1.5, calmar=0.7))[1]["passes"]
    assert not vv.a2_bars(v1, _side(sortino=1.6, calmar=0.6))[1]["passes"]
    assert vv.a2_bars(v1, _side(sortino=1.6, calmar=0.7))[1]["passes"]
    # bar 3: neither lower; equal passes
    assert vv.a2_bars(v1, _side())[2]["passes"]
    assert not vv.a2_bars(v1, _side(alpha_t=1.09))[2]["passes"]
    assert not vv.a2_bars(v1, _side(alpha_annual=0.0499))[2]["passes"]
    # bars 4 and 5: not higher; equal passes
    assert vv.a2_bars(v1, _side())[3]["passes"] and vv.a2_bars(v1, _side())[4]["passes"]
    assert not vv.a2_bars(v1, _side(turnover_a2=1.81))[3]["passes"]
    assert not vv.a2_bars(v1, _side(capacity_p95=0.0201))[4]["passes"]
    with pytest.raises(AssertionError):
        vv.a2_bars(v1, _side(months=(69, "2020-03", "2025-11")))


def test_turnover_a2_is_the_formula_and_leaves_out_v3s_first_buys():
    trades = pd.DataFrame({
        "date": ["2020-02-17", "2020-02-17", "2021-06-01", "2026-09-17", "2019-11-15", "2021-06-01"],
        "side": ["buy", "buy", "sell", "buy", "buy", "cancel"],
        "value": [100_000.0, 150_000.0, 80_000.0, 70_000.0, 999_999.0, 555.0]})
    nav = pd.Series([500_000.0, 700_000.0, 900_000.0])                    # mean 700,000
    years = 2405 / 365.25
    # a trade before the window and a cancellation are not counted
    assert vv.turnover_a2(trades, nav) == pytest.approx(400_000 / 2 / 700_000 / years)
    assert vv.turnover_a2(trades, nav, initial_buy_value=250_000) == \
        pytest.approx(150_000 / 2 / 700_000 / years)
    fills = pd.DataFrame({"D": ["2020-02-17", "2020-02-17", "2020-06-01", "2020-02-17"],
                          "kind": ["buy", "buy", "buy", "exit"], "event": ["fill", "filled", "fill", "fill"],
                          "value": [100_000.0, 150_000.0, 40_000.0, 9.0]})
    assert vv.initial_build_value(fills) == 250_000.0


# ---------------------------------------------------------------- 14.3
def _clock(values: dict) -> pd.DataFrame:
    """One close mark at each month end, plus an open mark that must be ignored."""
    idx = [pd.Timestamp(m).to_period("M").end_time.normalize() + pd.Timedelta(hours=15, minutes=30)
           for m in values]
    df = pd.DataFrame({"mark": "close", "v": list(values.values())}, index=pd.DatetimeIndex(idx))
    first = pd.DataFrame({"mark": ["open"], "v": [1.0]},
                         index=[idx[0].normalize() + pd.Timedelta(hours=9, minutes=15)])
    return pd.concat([first, df]).sort_index()


def test_windows_start_at_a_month_end_from_feb_2020_and_end_by_aug_2026():
    months = pd.period_range("2020-01", "2026-09", freq="M")
    me = pd.Series(np.arange(1, len(months) + 1, dtype=float), index=months)
    w = vv.window_returns(me, 3)
    assert w.index[0] == pd.Period("2020-05", freq="M")        # Feb 2020 close to May 2020 close
    assert w.index[-1] == pd.Period("2026-08", freq="M")       # nothing ends after August 2026
    assert w.iloc[0] == pytest.approx(5 / 2 - 1)
    assert len(vv.window_returns(me, 24)) == len(pd.period_range("2022-02", "2026-08", freq="M"))
    c = _clock({"2020-02-28": 100.0, "2020-03-31": 110.0})
    assert vv.month_end_closes(c, "v").tolist() == [100.0, 110.0]


def test_window_statistics_are_excess_points_and_strict_beats():
    months = pd.period_range("2020-02", "2021-02", freq="M")           # ten 3-month windows
    bench = pd.Series(100.0, index=months)                              # flat: every return 0
    v3 = pd.Series(100.0 * 1.02 ** np.arange(len(months)), index=months)
    v1 = v3.copy()                                                      # ties: never "beat"
    w = vv.windows({"v3": v3, "v1": v1, "nifty500": bench})["3"]
    assert w["windows"] == 10
    assert w["v3"]["median_excess_points"] == pytest.approx((1.02 ** 3 - 1) * 100)
    assert w["v3"]["share_beat_benchmark"] == 1.0 and w["v3"]["share_beat_v1"] == 0.0
    w12 = vv.windows({"v3": v3, "v1": v1, "nifty500": bench})["12"]
    assert w12["windows"] == 1
    assert w12["v3"]["share_beat_benchmark_by_15_points_a_year"] == 1.0   # 1.02^12 - 1 = 26.8%
    with pytest.raises(AssertionError):
        vv.windows({"v3": v3.iloc[1:], "v1": v1, "nifty500": bench})


def test_multibaggers_counts_doubled_episodes():
    e = pd.DataFrame({"doubled": ["true", "false", "True", "false"]})
    assert vv.multibaggers(e) == {"episodes": 4, "doubled_while_held": 2, "share": 0.5}


# ---------------------------------------------------------------- 14.4
def _panel() -> SimpleNamespace:
    """Three sessions, two stocks. Stock b does not trade on the last session."""
    nan = np.nan
    return SimpleNamespace(
        symbols=pd.Index(["a", "b"]),
        dates=pd.DatetimeIndex(["2020-02-14", "2020-02-17", "2020-06-01"]),
        open_=np.array([[9.0, 19.0], [10.0, 20.0], [12.0, nan]]),
        last=np.array([[9.5, 19.5], [10.5, 21.0], [12.5, 21.0]]),
        traded=np.array([[True, True], [True, True], [True, False]]),
        div_unit=np.array([[0.0, 0.0], [5.0, 0.0], [0.5, 1.0]]),
        col=lambda s: {"a": 0, "b": 1}[s])


def test_forward_return_uses_the_open_or_the_last_close_and_dividends_after_D():
    p = _panel()
    # a: (12 + 0.5) / 10 - 1; the dividend with ex-date D itself (5.0) is not counted
    # b: does not trade at D': its last close before D' (21.0), plus the 1.0 dividend
    assert dg.forward_returns(p, 1, 2).tolist() == pytest.approx([0.25, 22.0 / 20.0 - 1])


def test_ic_summary_is_a_fama_macbeth_t_over_the_first_reviews():
    ics = [0.1, 0.2, 0.3, -0.1, 0.9]                       # the last is left out
    s = dg.ic_summary(ics, n_test=4)
    x = np.array(ics[:4])
    assert s["mean"] == pytest.approx(x.mean())
    assert s["t"] == pytest.approx(x.mean() / (x.std(ddof=1) / 2))
    assert s["share_positive"] == 0.75
    with pytest.raises(ValueError):
        dg.ic_summary([0.1, np.nan, 0.2], n_test=3)
    assert dg.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)


# ---------------------------------------------------------------- 14.5
def test_the_split_is_exact_and_chains():
    a = dg.split_interval(100.0, 132.0, 10.0, 11.0)        # price x1.32 = earnings x1.1 x re-rating x1.2
    assert a["earnings"] == pytest.approx(1.1) and a["rerating"] == pytest.approx(1.2)
    assert a["earnings"] * a["rerating"] == pytest.approx(a["price"])
    assert dg.split_interval(100.0, 120.0, -1.0, 11.0) == {"defined": False}
    c = dg.chain([a, {"defined": False}, a], days=731)
    assert c["undefined_intervals"] == 1
    assert c["earnings_factor"] == pytest.approx(1.21) and c["rerating_factor"] == pytest.approx(1.44)
    assert c["earnings_annual"] == pytest.approx(1.21 ** (365.25 / 731) - 1)
    assert c["share_from_earnings"] == pytest.approx(np.log(1.21) / np.log(1.21 * 1.44))


def test_units_at_is_the_book_at_a_close():
    t = pd.DataFrame({"date": pd.to_datetime(["2020-02-17", "2020-03-02", "2020-03-02", "2020-06-01"]),
                      "symbol": ["a", "a", "b", "b"], "side": ["buy", "sell", "buy", "sell"],
                      "units": [10.0, 4.0, 3.0, 3.0]})
    assert dg.units_at(t, pd.Timestamp("2020-03-31")).to_dict() == {"a": 6.0, "b": 3.0}
    assert dg.units_at(t, pd.Timestamp("2020-06-01")).to_dict() == {"a": 6.0}      # b sold out
