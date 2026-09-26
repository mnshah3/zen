"""The v1 re-run's two rule changes (v2-spec, Clarification to A2).

(a) Trade-for-trade (v1 disclosure 34): a session with no EQ/BE trade but a
    BZ trade is traded for Clarification 15's no-trade exit; the holding is
    valued at the BZ close and can be sold at the BZ price with the same cost;
    nothing is bought in BZ. Synthetic stocks move to BZ and back, stay in BZ,
    and stop trading altogether.
(b) Factor-index benchmarks (A4 and the Clarification to A4): opens from NSE's
    printed prices in the endpoints file; the parent's overnight move where
    NSE printed no open; the no-overnight sensitivity; refusal, never a guess,
    when a row the run needs is missing.
"""

from __future__ import annotations

import duckdb
import numpy as np
import pandas as pd
import pytest

from zen.data import store
from zen.portfolio import engine
from zen.universe import pit


# ---------------------------------------------------------------- (a) trade-for-trade
EQ_BEFORE, EQ_AFTER = 100.0, 90.0          # EQ price before and after a BZ spell
BZ_OPEN, BZ_CLOSE = 78.0, 80.0             # BZ prices
BOTH_BZ = 50.0                             # BZ price on a session that also has an EQ row


def _t4t_db(codes: dict[str, str], isin: dict[str, str] | None = None, n: int = 40):
    """One session per character: E an EQ row, B a BZ row only, 2 both, . neither.
    CAL trades EQ every session so the market calendar is complete."""
    c = duckdb.connect(":memory:")
    c.execute(store.SCHEMA + store.SCHEMA_OTHER)
    c.execute("CREATE TABLE corpactions (ex_date DATE, symbol VARCHAR, isin VARCHAR, "
              "company VARCHAR, action VARCHAR, subject VARCHAR, factor DOUBLE)")
    days = pd.bdate_range("2021-01-04", periods=n)
    codes = {"CAL": "E" * n, **codes}
    isin = isin or {}
    eq, bz = [], []
    for j, (sym, cs) in enumerate(codes.items()):
        assert len(cs) == n, sym
        code = isin.get(sym, f"INE{j:03d}A01011")
        seen_b = False
        for d, k in zip(days, cs):
            seen_b = seen_b or k == "B"
            if k in "E2":
                p = EQ_AFTER if seen_b else EQ_BEFORE
                eq.append((d.date(), sym, "EQ", code, p, p, p, p, None, 1000, 1e7, 10))
            if k == "B":
                bz.append((d.date(), sym, "BZ", code, BZ_OPEN, BZ_CLOSE, BZ_OPEN, BZ_CLOSE,
                           None, 100, 1e5, 5))
            if k == "2":
                bz.append((d.date(), sym, "BZ", code, BOTH_BZ, BOTH_BZ, BOTH_BZ, BOTH_BZ,
                           None, 100, 1e5, 5))
    c.executemany("INSERT INTO prices VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", eq)
    if bz:
        c.executemany("INSERT INTO prices_other VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", bz)
    return c, days


MOVES_AND_BACK = "E" * 5 + "B" * 25 + "E" * 10     # 25 BZ sessions: past the 20-session exit


def _one(sym):
    return lambda D, held: ([sym], {})


def test_bz_spell_is_traded_valued_at_bz_close_and_not_force_sold():
    c, days = _t4t_db({"BZS": MOVES_AND_BACK})
    end = days[-1]
    cfg = engine.Config(n=1, buffer_mult=1, sector_cap=None, cost=0.0, initial_capital=1000.0)
    panel = engine.build_panel(c, ["BZS"], days[2], end, is_end=end)
    s = panel.col("BZS")
    assert panel.traded[:, s].all()
    assert not panel.buyable[5:30, s].any() and panel.buyable[:5, s].all()
    assert panel.bz_only[5:30, s].all() and panel.no_trade_run[:, s].max() == 0
    assert np.allclose(panel.close[5:30, s], BZ_CLOSE) and np.allclose(panel.open_[5:30, s], BZ_OPEN)

    res = engine.simulate(panel, [days[2]], _one("BZS"), cfg, is_end=end)
    assert list(res.trades["side"]) == ["buy"]                    # no forced exit
    nav = res.nav.set_index(["date", "mark"])["nav"]
    assert nav[(days[10], "close")] == pytest.approx(10 * BZ_CLOSE)  # 10 units at the BZ close
    assert nav[(days[29], "close")] == pytest.approx(10 * BZ_CLOSE)
    assert nav[(end, "open")] == pytest.approx(10 * EQ_AFTER)        # back in EQ
    bzh = engine.bz_holdings(panel, res)
    assert len(bzh) == 1 and bzh.iloc[0]["bz_sessions"] == 25
    assert bzh.iloc[0]["first_bz"] == days[5].date() and bzh.iloc[0]["last_bz"] == days[29].date()
    assert not bzh.iloc[0]["exit_fill_in_bz"]

    # Without the rule (Clarification 15 alone) the same holding is sold at the
    # close of the 20th session without an EQ/BE trade, at its last EQ close.
    old = engine.build_panel(c, ["BZS"], days[2], end, is_end=end, trade_for_trade=False)
    assert not old.bz_only.any()
    r0 = engine.simulate(old, [days[2]], _one("BZS"), cfg, is_end=end)
    forced = r0.trades[r0.trades["side"] == "sell"]
    assert list(forced["date"]) == [days[24].date()] and forced["price"].iloc[0] == EQ_BEFORE
    assert r0.nav["nav"].iloc[-1] == pytest.approx(1000.0)


def test_sale_on_a_bz_session_fills_at_the_bz_open_with_the_same_cost():
    c, days = _t4t_db({"BZS": MOVES_AND_BACK, "OTH": "E" * 40})
    end = days[-1]
    cfg = engine.Config(n=1, buffer_mult=1, sector_cap=None, cost=0.002, initial_capital=1000.0)
    panel = engine.build_panel(c, ["BZS", "OTH"], days[2], end, is_end=end)
    pick = {days[2]: "BZS", days[8]: "OTH"}
    res = engine.simulate(panel, [days[2], days[8]], lambda D, h: ([pick[D]], {}), cfg, is_end=end)
    tr = res.trades
    units = 1000.0 / 1.002 / EQ_BEFORE
    sell = tr[(tr["symbol"] == "BZS") & (tr["side"] == "sell")].iloc[0]
    assert sell["date"] == days[8].date() and sell["price"] == BZ_OPEN
    assert sell["units"] == pytest.approx(units)
    assert sell["value"] == pytest.approx(units * BZ_OPEN)
    assert sell["cost"] == pytest.approx(units * BZ_OPEN * 0.002)
    buy = tr[(tr["symbol"] == "OTH") & (tr["side"] == "buy")].iloc[0]
    assert buy["date"] == days[8].date()                          # same session, after the sale
    bzh = engine.bz_holdings(panel, res)
    assert bzh.set_index("symbol").at["BZS", "exit_fill_in_bz"]


def test_nothing_is_bought_in_bz():
    back_soon = "E" * 5 + "B" * 5 + "E" * 30                      # EQ again on session 10
    c, days = _t4t_db({"BZS": MOVES_AND_BACK, "BZ2": back_soon})
    end = days[-1]
    cfg = engine.Config(n=1, buffer_mult=1, sector_cap=None, cost=0.0, initial_capital=1000.0)
    panel = engine.build_panel(c, ["BZS", "BZ2"], days[2], end, is_end=end)

    # Ordered at D = session 7 while in BZ: waits, and fills at the first EQ open.
    r = engine.simulate(panel, [days[7]], _one("BZ2"), cfg, is_end=end)
    buys = r.trades[r.trades["side"] == "buy"]
    assert list(buys["date"]) == [days[10].date()] and buys["price"].iloc[0] == EQ_AFTER

    # Still in BZ after the 5-session window: cancelled, never bought.
    r = engine.simulate(panel, [days[7]], _one("BZS"), cfg, is_end=end)
    assert list(r.trades["side"]) == ["cancel"] and r.trades["date"].iloc[0] == days[12].date()
    assert np.allclose(r.nav["nav"], 1000.0)

    # A kept holding's top-up is a buy too: on a BZ session it waits for EQ.
    pick = {days[2]: ["BZ2", "BZS"], days[7]: ["BZ2"]}
    cfg2 = engine.Config(n=2, buffer_mult=1, sector_cap=None, cost=0.0, initial_capital=1000.0)
    r = engine.simulate(panel, [days[2], days[7]], lambda D, h: (pick[D], {}), cfg2, is_end=end)
    tr = r.trades[r.trades["date"] > days[2].date()]
    sold = tr[tr["side"] == "sell"]
    topup = tr[(tr["side"] == "buy") & (tr["symbol"] == "BZ2")]
    assert list(sold["symbol"]) == ["BZS"] and sold["date"].iloc[0] == days[7].date()
    assert sold["price"].iloc[0] == BZ_OPEN
    assert list(topup["date"]) == [days[10].date()]


def test_a_stock_that_stops_trading_is_still_force_sold_at_its_last_close():
    gone = "E" * 5 + "." * 35
    gone_from_bz = "E" * 5 + "B" * 5 + "." * 30
    c, days = _t4t_db({"GONE": gone, "GONEBZ": gone_from_bz})
    end = days[-1]
    cfg = engine.Config(n=1, buffer_mult=1, sector_cap=None, cost=0.002, delist_haircut=0.5,
                        initial_capital=1000.0)
    panel = engine.build_panel(c, ["GONE", "GONEBZ"], days[2], end, is_end=end)
    for sym, t_exit, last_close in (("GONE", 24, EQ_BEFORE), ("GONEBZ", 29, BZ_CLOSE)):
        r = engine.simulate(panel, [days[2]], _one(sym), cfg, is_end=end)
        sell = r.trades[r.trades["side"] == "sell"]
        assert list(sell["date"]) == [days[t_exit].date()], sym
        assert sell["price"].iloc[0] == pytest.approx(last_close * 0.5)
        assert sell["reason"].iloc[0] == "no trade for 20 sessions"
        assert sell["cost"].iloc[0] == pytest.approx(sell["value"].iloc[0] * 0.002)
        assert r.episodes["exit_reason"].tolist() == ["no_trade_exit"]


def test_bz_rows_map_to_stock_ids_and_an_eq_row_wins():
    """OLD renames to NEW on one ISIN at session 10; NEW trades in BZ on
    sessions 12-15 and in both EQ and BZ on 16. The BZ rows join the one
    stock id; on 16 the EQ row is the price and the session is buyable."""
    from zen.universe.identity import Identity, link
    n = 40
    old = "E" * 10 + "." * 30
    new = "." * 10 + "EE" + "BBBB" + "2" + "E" * 23
    isin = {"OLD": "INE777A01011", "NEW": "INE777A01011"}
    c, days = _t4t_db({"OLD": old, "NEW": new}, isin=isin, n=n)
    keys = c.execute("SELECT symbol, isin_code AS isin, min(date) AS first, max(date) AS last "
                     "FROM prices GROUP BY 1, 2").df()
    ids = Identity(link(keys, pd.DatetimeIndex(days)))
    assert set(ids.spells.loc[ids.spells["symbol"].isin(["OLD", "NEW"]), "cid"]) == {"NEW"}
    end = days[-1]
    panel = engine.build_panel(c, ["NEW"], days[2], end, is_end=end, ids=ids)
    s = panel.col("NEW")
    assert panel.traded[:, s].all()
    assert panel.bz_only[12:16, s].all() and panel.bz_only[:, s].sum() == 4
    assert panel.buyable[16, s] and panel.close[16, s] == EQ_AFTER    # not the BZ print
    assert np.allclose(panel.close[12:16, s], BZ_CLOSE)
    # the equal-weight benchmark follows the same rule
    ranks = pd.DataFrame({"D": [days[2]], "symbol": ["NEW"], "rank": [1], "composite": [1.0],
                          "sector": [None], "sector_source": ["none"]})
    ew = engine.run_universe_ew(panel, ranks, [days[2]], engine.Config(cost=0.0), is_end=end)
    bzh = engine.bz_holdings(panel, ew)
    assert bzh["bz_sessions"].tolist() == [4]
    assert ew.episodes["exit_reason"].tolist() == ["open_at_end"]


def test_no_prices_other_table_is_an_error_not_a_silent_skip():
    c = duckdb.connect(":memory:")
    c.execute(store.SCHEMA)
    c.execute("CREATE TABLE corpactions (ex_date DATE, symbol VARCHAR, isin VARCHAR, "
              "company VARCHAR, action VARCHAR, subject VARCHAR, factor DOUBLE)")
    d = pd.Timestamp("2021-01-04")
    c.execute("INSERT INTO prices VALUES (?, 'A', 'EQ', 'INE1', 1, 1, 1, 1, NULL, 1, 1, 1)", [d.date()])
    with pytest.raises(duckdb.CatalogException):
        engine.build_panel(c, ["A"], d, d, is_end=d)
    engine.build_panel(c, ["A"], d, d, is_end=d, trade_for_trade=False)


# ---------------------------------------------------------------- (b) factor-index benchmarks
FAC, PARENT = "NIFTY200 MOMENTUM 30", "Nifty 200"
TRI_LEVELS = [1000, 1000, 1010, 1010, 1020, 1020, 1030, 5000.0]   # the end date's is never read
PARENT_OPEN = {1: 105.0, 4: 104.0, 7: 103.0}                       # after a 100 close
OWN = {1: (202.0, 200.0), 4: (408.0, 400.0), 7: (721.0, 700.0)}    # (open on d, close on d-1)


def _factor_setup(tmp_path, dash=(), drop=()):
    """Eight sessions d0..d7; the clock is open of d1 to open of d7, with an
    interior open mark at d4. `dash`: endpoint sessions where NSE printed no
    open. `drop`: session indices whose endpoint rows are absent."""
    days = pd.bdate_range("2021-01-04", periods=8)
    c = duckdb.connect(":memory:")
    c.execute("CREATE TABLE indices (index_name VARCHAR, date DATE, open DOUBLE, close DOUBLE)")
    rows = [(PARENT, d.date(), PARENT_OPEN.get(i, 100.0), 100.0 if i < 7 else 999.0)
            for i, d in enumerate(days)]
    c.executemany("INSERT INTO indices VALUES (?,?,?,?)", rows)
    tri = pd.DataFrame({"index_name": FAC, "date": days, "tri": TRI_LEVELS, "net_tri": 0.0})
    tp = tmp_path / "tri.parquet"
    tri.to_parquet(tp)
    fmt = lambda d: d.strftime("%d %b %Y")
    lines = ['"IndexName","Date","Open","High","Low","Close"']
    for i, (o, pc) in OWN.items():
        if i not in drop:
            op = "-" if i in dash else f"{o:.2f}"
            lines.append(f'"{FAC}","{fmt(days[i])}","{op}","{op}","{op}","{o + 1:.2f}"')
        if i - 1 not in drop:
            lines.append(f'"{FAC}","{fmt(days[i - 1])}","1.00","1.00","1.00","{pc:.2f}"')
    ep = tmp_path / "endpoints.csv"
    ep.write_text("\n".join(lines) + "\n")
    return c, days, tp, ep


def _fnav(c, days, tp, ep, overnight=True, is_end=None):
    return engine.factor_tri_nav(c, FAC, PARENT, days[1], days[7],
                                 is_end=days[7] if is_end is None else is_end,
                                 overnight=overnight, path=tp, endpoints_path=ep)


def _fmark(c, days, tp, ep, d, overnight=True):
    return engine.tri_value_at_open(c, FAC, PARENT, days[1], d, days[7], is_end=days[7],
                                    factor=True, overnight=overnight, path=tp, endpoints_path=ep)


def test_factor_index_uses_nses_own_open_and_prior_close(tmp_path):
    c, days, tp, ep = _factor_setup(tmp_path)
    nav = _fnav(c, days, tp, ep)
    start = 1000 * 202 / 200                            # not the parent's 105/100
    assert list(nav["mark"]) == ["open"] + ["close"] * 6 + ["open"]
    assert nav["nav"].tolist() == pytest.approx(
        [1.0] + [x / start for x in TRI_LEVELS[1:7]] + [1030 * 721 / 700 / start])
    # where NSE's open exists the no-overnight column is identical
    assert _fnav(c, days, tp, ep, overnight=False).equals(nav)
    assert _fmark(c, days, tp, ep, days[4]) == pytest.approx(1010 * 408 / 400 / start)
    assert _fmark(c, days, tp, ep, days[7]) == nav["nav"].iloc[-1]


def test_factor_index_without_an_open_takes_the_parents_overnight_move(tmp_path):
    c, days, tp, ep = _factor_setup(tmp_path, dash=(1, 4))
    nav = _fnav(c, days, tp, ep)
    start = 1000 * 105 / 100                            # parent open d1 / parent close d0
    assert nav["nav"].iloc[1] == pytest.approx(1000 / start)
    assert nav["nav"].iloc[-1] == pytest.approx(1030 * 721 / 700 / start)   # d7 has its own open
    assert _fmark(c, days, tp, ep, days[4]) == pytest.approx(1010 * 104 / 100 / start)
    # the sensitivity: no overnight move at those endpoints only
    flat = _fnav(c, days, tp, ep, overnight=False)
    assert flat["nav"].iloc[1] == pytest.approx(1.0)
    assert flat["nav"].iloc[-1] == pytest.approx(1030 * 721 / 700 / 1000)
    assert _fmark(c, days, tp, ep, days[4], overnight=False) == pytest.approx(1010 / 1000)


def test_factor_index_refuses_a_missing_endpoint_row(tmp_path):
    assert not issubclass(engine.EndpointMissing, ValueError)   # a skip-on-ValueError cannot hide it
    for drop in ((7,), (6,), (1,)):
        c, days, tp, ep = _factor_setup(tmp_path, drop=drop)
        for overnight in (True, False):
            with pytest.raises(engine.EndpointMissing):
                _fnav(c, days, tp, ep, overnight=overnight)
    # the interior mark needs its own rows too, even when the clock's two ends are there
    c, days, tp, ep = _factor_setup(tmp_path, drop=(4,))
    _fnav(c, days, tp, ep)
    with pytest.raises(engine.EndpointMissing):
        _fmark(c, days, tp, ep, days[4])
    # a '-' open needs no prior close from the file: the parent supplies the move
    c, days, tp, ep = _factor_setup(tmp_path, dash=(1,), drop=(0,))
    assert _fnav(c, days, tp, ep)["nav"].iloc[0] == 1.0


def test_endpoints_file_is_cut_at_the_end_and_conflicts_raise(tmp_path):
    c, days, tp, ep = _factor_setup(tmp_path)
    e = engine.load_endpoints(ep, end=days[4])
    assert e.index.get_level_values("date").max() == days[4]
    assert np.isnan(e.at[(FAC, days[4]), "close"]) and e.at[(FAC, days[4]), "open"] == 408.0
    ep.write_text(ep.read_text() + f'"{FAC}","{days[4].strftime("%d %b %Y")}","409","1","1","1"\n')
    with pytest.raises(ValueError, match="conflicting"):
        engine.load_endpoints(ep)


def test_factor_index_and_open_marks_respect_the_holdout_lock(tmp_path):
    c, days, tp, ep = _factor_setup(tmp_path)
    with pytest.raises(engine.HoldoutLocked):
        _fnav(c, days, tp, ep, is_end=days[5])
    with pytest.raises(engine.HoldoutLocked):
        engine.tri_value_at_open(c, FAC, PARENT, days[1], days[4], days[7], is_end=days[5],
                                 factor=True, path=tp, endpoints_path=ep)


def test_main_index_open_mark_uses_the_price_index_move(tmp_path):
    """The three original benchmarks' value at an interior open, by tri_nav's own rule."""
    c, days, tp, ep = _factor_setup(tmp_path)
    nav = engine.tri_nav(c, FAC, PARENT, days[1], days[7], is_end=days[7], path=tp)
    start = 1000 * 105 / 100
    assert nav["nav"].iloc[-1] == pytest.approx(1030 * 103 / 100 / start)
    v = engine.tri_value_at_open(c, FAC, PARENT, days[1], days[4], days[7], is_end=days[7], path=tp)
    assert v == pytest.approx(1010 * 104 / 100 / start)
    assert engine.tri_value_at_open(c, FAC, PARENT, days[1], days[7], days[7], is_end=days[7],
                                    path=tp) == nav["nav"].iloc[-1]


NAV_COLUMNS = ["date", "mark", "strategy", "universe_ew", "nifty500", "midcap150", "smallcap250",
               "momentum30", "value50", "quality30", "lowvol30", "alpha50",
               "momentum30_no_overnight", "value50_no_overnight", "quality30_no_overnight",
               "lowvol30_no_overnight", "alpha50_no_overnight"]


def test_benchmark_columns_on_the_archive_in_sample():
    """On the real archive, in-sample only: the column names and order, and the
    no-overnight columns differ exactly where NSE printed no open (Momentum 30
    on 15 Feb 2019; Value 50 on 15 Feb 2019 and 15 Feb 2023)."""
    from jobs import backtest_v1
    con = pit.connect(read_only=True)
    try:
        is_end = engine.in_sample_end(pit.sessions(con))
        start = pd.Timestamp("2019-02-15")
        idx = backtest_v1.index_navs(con, start, is_end, is_end, False)
        opens = backtest_v1.index_opens(con, start, is_end, is_end, is_end, False)
    finally:
        con.close()
    dummy = idx["nifty500"].assign(nav=1.0)
    wide = backtest_v1.wide_nav({"strategy": dummy, "universe_ew": dummy, **idx})
    assert list(wide.columns) == NAV_COLUMNS
    assert not wide[NAV_COLUMNS[2:]].isna().any().any()
    for k in ("quality30", "lowvol30", "alpha50"):
        assert idx[k].equals(idx[k + "_no_overnight"]), k
    for k in ("momentum30", "value50"):
        assert not np.allclose(idx[k]["nav"], idx[k + "_no_overnight"]["nav"]), k
    # at the in-sample end the value at the open is the nav's last point
    for k, v in opens.items():
        assert v == idx[k]["nav"].iloc[-1], k
