"""Strategy v1 backtest, in-sample only (research/strategy/v1-spec.md).

    python -m jobs.backtest_v1 --config base            # the investable configuration
    python -m jobs.backtest_v1 --config base --reuse-ranks
    python -m jobs.backtest_v1 --config cost40          # 0.40% per side sensitivity
    python -m jobs.backtest_v1 --config haircut50       # delisting at half the last close
    python -m jobs.backtest_v1 --config grid            # the 36 construction variants
    python -m jobs.backtest_v1 --config diagnostics     # quintile, group and leave-one-out spreads

Writes to data/backtest/v1/:
    universe_funnel.csv   per D: names remaining after each universe rule
    ranks.parquet         per D per symbol: measures, percentiles, groups, composite
    holdings.csv          per rebalance D: symbol, rank, sector, weight, action
    trades.csv            every fill, cancellation and forced exit
    nav.csv               strategy, universe_ew and index NAVs on the strategy clock
    metrics.json          every metric the spec lists, plus sanity checks
    sanity.json           the self-checks run on every build

HOLDOUT LOCK. The run ends at the open of the Feb 2023 decision date. Any
--end later than that raises HoldoutLocked unless --run-final-test is passed,
and the engine re-checks the same guard independently. The unlock flag exists
for the single, reviewed holdout run and for nothing else.

Every run of a portfolio variant is appended to state/trials.jsonl (study
'strategy_v1'), whatever it shows. --no-record exists only for tests.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path

import numpy as np
import pandas as pd

from zen.portfolio import engine, metrics
from zen.universe import pit
from zen.validation import trials

log = logging.getLogger("backtest_v1")

OUT = pit.REPO / "data" / "backtest" / "v1"
STUDY = "strategy_v1"
# Benchmarks (Clarification 17, rewritten 2026-09-22): NSE's official Total
# Returns Index, `tri` column (gross), from data/external/nifty_tri.parquet;
# the price index supplies only the overnight move to the two opens on the clock.
# key: (TRI index_name, price index_name in the indices table)
INDICES = {"nifty500": ("NIFTY 500", "Nifty 500"),
           "midcap150": ("NIFTY MIDCAP 150", "Nifty Midcap 150"),
           "smallcap250": ("NIFTY SMALLCAP 250", "Nifty Smallcap 250")}


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="base",
                   choices=["base", "cost40", "haircut50", "grid", "diagnostics"])
    p.add_argument("--reuse-ranks", action="store_true",
                   help="load ranks.parquet instead of recomputing (same decision dates)")
    p.add_argument("--end", default=None, help="end date; defaults to the in-sample end")
    p.add_argument("--run-final-test", action="store_true",
                   help="permit returns after the in-sample end. Never pass this during "
                        "in-sample work.")
    p.add_argument("--no-record", action="store_true", help="do not append to trials.jsonl")
    p.add_argument("--out", default=str(OUT))
    return p.parse_args(argv)


# ---------------------------------------------------------------- pieces
def ranks_for(con, decisions, out: Path, reuse: bool, static=None):
    rp, fp = out / "ranks.parquet", out / "universe_funnel.csv"
    if reuse and rp.exists() and fp.exists():
        ranks = pd.read_parquet(rp)
        ranks["D"] = pd.to_datetime(ranks["D"])
        have = sorted(ranks["D"].unique())
        if [pd.Timestamp(d) for d in have] == [pd.Timestamp(d) for d in decisions]:
            log.info("reusing %s", rp)
            return ranks, pd.read_csv(fp)
        log.warning("ranks.parquet covers different dates; recomputing")
    static = static or pit.StaticLabels.load(con)
    t = time.time()
    ranks, funnel = engine.build_ranks(con, decisions, static)
    log.info("ranks for %d dates in %.0fs", len(decisions), time.time() - t)
    out.mkdir(parents=True, exist_ok=True)
    dq = ranks.attrs.pop("data_quality", None)
    if dq is not None:
        # Clarification 30 audit trail: every filing treated as not filed at a D
        # and every share count replaced, per decision date.
        dq.to_csv(out / "data_quality.csv", index=False)
    ranks.to_parquet(rp, index=False)
    funnel.to_csv(fp, index=False)
    return ranks, funnel


def index_navs(con, start, end, is_end, unlock):
    out = {}
    for key, (tri_name, price_name) in INDICES.items():
        try:
            out[key] = engine.tri_nav(con, tri_name, price_name, start, end, is_end, unlock)
        except ValueError as e:
            log.warning("%s unavailable: %s", tri_name, e)
    return out


def wide_nav(frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
    w = None
    for k, f in frames.items():
        g = f.rename(columns={"nav": k})
        w = g if w is None else w.merge(g, on=["date", "mark"], how="left")
    return w


def sanity(con, ranks, panel: engine.Panel, decisions) -> dict:
    """Self-checks: market caps, universe sizes, extreme returns, TCS dividends."""
    out: dict = {}
    d21 = [d for d in decisions if pd.Timestamp(d).year == 2021][0]
    r = ranks[ranks["D"] == pd.Timestamp(d21)].set_index("symbol")
    # Approximate market caps (Rs crore) on or about mid-Feb 2021, from exchange data.
    reality_cr = {"RELIANCE": 1_300_000, "TCS": 1_200_000, "INFY": 560_000}
    caps = {}
    for s, real in reality_cr.items():
        m = float(r.at[s, "mcap"]) / 1e7 if s in r.index else np.nan
        caps[s] = {"model_cr": round(m), "reference_cr": real,
                   "ratio": round(m / real, 3) if np.isfinite(m) else None,
                   "within_20pct": bool(np.isfinite(m) and abs(m / real - 1) <= 0.2)}
    out["mcap_check"] = {"D": str(pd.Timestamp(d21).date()), "caps": caps}
    out["universe_size"] = {str(pd.Timestamp(d).date()): int((ranks["D"] == d).sum())
                            for d in sorted(ranks["D"].unique())}
    ext = engine.daily_returns_check(panel, 0.6)
    out["extreme_daily_returns"] = ext.assign(date=ext["date"].dt.date.astype(str)
                                              ).to_dict("records")
    s = panel.col("TCS") if "TCS" in panel.symbols else None
    if s is not None:
        dv = panel.div_unit[:, s]
        nz = np.flatnonzero(dv)
        out["tcs_dividends"] = [{"date": str(panel.dates[t].date()), "cash_per_unit": float(dv[t])}
                                for t in nz]
    out["dividends_dropped_as_implausible"] = (
        panel.dropped_dividends[["symbol", "ex_date", "dps"]].assign(
            ex_date=lambda d: d["ex_date"].dt.date.astype(str)).to_dict("records")
        if not panel.dropped_dividends.empty else [])
    return out


def variant_result(res: engine.Result, benches: dict) -> dict:
    return metrics.full_report(res.nav, benches, res.trades, res.episodes)


def record(args, variant: dict, rep: dict) -> None:
    if args.no_record:
        return
    keep = {k: rep.get(k) for k in ("cagr", "volatility", "sharpe_rf0", "max_drawdown",
                                    "turnover_annual", "doubled", "start", "end")}
    ew = rep.get("vs", {}).get("universe_ew", {})
    keep["ir_vs_universe_ew"] = ew.get("information_ratio")
    keep["cagr_vs_universe_ew"] = ew.get("cagr_diff")
    n = trials.record(STUDY, variant, keep)
    log.info("trial recorded (%s: %d in study, %d lifetime)", STUDY, n, trials.lifetime())


# ---------------------------------------------------------------- main
def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    con = pit.connect()
    cal = pit.sessions(con)
    is_end = engine.in_sample_end(cal)
    end = pd.Timestamp(args.end) if args.end else is_end
    # The default decision dates stop at the last in-sample rebalance. For the
    # final test the strategy has to keep rebalancing through the held-back
    # period, otherwise the run measures a portfolio frozen in Nov 2022 rather
    # than the strategy. The first run of the final test did exactly that.
    if args.run_final_test and end > is_end:
        decisions = pit.decision_dates(cal, last=(end.year, end.month))
    else:
        decisions = pit.decision_dates(cal)
    # Guard 1 of 2: the CLI. The engine checks again on every load and simulation.
    engine.guard(end, is_end, args.run_final_test)
    decisions = [d for d in decisions if d < end]
    log.info("decision dates %s .. %s (%d); returns to the open of %s",
             decisions[0].date(), decisions[-1].date(), len(decisions), end.date())

    static = pit.StaticLabels.load(con)
    ranks, funnel = ranks_for(con, decisions, out, args.reuse_ranks, static)
    print("\nUniverse per decision date:")
    print(funnel.to_string(index=False))

    t = time.time()
    panel = engine.build_panel(con, ranks["symbol"].unique(), decisions[0], end, is_end,
                               args.run_final_test, ids=static.ids)
    log.info("panel %d sessions x %d symbols in %.1fs", len(panel.dates), len(panel.symbols),
             time.time() - t)
    idx = index_navs(con, decisions[0], end, is_end, args.run_final_test)

    if args.config in ("base", "cost40", "haircut50"):
        cfg = {"base": engine.BASE,
               "cost40": engine.Config(cost=0.004),
               "haircut50": engine.Config(delist_haircut=0.5)}[args.config]
        t = time.time()
        res = engine.run_strategy(panel, ranks, decisions, cfg, is_end, args.run_final_test)
        ew = engine.run_universe_ew(panel, ranks, decisions, cfg, is_end, args.run_final_test)
        log.info("strategy + universe EW simulated in %.2fs", time.time() - t)
        benches = {"universe_ew": ew.nav, **idx}
        rep = variant_result(res, benches)
        rep["config"] = engine.config_dict(cfg)
        rep["label"] = cfg.label()
        rep["decision_dates"] = [str(d.date()) for d in decisions]
        rep["in_sample_end"] = str(is_end.date())
        rep["universe_median_size"] = float(funnel["r7_mcap_computable"].median())
        rep["avg_holdings"] = float(res.holdings.groupby("D").apply(
            lambda g: (g["action"] != "sell").sum()).mean())
        rep["sector_source_share"] = (ranks["sector_source"].value_counts(normalize=True)
                                      .round(3).to_dict())
        rep["benchmark_notes"] = {
            "nifty": "NSE's official Total Returns Index (gross, tri column of "
                     "data/external/nifty_tri.parquet) for Nifty 500, Midcap 150 and "
                     "Smallcap 250; the two opens on the clock are the previous TRI close "
                     "times the price index's overnight move (Clarification 17)",
            "universe_ew": "includes dividends and the same costs as the strategy",
        }
        san = sanity(con, ranks, panel, decisions)
        rep["sanity"] = {k: san[k] for k in ("mcap_check", "universe_size")}
        rep["sanity"]["n_extreme_daily_returns"] = len(san["extreme_daily_returns"])
        rep["sanity"]["tcs_dividend_events"] = len(san.get("tcs_dividends", []))

        suffix = "" if args.config == "base" else f"_{args.config}"
        res.holdings.to_csv(out / f"holdings{suffix}.csv", index=False)
        res.trades.to_csv(out / f"trades{suffix}.csv", index=False)
        res.episodes.drop(columns=["entry_t", "exit_t"], errors="ignore").to_csv(
            out / f"episodes{suffix}.csv", index=False)
        wide_nav({"strategy": res.nav, "universe_ew": ew.nav, **idx}).to_csv(
            out / f"nav{suffix}.csv", index=False)
        (out / f"metrics{suffix}.json").write_text(json.dumps(rep, indent=2, default=str))
        (out / "sanity.json").write_text(json.dumps(san, indent=2, default=str))
        record(args, {"config": args.config, **engine.config_dict(cfg)}, rep)
        _print(rep, san)
        return 0

    if args.config == "grid":
        rows = []
        ew_cache = {}
        for cfg in engine.configs_grid():
            t = time.time()
            res = engine.run_strategy(panel, ranks, decisions, cfg, is_end,
                                      args.run_final_test, log_trades=True)
            key = (cfg.rebalance, cfg.cost)
            if key not in ew_cache:
                ew_cache[key] = engine.run_universe_ew(panel, ranks, decisions, cfg, is_end,
                                                       args.run_final_test).nav
            rep = variant_result(res, {"universe_ew": ew_cache[key], **idx})
            record(args, {"config": "grid", **engine.config_dict(cfg)}, rep)
            rows.append({"label": cfg.label(), **engine.config_dict(cfg),
                         "cagr": rep["cagr"], "sharpe_rf0": rep["sharpe_rf0"],
                         "max_drawdown": rep["max_drawdown"],
                         "ir_vs_universe_ew": rep["vs"]["universe_ew"]["information_ratio"],
                         "seconds": round(time.time() - t, 2)})
            log.info("%s done in %.2fs", cfg.label(), time.time() - t)
        pd.DataFrame(rows).to_csv(out / "grid.csv", index=False)
        return 0

    if args.config == "diagnostics":
        from zen.signals.composite import GROUPS
        cols = (["composite"] + [f"grp_{g}" for g in GROUPS] + [f"loo_{g}" for g in GROUPS])
        navs, rows = {}, []
        for col in cols:
            qs = {q: engine.run_quantile(panel, ranks, decisions, col, q, is_end,
                                         run_final_test=args.run_final_test).nav
                  for q in range(1, 6)}
            for q, nv in qs.items():
                navs[f"{col}_q{q}"] = nv
            spread = metrics.active(qs[1], qs[5])
            rep = {"score": col, "q1_cagr": metrics.cagr(qs[1]), "q5_cagr": metrics.cagr(qs[5]),
                   "top_minus_bottom_cagr": spread["cagr_diff"],
                   "ir_q1_vs_q5": spread["information_ratio"]}
            rows.append(rep)
            record(args, {"config": "diagnostic_quintile", "score": col}, dict(rep))
        wide_nav(navs).to_csv(out / "nav_quintiles.csv", index=False)
        pd.DataFrame(rows).to_csv(out / "diagnostics.csv", index=False)
        return 0
    return 1


def _print(rep: dict, san: dict) -> None:
    pct = lambda x: f"{100 * x:+.1f}%" if x is not None and np.isfinite(x) else "n/a"
    print(f"\n=== {rep['label']}  ({rep['start']} open -> {rep['end']} open) ===")
    print(f"CAGR {pct(rep['cagr'])}  vol {pct(rep['volatility'])}  "
          f"Sharpe(rf=0) {rep['sharpe_rf0']:.2f}  maxDD {pct(rep['max_drawdown'])}")
    print(f"turnover {rep['turnover_annual']:.2f}x/yr  avg hold {rep['avg_holding_days']:.0f}d  "
          f"doubled {rep['doubled']}  episodes {rep['episodes']}  avg holdings {rep['avg_holdings']:.1f}")
    for k, v in rep["vs"].items():
        print(f"  vs {k:12s} bench CAGR {pct(v['cagr'])}  diff {pct(v['cagr_diff'])}  "
              f"IR {v['information_ratio']:.2f}  TE {pct(v['tracking_error'])}")
    b = rep.get("bootstrap_vs_universe_ew")
    if b:
        print(f"  bootstrap 90% CI of annualised excess vs universe EW: "
              f"[{pct(b['lo'])}, {pct(b['hi'])}] (point {pct(b['point'])})")
    print("  calendar years:", {y: pct(v) for y, v in rep["calendar_years"].items()})
    print("\nSanity:")
    print("  mcap:", json.dumps(san["mcap_check"]))
    print(f"  extreme daily returns (>|60%|): {len(san['extreme_daily_returns'])}")
    print(f"  TCS dividend events credited: {len(san.get('tcs_dividends', []))}")


if __name__ == "__main__":
    raise SystemExit(main())
