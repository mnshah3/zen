"""The verdict on strategy v2 (research/strategy/v2-spec.md), from the measured runs.

    python -m jobs.v2_verdict [--v1 data/backtest/v1_final] [--a data/backtest/v2a]
                              [--b data/backtest/v2b] [--out data/backtest]

Runs no backtest and no engine. Every figure is read from the files the
measurement jobs wrote into the three run folders (libcheck.json, verify.json,
attribution.json, after_tax/summary.json, a1.json, metrics.json, nav.csv, and
v1's baseline_report.json), each with the file and key it came from; the few
figures computed here (safeguard 2) are computed from the runs' own nav.csv and
a1.json, by the functions below.

  A1 adoption     the Clarification before the v2 build: (1) the V-weighted average
                  price paid for new positions, over the adjusted close on the
                  session before each position's decision date (a1.json
                  v_weighted_ratio), lower in (b) than in (a); (2) the full-period
                  CAGR of (b) not lower than (a)'s; (3) the full-period maximum
                  drawdown of (b) not worse than (a)'s. All three hold: (b) is
                  adopted; otherwise (a).
  A2 verdict      the adopted variant against v1_final, the five rules as the
                  Clarification to A2's measurement fixes them. v1's figures are
                  the ones data/backtest/v1_final/baseline_report.json records; v2's
                  are read from the same file and key in the adopted run's folder,
                  so both sides are the same quantity by construction. v2 replaces
                  v1 only if all five hold.
  safeguard 2     the Clarification before the v2 build: combinatorially symmetric
                  cross-validation of A1's choice (Bailey, Borwein, Lopez de Prado
                  and Zhu 2017); the stationary block bootstrap v1's libcheck used
                  (jobs/libcheck_v1.margin_interval) for v2's excess over v1 on the
                  same clock; every 3-year window's CAGR and maximum drawdown for v2
                  and v1 on the same dates.
  reported        the factor indices (A4), the IIMA alpha with and without the A5
                  quality factor, after costs and tax (A6), the monkey test and the
                  deflated Sharpe, each from the adopted run's files beside v1's.

Writes <out>/v2_verdict.json, <out>/v2_verdict.md and
<out>/v2_verdict_windows_3y.csv (one row per 3-year window).

Readings this job takes where the text leaves a detail open, fixed here and
written into the JSON (`readings`), none chosen by looking at a result:

  cscv_blocks     the 1,872 returns between consecutive marks of nav.csv (open of
                  the first decision date, every close, open of the end date) are
                  cut into 10 contiguous blocks with numpy.array_split (the first
                  two blocks one return longer).
  cscv_rule       on a half (5 blocks), rule 1 compares the V-weighted mean ratio
                  of the new positions whose decision date falls in the half (the
                  block of the return that ends at that date's close); rules 2 and
                  3 compare the compounded return and the maximum drawdown (the
                  starting value counting as a peak) of the half's returns joined in
                  time order, as CSCV joins its blocks. The CAGR comparison is the
                  compounded-return comparison: both variants have the same number
                  of returns in a half, so any annualisation keeps the order.
  cscv_worse      the pick does worse on the test half than the variant not picked
                  when the same A1 rule, applied to the test half, picks the other
                  variant (the rule is the measure of better that chose it). The
                  per-criterion outcomes are reported beside it.
  windows_3y      a window starts at every mark of the clock and ends at the last
                  mark dated on or before the same calendar date three years later
                  (pandas DateOffset(years=3)); windows ending after the last mark's
                  date are not formed. CAGR over calendar days (365.25), maximum
                  drawdown within the window from its first value.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from importlib.metadata import version
from itertools import combinations
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jobs import attribution as at  # noqa: E402
from jobs import baseline_report as br  # noqa: E402
from jobs import libcheck_v1 as lc  # noqa: E402
from zen.validation import trials  # noqa: E402

BACKTEST = ROOT / "data" / "backtest"
V1_RUN, A_RUN, B_RUN = BACKTEST / "v1_final", BACKTEST / "v2a", BACKTEST / "v2b"
N_BLOCKS, N_TRAIN = 10, 5
WINDOW_YEARS = 3
AGREE_TOL = 1e-9
DD_MARGIN = br.DD_MARGIN                       # A2 rule 1: 5 percentage points
SHORT_LIVE_YEARS = br.SHORT_LIVE_YEARS         # wording only, as in baseline_report

READINGS = {
    "cscv_blocks": "the returns between consecutive marks of nav.csv, cut into 10 contiguous "
                   "blocks by numpy.array_split",
    "cscv_rule": "rule 1 on the new positions whose decision date's close falls in the half "
                 "(V-weighted mean of a1.json's ratio); rules 2 and 3 on the half's returns "
                 "joined in time order: compounded return (same number of returns for both "
                 "variants, so any annualisation keeps the order) and maximum drawdown with "
                 "the starting value as a peak",
    "cscv_worse": "the pick does worse on the test half when the A1 rule applied to the test "
                  "half picks the other variant; per-criterion outcomes reported beside it",
    "windows_3y": "a window starts at every mark and ends at the last mark dated on or before "
                  "the same calendar date three years later; calendar-day CAGR (365.25), "
                  "maximum drawdown from the window's first value",
}


# ---------------------------------------------------------------- sources
class Sources:
    """The run folders' files, read once each, with their SHA-256 recorded."""

    def __init__(self, runs: dict[str, Path]):
        self.runs = {k: Path(v) for k, v in runs.items()}
        self._json: dict[str, dict] = {}
        self.files: dict[str, str] = {}

    def path(self, run: str, rel: str) -> Path:
        return self.runs[run] / rel

    def name(self, run: str, rel: str) -> str:
        return br.repo_path(self.path(run, rel))

    def _record(self, p: Path) -> None:
        if not p.exists():
            raise FileNotFoundError(f"{p}: run the measurement job first (jobs/v2_verdict.py)")
        self.files.setdefault(br.repo_path(p), br.sha256(p))

    def doc(self, run: str, rel: str) -> dict:
        p = self.path(run, rel)
        k = br.repo_path(p)
        if k not in self._json:
            self._record(p)
            self._json[k] = json.loads(p.read_text(encoding="utf-8"))
        return self._json[k]

    def fig(self, run: str, rel: str, key: str) -> dict:
        """{'value', 'source'}: the value at `key` of the run's file `rel`."""
        return {"value": br.pick(self.doc(run, rel), key), "source": f"{self.name(run, rel)}: {key}"}

    def clock(self, run: str) -> pd.DataFrame:
        self._record(self.path(run, "nav.csv"))
        return lc.load_clock(self.runs[run])


def _num(f: dict) -> float:
    return float(f["value"])


def check_equal(a: float, b: float, what: str, tol: float = AGREE_TOL) -> None:
    if not abs(float(a) - float(b)) <= tol:
        raise AssertionError(f"{what}: {a} and {b} differ")


# ---------------------------------------------------------------- A1
A1_RULES = (
    ("entry_price", "the average price paid for new positions, relative to the adjusted close "
                    "on the session before each position's decision date, is lower in (b)",
     "a1.json", "v_weighted_ratio"),
    ("cagr", "annual return (full-period CAGR) is not lower in (b)",
     "libcheck.json", "full_period.strategy.exact.cagr_calendar"),
    ("max_drawdown", "maximum drawdown (full period) is not worse in (b)",
     "libcheck.json", "full_period.strategy.exact.max_drawdown"),
)


def a1_holds(entry_a: float, entry_b: float, growth_a: float, growth_b: float,
             mdd_a: float, mdd_b: float) -> tuple[bool, bool, bool]:
    """A1's three conditions for (b): entry ratio lower, growth not lower, drawdown not
    worse (drawdowns are negative: not worse means not below)."""
    return bool(entry_b < entry_a), bool(growth_b >= growth_a), bool(mdd_b >= mdd_a)


def a1_pick(holds) -> str:
    return "b" if all(holds) else "a"


def a1_adoption(src: Sources) -> dict:
    rules, vals = [], {}
    for i, (name, text, rel, key) in enumerate(A1_RULES, start=1):
        a, b = src.fig("a", rel, key), src.fig("b", rel, key)
        vals[name] = (_num(a), _num(b))
        rules.append({"rule": i, "name": name, "text": text, "a": a, "b": b})
    # the same figures as each run's own metrics.json records them
    for run in ("a", "b"):
        m = src.doc(run, "metrics.json")
        check_equal(src.doc(run, "a1.json")["v_weighted_ratio"], m["a1"]["v_weighted_ratio"],
                    f"{run}: a1.json vs metrics.json a1.v_weighted_ratio")
        check_equal(vals["cagr"][0 if run == "a" else 1], m["cagr"], f"{run}: CAGR vs metrics.json")
        check_equal(vals["max_drawdown"][0 if run == "a" else 1], m["max_drawdown"],
                    f"{run}: max drawdown vs metrics.json")
    holds = a1_holds(*vals["entry_price"], *vals["cagr"], *vals["max_drawdown"])
    for r, h in zip(rules, holds):
        r["holds"] = h
        r["b_minus_a"] = _num(r["b"]) - _num(r["a"])
    pick = a1_pick(holds)
    for run in ("a", "b"):
        a1 = src.doc(run, "a1.json")
        if a1["n_without_fills"]:
            raise AssertionError(f"{run}: {a1['n_without_fills']} new positions without a fill")
    return {"definition": "research/strategy/v2-spec.md A1 and the Clarification before the v2 "
                          "build ('A1's adoption test, made exact'): (b) is adopted only if all "
                          "three hold, otherwise (a)",
            "rules": rules, "all_hold": all(holds), "adopted": pick,
            "adopted_run": br.repo_path(src.runs[pick]),
            "check": "rules 2 and 3 equal each run's metrics.json (cagr, max_drawdown) and rule 1 "
                     "its metrics.json a1.v_weighted_ratio, to 1e-9"}


# ---------------------------------------------------------------- A2
A2_ORDER = ("max_drawdown", "sortino", "calmar", "iima_alpha_annual", "iima_alpha_t",
            "turnover_annual_incl_initial", "capacity_p95_share_of_turnover")


def a2_figures(src: Sources, run: str) -> dict:
    """The five A2 quantities of `run`, each from the file and key v1's baseline read it
    from (baseline_report.json records them as 'file: key')."""
    base = src.doc("v1", "baseline_report.json")["a2_acceptance_quantities"]
    out = {}
    for q in A2_ORDER:
        rel, key = base[q]["source"].split(": ")
        out[q] = src.fig(run, rel, key)
    return out


def a2_rules(v1: dict, v2: dict, bars: dict, months_v1: dict, months_v2: dict) -> list[dict]:
    """A2's five rules from plain numbers: v1 and v2 map each A2_ORDER name to its value,
    bars holds v1's baseline bars. Refuses alphas over different months."""
    if months_v1 != months_v2:
        raise AssertionError(f"the alphas are not over the same months: v1 {months_v1}, "
                             f"v2 {months_v2}")
    mdd_bar = bars["max_drawdown"]
    check_equal(mdd_bar, v1["max_drawdown"] + DD_MARGIN, "rule 1's bar")
    r1 = v2["max_drawdown"] >= mdd_bar
    so, ca = v2["sortino"] > v1["sortino"], v2["calmar"] > v1["calmar"]
    al = v2["iima_alpha_annual"] >= v1["iima_alpha_annual"]
    t = v2["iima_alpha_t"] >= v1["iima_alpha_t"]
    tu = v2["turnover_annual_incl_initial"] <= v1["turnover_annual_incl_initial"]
    cp = v2["capacity_p95_share_of_turnover"] <= v1["capacity_p95_share_of_turnover"]
    return [
        {"rule": 1, "text": "maximum drawdown over the full period at least 5 percentage points "
                            "smaller than v1's",
         "parts": {"max_drawdown": {"v1": v1["max_drawdown"], "v2": v2["max_drawdown"],
                                    "v2_must_be_at_least": mdd_bar, "passes": bool(r1),
                                    "improvement_points": (v2["max_drawdown"]
                                                           - v1["max_drawdown"]) * 100}},
         "passes": bool(r1)},
        {"rule": 2, "text": "Sortino and Calmar, as quantstats computes them, both higher than v1's",
         "parts": {"sortino": {"v1": v1["sortino"], "v2": v2["sortino"], "passes": bool(so)},
                   "calmar": {"v1": v1["calmar"], "v2": v2["calmar"], "passes": bool(ca)}},
         "passes": bool(so and ca)},
        {"rule": 3, "text": "IIMA four-factor annual alpha (statsmodels, Newey-West HAC 3 lags, "
                            "no small-sample correction) not lower than v1's over the same months, "
                            "and its t-statistic not lower than v1's",
         "parts": {"iima_alpha_annual": {"v1": v1["iima_alpha_annual"],
                                         "v2": v2["iima_alpha_annual"], "passes": bool(al)},
                   "iima_alpha_t": {"v1": v1["iima_alpha_t"], "v2": v2["iima_alpha_t"],
                                    "passes": bool(t)}},
         "months": months_v2, "passes": bool(al and t)},
        {"rule": 4, "text": "annual one-way turnover, including the initial build, not higher "
                            "than v1's",
         "parts": {"turnover_annual_incl_initial": {
             "v1": v1["turnover_annual_incl_initial"], "v2": v2["turnover_annual_incl_initial"],
             "passes": bool(tu)}},
         "passes": bool(tu)},
        {"rule": 5, "text": "capacity not worse: the 95th percentile, over every buy and sell, of "
                            "trade value over the stock's 60-session median turnover, at Rs 5 "
                            "lakh, not higher than v1's",
         "parts": {"capacity_p95_share_of_turnover": {
             "v1": v1["capacity_p95_share_of_turnover"],
             "v2": v2["capacity_p95_share_of_turnover"], "passes": bool(cp)}},
         "passes": bool(cp)},
    ]


def a2_verdict(src: Sources, adopted: str) -> dict:
    base = src.doc("v1", "baseline_report.json")["a2_acceptance_quantities"]
    v1f = {q: {"value": base[q]["value"],
               "source": f"{src.name('v1', 'baseline_report.json')}: "
                         f"a2_acceptance_quantities.{q}.value"} for q in A2_ORDER}
    # v1's report must still be the number its own job wrote
    for q, f in a2_figures(src, "v1").items():
        if f["value"] != v1f[q]["value"]:
            raise AssertionError(f"v1 {q}: baseline_report.json {v1f[q]['value']} vs {f['source']} "
                                 f"{f['value']}")
    v2f = a2_figures(src, adopted)
    # the adopted run's own baseline report, where written, must say the same
    rp = src.path(adopted, "baseline_report.json")
    if rp.exists():
        own = src.doc(adopted, "baseline_report.json")["a2_acceptance_quantities"]
        for q in A2_ORDER:
            if own[q]["value"] != v2f[q]["value"]:
                raise AssertionError(f"{adopted} {q}: its baseline_report.json disagrees")
    m1 = base["iima_alpha_annual"]["months"]
    reg = src.doc(adopted, "libcheck.json")["regression_full_period"]["strategy"]
    m2 = {"n": reg["months"], "first": reg["first"], "last": reg["last"]}
    bars = {"max_drawdown": base["max_drawdown"]["bar_for_v2"]["v2_max_drawdown_must_be_at_least"]}
    rules = a2_rules({q: _num(v1f[q]) for q in A2_ORDER}, {q: _num(v2f[q]) for q in A2_ORDER},
                     bars, m1, m2)
    for r in rules:
        for q, part in r["parts"].items():
            part["v1_source"], part["v2_source"] = v1f[q]["source"], v2f[q]["source"]
    r1 = rules[0]["parts"]["max_drawdown"]
    r1["bar_source"] = (f"{src.name('v1', 'baseline_report.json')}: a2_acceptance_quantities."
                        "max_drawdown.bar_for_v2.v2_max_drawdown_must_be_at_least")
    failed = [r["rule"] for r in rules if not r["passes"]]
    return {"definition": "research/strategy/v2-spec.md A2 as the Clarification to A2's "
                          "measurement (2026-09-26) fixes it: v2 replaces v1 only if all five hold",
            "variant": adopted, "run": br.repo_path(src.runs[adopted]),
            "against": br.repo_path(src.path("v1", "baseline_report.json")),
            "rules": rules, "rules_passed": [r["rule"] for r in rules if r["passes"]],
            "rules_failed": failed, "all_pass": not failed,
            "verdict": "v2 replaces v1" if not failed else "v1 stays: v2 does not replace it",
            "hand_rolled_t_beside": {
                "v1": src.fig("v1", "verify.json", "attribution.strategy.alpha_t"),
                "v2": src.fig(adopted, "verify.json", "attribution.strategy.alpha_t"),
                "note": "the n/(n-k) corrected t, reported beside the A2 figure, not used for it"},
            "capacity_decision_date_fills_beside": {
                "v1": src.fig("v1", "verify.json", "capacity.p95_pct"),
                "v2": src.fig(adopted, "verify.json", "capacity.p95_pct"),
                "note": "percent; only fills on a decision date, which misses v2's later tranches"}}


# ---------------------------------------------------------------- safeguard 2: CSCV
def blocks(n: int, k: int = N_BLOCKS) -> list[np.ndarray]:
    """k contiguous blocks of the indices 0..n-1 (numpy.array_split)."""
    if n < k:
        raise ValueError(f"{n} returns cannot make {k} blocks")
    return np.array_split(np.arange(n), k)


def growth(r: np.ndarray) -> float:
    """Compounded return of a sequence of returns."""
    return float(np.prod(1.0 + np.asarray(r, float)) - 1.0)


def max_drawdown(r: np.ndarray) -> float:
    """Worst fall from a running peak of the path the returns make from 1, the
    starting value counting as a peak (as quantstats counts it)."""
    v = np.r_[1.0, np.cumprod(1.0 + np.asarray(r, float))]
    return float(np.min(v / np.maximum.accumulate(v) - 1.0))


def v_weighted(ratio: np.ndarray, v: np.ndarray) -> float:
    return float(np.sum(ratio * v) / np.sum(v)) if len(v) else float("nan")


def rule_on(idx: np.ndarray, blocks_in: set, ra: np.ndarray, rb: np.ndarray,
            pos_a: pd.DataFrame, pos_b: pd.DataFrame) -> dict:
    """A1's three conditions and pick on one half: return indices `idx` (in time order)
    and the positions whose block is in `blocks_in`."""
    pa, pb = pos_a[pos_a["block"].isin(blocks_in)], pos_b[pos_b["block"].isin(blocks_in)]
    ea, eb = v_weighted(pa["ratio"].to_numpy(), pa["V"].to_numpy()), \
        v_weighted(pb["ratio"].to_numpy(), pb["V"].to_numpy())
    ga, gb = growth(ra[idx]), growth(rb[idx])
    da, db = max_drawdown(ra[idx]), max_drawdown(rb[idx])
    # no position in the half: rule 1 cannot hold (a NaN comparison is False)
    holds = a1_holds(ea, eb, ga, gb, da, db)
    return {"entry": (ea, eb), "growth": (ga, gb), "mdd": (da, db), "holds": holds,
            "pick": a1_pick(holds), "positions": (len(pa), len(pb))}


def cscv(ra: np.ndarray, rb: np.ndarray, pos_a: pd.DataFrame, pos_b: pd.DataFrame,
         k: int = N_BLOCKS, n_train: int = N_TRAIN) -> tuple[dict, pd.DataFrame]:
    """Combinatorially symmetric cross-validation of A1's choice between (a) and (b).

    ra, rb: the two variants' returns on the same clock. pos_a, pos_b: one row per new
    position with its 'ratio', 'V' and 'block' (the block of the return ending at its
    decision date's close). Every choice of n_train of the k blocks is a training half;
    the A1 rule picks on it, and the pick does worse when the rule applied to the other
    half picks the other variant. Returns (summary, one row per split)."""
    ra, rb = np.asarray(ra, float), np.asarray(rb, float)
    if ra.shape != rb.shape:
        raise ValueError("the two variants are not on the same clock")
    B = blocks(len(ra), k)
    rows = []
    for train in combinations(range(k), n_train):
        test = tuple(i for i in range(k) if i not in train)
        tr = rule_on(np.concatenate([B[i] for i in train]), set(train), ra, rb, pos_a, pos_b)
        te = rule_on(np.concatenate([B[i] for i in test]), set(test), ra, rb, pos_a, pos_b)
        pick = tr["pick"]
        j, o = (0, 1) if pick == "a" else (1, 0)        # the pick's and the other's position
        rows.append({"train": "-".join(str(i) for i in train), "test": "-".join(str(i) for i in test),
                     "pick": pick, "train_holds": "".join("y" if h else "n" for h in tr["holds"]),
                     "test_pick": te["pick"],
                     "test_holds": "".join("y" if h else "n" for h in te["holds"]),
                     "worse": te["pick"] != pick,
                     "test_growth_pick": te["growth"][j], "test_growth_other": te["growth"][o],
                     "test_mdd_pick": te["mdd"][j], "test_mdd_other": te["mdd"][o],
                     "test_entry_pick": te["entry"][j], "test_entry_other": te["entry"][o]})
    df = pd.DataFrame(rows)
    n = len(df)
    summary = {
        "splits": int(n), "blocks": int(k), "training_blocks": int(n_train),
        "block_sizes": [int(len(b)) for b in B],
        "picked_b_on_training": int((df["pick"] == "b").sum()),
        "pbo": float(df["worse"].mean()),
        "worse_splits": int(df["worse"].sum()),
        "pbo_when_a_picked": (float(df.loc[df["pick"] == "a", "worse"].mean())
                              if (df["pick"] == "a").any() else None),
        "pbo_when_b_picked": (float(df.loc[df["pick"] == "b", "worse"].mean())
                              if (df["pick"] == "b").any() else None),
        "beside": {
            "share_pick_lower_test_growth": float(
                (df["test_growth_pick"] < df["test_growth_other"]).mean()),
            "share_pick_deeper_test_drawdown": float(
                (df["test_mdd_pick"] < df["test_mdd_other"]).mean()),
            "share_pick_higher_test_entry_ratio": float(
                (df["test_entry_pick"] > df["test_entry_other"]).mean()),
            "note": "each criterion alone on the test half, for the pick against the other "
                    "variant; not the probability of overfitting, which uses the whole rule"},
        "logit_note": "with two variants the pick's relative rank on the test half is 2/3 or "
                      "1/3, a logit of +ln 2 or -ln 2, so the probability of backtest "
                      "overfitting is simply the share of splits in which it is -ln 2"}
    return summary, df


def position_blocks(clock: pd.DataFrame, a1: dict, k: int = N_BLOCKS) -> pd.DataFrame:
    """a1.json's positions with the block of the return that ends at their decision
    date's close (the return whose period contains the open at which tranche 1 buys)."""
    pos = pd.DataFrame(a1["positions"])
    pos = pos[pos["ratio"].notna()].copy()
    pos["ratio"] = pos["ratio"].astype(float)
    B = blocks(len(clock) - 1, k)
    block_of = np.empty(len(clock) - 1, dtype=int)
    for i, b in enumerate(B):
        block_of[b] = i
    ts = pd.to_datetime(pos["D"]) + lc.CLOSE_AT
    loc = clock.index.get_indexer(ts)
    if (loc < 1).any():
        raise ValueError("a decision date has no close mark on the clock")
    pos["return_index"] = loc - 1
    pos["block"] = block_of[loc - 1]
    return pos.reset_index(drop=True)


def same_clock(*clocks: pd.DataFrame) -> None:
    c0 = clocks[0]
    for c in clocks[1:]:
        if not (c.index.equals(c0.index) and c["mark"].equals(c0["mark"])):
            raise AssertionError("the runs are not on the same clock")


# ---------------------------------------------------------------- safeguard 2: windows
def windows(v2: pd.Series, v1: pd.Series, years: int = WINDOW_YEARS) -> pd.DataFrame:
    """Every `years`-year window on the common clock: CAGR and maximum drawdown of both."""
    if not v2.index.equals(v1.index):
        raise AssertionError("the two series are not on the same clock")
    t = v2.index
    dates = t.normalize()
    last = dates[-1]
    a, b = v2.to_numpy(float), v1.to_numpy(float)
    rows = []
    for i in range(len(t)):
        end = dates[i] + pd.DateOffset(years=years)
        if end > last:
            break
        j = int(dates.searchsorted(end, side="right")) - 1
        yrs = lc.years_between(t[i], t[j])
        wa, wb = a[i:j + 1], b[i:j + 1]
        ga = (wa[-1] / wa[0]) ** (1 / yrs) - 1
        gb = (wb[-1] / wb[0]) ** (1 / yrs) - 1
        da = float(np.min(wa / np.maximum.accumulate(wa) - 1))
        db = float(np.min(wb / np.maximum.accumulate(wb) - 1))
        rows.append({"start": t[i], "end": t[j], "years": yrs,
                     "v2_cagr": ga, "v1_cagr": gb, "cagr_diff": ga - gb,
                     "v2_max_drawdown": da, "v1_max_drawdown": db, "drawdown_diff": da - db})
    return pd.DataFrame(rows)


def windows_summary(w: pd.DataFrame) -> dict:
    def dist(s: pd.Series) -> dict:
        return {"min": float(s.min()), "median": float(s.median()), "max": float(s.max())}

    def when(col, fn):
        r = w.loc[getattr(w[col], fn)()]
        return {"value": float(r[col]), "start": str(r["start"]), "end": str(r["end"])}

    return {"windows": int(len(w)),
            "first": {"start": str(w["start"].iloc[0]), "end": str(w["end"].iloc[0])},
            "last": {"start": str(w["start"].iloc[-1]), "end": str(w["end"].iloc[-1])},
            "v2_cagr": dist(w["v2_cagr"]), "v1_cagr": dist(w["v1_cagr"]),
            "cagr_diff": dist(w["cagr_diff"]),
            "v2_max_drawdown": dist(w["v2_max_drawdown"]),
            "v1_max_drawdown": dist(w["v1_max_drawdown"]),
            "share_v2_cagr_higher": float((w["v2_cagr"] > w["v1_cagr"]).mean()),
            "share_v2_drawdown_shallower": float((w["v2_max_drawdown"] > w["v1_max_drawdown"]).mean()),
            "share_v2_drawdown_shallower_by_5_points": float(
                (w["v2_max_drawdown"] >= w["v1_max_drawdown"] + DD_MARGIN).mean()),
            "worst_v2_cagr": when("v2_cagr", "idxmin"), "worst_v1_cagr": when("v1_cagr", "idxmin"),
            "note": "windows overlap almost entirely from one to the next, so they are far from "
                    "independent: this is a description of the path, not a sample"}


# ---------------------------------------------------------------- reported beside
BENCH_ORDER = ("nifty500", "midcap150", "smallcap250", "momentum30", "value50", "quality30",
               "lowvol30", "alpha50")


def _verdict_sign(x: float, y: float | None) -> str:
    """'beat' / 'did not beat' on the excess; 'tie' when the no-overnight reading differs."""
    word = "beat" if x > 0 else "did not beat"
    if y is not None and (x > 0) != (y > 0):
        return "tie"
    return word


def factor_indices(src: Sources, adopted: str) -> dict:
    att = src.doc(adopted, "attribution.json")["benchmarks"]
    v1b = src.doc("v1", "attribution.json")["benchmarks"]
    rows = {}
    for base in BENCH_ORDER:
        if base not in att:
            continue
        no = f"{base}_no_overnight"
        row = {"name": att[base]["name"], "factor_index": base in at.FACTOR_INDICES}
        for part in ("whole_period", "live_part"):
            if part not in att[base] or "excess_cagr" not in att[base][part]:
                if part in att[base]:
                    row[part] = {"note": att[base][part].get("note")}
                continue
            p = f"benchmarks.{base}.{part}"
            d = att[base][part]
            e = src.fig(adopted, "attribution.json", f"{p}.excess_cagr")
            cell = {"from": d["from"], "to": d["to"], "years": d["years"],
                    "v2_cagr": src.fig(adopted, "attribution.json", f"{p}.strategy_cagr"),
                    "index_cagr": src.fig(adopted, "attribution.json", f"{p}.benchmark_cagr"),
                    "excess_cagr": e,
                    "information_ratio": src.fig(adopted, "attribution.json",
                                                 f"{p}.information_ratio")}
            y = None
            if no in att and part in att[no] and "excess_cagr" in att[no][part]:
                cell["excess_cagr_no_overnight"] = src.fig(adopted, "attribution.json",
                                                           f"benchmarks.{no}.{part}.excess_cagr")
                y = _num(cell["excess_cagr_no_overnight"])
            cell["v2"] = _verdict_sign(_num(e), y)
            if part == "live_part":
                cell["live_start"] = d["live_start"]
                cell["basis"] = d["basis"]
                cell["too_short_to_judge"] = bool(d["years"] < SHORT_LIVE_YEARS)
                cell["same_as_whole_period"] = bool(d.get("same_as_whole_period", False))
            if base in v1b and part in v1b[base] and "excess_cagr" in v1b[base][part]:
                cell["v1_excess_cagr"] = src.fig("v1", "attribution.json", f"{p}.excess_cagr")
            row[part] = cell
        rows[base] = row
    gaps = [abs(_num(c["excess_cagr"]) - _num(c["excess_cagr_no_overnight"]))
            for r in rows.values() for part in ("whole_period", "live_part")
            for c in [r.get(part, {})] if "excess_cagr_no_overnight" in c]
    return {"definition": "excess CAGR = strategy CAGR minus the index's total-return CAGR "
                          "(calendar days) on the run's clock (jobs/attribution.py); the live "
                          "part starts at the close of the index's first live date "
                          "(Clarification to A4). 'tie' where the reading with no overnight move "
                          "at endpoints NSE printed no open for gives the other answer",
            "no_overnight_sensitivity": {
                "largest_change_in_excess": max(gaps) if gaps else None,
                "ties": [r["name"] + f" ({part})" for r in rows.values()
                         for part in ("whole_period", "live_part")
                         if r.get(part, {}).get("v2") == "tie"]},
            "rows": rows}


def quality(src: Sources, adopted: str) -> dict:
    out = {}
    for v in ("margin", "roce", "margin_no_r6", "roce_no_r6"):
        p = f"quality.{v}"
        q = src.doc(adopted, "attribution.json")["quality"][v]
        row = {"label": q["label"], "months": q["months"], "first": q["first"], "last": q["last"],
               "identical_months": q["identical_months"]}
        for run, key in (("v2", adopted), ("v1", "v1")):
            row[run] = {
                "without_alpha": src.fig(key, "attribution.json",
                                         f"{p}.without_quality.exact.alpha_annual"),
                "without_t": src.fig(key, "attribution.json", f"{p}.without_quality.exact.alpha_t"),
                "with_alpha": src.fig(key, "attribution.json", f"{p}.with_quality.exact.alpha_annual"),
                "with_t": src.fig(key, "attribution.json", f"{p}.with_quality.exact.alpha_t"),
                "quality_loading": src.fig(key, "attribution.json",
                                           f"{p}.with_quality.loadings.QUAL")}
        vq = src.doc("v1", "attribution.json")["quality"][v]
        if (vq["months"], vq["first"], vq["last"]) != (q["months"], q["first"], q["last"]):
            raise AssertionError(f"quality {v}: v1 and v2 regressions are not on the same months")
        out[v] = row
    return {"definition": "IIMA four-factor regression without and with the A5 quality factor, "
                          "on identical months checked in code (jobs/attribution.py); margin and "
                          "ROCE versions are the headline, '_no_r6' (loss-makers kept) robustness "
                          "only", "rows": out}


def after_tax(src: Sources, adopted: str) -> dict:
    rel = "after_tax/summary.json"

    def one(run):
        return {"cagr": src.fig(run, rel, "after_tax_stats.strategy.cagr"),
                "max_drawdown": src.fig(run, rel, "after_tax_stats.strategy.max_drawdown"),
                "end_value_liquidated": src.fig(run, rel, "strategy.after_tax_end_liquidated"),
                "tax_paid_during": src.fig(run, rel, "strategy.tax_paid_during"),
                "excess_cagr_vs_fund": src.fig(run, rel,
                                               "after_tax_stats.strategy_vs_benchmark_growth.cagr_diff"),
                "replay_check_worst_rel": src.fig(run, rel, "replay_check.worst_rel_error")}

    v2, v1 = one(adopted), one("v1")
    fund = {"cagr": src.fig(adopted, rel, "after_tax_stats.benchmark_growth.cagr"),
            "end_value_liquidated": src.fig(adopted, rel,
                                            "benchmark.growth.after_tax_end_liquidated"),
            "max_drawdown": src.fig(adopted, rel, "after_tax_stats.benchmark_growth.max_drawdown")}
    check_equal(_num(fund["cagr"]), src.doc("v1", rel)["after_tax_stats"]["benchmark_growth"]["cagr"],
                "the index fund's after-tax CAGR in v1's and v2's after-tax runs")
    capital = src.fig(adopted, rel, "strategy.capital")
    check_equal(_num(capital), src.doc("v1", rel)["strategy"]["capital"], "starting capital")
    return {"definition": br.AFTER_TAX_DEFINITION, "capital": capital, "v2": v2, "v1": v1,
            "fund": fund,
            "v2_minus_v1_cagr": _num(v2["cagr"]) - _num(v1["cagr"]),
            "v2_beats_fund": bool(_num(v2["excess_cagr_vs_fund"]) > 0),
            "v2_beats_v1": bool(_num(v2["cagr"]) > _num(v1["cagr"]))}


def monkey(src: Sources, adopted: str) -> dict:
    out = {"replay_worst_rel": src.fig(adopted, "verify.json", "monkey_test.replay_worst_rel"),
           "engine": src.doc(adopted, "verify.json")["monkey_test"].get("engine")}
    for mode in ("persistent", "fresh"):
        p = f"monkey_test.{mode}"
        out[mode] = {k: src.fig(adopted, "verify.json", f"{p}.{k}")
                     for k in ("draws", "random_median_pct", "random_p95_pct", "random_best_pct",
                               "random_median_turnover", "strategy_percentile",
                               "beaten_by_n_random")}
        out[mode]["v1"] = {k: src.fig("v1", "verify.json", f"{p}.{k}")
                           for k in ("strategy_percentile", "beaten_by_n_random")}
    out["strategy_cagr_pct"] = src.fig(adopted, "verify.json", "monkey_test.strategy_cagr_pct")
    out["strategy_turnover"] = src.fig(adopted, "verify.json", "monkey_test.strategy_turnover")
    return out


def deflated(src: Sources, adopted: str, trials_path: Path = trials.LOG_PATH) -> dict:
    p = "deflated_sharpe"
    out = {k: src.fig(adopted, "verify.json", f"{p}.{k}")
           for k in ("sharpe_annual", "n_obs", "n_trials", "prob_null_sampling_variance",
                     "prob_cross_trial_variance_36_grid", "prob_grid_trials_only")}
    out["v1"] = {k: src.fig("v1", "verify.json", f"{p}.{k}")
                 for k in ("sharpe_annual", "n_trials", "prob_null_sampling_variance",
                           "prob_cross_trial_variance_36_grid", "prob_grid_trials_only")}
    lines = [json.loads(x) for x in trials_path.read_text(encoding="utf-8").splitlines() if x.strip()]
    n = int(_num(out["n_trials"]))
    v2_lines = [i + 1 for i, e in enumerate(lines) if e.get("study") == "strategy_v2"]
    out["trials_file"] = {"path": br.repo_path(trials_path), "records_now": len(lines),
                          "strategy_v2_records_at_lines": v2_lines,
                          "both_v2_runs_counted": bool(len(v2_lines) >= 2 and max(v2_lines) <= n),
                          "count_matches_the_file_now": bool(n == len(lines))}
    if not out["trials_file"]["both_v2_runs_counted"]:
        raise AssertionError(f"verify.json counted {n} trials, which does not include both v2 "
                             f"runs (lines {v2_lines} of {br.repo_path(trials_path)})")
    return out


def variants(src: Sources) -> dict:
    out = {}
    for run in ("a", "b"):
        f = a2_figures(src, run)
        f["cagr"] = src.fig(run, "libcheck.json", "full_period.strategy.exact.cagr_calendar")
        f["a1_entry_ratio"] = src.fig(run, "a1.json", "v_weighted_ratio")
        f["universe_ew_cagr"] = src.fig(run, "libcheck.json",
                                        "full_period.universe_ew.exact.cagr_calendar")
        out[run] = f
    f = {q: {"value": v["value"], "source": f"{src.name('v1', 'baseline_report.json')}: "
                                            f"a2_acceptance_quantities.{q}.value"}
         for q, v in src.doc("v1", "baseline_report.json")["a2_acceptance_quantities"].items()}
    f["cagr"] = src.fig("v1", "libcheck.json", "full_period.strategy.exact.cagr_calendar")
    f["universe_ew_cagr"] = src.fig("v1", "libcheck.json",
                                    "full_period.universe_ew.exact.cagr_calendar")
    out["v1"] = f
    return out


# ---------------------------------------------------------------- the whole verdict
def build(v1_run: Path = V1_RUN, a_run: Path = A_RUN, b_run: Path = B_RUN,
          trials_path: Path = trials.LOG_PATH) -> tuple[dict, pd.DataFrame, pd.DataFrame]:
    src = Sources({"v1": v1_run, "a": a_run, "b": b_run})
    a1 = a1_adoption(src)
    adopted = a1["adopted"]
    a2 = a2_verdict(src, adopted)

    ca, cb, c1 = src.clock("a"), src.clock("b"), src.clock("v1")
    same_clock(ca, cb, c1)
    ra = ca["strategy"].pct_change().dropna().to_numpy(float)
    rb = cb["strategy"].pct_change().dropna().to_numpy(float)
    pos_a = position_blocks(ca, src.doc("a", "a1.json"))
    pos_b = position_blocks(cb, src.doc("b", "a1.json"))
    cs, splits = cscv(ra, rb, pos_a, pos_b)
    full = rule_on(np.arange(len(ra)), set(range(N_BLOCKS)), ra, rb, pos_a, pos_b)
    if full["pick"] != adopted:
        raise AssertionError("the A1 rule on all ten blocks does not reproduce the adoption")
    cs["whole_period_pick"] = full["pick"]
    cs["check"] = ("the same rule on all ten blocks picks the adopted variant, as the run "
                   "files' figures do")
    cs["positions_per_block"] = pos_a.groupby("block").size().reindex(
        range(N_BLOCKS), fill_value=0).astype(int).tolist()

    c2 = ca if adopted == "a" else cb
    s2, s1 = c2["strategy"].astype(float), c1["strategy"].astype(float)
    boot = {"method": "jobs/libcheck_v1.margin_interval, the bootstrap v1's final test used: "
                      "90% interval for the difference in compound annual growth of two NAV "
                      "series, each draw rebuilding both paths from the same resampled returns",
            "v2_minus_v1": lc.margin_interval(s2, s1),
            "v2_minus_its_universe_ew": lc.margin_interval(s2, c2["universe_ew"].astype(float)),
            "v1_minus_its_universe_ew": lc.margin_interval(s1, c1["universe_ew"].astype(float)),
            "clock": f"{c2.index[0]} to {c2.index[-1]}, {len(c2)} marks, the same for both",
            "note": "percentage points of compound annual growth; stationary bootstrap of paired "
                    "returns between marks, mean block 21, 5000 draws, seed 7 "
                    "(jobs/libcheck_v1.margin_interval, as v1's final test used)"}
    w = windows(s2, s1)
    ws = windows_summary(w)

    rep = {
        "title": "Strategy v2 against v1: A1's adoption, A2's verdict and safeguard 2",
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "spec": "research/strategy/v2-spec.md (A1, A2, A4-A6, the Clarification before the v2 "
                "build and the Clarification to A2's measurement)",
        "runs": {"v1": br.repo_path(v1_run), "a": br.repo_path(a_run), "b": br.repo_path(b_run)},
        "period": {"start": str(c1.index[0]), "end": str(c1.index[-1]), "marks": int(len(c1))},
        "libraries": {p: version(p) for p in ("quantstats", "statsmodels", "arch", "pandas",
                                              "numpy")},
        "readings": READINGS,
        "a1_adoption": a1,
        "a2_verdict": a2,
        "safeguard_2": {
            "cscv_a1_choice": {**cs, "definition": "combinatorially symmetric cross-validation "
                                                   "(Bailey, Borwein, Lopez de Prado and Zhu 2017) "
                                                   "of A1's choice, as the Clarification before the "
                                                   "v2 build defines it",
                               "splits_file": None},
            "bootstrap": boot,
            "windows_3y": {**ws, "file": None}},
        "factor_indices": factor_indices(src, adopted),
        "quality_factor": quality(src, adopted),
        "after_tax": after_tax(src, adopted),
        "monkey_test": monkey(src, adopted),
        "deflated_sharpe": deflated(src, adopted, trials_path),
        "variants": variants(src),
        "sources": dict(sorted(src.files.items())),
    }
    return rep, w, splits


# ---------------------------------------------------------------- plain English
def pct(x: float, nd: int = 2, sign: bool = False) -> str:
    return f"{x * 100:+.{nd}f}%" if sign else f"{x * 100:.{nd}f}%"


def pts(x: float, nd: int = 2) -> str:
    return f"{x * 100:+.{nd}f} points"


def src_of(f: dict) -> str:
    return f"`{f['source']}`"


def yes(b: bool) -> str:
    return "yes" if b else "no"


def and_list(xs) -> str:
    xs = [str(x) for x in xs]
    return xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + " and " + xs[-1]


def markdown(rep: dict) -> str:
    a1, a2 = rep["a1_adoption"], rep["a2_verdict"]
    ad = a1["adopted"]
    name = {"a": "(a), staggered entry", "b": "(b), staggered entry with A1's entry rule"}
    L = [f"# {rep['title']}", ""]
    L.append(f"Written by `jobs/v2_verdict.py` from the measurement files of "
             f"`{rep['runs']['v1']}` (v1), `{rep['runs']['a']}` (v2, variant a) and "
             f"`{rep['runs']['b']}` (v2, variant b), all on the same clock: the open of "
             f"{rep['period']['start'][:10]}, every close, and the open of "
             f"{rep['period']['end'][:10]} ({rep['period']['marks']} marks). No backtest was run "
             f"to write it. Every figure below gives the file and key it was read from; the "
             f"figures computed here (safeguard 2) are in `v2_verdict.json`, "
             f"`{rep['safeguard_2']['windows_3y']['file']}` and "
             f"`{rep['safeguard_2']['cscv_a1_choice']['splits_file']}`, and the sha256 of every "
             f"file read is at the end. Each run's own measurements are summarised in its "
             f"`baseline_report.md` (`{rep['runs']['a']}/baseline_report.md`, "
             f"`{rep['runs']['b']}/baseline_report.md`).")
    L.append("")
    L.append("## The answer")
    L.append("")
    passed, failed = a2["rules_passed"], a2["rules_failed"]
    L.append(f"**{a2['verdict']}.** A1's test adopts variant {name[ad]}. Against v1 on the "
             f"same data it passes {len(passed)} of A2's five rules"
             + (f" ({and_list(passed)})" if passed else "")
             + (f" and fails {and_list(failed)}" if failed else "")
             + ". The spec lets v2 replace v1 only if all five hold.")
    L.append("")
    L.append("The evidence is weaker than v1's by construction: v2's rules were written after "
             "v1's full-period results were known, so every figure here is on data v1 had "
             "already seen, and there is no fresh holdout (v2-spec, 'How it gets tested, and the "
             "honest problem'). Each variant was run once on the full period, and both runs are "
             "counted in the deflated Sharpe below. Before that, both engines ran each variant "
             "on the in-sample period only, to check they agree (data/backtest/v2a_is, v2b_is "
             "and their _check twins), and the production engine's first full-period attempt "
             "stopped with an error before writing any result (disclosed in the v2 spec, "
             "2026-09-28).")
    L.append("")

    # ---- A1
    L.append("## A1: which way to enter")
    L.append("")
    L.append("(b) is adopted only if all three hold; otherwise (a).")
    L.append("")
    L.append("| Rule | (a) | (b) | Holds | Source |")
    L.append("|---|---|---|---|---|")
    fmt = {"entry_price": lambda x: f"{x:.6f}", "cagr": lambda x: pct(x, 3),
           "max_drawdown": lambda x: pct(x, 3)}
    for r in a1["rules"]:
        L.append(f"| {r['rule']}. {r['text']} | {fmt[r['name']](r['a']['value'])} | "
                 f"{fmt[r['name']](r['b']['value'])} | {yes(r['holds'])} | "
                 f"`{r['a']['source']}` and the same key in `{r['b']['source'].split(':')[0]}` |")
    L.append("")
    r1, r2, r3 = a1["rules"]
    L.append(f"Rule 1: new positions cost {r1['b']['value']:.4f} times the close before their "
             f"decision date in (b) and {r1['a']['value']:.4f} in (a), so buying on A1's signal "
             f"{'did' if r1['holds'] else 'did not'} lower the price paid. Rule 2: (b) grew "
             f"{pct(r2['b']['value'])} a year and (a) {pct(r2['a']['value'])}, "
             f"{abs(r2['b_minus_a']) * 100:.2f} points {'less' if r2['b_minus_a'] < 0 else 'more'}. "
             f"Rule 3: (b)'s worst fall was {pct(r3['b']['value'], 3)} and (a)'s "
             f"{pct(r3['a']['value'], 3)}, {abs(r3['b_minus_a']) * 100:.4f} points "
             f"{'deeper' if r3['b_minus_a'] < 0 else 'shallower or equal'}. "
             + (f"{'Rules' if sum(not r['holds'] for r in a1['rules']) > 1 else 'Rule'} "
                f"{and_list(r['rule'] for r in a1['rules'] if not r['holds'])} "
                f"{'fail' if sum(not r['holds'] for r in a1['rules']) > 1 else 'fails'}, so "
                f"**{name[ad]} is adopted**." if not a1["all_hold"] else
                f"All three hold, so **{name[ad]} is adopted**."))
    L.append("")

    # ---- A2
    L.append(f"## A2: v2 ({ad}) against v1")
    L.append("")
    L.append(f"v1's figures are the baseline's (`{a2['against']}`); v2's are read from the "
             f"same file and key in `{a2['run']}`.")
    L.append("")
    L.append(f"v1's key in that file is `a2_acceptance_quantities.<measure>.value`, with "
             f"<measure> as in the second column; rule 1's bar is "
             f"`a2_acceptance_quantities.max_drawdown.bar_for_v2.v2_max_drawdown_must_be_at_least`.")
    L.append("")
    L.append("| Rule | Measure | v1 | v2 | Needed | Passes | Source of v2's figure |")
    L.append("|---|---|---|---|---|---|---|")
    need = {"max_drawdown": lambda p: f"at least {pct(p['v2_must_be_at_least'])}",
            "sortino": lambda p: f"above {p['v1']:.3f}", "calmar": lambda p: f"above {p['v1']:.3f}",
            "iima_alpha_annual": lambda p: f"at least {pct(p['v1'])}",
            "iima_alpha_t": lambda p: f"at least {p['v1']:.2f}",
            "turnover_annual_incl_initial": lambda p: f"at most {p['v1']:.3f}",
            "capacity_p95_share_of_turnover": lambda p: f"at most {pct(p['v1'], 3)}"}
    show = {"max_drawdown": lambda x: pct(x), "sortino": lambda x: f"{x:.3f}",
            "calmar": lambda x: f"{x:.3f}", "iima_alpha_annual": lambda x: pct(x),
            "iima_alpha_t": lambda x: f"{x:.2f}", "turnover_annual_incl_initial": lambda x: f"{x:.3f}",
            "capacity_p95_share_of_turnover": lambda x: pct(x, 3)}
    label = {"max_drawdown": "Maximum drawdown", "sortino": "Sortino (quantstats)",
             "calmar": "Calmar (quantstats)", "iima_alpha_annual": "IIMA four-factor alpha, a year",
             "iima_alpha_t": "t of that alpha (HAC, 3 lags)",
             "turnover_annual_incl_initial": "Turnover a year, incl. initial build",
             "capacity_p95_share_of_turnover": "Capacity: p95 trade / 60-session median turnover"}
    for r in a2["rules"]:
        for q, p in r["parts"].items():
            L.append(f"| {r['rule']} | {label[q]}; `{q}` | {show[q](p['v1'])} | {show[q](p['v2'])} | "
                     f"{need[q](p)} | {yes(p['passes'])} | `{p['v2_source']}` |")
    L.append("")
    rr = {r["rule"]: r for r in a2["rules"]}
    m = rr[1]["parts"]["max_drawdown"]
    words = {"sortino": "Sortino", "calmar": "Calmar", "iima_alpha_annual": "alpha",
             "iima_alpha_t": "its t", "turnover_annual_incl_initial": "turnover",
             "capacity_p95_share_of_turnover": "capacity p95"}
    say = [f"Rule 1 {'holds' if rr[1]['passes'] else 'fails'}: v2's worst fall is "
           f"{abs(m['improvement_points']):.2f} points "
           f"{'shallower' if m['improvement_points'] > 0 else 'deeper'} than v1's, where at "
           f"least 5 points shallower was needed."]
    for k in (2, 3, 4, 5):
        bad = [q for q, p in rr[k]["parts"].items() if not p["passes"]]
        if not bad:
            say.append(f"Rule {k} holds.")
            continue
        say.append(f"Rule {k} fails on " + " and ".join(
            f"{words[q]} ({show[q](rr[k]['parts'][q]['v2'])} against v1's "
            f"{show[q](rr[k]['parts'][q]['v1'])})" for q in bad) + ".")
    failing = [k for k in (1, 2, 3, 4, 5) if not rr[k]["passes"]]
    for k in (2, 3, 4, 5):
        for q, p in rr[k]["parts"].items():
            gap = abs(p["v2"] / p["v1"] - 1) if p["v1"] else None
            if not p["passes"] and gap is not None and gap < 0.05:
                others = [str(x) for x in failing if x != k]
                say.append(f"Rule {k} fails narrowly ({words[q]} {gap:.1%} from v1's figure), so "
                           f"the verdict does not rest on it"
                           + (f": rule{'s' if len(others) > 1 else ''} {and_list(others)} "
                              f"fail{'' if len(others) > 1 else 's'} as well." if others else "."))
    L.append(" ".join(say) + f" The alphas are over the same {rr[3]['months']['n']} months, "
             f"{rr[3]['months']['first']} to {rr[3]['months']['last']}, the last month IIMA has "
             f"published.")
    L.append("")
    h, c = a2["hand_rolled_t_beside"], a2["capacity_decision_date_fills_beside"]
    L.append(f"Beside the A2 figures, not used for them: the hand-written regression's t with the "
             f"n/(n-k) correction is {h['v2']['value']:.2f} for v2 and {h['v1']['value']:.2f} for "
             f"v1 (`{h['v2']['source']}`); capacity counting only fills on a decision date is "
             f"{c['v2']['value']:.3f}% for v2 and {c['v1']['value']:.3f}% for v1 "
             f"(`{c['v2']['source']}`).")
    L.append("")

    # ---- safeguard 2
    sg = rep["safeguard_2"]
    cs, bt, ws = sg["cscv_a1_choice"], sg["bootstrap"], sg["windows_3y"]
    L.append("## Safeguard 2: how much to trust it")
    L.append("")
    L.append("### Is A1's choice overfit? Combinatorially symmetric cross-validation")
    L.append("")
    L.append(f"The returns of (a) and (b) were cut into {cs['blocks']} contiguous blocks "
             f"({', '.join(map(str, cs['block_sizes']))} returns). For each of the "
             f"{cs['splits']} ways to choose {cs['training_blocks']} blocks as the training half, "
             f"A1's rule picked a variant on those blocks and was then applied to the other five. "
             f"It picked (b) on {cs['picked_b_on_training']} training halves and (a) on "
             f"{cs['splits'] - cs['picked_b_on_training']}. **In {cs['worse_splits']} of "
             f"{cs['splits']} splits ({cs['pbo']:.1%}) the pick lost on the test half** (the rule, "
             f"applied there, picked the other variant): the probability of backtest overfitting "
             f"of this one choice. On all ten blocks the rule picks ({cs['whole_period_pick']}), "
             f"as the run files do. New positions per block: "
             f"{', '.join(map(str, cs['positions_per_block']))}. A low figure here is close to "
             f"automatic: (a) is the default and (b) must win all three tests, so this checks "
             f"that the choice was not a fluke of one stretch of data, not that A1 has no value.")
    L.append("")
    b = cs["beside"]
    L.append(f"Each criterion alone on the test half, the pick against the other variant: lower "
             f"growth in {b['share_pick_lower_test_growth']:.1%} of splits, a deeper worst fall "
             f"in {b['share_pick_deeper_test_drawdown']:.1%}, a higher entry price in "
             f"{b['share_pick_higher_test_entry_ratio']:.1%}. Source: "
             f"`data/backtest/v2_verdict.json: safeguard_2.cscv_a1_choice`, one row per split in "
             f"`{cs['splits_file']}`.")
    L.append("")
    L.append("### How sure is the gap? Stationary block bootstrap")
    L.append("")
    g = bt["v2_minus_v1"]
    L.append(f"v2's compound annual growth minus v1's over the whole clock is "
             f"{g['point_pct']:+.2f} points, 90% interval {g['lo90_pct']:+.2f} to "
             f"{g['hi90_pct']:+.2f}; {g['share_of_draws_le_zero']:.1%} of 5,000 draws are at or "
             f"below zero. The same bootstrap of each against its own equal-weight universe: v2 "
             f"{bt['v2_minus_its_universe_ew']['point_pct']:+.2f} points "
             f"({bt['v2_minus_its_universe_ew']['lo90_pct']:+.2f} to "
             f"{bt['v2_minus_its_universe_ew']['hi90_pct']:+.2f}), v1 "
             f"{bt['v1_minus_its_universe_ew']['point_pct']:+.2f} "
             f"({bt['v1_minus_its_universe_ew']['lo90_pct']:+.2f} to "
             f"{bt['v1_minus_its_universe_ew']['hi90_pct']:+.2f}). Source: "
             f"`data/backtest/v2_verdict.json: safeguard_2.bootstrap`.")
    L.append("")
    L.append("### Every three-year window")
    L.append("")
    L.append(f"{ws['windows']} windows, the first {ws['first']['start'][:10]} to "
             f"{ws['first']['end'][:10]}, the last {ws['last']['start'][:10]} to "
             f"{ws['last']['end'][:10]}; they overlap, so this describes one path rather than "
             f"sampling many.")
    L.append("")
    L.append("| | v2 | v1 |")
    L.append("|---|---|---|")
    L.append(f"| Three-year CAGR: worst / median / best | {pct(ws['v2_cagr']['min'], 1)} / "
             f"{pct(ws['v2_cagr']['median'], 1)} / {pct(ws['v2_cagr']['max'], 1)} | "
             f"{pct(ws['v1_cagr']['min'], 1)} / {pct(ws['v1_cagr']['median'], 1)} / "
             f"{pct(ws['v1_cagr']['max'], 1)} |")
    L.append(f"| Worst fall within a window: worst / median / mildest | "
             f"{pct(ws['v2_max_drawdown']['min'], 1)} / {pct(ws['v2_max_drawdown']['median'], 1)} / "
             f"{pct(ws['v2_max_drawdown']['max'], 1)} | {pct(ws['v1_max_drawdown']['min'], 1)} / "
             f"{pct(ws['v1_max_drawdown']['median'], 1)} / {pct(ws['v1_max_drawdown']['max'], 1)} |")
    L.append("")
    L.append(f"v2 grew faster than v1 in {ws['share_v2_cagr_higher']:.1%} of windows and fell less "
             f"in {ws['share_v2_drawdown_shallower']:.1%} (by 5 points or more in "
             f"{ws['share_v2_drawdown_shallower_by_5_points']:.1%}). Source: "
             f"`data/backtest/v2_verdict.json: safeguard_2.windows_3y`, every window in "
             f"`{ws['file']}`.")
    L.append("")

    # ---- A4
    fi = rep["factor_indices"]["rows"]
    L.append("## Could the owner just buy the index? (A4)")
    L.append("")
    L.append(f"v2 ({ad}) against each index's total return, whole period and the index's live "
             f"part. A 'tie' means the reading with no overnight move where NSE printed no open "
             f"gives the other answer. v1's whole-period excess is beside it.")
    L.append("")
    L.append("| Index | Whole: v2 excess | v2 | v1 excess | Live part | Live: v2 excess | v2 | Source |")
    L.append("|---|---|---|---|---|---|---|---|")
    for base, r in fi.items():
        w_ = r["whole_period"]
        lp = r.get("live_part")
        if lp is None:
            live = ("not a factor index", "", "")
        elif "excess_cagr" not in lp:
            live = (lp.get("note") or "", "", "")
        elif lp["same_as_whole_period"]:
            live = ("whole period", "same", w_["v2"])
        else:
            live = (f"from {lp['live_start']} ({lp['years']:.1f} years"
                    + (", too short to judge" if lp["too_short_to_judge"] else "") + ")",
                    pct(lp["excess_cagr"]["value"], 2, True), lp["v2"])
        v1e = w_.get("v1_excess_cagr")
        L.append(f"| {r['name']} | {pct(w_['excess_cagr']['value'], 2, True)} | {w_['v2']} | "
                 f"{pct(v1e['value'], 2, True) if v1e else ''} | {live[0]} | {live[1]} | "
                 f"{live[2]} | `{w_['excess_cagr']['source']}` |")
    L.append("")
    beat = [r["name"] for r in fi.values() if r["factor_index"] and r["whole_period"]["v2"] == "beat"]
    lost = [r["name"] for r in fi.values() if r["factor_index"]
            and r["whole_period"]["v2"] == "did not beat"]
    tie = [r["name"] for r in fi.values() if r["factor_index"] and r["whole_period"]["v2"] == "tie"]
    live = [r for r in fi.values() if "excess_cagr" in r.get("live_part", {})]
    differ = [r["name"] for r in live if r["live_part"]["v2"] != r["whole_period"]["v2"]]
    short = [f"{r['name']} ({r['live_part']['years']:.1f} years)" for r in live
             if r["live_part"]["too_short_to_judge"]]
    ns = rep["factor_indices"]["no_overnight_sensitivity"]
    L.append(f"Over the whole period v2 beat "
             f"{and_list(beat) if beat else 'none of the five factor indices'}"
             + (f" and did not beat {and_list(lost)}" if lost else "")
             + (f"; a tie with {and_list(tie)}" if tie else "") + ". "
             + (f"In the live parts the answer differs for {and_list(differ)}. " if differ else
                "Each live part gives the same answer. ")
             + "A live part starts on the first date NSE printed an opening level for the index "
               "(jobs/attribution.py, LIVE_START), used because NSE's launch documents could not "
               "be retrieved; the three indices with an open at the clock's start count as live "
               "for the whole period. "
             + (f"Too short to judge on its own (under {SHORT_LIVE_YEARS} years live): "
                f"{and_list(short)}. " if short else "")
             + f"With no overnight move where NSE printed no open, the excess changes by at most "
               f"{ns['largest_change_in_excess'] * 100:.3f} points and "
             + ("no answer changes." if not ns["ties"] else
                f"the answer changes for {and_list(ns['ties'])}, called a tie."))
    L.append("")

    # ---- A5
    q = rep["quality_factor"]["rows"]
    L.append("## Is the alpha a quality premium? (A5)")
    L.append("")
    L.append("| Version | Months | v2 alpha without, t | v2 alpha with, t | v2 quality loading | "
             "v1 alpha without, t | v1 alpha with, t | Source (v2) |")
    L.append("|---|---|---|---|---|---|---|---|")
    for v, r in q.items():
        a, o = r["v2"], r["v1"]
        L.append(f"| {v}{' (robustness only)' if v.endswith('no_r6') else ''} | {r['months']} "
                 f"({r['first']} to {r['last']}) | {pct(a['without_alpha']['value'])}, "
                 f"{a['without_t']['value']:.2f} | {pct(a['with_alpha']['value'])}, "
                 f"{a['with_t']['value']:.2f} | {a['quality_loading']['value']:.2f} | "
                 f"{pct(o['without_alpha']['value'])}, {o['without_t']['value']:.2f} | "
                 f"{pct(o['with_alpha']['value'])}, {o['with_t']['value']:.2f} | "
                 f"`{a['without_alpha']['source'].rsplit('.without', 1)[0]}` |")
    L.append("")
    sent = []
    for v, what in (("margin", "over the whole period (margin version)"),
                    ("roce", f"over the {q['roce']['months']} ROCE months, {q['roce']['first']} "
                             f"to {q['roce']['last']},")):
        a = q[v]["v2"]
        wo, wi = a["without_alpha"]["value"], a["with_alpha"]["value"]
        sent.append(f"{what} the quality factor moves v2's alpha from {pct(wo)} (t "
                    f"{a['without_t']['value']:.2f}) to {pct(wi)} (t {a['with_t']['value']:.2f}), "
                    f"a loading of {a['quality_loading']['value']:.2f}")
    L.append(sent[0][0].upper() + sent[0][1:] + "; " + sent[1] + ". "
             + ("None of these four alphas has a t of 1.96 or more, the usual 5% level."
                if all(abs(q[v]["v2"][k]["value"]) < 1.96 for v in ("margin", "roce")
                       for k in ("without_t", "with_t")) else
                "See the t-statistics in the table."))
    L.append("")

    # ---- A6
    t = rep["after_tax"]
    L.append("## After Indian costs and tax (A6)")
    L.append("")
    L.append("| | v2 | v1 | Nifty 500 index fund |")
    L.append("|---|---|---|---|")
    L.append(f"| Value at the end, sold, after costs and tax, from Rs {t['capital']['value']:,.0f} | "
             f"Rs {t['v2']['end_value_liquidated']['value']:,.0f} | "
             f"Rs {t['v1']['end_value_liquidated']['value']:,.0f} | "
             f"Rs {t['fund']['end_value_liquidated']['value']:,.0f} |")
    L.append(f"| After-tax CAGR | {pct(t['v2']['cagr']['value'])} | {pct(t['v1']['cagr']['value'])} | "
             f"{pct(t['fund']['cagr']['value'])} |")
    L.append(f"| After-tax maximum drawdown | {pct(t['v2']['max_drawdown']['value'])} | "
             f"{pct(t['v1']['max_drawdown']['value'])} | {pct(t['fund']['max_drawdown']['value'])} |")
    L.append(f"| Tax paid along the way | Rs {t['v2']['tax_paid_during']['value']:,.0f} | "
             f"Rs {t['v1']['tax_paid_during']['value']:,.0f} | |")
    L.append("")
    L.append(f"After tax v2 {'beat' if t['v2_beats_fund'] else 'did not beat'} the index fund "
             f"({pts(t['v2']['excess_cagr_vs_fund']['value'])} a year) and "
             f"{'beat' if t['v2_beats_v1'] else 'trailed'} v1 by "
             f"{abs(t['v2_minus_v1_cagr']) * 100:.2f} points a year. Sources: "
             f"`{t['v2']['cagr']['source']}`, `{t['v1']['cagr']['source']}`, "
             f"`{t['fund']['cagr']['source']}` and the keys beside them in `v2_verdict.json`.")
    L.append("")

    # ---- luck and search
    mk, ds = rep["monkey_test"], rep["deflated_sharpe"]
    p, f = mk["persistent"], mk["fresh"]
    L.append("## Luck: the monkey test")
    L.append("")
    L.append(f"The v2 engine replayed the run to {mk['replay_worst_rel']['value']:.1e} of its own "
             f"NAV, then ran {p['draws']['value']} random rankings of each kind through the same "
             f"rules. With a random but persistent ranking, {p['beaten_by_n_random']['value']} did "
             f"at least as well as v2's {mk['strategy_cagr_pct']['value']:.2f}% (median "
             f"{p['random_median_pct']['value']:.2f}%, v2 at percentile "
             f"{p['strategy_percentile']['value']:.1f}; v1 was at "
             f"{p['v1']['strategy_percentile']['value']:.1f}); with a fresh random ranking each "
             f"quarter, {f['beaten_by_n_random']['value']} of {f['draws']['value']} (median "
             f"{f['random_median_pct']['value']:.2f}%, percentile "
             f"{f['strategy_percentile']['value']:.1f}; v1 {f['v1']['strategy_percentile']['value']:.1f}). "
             f"The random books' median turnover was {f['random_median_turnover']['value']:.2f} "
             f"times a year with a fresh ranking and {p['random_median_turnover']['value']:.2f} with a "
             f"persistent one, against v2's {mk['strategy_turnover']['value']:.2f}. So much of v2's lead "
             f"over the fresh kind is their extra trading cost, and the persistent kind is the "
             f"fairer comparison. "
             f"Source: `{p['strategy_percentile']['source'].rsplit('.', 2)[0]}`.")
    L.append("")
    L.append("## The search: deflated Sharpe")
    L.append("")
    probs = [ds[k]["value"] for k in ("prob_null_sampling_variance",
                                      "prob_cross_trial_variance_36_grid", "prob_grid_trials_only")]
    tf = ds["trials_file"]
    L.append(f"Counting all {ds['n_trials']['value']} trials in `{tf['path']}` (both v2 runs are "
             f"lines {', '.join(map(str, tf['strategy_v2_records_at_lines']))}), v2's Sharpe of "
             f"{ds['sharpe_annual']['value']:.2f} gives a probability of skill between "
             f"{min(probs):.3f} and {max(probs):.3f} depending on the variance used "
             f"(`{ds['n_trials']['source'].rsplit('.', 1)[0]}`); v1's, counted at "
             f"{ds['v1']['n_trials']['value']} trials, was between "
             f"{min(ds['v1'][k]['value'] for k in ('prob_null_sampling_variance', 'prob_cross_trial_variance_36_grid', 'prob_grid_trials_only')):.3f} and "
             f"{max(ds['v1'][k]['value'] for k in ('prob_null_sampling_variance', 'prob_cross_trial_variance_36_grid', 'prob_grid_trials_only')):.3f}.")
    L.append("")

    # ---- both variants
    va = rep["variants"]
    L.append("## Both variants and v1, for the record")
    L.append("")
    L.append("| Measure | v2 (a) | v2 (b) | v1 |")
    L.append("|---|---|---|---|")
    rows = [("CAGR", "cagr", lambda x: pct(x)), ("Equal-weight universe CAGR", "universe_ew_cagr",
                                                 lambda x: pct(x))] + \
           [(label[q], q, show[q]) for q in A2_ORDER]
    for lab, k, fn in rows:
        L.append(f"| {lab} | {fn(va['a'][k]['value'])} | {fn(va['b'][k]['value'])} | "
                 f"{fn(va['v1'][k]['value'])} |")
    L.append("")
    L.append("Each figure's file and key are under `variants` in `v2_verdict.json`. The two v2 "
             "variants trade the same names; the equal-weight universe is v2's (rules 1 to 7 and "
             "the hard filters), which is why it differs from v1's.")
    L.append("")
    L.append("## Files read")
    L.append("")
    L.append("| File | sha256 (line endings normalised) |")
    L.append("|---|---|")
    for k, v in rep["sources"].items():
        L.append(f"| `{k}` | `{v[:16]}...` |")
    L.append("")
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--v1", default=str(V1_RUN), help="v1's measured run (the A2 baseline)")
    ap.add_argument("--a", default=str(A_RUN), help="v2 variant (a)")
    ap.add_argument("--b", default=str(B_RUN), help="v2 variant (b)")
    ap.add_argument("--out", default=str(BACKTEST), help="folder for v2_verdict.*")
    args = ap.parse_args(argv)
    out = Path(args.out)
    rep, w, splits = build(Path(args.v1), Path(args.a), Path(args.b))
    wf, sf = out / "v2_verdict_windows_3y.csv", out / "v2_verdict_cscv_splits.csv"
    w.assign(start=w["start"].astype(str), end=w["end"].astype(str)).to_csv(wf, index=False)
    splits.to_csv(sf, index=False)
    rep["safeguard_2"]["windows_3y"]["file"] = br.repo_path(wf)
    rep["safeguard_2"]["cscv_a1_choice"]["splits_file"] = br.repo_path(sf)
    (out / "v2_verdict.json").write_text(json.dumps(rep, indent=2), encoding="utf-8")
    (out / "v2_verdict.md").write_text(markdown(rep) + "\n", encoding="utf-8")
    print(f"A1 adopts ({rep['a1_adoption']['adopted']}); A2: {rep['a2_verdict']['verdict']} "
          f"(passes {rep['a2_verdict']['rules_passed']}, fails {rep['a2_verdict']['rules_failed']})")
    print(f"written to {out / 'v2_verdict.json'}, .md, {wf.name} and {sf.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
