"""Falsification test: is a stock's reaction to news persistent enough to bid on?

The feature this is meant to justify is an email that says "buy this company,
bid at THIS price", where the price comes from how that company has historically
behaved after its own news -- some names spike and give it all back to profit
booking, others drift up, and the claim is that which one a company is, is a
property of the company.

That claim is testable and this study exists to try to break it. Split each
company's announcement history in half, estimate its reaction pattern on one
half, and see whether it predicts the other half. If it does not, per-company
entry timing is a story and must be killed today rather than shipped.

THE THREE THINGS THAT MAKE A NAIVE VERSION OF THIS STUDY LIE:

1. THE NULL IS NOT ZERO. Split-half correlations of a drift statistic are
   large by construction. Two halves of a company's history sit in overlapping
   market regimes, and a company that drifted in 2023 drifted in 2023 in both
   halves. Measured here on placebo dates -- the same events, the same
   companies, the same spacing, shifted bodily by a few months so that they
   align with no news at all -- the twenty-day continuation reproduces r = 0.49
   of an observed 0.51. A study testing against zero publishes "highly
   persistent, p < 1e-90" for something that is 96% artefact. Every correlation
   here is read against a placebo that was put through the entire pipeline.

2. THE EXCHANGE MOVES EVERY STOCK EVERY DAY. Overnight returns on this archive
   average +0.28% to +0.57% a year and intraday returns average -0.26% to
   -0.38%, with no news. "Buy at the close of day+3 rather than the open of
   day+1" therefore earns a third of a percent a day mechanically. Every leg is
   benchmarked against the same leg in a matched cohort, never against a
   close-to-close index.

3. A PULLBACK RULE DOES NOT FILL WHEN THE STOCK RUNS AWAY. It systematically
   misses the winners, so scoring it on filled orders only is a lie. Every rule
   is scored on the whole opportunity set with unfilled orders bought at the
   benchmark price instead, and the fill rate and the return of the orders that
   never filled are printed on the face of every row.

WHAT WOULD FALSIFY THE FEATURE, DECLARED BEFORE THE RUN: an out-of-sample
top-minus-bottom quintile spread in abnormal continuation below 150 basis
points, or a placebo-adjusted z below 3.0. 150bp is twice the measured
round-trip cost floor for stocks of this size; below it there is nothing to put
in an email even if the pattern is real.

A clean negative is the successful outcome of this work, not a failure of it.

    python -m jobs.study_news_reaction
    python -m jobs.study_news_reaction --shift-reps 400 --stage all
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import date
from math import erf, sqrt
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from zen.validation import eventstudy as es, paths as pth, trials

log = logging.getLogger(__name__)

STUDY = "news_reaction"
ROOT = Path(__file__).resolve().parents[1]

# The four filing families the user cares about, taken from the regex column,
# then cleaned with NSE's OWN announcement subtype. The regex `orders` bucket is
# 44% precise and roughly a quarter of it is SEBI/GST/NCLT orders AGAINST the
# company -- opposite sign, pooled in with order wins, and its share ran from 0%
# in 2022-23 to 40% in 2025. Averaging a good-news reaction with a bad-news
# reaction under year-varying weights is enough to manufacture a null on its own.
FAMILIES = ("expansion", "orders", "guidance", "results")

# Investor-relations logistics. A calendar notice for a future conference call
# is, by construction, a filing containing no news. 95% of the `guidance`
# category is this, and the published "guidance +0.0pp at 12m, noise" result was
# measuring meeting schedules.
IR_LOGISTICS = (
    "Analysts/Institutional Investor Meet/Con. Call Updates",
    "Investor Presentation",
    "Schedule of Analysts/Institutional Investor Meet/Con. Call",
    "Transcript of Analysts/Institutional Investor Meet/Con. Call",
    "Recording of Analysts/Institutional Investor Meet/Con. Call",
)

# The only reliably SIGNED event sets in this archive. Everything else --
# ratings (96% state no direction), results (0.1% carry a number), M&A -- is
# unsigned in the text and cannot be split into good and bad.
GOOD_DESC = (
    "Bagging/Receiving of orders/contracts", "Bagging orders/contract",
    "Awarding of order(s)/contract(s)", "Awarding orders/contract",
    "Capacity addition", "Capacity addition/product launch",
    "Commencement of commercial production/operations",
)
# Diversification/Disinvestment is deliberately NOT here: it is one NSE bucket
# holding both expansion and disposal, sign-mixed by construction.
BAD_DESC = (
    "Action(s) taken or orders passed", "Action(s) initiated or orders passed",
    "Corporate Insolvency Resolution Process",
)

KILL_BP = 150.0        # the number the feature has to clear
G1_Z = 3.0             # placebo-adjusted z below this stops the study
PRIOR_TRIALS = 39      # already consumed by earlier studies in state/trials.jsonl
PILOT_STATS = 50       # exploratory split-half statistics examined before this run


def open_readonly() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    for name, pattern in [
        ("prices", "data/daily/**/*.parquet"),
        ("announcements", "data/announcements/**/*.parquet"),
        ("corpactions", "data/corpactions/*.parquet"),
        ("indices", "data/indices/*.parquet"),
    ]:
        con.execute(f"CREATE VIEW {name} AS SELECT * FROM "
                    f"read_parquet('{pattern}', union_by_name=true)")
    return con


# ---------------------------------------------------------------------------
# small statistics, written out rather than imported: scipy is not installed
# ---------------------------------------------------------------------------

def norm_cdf(x: float) -> float:
    return 0.5 * (1 + erf(x / sqrt(2)))


def pearson(a: np.ndarray, b: np.ndarray) -> float:
    m = np.isfinite(a) & np.isfinite(b)
    if m.sum() < 3:
        return float("nan")
    a, b = a[m], b[m]
    a = a - a.mean()
    b = b - b.mean()
    d = np.sqrt((a * a).sum() * (b * b).sum())
    return float((a * b).sum() / d) if d > 0 else float("nan")


def spearman(a: np.ndarray, b: np.ndarray) -> float:
    m = np.isfinite(a) & np.isfinite(b)
    if m.sum() < 3:
        return float("nan")
    ra = pd.Series(a[m]).rank().to_numpy()
    rb = pd.Series(b[m]).rank().to_numpy()
    return pearson(ra, rb)


def demean(y: np.ndarray, g: np.ndarray) -> np.ndarray:
    """Absorb a categorical by subtracting its group mean. O(n), no dummies."""
    k = int(g.max()) + 1
    n = np.bincount(g, minlength=k).astype(float)
    s = np.bincount(g, weights=np.nan_to_num(y), minlength=k)
    return y - (s / np.maximum(n, 1))[g]


def residualise(y: np.ndarray, X: np.ndarray, g: np.ndarray | None) -> np.ndarray:
    """Residual of y on X, optionally after absorbing a categorical g."""
    yy = y.copy()
    XX = X.copy()
    if g is not None:
        yy = demean(yy, g)
        for j in range(XX.shape[1]):
            XX[:, j] = demean(XX[:, j], g)
    XX = np.column_stack([XX, np.ones(len(yy))])
    ok = np.isfinite(yy) & np.isfinite(XX).all(axis=1)
    out = np.full(len(yy), np.nan)
    if ok.sum() < XX.shape[1] + 5:
        return out
    beta, *_ = np.linalg.lstsq(XX[ok], yy[ok], rcond=None)
    out[ok] = yy[ok] - XX[ok] @ beta
    return out


def cluster_bootstrap_mean(values: np.ndarray, clusters: np.ndarray,
                           reps: int, rng) -> tuple[float, float]:
    """Percentile interval for a mean, resampling whole clusters.

    Events are not independent observations. Two thousand filings from six
    hundred companies with overlapping windows carry nowhere near two thousand
    observations, and the naive standard error understates the truth by 1.4x to
    3.1x on this archive.
    """
    ok = np.isfinite(values)
    values, clusters = values[ok], clusters[ok]
    if len(values) < 10:
        return float("nan"), float("nan")
    uniq, inv = np.unique(clusters, return_inverse=True)
    order = np.argsort(inv, kind="stable")
    vs = values[order]
    bounds = np.searchsorted(inv[order], np.arange(len(uniq) + 1))
    draws = np.empty(reps)
    for r in range(reps):
        pick = rng.integers(0, len(uniq), len(uniq))
        idx = np.concatenate([np.arange(bounds[p], bounds[p + 1]) for p in pick])
        draws[r] = vs[idx].mean() if len(idx) else np.nan
    return float(np.nanpercentile(draws, 2.5)), float(np.nanpercentile(draws, 97.5))


# ---------------------------------------------------------------------------
# sample
# ---------------------------------------------------------------------------

def load_filings(con) -> pd.DataFrame:
    quoted = ", ".join(f"'{d}'" for d in IR_LOGISTICS)
    fams = ", ".join(f"'{f}'" for f in FAMILIES)
    df = con.execute(f"""
        WITH a AS (
            SELECT symbol, an_dt, trade_date, category,
                   CASE WHEN position(': ' IN subject) > 0
                        THEN substr(subject, 1, position(': ' IN subject) - 1)
                        ELSE subject END AS nse_desc
            FROM announcements
            WHERE category IN ({fams})
        )
        SELECT * FROM a WHERE nse_desc NOT IN ({quoted})
    """).df()
    df["an_dt"] = pd.to_datetime(df["an_dt"])
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return df


def _desc_rank(desc: pd.Series) -> np.ndarray:
    """Which filing wins when a company files several subtypes on one day.

    A signed, discrete event beats an unsigned scheduled one. Without a fixed
    hierarchy the survivor of a same-day collision is whatever the sort happened
    to put first, which quietly makes the sample depend on row order.
    """
    r = np.full(len(desc), 5, dtype=np.int8)
    r[desc.isin(BAD_DESC).to_numpy()] = 3
    r[desc.str.contains("Financial Result", na=False).to_numpy()] = 4
    r[desc.isin(("Capacity addition", "Capacity addition/product launch",
                 "Commencement of commercial production/operations")).to_numpy()] = 2
    r[desc.isin(("Bagging/Receiving of orders/contracts", "Bagging orders/contract",
                 "Awarding of order(s)/contract(s)", "Awarding orders/contract")).to_numpy()] = 1
    return r


def build_events(con, panel: pth.Panel, min_liq_cr: float, spacing: int,
                 min_history: int, min_forward: int) -> pd.DataFrame:
    """Every filter, in order, with the surviving count printed at each step."""
    f = load_filings(con)
    steps = [("filings in the four families, IR logistics removed", len(f),
              f["symbol"].nunique())]

    # Day zero: the first session ON OR AFTER trade_date that the symbol traded.
    pos = pth.positions(panel, f, date_col="trade_date")
    f["pos"] = pos
    f = f[np.isfinite(f["pos"])].copy()
    f["pos"] = f["pos"].astype(np.int64)
    steps.append(("rolled forward to a session the symbol actually traded",
                  len(f), f["symbol"].nunique()))

    pdate = panel.df["date"].to_numpy()
    f["d0"] = pdate[f["pos"].to_numpy()]

    # Arrival gate. The point of this is the first OPEN that could be traded on
    # the information, which is not the same thing as trade_date. `_trade_date`
    # upstream uses minute > 30, so a filing stamped 15:30:00 -- after the close
    # -- is attributed to the session that had already ended.
    t = f["an_dt"].dt.time
    same_day = f["an_dt"].dt.normalize().to_numpy() == f["d0"]
    tmin = f["an_dt"].dt.hour * 60 + f["an_dt"].dt.minute
    sec = f["an_dt"].dt.second
    cls = np.where(~same_day, np.where(tmin < 9 * 60, "PRE", "POST"), "INTRA")
    ambig = same_day & (((tmin >= 9 * 60) & (tmin < 9 * 60 + 15)) |
                        ((tmin == 15 * 60 + 30) & (sec <= 59)) |
                        (tmin > 15 * 60 + 30))
    cls = np.where(same_day & (tmin < 9 * 60), "PRE", cls)
    cls = np.where(ambig, "AMBIG", cls)
    f["filing_class"] = cls
    n_ambig = int((f["filing_class"] == "AMBIG").sum())
    f = f[f["filing_class"] != "AMBIG"].copy()
    steps.append((f"arrival gate applied ({n_ambig} ambiguous 09:00-09:15 / "
                  f"15:30:00-15:30:59 filings dropped)", len(f), f["symbol"].nunique()))

    del t
    # Dedup to one event per (symbol, d0), keeping the most informative subtype.
    f["rank"] = _desc_rank(f["nse_desc"])
    f = f.sort_values(["symbol", "pos", "rank", "an_dt"])
    multi = f.groupby(["symbol", "pos"])["nse_desc"].transform("nunique") > 1
    f["multi_desc"] = multi
    f = f.drop_duplicates(["symbol", "pos"], keep="first")
    steps.append(("deduped to one event per symbol-day", len(f), f["symbol"].nunique()))

    # Spacing: two filings about one contract are one piece of news.
    keep = np.ones(len(f), dtype=bool)
    sym = f["symbol"].to_numpy()
    p = f["pos"].to_numpy()
    last_sym, last_pos = None, -10**9
    for i in range(len(f)):
        if sym[i] != last_sym:
            last_sym, last_pos = sym[i], -10**9
        if p[i] - last_pos < spacing:
            keep[i] = False
        else:
            last_pos = p[i]
    f = f[keep].copy()
    steps.append((f"spaced at least {spacing} sessions apart within a company",
                  len(f), f["symbol"].nunique()))

    P = f["pos"].to_numpy()
    d = panel.df
    ntc = d["normal_turnover"].to_numpy()[P] / 1e7
    f["normal_turnover_cr"] = ntc
    f = f[np.isfinite(ntc) & (ntc >= min_liq_cr)].copy()
    steps.append((f"trailing-60d median turnover (window ending d0-1) >= Rs {min_liq_cr}cr",
                  len(f), f["symbol"].nunique()))

    # History and forward room. The liquidity screen above uses a window that
    # ENDS the session before day zero -- copying study_filings, which screens on
    # the event session itself, would screen on turnover the filing caused.
    P = f["pos"].to_numpy()
    have_back = P - panel.first[P]
    have_fwd = panel.last[P] - P
    f = f[(have_back >= min_history) & (have_fwd >= min_forward)].copy()
    steps.append((f">= {min_history} prior and >= {min_forward} forward sessions",
                  len(f), f["symbol"].nunique()))

    # Corporate actions: exclude ONLY factor-bearing ones. Dividends are 15,086
    # of 25,853 corpaction rows, an 80-session window catches an annual dividend
    # about a third of the time, and dividend payers are larger, more profitable
    # and more liquid -- excluding on them would delete the sample along exactly
    # the axis that produced this repo's retracted 6.4x liquidity result.
    acts = con.execute("""
        SELECT symbol, ex_date FROM corpactions
        WHERE action IN ('split','bonus','rights') OR factor IS NOT NULL
    """).df()
    acts["ex_date"] = pd.to_datetime(acts["ex_date"])
    P = f["pos"].to_numpy()
    lo_d = pdate[np.maximum(P - 5, panel.first[P])]
    hi_d = pdate[np.minimum(P + 60, panel.last[P])]
    by_sym = {s: np.sort(g["ex_date"].to_numpy()) for s, g in acts.groupby("symbol")}
    hit = np.zeros(len(f), dtype=bool)
    fs = f["symbol"].to_numpy()
    for i in range(len(f)):
        arr = by_sym.get(fs[i])
        if arr is None:
            continue
        a = np.searchsorted(arr, lo_d[i], "left")
        b = np.searchsorted(arr, hi_d[i], "right")
        hit[i] = b > a
    f = f[~hit].copy()
    steps.append(("no split/bonus/rights ex-date in [d0-5, d0+60]",
                  len(f), f["symbol"].nunique()))

    # Unexplained-move screen. ~25 real splits and bonuses in this archive carry
    # a NULL factor because the parser demands the literal "per share" or the
    # word "bonus", and adjustment_factors filters factor < 1 so a consolidation
    # is never adjusted at all. A 40% single session is a corporate action, not
    # a company: the widest circuit filter NSE applies is 20%.
    P = f["pos"].to_numpy()
    scc = panel.df["simple_cc"].to_numpy()
    win = pth.window_matrix(panel, P, scc, -20, 60)
    bad = np.nanmax(np.abs(win), axis=1) > 0.40
    f = f[~bad].copy()
    steps.append(("no unadjusted >40% single session in [d0-20, d0+60]",
                  len(f), f["symbol"].nunique()))

    f = f.reset_index(drop=True)
    f["event_id"] = np.arange(len(f))
    f["sign"] = np.where(f["nse_desc"].isin(GOOD_DESC), "good",
                         np.where(f["nse_desc"].isin(BAD_DESC), "bad", "unsigned"))
    print("\nFILTER LADDER (events / companies)")
    for label, n, k in steps:
        print(f"  {n:>8,}  {k:>5}   {label}")
    return f


def event_stats(panel: pth.Panel, ev: pd.DataFrame) -> pd.DataFrame:
    """The five path statistics, plus every characteristic, all close-anchored."""
    P = ev["pos"].to_numpy()
    d = panel.df
    ev = ev.copy()

    ev["runup"] = panel.car(P - 11, P - 1)
    ev["pop"] = panel.car(P - 1, P)
    ev["cont"] = panel.car(P, P + 10)
    ev["cont_2_10"] = panel.car(P + 1, P + 10)
    ev["drift20"] = panel.car(P, P + 20)
    ev["drift60"] = panel.car(P, P + 60)

    sigma = d["sigma"].to_numpy()[P]
    ev["sigma60"] = sigma
    # Standardised twin of every statistic. Raw amplitude has an ICC of 0.160
    # against 0.118 standardised, so a quarter of "this company always spikes"
    # is just "this company is volatile" -- which is a screen this repo already
    # has, and is not a reaction pattern.
    for c, L in [("runup", 10), ("pop", 1), ("cont", 10), ("cont_2_10", 9),
                 ("drift20", 20), ("drift60", 60)]:
        ev[c + "_z"] = ev[c].to_numpy() / (sigma * np.sqrt(L))

    # pop is meaningless for an INTRA filing: at daily resolution the day-zero
    # gap is pre-news and the intraday leg is a blend, so there is no pre-news
    # reference price. Carried in the persistence test (continuation from the
    # day-zero close is post-information for all three classes), excluded from
    # every amplitude statistic.
    intra = (ev["filing_class"] == "INTRA").to_numpy()
    ev["has_pop"] = ~intra
    ev.loc[intra, ["pop", "pop_z"]] = np.nan

    raw = panel.raw_cum
    ev["ret_6m"] = np.where(panel.within(P, P - 127), raw[P - 1] - raw[np.maximum(P - 127, 0)], np.nan)
    ev["ret_12m"] = np.where(panel.within(P, P - 253), raw[P - 1] - raw[np.maximum(P - 253, 0)], np.nan)
    hi, lo = d["high_252"].to_numpy()[P - 1], d["low_252"].to_numpy()[P - 1]
    c1 = panel.adj_close[P - 1]
    ev["pos_52w"] = (c1 - lo) / np.where(hi - lo > 0, hi - lo, np.nan)
    ev["price"] = c1
    ev["band"] = d["band"].to_numpy()[P]
    ev["band_headroom"] = (ev["band"] - np.abs(np.expm1(ev["pop"]))) / ev["band"]
    ev["band_hit"] = d["band_hit"].to_numpy()[P]
    ev["series_d0"] = d["series"].to_numpy()[P]
    ev["rel_turnover_d0"] = d["rel_turnover"].to_numpy()[P]

    tw = pth.window_matrix(panel, P, d["turnover"].to_numpy(), -60, -1)
    ev["turnover_trend"] = (np.nanmedian(tw[:, -5:], axis=1) /
                            np.nanmedian(tw[:, :55], axis=1))

    lk = pth.window_matrix(panel, P, d["locked"].to_numpy().astype(float), 0, 10)
    sp = pth.window_matrix(panel, P, d["single_print"].to_numpy().astype(float), 0, 10)
    ev["locked_any"] = np.nanmax(lk, axis=1) > 0
    ev["single_any"] = np.nanmax(sp, axis=1) > 0
    ser = pth.window_matrix(panel, P, (d["series"] == "BE").to_numpy().astype(float), 1, 20)
    ev["became_BE_20"] = np.nanmax(ser, axis=1) > 0

    ev["year"] = pd.to_datetime(ev["d0"]).dt.year
    ev["quarter"] = pd.to_datetime(ev["d0"]).dt.to_period("Q").astype(str)
    ev["month"] = pd.to_datetime(ev["d0"]).dt.to_period("M").astype(str)
    return ev


# ---------------------------------------------------------------------------
# persistence pipeline -- one function, used identically on real and placebo data
# ---------------------------------------------------------------------------

STAGE3_CHARS = ["log_ntc", "log_sig", "ret_6m", "ret_12m", "pos_52w",
                "log_price", "log_n", "mean_pop_z"]


def _stage1(y: np.ndarray, ev: pd.DataFrame, gcode: np.ndarray) -> np.ndarray:
    """Strip everything that is not company identity.

    Pooled over all events rather than per company: with five events per company
    a per-company control removes the signal along with the nuisance. The
    absorbed categorical is subtype x quarter, which handles both the composition
    of what a company files and NSE's own taxonomy changing three times inside
    the window.
    """
    popz = np.nan_to_num(ev["pop_z"].to_numpy())
    neg = popz * (ev["pop"].to_numpy() < 0)
    X = np.column_stack([
        popz, np.nan_to_num(neg),
        (~ev["has_pop"].to_numpy()).astype(float),
        (ev["filing_class"].to_numpy() == "PRE").astype(float),
        np.log(np.maximum(ev["normal_turnover_cr"].to_numpy(), 1e-3)),
        np.log(np.maximum(ev["sigma60"].to_numpy(), 1e-5)),
    ])
    return residualise(y, X, gcode)


def run_persistence(ev: pd.DataFrame, y: np.ndarray, gcode: np.ndarray,
                    half: np.ndarray, min_per_half: int = 5,
                    stage3: bool = True) -> dict:
    """Half-1 theta against half-2 theta, across companies. The whole test."""
    f = _stage1(y, ev, gcode)
    ok = np.isfinite(f) & np.isfinite(half)
    if ok.sum() < 100:
        return {"k": 0, "r": float("nan")}

    w = pd.DataFrame({
        "sym": ev["symbol"].to_numpy()[ok], "half": half[ok].astype(int),
        "f": f[ok],
        "log_ntc": np.log(np.maximum(ev["normal_turnover_cr"].to_numpy()[ok], 1e-3)),
        "log_sig": np.log(np.maximum(ev["sigma60"].to_numpy()[ok], 1e-5)),
        "ret_6m": ev["ret_6m"].to_numpy()[ok], "ret_12m": ev["ret_12m"].to_numpy()[ok],
        "pos_52w": ev["pos_52w"].to_numpy()[ok],
        "log_price": np.log(np.maximum(ev["price"].to_numpy()[ok], 1e-3)),
        "mean_pop_z": np.nan_to_num(ev["pop_z"].to_numpy()[ok]),
        "cont_raw": ev["cont"].to_numpy()[ok],
    })
    g = w.groupby(["sym", "half"])
    # Median, not mean: one unparsed corporate action or one takeover would
    # otherwise own a five-event company estimate.
    agg = g.agg(theta=("f", "median"), n=("f", "size"),
                cont_raw=("cont_raw", "median"),
                **{c: (c, "mean") for c in STAGE3_CHARS if c != "log_n"})
    agg = agg.reset_index()
    agg["log_n"] = np.log(agg["n"])
    agg = agg[agg["n"] >= min_per_half]

    agg["theta_r"] = np.nan
    if stage3:
        # Residualise on same-half characteristics. If identity survives only
        # before this step, the email cannot say "this company fades", only
        # "stocks like this fade" -- a different and much weaker product.
        for h in (0, 1):
            m = agg["half"] == h
            if m.sum() < 30:
                continue
            X = agg.loc[m, STAGE3_CHARS].to_numpy(dtype=float)
            X = np.where(np.isfinite(X), X, np.nan)
            for j in range(X.shape[1]):
                col = X[:, j]
                col[~np.isfinite(col)] = np.nanmedian(col) if np.isfinite(col).any() else 0.0
                X[:, j] = col
            agg.loc[m, "theta_r"] = residualise(agg.loc[m, "theta"].to_numpy(), X, None)
    else:
        agg["theta_r"] = agg["theta"]

    if agg["half"].nunique() < 2 or len(agg) < 60:
        return {"k": 0, "r": float("nan")}
    wide = agg.pivot(index="sym", columns="half",
                     values=["theta", "theta_r", "cont_raw", "n"])
    wide = wide.dropna(subset=[("theta", 0), ("theta", 1)])
    if len(wide) < 30:
        return {"k": len(wide), "r": float("nan")}
    return {
        "k": int(len(wide)),
        "r_raw": pearson(wide[("theta", 0)].to_numpy(), wide[("theta", 1)].to_numpy()),
        "r": pearson(wide[("theta_r", 0)].to_numpy(), wide[("theta_r", 1)].to_numpy()),
        "wide": wide,
    }


def half_labels(ev: pd.DataFrame, mode: str) -> np.ndarray:
    """Odd/even interleave, or a split at the company's own median event date.

    Odd/even first and always: it has maximum power, it is immune to NSE's
    taxonomy collapsing into `Updates` for all of 2023 and to the modern
    vocabulary arriving in late 2024, and it is a strict upper bound. A
    statistic that fails odd/even cannot pass chronological, so failing it ends
    the study without spending the second test.
    """
    order = ev.groupby("symbol").cumcount().to_numpy()
    n = ev.groupby("symbol")["pos"].transform("size").to_numpy()
    if mode == "oddeven":
        return (order % 2).astype(float)
    return (order >= n / 2).astype(float)


# ---------------------------------------------------------------------------
# placebos
# ---------------------------------------------------------------------------

def placebo_shift(panel: pth.Panel, ev: pd.DataFrame, stat: str, half: np.ndarray,
                  reps: int, rng, kind: str = "shift",
                  standardise: bool = True) -> np.ndarray:
    """The null distribution of the split-half correlation with news removed.

    The identical events, shifted bodily by the same k trading sessions. This
    keeps company identity, per-company event counts, within-company spacing,
    cross-company date clustering, sector composition and volatility regime, and
    destroys only the alignment with news -- so each company is its own control.

    The alternatives were tried and rejected: scattering each event to a random
    session of the same company in the same year, or permuting company labels
    within a date, both destroy the calendar adjacency between a company's two
    halves, which is the very thing that generates a correlation of 0.33 to 0.49
    with no news content. Those nulls hand a pure artefact a p below 0.001.
    They are kept below as diagnostics, never as the null.
    """
    P = ev["pos"].to_numpy()
    d = panel.df
    sigma_all = d["sigma"].to_numpy()
    qcode_all = pd.factorize(pd.to_datetime(d["date"]).dt.to_period("Q").astype(str))[0]
    desc_code = pd.factorize(ev["nse_desc"])[0]
    L = {"cont": 10, "cont_2_10": 9, "drift20": 20, "pop": 1}[stat]
    out = np.full(reps, np.nan)

    for r in range(reps):
        if kind == "shift":
            k = int(rng.integers(56, 261)) * (1 if rng.random() < 0.5 else -1)
            P2 = P + k
        elif kind == "scatter":
            # within-company, within-year reshuffle: a deliberately generous null
            jit = rng.integers(-250, 251, len(P))
            P2 = P + jit
        else:                                  # "label": permute companies within date
            P2 = P.copy()

        okw = panel.within(P, P2) & panel.within(P, P2 + 60) & panel.within(P, P2 - 11)
        if okw.sum() < 500:
            continue
        e2 = ev.copy()
        e2["pos"] = P2
        if stat == "pop":
            y_raw = panel.car(P2 - 1, P2)
        elif stat == "cont_2_10":
            y_raw = panel.car(P2 + 1, P2 + 10)
        elif stat == "drift20":
            y_raw = panel.car(P2, P2 + 20)
        else:
            y_raw = panel.car(P2, P2 + 10)
        sg = np.where(okw, sigma_all[np.clip(P2, 0, len(sigma_all) - 1)], np.nan)
        # The null has to be built for the EXACT statistic being tested. A raw
        # statistic read against a standardised null is not a placebo, it is a
        # different question.
        y = (y_raw / (sg * np.sqrt(L))) if standardise else y_raw.copy()
        y[~okw] = np.nan

        e2["pop"] = panel.car(P2 - 1, P2)
        e2["pop_z"] = e2["pop"].to_numpy() / sg
        e2.loc[~e2["has_pop"].to_numpy(), ["pop", "pop_z"]] = np.nan
        e2["sigma60"] = sg
        q2 = np.where(okw, qcode_all[np.clip(P2, 0, len(qcode_all) - 1)], 0)
        g2 = (desc_code.astype(np.int64) * 1000 + q2)
        g2 = pd.factorize(g2)[0]

        res = run_persistence(e2, y, g2, np.where(okw, half, np.nan))
        out[r] = res.get("r", np.nan)
    return out


def positive_control(ev: pd.DataFrame, y: np.ndarray, gcode: np.ndarray,
                     half: np.ndarray, levels, rng) -> pd.DataFrame:
    """Inject a real per-company effect and see whether the pipeline finds it.

    Without this, "the design detects nothing" and "the design is broken" are the
    same output -- and the expected outcome of this study is a null, so that
    distinction is the whole deliverable. What comes back is a power curve: the
    smallest true company-level effect this archive can separate from the
    placebo.
    """
    base = _stage1(y, ev, gcode)
    sd = np.nanstd(base)
    codes, uniq = pd.factorize(ev["symbol"])
    rows = []
    for s in levels:
        g = rng.normal(0, s * sd, len(uniq))[codes]
        res = run_persistence(ev, y + g, gcode, half)
        rows.append({"s": s, "k": res["k"], "r": res.get("r", np.nan),
                     "r_raw": res.get("r_raw", np.nan)})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# reporting helpers
# ---------------------------------------------------------------------------

def bp(x) -> float:
    return round(float(x) * 1e4, 1)


def quintile_table(wide: pd.DataFrame, rng, reps: int = 2000) -> pd.DataFrame:
    """Sort companies on half-1 theta; report what half 2 actually did.

    This is where the kill number is read. Not the correlation -- a correlation
    of 0.05 can be enormously significant and worth nothing. The question is how
    many basis points separate the companies you would have bid on from the ones
    you would have skipped.
    """
    t1 = wide[("theta_r", 0)].to_numpy()
    out2 = wide[("cont_raw", 1)].to_numpy()
    q = pd.qcut(pd.Series(t1).rank(method="first"), 5, labels=False) + 1
    rows = []
    for k in range(1, 6):
        m = q.to_numpy() == k
        lo, hi = cluster_bootstrap_mean(out2[m], np.arange(m.sum()), reps, rng)
        rows.append({"quintile": k, "companies": int(m.sum()),
                     "half2_cont_bp": bp(np.nanmean(out2[m])),
                     "ci_lo_bp": bp(lo), "ci_hi_bp": bp(hi)})
    t = pd.DataFrame(rows)
    spread = t.loc[t.quintile == 5, "half2_cont_bp"].iloc[0] - t.loc[t.quintile == 1, "half2_cont_bp"].iloc[0]
    t.attrs["spread_bp"] = spread
    t.attrs["mono_rho"] = spearman(t["quintile"].to_numpy(), t["half2_cont_bp"].to_numpy())
    return t


def rec(variant: dict, result: dict, kind: str = "confirmatory") -> None:
    trials.record(STUDY, {**variant, "kind": kind}, result)


# ---------------------------------------------------------------------------
# T1 shape, and the flat-path placebo that has to pass before anything is read
# ---------------------------------------------------------------------------

def shape_table(panel: pth.Panel, ev: pd.DataFrame, label: str,
                rng, reps: int) -> pd.DataFrame:
    P = ev["pos"].to_numpy()
    car = pth.car_path(panel, P, -20, 60, anchor=-1)
    cg = pth.car_path(panel, P, -20, 60, anchor=-1, leg="gap")
    ci_ = pth.car_path(panel, P, -20, 60, anchor=-1, leg="intra")
    rel = np.arange(-20, 61)
    keep = [-20, -10, -5, -1, 0, 1, 2, 3, 5, 10, 20, 40, 60]
    sym = pd.factorize(ev["symbol"])[0]
    rows = []
    turn = pth.window_matrix(panel, P, panel.df["rel_turnover"].to_numpy(), -20, 60)
    bh = pth.window_matrix(panel, P, panel.df["band_hit"].to_numpy().astype(float), -20, 60)
    for r in keep:
        j = int(np.where(rel == r)[0][0])
        v = car[:, j]
        lo, hi = cluster_bootstrap_mean(v, sym, reps, rng)
        rows.append({"rel": r, "n": int(np.isfinite(v).sum()),
                     "mean_car_bp": bp(np.nanmean(v)), "med_car_bp": bp(np.nanmedian(v)),
                     "ci_lo_bp": bp(lo), "ci_hi_bp": bp(hi),
                     "gap_leg_bp": bp(np.nanmean(cg[:, j])),
                     "intra_leg_bp": bp(np.nanmean(ci_[:, j])),
                     "rel_turnover": round(float(np.nanmedian(turn[:, j])), 2),
                     "band_hit_pct": round(100 * float(np.nanmean(bh[:, j])), 2)})
    t = pd.DataFrame(rows)
    t.insert(0, "sample", label)
    return t


def flat_path_placebo(panel: pth.Panel, ev: pd.DataFrame, rng, reps: int) -> pd.DataFrame:
    """The same machinery on matched NON-event days for the same companies.

    This is a publish gate, not a result. If a spike-and-fade appears where
    there is no news, the adjustment, the alignment or the control selection is
    broken and every number downstream is void.
    """
    P = ev["pos"].to_numpy()
    k = rng.integers(56, 261, len(P)) * rng.choice([-1, 1], len(P))
    P2 = P + k
    ok = panel.within(P, P2) & panel.within(P, P2 + 60) & panel.within(P, P2 - 20)
    e2 = ev[ok].copy()
    e2["pos"] = P2[ok]
    return shape_table(panel, e2, "PS placebo (non-event days)", rng, reps)


# ---------------------------------------------------------------------------
# entry rules
# ---------------------------------------------------------------------------

def entry_rules(panel: pth.Panel, ev: pd.DataFrame, label: str,
                hold: int, rng, reps: int) -> pd.DataFrame:
    """Every rule scored on the SAME opportunity set and the SAME exit.

    Holding the exit fixed is what makes the comparison honest: with a common
    exit session, a rule's advantage over the benchmark is exactly the ratio of
    the two entry prices, and an unfilled order that is bought at the benchmark
    price instead contributes exactly zero. Nothing can hide in the horizon.
    """
    P = ev["pos"].to_numpy()
    first_fill = np.where(ev["filing_class"].to_numpy() == "INTRA", 1, 0)

    # B0: market at the first open that could possibly have been traded on the
    # information. Not the open after trade_date -- for the ~60% of filings that
    # arrive after the close, that is a full session late and forgoes the whole
    # day-zero reaction. It is scored below as R1, to price the lag.
    b0_px = np.full(len(ev), np.nan)
    b0_off = np.full(len(ev), np.nan)
    for rel in (0, 1):
        m = first_fill == rel
        px_, off_, _ = pth.fill_market(panel, P[m], rel)
        b0_px[m], b0_off[m] = px_, off_

    exit_idx = (P + hold).astype(np.int64)
    exit_ok = panel.within(P, exit_idx)
    exit_px = np.where(exit_ok, panel.adj_close[np.clip(exit_idx, 0, len(panel.cum) - 1)], np.nan)

    base_ok = np.isfinite(b0_px) & exit_ok
    sym = pd.factorize(ev["symbol"])[0]

    def score(name, px_, off_, filled):
        cost_b0 = pth.net_of_costs(b0_px, panel, P, b0_off)
        cost_r = pth.net_of_costs(px_, panel, P, off_)
        # Row C, the primary: unfilled orders buy at the benchmark instead, so
        # the opportunity set and the deployed capital are identical and the
        # only difference between the two portfolios is the entry price.
        entry_c = np.where(filled, cost_r, cost_b0)
        ok = base_ok & np.isfinite(entry_c)
        ret_c = exit_px[ok] / entry_c[ok] - 1
        ret_b0 = exit_px[ok] / cost_b0[ok] - 1
        adv = ret_c - ret_b0
        lo, hi = cluster_bootstrap_mean(adv, sym[ok], reps, rng)

        # The price edge on its own, with no cost model at all. This matters: a
        # market order on day zero is modelled as paying 0.15 of a news day's
        # high-low range, and a limit five sessions later pays 0.15 of a quiet
        # day's, so a chunk of any "advantage" below is the impact ASSUMPTION
        # rather than a measured price. Printing both makes that visible instead
        # of leaving it inside one number.
        raw_edge = np.where(filled, b0_px / px_ - 1, 0.0)
        edge = bp(np.nanmean(raw_edge[base_ok]))

        okA = base_ok & filled & np.isfinite(cost_r)
        ret_a = exit_px[okA] / cost_r[okA] - 1
        unf = base_ok & ~filled
        n_unf = int(unf.sum())
        # Lesson 6: an adverse-selection flag computed on twenty-six unfilled
        # orders is not a finding.
        if n_unf >= 50 and okA.sum() >= 50:
            ret_unfilled_b0 = exit_px[unf] / cost_b0[unf] - 1
            ret_filled_b0 = exit_px[okA] / cost_b0[okA] - 1
            regret = round(100 * float(np.nanmean(ret_unfilled_b0)), 2)
            filled_fwd = round(100 * float(np.nanmean(ret_filled_b0)), 2)
            sheds = "SHEDS WINNERS" if (regret - filled_fwd) > 5.0 else ""
        else:
            regret = filled_fwd = np.nan
            sheds = f"n_unfilled={n_unf}" if n_unf else ""

        # Break-even slippage: the extra cost per side at which the advantage
        # vanishes. A rule that dies at 5bp is not a rule.
        be = ">100"
        for s in (0, 5, 10, 15, 20, 30, 50, 100):
            e2 = np.where(filled, pth.net_of_costs(px_, panel, P, off_, s), cost_b0)
            a2 = (exit_px[ok] / e2[ok] - 1) - ret_b0
            if np.nanmean(a2) <= 0:
                be = str(s)
                break
        return {
            "rule": name, "n": int(ok.sum()),
            "fill_pct": round(100 * float(filled[base_ok].mean()), 1),
            "edge_bp_no_costs": edge,
            "advC_bp": bp(np.nanmean(adv)), "ci_lo_bp": bp(lo), "ci_hi_bp": bp(hi),
            "medC_bp": bp(np.nanmedian(adv)),
            "retC_mean_pct": round(100 * float(np.nanmean(ret_c)), 2),
            "retC_med_pct": round(100 * float(np.nanmedian(ret_c)), 2),
            "retC_p25_pct": round(100 * float(np.nanpercentile(ret_c, 25)), 2),
            "retA_filled_only_pct": round(100 * float(np.nanmean(ret_a)), 2) if okA.sum() else np.nan,
            "regret_unfilled_pct": regret, "filled_fwd_pct": filled_fwd,
            "sheds_winners": sheds, "breakeven_slip_bp": be,
        }

    rows = [score("B0 market @ first fillable open", b0_px, b0_off,
                  np.isfinite(b0_px))]

    px_, off_, fl = pth.fill_limit(panel, P, panel.adj_close[np.clip(P - 1, 0, len(panel.cum) - 1)],
                                   0, 1)
    rows.append(score("B0L limit @ prior close, 1 session", px_, off_, fl))

    px_, off_, fl = pth.fill_market(panel, P, 1)
    fl = fl & (first_fill == 0)
    px_ = np.where(fl, px_, np.nan)
    px2, off2, fl2 = pth.fill_market(panel, P, 2)
    take = (first_fill == 1)
    px_ = np.where(take, px2, px_)
    off_ = np.where(take, off2, off_)
    fl = np.where(take, fl2, fl)
    rows.append(score("R1 open of session AFTER B0 (= es.measure today)", px_, off_, fl))

    for name, rel in [("R2 close of d0+3", 3), ("R2b open of d0+4", 4),
                      ("R3 close of d0+5", 5), ("R3b open of d0+6", 6)]:
        idx = (P + rel).astype(np.int64)
        ok = panel.within(P, idx)
        arr = panel.adj_close if "close" in name else panel.adj_open
        px_ = np.where(ok, arr[np.clip(idx, 0, len(panel.cum) - 1)], np.nan)
        rows.append(score(name, px_, np.full(len(P), float(rel)), ok))

    for x in (0.02, 0.03, 0.05):
        px_, off_, fl = pth.fill_limit(panel, P, np.full(len(P), np.nan),
                                       1, 10, trail_high=True, pullback=x)
        rows.append(score(f"R{4 + int(x * 100) // 2} limit {int(x * 100)}% under running high, 10d",
                          px_, off_, fl))

    anchor = panel.adj_close[np.clip(P, 0, len(panel.cum) - 1)]
    px_, off_, fl = pth.fill_limit(panel, P, anchor, 1, 10)
    rows.append(score("R7 limit @ close(d0), 10 sessions", px_, off_, fl))

    cc = pth.window_matrix(panel, P, panel.df["simple_cc"].to_numpy(), 1, 10)
    down = cc < 0
    firstdown = np.where(down.any(axis=1), down.argmax(axis=1) + 1, -1)
    idx = (P + firstdown + 1).astype(np.int64)
    ok = (firstdown > 0) & panel.within(P, idx)
    px_ = np.where(ok, panel.adj_open[np.clip(idx, 0, len(panel.cum) - 1)], np.nan)
    rows.append(score("R8 market @ open after first down day", px_,
                      (firstdown + 1).astype(float), ok))

    t = pd.DataFrame(rows)
    t.insert(0, "sample", label)
    return t


def fill_probability_table(panel: pth.Panel, ev: pd.DataFrame) -> pd.DataFrame:
    """The consolation deliverable: a measured distribution, not a prediction.

    Survives a null intact, because it forecasts nothing. It answers the user's
    question -- "bid at what price" -- in the only form the evidence supports:
    here is how often a stock like this one came back to your bid, here is what
    you saved when it did, and here is what the ones that never came back went
    on to do.
    """
    P = ev["pos"].to_numpy()
    first_fill = np.where(ev["filing_class"].to_numpy() == "INTRA", 1, 0)
    b0 = np.full(len(ev), np.nan)
    for rel in (0, 1):
        m = first_fill == rel
        b0[m], _, _ = pth.fill_market(panel, P[m], rel)

    sig_t = pd.qcut(ev["sigma60"].rank(method="first"), 3, labels=[1, 2, 3])
    liq_t = pd.qcut(ev["normal_turnover_cr"].rank(method="first"), 3, labels=[1, 2, 3])
    hold = 126
    exit_idx = (P + hold).astype(np.int64)
    exit_ok = panel.within(P, exit_idx)
    exit_px = np.where(exit_ok, panel.adj_close[np.clip(exit_idx, 0, len(panel.cum) - 1)], np.nan)

    rows = []
    for x in (0.02, 0.05):
        limit = b0 * (1 - x)
        px_, off_, fl = pth.fill_limit(panel, P, limit, 0, 5)
        saved = np.where(fl, b0 / px_ - 1, np.nan)
        for s in (1, 2, 3):
            for l in (1, 2, 3):
                m = (sig_t == s).to_numpy() & (liq_t == l).to_numpy() & np.isfinite(b0)
                if m.sum() < 50:
                    continue
                miss = m & ~fl & exit_ok
                rows.append({
                    "bid_under_open": f"{int(x*100)}%", "sigma_tercile": s,
                    "liq_tercile": l, "n": int(m.sum()),
                    "fill_pct": round(100 * float(fl[m].mean()), 1),
                    "saved_when_filled_pct": round(100 * float(np.nanmean(saved[m])), 2),
                    "missed_fwd_6m_pct": round(100 * float(np.nanmean(exit_px[miss] / b0[miss] - 1)), 1)
                    if miss.sum() >= 20 else np.nan,
                })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--min-liq-cr", type=float, default=1.0)
    p.add_argument("--spacing", type=int, default=5)
    p.add_argument("--min-history", type=int, default=260)
    p.add_argument("--min-forward", type=int, default=62)
    p.add_argument("--shift-reps", type=int, default=400)
    p.add_argument("--diag-reps", type=int, default=200)
    p.add_argument("--boot-reps", type=int, default=2000)
    p.add_argument("--holdout", type=date.fromisoformat, default=date(2025, 9, 1))
    p.add_argument("--stage", default="all")
    args = p.parse_args()

    rng = np.random.default_rng(7)
    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 60)

    con = open_readonly()
    panel = pth.build_panel(con)

    ev = build_events(con, panel, args.min_liq_cr, args.spacing,
                      args.min_history, args.min_forward)
    ev = event_stats(panel, ev)

    # Quarantine the pilot. Roughly fifty split-half statistics were examined on
    # the full sample before this run; the amplitude result was DISCOVERED in
    # that search and cannot be published from the same data.
    rec({"block": "pilot", "statistics_examined": PILOT_STATS},
        {"note": "exploratory split-half search on the full sample; "
                 "pop_z z=3.82 discovered here, quarantined to a 2025-09+ holdout"},
        kind="exploratory_block")

    print(f"\nEVENT SET  n={len(ev):,}  companies={ev['symbol'].nunique():,}  "
          f"{pd.to_datetime(ev['d0']).min().date()} -> {pd.to_datetime(ev['d0']).max().date()}")
    print("\nby arrival class:")
    print(ev.groupby("filing_class").agg(
        n=("event_id", "size"), companies=("symbol", "nunique"),
        med_liq_cr=("normal_turnover_cr", "median")).to_string())
    print("\nby sign:")
    print(ev.groupby("sign").agg(
        n=("event_id", "size"), companies=("symbol", "nunique"),
        med_pop_bp=("pop", lambda s: bp(s.median())),
        med_cont_bp=("cont", lambda s: bp(s.median()))).to_string())
    print("\nby year:")
    print(ev.groupby("year").agg(n=("event_id", "size"),
                                 companies=("symbol", "nunique")).to_string())

    samples = {
        "E_broad": ev,
        "S_good": ev[ev["sign"] == "good"],
        "S_bad": ev[ev["sign"] == "bad"],
    }

    # Reconciliation with the published studies' own liquidity yardstick, using
    # their function rather than ours, so a drifted population is visible.
    liq = es.liquidity_profile(
        con, ev[["symbol", "d0"]].rename(columns={"d0": "signal_date"}),
        date_col="signal_date")
    print(f"\nes.liquidity_profile on the event set: Rs {liq} cr median trailing "
          f"turnover (the retracted 2026-09-08 result compared Rs 1.06cr events "
          f"against a Rs 6.81cr control)")

    # ================= T1b: the base rate the legs are measured against =======
    d = panel.df
    base = d.groupby(d["date"].dt.year).agg(
        overnight_bp=("loggap", lambda s: bp(s.mean())),
        intraday_bp=("logintra", lambda s: bp(s.mean())))
    print("\nT1b  UNCONDITIONAL DAILY LEGS, WHOLE ARCHIVE, NO NEWS (bp/session)")
    print(base.to_string())
    print("  This is what a rule earns for waiting, mechanically. Every abnormal")
    print("  number below has it removed, leg by leg, against a matched cohort.")

    # ================= T1: shape ============================================
    print("\nT1  ABNORMAL CAR PATH, anchored at close(d0-1), leg-matched, bp")
    shapes = []
    for name, s in samples.items():
        if len(s) < 200:
            print(f"  {name}: n={len(s)}, suppressed")
            continue
        t = shape_table(panel, s, name, rng, args.boot_reps)
        shapes.append(t)
        rec({"table": "T1_shape", "sample": name},
            {"n": len(s), "path_bp": t.to_dict("records")})
    for name in ("PRE", "POST", "INTRA"):
        s = ev[ev["filing_class"] == name]
        if len(s) < 200:
            continue
        t = shape_table(panel, s, f"E_broad {name}", rng, args.boot_reps)
        shapes.append(t)
        rec({"table": "T1_shape", "sample": f"E_broad::{name}"},
            {"n": len(s), "path_bp": t.to_dict("records")})
    liq_t = pd.qcut(ev["normal_turnover_cr"].rank(method="first"), 3, labels=[1, 2, 3])
    for q in (1, 3):
        s = ev[(liq_t == q).to_numpy()]
        t = shape_table(panel, s, f"E_broad liq-tercile {q}", rng, args.boot_reps)
        shapes.append(t)
        rec({"table": "T1_shape", "sample": f"E_broad::liq{q}"},
            {"n": len(s), "path_bp": t.to_dict("records")})

    ps = flat_path_placebo(panel, ev, rng, args.boot_reps)
    shapes.append(ps)
    rec({"table": "T1_shape", "sample": "PS_flat_path_placebo"},
        {"path_bp": ps.to_dict("records")})
    print(pd.concat(shapes, ignore_index=True).to_string(index=False))

    ps0 = ps[ps["rel"].isin([-20, -10, 0, 10, 20, 60])]
    flat = bool((ps0["mean_car_bp"].abs() < 120).all())
    print(f"\nPS GATE: placebo path {'flat' if flat else 'NOT FLAT'} "
          f"(max |mean CAR| at the checked days = "
          f"{ps0['mean_car_bp'].abs().max():.0f} bp). "
          f"{'Machinery passes.' if flat else 'STOP: machinery is broken.'}")

    # ================= persistence ==========================================
    gcode = pd.factorize(ev["nse_desc"].astype(str) + "|" + ev["quarter"])[0]
    he = half_labels(ev, "oddeven")
    hc = half_labels(ev, "chrono")

    print("\nT2c  POSITIVE CONTROL: inject a real per-company effect, see if we find it")
    pc = positive_control(ev, ev["cont_z"].to_numpy(), gcode, he,
                          (0.05, 0.10, 0.20, 0.40), rng)
    print(pc.to_string(index=False))
    rec({"table": "T2c_positive_control"}, {"curve": pc.to_dict("records")},
        kind="calibration")

    print(f"\nrunning P-SHIFT placebo: {args.shift_reps} full-pipeline replicates "
          f"on the primary, {args.diag_reps} on the rest. This is the slow part.")

    t2, nulls = [], {}
    for sample_name, s in (("E_broad", ev), ("S_good", samples["S_good"])):
        if len(s) < 400:
            print(f"  {sample_name}: n={len(s)}, persistence suppressed")
            continue
        g_s = pd.factorize(s["nse_desc"].astype(str) + "|" + s["quarter"])[0]
        for stat in ("cont_z", "cont"):
            for split in ("oddeven", "chrono"):
                h = half_labels(s, split)
                y = s[stat].to_numpy()
                if stat == "cont":
                    y = y * 1.0
                res = run_persistence(s, y, g_s, h)
                reps = (args.shift_reps if (sample_name == "E_broad" and
                                            stat == "cont_z" and split == "oddeven")
                        else args.diag_reps)
                null = placebo_shift(panel, s, "cont", h, reps, rng,
                                     standardise=(stat == "cont_z"))
                nulls[(sample_name, stat, split)] = null
                mu, sd = float(np.nanmean(null)), float(np.nanstd(null))
                z = (res.get("r", np.nan) - mu) / sd if sd > 0 else np.nan
                pemp = float(np.nanmean(np.abs(null - mu) >= abs(res.get("r", np.nan) - mu)))
                mde = 2.8 * sd
                row = {"sample": sample_name, "stat": stat, "split": split,
                       "k": res["k"], "r_raw": round(res.get("r_raw", np.nan), 4),
                       "r_resid": round(res.get("r", np.nan), 4),
                       "null_mean": round(mu, 4), "null_sd": round(sd, 4),
                       "reps": int(np.isfinite(null).sum()),
                       "z_vs_placebo": round(z, 2),
                       "p_emp": f"<{1/max(reps,1):.4f}" if pemp == 0 else round(pemp, 4),
                       "MDE_excess_r": round(mde, 3)}
                t2.append(row)
                rec({"table": "T2_persistence", "sample": sample_name,
                     "stat": stat, "split": split}, row)
                if sample_name == "E_broad" and stat == "cont_z" and split == "oddeven":
                    primary = res
                    primary_z = z

    T2 = pd.DataFrame(t2)
    print("\nT2  PERSISTENCE: does half 1 predict half 2, once the placebo is subtracted")
    print(T2.to_string(index=False))
    print("  r_resid is the primary: company theta residualised on same-half")
    print("  characteristics. MDE_excess_r is the smallest excess correlation this")
    print("  sample could have detected at 80% power. A null below it is a power")
    print("  failure, not evidence of absence.")

    # ================= reliability ==========================================
    print("\nT2e  RELIABILITY: can a half-sample company mean be measured at all")
    rel_rows = []
    for h_name, h in (("oddeven", he), ("chrono", hc)):
        f1 = _stage1(ev["cont_z"].to_numpy(), ev, gcode)
        sub = pd.DataFrame({"sym": ev["symbol"], "half": h, "f": f1,
                            "r": rng.random(len(ev))})
        sub = sub[np.isfinite(sub["f"]) & np.isfinite(sub["half"])]
        for half in (0, 1):
            q = sub[sub["half"] == half].copy()
            q["sub"] = (q.groupby("sym")["r"].rank(method="first") %
                        2).astype(int)
            a = q.groupby(["sym", "sub"])["f"].agg(["median", "size"]).reset_index()
            a = a[a["size"] >= 2]
            w = a.pivot(index="sym", columns="sub", values="median").dropna()
            r = pearson(w[0].to_numpy(), w[1].to_numpy()) if len(w) > 30 else np.nan
            sb = 2 * r / (1 + r) if np.isfinite(r) else np.nan
            rel_rows.append({"split": h_name, "half": half, "companies": len(w),
                             "r_within": round(r, 4),
                             "reliability_SB": round(sb, 4)})
    REL = pd.DataFrame(rel_rows)
    print(REL.to_string(index=False))
    rec({"table": "T2e_reliability"}, {"rows": REL.to_dict("records")},
        kind="calibration")
    rel_h = REL[REL["split"] == "oddeven"]["reliability_SB"].to_numpy()

    # ================= T2b: where the kill number is read ===================
    print("\nT2b  OUT-OF-SAMPLE QUINTILES -- sort companies on half-1 reaction,")
    print("     report what half 2 actually did. THE KILL NUMBER IS READ HERE.")
    QT = quintile_table(primary["wide"], rng, args.boot_reps)
    print(QT.to_string(index=False))
    spread = QT.attrs["spread_bp"]
    mono = QT.attrs["mono_rho"]
    print(f"  Q5 - Q1 = {spread:+.0f} bp   monotonicity rho = {mono:+.2f}   "
          f"gate = {KILL_BP:.0f} bp")
    rec({"table": "T2b_oos_quintiles"},
        {"spread_bp": spread, "mono_rho": mono, "rows": QT.to_dict("records")})

    # ================= composition audit ====================================
    # NSE collapsed every granular subtype into "Updates" for all of 2023 and
    # introduced the modern vocabulary around September 2024. A chronological
    # half-split therefore straddles a taxonomy change, and a null produced by
    # that rather than by absent persistence would be its own error. This is the
    # check that tells the two apart.
    comp = ev.assign(half=pd.Series(hc, index=ev.index))
    cm = comp.groupby(["symbol", "half", "nse_desc"]).size().rename("n").reset_index()
    cm["share"] = cm["n"] / cm.groupby(["symbol", "half"])["n"].transform("sum")
    print("\nT2d  COMPOSITION AUDIT: does a company file different things in half 2")
    cm2 = cm.pivot_table(index=["symbol", "nse_desc"], columns="half",
                         values="share", fill_value=0.0)
    if 0 in cm2.columns and 1 in cm2.columns:
        dist = (cm2[0] - cm2[1]).abs().groupby("symbol").sum()
        print(f"  median L1 distance between a company's half-1 and half-2 filing mix: "
              f"{dist.median():.2f} (0 = identical, 2 = disjoint)")
        wide_c = primary["wide"]
        dth = (wide_c[("theta", 0)] - wide_c[("theta", 1)]).abs()
        j = dth.index.intersection(dist.index)
        rho = spearman(dist.loc[j].to_numpy(), dth.loc[j].to_numpy())
        print(f"  rank correlation of mix drift with |theta_1 - theta_2|: {rho:+.3f} "
              f"(k={len(j)}). A near-zero chronological result caused by taxonomy")
        print("  drift rather than by absent persistence would show up here.")
        rec({"table": "T2d_composition"},
            {"median_L1": float(dist.median()), "rho": rho, "k": int(len(j))})

    # ================= T3 amplitude, quarantined ============================
    print("\nT3  AMPLITUDE (pop_z) -- QUARANTINED to the 2025-09+ holdout.")
    print("    Discovered in a ~50-statistic exploratory search, so it may not be")
    print("    published from the data it was found in. This is a filter and a")
    print("    sizing input -- 'does this company move on its own news at all' --")
    print("    and explicitly NOT a bid price.")
    hold_ev = ev[(pd.to_datetime(ev["d0"]).dt.date >= args.holdout) & ev["has_pop"]]
    if len(hold_ev) < 400:
        print(f"    holdout n={len(hold_ev)}, suppressed")
    else:
        g_h = pd.factorize(hold_ev["nse_desc"].astype(str) + "|" + hold_ev["quarter"])[0]
        h_h = half_labels(hold_ev, "oddeven")
        for stat in ("pop_z", "pop"):
            res = run_persistence(hold_ev, hold_ev[stat].to_numpy(), g_h, h_h,
                                  min_per_half=3)
            null = placebo_shift(panel, hold_ev, "pop", h_h, args.diag_reps, rng,
                                 standardise=(stat == "pop_z"))
            mu, sd = float(np.nanmean(null)), float(np.nanstd(null))
            z = (res.get("r", np.nan) - mu) / sd if sd > 0 else np.nan
            row = {"stat": stat, "k": res["k"], "r_resid": round(res.get("r", np.nan), 4),
                   "r_raw": round(res.get("r_raw", np.nan), 4),
                   "null_mean": round(mu, 4), "null_sd": round(sd, 4),
                   "z_vs_placebo": round(z, 2)}
            print("   ", row)
            rec({"table": "T3_amplitude_holdout", "stat": stat}, row)

    # ================= T4 characteristics ===================================
    print("\nT4  CHARACTERISTICS -- do stocks LIKE THIS fade, even if this company")
    print("    does not? Quintiles formed within each event's own calendar month,")
    print("    fitted on 2022-01..2024-12, applied unchanged to 2025-01..2026-06.")
    CH = ["ret_6m", "ret_12m", "pos_52w", "sigma60", "normal_turnover_cr",
          "band_headroom", "turnover_trend"]
    ev = ev.copy()
    ev["hot"] = (ev.groupby("month")["ret_6m"].rank(pct=True) +
                 ev.groupby("month")["pos_52w"].rank(pct=True) +
                 ev.groupby("month")["sigma60"].rank(pct=True)) / 3
    train = pd.to_datetime(ev["d0"]) < "2025-01-01"
    rows = []
    for c in CH + ["hot"]:
        q = ev.groupby("month")[c].rank(pct=True, method="first")
        qq = np.clip(np.ceil(q * 5), 1, 5)
        tr, ho = [], []
        for m, lab in ((train, "train"), (~train, "holdout")):
            sub = ev[m]
            v = qq[m]
            a = sub.loc[(v == 5).to_numpy(), "cont"]
            b = sub.loc[(v == 1).to_numpy(), "cont"]
            (tr if lab == "train" else ho).append(
                (bp(a.mean()) - bp(b.mean()), len(a), len(b)))
        d_tr, d_ho = tr[0][0], ho[0][0]
        per_year = []
        for y in sorted(ev["year"].unique()):
            s = ev[ev["year"] == y]
            v = qq[ev["year"] == y]
            if (v == 5).sum() < 50 or (v == 1).sum() < 50:
                continue
            per_year.append(np.sign(bp(s.loc[(v == 5).to_numpy(), "cont"].mean()) -
                                    bp(s.loc[(v == 1).to_numpy(), "cont"].mean())))
        sub = ev[~train]
        v = qq[~train]
        adv = (sub.loc[(v == 5).to_numpy(), "cont"].to_numpy())
        lo, hi = cluster_bootstrap_mean(
            np.concatenate([adv, -sub.loc[(v == 1).to_numpy(), "cont"].to_numpy()]),
            pd.factorize(pd.concat([sub.loc[(v == 5).to_numpy(), "symbol"],
                                    sub.loc[(v == 1).to_numpy(), "symbol"]]))[0],
            args.boot_reps, rng)
        row = {"characteristic": c, "train_Q5_Q1_bp": d_tr, "holdout_Q5_Q1_bp": d_ho,
               "retention_pct": round(100 * d_ho / d_tr, 0) if d_tr else np.nan,
               "sign_holds": f"{int(np.sum(np.array(per_year) == np.sign(d_tr)))}/{len(per_year)} yrs",
               "ci_lo_bp": bp(2 * lo), "ci_hi_bp": bp(2 * hi),
               "passes_150bp": "PASS" if (d_ho >= KILL_BP and np.sign(d_ho) == np.sign(d_tr))
                               or (d_ho <= -KILL_BP and np.sign(d_ho) == np.sign(d_tr)) else "fail"}
        rows.append(row)
        rec({"table": "T4_characteristic", "characteristic": c}, row)
    T4 = pd.DataFrame(rows)
    print(T4.to_string(index=False))
    best_char = T4["holdout_Q5_Q1_bp"].abs().max()

    # ================= T6 microstructure mediation ==========================
    print("\nT6  MICROSTRUCTURE MEDIATION -- each panel can void a positive result")
    med = []
    for name, m in (("all", np.ones(len(ev), bool)),
                    ("band-hit on d0", ev["band_hit"].to_numpy().astype(bool)),
                    ("no band-hit", ~ev["band_hit"].to_numpy().astype(bool)),
                    ("locked in [0,10]", ev["locked_any"].to_numpy()),
                    ("became BE within 20d", ev["became_BE_20"].to_numpy()),
                    ("series BE on d0", (ev["series_d0"] == "BE").to_numpy()),
                    ("PRE+POST", ev["has_pop"].to_numpy()),
                    ("INTRA", ~ev["has_pop"].to_numpy()),
                    ("CLEAN (no band/lock/BE)",
                     (~ev["band_hit"].to_numpy().astype(bool)) & (~ev["locked_any"].to_numpy())
                     & (~ev["became_BE_20"].to_numpy()) & (ev["series_d0"] == "EQ").to_numpy())):
        s = ev[m]
        if len(s) < 50:
            med.append({"subset": name, "n": len(s), "note": "n<50, suppressed"})
            continue
        med.append({"subset": name, "n": len(s),
                    "pop_bp": bp(s["pop"].mean()), "cont_bp": bp(s["cont"].mean()),
                    "cont_med_bp": bp(s["cont"].median()),
                    "drift20_bp": bp(s["drift20"].mean()),
                    "drift60_bp": bp(s["drift60"].mean())})
    print(pd.DataFrame(med).to_string(index=False))

    # ================= T5 entry rules =======================================
    print("\nT5  ENTRY RULES. Row C accounting: an order that never filled buys at")
    print("    the benchmark price instead, so every rule is scored on the SAME")
    print("    opportunity set with the SAME capital deployed and the SAME exit")
    print("    (close of d0+130). advC_bp is therefore exactly the entry-price")
    print("    edge, net of 40bp/side plus 0.15 x the entry session's own range.")
    rules = []
    for name, s in (("E_broad", ev), ("S_good", samples["S_good"])):
        s = s[panel.last[s["pos"].to_numpy()] - s["pos"].to_numpy() >= 130]
        if len(s) < 300:
            print(f"  {name}: n={len(s)} with 130 forward sessions, suppressed")
            continue
        t = entry_rules(panel, s, name, 130, rng, args.boot_reps)
        rules.append(t)
        for r in t.to_dict("records"):
            rec({"table": "T5_entry_rule", "sample": name, "rule": r["rule"]}, r)
    T5 = pd.concat(rules, ignore_index=True) if rules else pd.DataFrame()
    if not T5.empty:
        print(T5.to_string(index=False))

    # ================= T5b rule placebo =====================================
    print("\nT5b RULE PLACEBO -- the same rules on matched NON-event days. 'Wait for")
    print("    a 3% dip' is a generic short-horizon mean-reversion bet that has")
    print("    nothing to do with news. If the advantage survives here, the")
    print("    feature is dead even if the rule makes money.")
    P = ev["pos"].to_numpy()
    k = rng.integers(56, 261, len(P)) * rng.choice([-1, 1], len(P))
    P2 = P + k
    okp = panel.within(P, P2) & panel.within(P, P2 + 140) & panel.within(P, P2 - 11)
    e2 = ev[okp].copy()
    e2["pos"] = P2[okp]
    T5P = entry_rules(panel, e2, "NON-EVENT placebo", 130, rng, args.boot_reps)
    print(T5P.to_string(index=False))
    for r in T5P.to_dict("records"):
        rec({"table": "T5b_rule_placebo", "rule": r["rule"]}, r)

    # G6: how much of each rule's edge is generic short-horizon mean reversion
    # pointed at filings rather than anything to do with news.
    if not T5.empty:
        pl = T5P.set_index("rule")["advC_bp"]
        T5 = T5.copy()
        T5["placebo_advC_bp"] = T5["rule"].map(pl)
        T5["news_only_bp"] = (T5["advC_bp"] - T5["placebo_advC_bp"]).round(1)
        T5["placebo_retention_pct"] = np.where(
            T5["advC_bp"].abs() > 1e-9,
            (100 * T5["placebo_advC_bp"] / T5["advC_bp"]).round(0), np.nan)
        T5["G6"] = np.where(T5["placebo_retention_pct"] >= 50, "NOT NEWS", "news-specific")
        print("\nT5c RULE EDGE, NET OF THE SAME RULE ON NON-EVENT DAYS")
        print(T5[["sample", "rule", "advC_bp", "placebo_advC_bp", "news_only_bp",
                  "placebo_retention_pct", "G6", "fill_pct",
                  "regret_unfilled_pct", "sheds_winners"]].to_string(index=False))

    # ================= the consolation deliverable ==========================
    print("\nFILL-PROBABILITY CALCULATOR (ships whatever the verdict, forecasts nothing)")
    FP = fill_probability_table(panel, samples["S_good"] if len(samples["S_good"]) > 500 else ev)
    print(FP.to_string(index=False))

    # ================= verdict ==============================================
    lifetime = PRIOR_TRIALS + PILOT_STATS + trials.count(STUDY)
    g1 = "FIRES" if not (np.isfinite(primary_z) and primary_z >= G1_Z) else "clear"
    g2 = "FIRES" if abs(mono) < 1.0 else "clear"
    g3 = "FIRES" if (np.nanmin(rel_h) < 0.15) else "clear"
    chrono_row = T2[(T2["sample"] == "E_broad") & (T2["stat"] == "cont_z") &
                    (T2["split"] == "chrono")]
    zc = float(chrono_row["z_vs_placebo"].iloc[0]) if len(chrono_row) else np.nan
    g4 = "FIRES" if not (np.isfinite(zc) and zc >= 2.0) else "clear"
    best_rule = T5[T5["rule"] != "B0 market @ first fillable open"] if not T5.empty else pd.DataFrame()
    if not best_rule.empty:
        # Ranked on the NEWS-ONLY edge, not the raw one: a rule whose advantage
        # is reproduced on non-event days has not been ranked out of the running
        # by accident, it has been ranked out on purpose.
        b = best_rule.loc[best_rule["news_only_bp"].idxmax()]
        g5 = "FIRES" if (b["advC_bp"] < 100 or b["ci_lo_bp"] <= 0 or b["fill_pct"] < 60) else "clear"
        g6 = "FIRES" if (b["placebo_retention_pct"] >= 50) else "clear"
    else:
        b, g5, g6 = None, "FIRES", "FIRES"
    verdict = "KILL" if (g1 == "FIRES" or abs(spread) < KILL_BP) else "PASS"

    print("\n" + "=" * 100)
    print(f"VERDICT: {verdict} -- per-company news-reaction timing")
    print("=" * 100)
    print(f"  out-of-sample Q5-Q1 spread   {spread:+8.0f} bp   against a {KILL_BP:.0f} bp gate")
    print(f"  G1 placebo-adjusted z        {primary_z:+8.2f}      against a {G1_Z:.1f} gate     [{g1}]")
    print(f"  reliability half1/half2      {rel_h[0]:8.3f} / {rel_h[1]:.3f}   floor 0.15   [{g3}]")
    rdis = primary.get("r", np.nan) / np.sqrt(max(rel_h[0] * rel_h[1], 1e-9))
    print(f"  rho residualised             {primary.get('r', float('nan')):+8.4f}   disattenuated {rdis:+.3f}")
    if b is not None:
        print(f"  best rule vs B0, row C       {b['advC_bp']:+8.0f} bp  [{b['sample']}: {b['rule']}]")
        print(f"    of which reproduces on non-event days {b['placebo_advC_bp']:+.0f} bp "
              f"({b['placebo_retention_pct']:.0f}%), leaving {b['news_only_bp']:+.0f} bp "
              f"attributable to the news   [G5 {g5} / G6 {g6}]")
        print(f"    fill {b['fill_pct']:.1f}%  CI [{b['ci_lo_bp']:.0f}, {b['ci_hi_bp']:.0f}] bp  "
              f"break-even slippage {b['breakeven_slip_bp']} bp/side  {b['sheds_winners']}")
    print(f"  best characteristic, holdout {best_char:+8.0f} bp   against the same {KILL_BP:.0f} bp gate")
    print(f"  lifetime hypothesis count    {lifetime:8d}      "
          f"(39 prior + {PILOT_STATS} pilot + {trials.count(STUDY)} here); "
          f"Sidak at 0.05 needs |z| >= 3.5")
    print(f"  gates: G1 {g1} | G2 monotonicity {g2} | G3 measurability {g3} | "
          f"G4 non-stationarity {g4} | G5 money {g5} | G6 not-news {g6}")
    print("\n  READ THE VERDICT NARROWLY. It is about PER-COMPANY timing -- whether a")
    print("  company's own history tells you where to bid. The unconditional")
    print("  question, 'does waiting beat buying at the first fillable open for")
    print("  everybody', is a different claim and is answered in T5/T5c, not here.")

    policy = {
        "rule": "open_t0" if verdict == "KILL" else "see_write_up",
        "reason": (f"no conditioning variable cleared the {KILL_BP:.0f} bp gate; "
                   f"OOS company spread {spread:+.0f} bp, best characteristic "
                   f"{best_char:+.0f} bp, G1 z={primary_z:+.2f}")
        if verdict == "KILL" else "see research/studies/news_reaction_entry.md",
        "measured_on": str(date.today()),
        "events": int(len(ev)), "companies": int(ev["symbol"].nunique()),
        "lifetime_trials": lifetime,
        "per_company_timing": {"oos_quintile_spread_bp": spread,
                               "monotonicity_rho": mono, "G1_z": round(float(primary_z), 2),
                               "verdict": verdict},
        "unconditional_entry": None if b is None else {
            "sample": b["sample"], "rule": b["rule"],
            "advantage_vs_B0_bp": float(b["advC_bp"]),
            "reproduced_on_non_event_days_bp": float(b["placebo_advC_bp"]),
            "news_only_bp": float(b["news_only_bp"]),
            "fill_pct": float(b["fill_pct"]),
            "ci_bp": [float(b["ci_lo_bp"]), float(b["ci_hi_bp"])],
            "note": "a claim about EVERY good-news event, not about this company; "
                    "amplitude and timing are separate and must stay separate"},
        "fill_probability_table": FP.to_dict("records"),
    }
    (ROOT / "state").mkdir(exist_ok=True)
    (ROOT / "state" / "entry_policy.json").write_text(json.dumps(policy, indent=2))
    print("\nwrote state/entry_policy.json -- the null policy ships too, it is what")
    print("stops the next person re-inventing this.")

    outdir = ROOT / "data" / "study"
    outdir.mkdir(parents=True, exist_ok=True)
    keep_cols = [c for c in ev.columns if ev[c].dtype != object or c in
                 ("symbol", "nse_desc", "filing_class", "sign", "series_d0",
                  "category", "quarter", "month")]
    ev[keep_cols].to_parquet(outdir / "news_events.parquet", index=False)
    primary["wide"].to_parquet(outdir / "theta.parquet")
    print(f"froze {len(ev):,} events to data/study/news_events.parquet")
    print(f"trials recorded for this study: {trials.count(STUDY)}  "
          f"(lifetime across all studies: {trials.lifetime()})")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
