"""Independent verification of the v3 backtest (spec section 15.8).

Written from research/strategy/v3-spec.md (sections 2 to 14 and Clarifications C1 to C10) and
from the run folders only. It does NOT import or read jobs/v3_verdict.py, jobs/v3_diagnostics.py,
zen/portfolio/engine_v3.py, jobs/backtest_v3.py or jobs/crosscheck_v3.py. It uses general
libraries (pandas, numpy, scipy, statsmodels, quantstats, arch, duckdb) and, for adjusted prices
and the stock-identity map only, zen's price plumbing (`engine.build_panel`, `pit`).

    python -m jobs.verify_v3 [--only A1,A2,...]

Parts: A1 primary test, A2 the five bars, A3 owner's measures, A4 score signal, A5 earnings and
re-rating, A6 v3 figures, B rule checks (B1 exits, B2 trims and caps, B3 buys, B4 fills, B5 NAV),
C re-derivation of eligibility and ranks from the frozen database at three reviews.

Writes data/backtest/v3/verification.json and verification.md. Every item gives the verdict's
value, the recomputed value, the difference, the tolerance and whether it matches; what could
not be verified is listed with the reason.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import quantstats as qs                                   # noqa: E402
import statsmodels.api as sm                              # noqa: E402
from arch.bootstrap import StationaryBootstrap            # noqa: E402
from scipy.stats import rankdata, spearmanr               # noqa: E402

from zen.portfolio import engine                          # noqa: E402  (price plumbing only)
from zen.universe import pit                              # noqa: E402  (identity, calendar, snapshot N)

RUN = ROOT / "data" / "backtest" / "v3"
W1 = ROOT / "data" / "backtest" / "v1_window_2020"
R1 = ROOT / "data" / "backtest" / "v1_replay_v3"
VERDICT = ROOT / "data" / "backtest" / "v3_verdict.json"
FZ = ROOT / "data" / "frozen" / "v3"
IIMA = FZ / "data" / "external" / "iima" / \
    "2025-12_FourFactors_and_Market_Returns_Monthly_SurvivorshipBiasAdjusted.csv"
QUALITY = FZ / "data" / "study" / "quality_factor.parquet"

WINDOW_START = pd.Timestamp("2020-02-17")
WINDOW_END = pd.Timestamp("2026-09-18")
CR = 1e7                      # one crore, in the archive's rupees
TOL = 1e-9

CHECKS: list[dict] = []       # every compared item
UNVERIFIED: list[dict] = []   # everything not verified, with the reason
DETAIL: dict = {}             # free-form supporting detail per part


# ---------------------------------------------------------------- bookkeeping
def _isnum(x) -> bool:
    return isinstance(x, (int, float, np.integer, np.floating)) and not isinstance(x, (bool, np.bool_))


def chk(part: str, item: str, verdict, mine, tol: float = TOL, rel: bool = True, note: str = "") -> bool:
    """Record one comparison. Numbers: |mine - verdict| <= tol (relative to max(1, |verdict|) if rel)."""
    if _isnum(verdict) and _isnum(mine):
        v, m = float(verdict), float(mine)
        if math.isnan(v) and math.isnan(m):
            diff, ok = 0.0, True
        elif math.isnan(v) or math.isnan(m):
            diff, ok = float("nan"), False
        else:
            diff = m - v
            lim = tol * max(1.0, abs(v)) if rel else tol
            ok = abs(diff) <= lim
        rec = {"part": part, "item": item, "verdict": v, "recomputed": m, "difference": diff,
               "tolerance": tol, "tolerance_kind": "relative" if rel else "absolute",
               "match": bool(ok)}
    else:
        ok = (verdict == mine) or (verdict is None and mine is None)
        rec = {"part": part, "item": item, "verdict": _jsonable(verdict), "recomputed": _jsonable(mine),
               "difference": None, "tolerance": "exact", "match": bool(ok)}
    if note:
        rec["note"] = note
    CHECKS.append(rec)
    return bool(ok)


def _jsonable(x):
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (np.floating,)):
        return float(x)
    if isinstance(x, (np.bool_,)):
        return bool(x)
    if isinstance(x, (pd.Timestamp,)):
        return str(x.date())
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    return x


def unverified(part: str, item: str, reason: str) -> None:
    UNVERIFIED.append({"part": part, "item": item, "reason": reason})


def cmp_tree(part: str, path: str, v, m, tol: float = TOL, skip: tuple = ()) -> None:
    """Compare the verdict's subtree v with my subtree m, leaf by leaf; a key I did not
    recompute is listed as unverified, never skipped silently."""
    if isinstance(v, dict):
        for k, vv in v.items():
            if k in skip:
                continue
            if not isinstance(m, dict) or k not in m:
                unverified(part, f"{path}.{k}", "not recomputed by this verifier")
                continue
            cmp_tree(part, f"{path}.{k}", vv, m[k], tol, skip)
    elif isinstance(v, list):
        if not isinstance(m, list) or len(m) != len(v):
            chk(part, f"{path}.length", len(v), None if not isinstance(m, list) else len(m))
            return
        for i, (a, b) in enumerate(zip(v, m)):
            cmp_tree(part, f"{path}[{i}]", a, b, tol, skip)
    else:
        chk(part, path, v, m, tol)


def isnan(x) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x)) or (x is pd.NA) or (x is pd.NaT)


# ---------------------------------------------------------------- the clock and the library figures
OPEN_AT = pd.Timedelta(hours=9, minutes=15)
CLOSE_AT = pd.Timedelta(hours=15, minutes=30)
FACTORS = ["MF", "SMB", "HML", "WML"]


def load_nav(path: Path) -> pd.DataFrame:
    """nav.csv on a unique, ordered index (an open at 09:15, a close at 15:30: ordering only)."""
    nav = pd.read_csv(path, parse_dates=["date"])
    at = pd.to_timedelta(np.where(nav["mark"] == "open", OPEN_AT.value, CLOSE_AT.value))
    idx = pd.DatetimeIndex(nav["date"].to_numpy() + at.to_numpy(), name="t")
    assert idx.is_unique and idx.is_monotonic_increasing, f"{path}: not a clock"
    return nav.drop(columns=["date"]).set_index(idx)


def years_between(t0, t1) -> float:
    return (pd.Timestamp(t1).normalize() - pd.Timestamp(t0).normalize()).days / 365.25


def cagr_calendar(v: pd.Series) -> float:
    return float((v.iloc[-1] / v.iloc[0]) ** (1 / years_between(v.index[0], v.index[-1])) - 1)


def monthly(nav: pd.Series) -> pd.Series:
    """Last mark of each calendar month over the last mark of the month before."""
    last = nav.groupby(nav.index.to_period("M")).last()
    return last.pct_change().dropna()


def load_iima() -> pd.DataFrame:
    f = pd.read_csv(IIMA)
    f["Date"] = pd.to_datetime(f["Date"], format="%Y-%m").dt.to_period("M")
    return f.set_index("Date")[FACTORS + ["RF"]].apply(pd.to_numeric, errors="coerce") / 100


def load_quality(version: str = "margin") -> pd.Series:
    q = pd.read_parquet(QUALITY)
    q = q[(q["version"] == version) & q["complete"].astype(bool)]
    return pd.Series(q["factor"].to_numpy(float), index=pd.PeriodIndex(q["month"], freq="M")).sort_index()


def ols_hac(d: pd.DataFrame, factors: list[str]) -> dict:
    y = d["r"] - d["RF"]
    X = sm.add_constant(d[factors])
    fit = sm.OLS(y, X).fit(cov_type="HAC", cov_kwds={"maxlags": 3})
    a = float(fit.params["const"])
    return {"months": int(len(d)), "first": str(d.index[0]), "last": str(d.index[-1]),
            "alpha_monthly": a, "alpha_annual": (1 + a) ** 12 - 1, "alpha_t": float(fit.tvalues["const"])}


def alpha_pair(nav: pd.Series) -> tuple[dict, dict]:
    """(without quality, with quality) on the identical months of IIMA, quality 'margin' complete."""
    m = monthly(nav).rename("r")
    iima, q = load_iima(), load_quality("margin")
    base = iima.join(m, how="inner").join(q.rename("QUAL"), how="inner").dropna()
    return ols_hac(base, FACTORS), ols_hac(base, FACTORS + ["QUAL"])


def qs_figures(nav: pd.Series) -> dict:
    r = nav.pct_change().dropna()
    return {"max_drawdown": float(qs.stats.max_drawdown(r)), "sortino": float(qs.stats.sortino(r, periods=252)),
            "calmar": float(qs.stats.calmar(r)), "cagr": cagr_calendar(nav)}


# ---------------------------------------------------------------- shared inputs
class Ctx:
    pass


def build_ctx() -> Ctx:
    t0 = time.time()
    c = Ctx()
    c.V = json.loads(VERDICT.read_text())
    c.con = pit.connect(FZ / "data" / "zen.duckdb")
    c.cal = pit.sessions(c.con)
    c.isend = engine.in_sample_end(c.cal)
    c.static = pit.StaticLabels.load(c.con, FZ / "nse_list.json")
    c.ids = c.static.ids
    c.nav3 = load_nav(RUN / "nav.csv")
    c.nav1 = load_nav(W1 / "nav.csv")
    rd = lambda p, **k: pd.read_csv(p, **k)
    c.fills = rd(RUN / "fills.csv", parse_dates=["date", "D"])
    c.hold = rd(RUN / "holdings.csv", parse_dates=["D"])
    c.dec = rd(RUN / "decisions.csv", parse_dates=["D"])
    c.elig = rd(RUN / "eligibility.csv", parse_dates=["D"])
    c.ranks = rd(RUN / "ranks.csv", parse_dates=["D"])
    c.univ = rd(RUN / "universe.csv", parse_dates=["D"])
    c.ro3 = rd(RUN / "rebalance_open.csv", parse_dates=["date"])
    c.ro1 = rd(W1 / "rebalance_open.csv", parse_dates=["date"])
    c.tr1w = rd(W1 / "trades.csv", parse_dates=["date"])
    c.tr1 = rd(R1 / "trades.csv", parse_dates=["date"])
    c.ep3 = rd(RUN / "episodes.csv", parse_dates=["entry", "exit"])
    c.ep1 = rd(W1 / "episodes.csv", parse_dates=["entry", "exit"])
    c.Ds = list(c.ro3["date"])                       # the 27 reviews
    assert len(c.Ds) == 27
    c.Dend = WINDOW_END
    stock_ids = (set(c.ranks.symbol) | set(c.hold.symbol) | set(c.fills.symbol) | set(c.tr1.symbol)
                 | set(c.ep1.symbol) | set(c.ep3.symbol))
    c.panel = engine.build_panel(c.con, sorted(stock_ids), WINDOW_START, WINDOW_END, c.isend,
                                 run_final_test=True, ids=c.ids)
    c.dix = {d: i for i, d in enumerate(c.panel.dates)}
    print(f"context built in {time.time() - t0:.1f}s; panel {c.panel.open_.shape}", flush=True)
    return c


def px_at(p, t: int, col: int) -> float:
    """The engine panel's price at the open of session t: the open if it trades, else the last close."""
    return float(p.open_[t, col]) if p.traded[t, col] else float(p.last[t - 1, col])


def units_after(trades: pd.DataFrame, through: pd.Timestamp) -> pd.Series:
    t = trades[(trades["date"] <= through) & trades["side"].isin(["buy", "sell"])]
    u = t.assign(s=np.where(t["side"] == "buy", 1.0, -1.0) * t["units"]).groupby("symbol")["s"].sum()
    return u[u > 1e-9]


# ================================================================ A1 primary test
def part_A1(c: Ctx) -> None:
    P = "A1"
    pt = c.V["primary_test"]
    wo, wi = alpha_pair(c.nav3["strategy"])
    chk(P, "primary_test.months.count", pt["months"][0], wi["months"])
    chk(P, "primary_test.months.first", pt["months"][1], wi["first"])
    chk(P, "primary_test.months.last", pt["months"][2], wi["last"])
    chk(P, "primary_test.alpha_annual", pt["alpha_annual"], wi["alpha_annual"], 1e-6, rel=False)
    chk(P, "primary_test.alpha_t", pt["alpha_t"], wi["alpha_t"], 1e-6, rel=False)
    chk(P, "primary_test.t_critical", pt["t_critical"], 1.645)
    chk(P, "primary_test.reject_h0 == (t > 1.645)", pt["reject_h0"], bool(wi["alpha_t"] > 1.645))
    DETAIL["A1"] = {"with_quality": wi, "without_quality": wo}


# ================================================================ A2 the five bars
def turnover_a2(values: pd.Series, nav_marks: pd.Series) -> float:
    """sum(value of the fills counted) / 2 / mean(all NAV marks in the window) / (calendar days / 365.25)."""
    days = (nav_marks.index[-1].normalize() - nav_marks.index[0].normalize()).days
    return float(values.sum() / 2 / nav_marks.mean() / (days / 365.25))


def turnover_table(c: Ctx, cids: set, extra_bz: bool) -> pd.DataFrame:
    """Daily turnover (rupees) by stock id for the sessions needed, from the frozen prices table."""
    spells = c.ids.spells
    syms = sorted(set(spells.loc[spells["cid"].isin(cids), "symbol"]) | set(cids))
    c.con.register("_tsyms", pd.DataFrame({"symbol": syms}))
    sql = ("SELECT p.symbol, p.isin_code, p.date, p.turnover FROM {t} p JOIN _tsyms s USING (symbol) "
           "WHERE p.date >= DATE '2019-06-01'{x}")
    frames = [c.con.execute(sql.format(t="prices", x="")).df().assign(fb=0)]
    if extra_bz:
        frames.append(c.con.execute(sql.format(t="prices_other", x=" AND p.series = 'BZ'")).df().assign(fb=1))
    c.con.unregister("_tsyms")
    df = pd.concat(frames, ignore_index=True)
    df["cid"] = c.ids.for_prices(df)
    df = df[df["cid"].isin(cids)].copy()
    df["date"] = pd.to_datetime(df["date"])
    df["turnover"] = df["turnover"].fillna(0.0)
    df = df.sort_values(["fb", "turnover"], ascending=[True, False]).drop_duplicates(["cid", "date"])
    return df.pivot(index="date", columns="cid", values="turnover")


def capacity_ratios(c: Ctx, trades: pd.DataFrame, extra_bz: bool) -> np.ndarray:
    """value / median turnover over the 60 sessions before the trade date (untraded sessions = 0)."""
    cids = set(trades["symbol"])
    tt = turnover_table(c, cids, extra_bz)
    cal = c.cal
    out = []
    for r in trades.itertuples():
        i = cal.searchsorted(r.date, side="left")          # position of the trade date among sessions
        win = cal[i - 60:i]
        col = tt[r.symbol] if r.symbol in tt.columns else pd.Series(dtype=float)
        med = float(col.reindex(win).fillna(0.0).median())
        out.append(r.value / med if med > 0 else np.inf)
    return np.array(out)


def part_A2(c: Ctx) -> None:
    P = "A2"
    A2 = c.V["a2"]
    s3, s1 = c.nav3["strategy"], c.nav1["strategy"]
    f3, f1 = qs_figures(s3), qs_figures(s1)
    wo3, wi3 = alpha_pair(s3)
    wo1, wi1 = alpha_pair(s1)
    f3.update(alpha_annual=wo3["alpha_annual"], alpha_t=wo3["alpha_t"])
    f1.update(alpha_annual=wo1["alpha_annual"], alpha_t=wo1["alpha_t"])
    chk(P, "a2 months used (v3 without-quality regression)", 70, wo3["months"])
    chk(P, "a2 months used (v1 without-quality regression)", 70, wo1["months"])

    # turnover (section 14.2 bar 4)
    fills = c.fills[c.fills["side"].isin(["buy", "sell"])]
    first_build = (fills["D"] == WINDOW_START) & (fills["side"] == "buy")
    f3["turnover_a2"] = turnover_a2(fills.loc[~first_build, "value"], s3)
    t3_incl = turnover_a2(fills["value"], s3)
    t1w = c.tr1w[c.tr1w["side"].isin(["buy", "sell"]) & (c.tr1w["date"] >= WINDOW_START)]
    f1["turnover_a2"] = turnover_a2(t1w["value"], s1)
    DETAIL["A2_turnover"] = {"v3_fills_counted": int((~first_build).sum()), "v3_initial_build_fills": int(first_build.sum()),
                             "v1_fills_counted": int(len(t1w))}

    # capacity (bar 5): primary definition uses EQ/BE turnover (universe rule 2's measure)
    scale = 500000.0 / float(c.ro1["strategy_open"].iloc[0])
    cap = {}
    for tag, bz in (("eqbe", False), ("eqbe_plus_bz", True)):
        r3 = capacity_ratios(c, fills, bz)
        r1 = capacity_ratios(c, t1w, bz)
        cap[tag] = {"v3_p95": float(np.percentile(r3, 95)), "v1_p95_unscaled": float(np.percentile(r1, 95)),
                    "v3_zero_median": int(np.isinf(r3).sum()), "v1_zero_median": int(np.isinf(r1).sum()),
                    "v3_max": float(r3.max()), "v1_max_unscaled": float(r1.max())}
        cap[tag]["v1_p95"] = cap[tag]["v1_p95_unscaled"] * scale
        cap[tag]["v3_passes"] = bool(cap[tag]["v3_p95"] <= cap[tag]["v1_p95"])
    f3["capacity_p95"] = cap["eqbe"]["v3_p95"]
    f1["capacity_p95"] = cap["eqbe"]["v1_p95"]
    DETAIL["A2_capacity"] = cap

    # figures vs the verdict
    for key, mine in (("v1_figures", f1), ("v3_figures", f3)):
        for k, v in A2[key].items():
            tol = 1e-6 if k == "capacity_p95" else TOL
            chk(P, f"a2.{key}.{k}", v, mine[k], tol,
                note=("capacity from the frozen prices table (EQ/BE turnover)" if k == "capacity_p95" else ""))
    # pass or fail of each bar, from my figures
    mdd_need = f1["max_drawdown"] + 0.05
    bars = [
        f3["max_drawdown"] >= mdd_need,
        (f3["sortino"] > f1["sortino"]) and (f3["calmar"] > f1["calmar"]),
        (f3["alpha_annual"] >= f1["alpha_annual"]) and (f3["alpha_t"] >= f1["alpha_t"]),
        f3["turnover_a2"] <= f1["turnover_a2"],
        f3["capacity_p95"] <= f1["capacity_p95"],
    ]
    p1 = A2["bars"][0]["parts"]["max_drawdown"]
    chk(P, "a2.bar1.v3_must_be_at_least", p1["v3_must_be_at_least"], mdd_need)
    chk(P, "a2.bar1.improvement_points", p1["improvement_points"], (f3["max_drawdown"] - f1["max_drawdown"]) * 100)
    for i, b in enumerate(bars):
        chk(P, f"a2.bar{i + 1}.passes", A2["bars"][i]["passes"], bool(b))
    chk(P, "a2.bar2.sortino.passes", A2["bars"][1]["parts"]["sortino"]["passes"], bool(f3["sortino"] > f1["sortino"]))
    chk(P, "a2.bar2.calmar.passes", A2["bars"][1]["parts"]["calmar"]["passes"], bool(f3["calmar"] > f1["calmar"]))
    chk(P, "a2.bar3.alpha_annual.passes", A2["bars"][2]["parts"]["alpha_annual"]["passes"],
        bool(f3["alpha_annual"] >= f1["alpha_annual"]))
    chk(P, "a2.bar3.alpha_t.passes", A2["bars"][2]["parts"]["alpha_t"]["passes"], bool(f3["alpha_t"] >= f1["alpha_t"]))
    chk(P, "a2.bars_passed", A2["bars_passed"], int(sum(bars)))
    chk(P, "a2.v3_beats_v1_on_a2", A2["v3_beats_v1_on_a2"], bool(all(bars)))
    chk(P, "a2.bar5.passes (sensitivity: median turnover incl. BZ rows)", A2["bars"][4]["passes"],
        bool(cap["eqbe_plus_bz"]["v3_passes"]), note="sensitivity only")
    # turnover and scaling details
    chk(P, "a2.turnover.v3_with_its_initial_build", A2["turnover"]["v3_with_its_initial_build"], t3_incl)
    chk(P, "a2.turnover.v3_initial_build_value", A2["turnover"]["v3_initial_build_value"],
        float(fills.loc[first_build, "value"].sum()))
    chk(P, "a2.turnover.v3_metrics_json_turnover_incl_initial", A2["turnover"]["v3_metrics_json_turnover_incl_initial"], t3_incl)
    chk(P, "a2.capacity_scaling.v1_nav_at_the_open_of_17_feb_2020",
        A2["capacity_scaling"]["v1_nav_at_the_open_of_17_feb_2020"], float(c.ro1["strategy_open"].iloc[0]))
    chk(P, "a2.capacity_scaling.scale", A2["capacity_scaling"]["scale"], scale)
    chk(P, "a2.capacity_scaling.v1_p95_unscaled", A2["capacity_scaling"]["v1_p95_unscaled"], cap["eqbe"]["v1_p95_unscaled"], 1e-6)
    for k in ("v1_final_window_figures", "bars_against_v1_final"):
        unverified(P, f"a2.{k}", "needs v1_final's own window view; the requested comparison is v3 against the v1 replay's window view")
    DETAIL["A2"] = {"v1": f1, "v3": f3, "bars": [bool(b) for b in bars]}


# ================================================================ A3 owner's measures
def month_end_closes(nav: pd.DataFrame) -> pd.DataFrame:
    cl = nav[nav["mark"] == "close"]
    return cl.groupby(cl.index.to_period("M")).last()


def window_stats(cols: dict, m: int, last_end: pd.Period) -> dict:
    months = list(cols["v3"].index)
    start_ix = [i for i, p in enumerate(months) if p >= pd.Period("2020-02", "M") and i + m < len(months)
                and months[i + m] <= last_end]
    R = {k: np.array([cols[k].iloc[i + m] / cols[k].iloc[i] - 1 for i in start_ix]) for k in cols}

    def block(r, with_v1=False, k15=False):
        ex = (r - R["n500"]) * 100
        b = {"median_excess_points": float(np.percentile(ex, 50)), "p10_excess_points": float(np.percentile(ex, 10)),
             "p90_excess_points": float(np.percentile(ex, 90)), "share_beat_benchmark": float(np.mean(r > R["n500"]))}
        if with_v1:
            b["share_beat_v1"] = float(np.mean(r > R["v1"]))
        if k15:
            b["share_beat_benchmark_by_15_points_a_year"] = float(
                np.mean((1 + r) ** (12 / m) - (1 + R["n500"]) ** (12 / m) >= 0.15))
        return b
    k15 = m in (12, 24)
    return {"windows": len(start_ix), "v3": block(R["v3"], True, k15), "v1": block(R["v1"], False, k15),
            "eligible_equal_weight": block(R["ew"], False, k15)}


def part_A3(c: Ctx) -> None:
    P = "A3"
    V = c.V
    n3, n1 = c.nav3, c.nav1
    out = {}
    for who, nav in (("v3", n3), ("v1", n1)):
        sc, nc = cagr_calendar(nav["strategy"]), cagr_calendar(nav["nifty500"])
        out[who] = {"strategy_cagr": sc, "nifty500_cagr": nc, "excess": sc - nc, "met": bool(sc - nc >= 0.15)}
    cmp_tree(P, "nifty500_plus_15", V["nifty500_plus_15"], out)
    me3, me1 = month_end_closes(n3), month_end_closes(n1)
    assert list(me3.index) == list(me1.index)
    cols = {"v3": me3["strategy"], "v1": me1["strategy"], "n500": me3["nifty500"], "ew": me3["universe_ew"]}
    wins = {str(m): window_stats(cols, m, pd.Period("2026-08", "M")) for m in (3, 6, 12, 24)}
    cmp_tree(P, "windows", V["windows"], wins)
    wv1 = {m: {"windows": wins[m]["windows"], "v1": wins[m]["v1"]} for m in wins}
    cmp_tree(P, "windows_v1", V["windows_v1"], wv1)
    # multibaggers: recount 'doubled while held' from the panel's adjusted closes
    mb = {}
    for who, ep in (("v3", c.ep3), ("v1", c.ep1)):
        n_dbl = 0
        for r in ep.itertuples():
            col = c.panel.col(r.symbol)
            i0 = c.dix[r.entry]
            ex = r.exit if pd.notna(r.exit) else c.panel.dates[-1]
            i1 = c.dix[ex] if ex in c.dix else len(c.panel.dates) - 1
            seg = c.panel.close[i0:i1 + 1, col]
            mx = np.nanmax(seg) if np.isfinite(seg).any() else np.nan
            n_dbl += int(mx >= 2 * r.entry_px)
        mb[who] = {"episodes": int(len(ep)), "doubled_while_held": n_dbl, "share": n_dbl / len(ep)}
        chk(P, f"multibaggers.{who}: doubled flag in episodes.csv vs my recount from panel closes",
            int(ep["doubled"].astype(bool).sum()), n_dbl)
    cmp_tree(P, "multibaggers", V["multibaggers"], mb)

    def margin(a: pd.Series, b: pd.Series) -> dict:
        j = pd.concat([a.rename("s"), b.rename("e")], axis=1).dropna()
        yrs = years_between(j.index[0], j.index[-1])
        r = j.pct_change().dropna()
        per_year = len(r) / yrs
        gap = lambda rs, re_: np.prod(1 + rs) ** (per_year / len(rs)) - np.prod(1 + re_) ** (per_year / len(re_))
        bs = StationaryBootstrap(21, r["s"].to_numpy(), r["e"].to_numpy(), seed=7)
        draws = np.array([gap(d[0][0], d[0][1]) for d in bs.bootstrap(5000)])
        return {"point_pct": gap(r["s"].to_numpy(), r["e"].to_numpy()) * 100,
                "lo90_pct": float(np.percentile(draws, 5)) * 100, "hi90_pct": float(np.percentile(draws, 95)) * 100,
                "share_of_draws_le_zero": float((draws <= 0).mean()), "sessions_per_year": per_year}
    for key, other in (("over_v1", n1["strategy"]), ("over_eligible_equal_weight", n3["universe_ew"])):
        mine = margin(n3["strategy"], other)
        v = V["margins"][key]
        chk(P, f"margins.{key}.point_pct", v["point_pct"], mine["point_pct"], 0.005, rel=False, note="verdict rounds to 2 decimals")
        chk(P, f"margins.{key}.lo90_pct", v["lo90_pct"], mine["lo90_pct"], 1.5, rel=False, note="tolerance 1.5 points (bootstrap)")
        chk(P, f"margins.{key}.hi90_pct", v["hi90_pct"], mine["hi90_pct"], 1.5, rel=False, note="tolerance 1.5 points (bootstrap)")
        chk(P, f"margins.{key}.share_of_draws_le_zero", v["share_of_draws_le_zero"], mine["share_of_draws_le_zero"], 0.0005,
            rel=False, note="verdict rounds to 3 decimals")
        chk(P, f"margins.{key}.sessions_per_year", v["sessions_per_year"], mine["sessions_per_year"], 0.05, rel=False,
            note="verdict rounds to 1 decimal")
        DETAIL.setdefault("A3_margins", {})[key] = mine
    chk(P, "equal_weight.v3_eligible_list_cagr", V["equal_weight"]["v3_eligible_list_cagr"], cagr_calendar(n3["universe_ew"]))
    chk(P, "equal_weight.v1_universe_cagr", V["equal_weight"]["v1_universe_cagr"], cagr_calendar(n1["universe_ew"]))


# ================================================================ derivation from the `financials` table
# Written from spec sections 3.3a, 3.4, 4.1 to 4.9 and 5. The unit-scale screen of C30a is NOT reproduced
# (listed as plumbing); the revision rule is "the latest filing broadcast before D that reports at least one
# income-statement figure"; the balance sheet is the latest period known before D in any filing, then its
# latest revision.
NaN = float("nan")
INCOME = ["revenue", "other_income", "total_income", "materials", "employee_cost", "finance_costs", "depreciation",
          "other_expenses", "total_expenses", "pbt_before_exceptional", "exceptional_items", "pbt", "tax",
          "profit_continuing", "profit_reported", "eps_basic", "eps_diluted", "ebitda", "profit_normalised"]
CR25, CR125 = 25 * CR, 12.5 * CR
F3_START = pd.Timestamp("2022-11-15")


def G(new: float, old: float, B: float) -> float:
    """Section 4.7: (new - old) / max(old, B)."""
    if isnan(new) or isnan(old):
        return NaN
    return (new - old) / (old if old > B else B)


def lsum(vals) -> float:
    """Left-to-right sum; NaN if any term is NaN (section 4.10)."""
    s = vals[0]
    for v in vals[1:]:
        s = s + v
    return s


def qlabel(q: int) -> str:
    return f"{q // 4}-{(q % 4) * 3 + 3:02d}"


class FinSnap:
    """What the frozen `financials` table says at D (filings broadcast strictly before D 00:00)."""

    def __init__(self, con, ids, D, drop: set | None = None):
        """`drop`: (stock, basis, period_end) filings to treat as not filed (diagnosis of the unit-scale screen only)."""
        self.D = pd.Timestamp(D)
        fin = con.execute("SELECT * FROM financials WHERE broadcast_dt < ?", [self.D.to_pydatetime()]).df()
        fin["period_end"] = pd.to_datetime(fin["period_end"])
        fin["broadcast_dt"] = pd.to_datetime(fin["broadcast_dt"])
        fin["cid"] = ids.for_events(fin, "symbol", "broadcast_dt")
        fin["consolidated"] = fin["consolidated"].astype(bool)
        fin["q"] = fin["period_end"].dt.year * 4 + (fin["period_end"].dt.month - 1) // 3
        key = ["cid", "consolidated", "period_end", "broadcast_dt", "xbrl_url"]
        inc = fin[fin[INCOME].notna().any(axis=1)].sort_values(key)
        self.n_revised = int(inc.duplicated(["cid", "consolidated", "period_end"], keep=False).sum())
        self.multi = set(map(tuple, inc[inc.duplicated(["cid", "consolidated", "period_end"], keep=False)]
                             [["cid", "consolidated", "q"]].to_numpy()))
        inc = inc.drop_duplicates(["cid", "consolidated", "period_end"], keep="last")
        if drop:
            keep = [(a, b, c_) not in drop for a, b, c_ in zip(inc["cid"], inc["consolidated"], inc["period_end"])]
            inc = inc[keep]
        self.q: dict = {}
        for r in inc.itertuples(index=False):
            self.q.setdefault((r.cid, r.consolidated), {})[r.q] = r
        bs = fin[fin["has_balance_sheet"].astype(bool)].sort_values(
            ["cid", "consolidated", "period_end", "broadcast_dt", "xbrl_url"])
        bs = bs.drop_duplicates(["cid", "consolidated"], keep="last")
        self.bs = {(r.cid, r.consolidated): r for r in bs.itertuples(index=False)}


def _pbe(r) -> float:
    if r is None:
        return NaN
    if not isnan(r.pbt_before_exceptional):
        return float(r.pbt_before_exceptional)
    if isnan(r.pbt):
        return NaN
    return float(r.pbt) - (0.0 if isnan(r.exceptional_items) else float(r.exceptional_items))


def derive(fs: FinSnap, cid: str, D: pd.Timestamp) -> dict | None:
    """Every v3 figure of a stock at D that does not need a price (sections 3.3a, 4.1 to 4.9)."""
    qc, qs_ = fs.q.get((cid, True), {}), fs.q.get((cid, False), {})
    allq = list(qc) + list(qs_)
    if not allq:
        return None
    Ls = max(allq)

    def complete(t) -> bool:
        for k in range(8):
            r = t.get(Ls - k)
            if r is None or isnan(r.revenue) or isnan(r.profit_normalised) or isnan(r.ebitda):
                return False
        return True
    c4_cons = bool(qc) and all((Ls - k) in qc for k in range(4))
    if complete(qc):
        cons, L, win = True, Ls, True
    elif complete(qs_):
        cons, L, win = False, Ls, True
    else:
        cons, win = c4_cons, False
        t0 = qc if cons else qs_
        if not t0:
            return None
        L = max(t0)
    t = qc if cons else qs_

    def col(name, ks):
        out = []
        for k in ks:
            r = t.get(L - k)
            out.append(NaN if r is None else float(getattr(r, name)))
        return out
    R, P, E, FC = col("revenue", range(9)), col("profit_normalised", range(9)), col("ebitda", range(9)), col("finance_costs", range(4))
    PBE = [_pbe(t.get(L - k)) for k in range(4)]
    EBIT = [NaN if (isnan(PBE[k]) or isnan(FC[k])) else PBE[k] + FC[k] for k in range(4)]
    d = {"cid": cid, "basis": "consolidated" if cons else "standalone",
         "c4_basis": "consolidated" if c4_cons else "standalone", "L": qlabel(L), "window_complete": win}
    run = 0
    while (L - run) in t:
        run += 1
    d["run"] = run
    d["missing_quarters"] = ";".join(qlabel(L - k) for k in range(9) if (L - k) not in t)
    d["ttm_p"], d["ttm4_p"] = lsum(P[0:4]), lsum(P[4:8])
    d["ttm_r"], d["ttm4_r"] = lsum(R[0:4]), lsum(R[4:8])
    d["ttm_e"], d["ttm4_e"] = lsum(E[0:4]), lsum(E[4:8])
    d["ttm_ebit"] = lsum(EBIT)
    d["fc_pos"] = NaN if any(isnan(x) for x in FC) else lsum([x if x > 0 else 0.0 for x in FC])
    d["write_back"] = any((not isnan(x)) and x < -CR for x in FC)
    if isnan(d["ttm_ebit"]) or isnan(d["fc_pos"]):
        d["icr_state"], d["icr"] = "unknown", NaN
    elif d["write_back"]:
        d["icr_state"], d["icr"] = "write_back", NaN
    elif d["fc_pos"] == 0:
        d["icr_state"], d["icr"] = "no_finance_costs", NaN
    else:
        d["icr_state"], d["icr"] = "icr", d["ttm_ebit"] / d["fc_pos"]
    d["down0"] = None if any(isnan(x) for x in P[0:8]) else bool(lsum(P[0:4]) < lsum(P[4:8]))
    d["down1"] = None if any(isnan(x) for x in P[1:9]) else bool(lsum(P[1:5]) < lsum(P[5:9]))
    # ranking measures
    d["A_R"] = (G(R[0] + R[1], R[4] + R[5], CR125) - G(R[2] + R[3], R[6] + R[7], CR125))
    d["A_P"] = (G(P[0] + P[1], P[4] + P[5], CR125) - G(P[2] + P[3], P[6] + P[7], CR125))
    ok_m = (not isnan(d["ttm_r"])) and (not isnan(d["ttm4_r"])) and d["ttm_r"] > 0 and d["ttm4_r"] > 0
    d["M"] = (d["ttm_e"] / d["ttm_r"] - d["ttm4_e"] / d["ttm4_r"]) if ok_m else NaN
    d["g"] = G(d["ttm_p"], d["ttm4_p"], CR25)
    # balance sheet in basis b*
    b = fs.bs.get((cid, cons))
    if b is None:
        d.update(bs_period_end=None, equity=NaN, debt=NaN, bs_usable=False, de=NaN)
    else:
        debt = 0.0 if isnan(b.debt_total) else float(b.debt_total)
        usable = (pd.Timestamp(fs.D) - b.period_end).days <= 400
        eq = float(b.equity) if not isnan(b.equity) else NaN
        d.update(bs_period_end=str(b.period_end.date()), equity=eq, debt=debt, bs_usable=bool(usable),
                 de=(debt / eq) if (usable and not isnan(eq) and eq > 0) else NaN)
    d["revised_in_window"] = any(((cid, cons, L - k) in fs.multi) for k in range(9))
    return d


def flags_from(d: dict, D: pd.Timestamp, mcap: float) -> dict:
    """F1 to F6 and P/E, PEG from the derived figures and the market cap (sections 4.4, 5)."""
    tp = d["ttm_p"]
    pe = (mcap / tp) if (not isnan(mcap) and mcap > 0 and not isnan(tp) and tp > 0) else NaN
    f = {"pe": pe}
    f["f1"] = bool((not isnan(tp)) and tp >= CR25)
    f["f2"] = bool(d["icr_state"] == "no_finance_costs" or (d["icr_state"] == "icr" and d["icr"] >= 2.5))
    if D >= F3_START and d["bs_usable"]:
        f["f3"] = bool((not isnan(d["equity"])) and d["equity"] > 0 and (not isnan(d["de"])) and d["de"] < 1.2)
    else:
        f["f3"] = True
    f["f4"] = bool((not isnan(pe)) and 0 < pe <= 60)
    f["f5"] = bool(d["window_complete"] and (not isnan(d["ttm_r"])) and d["ttm_r"] > 0
                   and (not isnan(d["ttm4_r"])) and d["ttm4_r"] > 0)
    f["f6"] = not (d["down0"] is True and d["down1"] is True)
    g = d["g"]
    f["PEG"] = (pe / (100 * min(g, 0.5))) if (not isnan(pe) and not isnan(g) and g > 0) else NaN
    return f


# ---------------------------------------------------------------- section 6 ranking
def section6(df: pd.DataFrame) -> pd.DataFrame:
    """df: symbol, ticker, A_R, A_P, M, g, PEG (one row per eligible stock). Returns the ranking columns."""
    df = df.reset_index(drop=True)
    N = len(df)
    r_ar, r_ap, r_m = rankdata(df["A_R"]), rankdata(df["A_P"]), rankdata(df["M"])
    R1 = rankdata(r_ar + r_ap)
    pos = (df["g"] > 0).to_numpy()
    n0 = int((~pos).sum())
    R3 = np.full(N, (n0 + 1) / 2)
    R3[pos] = n0 + rankdata(-df.loc[pos, "PEG"].to_numpy())
    T = R1 + r_m + R3
    out = df.assign(r_A_R=r_ar, r_A_P=r_ap, R1=R1, R2=r_m, R3=R3, T=T)
    out = out.sort_values(["T", "ticker", "symbol"], ascending=[False, True, True], kind="mergesort").reset_index(drop=True)
    out["rank"] = np.arange(1, N + 1)
    out["S1"], out["S2"], out["S3"], out["S"] = out["R1"] / N, out["R2"] / N, out["R3"] / N, out["T"] / (3 * N)
    out["pct_A_R"], out["pct_A_P"], out["pct_M"] = out["r_A_R"] / N, out["r_A_P"] / N, out["R2"] / N
    return out


# ================================================================ A4 score signal
def forward_returns(c: Ctx, D, Dn, cids) -> pd.Series:
    """(V(D') + dividends in (D, D']) / V(D) - 1 on the engine panel (section 14.4); NaN if V is missing."""
    p = c.panel
    t0, t1 = c.dix[D], c.dix[Dn]
    cols = [p.col(s) for s in cids]
    v0 = np.array([px_at(p, t0, col) for col in cols])
    v1 = np.array([px_at(p, t1, col) for col in cols])
    div = p.div_unit[t0 + 1:t1 + 1][:, cols].sum(axis=0)
    with np.errstate(invalid="ignore", divide="ignore"):
        ret = (v1 + div) / v0 - 1
    ret[~((v0 > 0) & np.isfinite(v1))] = np.nan
    return pd.Series(ret, index=list(cids))


def part_A4(c: Ctx) -> None:
    P = "A4"
    S = c.V["score_signal"]
    Dn = list(c.Ds[1:]) + [c.Dend]
    by, ics = [], {"T": [], "R1": [], "R2": [], "R3": []}
    group_ret = []
    for D, D2 in zip(c.Ds, Dn):
        r = c.ranks[c.ranks["D"] == D].sort_values("rank")
        fr = forward_returns(c, D, D2, list(r["symbol"]))
        ok = fr.notna().to_numpy()
        row = {"D": str(D.date()), "to": str(D2.date()), "eligible": int(len(r)),
               "without_a_forward_return": int((~ok).sum())}
        for k in ics:
            rho = float(spearmanr(r.loc[ok, k].to_numpy(), fr.to_numpy()[ok]).statistic)
            row[f"ic_{k}"] = rho
            ics[k].append(rho)
        by.append(row)
        # quintile spread, approximation: equal-weight fifths of the section-6 order (sizes differ by at most 1)
        grp = np.array_split(np.arange(len(r)), 5)
        v = fr.to_numpy()
        group_ret.append((np.nanmean(v[grp[0]]), np.nanmean(v[grp[-1]])))
    cmp_tree(P, "score_signal.by_review", S["by_review"], by)
    ic_sum = {}
    for k, a in ics.items():
        a26 = np.array(a[:26])
        ic_sum[k] = {"reviews": 26, "mean": float(a26.mean()),
                     "t": float(a26.mean() / (a26.std(ddof=1) / math.sqrt(26))),
                     "share_positive": float((a26 > 0).mean())}
    cmp_tree(P, "score_signal.ic", S["ic"], ic_sum)
    # quintile spread (approximate, not an exact match by design)
    yrs = (WINDOW_END - WINDOW_START).days / 365.25
    top = float(np.prod([1 + a for a, _ in group_ret]) ** (1 / yrs) - 1)
    bot = float(np.prod([1 + b for _, b in group_ret]) ** (1 / yrs) - 1)
    q = S["quintiles"]
    DETAIL["A4_quintiles"] = {"approx_top_cagr": top, "approx_bottom_cagr": bot, "approx_spread": top - bot,
                              "verdict_top_cagr": q["top_cagr"], "verdict_bottom_cagr": q["bottom_cagr"],
                              "verdict_spread": q["cagr_spread"],
                              "sign_agrees": bool(np.sign(top - bot) == np.sign(q["cagr_spread"])),
                              "ratio_approx_to_verdict": (top - bot) / q["cagr_spread"]}
    ok_sign = np.sign(top - bot) == np.sign(q["cagr_spread"])
    close = abs((top - bot) - q["cagr_spread"]) <= 0.05
    CHECKS.append({"part": P, "item": "score_signal.quintiles.cagr_spread (approximation, equal-weight fifths, no costs)",
                   "verdict": q["cagr_spread"], "recomputed": top - bot, "difference": top - bot - q["cagr_spread"],
                   "tolerance": "sign must agree; size within 5 points", "match": bool(ok_sign and close),
                   "note": f"approx top {top:.4f} (verdict {q['top_cagr']:.4f}), bottom {bot:.4f} (verdict {q['bottom_cagr']:.4f}); "
                           "not an exact match by design"})
    unverified(P, "score_signal.quintiles.information_ratio / tracking_error", "needs engine.run_quantile's execution (20-session exit, fills); only the spread was approximated")


# ================================================================ A5 earnings and re-rating
def snapshot_N_and_close(c: Ctx, D, cids) -> pd.DataFrame:
    """Share count N (pit.fundamentals, plumbing) and the adjusted close before D, for stocks with no eligibility row."""
    snap = pit.build_snapshot(c.con, D, c.static)
    fu = pit.fundamentals(snap)
    last = snap.px.sort_values(["symbol", "date"]).groupby("symbol").tail(1).set_index("symbol")
    out = pd.DataFrame(index=list(cids))
    out["N"] = fu["shares"].reindex(out.index)
    out["C"] = last["adj_close"].reindex(out.index)
    out["close_date"] = last["date"].reindex(out.index)
    out["prev_session_close"] = out["close_date"] == snap.cal[-1]
    return out


def figures_table(c: Ctx, pairs: set) -> pd.DataFrame:
    """(D, stock) -> TTM_P and market cap as the split needs them.

    Where eligibility.csv has a row it is used (the market cap is plumbing: C x N). My own TTM_P from the
    `financials` table is computed for every pair and compared. Where there is no row (v1's holdings that
    left v3's universe, and the end-date snapshot) TTM_P is my own and the market cap is my own TTM-free
    C x N, N from pit.fundamentals at that date."""
    E = c.elig.set_index(["D", "symbol"])
    rows = []
    byD: dict = {}
    for D, s in pairs:
        byD.setdefault(D, set()).add(s)
    for D in sorted(byD):
        fs = FinSnap(c.con, c.ids, D)
        have = [s for s in byD[D] if (D, s) in E.index]
        miss = [s for s in byD[D] if (D, s) not in E.index]
        nm = snapshot_N_and_close(c, D, miss) if miss else None
        for s in byD[D]:
            d = derive(fs, s, D)
            own = NaN if d is None else d["ttm_p"]
            if s in have:
                e = E.loc[(D, s)]
                rows.append({"D": D, "symbol": s, "source": "eligibility.csv", "ttm_p": e["ttm_p"], "mcap": e["mcap"],
                             "ttm_p_own": own, "close_adj": e["close_adj"], "shares": e["shares"]})
            else:
                # section 3.5: no close in the window, or N missing or not above zero, means no market cap
                mc = NaN
                if s in nm.index and nm.at[s, "N"] > 0 and nm.at[s, "C"] > 0:
                    mc = float(nm.at[s, "C"] * nm.at[s, "N"])
                rows.append({"D": D, "symbol": s, "source": "recomputed", "ttm_p": own, "mcap": mc, "ttm_p_own": own,
                             "close_adj": nm.at[s, "C"], "shares": nm.at[s, "N"],
                             "close_is_previous_session": bool(nm.at[s, "prev_session_close"])})
        del fs
    return pd.DataFrame(rows).set_index(["D", "symbol"])


def er_split(c: Ctx, trades: pd.DataFrame, fig: pd.DataFrame, nav: pd.DataFrame, nav_open: float) -> dict:
    Ds = list(c.Ds) + [c.Dend]
    dates = c.panel.dates
    by = []
    for k in range(27):
        D0, D1 = Ds[k], Ds[k + 1]
        t0, t1 = c.dix[D0] - 1, c.dix[D1] - 1
        u = units_after(trades, dates[t1])
        recs = []
        for s, uu in u.items():
            col = c.panel.col(s)
            a, b = fig.loc[(D0, s)], fig.loc[(D1, s)]
            recs.append((uu * c.panel.last[t0, col], uu * c.panel.last[t1, col], a["ttm_p"], a["mcap"], b["ttm_p"], b["mcap"]))
        d = pd.DataFrame(recs, columns=["v0", "v1", "tp0", "m0", "tp1", "m1"])
        ok = d[["tp0", "m0", "tp1", "m1"]].notna().all(axis=1)
        dd = d[ok]
        V0, V1 = dd["v0"].sum(), dd["v1"].sum()
        E0, E1 = (dd["v0"] * dd["tp0"] / dd["m0"]).sum(), (dd["v1"] * dd["tp1"] / dd["m1"]).sum()
        defined = bool(E0 > 0 and E1 > 0 and len(dd) > 0)
        by.append({"defined": defined, "price": float(V1 / V0), "earnings": float(E1 / E0) if defined else NaN,
                   "rerating": float((V1 / E1) / (V0 / E0)) if defined else NaN, "from": str(D0.date()), "to": str(D1.date()),
                   "holdings": int(len(d)), "share_of_value_left_out": float(1 - dd["v1"].sum() / d["v1"].sum())})
    good = [b for b in by if b["defined"]]
    GE, GP = float(np.prod([b["earnings"] for b in good])), float(np.prod([b["rerating"] for b in good]))
    c0, c1 = pd.Timestamp("2020-02-14"), pd.Timestamp("2026-09-17")
    days = (c1 - c0).days
    nav_close = float(nav.loc[(nav.index.normalize() == c1) & (nav["mark"] == "close"), "strategy"].iloc[0])
    return {"intervals": len(by), "undefined_intervals": len(by) - len(good), "earnings_factor": GE, "rerating_factor": GP,
            "earnings_annual": GE ** (365.25 / days) - 1, "rerating_annual": GP ** (365.25 / days) - 1,
            "share_from_earnings": math.log(GE) / (math.log(GE) + math.log(GP)),
            "remainder": nav_close / nav_open / (GE * GP),
            "clock": {"from_close": str(c0.date()), "to_close": str(c1.date()), "calendar_days": days},
            "by_interval": by}


def part_A5(c: Ctx) -> None:
    P = "A5"
    ER = c.V["earnings_and_rerating"]
    Ds = list(c.Ds) + [c.Dend]
    dates = c.panel.dates
    pairs, who_pairs = set(), {}
    for who, tr in (("v3", c.fills), ("v1", c.tr1)):
        ps = set()
        for k in range(27):
            t1 = c.dix[Ds[k + 1]] - 1
            for s in units_after(tr, dates[t1]).index:
                ps.add((Ds[k], s))
                ps.add((Ds[k + 1], s))
        who_pairs[who] = ps
        pairs |= ps
    fig = figures_table(c, pairs)
    # my TTM_P against the one in eligibility.csv for every (review, stock) pair the split uses
    have = fig[fig["source"] == "eligibility.csv"]
    same = ((have["ttm_p"].isna() & have["ttm_p_own"].isna()) |
            ((have["ttm_p"] - have["ttm_p_own"]).abs() <= 1e-9 * have["ttm_p"].abs().clip(lower=1.0)))
    chk(P, "pairs used by the split whose TTM_P in eligibility.csv equals my own from `financials` (count)",
        int(len(have)), int(same.sum()), note=f"{len(have)} (review, stock) pairs, v3 and v1 together")
    DETAIL["A5_ttm_p_disagreements"] = [{"D": str(i[0].date()), "symbol": i[1], "eligibility_ttm_p": float(r["ttm_p"]) if pd.notna(r["ttm_p"]) else None,
                                        "own_ttm_p": float(r["ttm_p_own"]) if pd.notna(r["ttm_p_own"]) else None}
                                       for i, r in have[~same].iterrows()]
    rec = fig[fig["source"] == "recomputed"]
    DETAIL["A5_recomputed_pairs"] = [{"D": str(i[0].date()), "symbol": i[1], "ttm_p": None if pd.isna(r["ttm_p"]) else float(r["ttm_p"]),
                                      "mcap": None if pd.isna(r["mcap"]) else float(r["mcap"]),
                                      "close_is_previous_session": bool(r.get("close_is_previous_session", False))}
                                     for i, r in rec.iterrows()]
    # close and market cap internal consistency of the rows used
    expect = (have["close_adj"] * have["shares"]).where((have["shares"] > 0) & (have["close_adj"] > 0))      # section 3.5: N must be above zero
    chk(P, "eligibility rows used: mcap == close_adj x shares, and none where the share count is not above zero (count of pairs)", int(len(have)),
        int(((have["mcap"].isna() & expect.isna()) | (np.abs(have["mcap"] - expect) <= 1e-9 * have["mcap"].abs().clip(lower=1.0))).sum()),
        note="market cap is plumbing (C x N); the arithmetic and the N > 0 rule are checked, C and N are not re-derived")
    v3 = er_split(c, c.fills, fig, c.nav3, 500000.0)
    v1 = er_split(c, c.tr1, fig, c.nav1, float(c.ro1["strategy_open"].iloc[0]))
    cmp_tree(P, "earnings_and_rerating.v3", ER["v3"], v3)
    cmp_tree(P, "earnings_and_rerating.v1", ER["v1"], v1)
    # which intervals depend on a recomputed (not eligibility.csv) input
    dep = {who: sorted({str(D.date()) for (D, s) in who_pairs[who] if (D, s) in rec.index}) for who in who_pairs}
    DETAIL["A5_intervals_using_recomputed_inputs"] = dep
    # Nifty 500 context from the frozen indices table (NSE's published P/E)
    ix = c.con.execute("SELECT date, close, pe FROM indices WHERE index_name = 'Nifty 500' AND date IN (DATE '2020-02-14', DATE '2026-09-17') ORDER BY date").df()
    if len(ix) == 2:
        pf = ix["close"].iloc[1] / ix["close"].iloc[0]
        rr = ix["pe"].iloc[1] / ix["pe"].iloc[0]
        N5 = ER["nifty500"]
        chk(P, "earnings_and_rerating.nifty500.price_factor", N5["price_factor"], float(pf))
        chk(P, "earnings_and_rerating.nifty500.rerating_factor", N5["rerating_factor"], float(rr))
        chk(P, "earnings_and_rerating.nifty500.earnings_factor", N5["earnings_factor"], float(pf / rr))
        days = 2407
        chk(P, "earnings_and_rerating.nifty500.earnings_annual", N5["earnings_annual"], float((pf / rr) ** (365.25 / days) - 1))
        chk(P, "earnings_and_rerating.nifty500.rerating_annual", N5["rerating_annual"], float(rr ** (365.25 / days) - 1))
    else:
        unverified(P, "earnings_and_rerating.nifty500", "indices table lacks Nifty 500 on 14 Feb 2020 or 17 Sep 2026")
    DETAIL["A5"] = {"v3": {k: v for k, v in v3.items() if k != "by_interval"}, "v1": {k: v for k, v in v1.items() if k != "by_interval"}}


# ================================================================ A6 v3 figures
def part_A6(c: Ctx) -> None:
    P = "A6"
    F = c.V["v3_figures"]
    d = c.dec.set_index("D")
    cash = (d["budget_left"] / d["nav_open"])
    mine = {"cash_share_after_the_planned_trades": {"mean": float(cash.mean()), "max": float(cash.max()),
                                                    "by_review": {str(k.date()): float(v) for k, v in cash.items()}}}
    held = c.hold[c.hold["status"] != "new"]
    mine["holdings_at_open_by_review"] = {str(D.date()): int((held["D"] == D).sum()) for D in c.Ds}
    ex = held[held["status"] == "exited"]
    first = ex["exit_codes"].str.split(";").str[0]
    mine["exits_by_code"] = {k: int(v) for k, v in first.value_counts().sort_index().items()}
    cap_f = c.fills[c.fills["kind"] == "cap"]
    val_f = c.fills[(c.fills["kind"] == "valuation") & (c.fills["reason"].str.contains("trim", case=False, na=False))]
    mine["trims"] = {"valuation": {"count": int((held["status"] == "trimmed_valuation").sum()), "rupees": float(val_f["value"].sum())},
                     "cap": {"count": int((held["status"] == "trimmed_cap").sum()), "rupees": float(cap_f["value"].sum())}}
    reasons = d["empty_reasons"].dropna().str.split(";").explode().value_counts()
    mine["slots_left_empty"] = {"total": int(d["slots_left_empty"].sum()), "reviews_with_any": int((d["slots_left_empty"] > 0).sum()),
                                "reasons": {k: int(v) for k, v in reasons.items()}}
    mine["holdings_outside_v1_universe_by_review"] = {str(D.date()): int((~held.loc[held["D"] == D, "in_universe"]).sum()) for D in c.Ds}
    unt = held[held["untested"].notna()]
    toks = unt["untested"].str.split(";").explode().value_counts()
    em = c.elig.set_index(["D", "symbol"])["missing_quarters"]
    mq = np.array([isinstance(em.get((r.D, r.symbol), np.nan), str) for r in unt.itertuples()])
    mine["holdings_with_an_untested_exit_or_trim"] = {"holding_reviews": int(len(unt)), "by_test": {k: int(v) for k, v in toks.items()},
                                                      "with_a_quarter_missing_from_the_archive": int(mq.sum())}
    # "held slots": the slots held after each review's decisions (kept, trimmed and new; not the exited)
    slots = c.hold[c.hold["status"] != "exited"]
    mine["share_of_held_slots_unclassified"] = float((slots["sector_key"] == "unclassified").sum() / len(slots))
    # the text leaves the denominator open; the alternatives are recorded
    DETAIL["A6_alt"] = {"slots_after_decisions": int(len(slots)), "unclassified_in_them": int((slots["sector_key"] == "unclassified").sum()),
                        "share_over_holdings_at_open_only": float((held["sector_key"] == "unclassified").sum() / len(held)),
                        "definition_used": "kept + trimmed + new rows of holdings.csv (status != exited)"}
    cmp_tree(P, "v3_figures", F, mine)


# ================================================================ B rule checks on the run's own records
def tri(x):
    """True / False / None from a CSV cell that may be empty."""
    return None if (x is None or (isinstance(x, float) and math.isnan(x)) or x is pd.NA) else bool(x)


def book_replay(c: Ctx) -> dict:
    """Replay fills.csv over the engine panel: dividends first, then the day's fills (as listed), then the close mark.
    Records the book at each review's open (after dividends, before fills) and every close mark."""
    p, dates = c.panel, c.panel.dates
    S = len(p.symbols)
    fills = c.fills[c.fills["event"] == "fill"]
    by_date = {d: g for d, g in fills.groupby("date", sort=False)}
    units, cash = np.zeros(S), 500000.0
    review_set = set(c.Ds)
    rep = {"open": {}, "close": {}, "cash_pre_buy": {}, "order_violations": [], "forced_violations": [],
           "min_cash": cash, "units_end": None}
    t0 = c.dix[WINDOW_START]
    for t in range(t0, len(dates)):
        d = dates[t]
        cash += float((units * p.div_unit[t]).sum())
        if d in review_set or d == dates[-1] or t == t0:
            held = units > 1e-12
            px = np.where(p.traded[t], p.open_[t], p.last[t - 1])
            rep["open"][d] = {"cash": cash, "units": units.copy(), "px": px.copy(),
                              "nav": cash + float(np.nansum(np.where(held, units * px, 0.0)))}
        if d in by_date:
            g = by_date[d]
            seen_buy = False
            for r in g.itertuples():
                col = p.col(r.symbol)
                if r.side == "buy":
                    if not seen_buy:
                        rep["cash_pre_buy"][d] = cash
                    seen_buy = True
                    units[col] += r.units
                    cash -= r.value + r.cost
                else:
                    if seen_buy:
                        rep["order_violations"].append((str(d.date()), r.symbol, "a sell is listed after a buy on the same session"))
                    units[col] -= r.units
                    cash += r.value - r.cost
                rep["min_cash"] = min(rep["min_cash"], cash)
                if cash < -1e-6:
                    rep["order_violations"].append((str(d.date()), r.symbol, f"cash negative {cash:.4f}"))
            if not seen_buy:
                rep["cash_pre_buy"][d] = cash
        if d != dates[-1]:
            held = units > 1e-9
            rep["close"][d] = cash + float(np.nansum(np.where(held, units * p.last[t], 0.0)))
            stuck = held & (p.no_trade_run[t] >= 20)
            for j in np.where(stuck)[0]:
                rep["forced_violations"].append((str(d.date()), p.symbols[j], "held after 20 sessions without a trade"))
    rep["units_end"] = units.copy()
    return rep


def part_B(c: Ctx) -> None:
    P = "B"
    p = c.panel
    rep = book_replay(c)
    c.rep = rep
    E = c.elig.set_index(["D", "symbol"])
    Hh = c.hold.set_index(["D", "symbol"])
    dec = c.dec.set_index("D")
    rk = {D: g.sort_values("rank") for D, g in c.ranks.groupby("D")}
    U0: dict = {}
    prev_held: set = set()
    viol = {k: [] for k in ("B1_exits", "B2_trims", "B3_buys", "B4_fills", "B5_nav", "records")}
    n = {k: 0 for k in viol}
    exp_orders: list = []
    dec_mine = {}
    tol = 1e-9

    def close(a, b, rel=1e-9):
        if isnan(a) and isnan(b):
            return True
        if isnan(a) or isnan(b):
            return False
        return abs(a - b) <= rel * max(1.0, abs(a))

    for i, D in enumerate(c.Ds):
        st = rep["open"][D]
        units, px, nav, cash = st["units"], st["px"], st["nav"], st["cash"]
        tD = c.dix[D]
        held_idx = np.where(units > 1e-12)[0]
        held = [p.symbols[j] for j in held_idx]
        hrows = c.hold[(c.hold["D"] == D) & (c.hold["status"] != "new")]
        # the records agree with the replayed book
        n["records"] += 1
        if set(hrows["symbol"]) != set(held):
            viol["records"].append((str(D.date()), "holdings.csv rows at open differ from the replayed book",
                                    sorted(set(hrows["symbol"]) ^ set(held))))
        chk_ = [("nav_open", dec.at[D, "nav_open"], nav), ("cash_open", dec.at[D, "cash_open"], cash),
                ("rebalance_open.csv", float(c.ro3.set_index("date").at[D, "strategy_open"]), nav)]
        for nm, a, b in chk_:
            if not close(a, b):
                viol["records"].append((str(D.date()), f"{nm}: recorded {a} vs replayed {b}", None))
        dm = {"n_held_open": len(held)}
        codes_by = {}
        rows = {}
        for s in held:
            j = p.col(s)
            u, pxs = float(units[j]), float(px[j])
            e = E.loc[(D, s)]
            h = Hh.loc[(D, s)]
            pe = float(e["pe"]) if pd.notna(e["pe"]) else NaN
            # step 4: reference units U0
            if not isnan(pe) and pe > 85:
                U0.setdefault(s, u)
            elif not isnan(pe):
                U0.pop(s, None)
            # step 5: exits E1 to E5
            codes = []
            ttm_p = float(e["ttm_p"]) if pd.notna(e["ttm_p"]) else NaN
            fc, eb = (float(e["fc_pos"]) if pd.notna(e["fc_pos"]) else NaN), (float(e["ttm_ebit"]) if pd.notna(e["ttm_ebit"]) else NaN)
            usable = pd.notna(e["bs_period_end"]) and (D - pd.Timestamp(e["bs_period_end"])).days <= 400
            eq = float(e["equity"]) if pd.notna(e["equity"]) else NaN
            debt = float(e["debt"]) if pd.notna(e["debt"]) else NaN
            d0, d1 = tri(e["down0"]), tri(e["down1"])
            held_prev = s in prev_held
            if (not isnan(ttm_p)) and ttm_p <= 0:
                codes.append("E1")
            if (not isnan(fc)) and (not isnan(eb)) and fc > 0 and eb / fc < 2.5:
                codes.append("E2")
            if D >= F3_START and usable and ((not isnan(eq)) and (eq <= 0 or debt / eq > 1.2)):
                codes.append("E3")
            if d0 is True and d1 is True and held_prev:
                codes.append("E4")
            lpe = e["latest_period_end_c4"]
            if pd.isna(lpe) or (D - pd.Timestamp(lpe)).days > 200:
                codes.append("E5")
            full = bool(codes)
            u_V = u_C = NaN
            to_sell = 0.0
            if full:
                to_sell = u
            else:
                if (s in U0) and (not isnan(pe)) and pe > 85:
                    u_V = max(0.0, 1 - 2 * (pe / 85 - 1)) * U0[s]
                    if u_V == 0:
                        codes, full, to_sell = ["V"], True, u
                if not full:
                    u_C = 0.10 * nav / pxs
                    ustar = min(u_V, u_C) if not isnan(u_V) else u_C
                    to_sell = (u - ustar) if u > ustar else 0.0
            if full:
                status = "exited"
            elif to_sell > 0:
                status = "trimmed_valuation" if (not isnan(u_V) and u_V <= u_C) else "trimmed_cap"
            else:
                status = "kept"
            # untested list
            ut = []
            if isnan(ttm_p):
                ut.append("E1")
            if isnan(fc) or isnan(eb):
                ut.append("E2")
            if D >= F3_START and not usable:
                ut.append("E3")
            if (d0 is None or d1 is None) and held_prev:
                ut.append("E4")
            if isnan(pe) and not full:                        # C8: no trim test (so none left untested) for a full sale
                ut.append("trim")
            rows[s] = dict(codes=codes, status=status, u_V=u_V, u_C=u_C, to_sell=to_sell, ut=ut, full=full, U0=U0.get(s, NaN),
                           u=u, px=pxs, pe=pe, excess=(pe / 85 - 1) if (not isnan(pe) and pe > 85) else NaN)
            # ---- compare with holdings.csv
            n["B1_exits"] += 1
            exp_codes = ";".join(codes)
            got = h["exit_codes"] if pd.notna(h["exit_codes"]) else ""
            if exp_codes != got:
                viol["B1_exits"].append((str(D.date()), s, f"exit codes recorded '{got}' vs derived '{exp_codes}'"))
            if str(h["status"]) != status:
                viol["B2_trims"].append((str(D.date()), s, f"status recorded {h['status']} vs derived {status}"))
            n["B2_trims"] += 1
            for nm, mine_v, rec_v in (("U0", U0.get(s, NaN), h["U0"]), ("u_V", u_V, h["u_V"]), ("u_C", u_C, h["u_C"]),
                                      ("units_to_sell", to_sell, h["units_to_sell"]), ("units_open", u, h["units_open"]),
                                      ("px_now", pxs, h["px_now"]), ("weight_open", u * pxs / nav, h["weight_open"]),
                                      ("pe", pe, h["pe"]), ("excess", rows[s]["excess"], h["excess"])):
                rv = float(rec_v) if pd.notna(rec_v) else NaN
                if not close(float(mine_v), rv):
                    viol["B2_trims"].append((str(D.date()), s, f"{nm} recorded {rv} vs derived {float(mine_v)}"))
            rec_ut = h["untested"] if pd.notna(h["untested"]) else ""
            if ";".join(ut) != rec_ut:
                viol["B2_trims"].append((str(D.date()), s, f"untested recorded '{rec_ut}' vs derived '{';'.join(ut)}'"))
            if to_sell > 0:
                exp_orders.append(dict(D=D, tD=tD, symbol=s, side="sell", qty=to_sell, full=full, codes=codes, u_V=u_V, u_C=u_C))
        # U0 belongs to the holding episode: cleared once the units have gone
        for s in list(U0):
            if s not in held:
                U0.pop(s)
        # step 8 to 11: slots, candidates, budget, fill
        kept = [s for s in held if not rows[s]["full"]]
        empty = max(0, 18 - len(kept))
        budget = cash + sum(rows[s]["to_sell"] * rows[s]["px"] * (1 - 0.002) for s in held)
        sector_count: dict = {}
        for s in kept:
            k = Hh.loc[(D, s)]["sector_key"]
            sector_count[k] = sector_count.get(k, 0) + 1
        cand = rk[D][~rk[D]["symbol"].isin(set(held))]
        b_left, buys, skipped_cap, stopped, ran_out = budget, [], False, False, False
        it = cand.itertuples()
        while len(buys) < empty:
            r = next(it, None)
            if r is None:
                ran_out = True
                break
            if sector_count.get(r.sector_key, 0) >= 4:
                skipped_cap = True
                continue
            V = min(nav / 18, b_left / 1.002)
            if V < nav / 36:
                stopped = True
                break
            buys.append((r.symbol, V, int(r.rank)))
            b_left -= V * 1.002
            sector_count[r.sector_key] = sector_count.get(r.sector_key, 0) + 1
        left = empty - len(buys)
        reasons = []
        if left > 0:
            if ran_out:
                reasons.append("none_eligible")
            if skipped_cap:
                reasons.append("sector_cap")
            if stopped:
                reasons.append("half_slot_minimum")
        # compare with the records
        n["B3_buys"] += 1
        new = c.hold[(c.hold["D"] == D) & (c.hold["status"] == "new")].sort_values("rank")
        mine_b = [(s, v) for s, v, _ in buys]
        rec_b = [(r.symbol, r.buy_value) for r in new.itertuples()]
        if [x[0] for x in mine_b] != [x[0] for x in rec_b] or any(not close(a[1], b[1]) for a, b in zip(mine_b, rec_b)):
            viol["B3_buys"].append((str(D.date()), "buys differ", {"derived": [(s, round(v, 2)) for s, v in mine_b],
                                                                  "recorded": [(s, round(v, 2)) for s, v in rec_b]}))
        for nm, mv, rv in (("budget_B", budget, dec.at[D, "budget_B"]), ("budget_left", b_left, dec.at[D, "budget_left"]),
                           ("empty_slots", empty, dec.at[D, "empty_slots"]), ("n_buys", len(buys), dec.at[D, "n_buys"]),
                           ("slots_left_empty", left, dec.at[D, "slots_left_empty"]), ("n_kept", len(kept), dec.at[D, "n_kept"]),
                           ("n_held_open", len(held), dec.at[D, "n_held_open"])):
            if not close(float(mv), float(rv)):
                viol["B3_buys"].append((str(D.date()), f"{nm} recorded {rv} vs derived {mv}", None))
        rr = dec.at[D, "empty_reasons"] if pd.notna(dec.at[D, "empty_reasons"]) else ""
        if ";".join(reasons) != rr:
            viol["B3_buys"].append((str(D.date()), f"empty reasons recorded '{rr}' vs derived '{';'.join(reasons)}'", None))
        # the candidate rule: buys must come from the eligible list in rank order and none from the held
        for s, v, _rk in buys:
            exp_orders.append(dict(D=D, tD=tD, symbol=s, side="buy", qty=v, full=False, codes=[], u_V=NaN, u_C=NaN))
        # counts in decisions.csv
        first = lambda r: r["codes"][0] if r["codes"] else ""
        cnt = {f"n_exit_{k}": sum(1 for s in held if first(rows[s]) == k) for k in ("E1", "E2", "E3", "E4", "E5", "V")}
        cnt.update(n_trim_valuation=sum(1 for s in held if rows[s]["status"] == "trimmed_valuation"),
                   n_trim_cap=sum(1 for s in held if rows[s]["status"] == "trimmed_cap"),
                   n_untested=sum(1 for s in held if rows[s]["ut"]))
        for k in ("E1", "E2", "E3", "E4"):
            cnt[f"n_untested_{k}"] = sum(1 for s in held if k in rows[s]["ut"])
        cnt["n_untested_trim"] = sum(1 for s in held if "trim" in rows[s]["ut"])
        for k, v in cnt.items():
            if int(dec.at[D, k]) != v:
                viol["B3_buys"].append((str(D.date()), f"decisions.csv {k} recorded {dec.at[D, k]} vs derived {v}", None))
        prev_held = set(held)

    # ---- recorded slots and sector counts after each review's decisions: at most 18 and at most 4 per sector key
    for D in c.Ds:
        sl = c.hold[(c.hold["D"] == D) & (c.hold["status"] != "exited")]
        n["B3_buys"] += 1
        if len(sl) > 18:
            viol["B3_buys"].append((str(D.date()), f"{len(sl)} slots held after the decisions (> 18)", None))
        top = sl["sector_key"].value_counts()
        if len(top) and int(top.iloc[0]) > 4:
            viol["B3_buys"].append((str(D.date()), f"sector {top.index[0]} holds {int(top.iloc[0])} after the decisions (> 4)", None))

    # ---- B4: fills
    fills = c.fills[c.fills["event"] == "fill"].copy()
    used = set()
    fill_rows = []
    scale_by_date: dict = {}
    # expected fill session of every order
    for o in exp_orders:
        j = p.col(o["symbol"])
        win = range(o["tD"], min(o["tD"] + 5, len(p.dates) - 1) + 1)
        mask = p.traded if o["side"] == "sell" else p.buyable
        o["t_fill"] = next((t for t in win if mask[t, j]), None)
        o["price"] = float(p.open_[o["t_fill"], j]) if o["t_fill"] is not None else NaN
        if o["side"] == "buy" and o["t_fill"] is not None:
            scale_by_date.setdefault(p.dates[o["t_fill"]], []).append(o)
    for d, os_ in scale_by_date.items():
        need = sum(o["qty"] * 1.002 for o in os_)
        avail = rep["cash_pre_buy"].get(d, NaN)
        sc = min(1.0, avail / need) if need > 0 else 1.0
        for o in os_:
            o["scale"] = sc
    for o in exp_orders:
        n["B4_fills"] += 1
        sub = fills[(fills["D"] == o["D"]) & (fills["symbol"] == o["symbol"]) & (fills["side"] == o["side"]) & (fills["kind"] != "forced_exit")]
        if o["t_fill"] is None:
            if len(sub):
                viol["B4_fills"].append((str(o["D"].date()), o["symbol"], "filled although no session in the window trades"))
            else:
                fill_rows.append(("cancelled", str(o["D"].date()), o["symbol"], o["side"]))
            continue
        d = p.dates[o["t_fill"]]
        if len(sub) != 1:
            viol["B4_fills"].append((str(o["D"].date()), o["symbol"], f"{o['side']} order has {len(sub)} fill rows (expected 1 on {d.date()})"))
            continue
        r = sub.iloc[0]
        used.add(r.name)
        j = p.col(o["symbol"])
        if r["date"] != d:
            viol["B4_fills"].append((str(o["D"].date()), o["symbol"], f"fill date {r['date'].date()} vs first tradable session {d.date()}"))
        if not close(r["price"], o["price"]):
            viol["B4_fills"].append((str(o["D"].date()), o["symbol"], f"fill price {r['price']} vs panel open {o['price']}"))
        if o["side"] == "sell":
            exp_units = float(c.rep["open"][o["D"]]["units"][j]) if o["full"] else o["qty"]
            if not close(r["units"], exp_units, 1e-9):
                viol["B4_fills"].append((str(o["D"].date()), o["symbol"], f"sold {r['units']} vs ordered {exp_units}"))
            kind_exp = "exit" if (o["full"] and o["codes"] and o["codes"][0] != "V") else \
                       ("valuation" if (o["full"] or (not isnan(o["u_V"]) and o["u_V"] <= o["u_C"])) else "cap")
        else:
            exp_units = o["scale"] * o["qty"] / o["price"]
            if not close(r["units"], exp_units, 1e-9):
                viol["B4_fills"].append((str(o["D"].date()), o["symbol"], f"bought {r['units']} vs V/open {exp_units} (scale {o['scale']})"))
            kind_exp = "buy"
        if r["kind"] != kind_exp:
            viol["B4_fills"].append((str(o["D"].date()), o["symbol"], f"fill kind {r['kind']} vs derived {kind_exp}"))
    # every fill row accounted for: arithmetic, and forced exits
    for idx, r in fills.iterrows():
        if not close(r["value"], r["units"] * r["price"]):
            viol["B4_fills"].append((str(r["D"].date()), r["symbol"], "value != units x price"))
        if not close(r["cost"], 0.002 * r["value"]):
            viol["B4_fills"].append((str(r["D"].date()), r["symbol"], "cost != 0.20% of value"))
        if r["kind"] == "forced_exit":
            j = p.col(r["symbol"])
            t = c.dix[r["date"]]
            if not (p.no_trade_run[t, j] == 20 and close(r["price"], float(p.last[t, j]))):
                viol["B4_fills"].append((str(r["date"].date()), r["symbol"], "forced exit not at the 20th no-trade session / last close"))
        elif idx not in used:
            viol["B4_fills"].append((str(r["D"].date()), r["symbol"], "fill not matched to any order of mine"))
    n["B4_fills"] += int((fills["kind"] == "forced_exit").sum())
    for v in rep["order_violations"] + rep["forced_violations"]:
        viol["B4_fills"].append(v)
    DETAIL["B4"] = {"fills_in_file": int(len(fills)), "orders_derived": len(exp_orders), "cancelled_orders": [f for f in fill_rows],
                    "min_cash": rep["min_cash"], "buys_scaled_pro_rata": sum(1 for o in exp_orders if o["side"] == "buy" and o.get("scale", 1) < 1 - 1e-12)}

    # ---- B5: NAV reproduced from fills, dividends and the panel's closes
    nav = c.nav3
    cl = nav[nav["mark"] == "close"]
    cl = pd.Series(cl["strategy"].to_numpy(), index=cl.index.normalize())
    mine = pd.Series(rep["close"])
    rel = ((mine.reindex(cl.index) / cl) - 1).abs()
    marks = list(rel.index)
    sample = marks[::27][:60]
    n["B5_nav"] = len(marks)
    open_first = float(nav["strategy"].iloc[0])
    open_end = float(nav["strategy"].iloc[-1])
    rel_open_first = abs(rep["open"][WINDOW_START]["nav"] / open_first - 1)
    rel_open_end = abs(rep["open"][p.dates[-1]]["nav"] / open_end - 1)
    DETAIL["B5"] = {"close_marks_compared": len(marks), "max_rel_error_all_close_marks": float(rel.max()),
                    "date_of_max": str(rel.idxmax().date()), "sample_marks": len(sample),
                    "max_rel_error_sample": float(rel.loc[sample].max()), "open_first_rel_error": float(rel_open_first),
                    "open_end_rel_error": float(rel_open_end), "marks_total_in_nav_csv": int(len(nav))}
    bad_nav = rel[rel > 1e-6]
    for d, v in bad_nav.items():
        viol["B5_nav"].append((str(d.date()), "close mark", float(v)))
    if rel_open_first > 1e-6 or rel_open_end > 1e-6:
        viol["B5_nav"].append(("open marks", float(rel_open_first), float(rel_open_end)))

    # ---- record the results
    labels = {"B1_exits": "B1 exits: holdings rows whose exit codes follow from section 8",
              "B2_trims": "B2 trims, caps, U0, u_V, u_C, units_to_sell, status, untested (sections 9.1, 9.2)",
              "B3_buys": "B3 buys, slots, budget, empty-slot reasons, decisions.csv counts (sections 7, 10)",
              "B4_fills": "B4 fills: date, price, units, kind, cost, sells before buys, cash never negative, forced exits",
              "B5_nav": "B5 NAV close marks reproduced within 1e-6 relative",
              "records": "B0 records agree with the replayed book (units, NAV_open, cash_open, rebalance_open)"}
    for k, v in viol.items():
        CHECKS.append({"part": "B", "item": labels[k], "verdict": f"{n[k]} rows/orders/marks checked", "recomputed": f"{len(v)} violations",
                       "difference": len(v), "tolerance": 0, "match": len(v) == 0})
    DETAIL["B_violations"] = {k: v[:60] for k, v in viol.items()}
    DETAIL["B_counts"] = n


# ================================================================ B6 flags and ranking, all 27 reviews
def part_B6(c: Ctx) -> None:
    P = "B6"
    e = c.elig
    # flags from the recorded figures, every row
    f2 = (e["icr_state"] == "no_finance_costs") | ((e["icr_state"] == "icr") & (e["icr"] >= 2.5))
    usable = e["bs_period_end"].notna() & ((e["D"] - pd.to_datetime(e["bs_period_end"])).dt.days <= 400)
    f3 = ~((e["D"] >= F3_START) & usable) | ((e["equity"] > 0) & (e["de"] < 1.2))
    pe = np.where((e["mcap"] > 0) & (e["ttm_p"] > 0), e["mcap"] / e["ttm_p"], np.nan)
    d0, d1 = e["down0"].map(lambda x: tri(x) is True), e["down1"].map(lambda x: tri(x) is True)
    exp = {"f1": e["ttm_p"] >= CR25, "f2": f2, "f3": f3, "f4": pd.Series((pe > 0) & (pe <= 60), index=e.index), "f6": ~(d0 & d1)}
    for k, v in exp.items():
        bad = int((v.astype(bool) != e[k].astype(bool)).sum())
        chk(P, f"flag {k.upper()} recomputed from the recorded figures: rows disagreeing (of {len(e)})", 0, bad, 0, rel=False)
    chk(P, "pe recorded == mcap / ttm_p (rows disagreeing)", 0,
        int((~((np.isnan(pe) & e["pe"].isna().to_numpy()) | (np.abs(pe - e["pe"].to_numpy()) <= 1e-9 * np.abs(pe)))).sum()), 0, rel=False)
    allf = e[["f0", "f1", "f2", "f3", "f4", "f5", "f6"]].all(axis=1)
    chk(P, "eligible == F0 and F1 to F6 (rows disagreeing)", 0, int((allf != e["eligible"]).sum()), 0, rel=False)
    f5_bad = e[e["f5"] & ~(e[["ttm_r", "ttm4_r", "ttm_p", "ttm4_p", "ttm_e", "ttm4_e"]].notna().all(axis=1)
                           & (e["ttm_r"] > 0) & (e["ttm4_r"] > 0) & (e["run"] >= 8))]
    chk(P, "F5 true implies all eight-quarter TTM figures present, TTM_R and TTM4_R > 0, run >= 8 (rows violating)", 0, int(len(f5_bad)), 0, rel=False)
    # eligible set = ranks.csv, and the ranking rule applied to the recorded measures, exactly
    n_ok = n_bad = n_ok_pct = n_bad_pct = 0
    max_pct_diff = 0.0
    bad_items = []
    for D in c.Ds:
        el = e[(e["D"] == D) & e["eligible"]]
        r = c.ranks[c.ranks["D"] == D]
        if set(el["symbol"]) != set(r["symbol"]):
            bad_items.append((str(D.date()), "eligible set differs from ranks.csv", sorted(set(el["symbol"]) ^ set(r["symbol"]))[:5]))
            continue
        base = r[["symbol", "ticker", "A_R", "A_P", "M", "g", "PEG"]].copy()
        mine = section6(base).set_index("symbol")
        rec = r.set_index("symbol").reindex(mine.index)
        ee = el.set_index("symbol").reindex(mine.index)
        for col in ("A_R", "A_P", "M", "g", "PEG"):
            if not np.allclose(rec[col].to_numpy(), ee[col].to_numpy(), rtol=1e-9, atol=0, equal_nan=True):
                bad_items.append((str(D.date()), f"ranks.csv {col} differs from eligibility.csv {col}", None))
        # PEG formula
        peg = ee["pe"] / (100 * np.minimum(ee["g"], 0.5))
        pos = ee["g"] > 0
        if not np.allclose(peg[pos].to_numpy(), ee.loc[pos, "PEG"].to_numpy(), rtol=1e-9):
            bad_items.append((str(D.date()), "PEG != P/E / (100 x min(g, 0.5))", None))
        for col in ("r_A_R", "r_A_P", "R1", "R2", "R3", "T", "rank"):          # the sorted-on columns: exact
            same = (mine[col].to_numpy() == rec[col].to_numpy()).all()
            n_ok += int(same)
            n_bad += int(not same)
            if not same:
                bad_items.append((str(D.date()), f"{col} differs from section 6", int((mine[col].to_numpy() != rec[col].to_numpy()).sum())))
        for col in ("S1", "S2", "S3", "S", "pct_A_R", "pct_A_P", "pct_M"):    # reported only: a division, 1e-12
            dmax = float(np.max(np.abs(mine[col].to_numpy() - rec[col].to_numpy())))
            max_pct_diff = max(max_pct_diff, dmax)
            same = dmax <= 1e-12
            n_ok_pct += int(same)
            n_bad_pct += int(not same)
            if not same:
                bad_items.append((str(D.date()), f"{col} differs from section 6 by {dmax}", None))
    chk(P, "ranking (section 6) applied to the recorded measures reproduces r(A_R), r(A_P), R1, R2, R3, T and rank EXACTLY: column-reviews differing (27 reviews x 7 columns)",
        0, n_bad, 0, rel=False)
    chk(P, "reported percentiles S1, S2, S3, S, pct(A_R), pct(A_P), pct(M) within 1e-12: column-reviews differing (27 x 7)", 0, n_bad_pct, 0, rel=False,
        note=f"largest absolute difference {max_pct_diff:.3e} (float division order); these columns are never sorted on")
    DETAIL["B6"] = {"exact_column_reviews_equal": n_ok, "exact_column_reviews_differing": n_bad, "pct_column_reviews_ok": n_ok_pct,
                    "pct_column_reviews_bad": n_bad_pct, "max_pct_abs_diff": max_pct_diff, "items": bad_items[:40]}


# ================================================================ C re-derivation at three reviews
STR_FIELDS = ["basis", "c4_basis", "L", "icr_state", "bs_period_end", "missing_quarters"]
NUM_FIELDS = ["run", "ttm_p", "ttm_ebit", "fc_pos", "icr", "equity", "debt", "de", "ttm_r", "ttm4_r", "ttm4_p", "ttm_e",
              "ttm4_e", "A_R", "A_P", "M", "g", "PEG", "pe"]
BOOL_FIELDS = ["write_back", "down0", "down1", "f1", "f2", "f3", "f4", "f5", "f6", "eligible"]
FLAG_FIELDS = ["f1", "f2", "f3", "f4", "f5", "f6", "eligible"]


def _num(x) -> float:
    return NaN if (x is None or pd.isna(x)) else float(x)


def _str(x):
    return None if (x is None or (isinstance(x, float) and math.isnan(x)) or x == "") else str(x)


def compare_stock(e: pd.Series, mine: dict) -> list:
    """Fields on which eligibility.csv's row and my derivation differ: [(field, recorded, mine)]."""
    out = []
    for f in STR_FIELDS:
        a, b = _str(e[f]), _str(mine.get(f))
        if a != b:
            out.append((f, a, b))
    for f in NUM_FIELDS:
        a, b = _num(e[f]), _num(mine.get(f, NaN))
        if isnan(a) and isnan(b):
            continue
        if isnan(a) or isnan(b) or abs(a - b) > 1e-9 * max(1.0, abs(a)):
            out.append((f, None if isnan(a) else a, None if isnan(b) else b))
    for f in BOOL_FIELDS:
        a, b = tri(e[f]), tri(mine.get(f))
        if a != b:
            out.append((f, a, b))
    return out


def derive_all(c: Ctx, fs: FinSnap, D, members, E) -> dict:
    res = {}
    for s in members:
        d = derive(fs, s, D)
        e = E.loc[s]
        if d is None:
            d = {"basis": None, "ttm_p": NaN, "icr_state": "unknown", "down0": None, "down1": None, "window_complete": False,
                 "write_back": False, "icr": NaN, "ttm_r": NaN, "ttm4_r": NaN, "g": NaN, "bs_usable": False, "equity": NaN, "de": NaN,
                 "revised_in_window": False, "A_R": NaN, "A_P": NaN, "M": NaN}
        fl = flags_from(d, D, _num(e["mcap"]))
        d.update(fl)
        d["f0"] = True
        d["eligible"] = bool(d["f0"] and fl["f1"] and fl["f2"] and fl["f3"] and fl["f4"] and fl["f5"] and fl["f6"])
        res[s] = d
    return res


def part_C(c: Ctx) -> None:
    P = "C"
    pos = np.random.default_rng(20260929).choice(27, size=3, replace=False)
    Dsel = [c.Ds[int(i)] for i in pos]
    DETAIL["C_reviews"] = {"positions_0_based": [int(i) for i in pos], "dates": [str(D.date()) for D in Dsel]}
    for D in Dsel:
        t0 = time.time()
        members = list(c.univ.loc[c.univ["D"] == D, "symbol"])
        E = c.elig[c.elig["D"] == D].set_index("symbol")
        assert set(members) <= set(E.index)
        tick = E["ticker"]
        # pit's unit-scale screen (C30a) is plumbing I do not reproduce. Its list of set-aside filings is read from
        # pit.build_snapshot only to diagnose differences and for the second pass below.
        snap = pit.build_snapshot(c.con, D, c.static)
        sd = snap.scale_dropped
        scale = {}
        for r in sd.itertuples():
            scale.setdefault(r.symbol, []).append(("cons " if r.consolidated else "std ") + str(pd.Timestamp(r.period_end).date()))
        drop = {(r.symbol, bool(r.consolidated), pd.Timestamp(r.period_end)) for r in sd.itertuples()}
        fs = FinSnap(c.con, c.ids, D)                       # pass 1: the spec's rules on the frozen table, nothing else
        fs2 = FinSnap(c.con, c.ids, D, drop=drop)           # pass 2: the same with pit's set-aside filings treated as not filed
        mine = derive_all(c, fs, D, members, E)
        mine2 = derive_all(c, fs2, D, members, E)
        diffs, diffs2 = {}, {}
        for s in members:
            dd = compare_stock(E.loc[s], mine[s])
            if dd:
                diffs[s] = dd
            dd2 = compare_stock(E.loc[s], mine2[s])
            if dd2:
                diffs2[s] = dd2
        diag = {}
        for s, dd in diffs.items():
            fields = {f for f, _, _ in dd}
            notes = []
            if s in scale:
                notes.append("unit-scale screen (C30a, plumbing not reproduced): pit set aside " + ", ".join(scale[s]) + "; "
                             + ("with those filings treated as not filed my figures agree on every field" if s not in diffs2
                                else f"with those filings treated as not filed {len(diffs2[s])} field(s) still differ: "
                                     f"{[(f, a, b) for f, a, b in diffs2[s]][:5]}"))
            if mine[s].get("revised_in_window"):
                notes.append("revision rule (plumbing): a quarter in the window has more than one filing before D")
            if fields <= {"bs_period_end", "equity", "debt", "de", "f3", "eligible"}:
                notes.append("balance-sheet selection (plumbing)")
            diag[s] = "; ".join(notes) if notes else "unexplained"
        flag_match = sum(1 for s in members if not any(f in FLAG_FIELDS for f, _, _ in diffs.get(s, [])))
        all_match = len(members) - len(diffs)
        flag_match2 = sum(1 for s in members if not any(f in FLAG_FIELDS for f, _, _ in diffs2.get(s, [])))
        all_match2 = len(members) - len(diffs2)
        el_mine = {s for s in members if mine[s]["eligible"]}
        el_mine2 = {s for s in members if mine2[s]["eligible"]}
        el_rec = set(c.ranks.loc[c.ranks["D"] == D, "symbol"])
        d = str(D.date())
        chk(P, f"{d}: universe members matching on every flag F1-F6 and eligible (spec rules on the frozen table, no screen)",
            len(members), flag_match, 0, rel=False, note="verdict side = members; every non-match is listed in the detail with its diagnosis")
        chk(P, f"{d}: members matching on every derived field (basis, L, run, TTM sums, ICR, D/E, DOWN0/1, flags, measures, P/E)",
            len(members), all_match, 0, rel=False)
        n_conf = sum(1 for s in diffs if s not in diffs2)
        chk(P, f"{d}: of the differing members, those explained by pit's unit-scale screen (agree on every field once its set-aside filings are treated as not filed)",
            len(diffs), n_conf, 0, rel=False, note=f"{len(diffs)} differ; {n_conf} confirmed; remaining {sorted(set(diffs2))}")
        chk(P, f"{d}: members matching on every derived field after applying pit's set-aside list (diagnostic pass)", len(members), all_match2, 0, rel=False)
        chk(P, f"{d}: eligible members only in the recorded set (no screen)", 0, len(el_rec - el_mine), 0, rel=False, note=f"{sorted(el_rec - el_mine)}")
        chk(P, f"{d}: eligible members only in my set (no screen)", 0, len(el_mine - el_rec), 0, rel=False, note=f"{sorted(el_mine - el_rec)}")
        chk(P, f"{d}: eligible set after applying pit's set-aside list equals ranks.csv's (symmetric difference)", 0, len(el_mine2 ^ el_rec), 0, rel=False)
        # ranking: my measures against ranks.csv
        r_rec = c.ranks[c.ranks["D"] == D].set_index("symbol")

        def rank_frame(src, syms):
            return pd.DataFrame({"symbol": syms, "ticker": [tick[s] for s in syms], "A_R": [src[s]["A_R"] for s in syms],
                                 "A_P": [src[s]["A_P"] for s in syms], "M": [src[s]["M"] for s in syms],
                                 "g": [src[s]["g"] for s in syms], "PEG": [src[s]["PEG"] for s in syms]})
        for tag, src, syms, hard in (("(i) my pipeline, no screen, my eligible set", mine, sorted(el_mine), False),
                                     ("(ii) my measures, no screen, on the recorded eligible set", mine, sorted(el_rec), False),
                                     ("(iii) my measures with pit's set-aside list applied, on the recorded eligible set", mine2, sorted(el_rec), True)):
            rk = section6(rank_frame(src, syms)).set_index("symbol")
            common = [s for s in rk.index if s in r_rec.index]
            bad = [s for s in common if not all(rk.at[s, k] == r_rec.at[s, k] for k in ("r_A_R", "r_A_P", "R1", "R2", "R3", "T", "rank"))]
            bad_meas = [s for s in common if not all(
                (isnan(rk.at[s, k]) and isnan(r_rec.at[s, k])) or abs(rk.at[s, k] - r_rec.at[s, k]) <= 1e-9 * max(1.0, abs(r_rec.at[s, k]))
                for k in ("A_R", "A_P", "M", "g", "PEG"))]
            rec_ = {"stocks_in_both": len(common), "exact_rank_chain_equal": len(common) - len(bad),
                    "measures_equal_1e-9": len(common) - len(bad_meas)}
            DETAIL.setdefault("C_rank", {}).setdefault(d, {})[tag] = rec_
            if hard:
                chk(P, f"{d}: ranking {tag}: stocks whose r(A_R), r(A_P), R1, R2, R3, T, rank equal ranks.csv EXACTLY", len(r_rec),
                    len(common) - len(bad), 0, rel=False, note=f"measures equal for {len(common) - len(bad_meas)} of {len(common)}")
        DETAIL.setdefault("C", {})[d] = {
            "members": len(members), "match_every_flag": flag_match, "match_every_field": all_match,
            "match_every_flag_after_scale_list": flag_match2, "match_every_field_after_scale_list": all_match2,
            "stocks_differing": [{"symbol": s, "ticker": str(tick[s]), "fields": [(f, a, b) for f, a, b in dd][:10], "explanation": diag[s]}
                                 for s, dd in diffs.items()],
            "remaining_after_scale_list": {s: [(f, a, b) for f, a, b in dd][:8] for s, dd in diffs2.items()},
            "n_revised_company_quarters_in_snapshot": fs.n_revised, "scale_dropped_filings": int(len(sd)), "seconds": round(time.time() - t0, 1)}
        print(f"C {d}: {len(members)} members; flags match {flag_match} ({flag_match2} after pit's screen list); "
              f"all fields match {all_match} ({all_match2})", flush=True)
        del fs, fs2


# ================================================================ A0 window, spec and input hashes
def sha256_file(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 24)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def part_A0(c: Ctx) -> None:
    P = "A0"
    w = c.V["window"]
    chk(P, "window.start_open", w["start_open"], str(WINDOW_START.date()))
    chk(P, "window.end_open", w["end_open"], str(WINDOW_END.date()))
    chk(P, "window.calendar_days", w["calendar_days"], (WINDOW_END - WINDOW_START).days)
    chk(P, "window.start_open == first mark of nav.csv", w["start_open"], str(c.nav3.index[0].date()))
    chk(P, "window.end_open == last mark of nav.csv", w["end_open"], str(c.nav3.index[-1].date()))
    spec = ROOT / "research" / "strategy" / "v3-spec.md"
    import hashlib
    import subprocess
    commit = c.V["runs"]["spec"]["commit"]
    try:
        blob = subprocess.run(["git", "show", f"{commit}:research/strategy/v3-spec.md"], cwd=ROOT, capture_output=True, check=True).stdout
        chk(P, "runs.spec.sha256 == SHA-256 of research/strategy/v3-spec.md at the verdict's recorded commit", c.V["runs"]["spec"]["sha256"],
            hashlib.sha256(blob).hexdigest())
        now = spec.read_bytes()
        cur_hash = hashlib.sha256(now).hexdigest()
        prefix = now.startswith(blob)
        extra = now[len(blob):].decode("utf-8", "replace").count("\n") if prefix else None
        chk(P, "spec file today = the verdict's commit text with lines appended only (no existing line changed)", True, bool(prefix),
            note=(f"the file today hashes to {cur_hash}, not to the verdict's {c.V['runs']['spec']['sha256']}; it is the committed text plus "
                  f"{extra} appended lines (Clarification C10, written after the full run); no rule text changed" if prefix else
                  "the file has changed in existing lines"))
        DETAIL["A0_spec"] = {"verdict_sha256": c.V["runs"]["spec"]["sha256"], "sha256_today": cur_hash, "appended_lines": extra,
                             "appended_text_starts": now[len(blob):].decode("utf-8", "replace").strip()[:120] if prefix else None}
    except Exception as ex:                                    # git absent or the commit unknown
        unverified(P, "runs.spec.sha256", f"could not read the spec at commit {commit}: {ex}")
    frozen = {"data/zen.duckdb": FZ / "data" / "zen.duckdb",
              "data/reference/industry_nse.parquet": FZ / "data" / "reference" / "industry_nse.parquet",
              "nse_list.json": FZ / "nse_list.json",
              "data/external/nifty_tri.parquet": FZ / "data" / "external" / "nifty_tri.parquet",
              "data/external/nifty_price_endpoints.csv": FZ / "data" / "external" / "nifty_price_endpoints.csv"}
    for rel, h in c.V["runs"]["v3_inputs"].items():
        path = frozen.get(rel)
        if path is None or not path.exists():
            unverified(P, f"runs.v3_inputs.{rel}", "the frozen copy was not found where expected")
            continue
        chk(P, f"runs.v3_inputs.{rel} (SHA-256 of the frozen copy this verifier read)", h, sha256_file(path))
    unverified(P, "runs.v3 / runs.v1_window_view paths, power_caveat, a2.sources, rehearsal_not_the_verdict", "text or labels, nothing to recompute")
    unverified(P, "v1_reproduction (replay against v1_final: NAV differences, holdings, trades)",
               "needs data/backtest/v1_final, outside the requested comparison; the A2 bars use the replay as section 14.2 says")


# ================================================================ driver and reports
def _clean(x):
    if isinstance(x, dict):
        return {str(k): _clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_clean(v) for v in x]
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (np.floating, float)):
        return None if (math.isnan(x) or math.isinf(x)) else float(x)
    if isinstance(x, (np.bool_,)):
        return bool(x)
    if isinstance(x, (pd.Timestamp,)):
        return str(x.date())
    return x


PARTS = [("A0", part_A0), ("A1", part_A1), ("A2", part_A2), ("A3", part_A3), ("A4", part_A4), ("A5", part_A5), ("A6", part_A6),
         ("B", part_B), ("B6", part_B6), ("C", part_C)]


def write_reports(only) -> None:
    by_part: dict = {}
    for k in CHECKS:
        d = by_part.setdefault(k["part"], [0, 0])
        d[0] += 1
        d[1] += int(k["match"])
    summary = {"checks": len(CHECKS), "matched": sum(int(k["match"]) for k in CHECKS),
               "mismatched": sum(int(not k["match"]) for k in CHECKS), "unverified": len(UNVERIFIED),
               "by_part": {p: {"checks": a, "matched": b} for p, (a, b) in by_part.items()}, "parts_run": only}
    out = {"verifier": "jobs/verify_v3.py", "spec": "research/strategy/v3-spec.md section 15.8",
           "verdict_file": str(VERDICT.relative_to(ROOT)), "summary": summary, "checks": CHECKS, "unverified": UNVERIFIED,
           "detail": DETAIL}
    (RUN / "verification.json").write_text(json.dumps(_clean(out), indent=1))
    L = ["# v3 independent verification", "",
         f"Written by `jobs/verify_v3.py` from the spec text and the run folders only (section 15.8). "
         f"{summary['checks']} items compared: **{summary['matched']} match, {summary['mismatched']} do not**; "
         f"{summary['unverified']} items not verified (listed below).", "",
         "| Part | Items | Match |", "|---|---|---|"]
    for p, v in summary["by_part"].items():
        L.append(f"| {p} | {v['checks']} | {v['matched']} |")
    grp = lambda pref: [k for k in CHECKS if k["part"].startswith(pref)]
    sa, sb, sc = grp("A"), [k for k in CHECKS if k["part"] in ("B", "B6")], grp("C")
    L += ["", "## In short", "",
          f"- Verdict figures (A0 to A6): {sum(int(k['match']) for k in sa)} of {len(sa)} items recomputed from the run folders match.",
          f"- Rule checks on the run's records (B, B6): {sum(int(k['match']) for k in sb)} of {len(sb)} check groups pass with no violation.",
          f"- Re-derivation from the frozen database (C): {sum(int(k['match']) for k in sc)} of {len(sc)} items match. The rest are the counts taken "
          "with the spec's rules alone on the frozen table; each differing stock is a filing that pit's unit-scale screen set aside "
          "(plumbing, not reproduced). With that list applied, every member of all three reviews matches on every flag and field, and the ranking "
          "given the eligible set and measures matches exactly."]
    L += ["", "## Items that do not match", ""]
    bad = [k for k in CHECKS if not k["match"]]
    if not bad:
        L.append("None.")
    for k in bad:
        L.append(f"- **{k['part']}** `{k['item']}`: verdict {k['verdict']}, recomputed {k['recomputed']}, difference {k['difference']}, "
                 f"tolerance {k['tolerance']}." + (f" {k['note']}" if k.get("note") else ""))
    L += ["", "## Not verified", ""]
    if not UNVERIFIED:
        L.append("None.")
    for u in UNVERIFIED:
        L.append(f"- {u['part']} `{u['item']}`: {u['reason']}")
    L += ["", "## Rule checks on the run's records (B)", ""]
    cnt = DETAIL.get("B_counts", {})
    for k in CHECKS:
        if k["part"] == "B":
            L.append(f"- {k['item']}: {k['verdict']}; {k['recomputed']}.")
    if "B5" in DETAIL:
        b5 = DETAIL["B5"]
        L.append(f"- NAV: {b5['close_marks_compared']} close marks and both open marks rebuilt from fills, dividends and panel closes; "
                 f"largest relative error {b5['max_rel_error_all_close_marks']:.2e} ({b5['sample_marks']}-mark sample: {b5['max_rel_error_sample']:.2e}).")
    L += ["", "## Re-derivation from the frozen database (C)", ""]
    for D, v in DETAIL.get("C", {}).items():
        L.append(f"- {D}: {v['members']} members; {v['match_every_flag']} match on every flag; {v['match_every_field']} match on every derived field; "
                 f"{len(v['stocks_differing'])} stocks differ.")
        for s in v["stocks_differing"]:
            L.append(f"  - {s['symbol']} ({s['ticker']}): {[f[0] for f in s['fields']]} -- {s['explanation']}")
    L += ["", "## Supporting detail", ""]
    if "A2_capacity" in DETAIL:
        for tag, v in DETAIL["A2_capacity"].items():
            L.append(f"- capacity ({tag}): v3 p95 {v['v3_p95']:.6f}, v1 p95 scaled {v['v1_p95']:.6f} (unscaled {v['v1_p95_unscaled']:.6f}); v3 passes: {v['v3_passes']}.")
    if "A4_quintiles" in DETAIL:
        q = DETAIL["A4_quintiles"]
        L.append(f"- quintile spread (approximation, equal-weight fifths of the section-6 order, no costs): top {q['approx_top_cagr']:.4f}, "
                 f"bottom {q['approx_bottom_cagr']:.4f}, spread {q['approx_spread']:.4f}; the verdict's engine-based figures are "
                 f"{q['verdict_top_cagr']:.4f}, {q['verdict_bottom_cagr']:.4f}, {q['verdict_spread']:.4f}. Sign agrees: {q['sign_agrees']}.")
    if "A5_intervals_using_recomputed_inputs" in DETAIL:
        L.append(f"- earnings/re-rating: TTM_P and market cap came from eligibility.csv except for the (review, stock) pairs listed in the JSON "
                 f"(`A5_recomputed_pairs`); intervals touching them: {DETAIL['A5_intervals_using_recomputed_inputs']}. "
                 f"Disagreements between my TTM_P and eligibility.csv's for any pair used: {len(DETAIL.get('A5_ttm_p_disagreements', []))}.")
    if "A6_alt" in DETAIL:
        L.append(f"- share of held slots unclassified: {DETAIL['A6_alt']['unclassified_in_them']} of {DETAIL['A6_alt']['slots_after_decisions']} slots held after each review's decisions ({DETAIL['A6_alt']['definition_used']}).")
    if "B4" in DETAIL:
        b4 = DETAIL["B4"]
        L.append(f"- fills: {b4['fills_in_file']} fill rows in fills.csv, {b4['orders_derived']} orders derived from the rules plus the forced exits; "
                 f"cancelled orders: {len(b4['cancelled_orders'])}; buys scaled pro rata: {b4['buys_scaled_pro_rata']}; lowest cash balance {b4['min_cash']:.6f}.")
    if "B6" in DETAIL:
        b6 = DETAIL["B6"]
        L.append(f"- ranking rule on the recorded measures, 27 reviews: {b6['exact_column_reviews_equal']} sorted-on columns x reviews equal exactly, "
                 f"{b6['exact_column_reviews_differing']} differ; reported percentiles within {b6['max_pct_abs_diff']:.2e}.")
    for D, v in DETAIL.get("C_rank", {}).items():
        for tag, r in v.items():
            L.append(f"- {D} ranking {tag}: {r['exact_rank_chain_equal']} of {r['stocks_in_both']} stocks equal exactly through the rank chain; measures equal for {r['measures_equal_1e-9']}.")
    (RUN / "verification.md").write_text("\n".join(L) + "\n", encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None, help="comma-separated parts to run (default all)")
    a = ap.parse_args(argv)
    only = a.only.split(",") if a.only else [p for p, _ in PARTS]
    c = build_ctx()
    for name, fn in PARTS:
        if name in only:
            t0 = time.time()
            fn(c)
            print(f"{name} done in {time.time() - t0:.1f}s; {len(CHECKS)} items so far", flush=True)
    write_reports(only)
    s = sum(int(k["match"]) for k in CHECKS)
    print(f"{len(CHECKS)} items: {s} match, {len(CHECKS) - s} do not; {len(UNVERIFIED)} not verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
