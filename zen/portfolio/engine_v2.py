"""Strategy v2 portfolio engine (research/strategy/v2-spec.md).

v2 keeps v1's machinery and changes only what the v2 spec changes. Everything
that is v1's is called, not copied:

  point in time   zen/universe/pit.py (snapshot, rules 1-7, split factors)
  measures        zen/signals/composite.py (v1's eight measures)
  ROCE, D/E       zen/validation/factors.py: ttm_ebit, latest_balance_sheets and
                  signals, the code the A5 quality factor uses (Clarification to
                  A5 and to item 2)
  prices, costs,  zen/portfolio/engine.py: build_panel (adjusted units,
  dividends,      dividends as cash, the trade-for-trade BZ rule), select_top
  forced exit,    (hold band then fill under the sector cap), the holdout guard,
  benchmarks      the equal-weight benchmark and the index clocks

What v2 adds, each as the spec's "Clarification before the v2 build
(2026-09-26)" fixes it:

  universe       v1 rules 1-7 plus the hard filters: market cap above Rs 100
                 crore; trailing P/E = market cap / TTM normalised profit,
                 positive and at most 70; from the February 2023 decision date,
                 a usable balance sheet (known before D, same basis as the
                 income statement, at most 400 days old, equity above zero),
                 ROCE at least 10% and total debt / equity below 1.5. A stock
                 with no usable balance sheet fails both.
  thesis break   the hard filters are universe rules, so a holding that fails
                 one has left the universe and is sold at D's open whatever
                 its rank (engine.select_top keeps only names in the universe).
  quality group  v1's margin and stability before the February 2023 decision
                 date; ROCE and stability from it. A missing measure scores 0.5.
  book           12 names, kept while in the universe and ranked within the top
                 24, vacancies filled best rank first under 3 per NSE sector
                 (data/reference/industry_nse.parquet, joined by stock id; an
                 unlabelled company is its own sector).
  trend filter   at D, from Nifty 500 total-return closes on the archive's market
                 sessions (v1 Clarification 1; the index's special sessions the
                 archive does not hold are not counted) up to the session
                 before D: (1) last close below the mean of the last 200 closes;
                 (2) last close at least 10% below the highest of the last 252;
                 (3) on at least 60% of the last 126 closes, the close was below
                 its own trailing 200-close mean. Cash 0 / 20 / 27.5 / 35% of
                 NAV at D for 0 to 3 conditions true.
  sizing         weight proportional to 1/sigma (sigma = v1's low-volatility
                 standard deviation), scaled to sum to (1 - cash), then held
                 within 0.5x and 1.5x of (1 - cash)/12 with the excess or
                 shortfall redistributed pro rata over the names not at a bound
                 until all are within it (`target_weights`).
  tranches       a kept holding is resized in full at D (v1's order rules). A new
                 position's value V is bought in three tranches of V/3 at D and
                 at the 21st and 42nd sessions after D. Variant (a): each at its
                 session's open under v1's buying rules (waits for an EQ/BE
                 trade, cancelled after 5 sessions without one). Variant (b),
                 amendment A1: each tranche is bought at the open of the first
                 of the 30 sessions starting with its scheduled one (the
                 scheduled session and the next 29) whose previous session's
                 close qualifies (adjusted EQ/BE close at most 10% above its
                 50-session simple average and 14-session Wilder RSI below 70,
                 on EQ/BE closes only: `a1_closes`); if none qualifies, at the
                 open of the 31st session, 30 sessions after the scheduled one,
                 regardless (Clarification after the in-sample engine
                 comparison, 2026-09-28).
                 Pending tranches are cancelled when the position leaves the
                 book (a later decision date or the forced exit), and at the
                 next decision date whatever happens (a kept holding is then
                 resized in full, which replaces them).

Every reading the text left open, and the one taken, is listed in
`READINGS` below and written into each run's metrics.json.

HOLDOUT LOCK: as v1. `simulate_v2` checks engine.guard on the panel's end, the
panel builder and the index loaders check it too, and the trend filter reads
no total-return close on or after the run's end.

REPLAY HOOK (for the monkey test in jobs/verify_v1.py): `Replay.from_run(con,
run_dir)` rebuilds exactly the inputs a v2 run used (its config, decision
dates, ranks, cash fractions re-derived from the index and checked against
decisions.csv, the panel built as jobs/backtest_v2.py builds it, and A1's
qualification table). `replay.reproduce(nav)` re-runs the strategy and returns
the worst relative difference from the run's own NAV, and
`replay.simulate(replay.chooser(ranks))` runs the same machinery on any other
ranks frame (for instance with the `rank` column shuffled within each decision
date, as verify_v1.monkeys does for v1). Only the order changes; the universe,
the band, the sector cap, the sizing (each name's own sigma), the trend cash,
the tranches, A1, costs, dividends and the forced exit are the strategy's own.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from zen.portfolio import engine
from zen.signals import composite
from zen.universe import pit
from zen.validation import factors

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- spec constants
N_POSITIONS = 12
BAND_MULT = 2                         # kept while ranked within 2 x 12 = 24
SECTOR_CAP = 3
MCAP_MIN = 100 * 1e7                  # Rs 100 crore in rupees; the cap must be ABOVE it
PE_MAX = 70.0
ROCE_MIN = 0.10
DE_MAX = 1.5
ROCE_FROM = (2023, 2)                 # the February 2023 decision date onward
CASH_BY_CONDITIONS = (0.0, 0.20, 0.275, 0.35)
TREND_INDEX = "NIFTY 500"             # data/external/nifty_tri.parquet, tri column
TREND_SMA = 200
TREND_HIGH = 252
TREND_DRAWDOWN = 0.10
TREND_PERSIST = 126
TREND_PERSIST_SHARE = 0.60
WEIGHT_LO, WEIGHT_HI = 0.5, 1.5       # times (1 - cash) / 12
N_TRANCHES = 3
TRANCHE_GAP = 21                      # sessions after D: 0, 21, 42
A1_WINDOW = 30                        # qualifying buys at sessions 0..29 after the scheduled
                                      # one (it is the first of the 30); fallback at session 30
A1_SMA = 50
A1_MAX_ABOVE_SMA = 0.10
A1_RSI = 14
A1_RSI_MAX = 70.0
NSE_LABELS = pit.REPO / "data" / "reference" / "industry_nse.parquet"

QUALITY_V1, QUALITY_V2 = "margin", "roce"
MEASURE_COLS = ["margin", "roce", "stability", "rev_growth", "profit_growth",
                "earnings_yield", "sales_yield", "mom_12_1", "low_vol"]
GROUPS = composite.GROUPS

HARD_FUNNEL = ["h1_mcap_above_100cr", "h2_pe_positive_max_70", "h3_balance_sheet_usable",
               "h4_roce_min_10pct", "h5_de_below_1_5"]

READINGS = {
    "hard_filter_order": "rules 1-7 exactly as v1 (pit.universe), then the hard filters; "
                         "percentiles, groups and ranks are computed within the v2 universe "
                         "(rules 1-7 plus hard filters), since the hard filters are universe rules",
    "mcap_floor": "market cap strictly above Rs 100 crore (1e9 rupees)",
    "pe": "market cap / TTM normalised profit (v1's market cap and profit), > 0 and <= 70",
    "roce_switch": "(year, month) of D >= (2023, 2): the February 2023 decision date onward",
    "balance_sheet_usable": "zen.validation.factors.signals gives a capital figure: the latest "
                            "balance sheet known before D in the income statement's basis, at most "
                            "400 days old, equity > 0 (and equity + debt > 0)",
    "roce_unknown": "a usable balance sheet but no TTM EBIT (a missing EBIT line) has no ROCE and "
                    "fails the ROCE floor",
    "de": "debt_total (0 when the balance sheet has no borrowings line) / equity, strictly below 1.5",
    "no_balance_sheet_count": "n_excluded_no_balance_sheet counts rule-1-7 members with no usable "
                              "balance sheet at D, whether or not another hard filter also fails "
                              "them (0 before February 2023)",
    "measures_after_switch": "from February 2023 the quality group is ROCE and stability; margin is "
                             "not a measure then (its column is empty in ranks.csv), and before it "
                             "ROCE is not computed",
    "sector_join": "every labelled row of industry_nse.parquet is mapped to a stock id through its "
                   "(symbol, ISIN) spell; a stock with several labelled rows takes the row of its own "
                   "latest symbol, else the row whose spell ended last. The sector column is used as "
                   "it is, including NSE's legacy-scheme labels of suspended companies",
    "trend_sessions": "the NIFTY 500 total-return closes (nifty_tri.parquet) on the archive's market "
                      "sessions (v1 Clarification 1) with date strictly before D; the index's special "
                      "sessions the archive does not hold (Muhurat and budget-day trading) are not "
                      "counted, and an archive session without a close is a missing close "
                      "(Clarification after the in-sample engine comparison)",
    "trend_c2": "last close <= 0.9 x the highest of the last 252 closes (the last close included)",
    "trend_c3": "count of the last 126 closes below the mean of the 200 closes ending at each "
                ">= 0.6 x 126, i.e. at least 76",
    "sigma": "-low_vol of the ranks at D (sample sd of daily adjusted log returns between "
             "consecutive traded closes within the last 252 sessions)",
    "no_sigma": "a name without a positive finite sigma gets (1 - cash)/12 before scaling, the "
                "names with a sigma get (1 - cash)/12 x (1/sigma) / mean(1/sigma) before scaling, "
                "then all are scaled to sum to (1 - cash); cannot arise for a universe member "
                "(rule 3 gives every member 200 sessions)",
    "bounds": "all weights outside [0.5, 1.5] x (1 - cash)/12 are set to the bound together, the net "
              "excess or shortfall is spread over the names not at a bound in proportion to their "
              "weights, and this repeats until none is outside; a name set to a bound stays there; "
              "what cannot be placed is cash",
    "held": "a name is held at D when it has units at D's open (v1); kept = held and selected, "
            "new = selected and not held, sold = held and not selected",
    "tranche_amount": "each tranche buys V/3 rupees of stock (cost on top, as v1's buys), V = target "
                      "weight x NAV at D's open; buys that session are scaled down pro rata when "
                      "cash is short (v1 Clarification 11)",
    "tranche_sessions": "tranches 2 and 3 are scheduled 21 and 42 market sessions after D (D is "
                        "session 0)",
    "variant_a_wait": "a tranche fills at the open of the first session from its scheduled one with "
                      "an EQ/BE trade, up to 5 sessions after it, else it is cancelled",
    "a1_window": "the scheduled session is the first of the 30: a tranche is bought at the open of "
                 "session k after its scheduled session (k = 0 is the scheduled session) for the "
                 "first k in 0..29 whose previous session's close qualifies and on which the stock "
                 "has an EQ/BE trade; if none, at the open of session 30 (the 31st session), or, if "
                 "the stock has no EQ/BE trade then, at the first such trade within 5 more sessions "
                 "(v1's execution rule), else cancelled",
    "a1_closes": "EQ/BE closes only (not BZ), adjusted, on the market calendar, a session without an "
                 "EQ/BE trade carrying the last EQ/BE close (a1_closes); the SMA is the mean of the "
                 "last 50 such closes; Wilder's RSI is seeded with the simple mean of the first 14 "
                 "changes after the stock's first EQ/BE close in the archive, then smoothed as "
                 "(previous x 13 + current) / 14; RSI = 100 when only the average loss is 0, and "
                 "undefined (the condition fails) when the average gain and loss are both 0",
    "a1_metric": "per new position: sum of tranche fill values / sum of tranche fill units, over "
                 "the adjusted EQ/BE close on the session before D (a1_closes, carried over "
                 "sessions without an EQ/BE trade); positions with no tranche filled are left out; "
                 "the average is weighted by V",
    "pending_orders_at_D": "every order still pending at a decision date is cancelled there: a kept "
                           "holding is resized in full, a position that leaves the book is sold",
    "end_of_run": "no fills at the final open mark; orders still pending then are listed in "
                  "metrics.json, not as cancellations",
}


# ---------------------------------------------------------------- config
@dataclass(frozen=True)
class ConfigV2:
    variant: str = "a"                 # 'a' staggered entry, 'b' staggered entry with A1
    n: int = N_POSITIONS
    buffer_mult: int = BAND_MULT
    sector_cap: int | None = SECTOR_CAP
    cost: float = 0.002
    delist_haircut: float = 1.0
    wait_sessions: int = 5
    no_trade_exit: int = 20
    initial_capital: float = 5e5
    n_tranches: int = N_TRANCHES
    tranche_gap: int = TRANCHE_GAP
    a1_window: int = A1_WINDOW

    def __post_init__(self):
        if self.variant not in ("a", "b"):
            raise ValueError(f"variant must be 'a' or 'b', not {self.variant!r}")

    def label(self) -> str:
        cap = self.sector_cap if self.sector_cap else "none"
        return (f"v2{self.variant}_N{self.n}_buf{self.buffer_mult}x_cap{cap}"
                f"_c{self.cost:g}_h{self.delist_haircut:g}")

    def v1(self) -> engine.Config:
        """The v1 engine config with the same book rules: used for select_top and for
        the equal-weight benchmark (which ignores n, the band and the cap)."""
        return engine.Config(n=self.n, buffer_mult=self.buffer_mult, rebalance="quarterly",
                             sector_cap=self.sector_cap, cost=self.cost,
                             delist_haircut=self.delist_haircut, wait_sessions=self.wait_sessions,
                             no_trade_exit=self.no_trade_exit,
                             initial_capital=self.initial_capital)


def config_dict(cfg: ConfigV2) -> dict:
    return asdict(cfg)


def roce_on(D) -> bool:
    D = pd.Timestamp(D)
    return (D.year, D.month) >= ROCE_FROM


# ---------------------------------------------------------------- NSE sectors
def load_nse_labels(path: Path = NSE_LABELS) -> pd.DataFrame:
    return pd.read_parquet(path)


def nse_sectors(labels: pd.DataFrame, ids=None) -> pd.DataFrame:
    """NSE's sector per stock id (item 7; Clarification before the v2 build).

    Each labelled row is mapped to a stock id through its (symbol, ISIN) spell
    in the bhavcopy archive (identity.Identity.for_prices), so a renamed
    company keeps its label. A stock with several labelled rows (a rename, or
    a symbol and its suspended predecessor) takes the row of its own latest
    symbol (the stock id), else the row whose spell ended last, then by symbol.
    Returns a frame indexed by stock id: sector, label_symbol, label_scheme.
    """
    cols = ["sector", "label_symbol", "label_isin", "label_scheme"]
    lab = labels[labels["sector"].notna()].copy()
    if lab.empty:
        return pd.DataFrame(columns=cols, index=pd.Index([], name="symbol"))
    if "label_scheme" not in lab:
        lab["label_scheme"] = None
    use_ids = ids is not None and not ids.empty
    if use_ids:
        lab["cid"] = ids.for_prices(lab, "symbol", "isin")
        last = ids.spells.groupby(["symbol", "isin"])["last"].max()
        key = pd.MultiIndex.from_arrays([lab["symbol"], lab["isin"]])
        lab["spell_last"] = pd.to_datetime(last.reindex(key).to_numpy())
    else:
        lab["cid"] = lab["symbol"]
        lab["spell_last"] = pd.NaT
    lab["own"] = lab["symbol"] == lab["cid"]
    lab = (lab.sort_values(["cid", "own", "spell_last", "symbol"],
                           ascending=[True, False, False, True], na_position="last")
              .drop_duplicates("cid", keep="first"))
    out = pd.DataFrame({"sector": lab["sector"].to_numpy(),
                        "label_symbol": lab["symbol"].to_numpy(),
                        "label_isin": lab["isin"].to_numpy(),
                        "label_scheme": lab["label_scheme"].to_numpy()},
                       index=pd.Index(lab["cid"].to_numpy(), name="symbol"))
    return out


# ---------------------------------------------------------------- tickers
class Tickers:
    """The symbol a stock id traded under on a date, from the identity spells."""

    def __init__(self, ids=None):
        self.by: dict = {}
        if ids is not None and not ids.empty:
            sp = ids.spells.copy()
            sp["first"] = pd.to_datetime(sp["first"])
            sp["last"] = pd.to_datetime(sp["last"])
            for cid, g in sp.groupby("cid"):
                self.by[cid] = list(g[["symbol", "first", "last"]].itertuples(index=False,
                                                                              name=None))

    def at(self, cid, d) -> str:
        rows = self.by.get(cid)
        if not rows:
            return cid
        d = pd.Timestamp(d)
        active = [r for r in rows if r[1] <= d <= r[2]]
        if active:                                   # the spell that most recently started
            return max(active, key=lambda r: (r[1], r[0]))[0]
        past = [r for r in rows if r[1] <= d]
        if past:                                     # after delisting: the spell that ended last
            return max(past, key=lambda r: (r[2], r[0]))[0]
        return min(rows, key=lambda r: (r[1], r[0]))[0]


# ---------------------------------------------------------------- hard filters
def balance_sheet_measures(con, snap: pit.Snapshot, static: pit.StaticLabels,
                           fund: pd.DataFrame) -> pd.DataFrame:
    """ROCE and debt/equity per stock at D by the A5 factor's own code.

    `fund` is pit.fundamentals' table for the stocks concerned (it carries the
    chosen basis, the latest income quarter and the TTM sums). ROCE, TTM EBIT
    and capital come from factors.signals; the balance sheet it used is the
    latest one known before D in the income statement's basis
    (factors.latest_balance_sheets), and debt/equity is read from that same
    row. `bs_usable` is signals' own test: at most 400 days old, equity > 0.
    """
    idx = fund.index
    qx = factors.quarter_extras(con, snap, static.ids)
    bs = factors.latest_balance_sheets(con, snap, static.ids)
    sig = factors.signals(fund, qx, bs, snap.D)
    out = sig[["ttm_ebit", "capital", "roce", "bs_period_end", "bs_broadcast"]].copy()
    out["bs_usable"] = out["capital"].notna()
    eq = pd.Series(np.nan, index=idx)
    debt = pd.Series(np.nan, index=idx)
    if len(bs) and len(idx):
        basis = fund["consolidated"].astype(bool)
        b = bs[bs["symbol"].isin(idx)]
        b = b[b["consolidated"].astype(bool).to_numpy() ==
              basis.reindex(b["symbol"]).to_numpy()].set_index("symbol")
        eq = b["equity"].astype(float).reindex(idx)
        debt = b["debt_total"].astype(float).fillna(0.0).reindex(idx)
    u = out["bs_usable"]
    out["equity"] = eq.where(u)
    out["debt"] = debt.where(u)
    with np.errstate(divide="ignore", invalid="ignore"):
        out["de"] = (debt / eq).where(u)
    return out


def hard_filters(mcap: pd.Series, ttm_profit: pd.Series, D,
                 bsm: pd.DataFrame | None = None) -> pd.DataFrame:
    """The owner's hard filters (item 3) for the rule-1-7 members at D.

    Before February 2023 only market cap and P/E apply (bsm is ignored). From
    it, `bsm` (balance_sheet_measures) must be given; a stock with no usable
    balance sheet fails both the ROCE floor and the D/E limit.
    """
    idx = mcap.index
    out = pd.DataFrame(index=idx)
    with np.errstate(divide="ignore", invalid="ignore"):
        pe = mcap.astype(float) / ttm_profit.reindex(idx).astype(float)
    out["pe"] = pe
    out["pass_mcap"] = (mcap.astype(float) > MCAP_MIN).fillna(False).astype(bool)
    out["pass_pe"] = ((pe > 0) & (pe <= PE_MAX)).fillna(False).astype(bool)
    if roce_on(D):
        if bsm is None:
            raise ValueError(f"{pd.Timestamp(D).date()}: balance-sheet measures are required")
        b = bsm.reindex(idx)
        usable = b["bs_usable"].fillna(False).astype(bool)
        out["bs_usable"] = usable
        out["roce"] = b["roce"].astype(float)
        out["de"] = b["de"].astype(float)
        out["pass_roce"] = (usable & (out["roce"] >= ROCE_MIN)).fillna(False).astype(bool)
        out["pass_de"] = (usable & (out["de"] < DE_MAX)).fillna(False).astype(bool)
    else:
        out["bs_usable"] = pd.Series(pd.NA, index=idx, dtype="boolean")
        out["roce"] = np.nan
        out["de"] = np.nan
        out["pass_roce"] = True
        out["pass_de"] = True
    out["passes"] = out["pass_mcap"] & out["pass_pe"] & out["pass_roce"] & out["pass_de"]
    return out


def hard_funnel(hf: pd.DataFrame, D) -> dict:
    """Names remaining after each hard filter in turn, and the order-free counts."""
    f = {}
    m = pd.Series(True, index=hf.index)
    m &= hf["pass_mcap"]
    f["h1_mcap_above_100cr"] = int(m.sum())
    m &= hf["pass_pe"]
    f["h2_pe_positive_max_70"] = int(m.sum())
    if roce_on(D):
        m &= hf["bs_usable"].astype(bool)
    f["h3_balance_sheet_usable"] = int(m.sum())
    m &= hf["pass_roce"]
    f["h4_roce_min_10pct"] = int(m.sum())
    m &= hf["pass_de"]
    f["h5_de_below_1_5"] = int(m.sum())
    f["n_no_balance_sheet"] = int((~hf["bs_usable"].astype(bool)).sum()) if roce_on(D) else 0
    return f


# ---------------------------------------------------------------- scoring
def measure_groups(use_roce: bool) -> dict:
    """v1's measures and groups; with ROCE, ROCE replaces margin in the quality group."""
    if not use_roce:
        return dict(composite.MEASURES)
    return {(QUALITY_V2 if k == QUALITY_V1 else k): g for k, g in composite.MEASURES.items()}


def score_v2(m: pd.DataFrame, use_roce: bool) -> pd.DataFrame:
    """Percentiles, group scores, composite and rank, as composite.score, over the
    measures of `measure_groups(use_roce)`. Pure function of the measures."""
    mg = measure_groups(use_roce)
    out = m.copy()
    for k in mg:
        out[f"pct_{k}"] = m[k].rank(method="average", pct=True).fillna(0.5)
    for g in GROUPS:
        cols = [f"pct_{k}" for k, gg in mg.items() if gg == g]
        out[f"grp_{g}"] = out[cols].mean(axis=1)
    out["composite"] = out[[f"grp_{g}" for g in GROUPS]].mean(axis=1)
    tick = out["ticker"] if "ticker" in out else pd.Series(out.index, index=out.index)
    order = (out.assign(_t=tick.to_numpy()).reset_index()
             .sort_values(["composite", "_t", "symbol"], ascending=[False, True, True])["symbol"])
    out["rank"] = pd.Series(np.arange(1, len(order) + 1), index=order.to_numpy())
    return out.sort_values("rank")


RANK_COLS = (["D", "symbol", "ticker"] + MEASURE_COLS +
             [f"pct_{k}" for k in MEASURE_COLS] + [f"grp_{g}" for g in GROUPS] +
             ["composite", "rank"])


def compute_v2(con, D, static: pit.StaticLabels, sectors: pd.DataFrame):
    """The v2 universe and ranks at decision date D.

    Returns (ranked, funnel, excluded, data_quality):
      ranked    one row per v2 universe member, indexed by stock id, with every
                measure, percentile, group score, the composite and the rank,
                the NSE sector and sigma
      funnel    v1's funnel counts plus the hard-filter steps
      excluded  rule-1-7 members that failed a hard filter, with which
      data_quality  v1's Clarification 30 audit rows at D
    """
    D = pd.Timestamp(D).normalize()
    snap = pit.build_snapshot(con, D, static)
    members, funnel, fund = pit.universe(snap, static)
    f = fund.reindex(members.index)
    use_roce = roce_on(D)
    bsm = balance_sheet_measures(con, snap, static, f) if use_roce else None
    hf = hard_filters(members["mcap"], f["ttm_profit"], D, bsm)
    funnel = {**funnel, **hard_funnel(hf, D)}
    keep = hf.index[hf["passes"].to_numpy()]

    mem = members.loc[keep]
    m = composite.measures(mem, fund, snap)
    m["ticker"] = mem["ticker"]
    m[QUALITY_V2] = bsm["roce"].reindex(keep).astype(float) if use_roce else np.nan
    if use_roce:
        m[QUALITY_V1] = np.nan              # not a measure from February 2023
    ranked = score_v2(m, use_roce)
    for k in MEASURE_COLS:                   # a measure not used at D has no percentile
        if f"pct_{k}" not in ranked:
            ranked[f"pct_{k}"] = np.nan
    ranked["quality_measure"] = QUALITY_V2 if use_roce else QUALITY_V1
    sec = sectors["sector"] if len(sectors) else pd.Series(dtype=object)
    ranked["sector"] = sec.reindex(ranked.index).to_numpy()
    ranked["sector_scheme"] = (sectors["label_scheme"].reindex(ranked.index).to_numpy()
                               if len(sectors) else None)
    ranked["sigma"] = -ranked["low_vol"]
    ranked = ranked.join(mem[["mcap", "raw_close", "median_turnover_60"]])
    ranked["sector_v1"] = mem["sector"].reindex(ranked.index)
    ranked["sector_source_v1"] = mem["sector_source"].reindex(ranked.index)
    for c in ["consolidated", "latest_period_end", "consec_quarters", "ttm_revenue",
              "ttm_ebitda", "ttm_profit", "shares", "shares_filed", "shares_fix"]:
        ranked[c] = f[c].reindex(ranked.index)
    ranked["pe"] = hf["pe"].reindex(ranked.index)
    for c in ("ttm_ebit", "capital", "equity", "debt", "de", "bs_period_end"):
        ranked[c] = bsm[c].reindex(ranked.index) if use_roce else np.nan
    ranked.insert(0, "D", D)

    ex = hf[~hf["passes"]].copy()
    ex.insert(0, "D", D)
    ex["ticker"] = members["ticker"].reindex(ex.index)
    if use_roce and len(ex):
        ex["ttm_ebit"] = bsm["ttm_ebit"].reindex(ex.index)
        ex["capital"] = bsm["capital"].reindex(ex.index)
    ex.index.name = "symbol"
    ex = ex.reset_index()

    dq = snap.scale_dropped[["symbol", "consolidated", "period_end", "broadcast_dt",
                             "revenue", "shares_implied"]].assign(check="scale_error_dropped")
    fx = f[f["shares_fix"].fillna("") != ""]
    sf = pd.DataFrame({"symbol": fx.index, "shares_filed": fx["shares_filed"].to_numpy(),
                       "shares": fx["shares"].to_numpy(), "check": fx["shares_fix"].to_numpy()})
    dq = pd.concat([dq, sf], ignore_index=True).assign(D=D)
    return ranked, funnel, ex, dq


def build_ranks_v2(con, decisions, static: pit.StaticLabels, sectors: pd.DataFrame):
    """compute_v2 at every decision date. Returns (ranks, funnel, excluded, data_quality)."""
    frames, funnels, exs, dqs = [], [], [], []
    for D in decisions:
        r, fn, ex, dq = compute_v2(con, D, static, sectors)
        frames.append(r.reset_index())
        row = pit.funnel_frame(D, fn)
        prev = "r7_mcap_computable"
        for k in HARD_FUNNEL:
            row[k] = fn[k]
            row[f"removed_{k.split('_')[0]}"] = fn[prev] - fn[k]
            prev = k
        row["n_no_balance_sheet"] = fn["n_no_balance_sheet"]
        funnels.append(row)
        exs.append(ex)
        dqs.append(dq)
        log.info("%s: v1 rules %d, v2 universe %d", pd.Timestamp(D).date(),
                 fn["r7_mcap_computable"], len(r))
    ranks = pd.concat(frames, ignore_index=True)
    return (ranks, pd.concat(funnels, ignore_index=True),
            pd.concat(exs, ignore_index=True), pd.concat(dqs, ignore_index=True))


# ---------------------------------------------------------------- trend filter
def load_trend_index(end, sessions, path=engine.TRI_PATH) -> pd.Series:
    """Nifty 500 total-return closes on the archive's market sessions with date
    strictly before `end` (the holdout lock: nothing at or after the run's end
    is read).

    `sessions` is the archive's calendar (pit.sessions). "Sessions" and
    "closes" are that calendar's (v1 Clarification 1; Clarification after the
    in-sample engine comparison): a close the index published on a special
    session the archive does not hold (Muhurat or budget-day trading) is not
    counted, and an archive session from the index's first close on without a
    close is kept as a missing close (NaN), which trend_state refuses inside
    its window.
    """
    end = pd.Timestamp(end)
    t = engine._load_tri(path, TREND_INDEX, pd.Timestamp("1990-01-01"), end)
    t = t.sort_index()
    if t.index.duplicated().any():
        raise ValueError(f"{TREND_INDEX}: duplicate dates in {path}")
    cal = pd.DatetimeIndex(sessions)
    if len(t):
        cal = cal[(cal >= t.index[0]) & (cal < end)]
    else:
        cal = cal[:0]
    return t.reindex(cal).astype(float)


def trend_state(tri: pd.Series, D) -> dict:
    """The graded trend filter at decision date D (item 4)."""
    D = pd.Timestamp(D)
    h = tri[tri.index < D]
    need = max(TREND_SMA + TREND_PERSIST - 1, TREND_HIGH)
    if len(h) < need:
        raise ValueError(f"{D.date()}: {len(h)} index closes before D, {need} needed")
    x = h.to_numpy(float)
    if not np.isfinite(x[-need:]).all():
        raise ValueError(f"{D.date()}: a missing index close in the window")
    last = float(x[-1])
    sma = float(np.mean(x[-TREND_SMA:]))
    high = float(np.max(x[-TREND_HIGH:]))
    n = len(x)
    below = 0
    for j in range(n - TREND_PERSIST, n):
        if x[j] < np.mean(x[j - TREND_SMA + 1:j + 1]):
            below += 1
    c1 = bool(last < sma)
    c2 = bool(last <= (1 - TREND_DRAWDOWN) * high)
    c3 = bool(below >= TREND_PERSIST_SHARE * TREND_PERSIST)
    k = int(c1) + int(c2) + int(c3)
    return {"D": D, "trend_c1": c1, "trend_c2": c2, "trend_c3": c3, "n_true": k,
            "cash_fraction": CASH_BY_CONDITIONS[k], "index_last_date": h.index[-1],
            "index_last": last, "index_sma200": sma, "index_high252": high,
            "sessions_below_sma_of_126": below}


def trend_table(tri: pd.Series, decisions) -> pd.DataFrame:
    return pd.DataFrame([trend_state(tri, D) for D in decisions])


# ---------------------------------------------------------------- sizing
def target_weights(sigma: pd.Series, cash: float, n: int = N_POSITIONS,
                   lo: float = WEIGHT_LO, hi: float = WEIGHT_HI, max_rounds: int = 1000
                   ) -> pd.Series:
    """Weights of NAV at D for the names held after D's decisions (item 6).

    Proportional to 1/sigma, scaled to sum to (1 - cash); then every weight is
    held within [lo, hi] x (1 - cash)/n: all names outside are set to the bound
    together, and the net excess or shortfall is spread over the names not at
    a bound in proportion to their weights, until none is outside. A name set
    to a bound stays there. What the bounded weights cannot place (fewer than
    n names, or every name at its cap) is cash. A name with no sigma is given
    (1 - cash)/n before scaling, the others (1 - cash)/n x (1/sigma) over the
    mean 1/sigma of the names with one.
    """
    names = sigma.index
    if len(names) == 0:
        return pd.Series(dtype=float)
    book = 1.0 - cash
    E = book / n
    s = sigma.astype(float).to_numpy()
    ok = np.isfinite(s) & (s > 0)
    raw = np.full(len(s), E)
    if ok.any():
        inv = 1.0 / s[ok]
        raw[ok] = E * inv / inv.mean()
    w = raw / raw.sum() * book
    lo_w, hi_w = lo * E, hi * E
    at = np.zeros(len(w), dtype=bool)
    for _ in range(max_rounds):
        over = ~at & (w > hi_w)
        under = ~at & (w < lo_w)
        if not (over.any() or under.any()):
            break
        excess = float((w[over] - hi_w).sum() - (lo_w - w[under]).sum())
        w[over] = hi_w
        w[under] = lo_w
        at |= over | under
        free = ~at
        base = float(w[free].sum())
        if not free.any() or base <= 0:
            break                            # nothing left to absorb it: the rest is cash
        w[free] = w[free] * (1.0 + excess / base)
    else:
        raise RuntimeError("target_weights did not converge")
    tol = 1e-12 * max(book, 1e-300)
    if (w < lo_w - tol).any() or (w > hi_w + tol).any() or w.sum() > book + tol:
        raise RuntimeError(f"target_weights: bounds or budget violated ({w}, book {book})")
    return pd.Series(w, index=names)


# ---------------------------------------------------------------- A1
def a1_closes(panel: engine.Panel) -> np.ndarray:
    """Sessions x stocks: the adjusted EQ/BE close carried forward over sessions
    without an EQ/BE trade, NaN before the stock's first EQ/BE close.

    A1's closes (Clarification after the in-sample engine comparison): a stock
    can only be bought in EQ or BE, so the 50-session average, the RSI and the
    adoption test's reference close use EQ/BE closes only. A session traded
    only in BZ (trade-for-trade) is a session without an EQ/BE trade here: its
    BZ close, which engine.Panel.last carries, is not used.
    """
    eq = np.where(panel.buyable, panel.close, np.nan)
    return pd.DataFrame(eq).ffill().to_numpy()


def a1_indicators(last: np.ndarray, sma_n: int = A1_SMA, rsi_n: int = A1_RSI
                  ) -> tuple[np.ndarray, np.ndarray]:
    """50-session simple average and 14-session Wilder RSI at every close.

    `last` is sessions x stocks of adjusted EQ/BE closes carried forward over
    sessions without an EQ/BE trade (a1_closes), NaN before a stock's first
    EQ/BE close. The SMA at t is the mean of rows t-49..t (NaN until 50 exist).
    The RSI is seeded with the simple means of the first 14 gains and losses
    after the stock's first close, then avg = (avg x 13 + x) / 14; RSI = 100 -
    100 / (1 + avg_gain / avg_loss), 100 when only avg_loss is 0, and undefined
    (NaN, so A1's condition fails) when avg_gain and avg_loss are both 0.
    """
    L = np.asarray(last, dtype=float)
    T, S = L.shape
    sma = np.full((T, S), np.nan)
    if T >= sma_n:
        win = np.lib.stride_tricks.sliding_window_view(L, sma_n, axis=0)
        sma[sma_n - 1:] = win.mean(axis=-1)
    rsi = np.full((T, S), np.nan)
    cnt = np.zeros(S, dtype=np.int64)
    sg = np.zeros(S)
    sl = np.zeros(S)
    ag = np.full(S, np.nan)
    al = np.full(S, np.nan)
    for t in range(1, T):
        valid = np.isfinite(L[t]) & np.isfinite(L[t - 1])
        with np.errstate(invalid="ignore"):
            d = L[t] - L[t - 1]
        g = np.where(valid & (d > 0), d, 0.0)
        lo_ = np.where(valid & (d < 0), -d, 0.0)
        seeded_before = cnt >= rsi_n
        seeding = valid & ~seeded_before
        sg[seeding] += g[seeding]
        sl[seeding] += lo_[seeding]
        cnt[seeding] += 1
        just = seeding & (cnt == rsi_n)
        ag[just] = sg[just] / rsi_n
        al[just] = sl[just] / rsi_n
        upd = valid & seeded_before
        ag[upd] = (ag[upd] * (rsi_n - 1) + g[upd]) / rsi_n
        al[upd] = (al[upd] * (rsi_n - 1) + lo_[upd]) / rsi_n
        have = cnt >= rsi_n
        with np.errstate(divide="ignore", invalid="ignore"):
            r = np.where(al > 0, 100.0 - 100.0 / (1.0 + ag / al),
                         np.where(ag > 0, 100.0, np.nan))
        rsi[t] = np.where(have, r, np.nan)
    return sma, rsi


def a1_rule(last, sma, rsi) -> np.ndarray:
    """A1's test on one close: at most 10% above its 50-session SMA and RSI below
    70. Anything missing does not qualify."""
    L = np.asarray(last, dtype=float)
    sma = np.asarray(sma, dtype=float)
    rsi = np.asarray(rsi, dtype=float)
    with np.errstate(invalid="ignore"):
        q = (L <= (1.0 + A1_MAX_ABOVE_SMA) * sma) & (rsi < A1_RSI_MAX)
    return q & np.isfinite(L) & np.isfinite(sma) & np.isfinite(rsi)


def a1_qualifies(last: np.ndarray) -> np.ndarray:
    """Sessions x stocks: True where the close of that session qualifies under A1.
    `last` is a1_closes(panel): EQ/BE closes only."""
    sma, rsi = a1_indicators(last)
    return a1_rule(last, sma, rsi)


# ---------------------------------------------------------------- panel
def panel_start(con) -> pd.Timestamp:
    """The `start` to pass engine.build_panel so its 60-day lookback reaches the
    archive's first session: A1's indicators then see every close from the
    start of the archive (Wilder's RSI is seeded at each stock's first EQ/BE
    close).
    The simulation itself starts at the first decision date, as v1's."""
    first = con.execute("SELECT min(date) FROM prices").fetchone()[0]
    return pd.Timestamp(first) + pd.Timedelta(days=60)


def build_panel_v2(con, symbols, end, is_end, run_final_test: bool = False, ids=None,
                   trade_for_trade: bool = True) -> engine.Panel:
    return engine.build_panel(con, symbols, panel_start(con), end, is_end, run_final_test,
                              ids=ids, trade_for_trade=trade_for_trade)


# ---------------------------------------------------------------- simulation
FILL_COLUMNS = ["date", "symbol", "ticker", "side", "units_adjusted", "price_adjusted",
                "value", "cost", "kind", "scheduled_date", "D"]
HOLDING_COLUMNS = ["D", "symbol", "ticker", "rank", "sector", "sigma", "target_weight",
                   "status", "sold_reason"]


@dataclass
class ResultV2:
    nav: pd.DataFrame                  # date, mark, nav (v1's clock)
    fills: pd.DataFrame                # fills.csv: every fill and cancellation
    trades: pd.DataFrame               # v1-compatible trades.csv (buy / sell / cancel)
    holdings: pd.DataFrame             # per D: kept, new and sold names
    episodes: pd.DataFrame
    cash: pd.Series
    rebalance_open: pd.Series          # value at the open of every decision date
    positions: pd.DataFrame            # one row per new position (A1's metric)
    pending_at_end: pd.DataFrame = field(default_factory=pd.DataFrame)
    decisions: pd.DataFrame = field(default_factory=pd.DataFrame)   # per D: cash, counts


def simulate_v2(panel: engine.Panel, decisions, choose, cash_by_D: dict, cfg: ConfigV2,
                is_end, run_final_test: bool = False, qualify: np.ndarray | None = None,
                tickers: Tickers | None = None, log_trades: bool = True) -> ResultV2:
    """Run one v2 portfolio.

    choose(D, held: set) -> (target symbols in selection order, info) where
    info[symbol] holds at least 'sigma' for every target name and, where known,
    'rank', 'sector', 'ticker' and, for a held name not selected, 'sold_reason'.
    cash_by_D maps each decision date to its trend-filter cash fraction.
    qualify (variant b): sessions x stocks, A1's qualifying closes
    (a1_qualifies(a1_closes(panel))).
    """
    engine.guard(panel.end, is_end, run_final_test)
    if cfg.variant == "b" and qualify is None:
        raise ValueError("variant (b) needs A1's qualification table")
    tickers = tickers or Tickers()
    dates, syms = panel.dates, panel.symbols
    T, S = len(dates), len(syms)
    c = cfg.cost
    decisions = [pd.Timestamp(d) for d in decisions]
    idx = dates.get_indexer(decisions)
    if (idx < 0).any():
        raise ValueError("a decision date is not a session in the panel")
    rebal_idx = dict(zip(idx.tolist(), decisions))
    t0 = int(idx[0])
    t_end = T - 1
    wait = cfg.wait_sessions
    # the last session on which a tranche can still be bought, after its scheduled one:
    # in (b) the fallback is session a1_window (the 31st, counting the scheduled one as
    # the first), then v1's 5-session wait for an EQ/BE trade
    tranche_life = wait if cfg.variant == "a" else cfg.a1_window + wait
    ref_close = a1_closes(panel)          # the adoption test's reference close (EQ/BE only)

    units = np.zeros(S)
    cash = float(cfg.initial_capital)
    tgt_val = np.full(S, np.nan)          # v1-style order: rupee target value
    deadline = np.full(S, -1)
    tgt_D = np.full(S, -1)
    tranches: list[dict] = []             # live tranche orders
    entry_t = np.full(S, -1)
    entry_px = np.full(S, np.nan)
    nav_out = np.full(T, np.nan)
    cash_out = np.full(T, np.nan)
    fills, trades, books, episodes, positions, dec_rows = [], [], [], [], [], []
    open_marks: dict = {}
    cur_D = -1

    def d_of(t):
        return dates[t].date() if t >= 0 else None

    def record(t, s, side, du, px, val, kind, sched_t, D_t, reason):
        if not log_trades:
            return
        tk = tickers.at(syms[s], dates[t])
        fills.append({"date": d_of(t), "symbol": syms[s], "ticker": tk, "side": side,
                      "units_adjusted": du, "price_adjusted": px, "value": val,
                      "cost": val * c, "kind": kind, "scheduled_date": d_of(sched_t),
                      "D": d_of(D_t)})
        trades.append({"date": d_of(t), "symbol": syms[s], "side": side, "units": du,
                        "price": px, "value": val, "cost": val * c, "reason": reason,
                        "kind": kind, "scheduled_date": d_of(sched_t), "D": d_of(D_t),
                        "ticker": tk})

    def record_cancel(t, s, side, sched_t, D_t, reason, what):
        if not log_trades:
            return
        tk = tickers.at(syms[s], dates[t])
        fills.append({"date": d_of(t), "symbol": syms[s], "ticker": tk, "side": side,
                      "units_adjusted": 0.0, "price_adjusted": np.nan, "value": 0.0,
                      "cost": 0.0, "kind": "cancelled", "scheduled_date": d_of(sched_t),
                      "D": d_of(D_t)})
        trades.append({"date": d_of(t), "symbol": syms[s], "side": "cancel", "units": 0.0,
                       "price": np.nan, "value": 0.0, "cost": 0.0,
                       "reason": f"{what} cancelled: {reason}", "kind": "cancelled",
                       "scheduled_date": d_of(sched_t), "D": d_of(D_t), "ticker": tk})

    def target_side(s, t):
        cur = units[s] * (panel.last[t - 1, s] if t > 0 else np.nan)
        return "sell" if tgt_val[s] < np.nan_to_num(cur) else "buy"

    def cancel_tranches(t, pred, reason):
        nonlocal tranches
        keep = []
        for o in tranches:
            if pred(o):
                record_cancel(t, o["s"], "buy", o["t_sched"], o["D_t"], reason,
                              f"tranche{o['k']}")
            else:
                keep.append(o)
        tranches = keep

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

        # 2. decisions at the open of D
        if t in rebal_idx:
            D = rebal_idx[t]
            cur_D = t
            px_now = np.where(panel.traded[t], panel.open_[t],
                              panel.last[t - 1] if t > 0 else np.nan)
            nav_open = cash + float(np.nansum(units * px_now))
            open_marks[dates[t]] = nav_open
            # every order still pending ends here: the new book replaces it
            cancel_tranches(t, lambda o: True, "a new decision date replaces it")
            for s in np.flatnonzero(~np.isnan(tgt_val)):
                record_cancel(t, s, target_side(s, t), tgt_D[s], tgt_D[s],
                              "a new decision date replaces it", "order")
            tgt_val[:] = np.nan
            deadline[:] = -1
            tgt_D[:] = -1
            held = {syms[s] for s in np.flatnonzero(units > 0)}
            target, info = choose(D, held)
            if len(set(target)) != len(target):
                raise ValueError(f"{D.date()}: a name is selected twice")
            cash_frac = float(cash_by_D[D])
            sig = pd.Series({s: info.get(s, {}).get("sigma", np.nan) for s in target},
                            dtype=float)
            w = target_weights(sig, cash_frac, cfg.n)
            tset = set(target)
            for sym in target:
                s = syms.get_loc(sym)
                i = info.get(sym, {})
                status = "kept" if sym in held else "new"
                books.append({"D": D.date(), "symbol": sym,
                              "ticker": i.get("ticker", tickers.at(sym, dates[t])),
                              "rank": i.get("rank"), "sector": i.get("sector"),
                              "sigma": i.get("sigma"), "target_weight": float(w[sym]),
                              "status": status, "sold_reason": None})
                if status == "kept":
                    tgt_val[s] = float(w[sym]) * nav_open
                    deadline[s] = t + wait
                    tgt_D[s] = t
                else:
                    V = float(w[sym]) * nav_open
                    pos = len(positions)
                    positions.append({"D": D.date(), "symbol": sym, "V": V, "t_D": t,
                                      "close_before_D": float(ref_close[t - 1, s])
                                      if t > 0 else np.nan})
                    for k in range(cfg.n_tranches):
                        ts = t + k * cfg.tranche_gap
                        tranches.append({"s": s, "k": k + 1, "amount": V / cfg.n_tranches,
                                         "t_sched": ts, "t_last": ts + tranche_life,
                                         "D_t": t, "pos": pos})
            for sym in sorted(held - tset):
                s = syms.get_loc(sym)
                i = info.get(sym, {})
                books.append({"D": D.date(), "symbol": sym,
                              "ticker": i.get("ticker", tickers.at(sym, dates[t])),
                              "rank": i.get("rank"), "sector": i.get("sector"),
                              "sigma": np.nan, "target_weight": 0.0, "status": "sold",
                              "sold_reason": i.get("sold_reason")})
                tgt_val[s] = 0.0
                deadline[s] = t + wait
                tgt_D[s] = t
            dec_rows.append({"D": D.date(), "cash_fraction": cash_frac, "nav_open": nav_open,
                             "n_held": len(target),
                             "n_held_unlabelled_sector": int(sum(
                                 1 for s in target if pd.isna(info.get(s, {}).get("sector")))),
                             "book_weight": float(w.sum()) if len(w) else 0.0})

        # 3. orders executing at this open (none at the final mark)
        if t != t_end:
            op = panel.open_[t]
            live = ~np.isnan(tgt_val)
            ex = live & panel.traded[t]
            delta = np.zeros(S)
            if ex.any():
                cur = units * op
                delta = np.where(ex, tgt_val - np.nan_to_num(cur), 0.0)
            waits = ex & (delta > 0) & ~panel.buyable[t]
            for s in np.flatnonzero(ex & (delta < 0)):
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
                record(t, s, "sell", du, op[s], val, "rebalance", tgt_D[s], tgt_D[s], "rebalance")
                if units[s] == 0.0 and entry_t[s] >= 0:
                    close_episode(s, t, "rebalance")
            buys = []                           # (s, rupees, kind, scheduled t, D t, order)
            for s in np.flatnonzero(ex & (delta > 0) & ~waits):
                buys.append((s, float(delta[s]), "rebalance", tgt_D[s], tgt_D[s], None))
            for o in tranches:
                s = o["s"]
                if t < o["t_sched"] or not panel.buyable[t, s]:
                    continue
                k = t - o["t_sched"]
                if cfg.variant == "a":
                    due = True
                else:                           # A1: at k = 0..29 the previous session's close
                    # qualifies; from k = 30 (the 31st session) the fallback, regardless
                    due = k >= cfg.a1_window or (t > 0 and bool(qualify[t - 1, s]))
                if due:
                    buys.append((s, o["amount"], f"tranche{o['k']}", o["t_sched"], o["D_t"], o))
            need = sum(b[1] for b in buys) * (1 + c)
            scale = min(1.0, cash / need) if need > 0 else 0.0
            for s, amount, kind, ts, Dt, o in buys:
                if o is not None:
                    o["done"] = True
                val = amount * scale
                if val <= 0:                    # no cash at all (v1 drops such a buy too)
                    record_cancel(t, s, "buy", ts, Dt, "no cash left",
                                  kind if o is not None else "order")
                    continue
                du = val / op[s]
                if units[s] == 0 and entry_t[s] < 0:
                    entry_t[s] = t
                    entry_px[s] = op[s]
                units[s] += du
                cash -= val * (1 + c)
                record(t, s, "buy", du, op[s], val, kind, ts, Dt, kind)
            done = ex & ~waits
            tgt_val[done] = np.nan
            deadline[done] = -1
            tranches = [o for o in tranches if not o.get("done")]
            # expiry
            for s in np.flatnonzero(~np.isnan(tgt_val) & (deadline <= t)):
                record_cancel(t, s, target_side(s, t), tgt_D[s], tgt_D[s],
                              f"no trade within {wait} sessions", "order")
                tgt_val[s] = np.nan
                deadline[s] = -1
            cancel_tranches(t, lambda o: t >= o["t_last"],
                            f"not bought by the {tranche_life}th session after its scheduled one")

        if t == t_end:
            # Final mark at the open of the end date. Its close is never read.
            ok = panel.traded[t] & (panel.open_[t] > 0)
            px_mark = np.where(ok, panel.open_[t], panel.last[t - 1])
            nav_out[t] = cash + float(np.nansum(units * px_mark))
            cash_out[t] = cash
            break

        # 4. forced exit after 20 sessions without a trade, at the last close x haircut
        stuck = (units > 0) & (panel.no_trade_run[t] >= cfg.no_trade_exit)
        for s in np.flatnonzero(stuck):
            px = panel.last[t, s] * cfg.delist_haircut
            val = units[s] * px
            cash += val * (1 - c)
            record(t, s, "sell", units[s], px, val, "forced_exit", t, cur_D,
                   f"no trade for {cfg.no_trade_exit} sessions")
            units[s] = 0.0
            if not np.isnan(tgt_val[s]):
                record_cancel(t, s, target_side(s, t), tgt_D[s], tgt_D[s],
                              "the position was force-sold", "order")
            tgt_val[s] = np.nan
            deadline[s] = -1
            cancel_tranches(t, lambda o, _s=s: o["s"] == _s, "the position was force-sold")
            if entry_t[s] >= 0:
                close_episode(s, t, "no_trade_exit")

        # 5. mark at the close
        nav_out[t] = cash + float(np.nansum(units * panel.last[t]))
        cash_out[t] = cash

    pending = [{"symbol": syms[o["s"]], "tranche": o["k"], "amount": o["amount"],
                "scheduled_date": d_of(o["t_sched"]), "D": d_of(o["D_t"])} for o in tranches]
    pending += [{"symbol": syms[s], "tranche": None, "amount": float(tgt_val[s]),
                 "scheduled_date": d_of(tgt_D[s]), "D": d_of(tgt_D[s])}
                for s in np.flatnonzero(~np.isnan(tgt_val))]

    for s in np.flatnonzero(entry_t >= 0):
        episodes.append({"symbol": syms[s], "entry": dates[entry_t[s]], "exit": dates[t_end],
                         "entry_px": entry_px[s], "entry_t": entry_t[s], "exit_t": t_end,
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
    nav = engine.nav_frame(dates[t0], cfg.initial_capital, dates[t0:t_end], nav_out[t0:t_end],
                           dates[t_end], nav_out[t_end])
    return ResultV2(nav=nav, fills=pd.DataFrame(fills, columns=FILL_COLUMNS),
                    trades=pd.DataFrame(trades), holdings=pd.DataFrame(books,
                                                                       columns=HOLDING_COLUMNS),
                    episodes=ep, cash=pd.Series(cash_out[t0:t_end + 1],
                                                index=dates[t0:t_end + 1]),
                    rebalance_open=pd.Series(open_marks, dtype=float),
                    positions=pd.DataFrame(positions), pending_at_end=pd.DataFrame(pending),
                    decisions=pd.DataFrame(dec_rows))


# ---------------------------------------------------------------- choosing
def make_chooser(ranks: pd.DataFrame, cfg: ConfigV2, excluded: pd.DataFrame | None = None,
                 sectors: pd.DataFrame | None = None, tickers: Tickers | None = None):
    """choose(D, held) for simulate_v2 from a ranks frame (one v2 universe per D
    with rank, sector, sigma and ticker): engine.select_top with 12 names, the
    band of 24 and the cap of 3 per sector (an unlabelled name is its own
    sector). A held name that is not selected is labelled: 'outside_band' (in
    the universe, ranked beyond the band), 'thesis_break' (passed v1's rules 1-7
    but failed a hard filter at D) or 'left_universe' (failed one of rules 1-7)."""
    tickers = tickers or Tickers()
    byd = {pd.Timestamp(D): g.set_index("symbol") for D, g in ranks.groupby("D")}
    exd: dict = {}
    if excluded is not None and len(excluded):
        exd = {pd.Timestamp(D): set(g["symbol"]) for D, g in excluded.groupby("D")}
    sec_all = sectors["sector"] if sectors is not None and len(sectors) else pd.Series(dtype=object)
    v1cfg = cfg.v1()
    keep_lim = cfg.buffer_mult * cfg.n
    empty = pd.DataFrame(columns=["rank", "sector", "sigma", "ticker"])

    def choose(D, held):
        D = pd.Timestamp(D)
        r = byd.get(D, empty)
        tgt = engine.select_top(r, held, v1cfg)
        info = {}
        for s in list(tgt) + sorted(set(held) - set(tgt)):
            if s in r.index:
                i = {"ticker": r.at[s, "ticker"] if "ticker" in r else s,
                     "rank": int(r.at[s, "rank"]), "sector": r.at[s, "sector"],
                     "sigma": float(r.at[s, "sigma"])}
                if s not in tgt:
                    i["sold_reason"] = "outside_band" if i["rank"] > keep_lim else "not_selected"
            else:
                i = {"ticker": tickers.at(s, D - pd.Timedelta(days=1)), "rank": None,
                     "sector": sec_all.get(s), "sigma": np.nan,
                     "sold_reason": "thesis_break" if s in exd.get(D, ()) else "left_universe"}
            info[s] = i
        return tgt, info

    return choose


# ---------------------------------------------------------------- A1 metric
def a1_metric(res: ResultV2) -> dict:
    """A1's adoption test, rule 1 (Clarification before the v2 build): per new
    position, the unit-weighted average adjusted fill price over its tranches
    divided by the adjusted close on the session before its D; averaged over
    all new positions weighted by V."""
    pos = res.positions.copy()
    cols = ["D", "symbol", "V", "close_before_D", "tranches_filled", "units", "value",
            "avg_price", "ratio"]
    if pos.empty:
        return {"positions": [], "n_positions": 0, "n_with_fills": 0, "v_weighted_ratio": None}
    f = res.fills[res.fills["kind"].astype(str).str.startswith("tranche")]
    g = f.groupby(["D", "symbol"]).agg(tranches_filled=("kind", "size"),
                                       units=("units_adjusted", "sum"), value=("value", "sum"))
    pos = pos.merge(g.reset_index(), on=["D", "symbol"], how="left")
    pos["tranches_filled"] = pos["tranches_filled"].fillna(0).astype(int)
    pos["avg_price"] = pos["value"] / pos["units"]
    pos["ratio"] = pos["avg_price"] / pos["close_before_D"]
    ok = pos["ratio"].notna() & np.isfinite(pos["ratio"])
    vw = float((pos.loc[ok, "V"] * pos.loc[ok, "ratio"]).sum() / pos.loc[ok, "V"].sum()) \
        if ok.any() else None
    rows = pos[cols].assign(D=pos["D"].astype(str)).to_dict("records")
    return {"definition": READINGS["a1_metric"], "n_positions": int(len(pos)),
            "n_with_fills": int(ok.sum()),
            "n_without_fills": int((~ok).sum()),
            "v_weighted_ratio": vw,
            "unweighted_mean_ratio": float(pos.loc[ok, "ratio"].mean()) if ok.any() else None,
            "positions": rows}


# ---------------------------------------------------------------- replay hook
@dataclass
class Replay:
    """The v2 engine exactly as a run folder used it (see the module docstring).

    Built by `Replay.from_run(con, run_dir)`; drives simulate_v2 on the run's
    own panel, cash fractions and A1 table with any ranks frame. The monkey
    test in jobs/verify_v1.py would use it as:

        rep = engine_v2.Replay.from_run(con, run)
        worst = rep.reproduce(r["strategy"])      # nav.csv's strategy column; <= 1e-9
        for each draw:
            shuffled = rep.ranks.copy()           # a new order within each D (fresh), or
            shuffled["rank"] = ...                # one score per stock (persistent)
            res = rep.simulate(rep.chooser(shuffled))
            nav = res.nav.set_index("date")["nav"]; trades = res.trades

    in place of engine.select_top / engine.simulate. `res.trades` has v1's
    columns (date, symbol, side, units, price, value, cost, reason), so
    verify_v1.turnover applies unchanged.
    """
    cfg: ConfigV2
    panel: engine.Panel
    ranks: pd.DataFrame
    decisions: list
    cash_by_D: dict
    is_end: pd.Timestamp
    run_final_test: bool
    qualify: np.ndarray | None = None
    excluded: pd.DataFrame | None = None
    sectors: pd.DataFrame | None = None
    tickers: Tickers | None = None

    @classmethod
    def from_run(cls, con, run, static: pit.StaticLabels | None = None) -> "Replay":
        run = Path(run)
        m = json.loads((run / "metrics.json").read_text())
        if m.get("engine") != "v2" or "config" not in m:
            raise ValueError(f"{run}: not a v2 run (metrics.json has no v2 config)")
        cfg = ConfigV2(**m["config"])
        decisions = [pd.Timestamp(d) for d in m["decision_dates"]]
        ranks = pd.read_parquet(run / "ranks.parquet")
        ranks["D"] = pd.to_datetime(ranks["D"])
        ranks = ranks[ranks["D"].isin(decisions)]
        ex_p = run / "hard_filter_exclusions.csv"
        excluded = pd.read_csv(ex_p, parse_dates=["D"]) if ex_p.exists() else None
        nav = pd.read_csv(run / "nav.csv", parse_dates=["date"])
        end = pd.Timestamp(nav["date"].iloc[-1]).normalize()
        cal = pit.sessions(con)
        is_end = engine.in_sample_end(cal)
        rft = bool(end > is_end)
        engine.guard(end, is_end, rft)
        tri = load_trend_index(end, cal)
        cash_by_D = {D: trend_state(tri, D)["cash_fraction"] for D in decisions}
        dec = pd.read_csv(run / "decisions.csv", parse_dates=["D"]).set_index("D")
        for D, v in cash_by_D.items():
            if abs(float(dec.at[D, "cash_fraction"]) - v) > 0:
                raise RuntimeError(f"{D.date()}: trend cash {v} differs from decisions.csv")
        static = static or pit.StaticLabels.load(con)
        panel = build_panel_v2(con, ranks["symbol"].unique(), end, is_end, rft, ids=static.ids)
        qualify = a1_qualifies(a1_closes(panel)) if cfg.variant == "b" else None
        sectors = nse_sectors(load_nse_labels(), static.ids)
        return cls(cfg=cfg, panel=panel, ranks=ranks, decisions=decisions, cash_by_D=cash_by_D,
                   is_end=is_end, run_final_test=rft, qualify=qualify, excluded=excluded,
                   sectors=sectors, tickers=Tickers(static.ids))

    def chooser(self, ranks: pd.DataFrame | None = None):
        return make_chooser(self.ranks if ranks is None else ranks, self.cfg, self.excluded,
                            self.sectors, self.tickers)

    def simulate(self, choose=None, log_trades: bool = True) -> ResultV2:
        return simulate_v2(self.panel, self.decisions, choose or self.chooser(), self.cash_by_D,
                           self.cfg, self.is_end, self.run_final_test, self.qualify,
                           self.tickers, log_trades)

    def reproduce(self, nav_strategy) -> float:
        """Re-run the strategy; the worst relative difference from `nav_strategy`
        (the run's nav.csv strategy column, mark for mark)."""
        res = self.simulate()
        a = res.nav["nav"].to_numpy(float)
        b = np.asarray(nav_strategy, dtype=float)
        if len(a) != len(b):
            raise RuntimeError(f"replay has {len(a)} marks, the run {len(b)}")
        return float(np.max(np.abs(a / b - 1)))
