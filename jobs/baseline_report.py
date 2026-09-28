"""The measured baseline of a run, for v2-spec A2's comparison.

Assembles the figures the measurement jobs wrote into a run folder, each with
its definition and the file and key it was read from. It computes nothing new
except the bars a challenger has to clear, so every number in the report is
exactly the number a job wrote.

    python -m jobs.libcheck_v1  --run RUN --label L
    python -m jobs.verify_v1    --run RUN --label L
    python -m jobs.stats_v1     --run RUN --label L
    python -m jobs.attribution  --run RUN --label L
    python -m jobs.after_tax    --in RUN [--run-final-test]
    python -m jobs.baseline_report --run RUN --label L

Writes <run>/baseline_report.json and <run>/baseline_report.md.

The same report is written for a v2 run (metrics.json engine 'v2'): its figures
are the same quantities from the same files, the bars a later challenger would
face are left out (v2's own verdict against v1 is jobs/v2_verdict.py's), and
the text says so. `--allow-missing after_tax` writes the report while the
after-tax job has not been run on the folder, with the after-tax section marked
as not measured; every other source is always required.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SOURCES = {"libcheck": "libcheck.json", "verify": "verify.json", "stats": "stats.json",
           "attribution": "attribution.json", "after_tax": "after_tax/summary.json",
           "metrics": "metrics.json"}
DD_MARGIN = 0.05          # A2 rule 1: at least 5 percentage points smaller
SHORT_LIVE_YEARS = 3      # wording only: a live part shorter than this is called too short
                          # to judge (Clarification to A4); no figure or rule depends on it


TEXT_SUFFIXES = {".json", ".csv", ".md", ".txt", ".py"}
REPO = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
    """SHA-256 of a file; text files with line endings normalised to \\n, so the
    hash is the same on a Windows checkout (CRLF) and a fresh clone (LF)."""
    data = Path(path).read_bytes()
    if Path(path).suffix.lower() in TEXT_SUFFIXES:
        data = data.replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def repo_path(p: Path) -> str:
    """A path as the repo sees it: relative, forward slashes, no local folders."""
    p = Path(p).resolve()
    try:
        return p.relative_to(REPO).as_posix()
    except ValueError:
        return p.as_posix()


def pick(doc: dict, key: str):
    """doc['a']['b']['c'] for key 'a.b.c'; KeyError names the whole key."""
    cur = doc
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise KeyError(f"{key} (missing {part!r})")
        cur = cur[part]
    return cur


def figure(docs: dict, src: str, key: str, definition: str, **extra) -> dict:
    return {"value": pick(docs[src], key), "definition": definition,
            "source": f"{SOURCES[src]}: {key}", **extra}


OPTIONAL_SOURCES = ("after_tax",)     # may be allowed missing with --allow-missing


def load(run: Path, allow_missing: tuple = ()) -> tuple[dict, dict]:
    """Every source file of the report; a missing one stops the job unless it is named
    in `allow_missing` (only OPTIONAL_SOURCES may be), when it is recorded as missing."""
    bad = set(allow_missing) - set(OPTIONAL_SOURCES)
    if bad:
        raise ValueError(f"only {OPTIONAL_SOURCES} may be missing, not {sorted(bad)}")
    docs, files = {}, {}
    for k, rel in SOURCES.items():
        p = Path(run) / rel
        if not p.exists():
            if k in allow_missing:
                docs[k] = None
                files[k] = {"path": repo_path(p), "missing": True}
                continue
            raise FileNotFoundError(f"{p}: run the measurement job first (see module docstring)")
        docs[k] = json.loads(p.read_text(encoding="utf-8"))
        files[k] = {"path": repo_path(p), "sha256": sha256(p)}
    return docs, files


def is_v2_run(docs: dict) -> bool:
    return (docs.get("metrics") or {}).get("engine") == "v2"


def a2_quantities(docs: dict) -> dict:
    """v2-spec A2's five acceptance quantities, each with the bar v2 must clear."""
    lib_s = "full_period.strategy.exact"
    mdd = figure(docs, "libcheck", f"{lib_s}.max_drawdown",
                 "maximum drawdown over the full period: the worst fall of NAV from its "
                 "running peak, from the returns between consecutive marks of nav.csv "
                 "(open of the first decision date, every close, open of the end date), "
                 "quantstats qs.stats.max_drawdown")
    mdd["check_metrics_json"] = pick(docs["metrics"], "max_drawdown")
    mdd["bar_for_v2"] = {"rule": "at least 5 percentage points smaller than v1's",
                         "v2_max_drawdown_must_be_at_least": mdd["value"] + DD_MARGIN}
    sortino = figure(docs, "libcheck", f"{lib_s}.sortino",
                     "quantstats qs.stats.sortino(returns, periods=252): mean return over "
                     "the root mean square of the negative returns taken over all periods, "
                     "times sqrt(252), zero risk-free rate")
    sortino["bar_for_v2"] = {"rule": "higher than v1's", "v2_must_exceed": sortino["value"]}
    calmar = figure(docs, "libcheck", f"{lib_s}.calmar",
                    "quantstats qs.stats.calmar(returns): quantstats' CAGR (compounded "
                    "return over len(returns)/252 years) over the absolute maximum drawdown")
    calmar["bar_for_v2"] = {"rule": "higher than v1's", "v2_must_exceed": calmar["value"]}
    reg = "regression_full_period.strategy"
    alpha = figure(docs, "libcheck", f"{reg}.exact.alpha_annual",
                   "IIMA four-factor alpha: monthly strategy return minus IIMA's RF regressed "
                   "on IIMA's MF, SMB, HML and WML (survivorship-bias adjusted, Dec 2025 "
                   "release), statsmodels OLS; monthly intercept annualised as "
                   "(1 + a)^12 - 1")
    alpha["months"] = {"n": pick(docs["libcheck"], f"{reg}.months"),
                       "first": pick(docs["libcheck"], f"{reg}.first"),
                       "last": pick(docs["libcheck"], f"{reg}.last")}
    alpha["bar_for_v2"] = {"rule": "not lower than v1's, over the same months",
                           "v2_must_be_at_least": alpha["value"]}
    alpha_t = figure(docs, "libcheck", f"{reg}.exact.alpha_t",
                     "t-statistic of the intercept, statsmodels HAC (Newey-West, Bartlett "
                     "kernel, 3 lags, statsmodels' default: no small-sample correction)")
    alpha_t["hand_rolled_with_small_sample_correction"] = {
        "value": pick(docs["verify"], "attribution.strategy.alpha_t"),
        "source": "verify.json: attribution.strategy.alpha_t",
        "note": "the same regression with the n/(n-k) factor; not the A2 figure"}
    alpha_t["bar_for_v2"] = {"rule": "not lower than v1's", "v2_must_be_at_least": alpha_t["value"]}
    turn = figure(docs, "verify", "turnover_annual_incl_initial",
                  "annual one-way turnover including the initial build: the value of every "
                  "buy and sell / 2 / mean NAV over all marks / calendar years")
    turn["check_metrics_json"] = pick(docs["metrics"], "turnover_annual_incl_initial")
    turn["bar_for_v2"] = {"rule": "not higher than v1's", "v2_must_be_at_most": turn["value"]}
    cap = figure(docs, "verify", "capacity_all_trades.p95",
                 pick(docs["verify"], "capacity_all_trades.definition"),
                 trades=pick(docs["verify"], "capacity_all_trades.trades"),
                 check_against_ranks=pick(docs["verify"], "capacity_all_trades.check_against_ranks"))
    cap["decision_date_fills_only"] = {
        "value_pct": pick(docs["verify"], "capacity.p95_pct"),
        "trades_matched": pick(docs["verify"], "capacity.matched"),
        "source": "verify.json: capacity.p95_pct",
        "note": "the earlier definition: only fills dated on a decision date on which the "
                "stock was in the universe; it would not count v2's later tranches at all"}
    cap["bar_for_v2"] = {"rule": "not higher than v1's", "v2_must_be_at_most": cap["value"]}
    return {"max_drawdown": mdd, "sortino": sortino, "calmar": calmar,
            "iima_alpha_annual": alpha, "iima_alpha_t": alpha_t,
            "turnover_annual_incl_initial": turn, "capacity_p95_share_of_turnover": cap}


def benchmark_table(docs: dict) -> dict:
    out = {}
    for col, b in pick(docs["attribution"], "benchmarks").items():
        row = {"name": b["name"],
               "whole_period": {k: b["whole_period"][k] for k in
                                ("from", "to", "years", "strategy_cagr", "benchmark_cagr",
                                 "excess_cagr", "information_ratio")},
               "source": f"attribution.json: benchmarks.{col}"}
        if "live_part" in b:
            lp = b["live_part"]
            row["live_part"] = {k: lp[k] for k in
                                ("from", "to", "years", "strategy_cagr", "benchmark_cagr",
                                 "excess_cagr", "information_ratio", "live_start", "basis",
                                 "source", "note") if k in lp}
        out[col] = row
    return out


AFTER_TAX_DEFINITION = ("jobs/after_tax.py (v2-spec A6): itemised Indian costs and slippage, "
                        "capital gains tax by lot with advance tax; statistics from the value "
                        "if everything were sold at each mark after exit costs and tax; "
                        "benchmark: a growth-option Nifty 500 index fund held throughout")


def after_tax_section(tax: dict | None) -> dict:
    """The after-tax figures of after_tax/summary.json; marked as not measured when
    the file was allowed to be missing."""
    if tax is None:
        return {"definition": AFTER_TAX_DEFINITION, "source": "after_tax/summary.json",
                "not_measured": True,
                "reason": "after_tax/summary.json is not in the run folder: jobs/after_tax.py "
                          "has not been run on it (the report was written with "
                          "--allow-missing after_tax)"}
    return {
        "definition": AFTER_TAX_DEFINITION,
        "source": "after_tax/summary.json",
        "strategy_cagr": tax["after_tax_stats"]["strategy"]["cagr"],
        "strategy_max_drawdown": tax["after_tax_stats"]["strategy"]["max_drawdown"],
        "strategy_end_liquidated": tax["strategy"]["after_tax_end_liquidated"],
        "benchmark_growth_cagr": tax["after_tax_stats"]["benchmark_growth"]["cagr"],
        "benchmark_growth_end_liquidated": tax["benchmark"]["growth"]["after_tax_end_liquidated"],
        "excess_cagr_vs_growth_fund": tax["after_tax_stats"]["strategy_vs_benchmark_growth"]["cagr_diff"],
        "information_ratio_vs_growth_fund":
            tax["after_tax_stats"]["strategy_vs_benchmark_growth"]["information_ratio"],
        "tax_paid_during": tax["strategy"]["tax_paid_during"],
        "itemised_costs_total": tax["strategy"]["itemised_costs_total"],
        "flat_costs_total": tax["strategy"]["flat_costs_total"],
        "capital": tax["strategy"]["capital"],
        "replay_check_worst_rel": tax["replay_check"]["worst_rel_error"]}


def build(run: Path, label: str, allow_missing: tuple = ()) -> dict:
    run = Path(run)
    docs, files = load(run, allow_missing)
    lib, ver, att, tax = docs["libcheck"], docs["verify"], docs["attribution"], docs["after_tax"]
    q = att["quality"]
    rep = {
        "run": str(run), "label": label,
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "period": {"start": ver["period"]["start"], "end": ver["period"]["end"],
                   "clock": "open of the first decision date, every close, open of the end "
                            "date (v1 Clarification 16)"},
        "sources": files,
        "libraries": {p: version(p) for p in ("quantstats", "statsmodels", "arch", "pandas")},
        "a2_acceptance_quantities": a2_quantities(docs),
        "headline": {
            "strategy_cagr": figure(docs, "libcheck", "full_period.strategy.exact.cagr_calendar",
                                    "calendar-day CAGR over the full clock"),
            "universe_ew_cagr": figure(docs, "libcheck",
                                       "full_period.universe_ew.exact.cagr_calendar",
                                       "calendar-day CAGR over the full clock")},
        "final_test": figure(docs, "libcheck", "final_test",
                             "held-back sub-period, open of the in-sample end to open of the "
                             "end date, every column from in_sample_end_open.csv (v1 "
                             "disclosure 37); percentages"),
        "factor_attribution": {
            "iima_four_factor": figure(docs, "attribution", "iima_four_factor",
                                       "as the A2 alpha (same code, same months)"),
            "with_quality_factor": {
                v: {"label": q[v]["label"], "months": q[v]["months"], "first": q[v]["first"],
                    "last": q[v]["last"], "identical_months": q[v]["identical_months"],
                    "without": {k: q[v]["without_quality"]["exact"][k]
                                for k in ("alpha_annual", "alpha_t")},
                    "with": {**{k: q[v]["with_quality"]["exact"][k]
                                for k in ("alpha_annual", "alpha_t")},
                             "quality_loading": q[v]["with_quality"]["loadings"]["QUAL"],
                             "quality_loading_t": q[v]["with_quality"]["loading_t"]["QUAL"]},
                    "source": f"attribution.json: quality.{v}"}
                for v in q},
            "definition": "four-factor regression without and with the A5 quality factor "
                          "(data/study/quality_factor.parquet, complete months), on identical "
                          "months checked in code; alpha annualised, statsmodels HAC 3 lags"},
        "benchmarks": {
            "definition": "excess CAGR = strategy CAGR minus benchmark CAGR (calendar days); "
                          "information ratio = annualised mean daily return difference over "
                          "its annualised standard deviation (252); zen/portfolio/metrics.py, "
                          "equal to metrics.json on the whole period. Live part: from the close "
                          "of the index's first live date to the open of the end date",
            "rows": benchmark_table(docs)},
        "after_tax": after_tax_section(tax),
        "monkey_test": figure(docs, "verify", "monkey_test",
                              "random portfolios through the same engine and rules, ranking "
                              "order randomised (persistent and fresh), 500 draws each"),
        "deflated_sharpe": figure(docs, "verify", "deflated_sharpe",
                                  "Bailey and Lopez de Prado deflated Sharpe, three variance "
                                  "choices"),
    }
    if is_v2_run(docs):
        # A v2 run is measured, not a baseline: the bars it would set for a later
        # challenger are left out. Its verdict against v1 is jobs/v2_verdict.py's.
        rep["engine"] = "v2"
        rep["verdict"] = "data/backtest/v2_verdict.json (jobs/v2_verdict.py)"
        for fig in rep["a2_acceptance_quantities"].values():
            fig.pop("bar_for_v2", None)
    return rep


# ---------------------------------------------------------------- plain English
def pct(x: float, nd: int = 1, sign: bool = False) -> str:
    return f"{x * 100:+.{nd}f}%" if sign else f"{x * 100:.{nd}f}%"


def a2_table_v1(mdd, so, ca, al, at, tu, cp) -> list[str]:
    """The baseline's A2 section: v1's five quantities and the bar each sets for v2."""
    L = ["## What v2 has to beat (A2)", "", "v2 replaces v1 only if all five hold.", ""]
    L.append("| Measure | v1 | v2 must be |")
    L.append("|---|---|---|")
    L.append(f"| Maximum drawdown | {pct(mdd['value'], 2)} | no deeper than "
             f"{pct(mdd['bar_for_v2']['v2_max_drawdown_must_be_at_least'], 2)} |")
    L.append(f"| Sortino (quantstats) | {so['value']:.3f} | above {so['value']:.3f} |")
    L.append(f"| Calmar (quantstats) | {ca['value']:.3f} | above {ca['value']:.3f} |")
    L.append(f"| IIMA four-factor alpha, a year | {pct(al['value'], 2)} | at least "
             f"{pct(al['value'], 2)} |")
    L.append(f"| t-statistic of that alpha | {at['value']:.2f} | at least {at['value']:.2f} |")
    L.append(f"| Turnover a year, incl. initial build | {tu['value']:.3f}x | at most "
             f"{tu['value']:.3f}x |")
    L.append(f"| Capacity: 95th percentile trade as a share of 60-session median turnover | "
             f"{pct(cp['value'], 3)} | at most {pct(cp['value'], 3)} |")
    L.append("")
    L.append(f"The alpha is over {al['months']['n']} months ({al['months']['first']} to "
             f"{al['months']['last']}), the last month IIMA has published. v2 must be measured "
             f"over the same months. The t-statistic is statsmodels' Newey-West figure without "
             f"a small-sample correction; with it, as the project's hand-written regression "
             f"does, it is {at['hand_rolled_with_small_sample_correction']['value']:.2f}. "
             f"Capacity counts all {cp['trades']} buys and sells against the stock's median "
             f"turnover in the 60 sessions before each trade; counting only trades filled on a "
             f"decision date, as earlier reports did, gives "
             f"{cp['decision_date_fills_only']['value_pct']:.3f}%, but that version would not "
             f"see v2's second and third tranches.")
    L.append("")
    return L


def a2_table_v2(rep, mdd, so, ca, al, at, tu, cp) -> list[str]:
    """A v2 run's A2 section: its five quantities; the verdict is elsewhere."""
    L = ["## The five A2 quantities", "",
         f"The same quantities v1's baseline reports, measured on this run. Whether they clear "
         f"v1's bars is decided in `{rep['verdict'].split(' ')[0]}`.", "",
         f"| Measure | {rep['label']} |", "|---|---|",
         f"| Maximum drawdown | {pct(mdd['value'], 2)} |",
         f"| Sortino (quantstats) | {so['value']:.3f} |",
         f"| Calmar (quantstats) | {ca['value']:.3f} |",
         f"| IIMA four-factor alpha, a year | {pct(al['value'], 2)} |",
         f"| t-statistic of that alpha | {at['value']:.2f} |",
         f"| Turnover a year, incl. initial build | {tu['value']:.3f}x |",
         f"| Capacity: 95th percentile trade as a share of 60-session median turnover | "
         f"{pct(cp['value'], 3)} |", "",
         f"The alpha is over {al['months']['n']} months ({al['months']['first']} to "
         f"{al['months']['last']}), the last month IIMA has published. The t-statistic is "
         f"statsmodels' Newey-West figure without a small-sample correction; with it, as the "
         f"project's hand-written regression does, it is "
         f"{at['hand_rolled_with_small_sample_correction']['value']:.2f}. Capacity counts all "
         f"{cp['trades']} buys and sells, every tranche included, against the stock's median "
         f"turnover in the 60 sessions before each trade; counting only trades filled on a "
         f"decision date gives {cp['decision_date_fills_only']['value_pct']:.3f}%, which "
         f"leaves out the second and third tranches.", ""]
    return L


def markdown(rep: dict) -> str:
    a = rep["a2_acceptance_quantities"]
    mdd, so, ca = a["max_drawdown"], a["sortino"], a["calmar"]
    al, at, tu, cp = (a["iima_alpha_annual"], a["iima_alpha_t"],
                      a["turnover_annual_incl_initial"], a["capacity_p95_share_of_turnover"])
    ft = rep["final_test"]["value"]
    fa = rep["factor_attribution"]["with_quality_factor"]
    rows = rep["benchmarks"]["rows"]
    tax = rep["after_tax"]
    mk = rep["monkey_test"]["value"]
    v2 = rep.get("engine") == "v2"
    L = []
    L.append(f"# Measurements of a v2 run: {rep['label']}" if v2 else
             f"# Baseline for the v2 comparison: {rep['label']}")
    L.append("")
    L.append(f"Run `{Path(rep['run']).as_posix()}`, {rep['period']['start']} to {rep['period']['end']}, measured "
             "open to open. Every number below is read from the measurement jobs' own output "
             "files, listed with their definitions in `baseline_report.json`.")
    L.append("")
    L.append(f"The strategy grew {pct(rep['headline']['strategy_cagr']['value'])} a year, the "
             f"same universe equally weighted {pct(rep['headline']['universe_ew_cagr']['value'])}.")
    L.append("")
    if v2:
        L.extend(a2_table_v2(rep, mdd, so, ca, al, at, tu, cp))
    else:
        L.extend(a2_table_v1(mdd, so, ca, al, at, tu, cp))
    L.append("## Is the alpha a quality premium?")
    L.append("")
    m, r = fa["margin"], fa["roce"]
    L.append(f"Adding the A5 quality factor (margin version, {m['months']} months) moves alpha "
             f"from {pct(m['without']['alpha_annual'], 2)} (t {m['without']['alpha_t']:.2f}) to "
             f"{pct(m['with']['alpha_annual'], 2)} (t {m['with']['alpha_t']:.2f}); the strategy "
             f"loads {m['with']['quality_loading']:.2f} on quality. Over the ROCE version's "
             f"{r['months']} months ({r['first']} to {r['last']}) alpha is "
             f"{pct(r['without']['alpha_annual'], 2)} (t {r['without']['alpha_t']:.2f}) without "
             f"the factor and {pct(r['with']['alpha_annual'], 2)} (t {r['with']['alpha_t']:.2f}) "
             f"with it, a loading of {r['with']['quality_loading']:.2f}"
             + (f": the factor accounts for {1 - r['with']['alpha_annual'] / r['without']['alpha_annual']:.0%} "
                f"of the alpha in that window" if r['without']['alpha_annual'] > 0 else "")
             + f". {r['months']} months is about {r['months'] / 12:.1f} years.")
    L.append("")
    L.append("## Against the indices")
    L.append("")
    L.append("Excess is the strategy's annual growth minus the index's. The live part of a "
             "factor index starts at the close of its first live date.")
    L.append("")
    L.append("| Benchmark | Whole period: excess, IR | Live part | Live: excess, IR |")
    L.append("|---|---|---|---|")
    for col, b in rows.items():
        if col.endswith("_no_overnight"):
            continue
        w = b["whole_period"]
        if "live_part" in b and "note" in b["live_part"]:
            live_desc, live_fig = b["live_part"]["note"], ""
        elif "live_part" in b:
            lp = b["live_part"]
            same = lp["from"] == w["from"]
            live_desc = ("whole period" if same else f"from {lp['from'].split()[0]} "
                         f"({lp['years']:.1f} years)")
            live_fig = ("same" if same else f"{pct(lp['excess_cagr'], 1, True)}, "
                        f"{lp['information_ratio']:.2f}")
        else:
            live_desc, live_fig = "not a factor index", ""
        L.append(f"| {b['name']} | {pct(w['excess_cagr'], 1, True)}, "
                 f"{w['information_ratio']:.2f} | {live_desc} | {live_fig} |")
    L.append("")
    lives = [(b["name"], b["live_part"], b["whole_period"]["from"]) for c, b in rows.items()
             if "live_part" in b and "note" not in b["live_part"]
             and not c.endswith("_no_overnight")]
    later = [f"{n} from {lp['live_start']}" for n, lp, whole_from in lives
             if lp["from"] != whole_from]
    short = [f"{n} ({lp['years']:.1f} years)" for n, lp, _ in lives
             if lp["years"] < SHORT_LIVE_YEARS]
    fallback = all("fallback" in lp["basis"] for _, lp, _ in lives)
    L.append(("NSE's launch dates could not be read from an NSE document (see "
              "`launch_date_sources_tried` in attribution.json), so, as fixed in advance, each "
              "live part starts at the first date NSE printed an open for the index. "
              if fallback else "Live parts start at NSE's published launch dates. ")
             + ("Later live starts: " + ", ".join(later) + "; the others are live over the "
                "whole period. " if later else "")
             + (f"Too short to judge on its own (under {SHORT_LIVE_YEARS} years live): "
                + ", ".join(short) + ". " if short else "")
             + "With no overnight move where NSE printed no open, the whole-period excess "
             "changes by at most "
             f"{max(abs(rows[c]['whole_period']['excess_cagr'] - rows[c.replace('_no_overnight', '')]['whole_period']['excess_cagr']) for c in rows if c.endswith('_no_overnight')) * 100:.2f} "
             "percentage points.")
    L.append("")
    if v2:
        L.append(f"## After the in-sample end, {ft['clock']}")
        L.append("")
        L.append("Not a held-back test for v2: its rules were written after v1's full-period "
                 "results were known, so it reuses data v1 has already seen (v2-spec, 'How it "
                 "gets tested, and the honest problem'). The split is reported only because v1's "
                 "report has it.")
    else:
        L.append(f"## Held-back part, {ft['clock']}")
    L.append("")
    idx = ft.get("index_tri_cagr_pct", {})
    L.append(f"Strategy {ft['strategy_cagr_pct']:.2f}% a year, equal weight "
             f"{ft['universe_ew_cagr_pct']:.2f}% (margin {ft['margin_interval']['point_pct']:.2f} "
             f"points, 90% interval {ft['margin_interval']['lo90_pct']:.2f} to "
             f"{ft['margin_interval']['hi90_pct']:.2f}). Indices, open to open: "
             + ", ".join(f"{rows[c]['name'] if c in rows else c} {v:.2f}%"
                         for c, v in idx.items() if not c.endswith("_no_overnight")) + ".")
    L.append("")
    L.append("## After Indian costs and tax")
    L.append("")
    if tax.get("not_measured"):
        L.append(f"Not measured: {tax['reason']}.")
    else:
        L.append(f"From Rs {tax['capital']:,.0f}, the strategy would have been worth "
                 f"Rs {tax['strategy_end_liquidated']:,.0f} if sold at the end after costs and "
                 f"tax, {pct(tax['strategy_cagr'])} a year; a Nifty 500 index fund Rs "
                 f"{tax['benchmark_growth_end_liquidated']:,.0f}, "
                 f"{pct(tax['benchmark_growth_cagr'])} a year. The strategy paid "
                 f"Rs {tax['tax_paid_during']:,.0f} in tax along the way.")
    L.append("")
    if "persistent" in mk:
        p, f = mk["persistent"], mk["fresh"]
        L.append("## Luck")
        L.append("")
        L.append(f"Of {p['draws']} random portfolios run through the same rules with a random "
                 f"but persistent ranking, {p['beaten_by_n_random']} did at least as well "
                 f"(median {p['random_median_pct']:.1f}% a year); with a fresh random ranking "
                 f"each quarter, {f['beaten_by_n_random']} of {f['draws']} "
                 f"(median {f['random_median_pct']:.1f}%).")
        L.append("")
    ds = rep["deflated_sharpe"]["value"]
    probs = [ds[k] for k in ("prob_null_sampling_variance", "prob_cross_trial_variance_36_grid",
                             "prob_grid_trials_only")]
    L.append(f"Deflated Sharpe, counting {ds['n_trials']} lifetime trials: probability of skill "
             f"between {min(probs):.3f} and {max(probs):.3f} depending on the variance used.")
    L.append("")
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default="data/backtest/v1_final", help="backtest output folder")
    ap.add_argument("--label", default=None, help="name for this run (default: folder name)")
    ap.add_argument("--name", default="baseline_report", help="output file stem")
    ap.add_argument("--allow-missing", action="append", default=[], choices=OPTIONAL_SOURCES,
                    help="write the report without this source, marked as not measured")
    args = ap.parse_args(argv)
    run = Path(args.run)
    rep = build(run, args.label or run.name, tuple(args.allow_missing))
    (run / f"{args.name}.json").write_text(json.dumps(rep, indent=2), encoding="utf-8")
    (run / f"{args.name}.md").write_text(markdown(rep) + "\n", encoding="utf-8")
    print(f"written to {run / args.name}.json and .md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
