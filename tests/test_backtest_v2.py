"""Strategy v2 (research/strategy/v2-spec.md): every rule and branch on synthetic data.

  sizing        1/sigma weights, the 0.5x-1.5x bounds with pro-rata redistribution,
                fewer than 12 names, a name without a sigma, the trend cash
  trend filter  each of the three conditions, the cash per count, closes before D only,
                on the archive's market sessions only (not the index's special sessions)
  A1            Wilder's RSI and the 50-session average against a hand computation,
                the rule's boundaries, the undefined RSI of a flat stock, EQ/BE closes
                only (a BZ close is not an A1 close)
  hard filters  market cap, P/E, and from February 2023 ROCE, D/E and a missing
                or unusable balance sheet (fails both, counted)
  ranks         v1's scoring when ROCE is off; ROCE replaces margin from February
                2023; percentiles within the v2 universe
  sectors       NSE labels joined by stock id (a renamed company keeps its label);
                an unlabelled company is its own sector
  book          12 names, band of 24, thesis-break, left-universe and band exits
  tranches      three V/3 tranches at D, D+21, D+42; the 5-session cancel; BZ does
                not fill a buy; a pending tranche cancelled and the holding resized
                in full at the next D; cancelled when the position leaves the book or
                is force-sold
  A1 entry      waits for a qualifying close, tranche 1 uses the close before D,
                the scheduled session as the first of the 30, the fallback 30
                sessions after it and its 5-session wait
  A1 metric     V-weighted ratio of the price paid to the EQ/BE close before D
  integration   universe, ranks, panel and both variants on a synthetic archive
  guards        the holdout lock in the CLI

No real return is computed anywhere here; the tests that open data/zen.duckdb
read membership and labels only.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

from zen.portfolio import engine, engine_v2 as v2
from zen.signals import composite
from zen.universe import pit
from zen.universe.identity import Identity, link

DB = Path(__file__).resolve().parents[1] / "data" / "zen.duckdb"
E12 = 1 / 12


# ---------------------------------------------------------------- sizing
def test_weights_proportional_to_inverse_sigma_within_bounds():
    sig = pd.Series(np.linspace(0.018, 0.022, 12), index=[f"S{i}" for i in range(12)])
    w = v2.target_weights(sig, 0.0)
    inv = 1 / sig
    assert w.to_numpy() == pytest.approx((inv / inv.sum()).to_numpy(), rel=1e-12)
    assert w.sum() == pytest.approx(1.0, rel=1e-12)
    # trend cash scales the book, not the proportions
    w2 = v2.target_weights(sig, 0.35)
    assert w2.sum() == pytest.approx(0.65, rel=1e-12)
    assert (w2 / w).to_numpy() == pytest.approx(np.full(12, 0.65), rel=1e-12)


def test_weights_cap_a_quiet_name_and_redistribute_pro_rata():
    # one name four times quieter than eleven equal ones: 1/sigma share 4/15 > 1.5/12
    sig = pd.Series([0.005] + [0.02] * 11, index=list("ABCDEFGHIJKL"))
    w = v2.target_weights(sig, 0.2)
    E = 0.8 / 12
    assert w["A"] == pytest.approx(1.5 * E, rel=1e-12)
    # the other eleven share the rest equally (they were equal before)
    assert w.drop("A").to_numpy() == pytest.approx(np.full(11, (0.8 - 1.5 * E) / 11), rel=1e-12)
    assert w.sum() == pytest.approx(0.8, rel=1e-12)


def test_weights_floor_a_volatile_name_and_take_it_from_the_others():
    sig = pd.Series([0.08] + [0.02] * 11, index=list("ABCDEFGHIJKL"))
    w = v2.target_weights(sig, 0.0)
    assert w["A"] == pytest.approx(0.5 / 12, rel=1e-12)
    assert w.drop("A").to_numpy() == pytest.approx(np.full(11, (1 - 0.5 / 12) / 11), rel=1e-12)


def test_weights_cascade_until_every_weight_is_within_bounds():
    # after capping the quietest, the second one is pushed over the cap in turn
    sig = pd.Series([0.004, 0.0115] + [0.02] * 10, index=list("ABCDEFGHIJKL"))
    raw = (1 / sig) / (1 / sig).sum()
    assert raw["B"] < 1.5 / 12                  # B is inside the band at first
    w = v2.target_weights(sig, 0.0)
    assert w["A"] == pytest.approx(1.5 / 12) and w["B"] == pytest.approx(1.5 / 12)
    assert w.sum() == pytest.approx(1.0, rel=1e-12)
    assert (w >= 0.5 / 12 - 1e-15).all() and (w <= 1.5 / 12 + 1e-15).all()


def test_weights_cap_and_floor_in_the_same_round():
    sig = pd.Series([0.004, 0.09] + [0.02] * 10, index=list("ABCDEFGHIJKL"))
    w = v2.target_weights(sig, 0.0)
    assert w["A"] == pytest.approx(1.5 / 12) and w["B"] == pytest.approx(0.5 / 12)
    # net excess (A's excess less B's shortfall) spread over the ten equally
    assert w.drop(["A", "B"]).to_numpy() == pytest.approx(np.full(10, (1 - 2 / 12) / 10))


def test_weights_with_fewer_than_twelve_names_leave_cash():
    sig = pd.Series([0.01, 0.02, 0.03], index=list("ABC"))
    w = v2.target_weights(sig, 0.2)
    E = 0.8 / 12
    assert w.to_numpy() == pytest.approx(np.full(3, 1.5 * E))      # all at the cap
    assert w.sum() == pytest.approx(4.5 * E)                        # the rest is cash
    assert v2.target_weights(pd.Series(dtype=float), 0.0).empty


def test_weights_name_without_sigma_gets_the_equal_weight_before_scaling():
    sig = pd.Series([np.nan] + list(np.linspace(0.019, 0.021, 11)), index=list("ABCDEFGHIJKL"))
    w = v2.target_weights(sig, 0.0)
    assert w["A"] == pytest.approx(1 / 12, rel=1e-12)                 # 12 names: no rescale
    inv = 1 / sig.drop("A")
    assert w.drop("A").to_numpy() == pytest.approx((inv / inv.sum() * 11 / 12).to_numpy())
    # zero is not a sigma either
    w0 = v2.target_weights(sig.fillna(0.0), 0.0)
    assert w0["A"] == pytest.approx(1 / 12, rel=1e-12)


# ---------------------------------------------------------------- trend filter
def _tri(values, start="2015-01-01") -> pd.Series:
    return pd.Series(np.asarray(values, float), index=pd.bdate_range(start, periods=len(values)))


def test_trend_rising_market_has_no_condition_and_no_cash():
    tri = _tri(np.linspace(100, 200, 600))
    D = tri.index[-1] + pd.offsets.BDay(1)
    s = v2.trend_state(tri, D)
    assert (s["trend_c1"], s["trend_c2"], s["trend_c3"]) == (False, False, False)
    assert s["cash_fraction"] == 0.0


def test_trend_shallow_dip_below_the_average_is_condition_one_only():
    x = list(np.linspace(100, 120, 600)) + [115.0]          # below the 200-close mean, 4% off
    tri = _tri(x)
    s = v2.trend_state(tri, tri.index[-1] + pd.offsets.BDay(1))
    assert (s["trend_c1"], s["trend_c2"], s["trend_c3"]) == (True, False, False)
    assert s["cash_fraction"] == 0.20


def test_trend_deep_recent_fall_is_two_conditions():
    x = list(np.linspace(100, 200, 600)) + list(np.linspace(199, 170, 20))   # 15% off the high
    tri = _tri(x)
    s = v2.trend_state(tri, tri.index[-1] + pd.offsets.BDay(1))
    assert (s["trend_c1"], s["trend_c2"], s["trend_c3"]) == (True, True, False)
    assert s["cash_fraction"] == 0.275


def test_trend_grinding_bear_market_is_all_three():
    x = list(np.linspace(100, 200, 400)) + list(np.linspace(200, 120, 300))
    tri = _tri(x)
    s = v2.trend_state(tri, tri.index[-1] + pd.offsets.BDay(1))
    assert (s["trend_c1"], s["trend_c2"], s["trend_c3"]) == (True, True, True)
    assert s["cash_fraction"] == 0.35


@pytest.mark.parametrize("k,expect", [(76, True), (75, False)])
def test_trend_persistence_needs_60_percent_of_126_sessions(k, expect):
    # a flat 100 history; in the last 126 sessions, k closes at 99 (below any mean
    # that includes a 100 or 101) and the rest at 101, the last one at 101
    last = [99.0] * k + [101.0] * (126 - k)
    tri = _tri([100.0] * 400 + last)
    s = v2.trend_state(tri, tri.index[-1] + pd.offsets.BDay(1))
    assert s["sessions_below_sma_of_126"] == k
    assert s["trend_c3"] is expect
    assert not s["trend_c1"] and not s["trend_c2"]
    assert s["cash_fraction"] == (0.20 if expect else 0.0)


def test_trend_reads_only_closes_before_D_and_the_ten_percent_boundary():
    x = [100.0] * 599 + [90.0]                         # exactly 10% below the high
    tri = _tri(x)
    D = tri.index[-1] + pd.offsets.BDay(1)
    assert v2.trend_state(tri, D)["trend_c2"]
    crash = pd.concat([tri, pd.Series([1.0], index=[D])])   # a close ON D is not known at D
    assert v2.trend_state(crash, D) == v2.trend_state(tri, D)
    near = _tri([100.0] * 599 + [90.0001])
    assert not v2.trend_state(near, near.index[-1] + pd.offsets.BDay(1))["trend_c2"]
    with pytest.raises(ValueError):
        v2.trend_state(_tri([100.0] * 300), pd.Timestamp("2030-01-01"))


def _tri_file(path, dates, values, name=v2.TREND_INDEX):
    rows = pd.DataFrame({"index_name": name, "date": pd.DatetimeIndex(dates).astype("datetime64[us]"),
                         "tri": np.asarray(values, float), "net_tri": np.asarray(values, float)})
    other = rows.assign(index_name="NIFTY 50", tri=1.0, net_tri=1.0)
    pd.concat([rows, other], ignore_index=True).to_parquet(path, index=False)
    return path


def test_trend_counts_only_the_archive_market_sessions(tmp_path):
    """Clarification after the in-sample engine comparison: the index's special
    sessions the archive does not hold (a Muhurat Sunday, a budget Saturday) are
    not closes; an archive session without a close is a missing close; nothing
    on or after the end is read."""
    cal = pd.bdate_range("2015-01-01", periods=420)                 # the archive's sessions
    special = [pd.Timestamp("2015-02-28"), pd.Timestamp("2016-05-01")]   # Sat, Sun
    assert not any(d in cal for d in special)
    x = np.linspace(100.0, 140.0, len(cal))
    dates = cal.append(pd.DatetimeIndex(special)).sort_values()
    vals = pd.Series(x, index=cal).reindex(dates)
    vals[special] = 1.0                                             # absurd special closes
    end = cal[-1]
    p = _tri_file(tmp_path / "tri.parquet", dates, vals.to_numpy())
    t = v2.load_trend_index(end, cal, path=p)
    assert list(t.index) == list(cal[cal < end])                    # special sessions dropped
    np.testing.assert_array_equal(t.to_numpy(), x[:-1])
    s = v2.trend_state(t, end)
    assert s["n_true"] == 0 and s["index_last_date"] == cal[-2]
    # counting the special closes (every row of the index file) would change the average
    both = pd.Series(vals.to_numpy(), index=dates)
    assert v2.trend_state(both, end)["index_sma200"] != pytest.approx(s["index_sma200"])
    # an archive session the index file lacks is a missing close, refused in the window
    gap = _tri_file(tmp_path / "gap.parquet", cal.delete(400), np.delete(x, 400))
    tg = v2.load_trend_index(end, cal, path=gap)
    assert len(tg) == len(cal) - 1 and np.isnan(tg[cal[400]])
    with pytest.raises(ValueError, match="missing index close"):
        v2.trend_state(tg, end)
    # a close on or after the end is never loaded
    assert v2.load_trend_index(cal[300], cal, path=p).index[-1] == cal[299]


# ---------------------------------------------------------------- A1 indicators
def _wilder_by_hand(x, n=14):
    """Wilder's RSI written out step by step, independently of the engine."""
    out = [np.nan] * len(x)
    gains = [max(x[i] - x[i - 1], 0.0) for i in range(1, len(x))]
    losses = [max(x[i - 1] - x[i], 0.0) for i in range(1, len(x))]
    def rsi(ag, al):
        if al > 0:
            return 100 - 100 / (1 + ag / al)
        return 100.0 if ag > 0 else np.nan          # both zero: undefined

    ag = sum(gains[:n]) / n
    al = sum(losses[:n]) / n
    out[n] = rsi(ag, al)
    for i in range(n + 1, len(x)):
        ag = (ag * (n - 1) + gains[i - 1]) / n
        al = (al * (n - 1) + losses[i - 1]) / n
        out[i] = rsi(ag, al)
    return np.array(out)


def test_rsi_is_wilder_seeded_at_the_first_close_and_sma_is_50_closes():
    rng = np.random.default_rng(7)
    x = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, 120)))
    L = np.full((125, 2), np.nan)
    L[5:, 0] = x                                    # stock 0 lists at row 5
    L[:120, 1] = x                                  # stock 1 from row 0
    L[120:, 1] = x[-1]                              # then stops trading: carried close
    sma, rsi = v2.a1_indicators(L)
    np.testing.assert_allclose(rsi[5:, 0], _wilder_by_hand(list(x)), rtol=1e-12, equal_nan=True)
    assert np.isnan(rsi[:19, 0]).all() and np.isfinite(rsi[19, 0])
    assert np.isnan(sma[:54, 0]).all()
    assert sma[54, 0] == pytest.approx(x[:50].mean(), rel=1e-12)
    assert sma[124, 0] == pytest.approx(x[-50:].mean(), rel=1e-12)
    # a carried close is a zero change: the RSI does not move while the stock is idle
    assert rsi[124, 1] == pytest.approx(rsi[119, 1], rel=1e-12)


def test_a1_rule_boundaries():
    last = np.array([110.0, 110.0001, 100.0, 100.0, np.nan])
    sma = np.array([100.0, 100.0, 100.0, np.nan, 100.0])
    rsi = np.array([69.99, 50.0, 70.0, 50.0, 50.0])
    assert v2.a1_rule(last, sma, rsi).tolist() == [True, False, False, False, False]


def test_rsi_is_undefined_when_average_gain_and_loss_are_both_zero():
    """Clarification after the in-sample engine comparison: a flat stock has no
    RSI and fails A1; only a zero average loss with some gain is 100; a zero
    average gain with some loss is 0 and can qualify."""
    L = np.full((80, 3), 100.0)
    L[:, 1] = np.linspace(100.0, 101.0, 80)              # only gains: RSI 100
    L[70:, 2] = 99.0                                     # flat, then one fall on row 70
    sma, rsi = v2.a1_indicators(L)
    assert np.isnan(rsi[:, 0]).all()                     # flat throughout: never defined
    assert (rsi[14:, 1] == 100.0).all()
    assert np.isnan(rsi[14:70, 2]).all() and (rsi[70:, 2] == 0.0).all()
    q = v2.a1_qualifies(L)
    assert not q[:, 0].any()                             # the undefined RSI fails the condition
    assert not q[:, 1].any()                             # RSI 100 is not below 70
    assert not q[:70, 2].any() and q[70:, 2].all()       # 99 <= 1.1 x its average, RSI 0
    np.testing.assert_allclose(rsi[:, 2], _wilder_by_hand(list(L[:, 2])), equal_nan=True)


def test_a1_closes_are_eq_be_closes_only_carried_over_bz_and_idle_sessions():
    """A BZ-only session's close is not an A1 close: the last EQ/BE close is
    carried over it, and before a stock's first EQ/BE close there is none, so
    Wilder's RSI is seeded at the first EQ/BE close."""
    T = 40
    closes = np.full((T, 2), 100.0)
    closes[:, 0] = 100.0 + np.arange(T)                  # a distinct close every session
    closes[:, 1] = 50.0 + np.arange(T)
    traded = np.ones((T, 2), bool)
    traded[12, 0] = False                                # S0: no trade at all on 12
    buyable = np.ones((T, 2), bool)
    buyable[10:12, 0] = False                            # S0: BZ only on 10 and 11
    buyable[:5, 1] = False                               # S1: BZ only before its first EQ close
    p = _panel(closes, closes=closes, traded=traded, buyable=buyable)
    a = v2.a1_closes(p)
    assert np.isnan(a[:5, 1]).all() and a[5, 1] == 55.0  # no A1 close before the first EQ/BE
    assert p.last[4, 1] == 54.0                          # while Panel.last carries the BZ close
    assert a[9, 0] == 109.0
    assert (a[10:13, 0] == 109.0).all()                  # BZ closes and the idle day carry 109
    assert p.last[11, 0] == 111.0
    assert a[13, 0] == 113.0
    assert np.isnan(a[-1]).sum() == 0 and a[-1, 0] == closes[-2, 0]   # the end date: open only
    # the RSI on the A1 closes is seeded at S1's first EQ/BE close (row 5)
    _, rsi = v2.a1_indicators(a)
    assert np.isnan(rsi[:19, 1]).all() and np.isfinite(rsi[19, 1])


# ---------------------------------------------------------------- hard filters
def _bsm(roce, de, usable):
    idx = pd.Index(list("abcdefgh")[:len(roce)], name="symbol")
    return pd.DataFrame({"roce": roce, "de": de, "bs_usable": usable}, index=idx)


def test_market_cap_and_pe_filters_apply_throughout():
    idx = pd.Index(list("abcdef"), name="symbol")
    mcap = pd.Series([1e9, 1e9 + 1, 7e9, 7.0001e9, 5e9, 5e9], index=idx)
    profit = pd.Series([1e8, 1e8, 1e8, 1e8, -1e8, 0.0], index=idx)
    hf = v2.hard_filters(mcap, profit, pd.Timestamp("2022-11-15"))
    assert hf["pass_mcap"].tolist() == [False, True, True, True, True, True]   # above Rs 100 cr
    assert hf["pass_pe"].tolist() == [True, True, True, False, False, False]   # 0 < P/E <= 70
    assert hf["pass_roce"].all() and hf["pass_de"].all()                        # not yet
    assert hf["passes"].tolist() == [False, True, True, False, False, False]
    fn = v2.hard_funnel(hf, pd.Timestamp("2022-11-15"))
    assert fn["n_no_balance_sheet"] == 0 and fn["h5_de_below_1_5"] == 2


def test_roce_and_de_filters_from_february_2023_and_missing_balance_sheet_fails_both():
    idx = pd.Index(list("abcdefg"), name="symbol")
    mcap = pd.Series(5e9, index=idx)
    profit = pd.Series(5e8, index=idx)
    bsm = pd.DataFrame({"roce": [0.10, 0.0999, 0.25, 0.25, np.nan, 0.30, np.nan],
                        "de": [0.2, 0.2, 1.5, 1.4999, 0.2, np.nan, np.nan],
                        "bs_usable": [True, True, True, True, True, False, False]}, index=idx)
    before = v2.hard_filters(mcap, profit, pd.Timestamp("2022-11-15"), bsm)
    assert before["passes"].all()                   # January 2023 would still be v1's
    D = pd.Timestamp("2023-02-15")
    hf = v2.hard_filters(mcap, profit, D, bsm)
    assert hf["pass_roce"].tolist() == [True, False, True, True, False, False, False]
    assert hf["pass_de"].tolist() == [True, True, False, True, True, False, False]
    assert hf["passes"].tolist() == [True, False, False, True, False, False, False]
    fn = v2.hard_funnel(hf, D)
    assert fn["n_no_balance_sheet"] == 2            # f and g: no usable balance sheet
    assert fn["h3_balance_sheet_usable"] == 5 and fn["h5_de_below_1_5"] == 2
    with pytest.raises(ValueError):
        v2.hard_filters(mcap, profit, D, None)


# ---------------------------------------------------------------- scoring
def _measures(n=40, seed=3):
    rng = np.random.default_rng(seed)
    idx = pd.Index([f"S{i:02d}" for i in range(n)], name="symbol")
    m = pd.DataFrame({k: rng.normal(size=n) for k in list(composite.MEASURES) + ["roce"]},
                     index=idx)
    m.iloc[3, 0] = np.nan                           # a missing measure scores 0.5
    m["ticker"] = idx
    return m


def test_score_without_roce_is_v1s_score():
    m = _measures()
    a = v2.score_v2(m, use_roce=False)
    b = composite.score(m)
    cols = [f"pct_{k}" for k in composite.MEASURES] + [f"grp_{g}" for g in composite.GROUPS] + \
           ["composite", "rank"]
    pd.testing.assert_frame_equal(a[cols], b[cols])


def test_score_with_roce_replaces_margin_in_the_quality_group():
    m = _measures()
    a = v2.score_v2(m, use_roce=True)
    assert "pct_margin" not in a
    exp = (m["roce"].rank(pct=True) + m["stability"].rank(pct=True)) / 2
    assert a["grp_quality"].to_numpy() == pytest.approx(exp.reindex(a.index).to_numpy())
    assert v2.measure_groups(True)["roce"] == "quality"
    assert list(v2.measure_groups(True)) == ["roce"] + list(composite.MEASURES)[1:]


# ---------------------------------------------------------------- sectors
def _ids():
    cal = pd.bdate_range("2019-01-01", periods=400)
    keys = pd.DataFrame([("OLDCO", "INE001A01011", cal[0], cal[99]),
                         ("NEWCO", "INE001A01011", cal[100], cal[399]),
                         ("SOLO", "INE002A01011", cal[0], cal[399])],
                        columns=["symbol", "isin", "first", "last"])
    return Identity(link(keys, cal)), cal


def test_nse_sector_joins_by_stock_id_so_a_renamed_company_keeps_its_label():
    ids, cal = _ids()
    only_old = pd.DataFrame({"symbol": ["OLDCO", "SOLO", "GONE"],
                             "isin": ["INE001A01011", "INE002A01011", None],
                             "sector": ["Chemicals", "Power", None],
                             "label_scheme": ["legacy", "current", None]})
    s = v2.nse_sectors(only_old, ids)
    assert s.at["NEWCO", "sector"] == "Chemicals"   # labelled under its old symbol
    assert s.at["SOLO", "sector"] == "Power"
    assert "GONE" not in s.index                    # no label: its own sector later
    both = pd.DataFrame({"symbol": ["OLDCO", "NEWCO"], "isin": ["INE001A01011"] * 2,
                         "sector": ["IT", "Information Technology"],
                         "label_scheme": ["legacy", "current"]})
    assert v2.nse_sectors(both, ids).at["NEWCO", "sector"] == "Information Technology"
    t = v2.Tickers(ids)
    assert t.at("NEWCO", cal[50]) == "OLDCO" and t.at("NEWCO", cal[150]) == "NEWCO"


def _ranked(sectors, sigma=0.02):
    n = len(sectors)
    idx = pd.Index([f"S{i:02d}" for i in range(1, n + 1)], name="symbol")
    return pd.DataFrame({"rank": range(1, n + 1), "sector": sectors, "sigma": sigma,
                         "ticker": idx}, index=idx)


def test_book_of_twelve_with_the_nse_cap_and_unlabelled_as_own_sector():
    r = _ranked(["Steel"] * 5 + [None] * 5 + ["IT"] * 5 + ["Power"] * 15)
    ranks = r.reset_index().assign(D=pd.Timestamp("2021-02-15"))
    choose = v2.make_chooser(ranks, v2.ConfigV2())
    tgt, info = choose(pd.Timestamp("2021-02-15"), set())
    # three Steel, all five unlabelled (never capped), three IT, then Power to twelve
    assert tgt == ["S01", "S02", "S03", "S06", "S07", "S08", "S09", "S10", "S11", "S12",
                   "S13", "S16"]
    assert pd.isna(info["S06"]["sector"]) and info["S01"]["sigma"] == 0.02


def test_band_of_24_and_the_three_sold_reasons():
    r = _ranked([None] * 30)
    D = pd.Timestamp("2023-02-15")
    ranks = r.reset_index().assign(D=D)
    excluded = pd.DataFrame({"D": [D], "symbol": ["BROKE"]})    # failed a hard filter at D
    choose = v2.make_chooser(ranks, v2.ConfigV2(), excluded=excluded)
    held = {"S24", "S25", "BROKE", "DELISTED"}
    tgt, info = choose(D, held)
    assert "S24" in tgt and "S25" not in tgt
    assert len(tgt) == 12
    assert info["S25"]["sold_reason"] == "outside_band"
    assert info["BROKE"]["sold_reason"] == "thesis_break"     # whatever its rank had been
    assert info["DELISTED"]["sold_reason"] == "left_universe"


# ---------------------------------------------------------------- synthetic panel
def _panel(opens, closes=None, traded=None, buyable=None, symbols=None):
    opens = np.asarray(opens, float)
    T, S = opens.shape
    dates = pd.bdate_range("2021-01-04", periods=T)
    closes = opens.copy() if closes is None else np.asarray(closes, float)
    traded = np.ones((T, S), bool) if traded is None else np.asarray(traded, bool)
    buyable = traded.copy() if buyable is None else np.asarray(buyable, bool) & traded
    open_ = np.where(traded, opens, np.nan)
    close = np.where(traded, closes, np.nan)
    close[-1] = np.nan                                  # the end date: open only
    last = pd.DataFrame(close).ffill().to_numpy()
    run = np.zeros((T, S), dtype=np.int32)
    run[0] = np.where(traded[0], 0, 1)
    for t in range(1, T):
        run[t] = np.where(traded[t], 0, run[t - 1] + 1)
    syms = pd.Index(symbols or [f"S{i}" for i in range(S)])
    return engine.Panel(dates=dates, symbols=syms, open_=open_, close=close, last=last,
                        traded=traded, no_trade_run=run, div_unit=np.zeros((T, S)),
                        end=dates[-1], buyable=buyable)


def _fixed(targets_by_t, panel, sigma=0.02, sectors=None):
    """choose() returning a fixed book per decision date."""
    by = {panel.dates[t]: names for t, names in targets_by_t.items()}

    def choose(D, held):
        tgt = list(by[pd.Timestamp(D)])
        info = {s: {"sigma": sigma, "rank": i + 1, "sector": (sectors or {}).get(s),
                    "ticker": s} for i, s in enumerate(tgt)}
        for s in held - set(tgt):
            info[s] = {"sigma": np.nan, "rank": None, "sector": None, "ticker": s,
                       "sold_reason": "left_universe"}
        return tgt, info
    return choose


def _run(panel, targets_by_t, variant="a", cash=0.0, qualify=None, cost=0.0, capital=1.2e5):
    cfg = v2.ConfigV2(variant=variant, cost=cost, initial_capital=capital)
    dec = [panel.dates[t] for t in sorted(targets_by_t)]
    return v2.simulate_v2(panel, dec, _fixed(targets_by_t, panel), {d: cash for d in dec}, cfg,
                          is_end=panel.end, qualify=qualify)


def _fills(res, kind=None):
    f = res.fills
    return f if kind is None else f[f["kind"] == kind]


def test_new_position_is_bought_in_three_tranches_at_d_21_and_42():
    p = _panel(np.full((80, 1), 100.0))
    res = _run(p, {1: ["S0"]})
    V = 1.5 / 12 * 1.2e5                               # one name: capped at 1.5x equal weight
    buys = _fills(res)
    assert buys["kind"].tolist() == ["tranche1", "tranche2", "tranche3"]
    assert list(buys["date"]) == [p.dates[t].date() for t in (1, 22, 43)]
    assert list(buys["scheduled_date"]) == list(buys["date"])
    assert buys["value"].to_numpy() == pytest.approx(np.full(3, V / 3))
    assert (buys["D"] == p.dates[1].date()).all()
    assert res.nav["nav"].to_numpy() == pytest.approx(np.full(len(res.nav), 1.2e5))
    h = res.holdings
    assert h["status"].tolist() == ["new"] and h["target_weight"].iloc[0] == pytest.approx(0.125)


def test_a_tranche_scheduled_after_the_end_is_pending_not_a_crash():
    """The last decision date's third tranche falls 42 sessions later, after the
    run's end (17 Aug 2026 against an 18 Sep 2026 end). It is reported as pending
    at the end, dated 'after the end', and its cash stays in the NAV as cash."""
    p = _panel(np.full((30, 1), 100.0))                # sessions 0..29: t=43 is past the end
    res = _run(p, {1: ["S0"]})
    buys = _fills(res)
    assert buys["kind"].tolist() == ["tranche1", "tranche2"]
    pend = res.pending_at_end
    assert len(pend) == 1 and int(pend["tranche"].iloc[0]) == 3
    assert pend["scheduled_date"].iloc[0] is None and bool(pend["scheduled_after_end"].iloc[0])
    assert pend["D"].iloc[0] == p.dates[1].date()
    assert res.nav["nav"].to_numpy() == pytest.approx(np.full(len(res.nav), 1.2e5))


def test_trend_cash_reduces_every_target():
    p = _panel(np.full((80, 12), 100.0))
    res = _run(p, {1: [f"S{i}" for i in range(12)]}, cash=0.275)
    assert res.holdings["target_weight"].sum() == pytest.approx(0.725)
    t1 = _fills(res, "tranche1")
    assert t1["value"].sum() == pytest.approx(0.725 * 1.2e5 / 3)


def test_variant_a_cancels_a_tranche_after_five_sessions_without_a_trade():
    traded = np.ones((80, 2), bool)
    traded[22:28, 0] = False                            # S0: no trade on 22..27
    traded[22:27, 1] = False                            # S1: trades again on 27, the last chance
    p = _panel(np.full((80, 2), 100.0), traded=traded)
    res = _run(p, {1: ["S0", "S1"]})
    f0 = _fills(res)[lambda d: d["symbol"] == "S0"]
    assert f0["kind"].tolist() == ["tranche1", "cancelled", "tranche3"]
    c = f0[f0["kind"] == "cancelled"].iloc[0]
    assert c["date"] == p.dates[27].date() and c["scheduled_date"] == p.dates[22].date()
    assert c["value"] == 0 and c["units_adjusted"] == 0 and np.isnan(c["price_adjusted"])
    f1 = _fills(res)[lambda d: d["symbol"] == "S1"]
    assert f1["kind"].tolist() == ["tranche1", "tranche2", "tranche3"]
    assert f1.iloc[1]["date"] == p.dates[27].date()


def test_a_bz_only_session_does_not_fill_a_buy():
    buyable = np.ones((80, 1), bool)
    buyable[22, 0] = False                              # traded only in BZ on session 22
    p = _panel(np.full((80, 1), 100.0), buyable=buyable)
    res = _run(p, {1: ["S0"]})
    assert _fills(res, "tranche2")["date"].iloc[0] == p.dates[23].date()


def test_next_decision_date_cancels_a_pending_tranche_and_resizes_in_full():
    p = _panel(np.full((80, 1), 100.0))
    res = _run(p, {1: ["S0"], 31: ["S0"]})
    f = _fills(res)
    assert f["kind"].tolist() == ["tranche1", "tranche2", "cancelled", "rebalance"]
    c = f[f["kind"] == "cancelled"].iloc[0]
    assert c["date"] == p.dates[31].date() and c["scheduled_date"] == p.dates[43].date()
    assert c["D"] == p.dates[1].date()
    r = f[f["kind"] == "rebalance"].iloc[0]
    V = 0.125 * 1.2e5
    assert r["side"] == "buy" and r["value"] == pytest.approx(V / 3)   # topped up to V
    assert r["date"] == p.dates[31].date()
    assert res.holdings["status"].tolist() == ["new", "kept"]


def test_a_position_that_leaves_the_book_cancels_its_pending_tranche():
    p = _panel(np.full((80, 2), 100.0))
    res = _run(p, {1: ["S0"], 31: ["S1"]})
    f0 = _fills(res)[lambda d: d["symbol"] == "S0"]
    assert f0["kind"].tolist() == ["tranche1", "tranche2", "cancelled", "rebalance"]
    assert f0.iloc[3]["side"] == "sell" and f0.iloc[3]["date"] == p.dates[31].date()
    assert res.holdings.set_index("symbol").loc["S0"].iloc[-1]["status"] == "sold"
    f1 = _fills(res)[lambda d: d["symbol"] == "S1"]
    assert list(f1["date"]) == [p.dates[t].date() for t in (31, 52, 73)]


def test_forced_exit_cancels_pending_tranches():
    traded = np.ones((80, 1), bool)
    traded[6:, 0] = False                               # last trade on session 5
    p = _panel(np.full((80, 1), 100.0), traded=traded)
    res = _run(p, {1: ["S0"]})
    f = _fills(res)
    assert f["kind"].tolist() == ["tranche1", "forced_exit", "cancelled", "cancelled"]
    fe = f[f["kind"] == "forced_exit"].iloc[0]
    assert fe["date"] == p.dates[25].date()             # 20th session without a trade
    cancelled = f[f["kind"] == "cancelled"]
    assert (cancelled["date"] == p.dates[25].date()).all()
    assert sorted(cancelled["scheduled_date"]) == [p.dates[22].date(), p.dates[43].date()]


def _qualify(T, S, rows=()):
    q = np.zeros((T, S), bool)
    for t in rows:
        q[t, 0] = True
    return q


def test_a1_buys_after_the_first_qualifying_close():
    p = _panel(np.full((80, 1), 100.0))
    res = _run(p, {1: ["S0"]}, variant="b", qualify=_qualify(80, 1, rows=(4, 30, 60)))
    f = _fills(res)
    # tranche 1 (scheduled 1): close of 4 qualifies -> open of 5; tranche 2 (22): close of
    # 30 -> 31; tranche 3 (43): close of 60 -> 61
    assert list(f["date"]) == [p.dates[t].date() for t in (5, 31, 61)]
    assert list(f["scheduled_date"]) == [p.dates[t].date() for t in (1, 22, 43)]


def test_a1_first_tranche_uses_the_close_before_d():
    p = _panel(np.full((80, 1), 100.0))
    res = _run(p, {1: ["S0"]}, variant="b", qualify=_qualify(80, 1, rows=(0,)))
    assert _fills(res, "tranche1")["date"].iloc[0] == p.dates[1].date()


def test_a1_falls_back_to_the_31st_session():
    """Clarification after the in-sample engine comparison: the scheduled session
    is the first of the 30, so qualifying buys are at its open or the next 29
    opens, and the fallback is the open 30 sessions after it (the 31st)."""
    p = _panel(np.full((110, 1), 100.0))
    res = _run(p, {1: ["S0"]}, variant="b", qualify=np.zeros((110, 1), bool))
    f = _fills(res)
    # nothing ever qualifies: each tranche is bought at the open 30 sessions after its
    # scheduled one (1, 22, 43)
    assert list(f["date"]) == [p.dates[t].date() for t in (31, 52, 73)]
    assert list(f["scheduled_date"]) == [p.dates[t].date() for t in (1, 22, 43)]
    # the last in-window chance: the close of row 29 qualifies and buys at the open of
    # row 30, the 30th session counting the scheduled one (row 1) as the first
    q2 = _qualify(110, 1, rows=(1 + 28,))
    assert _fills(_run(p, {1: ["S0"]}, variant="b", qualify=q2), "tranche1")["date"].iloc[0] \
        == p.dates[30].date()
    # a qualifying close on row 30 is too late to matter: row 31 is the fallback anyway,
    # and a qualifying close on row 0 buys at the scheduled open itself
    for rows, t in (((30,), 31), ((0,), 1)):
        q3 = _qualify(110, 1, rows=rows)
        assert _fills(_run(p, {1: ["S0"]}, variant="b", qualify=q3), "tranche1")["date"].iloc[0] \
            == p.dates[t].date()


def test_a1_fallback_waits_five_sessions_for_a_trade_then_cancels():
    traded = np.ones((110, 2), bool)
    traded[31:36, 0] = False                            # S0 trades again on 36 (k = 35)
    traded[31:37, 1] = False                            # S1 not until 37
    p = _panel(np.full((110, 2), 100.0), traded=traded)
    res = _run(p, {1: ["S0", "S1"]}, variant="b", qualify=np.zeros((110, 2), bool))
    f = _fills(res)
    assert f[(f.symbol == "S0") & (f.kind == "tranche1")]["date"].iloc[0] == p.dates[36].date()
    c = f[(f.symbol == "S1") & (f.kind == "cancelled")]
    assert len(c) == 1 and c["date"].iloc[0] == p.dates[36].date()
    assert c["scheduled_date"].iloc[0] == p.dates[1].date()


def test_a1_metric_is_the_v_weighted_price_over_the_close_before_d():
    T = 80
    px = np.full((T, 2), 100.0)
    px[:, 1] = 50.0
    px[22:, 0] = 110.0                                  # S0's later tranches cost more
    p = _panel(px)
    res = _run(p, {1: ["S0", "S1"]})
    a = v2.a1_metric(res)
    # S0: V/3 at 100 and 2V/3 at 110 -> unit-weighted average 1/(1/3/100 + 2/3/110)
    avg0 = 1 / (1 / 300 + 2 / 330)
    assert a["n_positions"] == 2 and a["n_with_fills"] == 2
    rows = {r["symbol"]: r for r in a["positions"]}
    assert rows["S0"]["ratio"] == pytest.approx(avg0 / 100)
    assert rows["S1"]["ratio"] == pytest.approx(1.0)
    assert a["v_weighted_ratio"] == pytest.approx((avg0 / 100 + 1.0) / 2)   # equal V


def test_a1_metric_reference_close_is_the_last_eq_be_close_not_a_bz_close():
    """The adoption test's reference close is an EQ/BE close: when the session
    before D traded only in BZ, the last EQ/BE close before it is used."""
    T = 80
    px = np.full((T, 1), 100.0)
    closes = px.copy()
    closes[1, 0] = 80.0                                 # a BZ-only session just before D
    buyable = np.ones((T, 1), bool)
    buyable[1, 0] = False
    p = _panel(px, closes=closes, buyable=buyable)
    assert p.last[1, 0] == 80.0
    res = _run(p, {2: ["S0"]})
    pos = v2.a1_metric(res)["positions"][0]
    assert pos["close_before_D"] == 100.0 and pos["ratio"] == pytest.approx(1.0)


def test_costs_and_cash_add_up_and_the_last_tranche_absorbs_the_costs():
    rng = np.random.default_rng(1)
    px = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, (90, 12)), axis=0))
    p = _panel(px)
    res = _run(p, {1: [f"S{i}" for i in range(12)], 60: [f"S{i}" for i in range(12)]},
               cost=0.002)
    f = _fills(res)
    f = f[f["kind"] != "cancelled"]
    buy = f[f.side == "buy"]["value"].sum()
    sell = f[f.side == "sell"]["value"].sum()
    cash_end = 1.2e5 - buy * 1.002 + sell * 0.998
    assert res.cash.iloc[-1] == pytest.approx(cash_end, abs=1e-6)
    assert res.cash.min() > -1e-6
    # the costs of the first two tranches leave the third a little short: scaled pro rata
    t3 = _fills(res, "tranche3")["value"].to_numpy()
    t1 = _fills(res, "tranche1")["value"].to_numpy()
    assert (t3 < t1).all() and (t3 > 0.99 * t1).all()


def test_simulate_refuses_the_holdout_and_variant_b_needs_a1():
    p = _panel(np.full((30, 1), 100.0))
    cfg = v2.ConfigV2()
    with pytest.raises(engine.HoldoutLocked):
        v2.simulate_v2(p, [p.dates[1]], _fixed({1: ["S0"]}, p), {p.dates[1]: 0.0}, cfg,
                       is_end=p.dates[-3])
    with pytest.raises(ValueError):
        v2.simulate_v2(p, [p.dates[1]], _fixed({1: ["S0"]}, p), {p.dates[1]: 0.0},
                       v2.ConfigV2(variant="b"), is_end=p.end)
    with pytest.raises(ValueError):
        v2.ConfigV2(variant="c")


# ---------------------------------------------------------------- synthetic archive
REV, DEP, FIN = 1e9, 1e7, 2e7
CAL = pd.bdate_range("2021-06-01", "2023-04-28")
QTRS = pd.date_range("2021-06-30", "2022-12-31", freq="QE")
# name: (ebitda margin, close, shares, equity, debt, balance sheets from, nse sector)
STOCKS = {
    "AAA": (0.30, 200.0, 1e7, 4e9, 1e9, "2022-03-31", "Chemicals"),
    "BBB": (0.20, 90.0, 1e7, 4e9, 1e9, "2022-03-31", "Chemicals"),     # mcap about Rs 90 cr
    "CCC": (0.001, 150.0, 1e7, 4e9, 1e9, "2022-03-31", "Chemicals"),   # P/E 750
    "DDD": (0.08, 150.0, 1e7, 4e9, 1e9, "2022-03-31", "Power"),        # ROCE 5.6%
    "EEE": (0.50, 150.0, 1e7, 4e9, 7e9, "2022-03-31", "Power"),        # D/E 1.75
    "FFF": (0.30, 150.0, 1e7, 4e9, 1e9, None, "Power"),                # no balance sheet
    "GGG": (0.30, 150.0, 1e7, -1e9, 1e9, "2022-03-31", None),          # equity below zero
    "HHH": (0.30, 150.0, 1e7, 4e9, 1e9, "old", None),                  # only Dec 2021's
    "III": (0.25, 160.0, 1e7, 4e9, 1e9, "2022-03-31", "IT"),
    "JJJ": (0.35, 170.0, 1e7, 4e9, 0.0, "2022-03-31", "IT"),
    "KKK": (0.40, 180.0, 1e7, 4e9, 2e9, "2022-03-31", "IT"),
    "LLL": (0.22, 190.0, 1e7, 4e9, 1e9, "2022-03-31", "IT"),
}


def _synthetic_archive():
    con = duckdb.connect(":memory:")
    px, fin, ann = [], [], []
    for i, (sym, (m, close, shares, eq, debt, bs_from, _)) in enumerate(STOCKS.items()):
        for k, d in enumerate(CAL):
            c = close * (1 + 0.01 * np.sin(k / 7 + i))      # gentle, different moves
            px.append({"date": d.date(), "symbol": sym, "series": "EQ",
                       "isin_code": f"INE{i:03d}A01011", "open": c, "high": c, "low": c,
                       "close": c, "prev_close": c, "volume": 1e5, "turnover": 1e7,
                       "trades": 100})
        for pe in QTRS:
            if bs_from == "old":
                bs = pe == pd.Timestamp("2021-12-31")
            else:
                bs = bs_from is not None and pe >= pd.Timestamp(bs_from)
            ebitda = m * REV
            pbe = ebitda - DEP - FIN
            fin.append({"symbol": sym, "company": f"{sym} Industries Limited",
                        "period_end": pe.date(), "broadcast_dt": pe + pd.Timedelta(days=40),
                        "consolidated": False, "revenue": REV, "total_income": REV,
                        "employee_cost": 0.1 * REV, "other_income": 0.0, "ebitda": ebitda,
                        "depreciation": DEP, "finance_costs": FIN,
                        "pbt_before_exceptional": pbe, "exceptional_items": 0.0, "pbt": pbe,
                        "profit_normalised": 0.5 * ebitda, "shares_implied": shares,
                        "equity": eq if bs else None, "debt_total": debt if bs else None,
                        "has_balance_sheet": bool(bs),
                        "assets": (abs(eq) + debt + 2e9) if bs else None,
                        "equity_capital": 1e8 if bs else None,
                        "xbrl_url": "https://x/xbrl/INDAS_1_2.xml"})
        ann.append({"an_dt": pd.Timestamp("2021-06-05 10:00"), "symbol": sym,
                    "industry": "Chemicals"})
    prices = pd.DataFrame(px)
    frames = {"prices": prices, "prices_other": prices.iloc[:0], "financials": pd.DataFrame(fin),
              "announcements": pd.DataFrame(ann),
              "corpactions": pd.DataFrame({
                  "ex_date": pd.Series(dtype="datetime64[ns]"), "symbol": pd.Series(dtype=str),
                  "isin": pd.Series(dtype=str), "company": pd.Series(dtype=str),
                  "action": pd.Series(dtype=str), "subject": pd.Series(dtype=str),
                  "factor": pd.Series(dtype=float)})}
    date_cols = {"prices": "date", "prices_other": "date", "corpactions": "ex_date",
                 "financials": "period_end"}
    for name, df in frames.items():
        df = df.copy()
        if name in date_cols:
            df[date_cols[name]] = pd.to_datetime(df[date_cols[name]])
        con.register("_tmp", df)
        con.execute(f"CREATE TABLE {name} AS SELECT * FROM _tmp")
        con.unregister("_tmp")
        if name in date_cols:
            con.execute(f"ALTER TABLE {name} ALTER {date_cols[name]} TYPE DATE")
    labels = pd.DataFrame({"symbol": list(STOCKS),
                           "isin": [f"INE{i:03d}A01011" for i in range(len(STOCKS))],
                           "sector": [v[-1] for v in STOCKS.values()],
                           "label_scheme": ["current"] * len(STOCKS)})
    return con, labels


@pytest.fixture(scope="module")
def archive():
    con, labels = _synthetic_archive()
    static = pit.StaticLabels.load(con)
    sectors = v2.nse_sectors(labels, static.ids)
    return con, static, sectors


NOV22, FEB23 = pd.Timestamp("2022-11-15"), pd.Timestamp("2023-02-15")


def test_universe_before_february_2023_uses_mcap_and_pe_and_ranks_on_margin(archive):
    con, static, sectors = archive
    r, fn, ex, _ = v2.compute_v2(con, NOV22, static, sectors)
    assert fn["r7_mcap_computable"] == 12
    assert set(ex["symbol"]) == {"BBB", "CCC"}
    assert set(r.index) == set(STOCKS) - {"BBB", "CCC"}
    assert (r["quality_measure"] == "margin").all()
    assert r["roce"].isna().all() and r["pct_roce"].isna().all()
    # percentiles are taken within the v2 universe (10 names)
    assert r["pct_margin"].max() == 1.0 and r["pct_margin"].min() == pytest.approx(0.1)
    assert fn["n_no_balance_sheet"] == 0
    assert r.at["AAA", "sector"] == "Chemicals" and pd.isna(r.at["GGG", "sector"])
    assert r["sigma"].to_numpy() == pytest.approx(-r["low_vol"].to_numpy())


def test_universe_from_february_2023_adds_roce_de_and_a_usable_balance_sheet(archive):
    con, static, sectors = archive
    r, fn, ex, _ = v2.compute_v2(con, FEB23, static, sectors)
    out = set(ex["symbol"])
    assert out == {"BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH"}
    assert set(r.index) == {"AAA", "III", "JJJ", "KKK", "LLL"}
    e = ex.set_index("symbol")
    assert not e.at["DDD", "pass_roce"] and e.at["DDD", "pass_de"]
    assert e.at["EEE", "pass_roce"] and not e.at["EEE", "pass_de"]
    for s in ("FFF", "GGG", "HHH"):                  # none, equity < 0, older than 400 days
        assert not e.at[s, "bs_usable"] and not e.at[s, "pass_roce"] and not e.at[s, "pass_de"]
    assert fn["n_no_balance_sheet"] == 3
    assert (r["quality_measure"] == "roce").all()
    assert r["margin"].isna().all() and r["pct_margin"].isna().all()
    roce = 4 * (np.array([0.30, 0.25, 0.35, 0.40, 0.22]) * REV - DEP) / \
        np.array([5e9, 5e9, 4e9, 6e9, 5e9])
    got = r.loc[["AAA", "III", "JJJ", "KKK", "LLL"], "roce"].to_numpy()
    assert got == pytest.approx(roce, rel=1e-12)
    assert r.at["KKK", "de"] == pytest.approx(0.5)
    assert r["pct_roce"].to_numpy() == pytest.approx(r["roce"].rank(pct=True).to_numpy())


def test_both_variants_end_to_end_on_the_synthetic_archive(archive):
    """Universe, ranks, panel and simulation wired as the job wires them: the
    February 2023 rebalance sells every holding that failed a hard filter
    (thesis break) and the books obey 12 names and 3 per sector."""
    con, static, sectors = archive
    decisions = [NOV22, FEB23]
    ranks, funnel, excluded, _ = v2.build_ranks_v2(con, decisions, static, sectors)
    end = pd.Timestamp("2023-04-26")
    panel = v2.build_panel_v2(con, ranks["symbol"].unique(), end, end, ids=static.ids)
    assert panel.dates[0] == CAL[0]                  # the whole archive, for A1's history
    tickers = v2.Tickers(static.ids)
    for variant in ("a", "b"):
        cfg = v2.ConfigV2(variant=variant)
        q = v2.a1_qualifies(v2.a1_closes(panel)) if variant == "b" else None
        res = v2.simulate_v2(panel, decisions, v2.make_chooser(ranks, cfg, excluded, sectors,
                                                               tickers),
                             {NOV22: 0.2, FEB23: 0.0}, cfg, end, qualify=q, tickers=tickers)
        h = res.holdings
        nov = h[h["D"] == NOV22.date()]
        # ten names in the universe, four of them IT: the cap leaves nine
        assert (nov["status"] == "new").all() and len(nov) == 9
        assert nov.groupby("sector").size().to_dict() == {"Chemicals": 1, "IT": 3, "Power": 3}
        assert nov["target_weight"].sum() == pytest.approx(0.8)          # 20% trend cash
        feb = h[h["D"] == FEB23.date()].set_index("symbol")
        sold = feb[feb["status"] == "sold"]
        assert set(sold.index) == {"DDD", "EEE", "FFF", "GGG", "HHH"}
        assert (sold["sold_reason"] == "thesis_break").all()
        kept = set(feb.index[feb["status"] == "kept"])
        assert len(kept) == 4 and kept <= {"AAA", "III", "JJJ", "KKK", "LLL"}
        assert (feb["status"] != "new").all()        # the fourth IT name is still capped
        f = res.fills
        assert set(f.loc[f["kind"] == "rebalance", "symbol"]) >= set(sold.index)
        assert np.isfinite(res.nav["nav"]).all()
        if variant == "a":
            assert set(f["kind"]) <= {"tranche1", "tranche2", "tranche3", "rebalance", "cancelled"}


# ---------------------------------------------------------------- guards and real labels
@pytest.mark.skipif(not DB.exists(), reason="needs data/zen.duckdb")
def test_holdout_guard_raises_in_cli():
    from jobs import backtest_v2
    with pytest.raises(engine.HoldoutLocked):
        backtest_v2.main(["--variant", "a", "--end", "2023-06-01", "--no-record"])


@pytest.mark.skipif(not DB.exists(), reason="needs data/zen.duckdb")
def test_real_labels_join_renamed_companies_and_membership_at_one_date():
    """Membership and labels only; no return. Cadila Healthcare traded as CADILAHC
    in 2021 and is labelled under ZYDUSLIFE today."""
    con = pit.connect(read_only=True)
    try:
        static = pit.StaticLabels.load(con)
        sectors = v2.nse_sectors(v2.load_nse_labels(), static.ids)
        assert sectors.at["ZYDUSLIFE", "sector"] == "Healthcare"
        D = pd.Timestamp("2021-02-15")
        r, fn, ex, _ = v2.compute_v2(con, D, static, sectors)
        v1r, _ = composite.compute(con, D, static)
        assert set(r.index) <= set(v1r.index)
        assert len(r) + len(ex) == len(v1r) == fn["r7_mcap_computable"]
        assert (r["mcap"] > v2.MCAP_MIN).all() and (r["pe"] <= v2.PE_MAX).all()
        assert r.at["ZYDUSLIFE", "ticker"] == "CADILAHC"
        assert r.at["ZYDUSLIFE", "sector"] == "Healthcare"
        assert (r["quality_measure"] == "margin").all()
    finally:
        con.close()


RUN_A = Path(__file__).resolve().parents[1] / "data" / "backtest" / "v2a_is"


@pytest.mark.skipif(not (DB.exists() and (RUN_A / "metrics.json").exists()),
                    reason="needs data/zen.duckdb and the v2a_is run folder")
def test_replay_hook_reproduces_the_run_and_takes_other_ranks():
    """The monkey test's hook: the run folder's own inputs reproduce its NAV to
    1e-9, and a reordered ranks frame runs through the same machinery. Only the
    size of the replay difference is read, never a return."""
    con = pit.connect(read_only=True)
    try:
        rep = v2.Replay.from_run(con, RUN_A)
        nav = pd.read_csv(RUN_A / "nav.csv")
        assert rep.reproduce(nav["strategy"]) <= 1e-9
        shuffled = rep.ranks.copy()
        shuffled["rank"] = shuffled.groupby("D")["rank"].transform(lambda r: r[::-1].to_numpy())
        res = rep.simulate(rep.chooser(shuffled), log_trades=True)
        assert len(res.nav) == len(nav)
        assert {"date", "symbol", "side", "units", "price", "value", "cost"} <= set(res.trades)
    finally:
        con.close()
