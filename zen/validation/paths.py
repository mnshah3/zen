"""Day-by-day event paths, leg-matched abnormal returns, and a fill engine.

`eventstudy.py` answers "what happened over the next twelve months". It cannot
answer "what happened on the next ten days, and could I have bought it", and
those are different questions with different ways of going wrong. This module
exists for the second one. `eventstudy.py` is left untouched.

Four things here that a naive path study gets wrong, each of which was measured
on this archive and each of which is larger than the effect being hunted:

LEG MATCHING. Every stock on this exchange gaps up overnight and bleeds
intraday, every year, with no news at all: the overnight mean runs +0.28% to
+0.57% a year and the intraday mean runs -0.26% to -0.38%, reaching -0.75% a
day in the least liquid quintile. So "buy at the close of day+3 instead of the
open of day+1" collects a third of a percent a day for free, and a study that
benchmarks an intraday move against a close-to-close index return books that as
a discovery. Here each leg is benchmarked against the same leg -- gap against
gap, intraday against intraday -- in a cohort matched on the same session, the
same liquidity quintile, the same volatility tercile and the same series.

NO RATIOS WITH A REACTION IN THE DENOMINATOR. "What fraction of the pop is kept"
has a pooled standard deviation in the thousands, because the denominator is a
number that crosses zero. Differences, and differences divided by the stock's
own volatility. Nothing else.

CLOSE ANCHORS ONLY. A continuation measured from the running maximum of the
first three days is negative by construction and is not evidence of profit
booking. Every statistic here starts at a close.

FILLS THAT DO NOT EXIST. A limit order does not fill because the low printed
through it if nothing traded: locked sessions are 0.36% of EQ sessions but
14.21% of BE sessions, and single-print sessions are 18.39% of the bottom
turnover quintile in BE. A gap through the limit fills at the open, never at
the limit, and never at the low.

The control here is a deterministic in-cell median rather than a random draw of
K peers. That is a deliberate departure from a drawn cohort and it is stricter,
not looser: it uses every matched peer rather than five, it carries no sampling
noise into a rule-versus-rule comparison, and it cannot be re-rolled to a
friendlier seed. `es.matched_baseline()` is kept as an independent cross-check,
never as the control -- it redraws on every call, matches on liquidity alone,
and does not exclude peers that filed something themselves that day.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import eventstudy as es

log = logging.getLogger(__name__)

# Circuit filters NSE actually applies. A stock is assigned the tightest band
# that contains every move it made in the last year; a move that lands on the
# band is an order-book state, not a price.
BAND_VALUES = (0.05, 0.10, 0.20)
BAND_TOL = 0.0015          # 0.15pp, per spec
MIN_CELL = 15              # below this a control cell is too thin to trust


@dataclass
class Panel:
    """The whole adjusted archive, plus everything a path statistic needs.

    Rows are sorted by (symbol, date) and never re-sorted, so a symbol's rows
    are contiguous and a session offset is integer arithmetic on the row index.
    Every array below is aligned to `df.index`.
    """
    df: pd.DataFrame
    first: np.ndarray        # row index of the first session of each row's symbol
    last: np.ndarray         # row index of the last session of each row's symbol
    cum: np.ndarray          # cumulative ABNORMAL log return, close-anchored
    cum_gap: np.ndarray      # ...of which the overnight leg
    cum_intra: np.ndarray    # ...of which the intraday leg
    ctrl_cum: np.ndarray     # cumulative control-cohort log return, same anchor
    raw_cum: np.ndarray      # cumulative raw log return, same anchor
    adj_open: np.ndarray
    adj_high: np.ndarray
    adj_low: np.ndarray
    adj_close: np.ndarray

    def car(self, a: np.ndarray, b: np.ndarray) -> np.ndarray:
        """Abnormal CAR from close at row `a` to close at row `b`, in logs.

        NaN wherever either end falls outside the symbol's own data. The
        convention cum[first] = 0 makes this a clean difference: no window
        can silently straddle two companies.
        """
        ok = self.valid(a) & self.valid(b) & (self.first[np.clip(a, 0, len(self.cum) - 1)] ==
                                              self.first[np.clip(b, 0, len(self.cum) - 1)])
        out = np.full(len(a), np.nan)
        ai, bi = np.clip(a, 0, len(self.cum) - 1), np.clip(b, 0, len(self.cum) - 1)
        out[ok] = self.cum[bi[ok]] - self.cum[ai[ok]]
        return out

    def ctrl_car(self, a: np.ndarray, b: np.ndarray) -> np.ndarray:
        ok = self.valid(a) & self.valid(b)
        out = np.full(len(a), np.nan)
        ai, bi = np.clip(a, 0, len(self.cum) - 1), np.clip(b, 0, len(self.cum) - 1)
        out[ok] = self.ctrl_cum[bi[ok]] - self.ctrl_cum[ai[ok]]
        return out

    def valid(self, idx: np.ndarray) -> np.ndarray:
        n = len(self.cum)
        safe = np.clip(idx, 0, n - 1)
        return (idx >= 0) & (idx < n)

    def within(self, base: np.ndarray, idx: np.ndarray) -> np.ndarray:
        """True where `idx` is a real session of the same symbol as `base`."""
        n = len(self.cum)
        ok = (idx >= 0) & (idx < n) & (base >= 0) & (base < n)
        out = np.zeros(len(idx), dtype=bool)
        if ok.any():
            out[ok] = self.first[idx[ok]] == self.first[base[ok]]
        return out


def _rank_bucket(df: pd.DataFrame, key: str, value: str, n: int) -> np.ndarray:
    """Cross-sectional bucket within each session.

    Ranked within the day rather than cut on a fixed threshold, so a bucket is
    never a proxy for a period: a stock is "illiquid" relative to what else was
    trading that morning, not relative to 2019.
    """
    pct = df.groupby(key, sort=False)[value].rank(pct=True, method="first")
    b = np.ceil(pct.to_numpy() * n)
    # A row with no trailing history gets its own bucket 0 rather than being
    # lumped in with the least liquid stocks, which would poison that cell's
    # median with newly listed names.
    b = np.where(np.isnan(pct.to_numpy()), 0, b)
    return np.clip(b, 0, n).astype(np.int16)


def _cell_median(df: pd.DataFrame, keys: list[str], cols: list[str],
                 min_n: int = MIN_CELL) -> pd.DataFrame:
    """Equal-weighted mean of each column within a matching cell.

    A MEAN, not a median, and this matters more than it looks. Daily returns are
    right-skewed, so subtracting a cell median leaves a positive residual mean of
    about +22bp a session -- which compounds to +2,200bp over a hundred sessions
    and would swamp every effect this study is looking for. Subtracting the cell
    mean makes the abnormal return mean-zero by construction, which is what makes
    a cumulative path readable.

    Legs are winsorised at +/-25% in logs first, so one stock's takeover cannot
    move the benchmark its eighty-four peers are measured against.

    A cell of four stocks is one stock's bad morning. Where the cell is thin the
    value falls back to the whole session -- a weaker match, but an honest one,
    and the fallback rate is logged.
    """
    df = df.copy()
    for c in cols:
        df[c] = df[c].clip(-0.25, 0.25)
    grp = df.groupby(keys, sort=False, observed=True)
    med = grp[cols].transform("mean")
    cnt = grp[cols[0]].transform("size").to_numpy()
    thin = cnt < min_n
    if thin.any():
        day = df.groupby(keys[0], sort=False)[cols].transform("mean")
        for c in cols:
            med[c] = np.where(thin, day[c].to_numpy(), med[c].to_numpy())
        log.info("control cells: %.1f%% of rows fell back to the whole session "
                 "(cell had fewer than %d peers)", 100 * thin.mean(), min_n)
    return med


def build_panel(con, min_price: float = 1.0) -> Panel:
    """Load and adjust the whole EQ/BE archive and derive every path input.

    This is the expensive call -- one pass, then everything downstream is
    integer indexing. Deliberately loads all symbols, not just event symbols:
    the control cohort is drawn from whatever else was trading that session, so
    a panel restricted to event stocks would match events against events.
    """
    px = con.execute(f"""
        SELECT date, symbol, series, open, high, low, close, volume, turnover, trades
        FROM prices
        WHERE isin_code LIKE 'INE%' AND series IN ('EQ','BE')
          AND close > {min_price} AND open > 0 AND high > 0 AND low > 0
        ORDER BY symbol, date
    """).df()
    px["date"] = pd.to_datetime(px["date"])
    log.info("panel: %s raw sessions, %d symbols", f"{len(px):,}", px["symbol"].nunique())

    # Split/bonus factors, collapsed by (symbol, ex_date) upstream. Never
    # re-collapse and never bypass -- a split and a bonus share an ex-date often
    # enough in India that taking one of them cost this repo a 5x price error.
    acts = es.adjustment_factors(con)
    px = px.sort_values(["symbol", "date"], kind="mergesort").reset_index(drop=True)
    if acts.empty:
        f = np.ones(len(px))
    else:
        a = acts.rename(columns={"ex_date": "date"})[["symbol", "date", "cum_factor"]]
        a["date"] = pd.to_datetime(a["date"])
        # merge_asof requires the LEFT frame sorted by the merge key, and
        # returns rows in that order -- so carry the original row number
        # through and put the factor back where it belongs. Getting this wrong
        # silently shuffles factors across companies.
        left = px[["symbol", "date"]].copy()
        left["_o"] = np.arange(len(left))
        m = pd.merge_asof(left.sort_values("date"), a.sort_values("date"),
                          on="date", by="symbol",
                          direction="forward", allow_exact_matches=False)
        f = m.sort_values("_o")["cum_factor"].fillna(1.0).to_numpy()

    adj_open = (px["open"].to_numpy() * f)
    adj_high = (px["high"].to_numpy() * f)
    adj_low = (px["low"].to_numpy() * f)
    adj_close = (px["close"].to_numpy() * f)

    sym = px["symbol"].to_numpy()
    n = len(px)
    # Contiguous symbol blocks -> first/last row of each block, per row.
    new_sym = np.empty(n, dtype=bool)
    new_sym[0] = True
    new_sym[1:] = sym[1:] != sym[:-1]
    starts = np.flatnonzero(new_sym)
    block = np.cumsum(new_sym) - 1
    first = starts[block]
    ends = np.append(starts[1:] - 1, n - 1)
    last = ends[block]

    prev_close = np.empty(n)
    prev_close[1:] = adj_close[:-1]
    prev_close[0] = np.nan
    prev_close[new_sym] = np.nan          # never bridge two companies
    # prev_close is derived from the ADJUSTED series, never from the archive's
    # prev_close column: that column is unadjusted, and its implied return on an
    # ex-date has a median of -52.8% on this archive.

    with np.errstate(divide="ignore", invalid="ignore"):
        loggap = np.log(adj_open / prev_close)
        logintra = np.log(adj_close / adj_open)
    logcc = loggap + logintra
    simple_cc = np.expm1(logcc)

    px["loggap"], px["logintra"], px["logcc"] = loggap, logintra, logcc
    px["adj_close_col"] = adj_close

    # Point-in-time nuisance variables: every window ends the session BEFORE the
    # row it describes, so nothing here can see the day it is used on.
    g = px.groupby("symbol", sort=False)
    px["normal_turnover"] = g["turnover"].transform(
        lambda s: s.shift(1).rolling(60, min_periods=30).median())
    px["normal_trades"] = g["trades"].transform(
        lambda s: s.shift(1).rolling(60, min_periods=30).median())
    px["sigma_raw"] = g["logcc"].transform(
        lambda s: s.shift(1).rolling(60, min_periods=40).std())
    px["max_move_250"] = g["logcc"].transform(
        lambda s: s.abs().shift(1).rolling(250, min_periods=120).max())
    px["high_252"] = g["adj_close_col"].transform(
        lambda s: s.rolling(252, min_periods=200).max())
    px["low_252"] = g["adj_close_col"].transform(
        lambda s: s.rolling(252, min_periods=200).min())

    # Matching cells. Liquidity quintile x volatility tercile x series, formed
    # cross-sectionally within the session.
    px["dcode"] = pd.factorize(px["date"])[0]
    px["liq_q"] = _rank_bucket(px, "dcode", "normal_turnover", 5)
    px["sig_q"] = _rank_bucket(px, "dcode", "sigma_raw", 3)
    px["ser_c"] = (px["series"] == "BE").astype(np.int16)
    px["cell"] = (px["dcode"].to_numpy().astype(np.int64) * 100
                  + px["liq_q"] * 10 + px["sig_q"] * 2 + px["ser_c"])

    med = _cell_median(px, ["dcode", "cell"], ["loggap", "logintra"])
    cm_gap = med["loggap"].to_numpy()
    cm_intra = med["logintra"].to_numpy()

    abn_gap = loggap - cm_gap
    abn_intra = logintra - cm_intra
    abn_cc = abn_gap + abn_intra
    ctrl_cc = cm_gap + cm_intra

    # A missing first session is a zero step, not a gap in the sum. The
    # convention cum[first] = 0 is what makes car(a, b) a plain difference.
    for arr in (abn_cc, ctrl_cc, logcc, abn_gap, abn_intra):
        arr[~np.isfinite(arr)] = 0.0

    def _anchored(step):
        c = np.cumsum(step)
        return c - (c[first] - step[first])

    cum = _anchored(abn_cc)
    cum_gap = _anchored(abn_gap)
    cum_intra = _anchored(abn_intra)
    ctrl_cum = _anchored(ctrl_cc)
    raw_cum = _anchored(logcc)

    px["abn_cc"] = abn_cc
    px["sigma"] = (px.groupby("symbol", sort=False)["abn_cc"]
                     .transform(lambda s: s.shift(1).rolling(60, min_periods=40).std()))

    # Circuit band: the tightest standard filter that contained the last year.
    mm = px["max_move_250"].to_numpy()
    band = np.full(n, np.nan)
    for b in BAND_VALUES:
        band = np.where(np.isnan(band) & (np.expm1(mm) <= b + BAND_TOL), b, band)
    px["band"] = band
    px["band_hit"] = (np.abs(np.abs(simple_cc) - band) <= BAND_TOL) & np.isfinite(band)
    px["locked"] = adj_high <= adj_low
    px["single_print"] = (adj_open == adj_high) & (adj_high == adj_low) & (adj_low == adj_close)
    px["rel_turnover"] = px["turnover"].to_numpy() / px["normal_turnover"].to_numpy()
    px["simple_cc"] = simple_cc

    log.info("panel ready: %s rows, cum span %.3f", f"{n:,}", float(np.nanmax(cum)))
    return Panel(df=px, first=first, last=last, cum=cum, cum_gap=cum_gap,
                 cum_intra=cum_intra, ctrl_cum=ctrl_cum,
                 raw_cum=raw_cum, adj_open=adj_open, adj_high=adj_high,
                 adj_low=adj_low, adj_close=adj_close)


def positions(panel: Panel, events: pd.DataFrame, symbol_col: str = "symbol",
              date_col: str = "trade_date") -> np.ndarray:
    """Row index of each event's day zero: the first session ON OR AFTER the
    filing's trade_date THAT THE SYMBOL ACTUALLY TRADED.

    Rolling forward rather than joining on equality is not a refinement. An
    equality join drops the 39,572 filings (5.06% of the archive) whose
    trade_date lands on an exchange holiday -- trade_date rolls weekends but has
    no NSE holiday calendar -- and those are not a random 5%: companies dump
    news into the day before a long weekend.
    """
    left = events[[symbol_col, date_col]].copy()
    left[date_col] = pd.to_datetime(left[date_col])
    left = left.rename(columns={symbol_col: "symbol", date_col: "date"})
    left["_o"] = np.arange(len(left))

    right = panel.df[["symbol", "date"]].copy()
    right["_pos"] = np.arange(len(right))

    m = pd.merge_asof(left.sort_values("date"), right.sort_values("date"),
                      on="date", by="symbol", direction="forward")
    m = m.sort_values("_o")
    return m["_pos"].to_numpy(dtype="float64")


def car_path(panel: Panel, pos: np.ndarray, lo: int = -20, hi: int = 60,
             anchor: int = -1, leg: str = "total") -> np.ndarray:
    """Abnormal CAR matrix, one row per event, one column per relative day.

    Anchored at the close of `anchor` (default d0-1, so the day-zero reaction is
    visible in the path rather than hidden in the baseline).

    `leg` splits the path into its overnight and intraday halves. That split is
    the whole story for a news reaction: a filing released after the close is
    priced in the opening auction, and whether the rest of the session gives it
    back is a different question from whether the next month does.
    """
    src = {"total": panel.cum, "gap": panel.cum_gap, "intra": panel.cum_intra}[leg]
    rel = np.arange(lo, hi + 1)
    base = (pos + anchor).astype(np.int64)
    out = np.full((len(pos), len(rel)), np.nan)
    for j, r in enumerate(rel):
        idx = (pos + r).astype(np.int64)
        ok = panel.within(base, idx)
        v = np.full(len(pos), np.nan)
        if ok.any():
            v[ok] = src[idx[ok]] - src[base[ok]]
        out[:, j] = v
    return out


def window_matrix(panel: Panel, pos: np.ndarray, arr: np.ndarray,
                  lo: int, hi: int) -> np.ndarray:
    """Any per-session array, sliced to [lo, hi] around each event."""
    out = np.full((len(pos), hi - lo + 1), np.nan)
    for j, r in enumerate(range(lo, hi + 1)):
        idx = (pos + r).astype(np.int64)
        ok = panel.within(pos.astype(np.int64), idx)
        v = np.full(len(pos), np.nan)
        if ok.any():
            v[ok] = arr[idx[ok]]
        out[:, j] = v
    return out


# --------------------------------------------------------------------------
# Fills
# --------------------------------------------------------------------------

EXPLICIT_COST_BP = 40.0     # STT + exchange + stamp + SEBI/GST, one way
IMPACT_FRACTION = 0.15      # of the entry session's own high-low range


def fill_limit(panel: Panel, pos: np.ndarray, limit: np.ndarray,
               start_rel: int, n_sessions: int,
               trail_high: bool = False, pullback: float = 0.0):
    """Walk sessions forward and return (fill_price, fill_offset, filled).

    The four rejections, all of which flatter a pullback rule if skipped:
      * no fill unless the session's LOW reached the limit;
      * a gap through the limit fills at the OPEN, not at the limit -- you do
        not get the better price you did not ask for, but you do get the worse
        one you did;
      * no fill on a locked session (high == low): nobody is offering;
      * no fill on a single-print session: one trade is not a market.

    `trail_high` re-prices the limit each session to `(1 - pullback)` times the
    running maximum high since day zero, which is the rule as a trader would
    actually place it -- and is the version that turns out to fill 99.7% of the
    time, i.e. to be a delay rather than a filter.
    """
    n = len(pos)
    price = np.full(n, np.nan)
    offset = np.full(n, np.nan)
    lim = limit.astype(float).copy()
    run_high = np.full(n, -np.inf)
    open_live = np.zeros(n, dtype=bool)

    for k in range(start_rel, start_rel + n_sessions):
        idx = (pos + k).astype(np.int64)
        ok = panel.within(pos.astype(np.int64), idx) & np.isnan(price)
        if not ok.any():
            continue
        i = idx[ok]
        if trail_high:
            prev = (pos + k - 1).astype(np.int64)
            pok = panel.within(pos.astype(np.int64), prev)
            hh = np.where(pok, panel.adj_high[np.clip(prev, 0, len(panel.cum) - 1)], -np.inf)
            run_high = np.maximum(run_high, np.where(np.isfinite(hh), hh, -np.inf))
            lim = np.where(np.isfinite(run_high) & (run_high > 0),
                           run_high * (1 - pullback), lim)
        o, h, l = panel.adj_open[i], panel.adj_high[i], panel.adj_low[i]
        c = panel.adj_close[i]
        tradeable = (h > l) & ~((o == h) & (h == l) & (l == c))
        hit = (l <= lim[ok]) & tradeable & np.isfinite(lim[ok])
        px = np.minimum(lim[ok], o)
        rows = np.flatnonzero(ok)[hit]
        price[rows] = px[hit]
        offset[rows] = k

    return price, offset, np.isfinite(price)


def fill_market(panel: Panel, pos: np.ndarray, rel: int):
    """Market order at the open of `rel`. Refused if that open is locked at the
    upper band -- there is no offer to lift."""
    idx = (pos + rel).astype(np.int64)
    ok = panel.within(pos.astype(np.int64), idx)
    price = np.full(len(pos), np.nan)
    i = idx[ok]
    o, h, l = panel.adj_open[i], panel.adj_high[i], panel.adj_low[i]
    tradeable = h > l
    rows = np.flatnonzero(ok)[tradeable]
    price[rows] = o[tradeable]
    return price, np.full(len(pos), float(rel)), np.isfinite(price)


def net_of_costs(price: np.ndarray, panel: Panel, pos: np.ndarray,
                 offset: np.ndarray, slippage_bp: float = 0.0) -> np.ndarray:
    """Entry price grossed up by the costs actually paid to get filled."""
    idx = np.where(np.isfinite(offset), pos + np.nan_to_num(offset), pos).astype(np.int64)
    idx = np.clip(idx, 0, len(panel.cum) - 1)
    rng = (panel.adj_high[idx] - panel.adj_low[idx]) / np.maximum(panel.adj_close[idx], 1e-9)
    bp = EXPLICIT_COST_BP + slippage_bp + 1e4 * IMPACT_FRACTION * rng
    return price * (1 + bp / 1e4)
