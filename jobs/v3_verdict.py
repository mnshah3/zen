"""The verdict on strategy v3 (research/strategy/v3-spec.md, section 14.7), from the measured runs.

    python -m jobs.v3_verdict [--v3 data/backtest/v3] [--v1 data/backtest/v1_window_2020]
                              [--v1-replay data/backtest/v1_replay_v3]
                              [--v1-final-window data/backtest/v1_final_window_2020]
                              [--out data/backtest]

Runs no backtest and no engine. It reads what the measurement jobs wrote into the run
folders (libcheck.json, attribution.json, verify.json, diagnostics.json, metrics.json) and
the runs' own records (nav.csv, trades.csv, fills.csv, episodes.csv, decisions.csv,
holdings.csv, eligibility.csv, rebalance_open.csv). The few figures computed here are the
ones the spec defines for the verdict alone: the A2 turnover (`turnover_a2`), the scaling
of v1's capacity, and the owner's rolling windows.

What it states, in the order section 14.7 fixes:

  1. the primary test (section 14.1) and its power caveat;
  2. whether v3 beat v1 on all five A2 bars over the same window, bar by bar, with the
     v1 reproduction check (section 14.2);
  3. whether "Nifty 500 plus 15" was met, for v3 and for v1 (section 14.3), with the 3, 6,
     12 and 24-month windows and the multibaggers;
  4. the rank IC and the quintile spread (section 14.4, from diagnostics.json);
  5. the earnings versus re-rating split (section 14.5, from diagnostics.json);
  and v3's own figures (section 14.6).

v1's side is the window view of v1's replay on the frozen inputs (section 14.2 and the
Clarification C4 of 12 October 2026). Where --v1-final-window is given, v1_final's window
figures are reported beside it.

Writes <out>/v3_verdict.json and <out>/v3_verdict.md.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from jobs import libcheck_v1 as lc

ROOT = Path(__file__).resolve().parents[1]
BACKTEST = ROOT / "data" / "backtest"

FIRST_REVIEW = pd.Timestamp("2020-02-17")
END_OPEN = pd.Timestamp("2026-09-18")
WINDOW_DAYS = 2405                       # 17 Feb 2020 to 18 Sep 2026, calendar days (section 14.2)
START_CAPITAL = 500_000.0
DD_MARGIN = 0.05                         # bar 1: five percentage points
PRIMARY_MONTHS = (70, "2020-03", "2025-12")     # section 14.1
T_CRITICAL = 1.645                       # one-sided 5%, normal-based (statsmodels HAC)
PLUS_15 = 0.15
WINDOW_MONTHS = (3, 6, 12, 24)
LAST_WINDOW_END = pd.Period("2026-08", freq="M")
POWER_CAVEAT = ("70 months can only detect a large alpha: about 8.2% a year half the time, or "
                "12.6% a year 80% of the time, at v1's residual volatility (section 14.1). "
                "Failing to reject H0 is not evidence that v3 has no edge; it means the sample "
                "is too short to tell.")


# ---------------------------------------------------------------- reading
def _json(run: Path, name: str) -> dict:
    p = Path(run) / name
    if not p.exists():
        raise FileNotFoundError(f"{p} is missing: run the measurement jobs on {run} first")
    return json.loads(p.read_text(encoding="utf-8"))


def _csv(run: Path, name: str, **kw) -> pd.DataFrame:
    p = Path(run) / name
    if not p.exists():
        raise FileNotFoundError(f"{p} is missing")
    return pd.read_csv(p, **kw)


def _strategy(run: Path) -> dict:
    """The file-based figures of one run that the A2 bars and the primary test read."""
    lib = _json(run, "libcheck.json")["full_period"]["strategy"]["exact"]
    att = _json(run, "attribution.json")
    q = att["quality"]["margin"]
    ver = _json(run, "verify.json")
    return {
        "max_drawdown": float(lib["max_drawdown"]), "sortino": float(lib["sortino"]),
        "calmar": float(lib["calmar"]), "cagr": float(lib["cagr_calendar"]),
        "months": (int(q["months"]), str(q["first"]), str(q["last"])),
        "alpha_annual": float(q["without_quality"]["exact"]["alpha_annual"]),
        "alpha_t": float(q["without_quality"]["exact"]["alpha_t"]),
        "alpha_annual_with_quality": float(q["with_quality"]["exact"]["alpha_annual"]),
        "alpha_t_with_quality": float(q["with_quality"]["exact"]["alpha_t"]),
        "capacity_p95": float(ver["capacity_all_trades"]["p95"]),
        "excess_over_nifty500": float(att["benchmarks"]["nifty500"]["whole_period"]["excess_cagr"]),
        "nifty500_cagr": float(att["benchmarks"]["nifty500"]["whole_period"]["benchmark_cagr"]),
        "sources": {"max_drawdown, sortino, calmar": "libcheck.json: full_period.strategy.exact",
                    "alpha": "attribution.json: quality.margin.without_quality.exact",
                    "alpha with quality": "attribution.json: quality.margin.with_quality.exact",
                    "capacity": "verify.json: capacity_all_trades.p95",
                    "Nifty 500": "attribution.json: benchmarks.nifty500.whole_period"}}


# ---------------------------------------------------------------- 14.1 the primary test
def primary_test(alpha_annual: float, alpha_t: float, months: tuple, strict: bool = True) -> dict:
    """H0: alpha <= 0 after IIMA's four factors plus the quality factor (margin), on the
    70 months March 2020 to December 2025. Rejected when the HAC t-statistic is above 1.645."""
    if strict and tuple(months) != PRIMARY_MONTHS:
        raise AssertionError(f"the primary test's months are {months}, not the {PRIMARY_MONTHS} "
                             "section 14.1 fixes")
    return {"h0": "alpha <= 0", "h1": "alpha > 0, one-sided, 5%",
            "months": list(months), "alpha_annual": alpha_annual, "alpha_t": alpha_t,
            "t_critical": T_CRITICAL, "reject_h0": bool(alpha_t > T_CRITICAL),
            "power_caveat": POWER_CAVEAT}


# ---------------------------------------------------------------- 14.2 the A2 bars
def turnover_a2(trades: pd.DataFrame, nav_marks: pd.Series,
                initial_buy_value: float = 0.0) -> float:
    """Section 14.2, bar 4: the value of the fills counted / 2 / the mean of all NAV marks
    in the window / (2,405 / 365.25).

    `trades` holds every buy and sell row of trades.csv dated in the window; for v3,
    `initial_buy_value` is the value of the fills of the buy orders placed at 17 February
    2020, which is left out. `nav_marks` is the strategy column of nav.csv, every mark."""
    t = trades[trades["side"].isin(["buy", "sell"])]
    d = pd.to_datetime(t["date"])
    t = t[(d >= FIRST_REVIEW) & (d <= END_OPEN)]
    counted = float(t["value"].abs().sum()) - float(initial_buy_value)
    return counted / 2 / float(np.mean(np.asarray(nav_marks, dtype=float))) / (WINDOW_DAYS / 365.25)


def initial_build_value(fills: pd.DataFrame) -> float:
    """The value of the fills of v3's buy orders placed at the first review (fills.csv)."""
    f = fills[(pd.to_datetime(fills["D"]) == FIRST_REVIEW) & (fills["kind"] == "buy")
              & (fills["event"].astype(str).str.lower().isin(["fill", "filled"]))]
    return float(f["value"].abs().sum())


def a2_bars(v1: dict, v3: dict) -> list[dict]:
    """A2's five bars from plain numbers. Each dict has max_drawdown, sortino, calmar,
    alpha_annual, alpha_t, months, turnover_a2 and capacity_p95 (v1's already at Rs 5 lakh).
    Refuses alphas measured over different months."""
    if tuple(v1["months"]) != tuple(v3["months"]):
        raise AssertionError(f"the alphas are not over the same months: v1 {v1['months']}, "
                             f"v3 {v3['months']}")
    bar = v1["max_drawdown"] + DD_MARGIN
    r1 = v3["max_drawdown"] >= bar
    so, ca = v3["sortino"] > v1["sortino"], v3["calmar"] > v1["calmar"]
    al, tt = v3["alpha_annual"] >= v1["alpha_annual"], v3["alpha_t"] >= v1["alpha_t"]
    tu = v3["turnover_a2"] <= v1["turnover_a2"]
    cp = v3["capacity_p95"] <= v1["capacity_p95"]

    def part(k, ok, **extra):
        return {k: {"v1": v1[k], "v3": v3[k], "passes": bool(ok), **extra}}

    return [
        {"bar": 1, "text": "maximum drawdown over the window at least 5 percentage points "
                           "smaller than v1's",
         "parts": part("max_drawdown", r1, v3_must_be_at_least=bar,
                       improvement_points=(v3["max_drawdown"] - v1["max_drawdown"]) * 100),
         "passes": bool(r1)},
        {"bar": 2, "text": "Sortino and Calmar (quantstats) both higher than v1's",
         "parts": {**part("sortino", so), **part("calmar", ca)}, "passes": bool(so and ca)},
        {"bar": 3, "text": "annual alpha after IIMA's four factors not lower than v1's over the "
                           "same months, and its t-statistic not lower",
         "parts": {**part("alpha_annual", al), **part("alpha_t", tt)},
         "months": list(v3["months"]), "passes": bool(al and tt)},
        {"bar": 4, "text": "annual one-way turnover (section 14.2's formula) not higher than v1's",
         "parts": part("turnover_a2", tu), "passes": bool(tu)},
        {"bar": 5, "text": "capacity not worse: the 95th percentile of trade value over the "
                           "stock's 60-session median turnover, both at Rs 5 lakh at the "
                           "window's start",
         "parts": part("capacity_p95", cp), "passes": bool(cp)},
    ]


# ---------------------------------------------------------------- 14.3 the owner's measures
def month_end_closes(clock: pd.DataFrame, col: str) -> pd.Series:
    """The value at the close of the last session of each calendar month (close marks only)."""
    c = clock[clock["mark"] == "close"][col].dropna()
    s = c.groupby(c.index.to_period("M")).last()
    s.index.name = "month"
    return s


def window_returns(me: pd.Series, m: int) -> pd.Series:
    """Returns of every m-month window between month-end closes, indexed by the end month:
    windows start from February 2020 and end on or before 31 August 2026."""
    out = {}
    for start in me.index:
        if start < pd.Period("2020-02", freq="M"):
            continue
        end = start + m
        if end > LAST_WINDOW_END or end not in me.index:
            continue
        out[end] = me[end] / me[start] - 1
    return pd.Series(out, dtype=float)


def windows(series: dict[str, pd.Series], bench: str = "nifty500", versus: str | None = "v1",
            of: str = "v3") -> dict:
    """Section 14.3's rolling windows. `series` maps a name to its month-end closes on one
    clock. For every series but the benchmark: the number of windows, the median, 10th and
    90th percentile of its return minus the benchmark's (percentage points, not annualised,
    numpy.percentile linear) and the share of windows it beat the benchmark (strictly
    greater). For `of` also the share it beat `versus`. For 12 and 24 months, the share of
    windows a series beat the benchmark by at least 15 points a year."""
    out = {}
    for m in WINDOW_MONTHS:
        r = {k: window_returns(v, m) for k, v in series.items()}
        idx = r[bench].index
        for k, v in r.items():
            if not v.index.equals(idx):
                raise AssertionError(f"{m}-month windows of {k} are not on the benchmark's clock")
        row = {"windows": int(len(idx))}
        for k, v in r.items():
            if k == bench:
                continue
            d = (v - r[bench]).to_numpy(float) * 100
            e = {"median_excess_points": float(np.median(d)) if len(d) else None,
                 "p10_excess_points": float(np.percentile(d, 10)) if len(d) else None,
                 "p90_excess_points": float(np.percentile(d, 90)) if len(d) else None,
                 "share_beat_benchmark": float((v > r[bench]).mean()) if len(d) else None}
            if m in (12, 24) and len(d):
                ann = (1 + v) ** (12 / m) - (1 + r[bench]) ** (12 / m)
                e["share_beat_benchmark_by_15_points_a_year"] = float((ann >= PLUS_15).mean())
            row[k] = e
        if versus and versus in r and of in r and len(idx):
            row[of][f"share_beat_{versus}"] = float((r[of] > r[versus]).mean())
        out[str(m)] = row
    return out


def multibaggers(episodes: pd.DataFrame) -> dict:
    d = episodes["doubled"].astype(str).str.lower().isin(["true", "1", "1.0"])
    return {"episodes": int(len(episodes)), "doubled_while_held": int(d.sum()),
            "share": float(d.mean()) if len(episodes) else None}


# ---------------------------------------------------------------- 14.6 v3's own figures
def v3_figures(dec: pd.DataFrame, hold: pd.DataFrame, fills: pd.DataFrame,
               elig: pd.DataFrame) -> dict:
    cash = dec["budget_left"].astype(float) / dec["nav_open"].astype(float)
    sells = fills[(fills["side"] == "sell")
                  & fills["event"].astype(str).str.lower().isin(["fill", "filled"])]
    trims = hold[hold["status"].isin(["trimmed_valuation", "trimmed_cap"])]
    trim_value = {}
    for kind in ("valuation", "cap"):
        keys = set(zip(trims[trims["status"] == f"trimmed_{kind}"]["D"].astype(str),
                       trims[trims["status"] == f"trimmed_{kind}"]["symbol"].astype(str)))
        f = sells[[k in keys for k in zip(sells["D"].astype(str), sells["symbol"].astype(str))]]
        trim_value[kind] = float(f["value"].abs().sum())
    held = hold[hold["status"].isin(["kept", "trimmed_valuation", "trimmed_cap", "new"])]
    unt = hold[hold["untested"].fillna("").astype(str) != ""]
    e = elig.copy()
    e["k"] = list(zip(e["D"].astype(str), e["symbol"].astype(str)))
    gap = set(e[e["missing_quarters"].fillna("").astype(str) != ""]["k"])
    unt_gap = sum(1 for k in zip(unt["D"].astype(str), unt["symbol"].astype(str)) if k in gap)
    by_test = {}
    for u in unt["untested"].astype(str):
        for x in u.split(";"):
            by_test[x] = by_test.get(x, 0) + 1
    reasons = {}
    for r in dec["empty_reasons"].fillna("").astype(str):
        for x in filter(None, r.split(";")):
            reasons[x] = reasons.get(x, 0) + 1
    return {
        "cash_share_after_the_planned_trades": {
            "mean": float(cash.mean()), "max": float(cash.max()),
            "by_review": {str(d)[:10]: float(c) for d, c in zip(dec["D"], cash)}},
        "holdings_at_open_by_review": {str(d)[:10]: int(n) for d, n in zip(dec["D"], dec["n_held_open"])},
        "exits_by_code": {c: int(dec[f"n_exit_{c}"].sum()) for c in ("E1", "E2", "E3", "E4", "E5", "V")},
        "trims": {"valuation": {"count": int(dec["n_trim_valuation"].sum()), "rupees": trim_value["valuation"]},
                  "cap": {"count": int(dec["n_trim_cap"].sum()), "rupees": trim_value["cap"]}},
        "slots_left_empty": {"total": int(dec["slots_left_empty"].sum()),
                             "reviews_with_any": int((dec["slots_left_empty"] > 0).sum()),
                             "reasons": reasons},
        "holdings_outside_v1_universe_by_review": {
            str(d)[:10]: int(n) for d, n in zip(dec["D"], dec["n_held_outside_universe"])},
        "holdings_with_an_untested_exit_or_trim": {
            "holding_reviews": int(len(unt)), "by_test": by_test,
            "with_a_quarter_missing_from_the_archive": int(unt_gap)},
        "share_of_held_slots_unclassified": (float((held["sector_key"] == "unclassified").mean())
                                             if len(held) else None)}


# ---------------------------------------------------------------- build
def build(v3_run: Path, v1_run: Path, v1_replay: Path | None = None,
          v1_final_window: Path | None = None, strict: bool = True) -> dict:
    """`strict=False` is for rehearsing the job on a development run (the agreement period):
    v1's clock is cut to the run's, and the checks that the months and the window are the
    spec's are not made. Its output is labelled and is not the verdict."""
    v3_run, v1_run = Path(v3_run), Path(v1_run)
    s3, s1 = _strategy(v3_run), _strategy(v1_run)
    c3, c1 = lc.load_clock(v3_run), lc.load_clock(v1_run)
    if not strict:
        c1 = c1[c1.index <= c3.index[-1]]
        if len(c1) == len(c3) - 1:               # v1's file has no open mark at the run's end
            c1 = c1.reindex(c3.index).ffill()
        s1["months"] = s3["months"]
    if not c3.index.equals(c1.index):
        raise AssertionError("v3 and v1's window view are not on the same clock")
    if strict and (c3.index[0].normalize() != FIRST_REVIEW or c3.index[-1].normalize() != END_OPEN):
        raise AssertionError(f"v3's clock runs {c3.index[0]} to {c3.index[-1]}, not the window "
                             f"{FIRST_REVIEW.date()} to {END_OPEN.date()}")
    m3 = _json(v3_run, "metrics.json")

    # bar 4
    fills = _csv(v3_run, "fills.csv")
    init = initial_build_value(fills)
    t3, t1 = _csv(v3_run, "trades.csv"), _csv(v1_run, "trades.csv")
    s3["turnover_a2"] = turnover_a2(t3, c3["strategy"], init)
    s1["turnover_a2"] = turnover_a2(t1, c1["strategy"])
    turnover_note = {"v3_with_its_initial_build": turnover_a2(t3, c3["strategy"]),
                     "v3_initial_build_value": init,
                     "v3_metrics_json_turnover_incl_initial": m3.get("turnover_annual_incl_initial")}
    # bar 5: v1's trade values at Rs 5 lakh at the window's start
    ro = _csv(v1_run, "rebalance_open.csv")
    v1_open = float(ro[pd.to_datetime(ro["date"]) == FIRST_REVIEW]["strategy_open"].iloc[0])
    scale = START_CAPITAL / v1_open
    s1_cap_raw = s1["capacity_p95"]
    s1["capacity_p95"] = s1_cap_raw * scale
    bars = a2_bars(s1, s3)

    # 14.3
    plus15 = {k: {"strategy_cagr": s["cagr"], "nifty500_cagr": s["nifty500_cagr"],
                  "excess": s["excess_over_nifty500"],
                  "met": bool(s["excess_over_nifty500"] >= PLUS_15)}
              for k, s in (("v3", s3), ("v1", s1))}
    series = {"v3": month_end_closes(c3, "strategy"), "v1": month_end_closes(c1, "strategy"),
              "nifty500": month_end_closes(c3, "nifty500"),
              "eligible_equal_weight": month_end_closes(c3, "universe_ew")}
    win = windows(series)
    win_v1 = windows({"v1": series["v1"], "nifty500": series["nifty500"]}, versus=None, of="v1")

    out = {
        "window": {"start_open": str(FIRST_REVIEW.date()), "end_open": str(END_OPEN.date()),
                   "calendar_days": WINDOW_DAYS},
        "runs": {"v3": str(v3_run), "v1_window_view": str(v1_run),
                 "spec": m3.get("spec"), "v3_inputs": m3.get("inputs", {}).get("checked_by_this_job")},
        "rehearsal_not_the_verdict": not strict,
        "primary_test": primary_test(s3["alpha_annual_with_quality"], s3["alpha_t_with_quality"],
                                     s3["months"], strict),
        "a2": {"bars": bars, "bars_passed": int(sum(b["passes"] for b in bars)),
               "v3_beats_v1_on_a2": bool(all(b["passes"] for b in bars)),
               "turnover": turnover_note,
               "capacity_scaling": {"v1_nav_at_the_open_of_17_feb_2020": v1_open, "scale": scale,
                                    "v1_p95_unscaled": s1_cap_raw},
               "v1_figures": {k: s1[k] for k in ("max_drawdown", "sortino", "calmar", "alpha_annual",
                                                 "alpha_t", "turnover_a2", "capacity_p95", "cagr")},
               "v3_figures": {k: s3[k] for k in ("max_drawdown", "sortino", "calmar", "alpha_annual",
                                                 "alpha_t", "turnover_a2", "capacity_p95", "cagr")},
               "sources": s3["sources"]},
        "nifty500_plus_15": plus15,
        "windows": win, "windows_v1": win_v1,
        # 14.6: the stationary bootstrap interval for v3's margin over v1 and over its own
        # eligible equal-weight benchmark, and the two equal-weight benchmarks' growth
        "margins": {"over_v1": lc.margin_interval(c3["strategy"], c1["strategy"]),
                    "over_eligible_equal_weight": lc.margin_interval(c3["strategy"],
                                                                     c3["universe_ew"])},
        "equal_weight": {"v3_eligible_list_cagr": lc.cagr(c3["universe_ew"].dropna()),
                         "v1_universe_cagr": lc.cagr(c1["universe_ew"].dropna())},
        "multibaggers": {"v3": multibaggers(_csv(v3_run, "episodes.csv")),
                         "v1": multibaggers(_csv(v1_run, "episodes.csv"))},
        "v3_figures": v3_figures(_csv(v3_run, "decisions.csv"), _csv(v3_run, "holdings.csv"),
                                 fills, _csv(v3_run, "eligibility.csv", dtype=str,
                                             keep_default_na=False)),
    }
    diag_p = v3_run / "diagnostics.json"
    if diag_p.exists():
        d = json.loads(diag_p.read_text(encoding="utf-8"))
        out["score_signal"] = d.get("score_signal")
        out["earnings_and_rerating"] = d.get("earnings_and_rerating")
    else:
        raise FileNotFoundError(f"{diag_p} is missing: run jobs.v3_diagnostics first")
    if v1_replay and (Path(v1_replay) / "reproduction.json").exists():
        r = _json(Path(v1_replay), "reproduction.json")
        out["v1_reproduction"] = {"identical": r.get("identical"), "nav": r.get("nav", {}).get("max_rel_diff"),
                                  "first_difference": r.get("first_difference"),
                                  "holdings": r.get("holdings"), "trades": r.get("trades"),
                                  "v1_side_is": "v1_final" if r.get("identical") else "the replay"}
    if v1_final_window and Path(v1_final_window).exists():
        f = _strategy(Path(v1_final_window))
        if not strict:
            f["months"] = s3["months"]
        cf = lc.load_clock(Path(v1_final_window))
        f["turnover_a2"] = turnover_a2(_csv(Path(v1_final_window), "trades.csv"), cf["strategy"])
        rof = _csv(Path(v1_final_window), "rebalance_open.csv")
        f["capacity_p95"] *= START_CAPITAL / float(
            rof[pd.to_datetime(rof["date"]) == FIRST_REVIEW]["strategy_open"].iloc[0])
        out["a2"]["v1_final_window_figures"] = {
            k: f[k] for k in ("max_drawdown", "sortino", "calmar", "alpha_annual", "alpha_t",
                              "turnover_a2", "capacity_p95", "cagr")}
        out["a2"]["bars_against_v1_final"] = [
            {"bar": b["bar"], "passes": b["passes"]} for b in a2_bars(f, s3)]
    return out


# ---------------------------------------------------------------- markdown
def pct(x, nd: int = 2, sign: bool = False) -> str:
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return "n/a"
    return f"{x * 100:{'+' if sign else ''}.{nd}f}%"


def num(x, nd: int = 2) -> str:
    return "n/a" if x is None or (isinstance(x, float) and not np.isfinite(x)) else f"{x:.{nd}f}"


def yes(b) -> str:
    return "yes" if b else "no"


def markdown(rep: dict) -> str:
    p, a = rep["primary_test"], rep["a2"]
    v1, v3 = a["v1_figures"], a["v3_figures"]
    L = ["# Strategy v3: the verdict" + (" (REHEARSAL ON A DEVELOPMENT RUN, NOT THE VERDICT)"
                                         if rep.get("rehearsal_not_the_verdict") else ""), "",
         f"Window: the open of {rep['window']['start_open']} to the open of "
         f"{rep['window']['end_open']}. v1 is its replay on the same frozen inputs, over the "
         "same window. Every rule and every test below was fixed in "
         "`research/strategy/v3-spec.md` before v3's results existed.", "",
         "## 1. The primary test", "",
         f"Alpha after IIMA's four factors and the quality factor, {p['months'][0]} months "
         f"({p['months'][1]} to {p['months'][2]}): **{pct(p['alpha_annual'], sign=True)} a year, "
         f"t = {num(p['alpha_t'])}**. H0 (alpha ≤ 0) is "
         f"**{'rejected' if p['reject_h0'] else 'not rejected'}** at the one-sided 5% level "
         f"(t above {p['t_critical']}).", "", p["power_caveat"], "",
         "## 2. Against v1 on the five A2 bars", "",
         f"**v3 {'beats' if a['v3_beats_v1_on_a2'] else 'does not beat'} v1 on A2: "
         f"{a['bars_passed']} of 5 bars passed.**", "",
         "| Bar | v1 | v3 | Passed |", "|---|---|---|---|",
         f"| 1. Maximum drawdown, 5 points smaller | {pct(v1['max_drawdown'])} | "
         f"{pct(v3['max_drawdown'])} | {yes(a['bars'][0]['passes'])} |",
         f"| 2. Sortino | {num(v1['sortino'])} | {num(v3['sortino'])} | "
         f"{yes(a['bars'][1]['parts']['sortino']['passes'])} |",
         f"| 2. Calmar | {num(v1['calmar'])} | {num(v3['calmar'])} | "
         f"{yes(a['bars'][1]['parts']['calmar']['passes'])} |",
         f"| 3. Four-factor alpha a year | {pct(v1['alpha_annual'], sign=True)} | "
         f"{pct(v3['alpha_annual'], sign=True)} | {yes(a['bars'][2]['parts']['alpha_annual']['passes'])} |",
         f"| 3. Its t-statistic | {num(v1['alpha_t'])} | {num(v3['alpha_t'])} | "
         f"{yes(a['bars'][2]['parts']['alpha_t']['passes'])} |",
         f"| 4. Turnover a year | {num(v1['turnover_a2'])}x | {num(v3['turnover_a2'])}x | "
         f"{yes(a['bars'][3]['passes'])} |",
         f"| 5. Capacity, 95th percentile | {pct(v1['capacity_p95'], 3)} | "
         f"{pct(v3['capacity_p95'], 3)} | {yes(a['bars'][4]['passes'])} |", "",
         f"CAGR over the window: v1 {pct(v1['cagr'])}, v3 {pct(v3['cagr'])}.", ""]
    r = rep.get("v1_reproduction")
    if r:
        L += [f"v1 reproduction check: the replay is "
              f"{'identical to' if r['identical'] else 'not identical to'} `v1_final`; v1's side "
              f"above is {r['v1_side_is']}.", ""]
    n = rep["nifty500_plus_15"]
    L += ["## 3. Nifty 500 plus 15 points", "",
          "| | CAGR | Nifty 500 TRI | Excess | Met |", "|---|---|---|---|---|"]
    for k in ("v3", "v1"):
        L.append(f"| {k} | {pct(n[k]['strategy_cagr'])} | {pct(n[k]['nifty500_cagr'])} | "
                 f"{pct(n[k]['excess'], sign=True)} | {yes(n[k]['met'])} |")
    L += ["", "Rolling windows (return minus the Nifty 500's, percentage points, not annualised):", "",
          "| Months | Windows | v3 median | v3 10th | v3 90th | v3 beat Nifty 500 | v3 beat v1 |",
          "|---|---|---|---|---|---|---|"]
    for m, w in rep["windows"].items():
        x = w["v3"]
        L.append(f"| {m} | {w['windows']} | {num(x['median_excess_points'])} | "
                 f"{num(x['p10_excess_points'])} | {num(x['p90_excess_points'])} | "
                 f"{pct(x['share_beat_benchmark'], 0)} | {pct(x.get('share_beat_v1'), 0)} |")
    mg, ew = rep["margins"], rep["equal_weight"]
    L += ["", f"v3's margin over v1: {mg['over_v1']['point_pct']:+.2f} points a year "
              f"(90% interval {mg['over_v1']['lo90_pct']:+.2f} to {mg['over_v1']['hi90_pct']:+.2f}). "
              f"Over its own eligible list held equal-weight: "
              f"{mg['over_eligible_equal_weight']['point_pct']:+.2f} points "
              f"({mg['over_eligible_equal_weight']['lo90_pct']:+.2f} to "
              f"{mg['over_eligible_equal_weight']['hi90_pct']:+.2f}). The eligible list equal-weight "
              f"grew {pct(ew['v3_eligible_list_cagr'])} a year; v1's universe equal-weight "
              f"{pct(ew['v1_universe_cagr'])}."]
    mb = rep["multibaggers"]
    L += ["", f"Doubled while held: v3 {mb['v3']['doubled_while_held']} of {mb['v3']['episodes']} "
              f"holding episodes, v1 {mb['v1']['doubled_while_held']} of {mb['v1']['episodes']}.", ""]
    s = rep.get("score_signal") or {}
    if s:
        ic = s["ic"]["T"]
        q = s["quintiles"]
        L += ["## 4. Does the score carry signal?", "",
              f"Rank IC of the score against the next review period's return, {ic['reviews']} reviews: mean "
              f"{num(ic['mean'], 4)}, t = {num(ic['t'])}, positive at {pct(ic['share_positive'], 0)} "
              "of reviews.",
              f"Top quintile minus bottom quintile: {pct(q['cagr_spread'], sign=True)} a year "
              f"(information ratio {num(q['information_ratio'])}).", ""]
    e = rep.get("earnings_and_rerating") or {}
    if e:
        L += ["## 5. Earnings growth and re-rating", "",
              "| | Earnings a year | Re-rating a year | Share from earnings | Remainder |",
              "|---|---|---|---|---|"]
        for k in ("v3", "v1"):
            if k in e:
                x = e[k]
                L.append(f"| {k} | {pct(x['earnings_annual'], sign=True)} | "
                         f"{pct(x['rerating_annual'], sign=True)} | {pct(x['share_from_earnings'], 0)} | "
                         f"{num(x['remainder'], 3)}x |")
        L.append("")
    f = rep["v3_figures"]
    L += ["## v3's own figures", "",
          f"- Cash after the planned trades: mean {pct(f['cash_share_after_the_planned_trades']['mean'])}, "
          f"maximum {pct(f['cash_share_after_the_planned_trades']['max'])} of NAV.",
          f"- Exits by code: {f['exits_by_code']}.",
          f"- Trims: {f['trims']['valuation']['count']} valuation, {f['trims']['cap']['count']} cap.",
          f"- Slots left empty: {f['slots_left_empty']['total']} "
          f"({f['slots_left_empty']['reasons'] or 'none'}).",
          f"- Holdings with a test left untested: "
          f"{f['holdings_with_an_untested_exit_or_trim']['holding_reviews']} holding-reviews, "
          f"{f['holdings_with_an_untested_exit_or_trim']['with_a_quarter_missing_from_the_archive']} "
          "of them with a quarter missing from the archive.", ""]
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--v3", default=str(BACKTEST / "v3"))
    ap.add_argument("--v1", default=str(BACKTEST / "v1_window_2020"), help="v1's window view")
    ap.add_argument("--v1-replay", default=str(BACKTEST / "v1_replay_v3"))
    ap.add_argument("--v1-final-window", default=None, help="v1_final's window view, if built")
    ap.add_argument("--out", default=str(BACKTEST))
    ap.add_argument("--rehearsal", action="store_true",
                    help="rehearse the job on a development run; the output is not the verdict")
    a = ap.parse_args(argv)
    rep = build(Path(a.v3), Path(a.v1), Path(a.v1_replay) if a.v1_replay else None,
                Path(a.v1_final_window) if a.v1_final_window else None, strict=not a.rehearsal)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "v3_verdict.json").write_text(json.dumps(rep, indent=1, default=str), encoding="utf-8")
    (out / "v3_verdict.md").write_text(markdown(rep), encoding="utf-8")
    print(f"primary test: alpha t = {rep['primary_test']['alpha_t']:.2f}, "
          f"H0 {'rejected' if rep['primary_test']['reject_h0'] else 'not rejected'}")
    print(f"A2: {rep['a2']['bars_passed']} of 5 bars; v3 beats v1: {rep['a2']['v3_beats_v1_on_a2']}")
    print(f"written {out / 'v3_verdict.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
