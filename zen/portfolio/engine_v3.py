"""Strategy v3 portfolio engine (research/strategy/v3-spec.md, committed ce91bdd).

v3 is one rule set, built from the spec's text. What is v1's or v2's plumbing is
called, not copied (spec section 15.3):

  point in time    zen/universe/pit.py: the snapshot at D (filings broadcast
                   before D 00:00, prices to the previous close, split and bonus
                   factors with ex-date before D), v1's universe (rules 1-7),
                   C4's basis and C6's share count (`pit.fundamentals`)
  EBIT lines,      zen/validation/factors.py: `quarter_extras` (the EBIT lines of
  balance sheets   the income-statement revision the snapshot kept) and
                   `latest_balance_sheets`
  sectors          engine_v2.nse_sectors (NSE's labels joined by stock id)
  prices, costs,   zen/portfolio/engine.py: the adjusted panel with the BZ rule,
  dividends,       the holdout guard, the equal-weight benchmark and the index
  benchmarks       clocks

Where the spec departs from that code, this module implements the text:

  basis b* (3.3a)  chosen per stock and review: consolidated when its eight
                   quarters ending at L* are complete, else standalone when its
                   are, else C4's basis (and the stock fails F5)
  write-back (4.5) a finance-cost quarter below -Rs 1 crore fails F2; ICR is
                   TTM EBIT over the positive finance costs only
  market cap (3.5) the latest EQ, BE or BZ close before D on D's share basis
                   times pit's share count, for members and holdings alike
  D/E (4.6)        from `latest_balance_sheets`' row directly; equity <= 0 fails
  orders (9.4)     every order's quantity is fixed at D: full sales sell every
                   unit, trims are sell-only orders for a fixed number of units,
                   buys are fixed rupee amounts

Sections 6 (ranking on sums of whole and half ranks), 7 to 11 (18 slots, the
sector cap of 4, exits E1 to E5, the valuation trim in units, the 10% cap, the
review order) and 13 (records) are implemented below in the spec's own order.
Every reading the text left open is listed in READINGS and written into each
run's metrics.json.

HOLDOUT LOCK: `simulate_v3` checks engine.guard on the panel's end; the panel
builder and index loaders check it too. Nothing in the information step
(`compute_review`) reads a price at or after D.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy.stats import rankdata

from zen.portfolio import engine, engine_v2
from zen.universe import pit
from zen.validation import factors

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- spec constants
N_SLOTS = 18                       # section 7
SECTOR_CAP = 4
UNCLASSIFIED = "unclassified"      # section 3.6
PROFIT_MIN = 2.5e8                 # F1: TTM profit of at least Rs 25 crore
ICR_MIN = 2.5                      # F2 and E2
WRITE_BACK = -1e7                  # a finance-cost quarter below -Rs 1 crore (4.5)
DE_MAX = 1.2                       # F3 (strictly below) and E3 (strictly above)
DE_FROM = pd.Timestamp("2022-11-15")   # the review of 15 November 2022 onward
PE_MAX = 60.0                      # F4
PE_TRIM = 85.0                     # 9.1
TRIM_PER_EXCESS = 2.0              # 2% of the holding per 1% above 85
CAP_WEIGHT = 0.10                  # 9.2
BASE_2Q = 1.25e8                   # 4.7: growth floor for a two-quarter sum
BASE_4Q = 2.5e8                    # and for a four-quarter sum
PEG_GROWTH_CAP = 0.50
BS_MAX_AGE_DAYS = 400              # 4.6
STALE_DAYS = 200                   # E5, as v1 rule 5
HALF_SLOT_DIV = 36                 # a buy below NAV_open / 36 is not placed
WINDOW = 8                         # quarters in the history window (F5)
COST = 0.002
WAIT_SESSIONS = 5                  # C10's window: D to the 5th session after D
NO_TRADE_EXIT = 20
INITIAL_CAPITAL = 5e5
FIRST_REVIEW = (2020, 2)
LAST_REVIEW = (2026, 8)
AGREEMENT_LAST = (2022, 11)
EXIT_CODES = ["E1", "E2", "E3", "E4", "E5"]
UNTESTED_ORDER = ["E1", "E2", "E3", "E4", "trim"]

READINGS = {
    "quarter_identity": "a quarter is (stock id, basis, quarter index) of pit's snapshot.quarters "
                        "(latest revision before D, scale screen applied, an income statement "
                        "reported); figures are looked up by quarter index L - k in basis b*",
    "window_complete": "each of the quarters L* - 7 .. L* is a row of snapshot.quarters in that "
                       "basis whose revenue, profit_normalised and ebitda are all present",
    "v1_basis_missing": "a stock with known quarters that pit.fundamentals leaves out (only "
                        "consolidated rows, fewer than four in its TTM) is on the standalone "
                        "basis for C4 with no quarter in it: no L, every figure missing, E5 fires",
    "run": "consecutive known quarters (rows, whatever they report) in b* ending at L, as "
           "pit.fundamentals' consec_quarters counts them",
    "missing_quarters": "quarters L - 0 .. L - 8 with no row in b* (YYYY-MM of the quarter's last "
                        "month), separated by ';'",
    "write_back_flag": "true when any of FC_0..FC_3 that exists is below -1e7; the ICR state is "
                       "unknown first if TTM_EBIT or FC+ is missing",
    "icr_value": "TTM_EBIT / FC+ written only in the state 'icr'",
    "down_flags_record": "DOWN0 and DOWN1 are written true or false when known and empty when "
                         "unknown; an unknown flag counts as false in F6 and E4",
    "de_record": "D/E is written only for a usable balance sheet with equity above zero; a usable "
                 "balance sheet with equity <= 0 fails F3 and fires E3",
    "close_record": "close is the raw close C, close_adj = C x pit.cumulative_factor (D's share "
                    "basis); market cap = close_adj x shares",
    "ticker": "the symbol traded at D: pit's member ticker (its last EQ/BE row before D); for a "
              "non-member the ticker of its close C; else the identity spell at the day before D",
    "owners_profit": "consolidated basis only: P_k x f_k with f_k = (eps_basic_k x N_k) / "
                     "profit_continuing_k of the consolidated row, N_k the standalone row's "
                     "shares_implied for the same quarter; f_k outside (0, 1.5] or missing -> 1",
    "shares_reference": "median over up to four earlier known quarters (standalone first) of "
                        "shares_implied / pit.cumulative_factor at the filing's broadcast date, i.e. "
                        "carried to D's share basis; shares_ref_over_n = that median / shares",
    "untested_trim": "'trim' is untested when the holding's P/E is unknown",
    "sell_order": "at a session, sales fill first in stock-id order, then buys; buys filling at "
                  "one session are scaled pro rata when cash is short (need summed in stock-id "
                  "order)",
    "fills_event": "fills.csv has one row per order: event 'fill' or 'cancel'; side is the "
                   "order's side (buy or sell)",
    "end_of_run": "no fills at the final open mark; orders still pending then are listed in "
                  "metrics.json and pending_at_end.csv, not as cancellations",
    "panel_start": "engine.build_panel from the first review (its 60-day lookback before it)",
}


# ---------------------------------------------------------------- helpers
def quarter_index(period_end) -> pd.Series:
    pe = pd.to_datetime(period_end)
    return pe.dt.year * 4 + (pe.dt.month - 1) // 3


def quarter_label(q: int) -> str:
    q = int(q)
    return f"{q // 4:04d}-{(q % 4) * 3 + 3:02d}"


def growth(new, old, base):
    """G(new, old) = (new - old) / max(old, B) (section 4.7)."""
    new = np.asarray(new, dtype=float)
    old = np.asarray(old, dtype=float)
    return (new - old) / np.maximum(old, base)


def _s4(X: np.ndarray, j: int) -> np.ndarray:
    """X_j + X_(j+1) + X_(j+2) + X_(j+3), from the left (section 4.10)."""
    return ((X[:, j] + X[:, j + 1]) + X[:, j + 2]) + X[:, j + 3]


def sector_keys(sectors: pd.DataFrame) -> pd.Series:
    """Sector key per stock id (section 3.6): the label casefolded when it is in
    NSE's current scheme, else 'unclassified'. Stocks absent here are
    'unclassified' too (see `key_of`)."""
    if sectors is None or not len(sectors):
        return pd.Series(dtype=object)
    sec = sectors["sector"]
    cur = sectors["label_scheme"].astype(object).eq("current") & sec.notna()
    out = pd.Series(UNCLASSIFIED, index=sectors.index, dtype=object)
    out[cur] = sec[cur].map(lambda s: str(s).casefold())
    return out


def key_of(keys: pd.Series, sym) -> str:
    v = keys.get(sym) if keys is not None and len(keys) else None
    return v if isinstance(v, str) and v else UNCLASSIFIED


# ---------------------------------------------------------------- inputs at D
DIAG_COLS = ["profit_continuing", "eps_basic"]


def quarter_table(con, snap: pit.Snapshot, ids) -> pd.DataFrame:
    """snap.quarters with factors.quarter_extras' EBIT lines and, for the
    owners'-profit diagnostic only, profit_continuing and eps_basic of the same
    filing (joined exactly as quarter_extras joins)."""
    q = factors.quarter_extras(con, snap, ids)
    if q.empty:
        return q.assign(**{c: np.nan for c in DIAG_COLS})
    x = con.execute(
        "SELECT symbol, period_end, broadcast_dt, consolidated, xbrl_url, "
        + ", ".join(DIAG_COLS) + " FROM financials WHERE broadcast_dt < ?",
        [snap.D.to_pydatetime()]).df()
    x["period_end"] = pd.to_datetime(x["period_end"])
    x["broadcast_dt"] = pd.to_datetime(x["broadcast_dt"])
    x["symbol"] = ids.for_events(x, "symbol", "broadcast_dt") if len(x) else x["symbol"]
    x = (x.sort_values(factors.BS_KEY + ["xbrl_url"]).drop_duplicates(factors.BS_KEY, keep="last")
          .drop(columns="xbrl_url"))
    return q.merge(x, on=factors.BS_KEY, how="left")


def market_caps(con, snap: pit.Snapshot, fund: pd.DataFrame, ids) -> pd.DataFrame:
    """Section 3.5 for every stock with a close in the snapshot's price window:
    C = the latest close strictly before D in EQ, BE (snap.px) or BZ
    (prices_other), the EQ/BE close when both report one that day; C on D's
    share basis (x pit.cumulative_factor); N = pit's share count at D."""
    D = snap.D
    lo = (D - pd.Timedelta(days=pit.PRICE_LOOKBACK_DAYS)).date()
    eq = snap.px[["symbol", "ticker", "date", "series", "close", "cum"]].copy()
    eq["fb"] = 0
    bz = con.execute(
        "SELECT symbol, isin_code, date, series, close, turnover FROM prices_other "
        "WHERE series = 'BZ' AND date >= ? AND date < ? AND close > 0", [lo, D.date()]).df()
    if len(bz):
        bz["date"] = pd.to_datetime(bz["date"])
        bz["ticker"] = bz["symbol"]
        bz["symbol"] = ids.for_prices(bz) if ids is not None else bz["symbol"]
        bz = (bz.sort_values(["turnover", "ticker"], ascending=[False, True])
                .drop_duplicates(["symbol", "date"]))
        bz["cum"] = pit.cumulative_factor(bz, snap.factors)
        bz["fb"] = 1
        px = pd.concat([eq, bz[["symbol", "ticker", "date", "series", "close", "cum", "fb"]]],
                       ignore_index=True)
    else:
        px = eq
    # latest date first, EQ/BE before BZ on the same date
    last = (px.sort_values(["symbol", "date", "fb"], ascending=[True, False, True])
              .drop_duplicates("symbol", keep="first").set_index("symbol"))
    out = pd.DataFrame(index=pd.Index(sorted(set(last.index) | set(fund.index)), name="symbol"))
    out["close"] = last["close"].reindex(out.index).astype(float)
    out["close_factor"] = last["cum"].reindex(out.index).astype(float)
    out["close_adj"] = out["close"] * out["close_factor"]
    out["close_date"] = last["date"].reindex(out.index)
    out["close_series"] = last["series"].reindex(out.index)
    out["close_ticker"] = last["ticker"].reindex(out.index)
    sh = fund["shares"].reindex(out.index).astype(float) if len(fund) else np.nan
    out["shares"] = sh
    out["shares_fix"] = (fund["shares_fix"].reindex(out.index) if len(fund)
                         else pd.Series(np.nan, index=out.index))
    ok = out["close_adj"].notna() & np.isfinite(out["shares"]) & (out["shares"] > 0)
    out["mcap"] = (out["close_adj"] * out["shares"]).where(ok)
    return out


def share_reference(snap: pit.Snapshot) -> pd.Series:
    """Diagnostic (section 13): C30b's reference count carried to D's share
    basis: the median of up to four earlier known quarters' implied counts
    (standalone first), each divided by pit.cumulative_factor at its filing's
    broadcast date."""
    q_all = snap.quarters
    if q_all.empty:
        return pd.Series(dtype=float)
    lq = q_all.groupby("symbol")["q"].max()
    per_q = q_all[q_all["shares_implied"] > 0]
    per_q = (per_q.sort_values(["symbol", "q", "consolidated"])
                  .drop_duplicates(["symbol", "q"])
                  [["symbol", "q", "shares_implied", "broadcast_dt"]])
    per_q = per_q[per_q["q"] < per_q["symbol"].map(lq)]
    per_q = per_q.sort_values(["symbol", "q"]).groupby("symbol").tail(pit.SHARES_REF_QUARTERS)
    if per_q.empty:
        return pd.Series(dtype=float)
    per_q = per_q.assign(date=per_q["broadcast_dt"].dt.normalize())
    per_q["C"] = pit.cumulative_factor(per_q, snap.factors)
    per_q["units_D"] = per_q["shares_implied"] / per_q["C"]
    return per_q.groupby("symbol")["units_D"].median()


# ---------------------------------------------------------------- figures
FIG_COLS = ["R", "P", "E", "PBE", "FC", "EPS", "PC"]
SRC = {"R": "revenue", "P": "profit_normalised", "E": "ebitda", "FC": "finance_costs",
       "EPS": "eps_basic", "PC": "profit_continuing"}


def stock_figures(q: pd.DataFrame, fund: pd.DataFrame, caps: pd.DataFrame,
                  bs: pd.DataFrame, D, members: pd.Index,
                  share_ref: pd.Series | None = None) -> pd.DataFrame:
    """Every v3 figure, filter and exit input of every stock with a known
    quarter at D (sections 3.3a to 5, 8 and 12). Pure: no database.

    q        quarter_table() output (one row per stock id, basis, quarter)
    fund     pit.fundamentals() output (C4's basis, latest_period_end, shares)
    caps     market_caps() output
    bs       factors.latest_balance_sheets() output
    members  v1's universe at D (stock ids)
    """
    D = pd.Timestamp(D).normalize()
    q = q.copy()
    q["cons"] = q["consolidated"].astype(bool)
    if "q" not in q:
        q["q"] = quarter_index(q["period_end"])
    q["q"] = q["q"].astype(int)
    # one row per (stock, basis, quarter index): the latest period_end, then revision
    q = (q.sort_values(["symbol", "cons", "q", "period_end", "broadcast_dt"])
          .drop_duplicates(["symbol", "cons", "q"], keep="last").reset_index(drop=True))
    for c in ("pbt_before_exceptional", "pbt", "exceptional_items", "finance_costs",
              "eps_basic", "profit_continuing", "shares_implied"):
        if c not in q:
            q[c] = np.nan
    syms = pd.Index(sorted(q["symbol"].unique()), name="symbol")
    n = len(syms)
    out = pd.DataFrame(index=syms)
    if n == 0:
        return out

    full = q[["revenue", "profit_normalised", "ebitda"]].notna().all(axis=1)
    Lstar = q.groupby("symbol")["q"].max().reindex(syms)
    q["Lstar"] = q["symbol"].map(Lstar)
    inw = full & (q["q"] <= q["Lstar"]) & (q["q"] > q["Lstar"] - WINDOW)
    cnt = q[inw].groupby(["symbol", "cons"])["q"].nunique().unstack()

    def comp(b):
        if cnt is None or b not in cnt.columns:
            return np.zeros(n, dtype=bool)
        return (cnt[b].reindex(syms) == WINDOW).fillna(False).to_numpy(dtype=bool)

    comp_c, comp_s = comp(True), comp(False)
    complete = comp_c | comp_s
    in_fund = syms.isin(fund.index) if len(fund) else np.zeros(n, dtype=bool)
    v1b = (fund["consolidated"].reindex(syms).astype(object).fillna(False).astype(bool)
           .to_numpy() if len(fund) else np.zeros(n, dtype=bool))
    basis = np.where(comp_c, True, np.where(comp_s, False, v1b))
    Lb = q.groupby(["symbol", "cons"])["q"].max()
    Lv1 = Lb.reindex(pd.MultiIndex.from_arrays([syms, basis])).to_numpy(dtype=float)
    L = np.where(complete, Lstar.to_numpy(dtype=float), Lv1)
    has_L = np.isfinite(L)
    Li = np.where(has_L, L, -10**6).astype(np.int64)

    key = pd.MultiIndex.from_arrays([q["symbol"], q["cons"], q["q"]])

    def lookup(k: int, b=None) -> np.ndarray:
        bb = basis if b is None else np.full(n, b)
        return key.get_indexer(pd.MultiIndex.from_arrays([syms, bb, Li - k]))

    K = 9                                   # Q_0 .. Q_8
    idx = np.stack([lookup(k) for k in range(K)], axis=1)
    present = idx >= 0
    present[~has_L] = False

    def mat(col: str, ix=idx) -> np.ndarray:
        v = q[col].to_numpy(dtype=float)
        return np.where(ix >= 0, v[np.where(ix >= 0, ix, 0)], np.nan)

    X = {k: mat(c) for k, c in SRC.items()}
    pbe_line, pbt, exc = mat("pbt_before_exceptional"), mat("pbt"), mat("exceptional_items")
    X["PBE"] = np.where(np.isfinite(pbe_line), pbe_line, pbt - np.nan_to_num(exc, nan=0.0))
    sa_idx = np.stack([lookup(k, False) for k in range(K)], axis=1)
    N_sa = mat("shares_implied", sa_idx)
    R, P, E, FC = X["R"], X["P"], X["E"], X["FC"]

    # the run ending at L in b*
    run = np.zeros(n, dtype=int)
    alive = present[:, 0].copy()
    k = 0
    while alive.any():
        run[alive] += 1
        k += 1
        alive = alive & (key.get_indexer(pd.MultiIndex.from_arrays([syms, basis, Li - k])) >= 0)

    with np.errstate(invalid="ignore", divide="ignore"):
        ttm_R, ttm4_R = _s4(R, 0), _s4(R, 4)
        ttm_P, ttm4_P = _s4(P, 0), _s4(P, 4)
        ttm_E, ttm4_E = _s4(E, 0), _s4(E, 4)
        A_R = (growth(R[:, 0] + R[:, 1], R[:, 4] + R[:, 5], BASE_2Q)
               - growth(R[:, 2] + R[:, 3], R[:, 6] + R[:, 7], BASE_2Q))
        A_P = (growth(P[:, 0] + P[:, 1], P[:, 4] + P[:, 5], BASE_2Q)
               - growth(P[:, 2] + P[:, 3], P[:, 6] + P[:, 7], BASE_2Q))
        M = ttm_E / ttm_R - ttm4_E / ttm4_R
        g = growth(ttm_P, ttm4_P, BASE_4Q)
        EBIT = X["PBE"][:, :4] + FC[:, :4]
        ttm_ebit = _s4(EBIT, 0)
        fcp = np.maximum(FC[:, :4], 0.0)           # NaN stays NaN
        fc_pos = _s4(fcp, 0)
        write_back = (FC[:, :4] < WRITE_BACK).any(axis=1)
        icr_known = np.isfinite(ttm_ebit) & np.isfinite(fc_pos)
        icr_state = np.where(~icr_known, "unknown",
                             np.where(write_back, "write_back",
                                      np.where(fc_pos == 0, "no_finance_costs", "icr")))
        ratio = np.where(icr_known & (fc_pos > 0), ttm_ebit / np.where(fc_pos > 0, fc_pos, 1.0),
                         np.nan)
        icr = np.where(icr_state == "icr", ratio, np.nan)
        down0_known = np.isfinite(P[:, 0:8]).all(axis=1)
        down1_known = np.isfinite(P[:, 1:9]).all(axis=1)
        down0 = down0_known & (_s4(P, 0) < _s4(P, 4))
        down1 = down1_known & ((((P[:, 1] + P[:, 2]) + P[:, 3]) + P[:, 4])
                               < (((P[:, 5] + P[:, 6]) + P[:, 7]) + P[:, 8]))

    out["c4_basis"] = np.where(in_fund, np.where(v1b, "consolidated", "standalone"), "")
    out["basis"] = np.where(basis, "consolidated", "standalone")
    out["complete"] = complete
    out["Lq"] = np.where(has_L, Li, -1)
    out["L"] = [quarter_label(x) if h else "" for x, h in zip(Li, has_L)]
    out["run"] = run
    miss = []
    for i in range(n):
        if not has_L[i]:
            miss.append("")
            continue
        miss.append(";".join(quarter_label(Li[i] - k) for k in range(K) if not present[i, k]))
    out["missing_quarters"] = miss
    out["ttm_r"], out["ttm4_r"] = ttm_R, ttm4_R
    out["ttm_p"], out["ttm4_p"] = ttm_P, ttm4_P
    out["ttm_e"], out["ttm4_e"] = ttm_E, ttm4_E
    out["A_R"], out["A_P"], out["M"], out["g"] = A_R, A_P, M, g
    out["ttm_ebit"], out["fc_pos"] = ttm_ebit, fc_pos
    out["write_back"] = write_back
    out["icr_state"] = icr_state
    out["icr"] = icr
    out["icr_ratio"] = ratio
    out["down0"] = pd.array(np.where(down0_known, down0, None), dtype="boolean")
    out["down1"] = pd.array(np.where(down1_known, down1, None), dtype="boolean")

    # market cap and P/E (sections 3.5 and 4.4)
    c = caps.reindex(syms) if caps is not None and len(caps) else pd.DataFrame(index=syms)
    for col in ("close", "close_factor", "close_adj", "close_date", "close_series",
                "close_ticker", "shares", "shares_fix", "mcap"):
        out[col] = c[col] if col in c else np.nan
    mcap = out["mcap"].to_numpy(dtype=float)
    with np.errstate(invalid="ignore", divide="ignore"):
        pe = np.where(np.isfinite(mcap) & (ttm_P > 0), mcap / ttm_P, np.nan)
        peg = np.where(g > 0, pe / (100 * np.minimum(g, PEG_GROWTH_CAP)), np.nan)
    out["pe"], out["PEG"] = pe, peg

    # balance sheet in b* (section 4.6)
    out["bs_period_end"] = pd.NaT
    out["equity"] = np.nan
    out["debt"] = np.nan
    if bs is not None and len(bs):
        b = bs.copy()
        b["consolidated"] = b["consolidated"].astype(bool)
        b = b.drop_duplicates(["symbol", "consolidated"], keep="last").set_index(
            ["symbol", "consolidated"])
        bi = b.index.get_indexer(pd.MultiIndex.from_arrays([syms, basis]))
        ok = bi >= 0
        pe_b = pd.to_datetime(b["period_end"]).to_numpy()
        out["bs_period_end"] = pd.to_datetime(np.where(ok, pe_b[np.where(ok, bi, 0)],
                                                       np.datetime64("NaT", "ns")))
        eqv = b["equity"].to_numpy(dtype=float)
        dbt = b["debt_total"].astype(float).fillna(0.0).to_numpy()
        out["equity"] = np.where(ok, eqv[np.where(ok, bi, 0)], np.nan)
        out["debt"] = np.where(ok, dbt[np.where(ok, bi, 0)], np.nan)
    age = (D - pd.to_datetime(out["bs_period_end"])).dt.days
    usable = (age <= BS_MAX_AGE_DAYS).fillna(False).to_numpy(dtype=bool)
    eqv = out["equity"].to_numpy(dtype=float)
    with np.errstate(invalid="ignore", divide="ignore"):
        de = np.where(usable & (eqv > 0), out["debt"].to_numpy(dtype=float) / eqv, np.nan)
    out["bs_usable"] = usable
    out["de"] = de
    de_on = D >= DE_FROM

    # filters (section 5)
    out["in_universe"] = syms.isin(members)
    out["f0"] = out["in_universe"]
    out["f1"] = ttm_P >= PROFIT_MIN
    out["f2"] = (icr_state == "no_finance_costs") | ((icr_state == "icr") & (icr >= ICR_MIN))
    if de_on:
        out["f3"] = ~usable | ((eqv > 0) & (de < DE_MAX))
    else:
        out["f3"] = True
    out["f4"] = np.isfinite(pe) & (pe > 0) & (pe <= PE_MAX)
    out["f5"] = complete & (ttm_R > 0) & (ttm4_R > 0)
    out["f6"] = ~(down0 & down1)
    fl = ["f0", "f1", "f2", "f3", "f4", "f5", "f6"]
    out["eligible"] = out[fl].all(axis=1)

    # exit inputs (section 8)
    out["e1_known"] = np.isfinite(ttm_P)
    out["e1"] = np.isfinite(ttm_P) & (ttm_P <= 0)
    out["e2_known"] = icr_known
    out["e2"] = icr_known & (fc_pos > 0) & (ratio < ICR_MIN)
    out["e3_on"] = de_on
    out["e3_known"] = usable if de_on else False
    out["e3"] = (usable & ((eqv <= 0) | (de > DE_MAX))) if de_on else False
    out["down_both"] = down0 & down1
    out["down_known"] = down0_known & down1_known
    lpe = (pd.to_datetime(fund["latest_period_end"]).reindex(syms) if len(fund)
           else pd.Series(pd.NaT, index=syms))
    out["latest_period_end_c4"] = lpe
    stale = (D - lpe).dt.days
    out["e5"] = (lpe.isna() | (stale > STALE_DAYS)).to_numpy(dtype=bool)

    # diagnostics no rule uses (section 13)
    EPS, PC = X["EPS"], X["PC"]
    with np.errstate(invalid="ignore", divide="ignore"):
        f = (EPS[:, :4] * N_sa[:, :4]) / PC[:, :4]
        f = np.where(np.isfinite(f) & (f > 0) & (f <= 1.5), f, 1.0)
        own = P[:, :4] * f
        own_ttm = np.where(basis, _s4(own, 0), np.nan)
        out["owners_ttm_p"] = own_ttm
        out["owners_pe"] = np.where(np.isfinite(mcap) & (own_ttm > 0), mcap / own_ttm, np.nan)
    ref = share_ref.reindex(syms) if share_ref is not None and len(share_ref) else \
        pd.Series(np.nan, index=syms)
    # recorded only where N is above zero, i.e. where a market cap can exist
    # (Clarification of 12 October 2026); a diagnostic that no rule uses
    n_pos = out["shares"].to_numpy(dtype=float) > 0
    out["shares_ref"] = np.where(n_pos, ref.to_numpy(dtype=float), np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        out["shares_ref_over_n"] = np.where(n_pos, out["shares_ref"]
                                            / out["shares"].to_numpy(dtype=float), np.nan)
    return out


def v3_funnel(figs: pd.DataFrame) -> dict:
    """v1-universe members remaining after F1, then F2, ... then F6."""
    m = figs[figs["f0"]]
    out, keep = {}, np.ones(len(m), dtype=bool)
    for k in ("f1", "f2", "f3", "f4", "f5", "f6"):
        keep = keep & m[k].to_numpy(dtype=bool)
        out[f"n_{k}"] = int(keep.sum())
    return out


# ---------------------------------------------------------------- ranking (section 6)
RANK_COLS = ["A_R", "A_P", "M", "g", "PEG", "r_A_R", "r_A_P", "R1", "R2", "R3", "T", "rank",
             "S1", "S2", "S3", "S", "pct_A_R", "pct_A_P", "pct_M", "pe", "sector_key", "ticker"]


def rank_eligible(e: pd.DataFrame) -> pd.DataFrame:
    """The section-6 order of the eligible stocks (index: stock id; columns A_R,
    A_P, M, g, PEG, pe, ticker, sector_key). Returns the frame in rank order."""
    N = len(e)
    if N == 0:
        return pd.DataFrame(columns=RANK_COLS, index=pd.Index([], name="symbol"))
    for c in ("A_R", "A_P", "M", "g"):
        if not np.isfinite(e[c].to_numpy(dtype=float)).all():
            bad = e.index[~np.isfinite(e[c].to_numpy(dtype=float))].tolist()[:5]
            raise ValueError(f"measure {c} missing for eligible stocks {bad}")
    g = e["g"].to_numpy(dtype=float)
    pos = g > 0
    peg = e["PEG"].to_numpy(dtype=float)
    if not np.isfinite(peg[pos]).all():
        raise ValueError("PEG missing for an eligible stock with positive growth")
    r_ar = rankdata(e["A_R"].to_numpy(dtype=float), method="average")
    r_ap = rankdata(e["A_P"].to_numpy(dtype=float), method="average")
    R1 = rankdata(r_ar + r_ap, method="average")
    R2 = rankdata(e["M"].to_numpy(dtype=float), method="average")
    n0 = int((~pos).sum())
    R3 = np.full(N, (n0 + 1) / 2)
    if pos.any():
        R3[pos] = n0 + rankdata(-peg[pos], method="average")
    T = (R1 + R2) + R3
    out = e[[c for c in ("A_R", "A_P", "M", "g", "PEG", "pe", "sector_key", "ticker")
             if c in e]].copy()
    out["r_A_R"], out["r_A_P"] = r_ar, r_ap
    out["R1"], out["R2"], out["R3"], out["T"] = R1, R2, R3, T
    out["S1"], out["S2"], out["S3"] = R1 / N, R2 / N, R3 / N
    out["S"] = T / (3 * N)
    out["pct_A_R"], out["pct_A_P"], out["pct_M"] = r_ar / N, r_ap / N, R2 / N
    tick = out["ticker"].astype(object).fillna("").astype(str) if "ticker" in out else \
        pd.Series(out.index.astype(str), index=out.index)
    order = sorted(range(N), key=lambda i: (-T[i], tick.iloc[i], str(out.index[i])))
    out = out.iloc[order]
    out["rank"] = np.arange(1, N + 1)
    return out[[c for c in RANK_COLS if c in out]]


# ---------------------------------------------------------------- one review's information
@dataclass
class Review:
    D: pd.Timestamp
    figs: pd.DataFrame              # every stock with a known quarter (and members)
    ranks: pd.DataFrame             # eligible stocks in rank order
    funnel: dict                    # v1's funnel and the v3 funnel
    diag: dict = field(default_factory=dict)


def compute_review(con, D, static: pit.StaticLabels, keys: pd.Series,
                   tickers: engine_v2.Tickers | None = None) -> Review:
    """Step 3 of section 10: the information set at D for every stock."""
    D = pd.Timestamp(D).normalize()
    tickers = tickers or engine_v2.Tickers()
    snap = pit.build_snapshot(con, D, static)
    members, funnel, fund = pit.universe(snap, static)
    q = quarter_table(con, snap, static.ids)
    caps = market_caps(con, snap, fund, static.ids)
    bs = factors.latest_balance_sheets(con, snap, static.ids)
    figs = stock_figures(q, fund, caps, bs, D, members.index, share_reference(snap))
    # members always have a known quarter; anything else with a close but no
    # quarter has no figures and is never eligible
    miss = members.index.difference(figs.index)
    if len(miss):
        raise RuntimeError(f"{D.date()}: members without figures: {list(miss)[:5]}")
    tick = members["ticker"].reindex(figs.index).astype(object)
    tick = tick.where(tick.notna(), figs["close_ticker"].astype(object))
    no_t = tick.isna()
    if no_t.any():
        tick[no_t] = [tickers.at(s, D - pd.Timedelta(days=1)) for s in figs.index[no_t]]
    figs["ticker"] = tick
    figs["sector_key"] = [key_of(keys, s) for s in figs.index]
    figs.insert(0, "D", D)
    ranks = rank_eligible(figs[figs["eligible"]])
    ranks.insert(0, "D", D)
    fn = {**funnel, **v3_funnel(figs), "n_eligible": int(figs["eligible"].sum())}
    pm = members["mcap"].reindex(figs.index[figs["f0"]])
    mm = figs.loc[figs["f0"], "mcap"]
    rel = ((mm - pm).abs() / pm.abs())
    diag = {"n_members": int(len(members)),
            "n_mcap_differs_from_pit": int((rel > 1e-12).sum()),
            "n_member_close_bz": int((figs.loc[figs["f0"], "close_series"] == "BZ").sum()),
            "n_basis_differs_from_c4": int((figs.loc[figs["f0"], "basis"]
                                            != figs.loc[figs["f0"], "c4_basis"]).sum())}
    return Review(D=D, figs=figs, ranks=ranks, funnel=fn, diag=diag)


# ---------------------------------------------------------------- the review plan (section 10)
@dataclass
class Plan:
    orders: list                   # dicts: symbol, kind, side, full, units (trim n), value (buy V), reason
    U0: dict                       # stock id -> U0 after step 4 (nan = not set)
    holdings: list                 # holdings.csv rows
    decision: dict                 # decisions.csv fields decided here


def _flag(v):
    return None if v is None or (isinstance(v, float) and np.isnan(v)) or v is pd.NA else bool(v)


def plan_review(D, hold: pd.DataFrame, figs: pd.DataFrame, ranks: pd.DataFrame,
                keys: pd.Series, nav_open: float, cash: float, order=None,
                n_slots: int = N_SLOTS, sector_cap: int = SECTOR_CAP,
                cost: float = COST) -> Plan:
    """Steps 4 to 11 of section 10 at review D. Pure.

    hold   one row per stock held at D's open (units > 0): units, px_now, U0
           (nan when not set), held_prev (held at the previous review's open)
    figs   compute_review's figures (index stock id); a holding absent from it
           has no known quarter at all
    ranks  the eligible stocks in rank order (index stock id)
    order  optional callable(D, candidates) -> candidates reordered (the
           monkey test); the rank order otherwise
    """
    D = pd.Timestamp(D)
    orders, rows, U0_new = [], [], {}
    counts = {f"n_exit_{c}": 0 for c in EXIT_CODES}
    counts.update({"n_exit_V": 0, "n_trim_valuation": 0, "n_trim_cap": 0})
    unt_counts = {f"n_untested_{c}": 0 for c in UNTESTED_ORDER}
    n_untested = n_outside = n_uncl = 0
    kept_keys = []
    sell_value = []
    for sym in sorted(hold.index):
        h = hold.loc[sym]
        units, px, held_prev = float(h["units"]), float(h["px_now"]), bool(h["held_prev"])
        u0 = float(h["U0"]) if pd.notna(h["U0"]) else np.nan
        f = figs.loc[sym] if sym in figs.index else None
        pe = float(f["pe"]) if f is not None and pd.notna(f["pe"]) else np.nan
        # step 4: reference units
        if np.isfinite(pe) and pe > PE_TRIM:
            if not np.isfinite(u0):
                u0 = units
        elif np.isfinite(pe):
            u0 = np.nan
        U0_new[sym] = u0
        # step 5: exits
        codes, untested = [], []
        if f is None:
            codes = ["E5"]
            untested = ["E1", "E2"] + (["E3"] if D >= DE_FROM else []) + \
                (["E4"] if held_prev else []) + ["trim"]
            d0 = d1 = None
        else:
            if f["e1"]:
                codes.append("E1")
            if f["e2"]:
                codes.append("E2")
            if f["e3_on"] and f["e3"]:
                codes.append("E3")
            if held_prev and bool(f["down_both"]):
                codes.append("E4")
            if f["e5"]:
                codes.append("E5")
            if not f["e1_known"]:
                untested.append("E1")
            if not f["e2_known"]:
                untested.append("E2")
            if f["e3_on"] and not f["e3_known"]:
                untested.append("E3")
            if held_prev and not bool(f["down_known"]):
                untested.append("E4")
            if not np.isfinite(pe):
                untested.append("trim")
            d0, d1 = _flag(f["down0"]), _flag(f["down1"])
        excess = pe / PE_TRIM - 1 if np.isfinite(pe) and pe > PE_TRIM else np.nan
        # step 6: valuation target
        u_V = np.nan
        if np.isfinite(excess) and np.isfinite(u0):
            u_V = max(0.0, 1 - TRIM_PER_EXCESS * excess) * u0
        full = bool(codes) or (np.isfinite(u_V) and u_V == 0)
        if not codes and full:
            codes = ["V"]
        if full and codes[0] != "V":
            # A full sale under section 8: nothing more of section 9.1 applies (its step 3),
            # so no valuation target is recorded and the trim is not a test left untested
            # (Clarification of 12 October 2026). The order placed is the same either way.
            u_V = np.nan
            untested = [u for u in untested if u != "trim"]
        u_C = np.nan
        to_sell = 0.0
        status, kind = "kept", None
        if full:
            status, kind, to_sell = "exited", ("exit" if codes[0] != "V" else "valuation"), units
            counts["n_exit_" + codes[0]] += 1
            orders.append({"symbol": sym, "kind": kind, "side": "sell", "full": True,
                           "units": units, "value": np.nan, "reason": codes[0]})
        else:
            # step 7: the 10% cap
            u_C = CAP_WEIGHT * nav_open / px
            u_star = min(u_V, u_C) if np.isfinite(u_V) else u_C
            if units > u_star:
                to_sell = units - u_star
                kind = "valuation" if (np.isfinite(u_V) and u_V <= u_C) else "cap"
                status = f"trimmed_{kind}"
                counts[f"n_trim_{kind}"] += 1
                orders.append({"symbol": sym, "kind": kind, "side": "sell", "full": False,
                               "units": to_sell, "value": np.nan, "reason": f"trim_{kind}"})
            kept_keys.append(key_of(keys, sym))
        if to_sell > 0:
            sell_value.append(to_sell * px * (1 - cost))
        if untested:
            n_untested += 1
            for u in untested:
                unt_counts[f"n_untested_{u}"] += 1
        in_u = bool(f["in_universe"]) if f is not None else False
        n_outside += 0 if in_u else 1
        n_uncl += key_of(keys, sym) == UNCLASSIFIED
        rk = ranks["rank"].get(sym) if len(ranks) else None
        rows.append({"D": D, "symbol": sym, "ticker": f["ticker"] if f is not None else None,
                     "status": status, "exit_codes": ";".join(codes) if full else "",
                     "units_open": units, "px_now": px, "weight_open": units * px / nav_open,
                     "pe": pe, "excess": excess, "U0": u0, "u_V": u_V, "u_C": u_C,
                     "units_to_sell": to_sell, "buy_value": np.nan, "down0": d0, "down1": d1,
                     "untested": ";".join(untested), "rank": rk,
                     "sector_key": key_of(keys, sym), "in_universe": in_u,
                     "eligible": bool(f["eligible"]) if f is not None else False})

    # step 8: empty slots
    kept = len(kept_keys)
    empty = max(0, n_slots - kept)
    # step 9: candidates, leaving out every stock held at D's open
    cand = ranks[~ranks.index.isin(hold.index)] if len(ranks) else ranks
    if order is not None and len(cand):
        cand = order(D, cand)
    # step 10: budget
    B = cash
    for v in sell_value:
        B = B + v
    # step 11: fill
    cnt: dict = {}
    for k in kept_keys:
        cnt[k] = cnt.get(k, 0) + 1
    B_left, filled = B, 0
    skipped_cap = stopped_half = False
    slot, half = nav_open / n_slots, nav_open / HALF_SLOT_DIV
    for sym in cand.index:
        if filled >= empty:
            break
        k = key_of(keys, sym)
        if cnt.get(k, 0) >= sector_cap:
            skipped_cap = True
            continue
        V = min(slot, B_left / (1 + cost))
        if V < half:
            stopped_half = True
            break
        orders.append({"symbol": sym, "kind": "buy", "side": "buy", "full": False,
                       "units": np.nan, "value": V, "reason": "buy"})
        B_left = B_left - V * (1 + cost)
        cnt[k] = cnt.get(k, 0) + 1
        filled += 1
        f = figs.loc[sym]
        rows.append({"D": D, "symbol": sym, "ticker": f["ticker"], "status": "new",
                     "exit_codes": "", "units_open": 0.0, "px_now": np.nan,
                     "weight_open": 0.0, "pe": float(f["pe"]), "excess": np.nan, "U0": np.nan,
                     "u_V": np.nan, "u_C": np.nan, "units_to_sell": 0.0, "buy_value": V,
                     "down0": _flag(f["down0"]), "down1": _flag(f["down1"]), "untested": "",
                     "rank": int(cand.at[sym, "rank"]) if "rank" in cand else None,
                     "sector_key": k, "in_universe": bool(f["in_universe"]),
                     "eligible": bool(f["eligible"])})
    left = empty - filled
    reasons = []
    if left > 0:
        if not stopped_half:
            reasons.append("none_eligible")
        if skipped_cap:
            reasons.append("sector_cap")
        if stopped_half:
            reasons.append("half_slot_minimum")
    decision = {"D": D, "nav_open": nav_open, "cash_open": cash, "budget_B": B,
                "budget_left": B_left, "n_held_open": int(len(hold)), "n_kept": kept,
                **counts, "empty_slots": empty, "n_buys": filled, "slots_left_empty": left,
                "empty_reasons": ";".join(reasons), "n_untested": n_untested, **unt_counts,
                "n_held_outside_universe": n_outside, "n_held_unclassified": int(n_uncl)}
    return Plan(orders=orders, U0=U0_new, holdings=rows, decision=decision)


# ---------------------------------------------------------------- simulation (sections 9.4, 10, 11)
FILL_COLUMNS = ["date", "D", "symbol", "ticker", "kind", "side", "event", "units", "price",
                "value", "cost", "reason"]


@dataclass
class ResultV3:
    nav: pd.DataFrame
    fills: pd.DataFrame
    trades: pd.DataFrame
    holdings: pd.DataFrame
    decisions: pd.DataFrame
    episodes: pd.DataFrame
    cash: pd.Series
    rebalance_open: pd.Series
    pending_at_end: pd.DataFrame


def simulate_v3(panel: engine.Panel, decisions, reviews: dict, keys: pd.Series,
                is_end, run_final_test: bool = False,
                tickers: engine_v2.Tickers | None = None, order=None,
                cost: float = COST, wait: int = WAIT_SESSIONS, no_trade_exit: int = NO_TRADE_EXIT,
                initial_capital: float = INITIAL_CAPITAL, log_trades: bool = True) -> ResultV3:
    """Run the v3 book. reviews[D] is a Review (figs and ranks at D)."""
    engine.guard(panel.end, is_end, run_final_test)
    tickers = tickers or engine_v2.Tickers()
    dates, syms = panel.dates, panel.symbols
    T, S = len(dates), len(syms)
    decisions = [pd.Timestamp(d) for d in decisions]
    idx = dates.get_indexer(decisions)
    if (idx < 0).any():
        raise ValueError("a review date is not a session in the panel")
    rebal = dict(zip(idx.tolist(), decisions))
    t0, t_end = int(idx[0]), T - 1
    if max(rebal) >= t_end:
        raise ValueError("a review falls on or after the end mark")

    units = np.zeros(S)
    U0 = np.full(S, np.nan)
    cash = float(initial_capital)
    entry_t = np.full(S, -1)
    entry_px = np.full(S, np.nan)
    nav_out = np.full(T, np.nan)
    cash_out = np.full(T, np.nan)
    fills, trades, hold_rows, dec_rows, episodes = [], [], [], [], []
    open_marks: dict = {}
    pending: list[dict] = []
    prev_open: set = set()
    cur_D = None

    def tk(s, t):
        return tickers.at(syms[s], dates[t])

    def record(t, o, event, du, px, val, reason=None):
        if not log_trades:
            return
        d = dates[t].date()
        fills.append({"date": d, "D": o["D"].date(), "symbol": syms[o["s"]], "ticker": tk(o["s"], t),
                      "kind": o["kind"], "side": o["side"], "event": event, "units": du,
                      "price": px, "value": val, "cost": val * cost,
                      "reason": reason or o["reason"]})
        trades.append({"date": d, "symbol": syms[o["s"]],
                       "side": o["side"] if event == "fill" else "cancel", "units": du,
                       "price": px, "value": val, "cost": val * cost,
                       "reason": reason or o["reason"], "kind": o["kind"], "D": o["D"].date(),
                       "ticker": tk(o["s"], t)})

    def close_episode(s, t, reason):
        episodes.append({"symbol": syms[s], "entry": dates[entry_t[s]], "exit": dates[t],
                         "entry_px": entry_px[s], "entry_t": int(entry_t[s]), "exit_t": t,
                         "exit_reason": reason})
        entry_t[s] = -1
        entry_px[s] = np.nan

    for t in range(t0, T):
        # step 0: dividends to positions held at the previous close
        if t > t0:
            cash += float(units @ panel.div_unit[t])

        if t in rebal:
            D = rebal[t]
            cur_D = D
            rv = reviews[D]
            # step 1: value the book
            px_now = np.where(panel.traded[t], panel.open_[t],
                              panel.last[t - 1] if t > 0 else np.nan)
            nav_open = cash + float(np.nansum(units * px_now))
            open_marks[dates[t]] = nav_open
            # step 2: cancel pending orders (a safeguard; cannot fire)
            for o in pending:
                record(t, o, "cancel", 0.0, np.nan, 0.0, "cancelled at the next review")
            pending = []
            held = np.flatnonzero(units > 0)
            hold = pd.DataFrame({"units": units[held], "px_now": px_now[held], "U0": U0[held],
                                 "held_prev": [syms[s] in prev_open for s in held]},
                                index=pd.Index(syms[held], name="symbol"))
            plan = plan_review(D, hold, rv.figs, rv.ranks, keys, nav_open, cash, order,
                               cost=cost)
            for sym, u in plan.U0.items():
                U0[syms.get_loc(sym)] = u
            prev_open = set(hold.index)
            for o in plan.orders:
                o = dict(o)
                o["s"] = syms.get_loc(o["symbol"])
                o["D"] = D
                o["deadline"] = t + wait
                pending.append(o)
            for r in plan.holdings:
                hold_rows.append(r)
            dec_rows.append({**plan.decision, **rv.funnel})

        # step 12 / section 11: fill pending orders (none at the final mark)
        if t != t_end and pending:
            op = panel.open_[t]
            done = set()
            sells = sorted([o for o in pending if o["side"] == "sell" and panel.traded[t, o["s"]]],
                           key=lambda o: o["s"])
            for o in sells:
                s = o["s"]
                du = units[s] if o["full"] else min(o["units"], units[s])
                val = du * op[s]
                units[s] = 0.0 if o["full"] else units[s] - du
                cash += val * (1 - cost)
                record(t, o, "fill", du, op[s], val)
                done.add(id(o))
                if units[s] <= 0:
                    units[s] = 0.0
                    U0[s] = np.nan
                    if entry_t[s] >= 0:
                        close_episode(s, t, o["reason"])
            buys = sorted([o for o in pending if o["side"] == "buy" and panel.buyable[t, o["s"]]],
                          key=lambda o: o["s"])
            need = 0.0
            for o in buys:
                need = need + o["value"] * (1 + cost)
            scale = min(1.0, cash / need) if need > 0 else 0.0
            for o in buys:
                s = o["s"]
                done.add(id(o))
                val = o["value"] * scale
                if val <= 0:
                    record(t, o, "cancel", 0.0, np.nan, 0.0, "no cash left")
                    continue
                du = val / op[s]
                if units[s] == 0 and entry_t[s] < 0:
                    entry_t[s] = t
                    entry_px[s] = op[s]
                units[s] += du
                cash -= val * (1 + cost)
                record(t, o, "fill", du, op[s], val)
            keep = []
            for o in pending:
                if id(o) in done:
                    continue
                if o["deadline"] <= t:
                    record(t, o, "cancel", 0.0, np.nan, 0.0,
                           f"no trade within {wait} sessions after D")
                    continue
                keep.append(o)
            pending = keep

        if t == t_end:
            ok = panel.traded[t] & (panel.open_[t] > 0)
            px_mark = np.where(ok, panel.open_[t], panel.last[t - 1])
            nav_out[t] = cash + float(np.nansum(units * px_mark))
            cash_out[t] = cash
            break

        # section 3.8: the 20-session no-trade exit, at the last close
        stuck = (units > 0) & (panel.no_trade_run[t] >= no_trade_exit)
        for s in np.flatnonzero(stuck):
            px = panel.last[t, s]
            val = units[s] * px
            cash += val * (1 - cost)
            o = {"s": s, "kind": "forced_exit", "side": "sell", "D": cur_D,
                 "reason": f"no trade for {no_trade_exit} sessions"}
            record(t, o, "fill", units[s], px, val)
            units[s] = 0.0
            U0[s] = np.nan
            keep = []
            for p in pending:
                if p["s"] == s:
                    record(t, p, "cancel", 0.0, np.nan, 0.0, "the position was force-sold")
                else:
                    keep.append(p)
            pending = keep
            if entry_t[s] >= 0:
                close_episode(s, t, "no_trade_exit")

        nav_out[t] = cash + float(np.nansum(units * panel.last[t]))
        cash_out[t] = cash

    pend = [{"symbol": syms[o["s"]], "D": o["D"].date(), "kind": o["kind"], "side": o["side"],
             "units": o.get("units"), "value": o.get("value")} for o in pending]
    for s in np.flatnonzero(entry_t >= 0):
        episodes.append({"symbol": syms[s], "entry": dates[entry_t[s]], "exit": dates[t_end],
                         "entry_px": entry_px[s], "entry_t": int(entry_t[s]), "exit_t": t_end,
                         "exit_reason": "open_at_end"})
    ep = pd.DataFrame(episodes)
    if not ep.empty:
        mx = []
        for r in ep.itertuples():
            s = syms.get_loc(r.symbol)
            seg = panel.close[r.entry_t:r.exit_t + 1, s]
            mx.append(np.nanmax(seg) if np.isfinite(seg).any() else np.nan)
        ep["max_close"] = mx
        ep["doubled"] = ep["max_close"] >= 2 * ep["entry_px"]
        ep["days_held"] = (ep["exit"] - ep["entry"]).dt.days
    nav = engine.nav_frame(dates[t0], initial_capital, dates[t0:t_end], nav_out[t0:t_end],
                           dates[t_end], nav_out[t_end])
    return ResultV3(nav=nav, fills=pd.DataFrame(fills, columns=FILL_COLUMNS),
                    trades=pd.DataFrame(trades), holdings=pd.DataFrame(hold_rows),
                    decisions=pd.DataFrame(dec_rows), episodes=ep,
                    cash=pd.Series(cash_out[t0:t_end + 1], index=dates[t0:t_end + 1]),
                    rebalance_open=pd.Series(open_marks, dtype=float),
                    pending_at_end=pd.DataFrame(pend))


BZ_COLUMNS = engine.BZ_COLUMNS


def bz_holdings(panel: engine.Panel, res: ResultV3) -> pd.DataFrame:
    """engine.bz_holdings for v3's exit reasons: every holding episode that
    spanned a BZ-only session, and whether its exit sale filled at a BZ open."""
    ep = res.episodes
    rows = []
    if ep is not None and not ep.empty:
        bz = panel.bz_only
        for r in ep.itertuples():
            s = panel.col(r.symbol)
            hit = np.flatnonzero(bz[r.entry_t + 1:r.exit_t + 1, s]) + r.entry_t + 1
            if len(hit) == 0:
                continue
            rows.append({"symbol": r.symbol, "entry": r.entry.date(), "exit": r.exit.date(),
                         "exit_reason": r.exit_reason, "bz_sessions": len(hit),
                         "first_bz": panel.dates[hit[0]].date(),
                         "last_bz": panel.dates[hit[-1]].date(),
                         "exit_fill_in_bz": bool(r.exit_reason not in ("no_trade_exit",
                                                                       "open_at_end")
                                                 and bz[r.exit_t, s])})
    return pd.DataFrame(rows, columns=BZ_COLUMNS)


# ---------------------------------------------------------------- replay hook (monkey test)
@dataclass
class Replay:
    """The v3 book as a run folder used it, for jobs/verify_v1.py's monkey test.

    Built by jobs.backtest_v3.replay(run_dir); `simulate(order)` re-runs the
    book with the candidate order replaced by `order(D, candidates)` (e.g. a
    shuffle). Only the fill order changes; eligibility, exits, the trim, the
    cap, the sector cap, the slots, costs, dividends and the forced exit stay."""
    panel: engine.Panel
    decisions: list
    reviews: dict
    keys: pd.Series
    is_end: pd.Timestamp
    run_final_test: bool
    tickers: engine_v2.Tickers | None = None

    def simulate(self, order=None, log_trades: bool = True) -> ResultV3:
        return simulate_v3(self.panel, self.decisions, self.reviews, self.keys, self.is_end,
                           self.run_final_test, self.tickers, order, log_trades=log_trades)

    def reproduce(self, nav_strategy) -> float:
        a = self.simulate().nav["nav"].to_numpy(float)
        b = np.asarray(nav_strategy, dtype=float)
        if len(a) != len(b):
            raise RuntimeError(f"replay has {len(a)} marks, the run {len(b)}")
        return float(np.max(np.abs(a / b - 1)))
