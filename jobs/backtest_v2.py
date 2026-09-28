"""Strategy v2 backtest (research/strategy/v2-spec.md), variant (a) or (b) of A1.

    python -m jobs.backtest_v2 --variant a --no-record          # in-sample -> data/backtest/v2a_is
    python -m jobs.backtest_v2 --variant b --no-record --ranks-from data/backtest/v2a_is
    python -m jobs.backtest_v2 --variant a --run-final-test --end 2026-09-18   # the one full run

Variants (amendment A1): (a) staggered entry, three tranches of V/3 at D and the
21st and 42nd sessions after it; (b) the same with A1's entry rule on each
tranche. Everything else is identical, including the ranks (variant-free, so
--ranks-from may reuse another run folder's, after checking the decision
dates and the code and data fingerprint).

Writes to --out (default data/backtest/v2<variant>_is in-sample, and
data/backtest/v2<variant> with --run-final-test):

  common formats, identical in the independent checker (jobs/crosscheck_v2.py):
    nav.csv            date, mark, strategy (Rs, from 500000), universe_ew and every
                       index column (1.0 at the first open); v1's clock (Clarification 16)
    decisions.csv      per D: the three trend conditions, cash fraction, universe and
                       hard-filter counts, names ranked on ROCE, names held, unlabelled
    holdings.csv       per D: kept, new and sold names with rank, NSE sector, sigma,
                       target weight of NAV at D's open, and why a name was sold
    fills.csv          every fill and cancellation: adjusted units and price (the
                       engine's basis), value, cost, kind, scheduled date, D
    a1.json            A1's price test per new position and V-weighted
    universe.csv       D, symbol: the v2 universe (rules 1-7 plus the hard filters)
    ranks.csv          D, symbol, every measure, percentile, group score, composite, rank
  also:
    ranks.parquet      the full ranks (for the measurement jobs; median turnover, sigma)
    trades.csv         v1's trades format (buy / sell / cancel rows), for verify/libcheck
    episodes.csv, rebalance_open.csv, in_sample_end_open.csv (full period only),
    universe_funnel.csv, hard_filter_exclusions.csv, trend.csv, data_quality.csv,
    trade_for_trade.csv, pending_at_end.csv, metrics.json, sanity.json

HOLDOUT LOCK. As v1: the run ends at the open of the February 2023 decision date
unless --run-final-test is passed; the CLI and the engine both check.

Every portfolio run is appended to state/trials.jsonl (study 'strategy_v2')
unless --no-record. The spec's one-run discipline: the full-period run of each
variant is made once, by the owner's process, and recorded.

The monkey test can replay a run folder through this engine with
zen.portfolio.engine_v2.Replay (see that module's docstring).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import time
from pathlib import Path

import numpy as np
import pandas as pd

from jobs import backtest_v1
from zen.portfolio import engine, engine_v2, metrics
from zen.universe import pit
from zen.validation import trials

log = logging.getLogger("backtest_v2")

STUDY = "strategy_v2"
BACKTEST = pit.REPO / "data" / "backtest"
NAV_COLUMNS = (["date", "mark", "strategy", "universe_ew"] + list(backtest_v1.INDICES) +
               list(backtest_v1.FACTOR_INDICES) +
               [k + "_no_overnight" for k in backtest_v1.FACTOR_INDICES])
DECISION_COLUMNS = ["D", "trend_c1", "trend_c2", "trend_c3", "cash_fraction", "n_universe",
                    "n_excluded_hard_filters", "n_excluded_no_balance_sheet",
                    "n_ranked_on_roce", "n_held", "n_held_unlabelled_sector"]
RANK_FILES = ("ranks.parquet", "universe_funnel.csv", "hard_filter_exclusions.csv",
              "data_quality.csv", "ranks_meta.json")
# Code and data behind the ranks: a change to any of them invalidates reused ranks.
FINGERPRINT_FILES = ["zen/portfolio/engine_v2.py", "zen/signals/composite.py",
                     "zen/validation/factors.py", "zen/universe/pit.py",
                     "zen/universe/identity.py", "data/reference/industry_nse.parquet"]


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--variant", choices=["a", "b"], required=True,
                   help="(a) staggered entry; (b) staggered entry with A1's rule")
    p.add_argument("--end", default=None, help="end date; defaults to the in-sample end")
    p.add_argument("--run-final-test", action="store_true",
                   help="permit returns after the in-sample end. Never pass this during "
                        "in-sample work.")
    p.add_argument("--no-record", action="store_true", help="do not append to trials.jsonl")
    p.add_argument("--out", default=None, help="output folder (default by variant and period)")
    p.add_argument("--reuse-ranks", action="store_true",
                   help="reuse --out's ranks when the dates and the fingerprint match")
    p.add_argument("--ranks-from", default=None,
                   help="reuse another run folder's ranks when the dates and fingerprint match")
    return p.parse_args(argv)


# ---------------------------------------------------------------- ranks
def fingerprint(con) -> dict:
    h = hashlib.sha256()
    for rel in FINGERPRINT_FILES:
        h.update(rel.encode())
        h.update((pit.REPO / rel).read_bytes())
    db = con.execute("SELECT count(*), max(date) FROM prices").fetchone()
    fin = con.execute("SELECT count(*), max(broadcast_dt) FROM financials").fetchone()
    return {"code_and_labels_sha256": h.hexdigest(),
            "prices_rows": int(db[0]), "prices_last": str(db[1]),
            "financials_rows": int(fin[0]), "financials_last": str(fin[1])}


def _load_ranks(folder: Path, decisions, fp: dict):
    if not all((folder / f).exists() for f in RANK_FILES):
        return None
    meta = json.loads((folder / "ranks_meta.json").read_text())
    want = [str(pd.Timestamp(d).date()) for d in decisions]
    if meta.get("decision_dates") != want or meta.get("fingerprint") != fp:
        log.warning("%s: ranks cover other dates or other code/data; recomputing", folder)
        return None
    ranks = pd.read_parquet(folder / "ranks.parquet")
    ranks["D"] = pd.to_datetime(ranks["D"])
    funnel = pd.read_csv(folder / "universe_funnel.csv")
    excluded = pd.read_csv(folder / "hard_filter_exclusions.csv", parse_dates=["D"])
    dq = pd.read_csv(folder / "data_quality.csv")
    log.info("reusing the ranks in %s", folder)
    return ranks, funnel, excluded, dq


def ranks_for(con, decisions, out: Path, args, static, sectors):
    fp = fingerprint(con)
    for folder, on in ((Path(args.ranks_from) if args.ranks_from else None, bool(args.ranks_from)),
                       (out, args.reuse_ranks)):
        if on and folder is not None:
            got = _load_ranks(folder, decisions, fp)
            if got is not None:
                _save_ranks(out, *got, decisions, fp)
                return got
    t = time.time()
    ranks, funnel, excluded, dq = engine_v2.build_ranks_v2(con, decisions, static, sectors)
    log.info("v2 ranks for %d dates in %.0fs", len(decisions), time.time() - t)
    _save_ranks(out, ranks, funnel, excluded, dq, decisions, fp)
    return ranks, funnel, excluded, dq


def _save_ranks(out: Path, ranks, funnel, excluded, dq, decisions, fp):
    out.mkdir(parents=True, exist_ok=True)
    ranks.to_parquet(out / "ranks.parquet", index=False)
    funnel.to_csv(out / "universe_funnel.csv", index=False)
    excluded.to_csv(out / "hard_filter_exclusions.csv", index=False)
    dq.to_csv(out / "data_quality.csv", index=False)
    (out / "ranks_meta.json").write_text(json.dumps(
        {"decision_dates": [str(pd.Timestamp(d).date()) for d in decisions],
         "fingerprint": fp}, indent=2))


# ---------------------------------------------------------------- outputs
def decisions_frame(ranks, funnel, trend, res) -> pd.DataFrame:
    fn = funnel.copy()
    fn["D"] = pd.to_datetime(fn["D"])
    fn = fn.set_index("D")
    tr = trend.set_index("D")
    held = res.decisions.copy()
    held["D"] = pd.to_datetime(held["D"])
    held = held.set_index("D")
    rows = []
    for D in tr.index:
        r = ranks[ranks["D"] == D]
        n_u = int(len(r))
        rows.append({"D": D.date(),
                     "trend_c1": bool(tr.at[D, "trend_c1"]), "trend_c2": bool(tr.at[D, "trend_c2"]),
                     "trend_c3": bool(tr.at[D, "trend_c3"]),
                     "cash_fraction": float(tr.at[D, "cash_fraction"]),
                     "n_universe": n_u,
                     "n_excluded_hard_filters": int(fn.at[D, "r7_mcap_computable"]) - n_u,
                     "n_excluded_no_balance_sheet": int(fn.at[D, "n_no_balance_sheet"]),
                     "n_ranked_on_roce": int((r["quality_measure"] == "roce").sum()),
                     "n_held": int(held.at[D, "n_held"]),
                     "n_held_unlabelled_sector": int(held.at[D, "n_held_unlabelled_sector"])})
    return pd.DataFrame(rows, columns=DECISION_COLUMNS)


def ranks_csv(ranks: pd.DataFrame) -> pd.DataFrame:
    req = [c for c in engine_v2.RANK_COLS if c != "ticker"]
    extra = ["quality_measure", "ticker", "sector", "sigma"]
    out = ranks[req + extra].copy()
    out["D"] = pd.to_datetime(out["D"]).dt.date
    out["rank"] = out["rank"].astype(int)
    return out.sort_values(["D", "rank"])


def shares_on_roce(ranks: pd.DataFrame, holdings: pd.DataFrame) -> dict:
    h = holdings[holdings["status"] != "sold"].copy()
    h["D"] = pd.to_datetime(h["D"])
    q = ranks[["D", "symbol", "quality_measure"]]
    h = h.merge(q, on=["D", "symbol"], how="left")
    return {"universe_rows": int(len(ranks)),
            "universe_rows_on_roce": int((ranks["quality_measure"] == "roce").sum()),
            "universe_share_on_roce": float((ranks["quality_measure"] == "roce").mean())
            if len(ranks) else None,
            "held_slots": int(len(h)),
            "held_slots_on_roce": int((h["quality_measure"] == "roce").sum()),
            "held_share_on_roce": float((h["quality_measure"] == "roce").mean()) if len(h) else None}


def sector_report(holdings: pd.DataFrame, sectors: pd.DataFrame) -> dict:
    h = holdings[holdings["status"] != "sold"]
    scheme = sectors["label_scheme"].reindex(h["symbol"]).to_numpy() if len(sectors) else None
    unl = h["sector"].isna()
    return {"held_slots": int(len(h)), "held_slots_unlabelled": int(unl.sum()),
            "held_share_unlabelled": float(unl.mean()) if len(h) else None,
            "held_slots_legacy_scheme_label": int((pd.Series(scheme) == "legacy").sum())
            if scheme is not None else 0,
            "unlabelled_ids_held": sorted(h.loc[unl, "symbol"].unique().tolist()),
            "legacy_label_ids_held": sorted(h.loc[(pd.Series(scheme, index=h.index) == "legacy")
                                                  .to_numpy(), "symbol"].unique().tolist())
            if scheme is not None else [],
            "note": "NSE's four-level classification as fetched in September 2026, applied "
                    "to the past (today's labels, disclosed); a company NSE no longer labels "
                    "is its own sector"}


def record(args, cfg, rep: dict) -> None:
    if args.no_record:
        return
    keep = {k: rep.get(k) for k in ("cagr", "volatility", "sharpe_rf0", "max_drawdown",
                                    "turnover_annual", "turnover_annual_incl_initial",
                                    "doubled", "start", "end")}
    ew = rep.get("vs", {}).get("universe_ew", {})
    keep["ir_vs_universe_ew"] = ew.get("information_ratio")
    keep["cagr_vs_universe_ew"] = ew.get("cagr_diff")
    keep["a1_v_weighted_ratio"] = rep.get("a1", {}).get("v_weighted_ratio")
    n = trials.record(STUDY, {"config": f"variant_{cfg.variant}", **engine_v2.config_dict(cfg)},
                      keep)
    log.info("trial recorded (%s: %d in study, %d lifetime)", STUDY, n, trials.lifetime())


# ---------------------------------------------------------------- main
def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args(argv)
    con = pit.connect()
    cal = pit.sessions(con)
    is_end = engine.in_sample_end(cal)
    end = pd.Timestamp(args.end) if args.end else is_end
    # Guard 1 of 2: the CLI. The engine checks again on every load and simulation.
    engine.guard(end, is_end, args.run_final_test)
    rft = bool(args.run_final_test)
    if rft and end > is_end:
        decisions = pit.decision_dates(cal, last=(end.year, end.month))
    else:
        decisions = pit.decision_dates(cal)
    decisions = [d for d in decisions if d < end]
    if end not in cal:
        raise ValueError(f"{end.date()} is not a session in the archive")
    cfg = engine_v2.ConfigV2(variant=args.variant)
    default = f"v2{args.variant}" if end > is_end else f"v2{args.variant}_is"
    out = Path(args.out) if args.out else BACKTEST / default
    out.mkdir(parents=True, exist_ok=True)
    log.info("variant %s: decision dates %s .. %s (%d); returns to the open of %s; out %s",
             args.variant, decisions[0].date(), decisions[-1].date(), len(decisions),
             end.date(), out)

    static = pit.StaticLabels.load(con)
    sectors = engine_v2.nse_sectors(engine_v2.load_nse_labels(), static.ids)
    ranks, funnel, excluded, dq = ranks_for(con, decisions, out, args, static, sectors)

    tri = engine_v2.load_trend_index(end, cal)       # the archive's market sessions only
    trend = engine_v2.trend_table(tri, decisions)
    cash_by_D = {pd.Timestamp(D): float(c) for D, c in zip(trend["D"], trend["cash_fraction"])}

    t = time.time()
    panel = engine_v2.build_panel_v2(con, ranks["symbol"].unique(), end, is_end, rft,
                                     ids=static.ids)
    log.info("panel %d sessions x %d stocks in %.1fs", len(panel.dates), len(panel.symbols),
             time.time() - t)
    qualify = (engine_v2.a1_qualifies(engine_v2.a1_closes(panel)) if cfg.variant == "b"
               else None)                            # A1 on EQ/BE closes only
    tickers = engine_v2.Tickers(static.ids)
    choose = engine_v2.make_chooser(ranks, cfg, excluded, sectors, tickers)
    t = time.time()
    res = engine_v2.simulate_v2(panel, decisions, choose, cash_by_D, cfg, is_end, rft,
                                qualify, tickers)
    ew = engine.run_universe_ew(panel, ranks, decisions, cfg.v1(), is_end, rft)
    log.info("strategy + universe EW simulated in %.2fs", time.time() - t)
    idx = backtest_v1.index_navs(con, decisions[0], end, is_end, rft)
    missing = [k for k in NAV_COLUMNS[4:] if k not in idx]
    if missing:
        raise RuntimeError(f"benchmarks unavailable: {missing}")

    # ------------------------------------------------ the common formats
    nav = backtest_v1.wide_nav({"strategy": res.nav, "universe_ew": ew.nav, **idx})[NAV_COLUMNS]
    nav.to_csv(out / "nav.csv", index=False)
    dec = decisions_frame(ranks, funnel, trend, res)
    dec.to_csv(out / "decisions.csv", index=False)
    hold = res.holdings.copy()
    hold["rank"] = hold["rank"].astype("Int64")
    hold.to_csv(out / "holdings.csv", index=False)
    res.fills.to_csv(out / "fills.csv", index=False)
    a1 = engine_v2.a1_metric(res)
    a1 = {"variant": cfg.variant, "start": str(decisions[0].date()), "end_open": str(end.date()),
          **a1}
    (out / "a1.json").write_text(json.dumps(a1, indent=2, default=str))
    u = ranks[["D", "symbol"]].copy()
    u["D"] = pd.to_datetime(u["D"]).dt.date
    u.sort_values(["D", "symbol"]).to_csv(out / "universe.csv", index=False)
    ranks_csv(ranks).to_csv(out / "ranks.csv", index=False)

    # ------------------------------------------------ the rest
    res.trades.to_csv(out / "trades.csv", index=False)
    res.episodes.drop(columns=["entry_t", "exit_t"], errors="ignore").to_csv(
        out / "episodes.csv", index=False)
    pd.DataFrame({"date": res.rebalance_open.index,
                  "strategy_open": res.rebalance_open.values,
                  "universe_ew_open": ew.rebalance_open.reindex(res.rebalance_open.index).values}
                 ).to_csv(out / "rebalance_open.csv", index=False)
    if decisions[0] < is_end < end:
        row = {"date": is_end.date(),
               "strategy": res.rebalance_open.get(is_end, np.nan),
               "universe_ew": ew.rebalance_open.get(is_end, np.nan),
               **backtest_v1.index_opens(con, decisions[0], is_end, end, is_end, rft)}
        pd.DataFrame([row]).to_csv(out / "in_sample_end_open.csv", index=False)
    trend.assign(D=trend["D"].dt.date, index_last_date=trend["index_last_date"].dt.date).to_csv(
        out / "trend.csv", index=False)
    res.pending_at_end.to_csv(out / "pending_at_end.csv", index=False)
    t4t = pd.concat([engine.bz_holdings(panel, res).assign(portfolio="strategy"),
                     engine.bz_holdings(panel, ew).assign(portfolio="universe_ew")],
                    ignore_index=True)
    t4t[["portfolio"] + engine.BZ_COLUMNS].to_csv(out / "trade_for_trade.csv", index=False)

    rep = metrics.full_report(res.nav, {"universe_ew": ew.nav, **idx}, res.trades, res.episodes)
    rep["engine"] = "v2"
    rep["variant"] = cfg.variant
    rep["config"] = engine_v2.config_dict(cfg)
    rep["label"] = cfg.label()
    rep["decision_dates"] = [str(d.date()) for d in decisions]
    rep["in_sample_end"] = str(is_end.date())
    rep["end_open"] = str(end.date())
    rep["readings"] = engine_v2.READINGS
    rep["quality_on_roce"] = shares_on_roce(ranks, res.holdings)
    rep["sectors"] = sector_report(res.holdings, sectors)
    rep["trend"] = {"cash_fraction_by_D": {str(pd.Timestamp(D).date()): c
                                           for D, c in cash_by_D.items()},
                    "n_dates_with_cash": int((trend["cash_fraction"] > 0).sum())}
    rep["universe_size"] = {str(pd.Timestamp(D).date()): int(n)
                            for D, n in ranks.groupby("D").size().items()}
    rep["hard_filters"] = {"excluded_rows": int(len(excluded)),
                           "by_filter": {c: int((~excluded[c].astype(bool)).sum())
                                         for c in ("pass_mcap", "pass_pe", "pass_roce", "pass_de")
                                         if c in excluded},
                           "no_balance_sheet": int(funnel["n_no_balance_sheet"].sum())}
    kept_new = res.holdings[res.holdings["status"] != "sold"]
    rep["sizing"] = {"names_without_sigma": int(kept_new["sigma"].isna().sum()),
                     "mean_book_weight": float(res.decisions["book_weight"].mean())}
    rep["fills_by_kind"] = res.fills["kind"].value_counts().to_dict()
    rep["pending_at_end"] = int(len(res.pending_at_end))
    rep["a1"] = {k: a1[k] for k in ("v_weighted_ratio", "unweighted_mean_ratio", "n_positions",
                                    "n_with_fills", "n_without_fills")}
    rep["benchmark_notes"] = {
        "universe_ew": "every stock in the v2 universe at D (rules 1-7 and the hard filters), "
                       "equal weight, v1's rules: bought in full at D, dividends and the same "
                       "costs, no trend cash, no tranches",
        "nifty_and_factor_indices": "as v1 (jobs/backtest_v1.py index_navs): NSE's TRI; factor "
                                    "indices with the Clarification to A4's open rule and the "
                                    "*_no_overnight sensitivity"}
    rep["trade_for_trade_episodes"] = t4t["portfolio"].value_counts().to_dict()
    rep["fingerprint"] = fingerprint(con)
    san = backtest_v1.sanity(con, ranks, panel, decisions)
    rep["sanity"] = {k: san[k] for k in ("mcap_check", "universe_size")}
    rep["sanity"]["n_extreme_daily_returns"] = len(san["extreme_daily_returns"])
    (out / "metrics.json").write_text(json.dumps(rep, indent=2, default=str))
    (out / "sanity.json").write_text(json.dumps(san, indent=2, default=str))
    record(args, cfg, rep)
    _print(rep, dec)
    return 0


def _print(rep: dict, dec: pd.DataFrame) -> None:
    """What was decided, never how it performed: in-sample work reads no return."""
    print(f"\n=== {rep['label']}  ({rep['start']} open -> {rep['end']} open) ===")
    print(dec.to_string(index=False))
    print("fills by kind:", rep["fills_by_kind"])
    print("quality on ROCE:", rep["quality_on_roce"])
    print("sectors:", {k: v for k, v in rep["sectors"].items() if not isinstance(v, list)})
    print("A1 price test:", rep["a1"])


if __name__ == "__main__":
    raise SystemExit(main())
