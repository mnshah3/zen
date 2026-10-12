"""Strategy v3 backtest, production engine (research/strategy/v3-spec.md).

    python -m jobs.backtest_v3 --screen [--frozen DIR] [--out DIR]
        universe.csv, eligibility.csv and ranks.csv (v1-universe members) for all
        27 reviews, 17 Feb 2020 to 17 Aug 2026. Information before each review
        only: no panel, no simulation, no return (spec 15.4).
    python -m jobs.backtest_v3 [--frozen DIR] [--out DIR] [--reviews-from DIR]
        the agreement period: reviews to 15 Nov 2022, measured to the open of
        15 Feb 2023 (the in-sample end; engine.guard refuses anything later).
    python -m jobs.backtest_v3 --frozen DIR --run-final-test --end 2026-09-18
        the one full run (spec 15.6); records one trial unless --no-record.

--frozen DIR reads every input from the freeze of spec 15.2.4 (DIR mirrors the
repo's relative paths; DIR/freeze.json maps each path to its SHA-256, checked
here before anything is read). Without it the live repo paths are read, for
development only, and metrics.json says so.

Records (spec section 13, formats 13.1: true/false, empty for missing, numbers
as Python's repr, ISO dates): universe.csv, eligibility.csv, ranks.csv,
decisions.csv, holdings.csv, fills.csv, nav.csv; also trades.csv (v1's format),
episodes.csv, rebalance_open.csv, in_sample_end_open.csv, trade_for_trade.csv,
pending_at_end.csv, universe_funnel.csv, metrics.json; and figures.parquet /
ranks.parquet (every stock's figures at every review, reused by --reviews-from
and by the replay hook).

No trial is recorded for the screen or the agreement period (spec 15.4).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import logging
import subprocess
import time
from pathlib import Path

import numpy as np
import pandas as pd

from jobs import backtest_v1
from zen.portfolio import engine, engine_v2, engine_v3, metrics
from zen.universe import pit
from zen.universe.identity import Identity
from zen.validation import trials

log = logging.getLogger("backtest_v3")

STUDY = "strategy_v3"
BACKTEST = pit.REPO / "data" / "backtest"
SPEC = "research/strategy/v3-spec.md"
INPUTS = {"db": "data/zen.duckdb",
          "labels": "data/reference/industry_nse.parquet",
          "nse_list": "nse_list.json",
          "tri": "data/external/nifty_tri.parquet",
          "endpoints": "data/external/nifty_price_endpoints.csv"}
FINGERPRINT_FILES = ["zen/portfolio/engine_v3.py", "zen/portfolio/engine_v2.py",
                     "zen/universe/pit.py", "zen/universe/identity.py",
                     "zen/validation/factors.py"]
NAV_COLUMNS = (["date", "mark", "strategy", "universe_ew"] + list(backtest_v1.INDICES) +
               list(backtest_v1.FACTOR_INDICES) +
               [k + "_no_overnight" for k in backtest_v1.FACTOR_INDICES])
ELIG_COLUMNS = ["D", "symbol", "ticker", "in_universe", "held_at_open", "c4_basis", "basis", "L",
                "run", "missing_quarters", "ttm_p", "close", "close_date", "close_series",
                "close_factor", "close_adj", "shares", "shares_fix", "mcap", "pe", "ttm_ebit",
                "fc_pos", "write_back", "icr_state", "icr", "bs_period_end", "equity", "debt",
                "de", "down0", "down1", "f0", "f1", "f2", "f3", "f4", "f5", "f6", "eligible",
                "sector_key", "ttm_r", "ttm4_r", "ttm4_p", "ttm_e", "ttm4_e", "A_R", "A_P", "M",
                "g", "PEG", "latest_period_end_c4", "owners_ttm_p", "owners_pe", "shares_ref",
                "shares_ref_over_n"]
RANK_COLUMNS = ["D", "symbol"] + engine_v3.RANK_COLS
HOLDING_COLUMNS = ["D", "symbol", "ticker", "status", "exit_codes", "units_open", "px_now",
                   "weight_open", "pe", "excess", "U0", "u_V", "u_C", "units_to_sell",
                   "buy_value", "down0", "down1", "untested", "rank", "sector_key",
                   "in_universe", "eligible"]
DECISION_COLUMNS = (["D", "nav_open", "cash_open", "budget_B", "budget_left", "n_eligible"] +
                    pit.FUNNEL_STEPS + [f"n_f{i}" for i in range(1, 7)] +
                    ["n_held_open", "n_kept"] + [f"n_exit_{c}" for c in engine_v3.EXIT_CODES] +
                    ["n_exit_V", "n_trim_valuation", "n_trim_cap", "empty_slots", "n_buys",
                     "slots_left_empty", "empty_reasons", "n_untested"] +
                    [f"n_untested_{c}" for c in engine_v3.UNTESTED_ORDER] +
                    ["n_held_outside_universe", "n_held_unclassified"])


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--screen", action="store_true",
                   help="universe, eligibility and ranks at all 27 reviews; no simulation")
    p.add_argument("--frozen", default=None, help="the freeze directory (spec 15.2.4)")
    p.add_argument("--end", default=None, help="end date; defaults to the in-sample end")
    p.add_argument("--run-final-test", action="store_true",
                   help="permit returns after the open of 15 Feb 2023 (the one full run only)")
    p.add_argument("--no-record", action="store_true", help="do not append to trials.jsonl")
    p.add_argument("--out", default=None, help="output folder")
    p.add_argument("--reviews-from", default=None,
                   help="reuse figures.parquet and ranks.parquet from this folder when the "
                        "fingerprint matches and it covers every review")
    return p.parse_args(argv)


# ---------------------------------------------------------------- inputs
def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def input_paths(frozen: str | None) -> dict:
    root = Path(frozen) if frozen else pit.REPO
    return {k: root / v for k, v in INPUTS.items()}


def verify_frozen(frozen: str) -> dict:
    """Check every input this job reads against DIR/freeze.json; return the map."""
    root = Path(frozen)
    fz = json.loads((root / "freeze.json").read_text(encoding="utf-8"))
    table = fz.get("files", fz) if isinstance(fz, dict) else {}
    table = {str(k).replace("\\", "/"): v for k, v in table.items()}
    checked = {}
    for rel in INPUTS.values():
        hit = [k for k in table if k == rel or k.endswith("/" + rel)]
        if not hit:
            raise RuntimeError(f"{rel} is not in {root / 'freeze.json'}")
        want = table[hit[0]]
        want = want.get("sha256") if isinstance(want, dict) else want
        got = sha256(root / rel)
        if got != want:
            raise RuntimeError(f"{rel}: SHA-256 {got} differs from freeze.json's {want}")
        checked[rel] = got
    return {"freeze_json": table, "checked_by_this_job": checked}


def spec_info() -> dict:
    out = {"path": SPEC, "sha256": sha256(pit.REPO / SPEC)}
    try:
        out["commit"] = subprocess.run(["git", "log", "-1", "--format=%H", "--", SPEC],
                                       cwd=pit.REPO, capture_output=True, text=True,
                                       check=True).stdout.strip()
        out["working_copy_matches_commit"] = subprocess.run(
            ["git", "diff", "--quiet", "HEAD", "--", SPEC], cwd=pit.REPO).returncode == 0
    except Exception as e:                      # not a git checkout
        out["commit"] = None
        out["git_error"] = str(e)
    return out


def static_labels(con, nse_list: Path) -> pit.StaticLabels:
    """pit.StaticLabels.load with nse_list.json read from `nse_list` (frozen or live)."""
    ids = Identity.build(con)
    return pit.StaticLabels(backfill_industry=pit.industry_labels(con, None, ids),
                            nse_lenders=ids.for_today(pit.nse_list_lenders(nse_list)), ids=ids,
                            names=pit.company_names(con, ids),
                            taxonomy_fill=pit.taxonomy_backfill(con, ids))


def fingerprint(con, paths: dict, frozen: str | None) -> dict:
    h = hashlib.sha256()
    for rel in FINGERPRINT_FILES:
        h.update(rel.encode())
        h.update((pit.REPO / rel).read_bytes())
    h.update(paths["labels"].read_bytes())
    db = con.execute("SELECT count(*), max(date) FROM prices").fetchone()
    fin = con.execute("SELECT count(*), max(broadcast_dt) FROM financials").fetchone()
    return {"code_and_labels_sha256": h.hexdigest(), "frozen": str(frozen) if frozen else None,
            "prices_rows": int(db[0]), "prices_last": str(db[1]),
            "financials_rows": int(fin[0]), "financials_last": str(fin[1])}


# ---------------------------------------------------------------- reviews
def compute_reviews(con, decisions, static, keys, tickers) -> dict:
    out = {}
    for D in decisions:
        t = time.time()
        rv = engine_v3.compute_review(con, D, static, keys, tickers)
        out[pd.Timestamp(D)] = rv
        log.info("%s: universe %d, eligible %d (%.0fs)", pd.Timestamp(D).date(),
                 rv.diag["n_members"], rv.funnel["n_eligible"], time.time() - t)
    return out


def save_reviews(out: Path, reviews: dict, fp: dict) -> None:
    out.mkdir(parents=True, exist_ok=True)
    figs = pd.concat([rv.figs.reset_index() for rv in reviews.values()], ignore_index=True)
    figs["shares_fix"] = figs["shares_fix"].astype(object).where(figs["shares_fix"].notna(), None)
    for c in ("close_series", "close_ticker", "ticker"):
        figs[c] = figs[c].astype(object).where(figs[c].notna(), None)
    figs.to_parquet(out / "figures.parquet", index=False)
    rk = pd.concat([rv.ranks.reset_index() for rv in reviews.values()], ignore_index=True)
    rk.to_parquet(out / "ranks.parquet", index=False)
    (out / "reviews_meta.json").write_text(json.dumps(
        {"decision_dates": [str(D.date()) for D in reviews], "fingerprint": fp,
         "funnel": {str(D.date()): rv.funnel for D, rv in reviews.items()},
         "diag": {str(D.date()): rv.diag for D, rv in reviews.items()}}, indent=2, default=str))


def load_reviews(folder: Path, decisions, fp: dict) -> dict | None:
    meta_p = folder / "reviews_meta.json"
    if not meta_p.exists():
        return None
    meta = json.loads(meta_p.read_text())
    if meta.get("fingerprint") != fp:
        log.warning("%s: other code or data; recomputing the reviews", folder)
        return None
    want = [str(pd.Timestamp(d).date()) for d in decisions]
    if not set(want) <= set(meta["decision_dates"]):
        log.warning("%s: does not cover every review; recomputing", folder)
        return None
    figs = pd.read_parquet(folder / "figures.parquet")
    figs["D"] = pd.to_datetime(figs["D"])
    rk = pd.read_parquet(folder / "ranks.parquet")
    rk["D"] = pd.to_datetime(rk["D"])
    out = {}
    for d in want:
        D = pd.Timestamp(d)
        f = figs[figs["D"] == D].set_index("symbol")
        r = rk[rk["D"] == D].sort_values("rank").set_index("symbol")
        out[D] = engine_v3.Review(D=D, figs=f, ranks=r, funnel=meta["funnel"][d],
                                  diag=meta["diag"][d])
    log.info("reusing the reviews in %s", folder)
    return out


# ---------------------------------------------------------------- records
def _fmt(v) -> str:
    if v is None or v is pd.NA or v is pd.NaT:
        return ""
    if isinstance(v, (bool, np.bool_)):
        return "true" if v else "false"
    if isinstance(v, (float, np.floating)):
        return "" if np.isnan(v) else repr(float(v))
    if isinstance(v, (int, np.integer)):
        return str(int(v))
    if isinstance(v, pd.Timestamp):
        return v.date().isoformat()
    if isinstance(v, np.datetime64):
        return "" if np.isnat(v) else pd.Timestamp(v).date().isoformat()
    if isinstance(v, _dt.datetime):
        return v.date().isoformat()
    if isinstance(v, _dt.date):
        return v.isoformat()
    return str(v)


def write_csv(df: pd.DataFrame, path: Path, columns=None) -> None:
    """Section 13.1 formats."""
    if columns is not None:
        df = df.reindex(columns=columns)
    body = df.astype(object)
    body = body.map(_fmt) if hasattr(body, "map") else body.applymap(_fmt)
    body.to_csv(path, index=False)


def universe_frame(reviews: dict) -> pd.DataFrame:
    rows = [rv.figs.loc[rv.figs["f0"], ["D", "ticker"]].reset_index() for rv in reviews.values()]
    u = pd.concat(rows, ignore_index=True)[["D", "symbol", "ticker"]]
    return u.sort_values(["D", "symbol"])


def eligibility_frame(reviews: dict, held_at_open: dict | None = None) -> pd.DataFrame:
    rows = []
    for D, rv in reviews.items():
        held = held_at_open.get(D, set()) if held_at_open else set()
        f = rv.figs
        keep = f["f0"].to_numpy(dtype=bool) | f.index.isin(list(held))
        e = f[keep].copy()
        e["held_at_open"] = e.index.isin(list(held))
        missing = sorted(set(held) - set(f.index))
        if missing:                                    # a holding with no known quarter
            extra = pd.DataFrame(index=pd.Index(missing, name="symbol"))
            extra["D"] = D
            extra["held_at_open"] = True
            extra["in_universe"] = False
            for c in ("f0", "f1", "f2", "f3", "f4", "f5", "f6", "eligible"):
                extra[c] = False
            e = pd.concat([e, extra])
        rows.append(e.reset_index())
    out = pd.concat(rows, ignore_index=True)
    return out.sort_values(["D", "symbol"])


def ranks_frame(reviews: dict) -> pd.DataFrame:
    rk = pd.concat([rv.ranks.reset_index() for rv in reviews.values()], ignore_index=True)
    rk["rank"] = rk["rank"].astype(int)
    return rk.sort_values(["D", "rank"])


def funnel_frame(reviews: dict) -> pd.DataFrame:
    rows = []
    for D, rv in reviews.items():
        row = pit.funnel_frame(D, rv.funnel).iloc[0].to_dict()
        for k in [f"n_f{i}" for i in range(1, 7)] + ["n_eligible"]:
            row[k] = rv.funnel[k]
        row.update({f"diag_{k}": v for k, v in rv.diag.items()})
        rows.append(row)
    return pd.DataFrame(rows)


def index_navs(con, start, end, is_end, rft, paths) -> dict:
    out = {}
    for key, (tri_name, price_name) in backtest_v1.INDICES.items():
        try:
            out[key] = engine.tri_nav(con, tri_name, price_name, start, end, is_end, rft,
                                      path=paths["tri"])
        except ValueError as e:
            log.warning("%s unavailable: %s", tri_name, e)
    for overnight, suffix in ((True, ""), (False, "_no_overnight")):
        for key, (tri_name, parent) in backtest_v1.FACTOR_INDICES.items():
            out[key + suffix] = engine.factor_tri_nav(con, tri_name, parent, start, end, is_end,
                                                      rft, overnight=overnight, path=paths["tri"],
                                                      endpoints_path=paths["endpoints"])
    return out


def index_opens(con, start, d, end, is_end, rft, paths) -> dict:
    out = {}
    for key, (tri_name, price_name) in backtest_v1.INDICES.items():
        out[key] = engine.tri_value_at_open(con, tri_name, price_name, start, d, end, is_end,
                                            rft, path=paths["tri"])
    for overnight, suffix in ((True, ""), (False, "_no_overnight")):
        for key, (tri_name, parent) in backtest_v1.FACTOR_INDICES.items():
            out[key + suffix] = engine.tri_value_at_open(
                con, tri_name, parent, start, d, end, is_end, rft, factor=True,
                overnight=overnight, path=paths["tri"], endpoints_path=paths["endpoints"])
    return out


def v3_figures(res: engine_v3.ResultV3) -> dict:
    """Section 14.6's v3-specific figures that need no return."""
    dec = res.decisions
    f = res.fills[res.fills["event"] == "fill"]
    out = {"cash_share_after_plan": {
        "mean": float((dec["budget_left"] / dec["nav_open"]).mean()) if len(dec) else None,
        "max": float((dec["budget_left"] / dec["nav_open"]).max()) if len(dec) else None},
        "holdings_by_review": {str(pd.Timestamp(r.D).date()): int(r.n_kept + r.n_buys)
                               for r in dec.itertuples()},
        "exits_by_code": {c: int(dec[f"n_exit_{c}"].sum())
                          for c in engine_v3.EXIT_CODES + ["V"]},
        "trims_placed": {k: int(dec[f"n_trim_{k}"].sum()) for k in ("valuation", "cap")},
        "trims_filled": {k: {"count": int(sel.sum()), "rupees": float(f.loc[sel, "value"].sum())}
                         for k in ("valuation", "cap")
                         for sel in [(f["kind"] == k) & (f["reason"] == f"trim_{k}")]},
        "empty_slots_left": int(dec["slots_left_empty"].sum()),
        "held_outside_universe": int(dec["n_held_outside_universe"].sum()),
        "held_untested": {c: int(dec[f"n_untested_{c}"].sum())
                          for c in engine_v3.UNTESTED_ORDER},
        "held_slots_unclassified_share": None}
    h = res.holdings[res.holdings["status"] != "exited"]
    if len(h):
        out["held_slots_unclassified_share"] = float(
            (h["sector_key"] == engine_v3.UNCLASSIFIED).mean())
    return out


def record_trial(args, rep: dict) -> None:
    if args.no_record or not args.run_final_test:
        return
    keep = {k: rep.get(k) for k in ("cagr", "volatility", "sharpe_rf0", "max_drawdown",
                                    "turnover_annual", "turnover_annual_incl_initial",
                                    "doubled", "start", "end")}
    n = trials.record(STUDY, {"config": "v3", "spec_commit": rep["spec"].get("commit")}, keep)
    log.info("trial recorded (%s: %d in study, %d lifetime)", STUDY, n, trials.lifetime())


# ---------------------------------------------------------------- main
def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args(argv)
    paths = input_paths(args.frozen)
    inputs = (verify_frozen(args.frozen) if args.frozen
              else {"live": "live repo paths (development only; not the freeze)",
                    "paths": {k: str(v) for k, v in paths.items()}})
    con = pit.connect(paths["db"])
    cal = pit.sessions(con)
    is_end = engine.in_sample_end(cal)
    static = static_labels(con, paths["nse_list"])
    sectors = engine_v2.nse_sectors(pd.read_parquet(paths["labels"]), static.ids)
    keys = engine_v3.sector_keys(sectors)
    tickers = engine_v2.Tickers(static.ids)
    fp = fingerprint(con, paths, args.frozen)
    spec = spec_info()

    if args.screen:
        decisions = pit.decision_dates(cal, first=engine_v3.FIRST_REVIEW,
                                       last=engine_v3.LAST_REVIEW)
        out = Path(args.out) if args.out else BACKTEST / "v3_dev" / "screen"
        reviews = (load_reviews(Path(args.reviews_from), decisions, fp)
                   if args.reviews_from else None) or compute_reviews(con, decisions, static,
                                                                      keys, tickers)
        save_reviews(out, reviews, fp)
        write_csv(universe_frame(reviews), out / "universe.csv")
        write_csv(eligibility_frame(reviews), out / "eligibility.csv", ELIG_COLUMNS)
        write_csv(ranks_frame(reviews), out / "ranks.csv", RANK_COLUMNS)
        write_csv(funnel_frame(reviews), out / "universe_funnel.csv")
        meta = {"engine": "v3", "mode": "screen", "spec": spec, "inputs": inputs,
                "decision_dates": [str(D.date()) for D in decisions], "fingerprint": fp,
                "readings": engine_v3.READINGS,
                "counts": {str(D.date()): {"universe": rv.diag["n_members"],
                                           "eligible": rv.funnel["n_eligible"], **rv.diag}
                           for D, rv in reviews.items()}}
        (out / "screen.json").write_text(json.dumps(meta, indent=2, default=str))
        print(funnel_frame(reviews)[["D", "r7_mcap_computable", "n_f1", "n_f2", "n_f3", "n_f4",
                                     "n_f5", "n_f6", "n_eligible"]].to_string(index=False))
        return 0

    end = pd.Timestamp(args.end) if args.end else is_end
    engine.guard(end, is_end, args.run_final_test)       # guard 1 of 2: the CLI
    rft = bool(args.run_final_test)
    if end not in cal:
        raise ValueError(f"{end.date()} is not a session in the archive")
    last = engine_v3.LAST_REVIEW if (rft and end > is_end) else engine_v3.AGREEMENT_LAST
    decisions = [d for d in pit.decision_dates(cal, first=engine_v3.FIRST_REVIEW, last=last)
                 if d < end]
    default = "v3" if end > is_end else "v3_dev"
    out = Path(args.out) if args.out else BACKTEST / default
    out.mkdir(parents=True, exist_ok=True)
    log.info("reviews %s .. %s (%d); measured to the open of %s; out %s", decisions[0].date(),
             decisions[-1].date(), len(decisions), end.date(), out)

    reviews = None
    for folder in ([Path(args.reviews_from)] if args.reviews_from else []) + [out]:
        reviews = load_reviews(folder, decisions, fp)
        if reviews is not None:
            break
    if reviews is None:
        reviews = compute_reviews(con, decisions, static, keys, tickers)
    reviews = {D: reviews[D] for D in decisions}
    save_reviews(out, reviews, fp)

    elig_rows = pd.concat([rv.ranks.reset_index()[["D", "symbol"]] for rv in reviews.values()],
                          ignore_index=True)
    t = time.time()
    panel = engine.build_panel(con, elig_rows["symbol"].unique(), decisions[0], end, is_end,
                               rft, ids=static.ids)
    log.info("panel %d sessions x %d stocks in %.1fs", len(panel.dates), len(panel.symbols),
             time.time() - t)
    res = engine_v3.simulate_v3(panel, decisions, reviews, keys, is_end, rft, tickers)
    ew_cfg = engine.Config(cost=engine_v3.COST, initial_capital=engine_v3.INITIAL_CAPITAL)
    ew = engine.run_universe_ew(panel, elig_rows, decisions, ew_cfg, is_end, rft)
    idx = index_navs(con, decisions[0], end, is_end, rft, paths)
    missing = [k for k in NAV_COLUMNS[4:] if k not in idx]
    if missing:
        raise RuntimeError(f"benchmarks unavailable: {missing}")

    # ------------------------------------------------ section 13 records
    nav = backtest_v1.wide_nav({"strategy": res.nav, "universe_ew": ew.nav, **idx})[NAV_COLUMNS]
    write_csv(nav, out / "nav.csv")
    write_csv(universe_frame(reviews), out / "universe.csv")
    held_open = {}
    hh = res.holdings[res.holdings["status"] != "new"]
    for D, g in hh.groupby("D"):
        held_open[pd.Timestamp(D)] = set(g["symbol"])
    write_csv(eligibility_frame(reviews, held_open), out / "eligibility.csv", ELIG_COLUMNS)
    write_csv(ranks_frame(reviews), out / "ranks.csv", RANK_COLUMNS)
    dec = res.decisions.copy()
    write_csv(dec, out / "decisions.csv", DECISION_COLUMNS)
    hold = res.holdings.copy()
    hold["rank"] = hold["rank"].astype("Int64")
    write_csv(hold, out / "holdings.csv", HOLDING_COLUMNS)
    write_csv(res.fills, out / "fills.csv", engine_v3.FILL_COLUMNS)

    # ------------------------------------------------ the rest
    write_csv(res.trades, out / "trades.csv")
    write_csv(res.episodes.drop(columns=["entry_t", "exit_t"], errors="ignore"),
              out / "episodes.csv")
    write_csv(pd.DataFrame({"date": res.rebalance_open.index,
                            "strategy_open": res.rebalance_open.values,
                            "universe_ew_open": ew.rebalance_open.reindex(
                                res.rebalance_open.index).values}), out / "rebalance_open.csv")
    if decisions[0] < is_end <= end:
        sv = (res.rebalance_open.get(is_end, np.nan) if is_end < end
              else float(res.nav["nav"].iloc[-1]))
        ev = (ew.rebalance_open.get(is_end, np.nan) if is_end < end
              else float(ew.nav["nav"].iloc[-1]))
        row = {"date": is_end.date(), "strategy": sv, "universe_ew": ev,
               **index_opens(con, decisions[0], is_end, end, is_end, rft, paths)}
        write_csv(pd.DataFrame([row]), out / "in_sample_end_open.csv")
    write_csv(res.pending_at_end, out / "pending_at_end.csv")
    t4t = pd.concat([engine_v3.bz_holdings(panel, res).assign(portfolio="strategy"),
                     engine.bz_holdings(panel, ew).assign(portfolio="universe_ew")],
                    ignore_index=True)
    write_csv(t4t[["portfolio"] + engine.BZ_COLUMNS], out / "trade_for_trade.csv")
    write_csv(funnel_frame(reviews), out / "universe_funnel.csv")

    rep = metrics.full_report(res.nav, {"universe_ew": ew.nav, **idx}, res.trades, res.episodes)
    rep["engine"] = "v3"
    rep["spec"] = spec
    rep["inputs"] = inputs
    rep["fingerprint"] = fp
    rep["decision_dates"] = [str(d.date()) for d in decisions]
    rep["in_sample_end"] = str(is_end.date())
    rep["end_open"] = str(end.date())
    rep["run_final_test"] = rft
    rep["readings"] = engine_v3.READINGS
    rep["v3"] = v3_figures(res)
    rep["fills_by_kind"] = {f"{k}_{e}": int(n) for (k, e), n in
                            res.fills.groupby(["kind", "event"]).size().items()}
    rep["pending_at_end"] = res.pending_at_end.to_dict("records")
    rep["review_diag"] = {str(D.date()): rv.diag for D, rv in reviews.items()}
    rep["benchmark_notes"] = {
        "universe_ew": "v3's eligible list at each review, equal weight, rebalanced at every "
                       "review (engine.run_universe_ew): same costs, dividends and forced exit",
        "nifty_and_factor_indices": "as v1 and v2: NSE's TRI; factor indices with the "
                                    "Clarification to A4's open rule and *_no_overnight"}
    (out / "metrics.json").write_text(json.dumps(rep, indent=2, default=str))
    record_trial(args, rep)
    _print(res)
    return 0


def replay(run_dir, con=None) -> engine_v3.Replay:
    """The v3 book exactly as a run folder used it, for jobs/verify_v1.py's monkey test.

    Everything comes from the run: its inputs (the frozen directory recorded in its
    metrics.json, checked against freeze.json, or the live paths for a development run),
    its review dates and end, and its reviews as written (figures.parquet and
    ranks.parquet, not recomputed). The caller checks `Replay.reproduce` against the
    run's nav.csv before drawing anything through it."""
    run = Path(run_dir)
    m = json.loads((run / "metrics.json").read_text(encoding="utf-8"))
    if m.get("engine") != "v3":
        raise ValueError(f"{run}: not a v3 run (metrics.json engine is {m.get('engine')!r})")
    frozen = (m.get("fingerprint") or {}).get("frozen")
    paths = input_paths(frozen)
    if frozen:
        verify_frozen(frozen)
    con = con or pit.connect(paths["db"])
    cal = pit.sessions(con)
    is_end = engine.in_sample_end(cal)
    end, rft = pd.Timestamp(m["end_open"]), bool(m["run_final_test"])
    engine.guard(end, is_end, rft)
    static = static_labels(con, paths["nse_list"])
    keys = engine_v3.sector_keys(engine_v2.nse_sectors(pd.read_parquet(paths["labels"]),
                                                       static.ids))
    decisions = [pd.Timestamp(d) for d in m["decision_dates"]]
    reviews = load_reviews(run, decisions, m["fingerprint"])
    if reviews is None:
        raise RuntimeError(f"{run}: its reviews (figures.parquet, ranks.parquet) do not match "
                           "its metrics.json; the run folder was changed after the run")
    reviews = {D: reviews[D] for D in decisions}
    syms = pd.concat([rv.ranks.reset_index()[["symbol"]] for rv in reviews.values()],
                     ignore_index=True)["symbol"].unique()
    panel = engine.build_panel(con, syms, decisions[0], end, is_end, rft, ids=static.ids)
    return engine_v3.Replay(panel=panel, decisions=decisions, reviews=reviews, keys=keys,
                            is_end=is_end, run_final_test=rft,
                            tickers=engine_v2.Tickers(static.ids))


def _print(res: engine_v3.ResultV3) -> None:
    """What was decided, never how it performed."""
    cols = ["D", "n_eligible", "n_held_open", "n_kept", "n_exit_E1", "n_exit_E2", "n_exit_E3",
            "n_exit_E4", "n_exit_E5", "n_exit_V", "n_trim_valuation", "n_trim_cap", "n_buys",
            "slots_left_empty", "empty_reasons", "n_untested", "n_held_outside_universe"]
    d = res.decisions.copy()
    d["D"] = pd.to_datetime(d["D"]).dt.date
    print(d[cols].to_string(index=False))
    print("fills:", res.fills.groupby(["kind", "event"]).size().to_dict())
    print("pending at end:", len(res.pending_at_end))


if __name__ == "__main__":
    raise SystemExit(main())
