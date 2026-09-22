"""Strategy v1 portfolio engine (research/strategy/v1-spec.md).

Ranks are computed once per decision date (zen.signals.composite.compute) and
reused by every portfolio variant; a variant is a pure simulation over one
pre-built price panel, so it costs well under a second.

Mechanics, as specified:

  execution     at the open of D on adjusted prices. A stock that does not
                trade on D waits for the next session it trades, up to 5
                sessions after D, and the order is otherwise cancelled.
  sizing        equal weight at each rebalance: every name selected is sized
                to NAV(open of D) / N, kept names included (only the
                difference is traded).
  costs         a flat rate of traded value per side.
  dividends     credited as cash on the ex-date to positions held at the
                previous close, held at zero interest until the next
                rebalance. The Rs amount is parsed from the corporate-action
                subject and converted to the share units held at the ex-date,
                so a later split or bonus cannot inflate it.
  no trade      a holding with no trade for 20 consecutive sessions is sold at
                its last close (times a haircut; 0.5 in the harsh sensitivity).
  NAV           daily at the close; the final point is the open of the
                in-sample end date, which is the first session on or after
                15 Feb 2023.

HOLDOUT LOCK. Nothing here computes a return after the in-sample end unless
`run_final_test=True` is passed explicitly. Every price load, index load and
simulation checks `guard()`; the default is locked.

Adjusted units. A position is held in "adjusted units": value = units x
adj_price, where adj_price(d) = raw(d) x cum(d) and cum(d) is the product of
split/bonus factors with d < ex_date <= end. Raw shares held on d are
units x cum(d). A split therefore leaves units, value and return unchanged,
which tests/test_backtest_v1.py proves on a synthetic split.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from zen.universe import pit

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- holdout lock
class HoldoutLocked(RuntimeError):
    """Raised when anything would compute a return after the in-sample end."""


IN_SAMPLE_END_ANCHOR = (2023, 2, 15)     # the Feb 2023 decision date's anchor


def in_sample_end(cal: pd.DatetimeIndex) -> pd.Timestamp:
    """The open of the Feb 2023 decision date: the last instant returns may be measured to."""
    return pit.decision_date(cal, *IN_SAMPLE_END_ANCHOR)


def guard(end, is_end: pd.Timestamp, run_final_test: bool = False) -> None:
    end = pd.Timestamp(end).normalize()
    if end > pd.Timestamp(is_end).normalize() and not run_final_test:
        raise HoldoutLocked(
            f"refusing to compute returns through {end.date()}: the in-sample period "
            f"ends at the open of {pd.Timestamp(is_end).date()}. The holdout is run once, "
            "after the in-sample work is reviewed, with an explicit unlock.")


# ---------------------------------------------------------------- config
@dataclass(frozen=True)
class Config:
    n: int = 10
    buffer_mult: int = 2              # keep while rank <= buffer_mult * n (1 = no buffer)
    rebalance: str = "quarterly"      # or "half" (Jun and Nov decision dates)
    sector_cap: int | None = 3
    cost: float = 0.002               # per side, fraction of traded value
    delist_haircut: float = 1.0       # 0.5 = sell at half the last close
    wait_sessions: int = 5
    no_trade_exit: int = 20
    initial_capital: float = 5e5

    def label(self) -> str:
        cap = self.sector_cap if self.sector_cap else "none"
        return (f"N{self.n}_buf{self.buffer_mult}x_{self.rebalance}_cap{cap}"
                f"_c{self.cost:g}_h{self.delist_haircut:g}")


BASE = Config()


def rebalance_dates(decisions: list[pd.Timestamp], how: str) -> list[pd.Timestamp]:
    """Quarterly uses every decision date. Half-yearly trades on Jun and Nov dates,
    after the initial build on the first decision date (so every variant covers
    the same in-sample window)."""
    if how == "quarterly":
        return list(decisions)
    if how == "half":
        return [decisions[0]] + [d for d in decisions[1:] if d.month in (6, 11)]
    raise ValueError(how)


# ---------------------------------------------------------------- dividends
_SEG = re.compile(r"/|\+|\band\b|\bplus\b", re.I)
_DIV_NUM = re.compile(r"dividend\D*?(\d+(?:\.\d+)?)(\s*%)?", re.I)


def parse_dividend(subject: str) -> float | None:
    """Rs per share from a corporate-action subject; None if not a cash equity dividend.

    'Interim Dividend - Rs 3.50 Per Share' -> 3.5
    'Final Dividend - Rs 8 Per Share And Special Dividend - Rs 10 Per Share' -> 18
    Percent-of-face-value dividends and REIT/InvIT unit distributions are skipped.
    """
    s = subject or ""
    if "dividend" not in s.lower() or re.search(r"distribution|per\s+unit", s, re.I):
        return None
    amts = []
    for seg in _SEG.split(s):
        if "dividend" not in seg.lower():
            continue
        # A segment can hold two clauses with no separator:
        # 'Interim Dividend - Rs 6 Per Share Special Interim Dividend - Rs 10 Per Share'
        # (HCLTECH, 2021-04-29) is Rs 16, not Rs 6 (Clarification 13).
        for m in _DIV_NUM.finditer(seg):
            if m.group(2):
                continue
            tok = m.group(1)
            if re.fullmatch(r"0\d+", tok):      # 'Rs.0125' -> 0.125
                tok = "0." + tok[1:]
            amts.append(float(tok))
    return sum(amts) if amts else None


def dividends(con, start, end, ids=None) -> pd.DataFrame:
    """Cash dividends (symbol, ex_date, dps) with start <= ex_date <= end.

    With `ids` (identity.Identity) the symbol is the stock id (Clarification 23)."""
    ca = con.execute(
        "SELECT symbol, ex_date, subject FROM corpactions "
        "WHERE ex_date >= ? AND ex_date <= ? AND lower(subject) LIKE '%dividend%'",
        [pd.Timestamp(start).date(), pd.Timestamp(end).date()]).df()
    if ca.empty:
        return pd.DataFrame(columns=["symbol", "ex_date", "dps"])
    ca["dps"] = ca["subject"].map(parse_dividend)
    ca = ca[ca["dps"].notna() & (ca["dps"] > 0)].copy()
    if ids is not None and not ca.empty:
        ca["symbol"] = ids.for_events(ca, "symbol", "ex_date")
    # A 'Purpose Revised' re-announcement repeats the same amount on the same day.
    ca = ca.drop_duplicates(["symbol", "ex_date", "dps"])
    out = ca.groupby(["symbol", "ex_date"], as_index=False)["dps"].sum()
    out["ex_date"] = pd.to_datetime(out["ex_date"])
    return out


# ---------------------------------------------------------------- panel
@dataclass
class Panel:
    dates: pd.DatetimeIndex
    symbols: pd.Index
    open_: np.ndarray            # adjusted open, nan if not traded
    close: np.ndarray            # adjusted close, nan if not traded (and on the end date)
    last: np.ndarray             # adjusted last close ffilled (as of each close)
    traded: np.ndarray           # bool
    no_trade_run: np.ndarray     # consecutive sessions without a trade, through t
    div_unit: np.ndarray         # cash per adjusted unit credited on t
    end: pd.Timestamp
    dropped_dividends: pd.DataFrame = field(default_factory=pd.DataFrame)

    def col(self, sym) -> int:
        return self.symbols.get_loc(sym)


def build_panel(con, symbols, start, end, is_end, run_final_test: bool = False,
                max_dividend_yield: float = 0.5, ids=None) -> Panel:
    """Adjusted price panel for `symbols` over sessions [start - 60d, end].

    With `ids` (identity.Identity), `symbols` are stock ids and every symbol
    the stock traded under is loaded into one column, so a rename while held
    is continuous (Clarification 23).

    On the end date only the open is loaded; its close is never read.
    """
    guard(end, is_end, run_final_test)
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    lo = (start - pd.Timedelta(days=60)).date()
    syms = pd.Index(sorted(set(symbols)))
    use_ids = ids is not None and not ids.empty
    load_syms = (sorted(set(ids.spells.loc[ids.spells["cid"].isin(syms), "symbol"]) | set(syms))
                 if use_ids else list(syms))
    con.register("_panel_syms", pd.DataFrame({"symbol": load_syms}))
    px = con.execute(
        "SELECT p.symbol, p.isin_code, p.date, p.open, p.turnover, "
        "CASE WHEN p.date = ? THEN NULL ELSE p.close END AS close "
        "FROM prices p JOIN _panel_syms s USING (symbol) "
        "WHERE p.date >= ? AND p.date <= ? AND (p.date = ? OR p.close > 0)",
        [end.date(), lo, end.date(), end.date()]).df()
    con.unregister("_panel_syms")
    px["date"] = pd.to_datetime(px["date"])
    if use_ids:
        px["symbol"] = ids.for_prices(px)
        px = px[px["symbol"].isin(syms)]
        px = (px.sort_values(["turnover", "isin_code"], ascending=[False, True], na_position="last")
                .drop_duplicates(["symbol", "date"]))
    dates = pd.DatetimeIndex(pd.to_datetime(con.execute(
        "SELECT DISTINCT date FROM prices WHERE date >= ? AND date <= ? ORDER BY date",
        [lo, end.date()]).df()["date"]))

    fac = pit.split_factors(con, through=end, ids=ids if use_ids else None)
    fac = fac[fac["symbol"].isin(syms)]
    px["cum"] = pit.cumulative_factor(px, fac)
    px["aopen"] = px["open"] * px["cum"]
    px["aclose"] = px["close"] * px["cum"]

    T, S = len(dates), len(syms)
    ti = dates.get_indexer(px["date"])
    si = syms.get_indexer(px["symbol"])
    open_ = np.full((T, S), np.nan)
    close = np.full((T, S), np.nan)
    traded = np.zeros((T, S), dtype=bool)
    open_[ti, si] = px["aopen"].to_numpy()
    close[ti, si] = px["aclose"].to_numpy()
    traded[ti, si] = True
    # A zero or missing open on a traded day falls back to that day's close.
    bad = traded & ~(open_ > 0)
    open_[bad] = close[bad]
    last = pd.DataFrame(close).ffill().to_numpy()

    run = np.zeros((T, S), dtype=np.int32)
    for t in range(T):
        run[t] = 0 if t == 0 else np.where(traded[t], 0, run[t - 1] + 1)
    run[0] = np.where(traded[0], 0, 1)

    # Dividends in adjusted units: cash = raw shares x dps = units x cum(ex) x dps.
    dv = dividends(con, dates[0], end, ids if use_ids else None)
    dv = dv[dv["symbol"].isin(syms)].copy()
    div_unit = np.zeros((T, S))
    dropped = pd.DataFrame()
    if not dv.empty:
        dv["cum"] = pit.cumulative_factor(dv.rename(columns={"ex_date": "date"}), fac)
        dv["t"] = dates.searchsorted(dv["ex_date"], side="left")
        dv = dv[dv["t"] < T]
        dv["s"] = syms.get_indexer(dv["symbol"])
        # Implausible amounts (a parse error such as 'Rs.0125' read as 125) are
        # dropped when the dividend exceeds half the last raw close before the ex-date.
        prev_t = np.maximum(dv["t"].to_numpy() - 1, 0)
        prev_raw = last[prev_t, dv["s"].to_numpy()] / dv["cum"].to_numpy()
        ok = ~(dv["dps"].to_numpy() > max_dividend_yield * prev_raw)
        dropped = dv[~ok]
        dv = dv[ok]
        np.add.at(div_unit, (dv["t"].to_numpy(), dv["s"].to_numpy()),
                  (dv["dps"] * dv["cum"]).to_numpy())
    return Panel(dates=dates, symbols=syms, open_=open_, close=close, last=last,
                 traded=traded, no_trade_run=run, div_unit=div_unit, end=end,
                 dropped_dividends=dropped)


def daily_returns_check(panel: Panel, limit: float = 0.6) -> pd.DataFrame:
    """Every traded close-to-close adjusted return beyond +/- limit, for the sanity report."""
    prev = np.vstack([np.full(panel.last.shape[1], np.nan), panel.last[:-1]])
    r = panel.close / prev - 1
    t, s = np.where(np.abs(np.nan_to_num(r)) > limit)
    return pd.DataFrame({"date": panel.dates[t], "symbol": panel.symbols[s],
                         "ret": r[t, s], "prev_adj_close": prev[t, s],
                         "adj_close": panel.close[t, s]})


# ---------------------------------------------------------------- selection
def select_top(ranked: pd.DataFrame, held: set, cfg: Config) -> list[str]:
    """Buffer then fill, respecting the sector cap. `ranked` is one D, indexed by symbol."""
    rank = ranked["rank"]
    sector = ranked["sector"] if "sector" in ranked else pd.Series(index=ranked.index, dtype=object)
    keep_lim = cfg.buffer_mult * cfg.n
    keep = sorted([s for s in held if s in rank.index and rank[s] <= keep_lim],
                  key=lambda s: rank[s])[:cfg.n]
    counts = Counter(sector.get(s) for s in keep if pd.notna(sector.get(s)))
    chosen = list(keep)
    for s in rank.sort_values().index:
        if len(chosen) >= cfg.n:
            break
        if s in chosen:
            continue
        sec = sector.get(s)
        # An unlabelled company is its own sector: the cap cannot bind on it.
        if cfg.sector_cap and pd.notna(sec) and counts[sec] >= cfg.sector_cap:
            continue
        chosen.append(s)
        if pd.notna(sec):
            counts[sec] += 1
    return chosen


# ---------------------------------------------------------------- simulation
@dataclass
class Result:
    nav: pd.DataFrame                # date, mark (open|close), nav: open of first D, closes, open of end
    trades: pd.DataFrame
    holdings: pd.DataFrame           # per rebalance D: target book
    episodes: pd.DataFrame           # per holding episode
    cash: pd.Series


def simulate(panel: Panel, rebal: list[pd.Timestamp], choose, cfg: Config,
             is_end: pd.Timestamp, run_final_test: bool = False,
             log_trades: bool = True) -> Result:
    """Run one portfolio.

    choose(D, held:set) -> (list of target symbols, dict of extra per-symbol info)
    """
    guard(panel.end, is_end, run_final_test)
    dates, syms = panel.dates, panel.symbols
    T, S = len(dates), len(syms)
    c = cfg.cost
    t0 = dates.get_indexer([rebal[0]])[0]
    rebal_idx = {dates.get_indexer([d])[0]: d for d in rebal}
    if min(rebal_idx) < 0:
        raise ValueError("a rebalance date is not a session in the panel")
    t_end = T - 1

    units = np.zeros(S)
    cash = float(cfg.initial_capital)
    tgt_val = np.full(S, np.nan)       # pending order target value (rupees)
    deadline = np.full(S, -1)
    entry_t = np.full(S, -1)
    entry_px = np.full(S, np.nan)

    nav_out = np.full(T, np.nan)
    cash_out = np.full(T, np.nan)
    trades, books, episodes = [], [], []

    def close_episode(s, t, reason):
        episodes.append({"symbol": syms[s], "entry": dates[entry_t[s]], "exit": dates[t],
                         "entry_px": entry_px[s], "entry_t": entry_t[s], "exit_t": t,
                         "exit_reason": reason})
        entry_t[s] = -1
        entry_px[s] = np.nan

    for t in range(t0, T):
        # 1. dividends to positions held at the previous close
        if t > t0:
            cash += float(units @ panel.div_unit[t])

        # 2. rebalance decision at the open of D
        if t in rebal_idx:
            D = rebal_idx[t]
            px_now = np.where(panel.traded[t], panel.open_[t],
                              panel.last[t - 1] if t > 0 else np.nan)
            nav_open = cash + float(np.nansum(units * px_now))
            held = {syms[s] for s in np.flatnonzero(units > 0)}
            target, info = choose(D, held)
            tset = set(target)
            tgt_val[:] = np.nan
            deadline[:] = -1
            per = nav_open / len(target) if target else 0.0
            for sym in set(target) | held:
                s = syms.get_loc(sym)
                tgt_val[s] = per if sym in tset else 0.0
                deadline[s] = t + cfg.wait_sessions
            for sym in target:
                books.append({"D": D.date(), "symbol": sym,
                              "action": "keep" if sym in held else "buy",
                              "weight": 1.0 / len(target), **info.get(sym, {})})
            for sym in held - tset:
                books.append({"D": D.date(), "symbol": sym, "action": "sell",
                              "weight": 0.0, **info.get(sym, {})})

        # 3. execute pending orders for names trading at this open
        live = ~np.isnan(tgt_val)
        if live.any():
            ex = live & panel.traded[t]
            if t == t_end:
                ex[:] = False           # the end date is a mark at the open, not a trading session
            if ex.any():
                op = panel.open_[t]
                cur = units * op
                delta = np.where(ex, tgt_val - np.nan_to_num(cur), 0.0)
                sells = ex & (delta < 0)
                for s in np.flatnonzero(sells):
                    val = -delta[s]
                    if tgt_val[s] == 0.0:
                        du = units[s]
                        val = du * op[s]
                    else:
                        du = val / op[s]
                    units[s] -= du
                    if tgt_val[s] == 0.0:
                        units[s] = 0.0
                    cash += val * (1 - c)
                    if log_trades:
                        trades.append({"date": dates[t].date(), "symbol": syms[s], "side": "sell",
                                       "units": du, "price": op[s], "value": val,
                                       "cost": val * c, "reason": "rebalance"})
                    if units[s] == 0.0 and entry_t[s] >= 0:
                        close_episode(s, t, "rebalance")
                buys = ex & (delta > 0)
                need = delta[buys].sum() * (1 + c)
                scale = min(1.0, cash / need) if need > 0 else 0.0
                for s in np.flatnonzero(buys):
                    val = delta[s] * scale
                    if val <= 0:
                        continue
                    du = val / op[s]
                    if units[s] == 0 and entry_t[s] < 0:
                        entry_t[s] = t
                        entry_px[s] = op[s]
                    units[s] += du
                    cash -= val * (1 + c)
                    if log_trades:
                        trades.append({"date": dates[t].date(), "symbol": syms[s], "side": "buy",
                                       "units": du, "price": op[s], "value": val,
                                       "cost": val * c, "reason": "rebalance"})
                tgt_val[ex] = np.nan
                deadline[ex] = -1
            expired = ~np.isnan(tgt_val) & (deadline <= t)
            if expired.any() and log_trades:
                for s in np.flatnonzero(expired):
                    trades.append({"date": dates[t].date(), "symbol": syms[s],
                                   "side": "cancel", "units": 0.0, "price": np.nan,
                                   "value": 0.0, "cost": 0.0,
                                   "reason": f"no trade within {cfg.wait_sessions} sessions"})
            tgt_val[expired] = np.nan
            deadline[expired] = -1

        if t == t_end:
            # Final mark at the open of the in-sample end date. Its close is never read.
            ok = panel.traded[t] & (panel.open_[t] > 0)
            px_mark = np.where(ok, panel.open_[t], panel.last[t - 1])
            nav_out[t] = cash + float(np.nansum(units * px_mark))
            cash_out[t] = cash
            break

        # 4. forced exit after 20 sessions without a trade, at the last close x haircut
        stuck = (units > 0) & (panel.no_trade_run[t] >= cfg.no_trade_exit)
        for s in np.flatnonzero(stuck):
            val = units[s] * panel.last[t, s] * cfg.delist_haircut
            cash += val * (1 - c)
            if log_trades:
                trades.append({"date": dates[t].date(), "symbol": syms[s], "side": "sell",
                               "units": units[s], "price": panel.last[t, s] * cfg.delist_haircut,
                               "value": val, "cost": val * c,
                               "reason": f"no trade for {cfg.no_trade_exit} sessions"})
            units[s] = 0.0
            tgt_val[s] = np.nan
            if entry_t[s] >= 0:
                close_episode(s, t, "no_trade_exit")

        # 5. mark at the close
        nav_out[t] = cash + float(np.nansum(units * panel.last[t]))
        cash_out[t] = cash

    for s in np.flatnonzero(entry_t >= 0):
        episodes.append({"symbol": syms[s], "entry": dates[entry_t[s]], "exit": dates[t_end],
                         "entry_px": entry_px[s], "entry_t": entry_t[s], "exit_t": t_end,
                         "exit_reason": "open_at_end"})
    ep = pd.DataFrame(episodes)
    if not ep.empty:
        # Doubled while held: any adjusted close in the episode >= 2x the entry price.
        mx = []
        for r in ep.itertuples():
            s = syms.get_loc(r.symbol)
            seg = panel.close[r.entry_t:r.exit_t + 1, s]
            mx.append(np.nanmax(seg) if np.isfinite(seg).any() else np.nan)
        ep["max_close"] = mx
        ep["doubled"] = ep["max_close"] >= 2 * ep["entry_px"]
        ep["days_held"] = (ep["exit"] - ep["entry"]).dt.days
    nav = nav_frame(dates[t0], cfg.initial_capital, dates[t0:t_end], nav_out[t0:t_end],
                    dates[t_end], nav_out[t_end])
    return Result(nav=nav, trades=pd.DataFrame(trades), holdings=pd.DataFrame(books),
                  episodes=ep, cash=pd.Series(cash_out[t0:t_end + 1], index=dates[t0:t_end + 1]))


def nav_frame(start, start_value, close_dates, close_values, end, end_value) -> pd.DataFrame:
    """NAV on the strategy clock: open of the first D, each close, open of the end date."""
    rows = ([(pd.Timestamp(start), "open", float(start_value))] +
            [(d, "close", float(v)) for d, v in zip(close_dates, close_values)] +
            [(pd.Timestamp(end), "open", float(end_value))])
    return pd.DataFrame(rows, columns=["date", "mark", "nav"])


# ---------------------------------------------------------------- ranks & runs
def build_ranks(con, decisions, static=None):
    """Ranks and funnel for every decision date. Computed once; reused by all variants."""
    from zen.signals import composite
    frames, funnels, dq = [], [], []
    for D in decisions:
        r, f = composite.compute(con, D, static)
        dq.append(r.attrs.get("data_quality", pd.DataFrame()))
        r.attrs = {}
        frames.append(r.reset_index())
        funnels.append(pit.funnel_frame(D, f))
        log.info("%s: universe %d", pd.Timestamp(D).date(), len(r))
    ranks = pd.concat(frames, ignore_index=True)
    ranks.attrs["data_quality"] = pd.concat(dq, ignore_index=True) if dq else pd.DataFrame()
    return ranks, pd.concat(funnels, ignore_index=True)


def _by_date(ranks: pd.DataFrame) -> dict:
    return {pd.Timestamp(D): g.set_index("symbol") for D, g in ranks.groupby("D")}


def run_strategy(panel: Panel, ranks: pd.DataFrame, decisions, cfg: Config,
                 is_end, run_final_test: bool = False, log_trades: bool = True) -> Result:
    byd = _by_date(ranks)
    reb = rebalance_dates(decisions, cfg.rebalance)

    def choose(D, held):
        r = byd[pd.Timestamp(D)]
        tgt = select_top(r, held, cfg)
        info = {}
        for s in set(tgt) | held:
            if s in r.index:
                info[s] = {"ticker": r.at[s, "ticker"] if "ticker" in r else s,
                           "rank": int(r.at[s, "rank"]), "sector": r.at[s, "sector"],
                           "sector_source": r.at[s, "sector_source"],
                           "composite": float(r.at[s, "composite"])}
            else:
                info[s] = {"rank": None, "sector": None, "sector_source": None,
                           "composite": None}
        return tgt, info

    return simulate(panel, reb, choose, cfg, is_end, run_final_test, log_trades)


def run_universe_ew(panel, ranks, decisions, cfg: Config, is_end,
                    run_final_test: bool = False) -> Result:
    """Every stock in the universe at D, equal weight, same dates and costs as cfg."""
    byd = _by_date(ranks)
    reb = rebalance_dates(decisions, cfg.rebalance)
    ew = Config(n=10**6, buffer_mult=1, rebalance=cfg.rebalance, sector_cap=None,
                cost=cfg.cost, delist_haircut=cfg.delist_haircut,
                initial_capital=cfg.initial_capital)
    return simulate(panel, reb, lambda D, held: (list(byd[pd.Timestamp(D)].index), {}),
                    ew, is_end, run_final_test, log_trades=False)


def run_quantile(panel, ranks, decisions, score_col: str, q: int, is_end,
                 n_q: int = 5, run_final_test: bool = False) -> Result:
    """Diagnostic: quantile q (1 = best) of score_col across the universe; no buffer, no costs."""
    byd = _by_date(ranks)

    def choose(D, held):
        r = byd[pd.Timestamp(D)]
        order = r.reset_index().sort_values([score_col, "symbol"], ascending=[False, True])
        buckets = np.array_split(order["symbol"].to_numpy(), n_q)
        return list(buckets[q - 1]), {}

    cfg = Config(n=10**6, buffer_mult=1, sector_cap=None, cost=0.0)
    return simulate(panel, list(decisions), choose, cfg, is_end, run_final_test,
                    log_trades=False)


def index_nav(con, name: str, start, end, is_end, run_final_test: bool = False) -> pd.DataFrame:
    """Price index NAV on the strategy's clock: open of start, daily closes, open of end."""
    guard(end, is_end, run_final_test)
    df = con.execute(
        "SELECT date, open, close FROM indices WHERE index_name = ? AND date >= ? AND date <= ? "
        "ORDER BY date", [name, pd.Timestamp(start).date(), pd.Timestamp(end).date()]).df()
    df["date"] = pd.to_datetime(df["date"])
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    d = df.set_index("date")
    if start not in d.index or end not in d.index:
        raise ValueError(f"{name}: no index row on {start.date()} or {end.date()}")
    body = d[d.index < end]
    nav = nav_frame(start, d.at[start, "open"], body.index, body["close"].to_numpy(),
                    end, d.at[end, "open"])        # the end date's close is never read
    nav["nav"] = nav["nav"] / nav["nav"].iloc[0]
    return nav


TRI_PATH = pit.REPO / "data" / "external" / "nifty_tri.parquet"


def tri_nav(con, tri_name: str, price_name: str, start, end, is_end,
            run_final_test: bool = False, path=TRI_PATH) -> pd.DataFrame:
    """NSE's official Total Returns Index on the strategy's clock (Clarification 17).

    Closes are the `tri` column (gross total return) of data/external/
    nifty_tri.parquet. The TRI is published at the close only, so the two
    opens on the clock (the first decision date and the in-sample end) are
    the previous session's TRI carried by the price index's overnight move:

        TRI_open(d) = TRI_close(d-1) * price_open(d) / price_close(d-1)

    No dividend is reinvested overnight in that step, which is the TRI's own
    convention (it reinvests at the close of the ex-date). The end date's TRI
    close is never read.
    """
    guard(end, is_end, run_final_test)
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    lo = start - pd.Timedelta(days=15)
    # rows on or after the end date are never loaded (holdout lock)
    t = pd.read_parquet(path, columns=["index_name", "date", "tri"],
                        filters=[("index_name", "==", tri_name), ("date", "<", end)])
    t["date"] = pd.to_datetime(t["date"]).astype("datetime64[ns]")
    t = t[(t["date"] >= lo) & (t["date"] < end)].set_index("date")["tri"]
    px = con.execute(
        "SELECT date, open, close FROM indices WHERE index_name = ? AND date >= ? AND date <= ? "
        "ORDER BY date", [price_name, lo.date(), end.date()]).df()
    px["date"] = pd.to_datetime(px["date"])
    px = px.set_index("date")
    if start not in px.index or end not in px.index:
        raise ValueError(f"{price_name}: no index row on {start.date()} or {end.date()}")

    def open_level(d):
        prev = px.index[px.index < d]
        if len(prev) == 0 or prev[-1] not in t.index:
            raise ValueError(f"{tri_name}: no TRI close before {d.date()}")
        p = prev[-1]
        return float(t[p] * px.at[d, "open"] / px.at[p, "close"])

    body_dates = px.index[(px.index >= start) & (px.index < end)]
    missing = body_dates.difference(t.index)
    if len(missing):
        raise ValueError(f"{tri_name}: TRI missing on {len(missing)} sessions, first {missing[0].date()}")
    nav = nav_frame(start, open_level(start), body_dates, t.reindex(body_dates).to_numpy(),
                    end, open_level(end))
    nav["nav"] = nav["nav"] / nav["nav"].iloc[0]
    return nav


def configs_grid(cost: float = 0.002) -> list[Config]:
    out = []
    for n in (10, 15, 25):
        for b in (1, 2, 3):
            for reb in ("quarterly", "half"):
                for cap in (3, None):
                    out.append(Config(n=n, buffer_mult=b, rebalance=reb, sector_cap=cap,
                                      cost=cost))
    return out


def config_dict(cfg: Config) -> dict:
    return asdict(cfg)
