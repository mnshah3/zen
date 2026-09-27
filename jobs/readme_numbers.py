"""The numbers in README.md, rendered from committed outputs and the archive.

README.md is the project's public front page. Every figure in it that comes
from a backtest or from the archive sits between a pair of markers

    <!-- numbers:NAME:start -->
    ...
    <!-- numbers:NAME:end -->

and this module renders what goes between them. The sources are:

- the v1 baseline run, data/backtest/v1_final: baseline_report.json and the
  measurement outputs it cites (libcheck.json, verify.json, stats.json,
  attribution.json, after_tax/summary.json, metrics.json), whose checksums
  must still match the ones the report recorded, plus the run's own
  nav.csv, in_sample_end_open.csv, holdings.csv, universe_funnel.csv and
  trade_for_trade.csv;
- the independent checker's run of the same rules, data/backtest/v1_final_check;
- the two earlier runs kept on record: data/backtest/v1_holdout, the one-shot
  held-back test of 22 Sep 2026, and data/backtest/v1_corrected, 23 Sep 2026;
- the archive, data/zen.duckdb, opened read-only and counted up to
  ARCHIVE_AS_OF, so the counts do not move when a new session is appended.

Nothing here re-measures a return. Apart from rounding for display, the only
figures computed here are counts, means, the engines' agreement, and the
worst falls inside the held-back period, which no job writes: those use the
same running-peak rule as metrics.json, and the whole-period worst falls
computed the same way must equal metrics.json's or nothing is rendered.

tests/test_readme_numbers.py checks every marked block in README.md against
this output. The archive blocks are skipped when the database is absent,
locked by a writer, or ends before ARCHIVE_AS_OF.

    python -m jobs.readme_numbers            # print every block
    python -m jobs.readme_numbers --check    # exit 1 if README.md is out of date
    python -m jobs.readme_numbers --write    # rewrite the marked blocks in README.md
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import textwrap
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pandas as pd

from jobs import baseline_report
from jobs.baseline_report import SHORT_LIVE_YEARS

ROOT = Path(__file__).resolve().parents[1]
README = "README.md"
RUN = "data/backtest/v1_final"
CHECK = "data/backtest/v1_final_check"
ONE_SHOT = "data/backtest/v1_holdout"
CORRECTED = "data/backtest/v1_corrected"
DB = "data/zen.duckdb"

# The archive counts stop here. Moving it is a deliberate edit, followed by --write.
ARCHIVE_AS_OF = "2026-09-23"
ARCHIVE_BLOCKS = ("archive", "survivorship", "survivorship_months")
SURVIVORSHIP_MONTHS = ("2015-03", "2023-06")      # plus the month of ARCHIVE_AS_OF

# The three runs of the same rules, oldest first. Dates are the commits that
# published them: 6223419 (22 Sep), e0c22a9 (23 Sep) and d3eb964 (26 Sep, 23:02 UTC).
# The current run: production engine on 25 Sep, the independent engine, the
# comparison and the measurements on 26 Sep.
RUNS = (
    (ONE_SHOT, "22 Sep 2026", "the one-shot test, as run"),
    (CORRECTED, "23 Sep 2026", "March 2025 quarter refilled, revised filings added (Clarification 36)"),
    (RUN, "25-26 Sep 2026", "only quarter-length figures read as a quarter (Clarifications 38 and 39); "
                         "the current result"),
)

# Stocks the earlier runs bought at this rebalance while the archive held their
# April-to-September figures as the September 2025 quarter. Evidence:
# data/financials/2025-09.parquet before and after commit bca9809 (Clarification
# 38) blanks the income statement of the filings each one had before 17 Nov 2025
# (CESC 17 Oct, FMGOETZE 11 Nov, IMPAL 29 Oct), whose revenue was about twice the
# later quarter-only revision. render() refuses if the runs' holdings disagree.
HALF_YEAR_REBALANCE = "2025-11-17"
HALF_YEAR_BUYS = {ONE_SHOT: ("CESC", "FMGOETZE", "IMPAL"), CORRECTED: ("IMPAL",)}

INDEX_ROWS = ("nifty500", "midcap150", "smallcap250", "momentum30", "value50", "quality30",
              "lowvol30", "alpha50")
MARK = re.compile(r"^<!-- numbers:([a-z_]+):(start|end) -->$")


class ArchiveUnavailable(Exception):
    """The database cannot give the archive counts; the archive blocks are skipped."""


# ---------------------------------------------------------------- formatting
def pct(x: float, nd: int = 1, sign: bool = False) -> str:
    """A fraction as a percentage: 0.3068 -> '30.7%'."""
    return f"{x * 100:+.{nd}f}%" if sign else f"{x * 100:.{nd}f}%"


def one_dp(v: float) -> str:
    """A percentage already rounded to 2 decimals, shown to 1 ('34.31' -> '34.3').
    Refuses a second decimal of 5, where the unrounded value decides the answer."""
    s = f"{v:.2f}"
    if s.endswith("5"):
        raise ValueError(f"{s} cannot be shown to one decimal without its unrounded value")
    return str(Decimal(s).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def num(n: int) -> str:
    return f"{int(n):,}"


def words(n: int) -> str:
    """Small counts in words, as prose writes them."""
    w = ("no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten")
    return w[n] if 0 <= n < len(w) else num(n)


def ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def lakh(rupees: float) -> str:
    v = rupees / 1e5
    return f"{v:.0f}" if abs(v - round(v)) < 1e-9 else f"{v:.1f}"


def _d(iso) -> date:
    return iso if isinstance(iso, date) and not isinstance(iso, datetime) else \
        datetime.strptime(str(iso)[:10], "%Y-%m-%d").date()


def day(iso) -> str:
    d = _d(iso)
    return f"{d.day} {d:%b %Y}"


def month(iso) -> str:
    return f"{_d(iso):%b %Y}"


def wrap(text: str, first: str = "", rest: str = "", width: int = 78) -> str:
    """Wrap a paragraph, never starting a continuation line with something
    Markdown would read as a list item, heading, quote or table row."""
    bad = re.compile(r"([-+*]\s|\d+[.)](\s|$)|#|>|\|)")
    for w in range(width, max(width - 20, 20), -1):
        lines = textwrap.wrap(text, width=w, initial_indent=first, subsequent_indent=rest,
                              break_long_words=False, break_on_hyphens=False)
        if not any(bad.match(line[len(rest):]) for line in lines[1:]):
            return "\n".join(lines)
    raise ValueError(f"cannot wrap safely: {text[:60]}")


def bullet(text: str) -> str:
    return wrap(text, first="- ", rest="  ")


# ---------------------------------------------------------------- inputs
def load_json(root: Path, rel: str) -> dict:
    return json.loads((root / rel).read_text(encoding="utf-8"))


def check_sources(root: Path, rep: dict) -> None:
    """The files baseline_report.json was built from must be the ones committed now."""
    for name, src in rep["sources"].items():
        p = root / Path(src["path"].replace("\\", "/"))
        got = baseline_report.sha256(p)                 # line endings normalised
        if got != src["sha256"]:
            raise ValueError(f"{p} changed after baseline_report.json was written; "
                             f"re-run jobs.baseline_report first")


def targets(root: Path, run: str) -> dict[str, frozenset]:
    """The stocks held after each decision date's trades (buys and keeps)."""
    h = pd.read_csv(root / run / "holdings.csv")
    h = h[h["action"] != "sell"]
    return {d: frozenset(g["symbol"]) for d, g in h.groupby("D")}


def worst_falls(nav: pd.DataFrame, cols: list[str]) -> dict[str, float]:
    """Deepest fall from a running peak, per column, over the rows given."""
    v = nav[cols].astype(float)
    return (v / v.cummax() - 1).min().to_dict()


# ---------------------------------------------------------------- the v1 blocks
def run_blocks(root: Path) -> dict[str, str]:
    rep = load_json(root, f"{RUN}/baseline_report.json")
    check_sources(root, rep)
    metrics = load_json(root, f"{RUN}/metrics.json")
    stats = load_json(root, f"{RUN}/stats.json")
    verify = load_json(root, f"{RUN}/verify.json")
    tax_src = load_json(root, f"{RUN}/after_tax/summary.json")
    ft = rep["final_test"]["value"]
    idx = ft["index_tri_cagr_pct"]
    rows = rep["benchmarks"]["rows"]
    head = rep["headline"]
    start, end = rep["period"]["start"], rep["period"]["end"]
    whole_years = rows["nifty500"]["whole_period"]["years"]
    split = ft["clock"].split()[2]                      # "open of 2023-02-15 to open of ..."
    if not ft["clock"].startswith(f"open of {split} to open of {end}"):
        raise ValueError(f"unexpected held-back clock: {ft['clock']}")

    # worst falls: whole period must reproduce metrics.json; held back from the split open
    nav = pd.read_csv(root / RUN / "nav.csv")
    cols = ["strategy", "universe_ew", *INDEX_ROWS]
    whole_dd = worst_falls(nav, cols)
    want = {"strategy": metrics["max_drawdown"],
            **{c: metrics["vs"][c]["max_drawdown"] for c in cols[1:]}}
    for c in cols:
        if abs(whole_dd[c] - want[c]) > 1e-12:
            raise ValueError(f"worst fall of {c} does not reproduce metrics.json")
    so = pd.read_csv(root / RUN / "in_sample_end_open.csv")
    if str(so["date"].iloc[0]) != split:
        raise ValueError("in_sample_end_open.csv is not at the held-back start")
    later = nav[(nav["date"] > split) | ((nav["date"] == split) & (nav["mark"] == "close"))]
    held_dd = worst_falls(pd.concat([so[cols], later[cols]], ignore_index=True), cols)

    out = {}

    # ---- headline
    out["headline"] = wrap(
        f"The answer, on {ft['years']:.1f} years the design had never seen: "
        f"**{one_dp(ft['strategy_cagr_pct'])}% a year against {one_dp(idx['nifty500'])}% for "
        f"the Nifty 500 including dividends**, and {one_dp(ft['universe_ew_cagr_pct'])}% for "
        f"the same universe equally weighted. That is the held-back test as re-run on "
        f"{RUNS[-1][1]} on a repaired archive, with the rules for picking stocks unchanged; "
        f"the first run stays on record [below](#the-result). Over the whole "
        f"{whole_years:.1f} years from {month(start)} it made "
        f"{pct(head['strategy_cagr']['value'])} a year, and its worst fall was "
        f"{pct(whole_dd['strategy'])} against {pct(whole_dd['nifty500'])} for the Nifty 500 "
        f"and {pct(whole_dd['universe_ew'])} for the same universe.")

    # ---- history: three runs of the same rules
    lines = ["| Run | What changed | Held back, a year | Same universe, equal weight, "
             "held back | Whole period, a year |",
             "|---|---|---|---|---|"]
    star = False
    for run, when, what in RUNS:
        lib = load_json(root, f"{run}/libcheck.json")["final_test"]
        met = load_json(root, f"{run}/metrics.json")
        mark = ""
        if lib["clock"].startswith("close of"):
            mark, star = "\\*", True
        elif not lib["clock"].startswith(f"open of {split}"):
            raise ValueError(f"{run}: unexpected clock {lib['clock']}")
        s, e, w = (f"{lib['strategy_cagr_pct']:.2f}%{mark}",
                   f"{lib['universe_ew_cagr_pct']:.2f}%{mark}", pct(met["cagr"], 2))
        if run == RUN:
            s, w = f"**{s}**", f"**{w}**"
        lines.append(f"| {when} | {what} | {s} | {e} | {w} |")
    if star:
        lines += ["", wrap(f"\\* From the close of {day(split)}, the clock used at the time. "
                           f"The later runs start at the open, as Clarification 16 requires "
                           f"(Clarification 37).")]
    now = targets(root, RUN)
    corr = targets(root, CORRECTED)
    bought, n_bought = {}, {}
    for run, expected in HALF_YEAR_BUYS.items():
        h = pd.read_csv(root / run / "holdings.csv")
        b = h[(h["D"] == HALF_YEAR_REBALANCE) & (h["action"] == "buy")]["symbol"]
        n_bought[run] = len(set(b))
        got = tuple(sorted(set(b) - now[HALF_YEAR_REBALANCE]))
        if got != tuple(sorted(expected)):
            raise ValueError(f"{run}: bought {got} at {HALF_YEAR_REBALANCE} that the current run "
                             f"does not hold, expected {expected}")
        bought[run] = got
    differ = sorted(d for d in now if now[d] != corr.get(d))
    if differ != [HALF_YEAR_REBALANCE] or set(corr) != set(now):
        raise ValueError(f"the current and 23 Sep runs differ at {differ}")
    instead = sorted(now[HALF_YEAR_REBALANCE] - corr[HALF_YEAR_REBALANCE])
    replaced = sorted(corr[HALF_YEAR_REBALANCE] - now[HALF_YEAR_REBALANCE])
    oneshot = bought[ONE_SHOT]
    second = RUNS[1][1].rsplit(" ", 1)[0]               # "23 Sep"
    t4t = pd.read_csv(root / RUN / "trade_for_trade.csv")
    n_ew = int((t4t["portfolio"] == "universe_ew").sum())
    n_st = int((t4t["portfolio"] == "strategy").sum())
    lines += ["", wrap(
        f"At the {day(HALF_YEAR_REBALANCE)} rebalance the one-shot run bought "
        f"{words(n_bought[ONE_SHOT])} stocks, and {words(len(oneshot))} of them, "
        f"{', '.join(oneshot[:-1])} and {oneshot[-1]}, were bought while the archive held "
        f"their April-to-September figures as the September quarter; the {second} run still "
        f"bought {' and '.join(bought[CORRECTED])} that way. The current run holds "
        f"{' and '.join(instead)} in place of {' and '.join(replaced)} for that quarter and "
        f"picks the same stocks as the {second} run at every other rebalance. Counting "
        f"trade-for-trade sessions as trading days changed {words(n_ew)} holdings of the "
        f"equal-weight universe and {'none' if n_st == 0 else words(n_st)} of the strategy's "
        f"([`trade_for_trade.csv`]({RUN}/trade_for_trade.csv)).")]
    out["history"] = "\n".join(lines)

    # ---- results: both periods against every benchmark
    names = {"strategy": "**The strategy**", "universe_ew": "The same universe, equally weighted",
             **{c: rows[c]["name"] for c in INDEX_ROWS}}
    whole = {"strategy": head["strategy_cagr"]["value"], "universe_ew": head["universe_ew_cagr"]["value"],
             **{c: rows[c]["whole_period"]["benchmark_cagr"] for c in INDEX_ROWS}}
    held = {"strategy": ft["strategy_cagr_pct"], "universe_ew": ft["universe_ew_cagr_pct"],
            **{c: idx[c] for c in INDEX_ROWS}}
    live = {c: rows[c]["live_part"] for c in INDEX_ROWS if "live_part" in rows[c]}
    later_live = [c for c in live if live[c]["from"] != rows[c]["whole_period"]["from"]]
    dagger = {c: " †" for c in later_live}
    lines = [wrap(f"Open of {day(start)} to open of {day(end)}, {whole_years:.1f} years, of which "
                  f"the last {ft['years']:.1f}, from the open of {day(split)}, were held back:"),
             "",
             "| | Whole period, a year | Worst fall | Held back, a year | Worst fall |",
             "|---|---|---|---|---|"]
    for c in cols:
        cells = [pct(whole[c], 2), pct(whole_dd[c]), f"{held[c]:.2f}%", pct(held_dd[c])]
        if c == "strategy":
            cells = [f"**{x}**" for x in cells]
        lines.append(f"| {names[c]}{dagger.get(c, '')} | " + " | ".join(cells) + " |")
    ahead_whole = all(whole["strategy"] > whole[c] for c in cols[1:])
    ahead_held = all(held["strategy"] > held[c] for c in cols[1:])
    note = ("Indices are NSE's total return indices, dividends included, on the strategy's "
            "clock; where NSE printed no open for a factor index, its parent index's overnight "
            "move stands in (v2-spec, Clarification to A4)."
            + (" The strategy is ahead of every one of them in both periods."
               if ahead_whole and ahead_held else ""))
    lines += ["", wrap(note)]
    if later_live:
        parts = []
        for c in later_live:
            lp = live[c]
            s = (f"{rows[c]['name']} is live from {day(lp['live_start'])}, and over those "
                 f"{lp['years']:.1f} years the strategy made {pct(lp['strategy_cagr'])} a year "
                 f"against the index's {pct(lp['benchmark_cagr'])}")
            if lp["years"] < SHORT_LIVE_YEARS:
                s += ", which is too short to judge"
            if lp["live_start"] > split:
                back = (_d(lp["live_start"]) - _d(split)).days / 365.25
                s += (f"; the first {back:.1f} of its {ft['years']:.1f} held-back years are "
                      f"back-test")
            parts.append(s + ".")
        whole_live = [rows[c]["name"] for c in live if c not in later_live]
        lines += ["", wrap(
            "† A factor index's figures from before it went live are NSE's back-test of its "
            "own rules, not returns anyone could have earned. NSE's launch dates could not be "
            "read from an NSE document, so each live part starts at the first date NSE printed "
            "an open for the index. " + " ".join(parts)
            + (f" The other {words(len(whole_live))} factor indices are live over the whole "
               f"period." if whole_live else ""))]
    out["results"] = "\n".join(lines)

    # ---- what backs it up
    chk_nav = pd.read_csv(root / CHECK / "nav.csv")
    chk_h = pd.read_csv(root / CHECK / "holdings.csv")
    if not (nav[["date", "mark"]].to_numpy() == chk_nav[["date", "mark"]].to_numpy()).all():
        raise ValueError("the two engines' NAV files are not on the same clock")
    series = [c for c in nav.columns if c not in ("date", "mark")]
    if sorted(series) != sorted(c for c in chk_nav.columns if c not in ("date", "mark")):
        raise ValueError("the two engines' NAV files have different columns")
    a = nav[series] / nav[series].iloc[0]
    b = chk_nav[series] / chk_nav[series].iloc[0]
    worst_rel = float(((a - b).abs() / b.abs()).to_numpy().max())
    bound = 10.0 ** math.ceil(math.log10(worst_rel)) if worst_rel > 0 else 0.0
    if bound > 1e-9:
        raise ValueError(f"the two engines' values differ by {worst_rel:.1e}")
    prod = pd.read_csv(root / RUN / "holdings.csv")
    prod = prod[prod["action"] != "sell"]
    key_p = set(zip(prod["D"], prod["ticker"], prod["rank"].astype(int),
                    prod["action"].map({"buy": "new", "keep": "kept"})))
    key_c = set(zip(chk_h["D"], chk_h["symbol"], chk_h["rank"].astype(int), chk_h["status"]))
    if key_p != key_c or len(prod) != len(chk_h):
        raise ValueError("the two engines' holdings differ")
    n_dates = prod["D"].nunique()
    n_idx = len(series) - 2
    mk = rep["monkey_test"]["value"]
    p, f = mk["persistent"], mk["fresh"]
    mi = ft["margin_interval"]
    cap_up, cap_dn = stats["strategy"]["up_capture_pct"], stats["strategy"]["down_capture_pct"]
    roll_n = stats["rolling_12m_vs_nifty500_tri"]["beat_pct_of_time"]
    roll_u = stats["rolling_12m_vs_universe_ew"]["beat_pct_of_time"]
    backing = [
        bullet(f"**Two engines agree exactly.** The production engine and an independent "
               f"re-implementation written from the specification alone "
               f"([`jobs/crosscheck_v1.py`](jobs/crosscheck_v1.py)) hold the same stocks with "
               f"the same ranks on all {n_dates} decision dates, {len(prod)} positions in all, "
               f"and their daily values agree on all {len(series)} series (the strategy, the "
               f"equal-weight universe and {n_idx} index series) to within {bound:.0e} of each "
               f"other ([`v1_final_check`]({CHECK}))."),
        bullet(f"**It beats random picks from the same list.** Against {p['draws']} random "
               f"portfolios run through the same engine and rules with one random ranking kept "
               f"for the whole run, it lands at the {p['strategy_percentile']:g}th percentile: "
               f"{p['beaten_by_n_random']} did at least as well, and the median random "
               f"portfolio made {p['random_median_pct']:.1f}% a year. Against {f['draws']} with a "
               f"fresh random ranking each quarter it lands at the "
               f"{f['strategy_percentile']:g}th: {f['beaten_by_n_random']} did as well, and the "
               f"median random portfolio made {f['random_median_pct']:.1f}%. "
               f"The first kind turned over {p['random_median_turnover']:g} times a year, the "
               f"second {f['random_median_turnover']:g}, the strategy {mk['strategy_turnover']:g}."),
        bullet(f"**It fell less than the market over the whole period.** In the months the "
               f"Nifty 500 rose, the strategy rose {cap_up / 100:.2f} times as much on average; in "
               f"the months it fell, {cap_dn / 100:.2f} times as much. Over rolling twelve-month "
               f"windows it was ahead of the Nifty 500 {roll_n:.0f}% of the time and of the same "
               f"universe {roll_u:.0f}%."
               + (f" That did not hold within the held-back period on its own, where its worst "
                  f"fall, {pct(held_dd['strategy'])}, was deeper than the Nifty 500's "
                  f"{pct(held_dd['nifty500'])}."
                  if held_dd["strategy"] < held_dd["nifty500"] else "")),
        bullet(f"**The margin over the same universe is probably real, but not proven.** In the "
               f"held-back period it is {mi['point_pct']:.2f} points a year. A 90% interval from "
               f"a stationary bootstrap of the paired daily returns runs from "
               f"{mi['lo90_pct']:+.2f} to {mi['hi90_pct']:+.2f} points, and "
               f"{mi['share_of_draws_le_zero'] * 100:.1f}% of the resamples put the margin at "
               f"zero or below."),
    ]
    out["backing"] = "\n".join(backing)

    # ---- what it does not show
    fa = rep["factor_attribution"]
    ff = fa["iima_four_factor"]["value"]
    q = fa["with_quality_factor"]
    m, r = q["margin"], q["roce"]
    at = rep["a2_acceptance_quantities"]["iima_alpha_t"]
    share = 1 - r["with"]["alpha_annual"] / r["without"]["alpha_annual"]
    ds = rep["deflated_sharpe"]["value"]
    probs = [ds[k] for k in ("prob_null_sampling_variance", "prob_cross_trial_variance_36_grid",
                             "prob_grid_trials_only")]
    wd = stats["worst_drawdowns"]                      # the five deepest, deepest first
    first_two = wd[:2]
    in_held = [w for w in wd if w["start"][:10] >= split]
    wd_open = [w for w in wd if w["recovered"] is None]
    yr = verify["by_year"][end[:4]]

    def episode(w):
        span = (f"from {month(w['start'])} to {month(w['trough'])}"
                if month(w["start"]) != month(w["trough"]) else f"in {month(w['start'])}")
        rec = (f", back to its peak by {month(w['recovered'])}" if w["recovered"]
               else ", not yet recovered at the end of the run")
        return f"{w['depth_pct']:.1f}% {span}{rec}"

    def updown(v):
        return f"{'down' if v < 0 else 'up'} {abs(v):.1f}%"

    limits = [
        bullet(f"**Skill beyond known factors.** Regressed on IIM Ahmedabad's published Indian "
               f"four factors over the {ff['months']} months from {month(ff['first'] + '-01')} "
               f"to {month(ff['last'] + '-01')}, the last month IIMA has published, the "
               f"strategy's alpha is {ff['alpha_annual_pct']:.2f}% a year at t = "
               f"{ff['alpha_t']:.2f} ({at['hand_rolled_with_small_sample_correction']['value']:.2f} "
               f"with a small-sample correction), which is not significant. Its value and "
               f"momentum tilts are, strongly (t = {ff['loading_t']['HML']:.2f} and "
               f"{ff['loading_t']['WML']:.2f}), and the four factors explain "
               f"{ff['r_squared'] * 100:.0f}% of its monthly returns' variation. Most of the "
               f"return is the market plus factors that can be bought more cheaply."),
        bullet(f"**That what is left is more than a quality tilt.** Adding a quality factor "
               f"built from the archive (v2-spec A5, operating-margin version, the same "
               f"{m['months']} months) lowers the alpha to {pct(m['with']['alpha_annual'], 2)} "
               f"(t = {m['with']['alpha_t']:.2f}). Over the {r['months']} months from "
               f"{month(r['first'] + '-01')}, where the factor can use return on capital, the "
               f"alpha is {pct(r['without']['alpha_annual'], 2)} (t = "
               f"{r['without']['alpha_t']:.2f}) without the factor, which on its own would count "
               f"as significant, and {pct(r['with']['alpha_annual'], 2)} (t = "
               f"{r['with']['alpha_t']:.2f}) with it: quality accounts for {share:.0%} of the "
               f"alpha in that window, which is only {r['months'] / 12:.1f} years long."),
        bullet(f"**Robustness to the search.** After {ds['n_trials']} logged trials, the "
               f"deflated Sharpe probability of skill is between {min(probs):.3f} and "
               f"{max(probs):.3f} depending on how the spread across trials is estimated, "
               f"around a usual bar of 0.95."),
        bullet(f"**A smooth ride.** The worst falls were "
               + ", and ".join(episode(w) for w in first_two) + ". "
               + (f"The deepest that started inside the held-back period was "
                  f"{episode(in_held[0])}. " if in_held and in_held[0] not in first_two else "")
               + f"From the end of {int(end[:4]) - 1} to the open of {day(end)} it is "
                 f"{updown(yr['strategy_pct'])} while the same universe is "
                 f"{updown(yr['universe_ew_pct'])}"
               + (f", and it is still below its {month(wd_open[0]['start'])} peak, having been "
                  f"{abs(wd_open[0]['depth_pct']):.1f}% below it at the worst in "
                  f"{month(wd_open[0]['trough'])}." if wd_open else ".")),
    ]
    out["limits"] = "\n".join(limits)

    # ---- after costs and tax
    tax = rep["after_tax"]
    fund = tax_src["benchmark"]["growth"]
    out["after_tax"] = wrap(
        f"**After Indian costs and tax.** From Rs {lakh(tax['capital'])} lakh at the open of "
        f"{day(start)}, with itemised Indian trading costs, an assumed "
        f"{tax_src['params']['slippage'] * 100:.1f}% slippage a side and capital gains tax by "
        f"lot at the rates in force at the time ([`jobs/after_tax.py`](jobs/after_tax.py), "
        f"v2-spec A6), the strategy would have been worth Rs "
        f"{lakh(tax['strategy_end_liquidated'])} lakh if sold at the open of {day(end)}, "
        f"{pct(tax['strategy_cagr'])} a year, after paying Rs {lakh(tax['tax_paid_during'])} "
        f"lakh of tax along the way; its worst fall on that basis was "
        f"{pct(tax['strategy_max_drawdown'])}. A Nifty 500 index fund with a "
        f"{fund['ter'] * 100:.2f}% expense ratio, held throughout and sold at the end, gives "
        f"Rs {lakh(tax['benchmark_growth_end_liquidated'])} lakh, "
        f"{pct(tax['benchmark_growth_cagr'])} a year. Before {day(fund['hypothetical_before'])}, "
        f"the start of the index fund whose costs are used, that fund is hypothetical.")

    # ---- the universe funnel
    fun = pd.read_csv(root / RUN / "universe_funnel.csv")
    size = fun["r7_mcap_computable"]
    out["funnel"] = wrap(
        f"Across the {len(fun)} decision dates that takes an average of "
        f"{num(round(fun['r1_ordinary_equity_recent_trade'].mean()))} ordinary-equity symbols "
        f"that traded recently down to an average of {num(round(size.mean()))}: "
        f"{num(size.iloc[0])} on the first date, when few companies had four quarters of tagged "
        f"results yet, up to {num(size.max())}, and {num(round(size[fun['D'] >= split].mean()))} "
        f"on average through the held-back period.")
    return out


# ---------------------------------------------------------------- the archive blocks
def archive_counts(db: Path, as_of: str = ARCHIVE_AS_OF) -> dict:
    """Counts from the database, read-only, up to and including `as_of`."""
    if not db.exists():
        raise ArchiveUnavailable(f"{db} is absent")
    import duckdb
    from zen.universe.identity import issuer_linkable, link
    try:
        con = duckdb.connect(str(db), read_only=True)
    except duckdb.Error as e:                          # absent, locked by a writer, ...
        raise ArchiveUnavailable(f"{db} cannot be opened: {e}") from e
    try:
        last = con.execute("SELECT max(date) FROM prices").fetchone()[0]
        if last is None or str(last) < as_of:
            raise ArchiveUnavailable(f"{db} ends on {last}, before {as_of}")
        q = lambda sql: con.execute(sql, [as_of]).fetchone()
        c = {"as_of": as_of}
        c["prices"] = q("SELECT count(*), count(DISTINCT date), count(DISTINCT symbol), min(date), "
                        "max(date) FROM prices WHERE date <= CAST(? AS DATE)")
        c["filings"] = q("SELECT count(*), count(DISTINCT symbol), min(an_dt), max(an_dt) FROM "
                         "announcements WHERE an_dt < CAST(? AS DATE) + INTERVAL 1 DAY")
        fu = q("SELECT count(*), count(DISTINCT symbol), min(period_end), max(period_end) "
               "FROM financials WHERE broadcast_dt < CAST(? AS DATE) + INTERVAL 1 DAY")
        # The first quarter with real coverage. A few earlier rows are stray re-tags
        # (17 before March 2018); v1 Clarification 21: tagged quarters start in Mar 2018.
        first = q("SELECT min(period_end) FROM (SELECT period_end FROM financials "
                  "WHERE broadcast_dt < CAST(? AS DATE) + INTERVAL 1 DAY "
                  "GROUP BY period_end HAVING count(*) >= 100)")[0]
        c["fundamentals"] = (fu[0], fu[1], first, fu[3])
        c["indices"] = q("SELECT count(DISTINCT index_name), min(date), max(date) FROM indices "
                         "WHERE date <= CAST(? AS DATE)")

        # survivorship: ordinary equity (ISIN type 01), linked across renames
        keys = con.execute("SELECT symbol, isin_code AS isin, min(date) AS first, max(date) AS last "
                           "FROM prices WHERE series IN ('EQ','BE') AND date <= CAST(? AS DATE) "
                           "GROUP BY 1, 2", [as_of]).df()
        cal = pd.DatetimeIndex(pd.to_datetime(con.execute(
            "SELECT DISTINCT date FROM prices WHERE date <= CAST(? AS DATE) ORDER BY date",
            [as_of]).df()["date"]))
        sp = link(keys, cal)
        sp["last"] = pd.to_datetime(sp["last"])
        m0 = pd.Timestamp(as_of).replace(day=1)
        eq = sp[sp["isin"].map(issuer_linkable)]
        now_cids = set(sp.loc[sp["last"] >= m0, "cid"])
        eq_cids = set(eq["cid"])
        gone = eq_cids - now_cids
        other = con.execute("SELECT DISTINCT symbol, isin_code, series FROM prices_other "
                            "WHERE date >= CAST(? AS DATE) AND date <= CAST(? AS DATE)",
                            [m0.date().isoformat(), as_of]).df()
        gsp = sp[sp["cid"].isin(gone)]
        hit_sym = gsp[gsp["symbol"].isin(other["symbol"])]
        hit_isin = gsp[gsp["isin"].isin(other["isin_code"])]
        elsewhere = set(hit_sym["cid"]) | set(hit_isin["cid"])
        series = set(other[other["symbol"].isin(hit_sym["symbol"])
                           | other["isin_code"].isin(hit_isin["isin"])]["series"])
        c["survivorship"] = {
            "symbols": int(eq["symbol"].nunique()),
            "symbols_now": int(eq.loc[eq["last"] >= m0, "symbol"].nunique()),
            "companies": len(eq_cids), "companies_now": len(eq_cids & now_cids),
            "gone": len(gone), "elsewhere": len(elsewhere), "elsewhere_series": sorted(series),
            "first_date": str(cal[0].date())}
        months = {}
        for mo in (*SURVIVORSHIP_MONTHS, as_of[:7]):
            lo = pd.Timestamp(mo + "-01")
            hi = min(lo + pd.offsets.MonthBegin(1) - pd.Timedelta(days=1), pd.Timestamp(as_of))
            months[mo] = con.execute(
                "SELECT count(DISTINCT symbol) FROM prices WHERE date BETWEEN CAST(? AS DATE) AND "
                "CAST(? AS DATE) AND length(isin_code) = 12 AND isin_code LIKE 'INE%' "
                "AND substr(isin_code, 8, 2) = '01'",
                [lo.date().isoformat(), hi.date().isoformat()]).fetchone()[0]
        if months[as_of[:7]] != c["survivorship"]["symbols_now"]:
            raise ValueError("month count and spell count disagree")
        c["months"] = months
        return c
    finally:
        con.close()


def archive_blocks(c: dict) -> dict[str, str]:
    p, f, fu, ix = c["prices"], c["filings"], c["fundamentals"], c["indices"]
    s = c["survivorship"]
    as_of = c["as_of"]
    out = {}
    out["archive"] = "\n".join([
        "```",
        f"{'prices':<12}{num(p[0]):>11} rows · {num(p[1])} sessions · {num(p[2])} ticker symbols "
        f"· {month(p[3])} to {month(p[4])}",
        f"{'filings':<12}{num(f[0]):>11} announcements · {num(f[1])} symbols · {month(f[2])} to "
        f"{month(f[3])}",
        f"{'fundamentals':<12}{num(fu[0]):>11} quarterly results filings · {num(fu[1])} symbols "
        f"· quarters ending {month(fu[2])} to {month(fu[3])}",
        f"{'indices':<12}{num(ix[0]):>11} NSE index series · {month(ix[1])} to {month(ix[2])}",
        "```",
        "",
        f"Counted up to {day(as_of)}."])
    where = ("NSE's trade-for-trade segment" if s["elsewhere_series"] == ["BZ"]
             else "other NSE series (" + ", ".join(s["elsewhere_series"]) + ")")
    out["survivorship"] = wrap(
        f"It isn't a small effect. Counting ordinary equity only (ISIN security type 01, so no "
        f"ETFs or rights entitlements), this archive holds **{num(s['symbols'])} ticker "
        f"symbols**. Linked across renames and ISIN changes by "
        f"[`zen/universe/identity.py`](zen/universe/identity.py), they belong to "
        f"**{num(s['companies'])} companies**, and {num(s['companies_now'])} of those traded in "
        f"NSE's main equity series in {month(as_of)}, up to the {ordinal(_d(as_of).day)}. That leaves "
        f"**{num(s['gone'])} companies** that were tradeable at some point since "
        f"{day(s['first_date'])} and are no longer quoted there. Of those, {num(s['elsewhere'])} "
        f"still trade in {where}; the other **{num(s['gone'] - s['elsewhere'])}** traded in no "
        f"NSE series at all that month.")
    rows = ["| Month | Ordinary equity symbols trading |", "|------|---------------------------|"]
    for mo, n in c["months"].items():
        label = month(mo + "-01") + (f", to the {ordinal(_d(as_of).day)}" if mo == as_of[:7] else "")
        rows.append(f"| {label} | {num(n)} |")
    out["survivorship_months"] = "\n".join(rows)
    return out


# ---------------------------------------------------------------- README plumbing
def read_blocks(text: str) -> dict[str, str]:
    """{name: content} for every marked block; raises on a malformed marker."""
    blocks, name, buf = {}, None, []
    for i, line in enumerate(text.splitlines(), 1):
        m = MARK.match(line.strip())
        if "<!-- numbers:" in line and not m:
            raise ValueError(f"line {i}: malformed marker {line!r}")
        if not m:
            if name is not None:
                buf.append(line)
            continue
        n, kind = m.groups()
        if kind == "start":
            if name is not None:
                raise ValueError(f"line {i}: {n} starts inside {name}")
            if n in blocks:
                raise ValueError(f"line {i}: {n} appears twice")
            name, buf = n, []
        else:
            if n != name:
                raise ValueError(f"line {i}: end of {n} without its start")
            blocks[n], name = "\n".join(buf), None
    if name is not None:
        raise ValueError(f"{name} has no end marker")
    return blocks


def replace_blocks(text: str, rendered: dict[str, str]) -> str:
    read_blocks(text)                                   # validate first
    out, skip = [], False
    for line in text.splitlines():
        m = MARK.match(line.strip())
        if m and m.group(2) == "start" and m.group(1) in rendered:
            out += [line, rendered[m.group(1)]]
            skip = True
            continue
        if m and m.group(2) == "end":
            skip = False
        if not skip:
            out.append(line)
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def render(root: Path = ROOT, db: Path | None = None) -> tuple[dict[str, str], dict[str, str]]:
    """(blocks, skipped): every block this module can render now, and the archive
    blocks it could not, with the reason."""
    root = Path(root)
    blocks = run_blocks(root)
    skipped = {}
    try:
        blocks |= archive_blocks(archive_counts(Path(db) if db else root / DB))
    except ArchiveUnavailable as e:
        skipped = {b: str(e) for b in ARCHIVE_BLOCKS}
    return blocks, skipped


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--write", action="store_true", help="rewrite the marked blocks in README.md")
    g.add_argument("--check", action="store_true", help="exit 1 if README.md is out of date")
    args = ap.parse_args(argv)
    blocks, skipped = render()
    for b, why in skipped.items():
        print(f"skipped {b}: {why}", file=sys.stderr)
    path = ROOT / README
    text = path.read_text(encoding="utf-8")
    have = read_blocks(text)
    if args.write:
        missing = sorted(set(blocks) - set(have))
        if missing:
            print(f"README.md has no markers for {missing}", file=sys.stderr)
            return 1
        path.write_text(replace_blocks(text, blocks), encoding="utf-8")
        print(f"rewrote {len(blocks)} blocks in {path}")
        return 0
    if args.check:
        stale = sorted(b for b in blocks if have.get(b) != blocks[b])
        unknown = sorted(set(have) - set(blocks) - set(skipped))
        for b in stale:
            print(f"out of date: {b}", file=sys.stderr)
        for b in unknown:
            print(f"no generator for: {b}", file=sys.stderr)
        return 1 if stale or unknown else 0
    for b, t in blocks.items():
        print(f"<!-- numbers:{b}:start -->\n{t}\n<!-- numbers:{b}:end -->\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
