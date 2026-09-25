"""A point-in-time quality factor for India (strategy v2, amendment A5).

IIMA publishes market, size, value and momentum factors for India but no
profitability factor. v2 ranks on ROCE and margin stability, so a quality
premium could otherwise be reported as alpha. This module builds a long-short
quality factor from the archive, in the manner of Fama and French's RMW.
A5 fixes the outline; every choice below that A5 leaves open is recorded in
research/strategy/v2-spec.md, "Clarification to A5" (2026-09-25), written
before any v2 measurement. Nothing here is tuned.

  Formation   at the close of the last session of every month, February 2019
              to August 2026 (ROCE from February 2023). The portfolio is held
              for the next calendar month, close to close, which is the clock
              IIMA's monthly factors use. The first holding month is March
              2019, the first month of v1's attribution sample.

  Information the information set of pit.build_snapshot at D = the formation
              session itself, i.e. what was known at the OPEN of the month's
              last session: prices through the previous close, filings with
              broadcast_dt strictly before D 00:00, split/bonus factors with
              ex_date before D. Every filing used was therefore broadcast
              before the month-end, and so was every filing broadcast on the
              last session's own day (they are not used either). The weights
              are market caps at the previous close; the one-session lag
              costs nothing and keeps the whole formation inside one
              audited information set.

  Universe    A5's "same liquid universe" is v1's universe at D, rules 1-7,
              exactly pit.universe()'s members: ordinary equity in EQ or BE
              traded in the last 5 sessions, median 60-session turnover at
              least Rs 20 lakh, 200 sessions in the last 365 days, not a bank,
              NBFC or insurer, four consecutive quarters known with the latest
              within 200 days, TTM profit and TTM EBITDA positive (rule 6),
              and a computable market cap. These are the only names v1 and v2
              can hold. The versions "margin" and "roce" use it.
              The versions "margin_no_r6" and "roce_no_r6" drop rule 6 (the
              usual Fama-French practice of keeping loss-makers) and are a
              labelled robustness line, never the headline. pit.universe()
              always applies rule 6, so the rule-6-free set is recovered from
              its own outputs: the rule-4 survivors are the snapshot's lender
              table (built by universe() on the rule-3 survivors), rules 5
              and 7 are re-applied with pit's constants, and the result is
              asserted to reproduce pit's members exactly once rule 6 is put
              back (`liquid_universe`). So the look-ahead rules have one
              implementation, pit's.

  Signals     MARGIN  TTM EBITDA / TTM revenue (revenue > 0), from
                      pit.fundamentals: operating EBITDA with other income
                      stripped, the company's chosen basis, all four quarters.
              ROCE    TTM EBIT / (equity + total debt), v2 item 2, with the
                      owner's standard EBIT: profit before exceptional items
                      and tax, plus finance costs (other income included,
                      exceptional items excluded), summed over the same four
                      quarters and basis as the margin, each quarter from the
                      revision the snapshot kept. Where a filing gives no
                      pre-exceptional profit, profit before tax less
                      exceptional items stands in. Capital is the latest
                      balance sheet known before D in that basis, chosen
                      separately from the income statement as Clarification
                      38 says (it may come from a filing with no income
                      statement, and from a period after the latest income
                      quarter), latest revision, filings pit's scale screen
                      set aside excluded, and a balance sheet on a different
                      unit scale from the company's others (assets and share
                      capital both 30x out) treated as not filed; no older
                      than 400 days at D, with
                      equity > 0; a balance sheet with no borrowings line has
                      zero debt. Balance sheets start with September 2022, so
                      ROCE runs from the February 2023 formation. v2's ROCE
                      filter and ranking must call `ttm_ebit` and
                      `latest_balance_sheets` so the two cannot drift apart.

  Sort        big and small halves by market cap at the median of the whole
              version universe (Fama and French's size breakpoint does not
              depend on the signal); within each half, among the names that
              have the signal, the 30th and 70th percentiles split it into
              low, middle and high (A5's wording; Fama and French take
              profitability breakpoints across NYSE stocks instead). Six
              portfolios, each value-weighted by market cap. The factor is
              (SH + BH)/2 - (SL + BL)/2.

  Returns     split/bonus-adjusted closes (pit.split_factors and
              pit.cumulative_factor), with cash dividends parsed as the
              engine parses them (engine.dividends) and reinvested on the
              ex-date, compounded daily. A dividend above half the previous
              raw close is dropped as a parse error, as in the engine. A stock
              that stops trading in the month keeps its last close. Where a
              stock has no EQ/BE row on a session but trades in the BZ
              (trade-for-trade) series, the BZ close is used, so a company
              pushed out of the normal market still shows its fall.
              KNOWN LIMITATION: only splits and bonuses are adjusted.
              Demergers, consolidations, capital reductions and rights
              issues inside a holding month enter as price moves (the
              independent review found single months off by up to 1.3
              percentage points, with the means barely moved).

Nothing here computes a strategy return.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

from zen.portfolio import engine
from zen.universe import pit

log = logging.getLogger(__name__)

FIRST_FORMATION = (2019, 2)
ROCE_FIRST_FORMATION = (2023, 2)
LAST_FORMATION = (2026, 8)
LOW_PCT, HIGH_PCT = 0.30, 0.70
BS_MAX_AGE_DAYS = 400
VERSIONS = ("margin", "roce", "margin_no_r6", "roce_no_r6")
SIGNAL = {"margin": "margin", "roce": "roce", "margin_no_r6": "margin", "roce_no_r6": "roce"}
HEADLINE = ("margin", "roce")
PORTS = ["SL", "SM", "SH", "BL", "BM", "BH"]
MAX_DIVIDEND_YIELD = 0.5          # engine's parse-error screen
FALLBACK_SERIES = ("BZ",)         # prices_other series used where EQ/BE is missing


# ---------------------------------------------------------------- calendar
def formation_sessions(cal: pd.DatetimeIndex, first=FIRST_FORMATION,
                       last=LAST_FORMATION) -> list[pd.Timestamp]:
    """The last session of every month from `first` to `last` inclusive."""
    s = pd.Series(cal, index=cal)
    ends = s.groupby([cal.year, cal.month]).max()
    out = [pd.Timestamp(d) for (y, m), d in ends.items() if first <= (y, m) <= last]
    return sorted(out)


# ---------------------------------------------------------------- universe
def liquid_universe(snap: pit.Snapshot, static: pit.StaticLabels) -> pd.DataFrame:
    """pit's universe without rule 6, indexed by stock id, with mcap and fundamentals.

    pit.universe() is run as it is; the rule-6-free set is then rebuilt from
    its outputs (see the module docstring) and checked against it.
    """
    members, funnel, fund = pit.universe(snap, static)
    lf = snap.lenders                                   # rule-3 survivors, rule-4 flags
    syms = lf.index[~lf["lender"].to_numpy()]
    assert len(syms) == funnel["r4_not_lender"], "rule-4 survivors do not match pit's funnel"
    D = snap.D
    fu = fund.reindex(syms)
    stale = (D - fu["latest_period_end"]).dt.days
    r5 = (fu["consec_quarters"] >= pit.MIN_CONSEC_QUARTERS) & (stale <= pit.MAX_STALENESS_DAYS)
    syms = syms[r5.fillna(False).to_numpy()]
    assert len(syms) == funnel["r5_four_quarters_fresh"], "rule 5 does not match pit's funnel"
    last = snap.px.groupby("symbol").tail(1).set_index("symbol")
    fu = fund.reindex(syms)
    mcap = last["close"].reindex(syms) * fu["shares"]
    r7 = mcap.notna() & (mcap > 0) & np.isfinite(mcap)
    syms = syms[r7.to_numpy()]
    out = fund.reindex(syms).copy()
    out.index.name = "symbol"
    out["mcap"] = mcap.reindex(syms)
    out["ticker"] = last["ticker"].reindex(syms)
    out["r6_pass"] = ((out["ttm_profit"] > 0) & (out["ttm_ebitda"] > 0)).fillna(False)
    # The rebuilt set with rule 6 put back must be pit's members, name for name
    # and market cap for market cap.
    back = out[out["r6_pass"]]
    if set(back.index) != set(members.index):
        raise AssertionError(
            f"{D.date()}: rule-6-free universe does not reproduce pit.universe: "
            f"{len(set(back.index) ^ set(members.index))} names differ")
    diff = (back["mcap"] - members["mcap"].reindex(back.index)).abs()
    if len(diff) and float(diff.max()) > 1e-6 * float(back["mcap"].max()):
        raise AssertionError(f"{D.date()}: market caps differ from pit.universe")
    return out


# ---------------------------------------------------------------- signals
EXTRA_COLS = ["pbt_before_exceptional", "pbt", "exceptional_items", "finance_costs"]
BS_KEY = ["symbol", "consolidated", "period_end", "broadcast_dt"]


def quarter_extras(con, snap: pit.Snapshot, ids) -> pd.DataFrame:
    """snap.quarters with the EBIT lines attached.

    The same cut as the snapshot (broadcast_dt strictly before D 00:00), and
    joined on (stock, basis, period_end, broadcast_dt), so each quarter carries
    the lines of exactly the income-statement revision the snapshot kept,
    after its scale screen. Nothing broadcast at or after D can reach it.
    """
    q = snap.quarters
    if q.empty:
        return q.assign(**{c: np.nan for c in EXTRA_COLS})
    x = con.execute(
        "SELECT symbol, period_end, broadcast_dt, consolidated, xbrl_url, "
        + ", ".join(EXTRA_COLS) + " FROM financials WHERE broadcast_dt < ?",
        [snap.D.to_pydatetime()]).df()
    x["period_end"] = pd.to_datetime(x["period_end"])
    x["broadcast_dt"] = pd.to_datetime(x["broadcast_dt"])
    x["symbol"] = ids.for_events(x, "symbol", "broadcast_dt") if len(x) else x["symbol"]
    x = (x.sort_values(BS_KEY + ["xbrl_url"]).drop_duplicates(BS_KEY, keep="last")
          .drop(columns="xbrl_url"))
    return q.merge(x, on=BS_KEY, how="left")


def latest_balance_sheets(con, snap: pit.Snapshot, ids) -> pd.DataFrame:
    """The latest balance sheet known before D per (stock, basis).

    Clarification 38: balance-sheet figures are chosen separately from the
    income statement, so a filing with no income statement still counts, and
    so does a balance sheet for a period after the latest income quarter.
    Latest period first, then the latest revision of it. A filing that pit's
    scale screen set aside is treated as not filed (Clarification 30a). That
    screen tests income lines only, so the balance sheets get their own
    (`balance_sheet_scale_errors`): the latest revision of each period is
    compared with the company's other balance sheets and, if on a different
    unit scale, treated as not filed, as pit treats a mis-scaled income
    statement: that period then has no balance sheet and the latest earlier
    period's is used. Columns: symbol, consolidated, period_end,
    broadcast_dt, equity, debt_total.
    """
    cols = BS_KEY + ["equity", "debt_total"]
    x = con.execute(
        "SELECT symbol, period_end, broadcast_dt, consolidated, equity, debt_total, "
        "assets, equity_capital, xbrl_url "
        "FROM financials WHERE broadcast_dt < ? AND has_balance_sheet",
        [snap.D.to_pydatetime()]).df()
    if x.empty:
        return pd.DataFrame(columns=cols)
    x["period_end"] = pd.to_datetime(x["period_end"])
    x["broadcast_dt"] = pd.to_datetime(x["broadcast_dt"])
    x["consolidated"] = x["consolidated"].astype(bool)
    x["symbol"] = ids.for_events(x, "symbol", "broadcast_dt")
    sd = snap.scale_dropped
    if sd is not None and len(sd):
        drop = sd[BS_KEY].copy()
        drop["consolidated"] = drop["consolidated"].astype(bool)
        drop["period_end"] = pd.to_datetime(drop["period_end"])
        drop["broadcast_dt"] = pd.to_datetime(drop["broadcast_dt"])
        x = x.merge(drop.assign(_scale_dropped=True), on=BS_KEY, how="left")
        x = x[x["_scale_dropped"].isna()].drop(columns="_scale_dropped")
    # the latest revision of each period, then the balance-sheet scale screen
    x = (x.sort_values(BS_KEY + ["xbrl_url"])
          .drop_duplicates(["symbol", "consolidated", "period_end"], keep="last")
          .reset_index(drop=True))
    x = x[~balance_sheet_scale_errors(x)]
    x = x.drop_duplicates(["symbol", "consolidated"], keep="last")
    return x[cols].reset_index(drop=True)


def balance_sheet_scale_errors(b: pd.DataFrame) -> np.ndarray:
    """Boolean mask over `b` (one row per symbol, basis and period, all known
    before D): the balance sheet is on a different unit scale from most of its
    neighbours.

    The balance-sheet twin of pit.scale_errors, with its band, neighbour count
    and majority rule. A unit error multiplies every rupee line by the same
    power of ten, so total assets and paid-up share capital break together, in
    the same direction; a genuine jump in assets (an acquisition, a large
    raise) leaves share capital where it was. Each balance sheet is compared
    with up to pit.SCALE_NEIGHBOURS nearest other periods of the same company
    and basis that have both lines positive, and flagged when it breaks scale
    with more than half of them. With no neighbour it is kept. Two periods
    that disagree with nothing else to judge by are both flagged: neither can
    be trusted, and the stock has no ROCE until a third balance sheet decides.
    """
    n = len(b)
    out = np.zeros(n, dtype=bool)
    if n == 0:
        return out
    f = b.reset_index(drop=True)
    pos = lambda v: np.where(np.isfinite(v) & (v > 0), v, np.nan)
    assets = pos(f["assets"].to_numpy(dtype=float))
    capital = pos(f["equity_capital"].to_numpy(dtype=float))
    pe = pd.to_datetime(f["period_end"])
    q = (pe.dt.year * 4 + (pe.dt.month - 1) // 3).to_numpy()
    for _, ix in f.groupby(["symbol", "consolidated"], sort=False).indices.items():
        if len(ix) < 2:
            continue
        ok = np.isfinite(assets[ix]) & np.isfinite(capital[ix])
        with np.errstate(invalid="ignore", divide="ignore"):
            ar = assets[ix][:, None] / assets[ix][None, :]
            cr = capital[ix][:, None] / capital[ix][None, :]
        brk = pit._band(ar) & pit._band(cr) & ((ar > 1) == (cr > 1))
        qq = q[ix]
        for a in range(len(ix)):
            if not ok[a]:
                continue
            d = np.abs(qq - qq[a]).astype(float)
            d[a] = np.inf
            d[~ok] = np.inf
            order = np.lexsort((qq, d))[:pit.SCALE_NEIGHBOURS]
            order = order[np.isfinite(d[order])]
            if len(order) and brk[a, order].sum() * 2 > len(order):
                out[ix[a]] = True
    return out


def ttm_ebit(liq: pd.DataFrame, quarters: pd.DataFrame) -> pd.Series:
    """TTM EBIT per stock: profit before exceptional items and tax plus finance
    costs, over the same four quarters and basis as the TTM EBITDA, all four
    present. `quarters` is quarter_extras()' output."""
    if quarters.empty or liq.empty:
        return pd.Series(np.nan, index=liq.index)
    q = quarters[quarters["symbol"].isin(liq.index)].copy()
    basis = liq["consolidated"].astype(bool)
    q = q[q["consolidated"].astype(bool).to_numpy() == basis.reindex(q["symbol"]).to_numpy()]
    lpe = pd.to_datetime(liq["latest_period_end"])
    Lq = lpe.dt.year * 4 + (lpe.dt.month - 1) // 3
    q["Lq"] = q["symbol"].map(Lq)
    t = q[(q["q"] <= q["Lq"]) & (q["q"] > q["Lq"] - 4)].copy()
    pbe = t["pbt_before_exceptional"].where(
        t["pbt_before_exceptional"].notna(), t["pbt"] - t["exceptional_items"].fillna(0.0))
    t["ebit"] = pbe + t["finance_costs"]
    g = t.groupby("symbol")["ebit"]
    ebit = g.sum(min_count=4).where(g.count() == 4)
    ebit = ebit.where(t.groupby("symbol")["q"].nunique() == 4)
    return ebit.reindex(liq.index)


def signals(liq: pd.DataFrame, quarters: pd.DataFrame, bs: pd.DataFrame,
            D: pd.Timestamp) -> pd.DataFrame:
    """MARGIN and ROCE per stock at D. `quarters` is quarter_extras()' output,
    `bs` is latest_balance_sheets()' output."""
    out = pd.DataFrame(index=liq.index)
    rev, ebitda = liq["ttm_revenue"], liq["ttm_ebitda"]
    with np.errstate(divide="ignore", invalid="ignore"):
        out["margin"] = (ebitda / rev).where(rev > 0)
    out["ttm_ebit"] = ttm_ebit(liq, quarters)
    out["capital"] = np.nan
    out["bs_period_end"] = pd.NaT
    out["bs_broadcast"] = pd.NaT
    if bs is not None and len(bs) and len(liq):
        basis = liq["consolidated"].astype(bool)
        b = bs[bs["symbol"].isin(liq.index)]
        b = b[b["consolidated"].astype(bool).to_numpy() == basis.reindex(b["symbol"]).to_numpy()]
        b = b.set_index("symbol")
        age = (D - pd.to_datetime(b["period_end"])).dt.days
        eq = b["equity"].astype(float)
        cap = eq + b["debt_total"].astype(float).fillna(0.0)
        ok = (age <= BS_MAX_AGE_DAYS) & (eq > 0) & (cap > 0)
        out["capital"] = cap.where(ok).reindex(out.index)
        out["bs_period_end"] = pd.to_datetime(b["period_end"]).where(ok).reindex(out.index)
        out["bs_broadcast"] = pd.to_datetime(b["broadcast_dt"]).where(ok).reindex(out.index)
    with np.errstate(divide="ignore", invalid="ignore"):
        out["roce"] = out["ttm_ebit"] / out["capital"]
    out.loc[~np.isfinite(out["roce"].astype(float)), "roce"] = np.nan
    return out


# ---------------------------------------------------------------- sorting
def sort_2x3(mcap: pd.Series, signal: pd.Series) -> pd.Series:
    """Label each stock SL/SM/SH/BL/BM/BH.

    `mcap` covers the whole sorting universe. Big = market cap above the
    median of every stock in it with a positive market cap, whether or not it
    has the signal (Fama-French: the size breakpoint does not depend on the
    signal). Only stocks with a finite signal are then sorted. Within each
    half, low = signal at or below the half's 30th percentile, high = at or
    above its 70th.
    """
    has_cap = mcap.notna() & (mcap > 0) & np.isfinite(mcap.astype(float))
    ok = has_cap & signal.notna() & np.isfinite(signal.astype(float))
    m, s = mcap[ok].astype(float), signal[ok].astype(float)
    out = pd.Series(pd.NA, index=mcap.index, dtype=object)
    if len(m) == 0:
        return out
    big = m > np.median(mcap[has_cap].astype(float).to_numpy())
    for half, mask in (("S", ~big), ("B", big)):
        sh = s[mask]
        if sh.empty:
            continue
        lo, hi = np.quantile(sh.to_numpy(), [LOW_PCT, HIGH_PCT])
        lab = np.where(sh <= lo, "L", np.where(sh >= hi, "H", "M"))
        out.loc[sh.index] = [half + x for x in lab]
    return out


def value_weighted(ret: pd.Series, weight: pd.Series) -> float:
    """Sum of w r over sum of w, over stocks with a return and a positive weight."""
    ok = ret.notna() & weight.notna() & (weight > 0)
    w = weight[ok].astype(float)
    if w.sum() <= 0:
        return np.nan
    return float((w * ret[ok].astype(float)).sum() / w.sum())


def factor_from_portfolios(p: dict) -> float:
    """(SH + BH)/2 - (SL + BL)/2."""
    return 0.5 * (p["SH"] + p["BH"]) - 0.5 * (p["SL"] + p["BL"])


def month_row(labels: pd.Series, mcap: pd.Series, ret: pd.Series) -> dict:
    """The six value-weighted portfolio returns, their counts and the factor."""
    row = {}
    for p in PORTS:
        idx = labels.index[(labels == p).to_numpy()]
        row[p] = value_weighted(ret.reindex(idx), mcap.reindex(idx))
        row[f"n_{p}"] = int(len(idx))
    row["factor"] = factor_from_portfolios(row)
    return row


# ---------------------------------------------------------------- returns
@dataclass
class ReturnPanel:
    dates: pd.DatetimeIndex
    symbols: pd.Index
    gross: np.ndarray        # (last_t + div_t) / last_{t-1}, per stock id; nan before listing
    fallback_days: np.ndarray  # bool: the close on t came from FALLBACK_SERIES
    dropped_dividends: pd.DataFrame

    def holding_returns(self, t0: pd.Timestamp, t1: pd.Timestamp, symbols) -> pd.Series:
        """Total return from the close of t0 to the close of t1, per stock."""
        a = self.dates.get_loc(pd.Timestamp(t0))
        b = self.dates.get_loc(pd.Timestamp(t1))
        cols = self.symbols.get_indexer(pd.Index(symbols))
        out = np.full(len(cols), np.nan)
        ok = cols >= 0
        # The last close is carried forward, so once a stock has a close at t0
        # every later gross is finite; a stock with none yet stays nan.
        g = self.gross[a + 1:b + 1][:, cols[ok]]
        out[ok] = np.prod(g, axis=0) - 1.0
        return pd.Series(out, index=pd.Index(symbols))

    def fallback_used(self, t0, t1, symbols) -> pd.Series:
        a = self.dates.get_loc(pd.Timestamp(t0))
        b = self.dates.get_loc(pd.Timestamp(t1))
        cols = self.symbols.get_indexer(pd.Index(symbols))
        out = np.zeros(len(cols), dtype=bool)
        ok = cols >= 0
        out[ok] = self.fallback_days[a + 1:b + 1][:, cols[ok]].any(axis=0)
        return pd.Series(out, index=pd.Index(symbols))


def build_return_panel(con, start: pd.Timestamp, end: pd.Timestamp, ids) -> ReturnPanel:
    """Daily gross total returns per stock id over sessions [start - 10d, end]."""
    lo = (pd.Timestamp(start) - pd.Timedelta(days=10)).date()
    hi = pd.Timestamp(end).date()
    px = con.execute(
        "SELECT symbol, isin_code, date, close, turnover, 0 AS fb FROM prices "
        "WHERE date >= ? AND date <= ? AND close > 0", [lo, hi]).df()
    ser = ", ".join(f"'{s}'" for s in FALLBACK_SERIES)
    oth = con.execute(
        f"SELECT symbol, isin_code, date, close, turnover, 1 AS fb FROM prices_other "
        f"WHERE date >= ? AND date <= ? AND close > 0 AND series IN ({ser})", [lo, hi]).df()
    px = pd.concat([px, oth], ignore_index=True)
    px["date"] = pd.to_datetime(px["date"])
    px["symbol"] = ids.for_prices(px)
    # EQ/BE first, then the traded one of two tickers of one stock on one day.
    px = (px.sort_values(["fb", "turnover"], ascending=[True, False], na_position="last")
            .drop_duplicates(["symbol", "date"]))
    dates = pd.DatetimeIndex(pd.to_datetime(con.execute(
        "SELECT DISTINCT date FROM prices WHERE date >= ? AND date <= ? ORDER BY date",
        [lo, hi]).df()["date"]))
    px = px[px["date"].isin(dates)]
    fac = pit.split_factors(con, through=end, ids=ids)
    px["cum"] = pit.cumulative_factor(px, fac)
    px["aclose"] = px["close"] * px["cum"]
    syms = pd.Index(sorted(px["symbol"].unique()))
    T, S = len(dates), len(syms)
    ti, si = dates.get_indexer(px["date"]), syms.get_indexer(px["symbol"])
    close = np.full((T, S), np.nan)
    close[ti, si] = px["aclose"].to_numpy()
    fb = np.zeros((T, S), dtype=bool)
    fb[ti, si] = px["fb"].to_numpy() == 1
    last = pd.DataFrame(close).ffill().to_numpy()

    div = np.zeros((T, S))
    dv = engine.dividends(con, dates[0], end, ids)
    dv = dv[dv["symbol"].isin(syms)].copy()
    dropped = pd.DataFrame()
    if not dv.empty:
        dv["cum"] = pit.cumulative_factor(dv.rename(columns={"ex_date": "date"}), fac)
        dv["t"] = dates.searchsorted(dv["ex_date"], side="left")
        dv = dv[(dv["t"] < T) & (dv["t"] > 0)]
        dv["s"] = syms.get_indexer(dv["symbol"])
        prev_raw = last[dv["t"].to_numpy() - 1, dv["s"].to_numpy()] / dv["cum"].to_numpy()
        ok = ~(dv["dps"].to_numpy() > MAX_DIVIDEND_YIELD * prev_raw)
        dropped = dv[~ok]
        dv = dv[ok]
        np.add.at(div, (dv["t"].to_numpy(), dv["s"].to_numpy()), (dv["dps"] * dv["cum"]).to_numpy())
    prev = np.vstack([np.full(S, np.nan), last[:-1]])
    with np.errstate(divide="ignore", invalid="ignore"):
        gross = (last + div) / prev
    return ReturnPanel(dates=dates, symbols=syms, gross=gross, fallback_days=fb,
                       dropped_dividends=dropped.reset_index(drop=True))


# ---------------------------------------------------------------- one formation
def formation(con, D: pd.Timestamp, static: pit.StaticLabels) -> pd.DataFrame:
    """Everything decided at formation session D: universe, signals, labels.

    One row per stock in the rule-6-free liquid universe; `r6_pass` marks the
    v1 universe the headline versions sort. Labels for each version in
    VERSIONS are in `label_<version>`, NA outside that version's universe.
    """
    D = pd.Timestamp(D).normalize()
    snap = pit.build_snapshot(con, D, static)
    liq = liquid_universe(snap, static)
    qx = quarter_extras(con, snap, static.ids)
    bs = latest_balance_sheets(con, snap, static.ids)
    sig = signals(liq, qx, bs, D)
    out = liq[["ticker", "mcap", "consolidated", "latest_period_end", "latest_broadcast",
               "ttm_revenue", "ttm_ebitda", "ttm_profit", "r6_pass"]].join(sig)
    roce_on = (D.year, D.month) >= ROCE_FIRST_FORMATION
    for v in VERSIONS:
        members = out.index if v.endswith("_no_r6") else out.index[out["r6_pass"].to_numpy()]
        sub = out.loc[members]
        if SIGNAL[v] == "roce" and not roce_on:
            out[f"label_{v}"] = pd.Series(pd.NA, index=out.index, dtype=object)
        else:
            out[f"label_{v}"] = sort_2x3(sub["mcap"], sub[SIGNAL[v]]).reindex(
                out.index, fill_value=pd.NA)
    out.insert(0, "formation", D)
    return out.reset_index()


def in_universe(f: pd.DataFrame, version: str) -> pd.Series:
    """Boolean mask of the rows of a formation frame inside `version`'s universe."""
    if version.endswith("_no_r6"):
        return pd.Series(True, index=f.index)
    return f["r6_pass"].astype(bool)


def monthly_factor(forms: pd.DataFrame, panel: ReturnPanel, cal: pd.DatetimeIndex
                   ) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Monthly factor rows (one per version and holding month) and per-stock holdings."""
    rows, hold = [], []
    ends = formation_sessions(cal, (1990, 1), (2100, 12))
    for D, f in forms.groupby("formation", sort=True):
        D = pd.Timestamp(D)
        nxt = [e for e in ends if e > D]
        if not nxt:
            continue
        t1 = nxt[0]
        complete = _month_complete(cal, t1)
        f = f.set_index("symbol")
        ret = panel.holding_returns(D, t1, f.index)
        fbk = panel.fallback_used(D, t1, f.index)
        for v in VERSIONS:
            lab = f[f"label_{v}"]
            if lab.isna().all():
                continue
            row = {"version": v, "month": t1.strftime("%Y-%m"), "formation": D,
                   "start": D, "end": t1, "complete": bool(complete)}
            row.update(month_row(lab.dropna(), f["mcap"], ret))
            row["n_sorted"] = int(lab.notna().sum())
            row["n_universe"] = int(in_universe(f, v).sum())
            row["n_missing_return"] = int(ret.reindex(lab.dropna().index).isna().sum())
            row["n_fallback_series"] = int(fbk.reindex(lab.dropna().index).sum())
            rows.append(row)
        h = f[["ticker", "mcap", "margin", "roce", "r6_pass"]
              + [f"label_{v}" for v in VERSIONS]].copy()
        h["ret"] = ret
        h["formation"], h["month"] = D, t1.strftime("%Y-%m")
        hold.append(h.reset_index())
    out = pd.DataFrame(rows).sort_values(["version", "month"]).reset_index(drop=True)
    return out, (pd.concat(hold, ignore_index=True) if hold else pd.DataFrame())


def _month_complete(cal: pd.DatetimeIndex, t1: pd.Timestamp) -> bool:
    """A month in the archive is complete only when a later month has started."""
    return bool((cal > pd.Timestamp(t1)).any())
