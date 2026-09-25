"""After Indian costs and tax (research/strategy/v2-spec.md, amendment A6).

    python -m jobs.after_tax --in data/backtest/v1 [--suffix _cost40] [--capital 500000]
                             [--slippage 0.001] [--slab 0.30] [--bench nifty500]
                             [--tax-timing advance|advance_minimum|april] [--ter 0.0016]
                             [--out DIR]

Reads a finished backtest folder (jobs/backtest_v1.py's layout) and never
re-runs the engine:

    trades{suffix}.csv     every fill (adjusted units and prices), cancellations ignored
    nav{suffix}.csv        date, mark, strategy, ..., nifty500 (TRI NAV on the same clock)

and inputs the engine does not write. Each is read from the input folder when
present, else from the output folder (a previous run's rebuild), else rebuilt
from the database exactly as the engine built them (engine.build_panel over
the traded symbols) and written to the output folder, so a second run is
repeated without the database:

    dividends{suffix}.csv  date, symbol, amount: cash the engine credited
                           (units held at the previous close x div_unit)
    close_px{suffix}.csv   adjusted closes, ffilled (engine.Panel.last), date x symbol
    end_px{suffix}.csv     symbol, price: the engine's end mark (open, or last close)
    bench{suffix}.csv      date, mark, tri, price: the benchmark index fund's TRI and
                           price levels on the clock (price from engine.index_nav)
    bonuses{suffix}.csv    symbol, ex_date, factor: equity bonus issues of the traded
                           stocks, de-duplicated as pit.split_factors does
    corp_unmodelled{suffix}.csv  symbol, ex_date, kind, subject: demergers,
                           amalgamations, schemes of arrangement and non-equity bonuses
                           (debentures, preference shares), which the lot book does
                           not model; reported, with the trades affected

summary.json records the sha256 of every input file and the database file's
size and modification time.

Before any tax is applied the inputs are replayed through the engine's cash
rule and must reproduce the NAV column to 1e-6, every trade's value must equal
units x price to 1e-6, and the benchmark's TRI must equal the NAV file's
benchmark column (replay_check); otherwise the job stops.

Writes to <in>/after_tax{suffix}/ (or --out):
    after_tax_nav.csv          date, mark, pre_tax, k, after_tax (the book), tax_owed_realised,
                               held_value, exit_charges, tax_owed_if_liquidated,
                               liquidation_value (strategy)
    tax_by_year.csv            per financial year: gains, set-off, exemption, tax, advance
                               tax, true-up, s.234B/234C interest, refund
    tax_payments.csv           every advance-tax instalment, true-up, interest and refund
    lots_realised.csv          every FIFO match of a sale against a lot
    costs_by_trade.csv         itemised charges per trade against the flat charge
    cost_per_side.csv          one Rs 50,000 order in each rate period vs 0.20% per side
    bench_after_tax_nav.csv    the headline benchmark: a growth-option index fund with
    bench_tax_by_year.csv      its expense ratio, held throughout and sold at the end
    bench_gross_tri_*.csv      the same fund at TER 0 (the gross TRI)
    bench_distributed_*.csv    sensitivity: the index's dividends paid out and taxed,
                               close to holding the index directly (s.115R before 2020)
    slippage_sensitivity.csv   end values at other slippage assumptions
    summary.json               end values, after-tax statistics, checks, parameters,
                               disclosures and every rate's source

Every after-tax statistic (CAGR, volatility, drawdown, calendar years) is
computed from liquidation_value, the value if everything were sold at that mark
after exit costs and tax: the strategy pays most of its tax as it goes and a
growth fund defers almost all of it, so only liquidation values are like for
like. This is a reporting layer. It does not choose, tune or re-run anything.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from zen.portfolio import india_tax as it  # noqa: E402
from zen.portfolio import metrics  # noqa: E402

log = logging.getLogger("after_tax")

BENCH_PRICE = {"nifty500": "Nifty 500", "midcap150": "Nifty Midcap 150",
               "smallcap250": "Nifty Smallcap 250"}
DERIVED = ("dividends", "close_px", "end_px", "bench", "bonuses", "corp_unmodelled")
UNMODELLED_RX = r"merg|amalg|arrange"


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--in", dest="inp", required=True, help="backtest output folder")
    p.add_argument("--suffix", default="", help="file suffix, e.g. _cost40")
    p.add_argument("--nav-col", default="strategy")
    p.add_argument("--bench", default="nifty500", help="nav column holding the benchmark TRI")
    p.add_argument("--capital", type=float, default=None,
                   help="starting rupees (default: the backtest's own starting NAV)")
    p.add_argument("--slippage", type=float, default=it.Params.slippage)
    p.add_argument("--slab", type=float, default=it.Params.slab)
    p.add_argument("--tax-timing", default=it.Params.tax_timing,
                   choices=("advance", "advance_minimum", "april"))
    p.add_argument("--ter", type=float, default=it.Params.fund_ter,
                   help="index fund expense ratio a year (default: FUND_SOURCES['ter'])")
    p.add_argument("--order", type=float, default=50_000.0, help="order size for cost_per_side")
    p.add_argument("--slippage-grid", default="0,0.0005,0.001,0.002")
    p.add_argument("--out", default=None)
    p.add_argument("--run-final-test", action="store_true",
                   help="only for rebuilding inputs of the reviewed holdout run's output")
    return p.parse_args(argv)


# ---------------------------------------------------------------- inputs
def _read(name: str, path: Path):
    if not path.exists():
        return None
    if name == "close_px":
        return pd.read_csv(path, index_col=0, parse_dates=True)
    if name == "end_px":
        return pd.read_csv(path).set_index("symbol")["price"]
    if name in ("dividends", "bench"):
        return pd.read_csv(path, parse_dates=["date"])
    if name in ("bonuses", "corp_unmodelled"):
        return pd.read_csv(path, parse_dates=["ex_date"])
    return pd.read_csv(path)


def _write(name: str, obj, path: Path) -> None:
    if name == "end_px":
        obj.rename("price").rename_axis("symbol").reset_index().to_csv(path, index=False)
    else:
        obj.to_csv(path, index=(name == "close_px"))


def corp_events(con, syms, start, end, ids=None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(equity bonus issues, unmodelled corporate actions) of `syms` with start < ex_date <= end.

    Bonuses follow pit.split_factors row for row (the lenient parse of a missing
    factor, the stock-id mapping, de-duplication on symbol, ex_date, action and
    factor), restricted to action 'bonus', multiplied per (symbol, ex_date).
    """
    from zen.universe import pit

    start, end = pd.Timestamp(start), pd.Timestamp(end)
    ca = con.execute("SELECT symbol, ex_date, action, subject, factor FROM corpactions "
                     "WHERE action = 'bonus' AND ex_date <= ?", [end.date()]).df()
    miss = ca["factor"].isna()
    if miss.any():
        ca.loc[miss, "factor"] = np.array(
            [pit._lenient_factor(a, s) for a, s in
             zip(ca.loc[miss, "action"], ca.loc[miss, "subject"])], dtype=float)
    if ids is not None and not ids.empty and not ca.empty:
        ca["symbol"] = ids.for_events(ca, "symbol", "ex_date")
    ca["ex_date"] = pd.to_datetime(ca["ex_date"])
    ca = ca[ca["symbol"].isin(set(syms)) & (ca["ex_date"] > start) & (ca["ex_date"] <= end)]
    eq = ca[ca["factor"].notna() & (ca["factor"] > 0)]
    eq = eq.drop_duplicates(subset=["symbol", "ex_date", "action", "factor"])
    bonuses = (eq.groupby(["symbol", "ex_date"], as_index=False)["factor"].prod()
                 .sort_values(["ex_date", "symbol"]).reset_index(drop=True))
    non_eq = ca[ca["factor"].isna()].assign(kind="bonus_non_equity")

    ot = con.execute("SELECT symbol, ex_date, subject FROM corpactions WHERE action = 'other' "
                     "AND regexp_matches(lower(subject), ?) AND ex_date <= ?",
                     [UNMODELLED_RX, end.date()]).df()
    if ids is not None and not ids.empty and not ot.empty:
        ot["symbol"] = ids.for_events(ot, "symbol", "ex_date")
    ot["ex_date"] = pd.to_datetime(ot["ex_date"])
    ot = ot[ot["symbol"].isin(set(syms)) & (ot["ex_date"] > start) & (ot["ex_date"] <= end)]
    low = ot["subject"].str.lower()
    ot["kind"] = np.where(low.str.contains("demerg|de-merg"), "demerger",
                          np.where(low.str.contains("amalg|merg"), "amalgamation",
                                   "scheme_of_arrangement"))
    un = pd.concat([ot[["symbol", "ex_date", "kind", "subject"]],
                    non_eq[["symbol", "ex_date", "kind", "subject"]]], ignore_index=True)
    un = (un.drop_duplicates(["symbol", "ex_date", "kind"])
            .sort_values(["ex_date", "symbol"]).reset_index(drop=True))
    return bonuses, un


def rebuild_inputs(trades: pd.DataFrame, nav: pd.DataFrame, bench_col: str,
                   run_final_test: bool) -> dict:
    """Dividends, prices, bonuses and the benchmark from the database, as the engine built them."""
    from zen.portfolio import engine
    from zen.universe import pit

    con = pit.connect()
    cal = pit.sessions(con)
    is_end = engine.in_sample_end(cal)
    start, end = pd.Timestamp(nav["date"].iloc[0]), pd.Timestamp(nav["date"].iloc[-1])
    static = pit.StaticLabels.load(con)
    syms = sorted(trades.loc[trades["side"].isin(["buy", "sell"]), "symbol"].unique())
    panel = engine.build_panel(con, syms, start, end, is_end, run_final_test, ids=static.ids)
    keep = (panel.dates >= start) & (panel.dates <= end)
    dates = panel.dates[keep]
    close_px = pd.DataFrame(panel.last[keep], index=dates, columns=panel.symbols)
    t_end = len(panel.dates) - 1
    ok = panel.traded[t_end] & (panel.open_[t_end] > 0)
    end_px = pd.Series(np.where(ok, panel.open_[t_end], panel.last[t_end - 1]),
                       index=panel.symbols, name="price")
    divs = it.reconstruct_dividends(trades, panel.dates, panel.symbols, panel.div_unit)
    divs = divs[(divs["date"] > start) & (divs["date"] <= end)].reset_index(drop=True)
    bonuses, unmodelled = corp_events(con, syms, start, end, static.ids)
    out = {"dividends": divs, "close_px": close_px, "end_px": end_px, "bonuses": bonuses,
           "corp_unmodelled": unmodelled}
    if bench_col in BENCH_PRICE and bench_col in nav.columns:
        px = engine.index_nav(con, BENCH_PRICE[bench_col], start, end, is_end, run_final_test)
        out["bench"] = _bench_clock(nav, bench_col, px)
    return out


def _bench_clock(nav: pd.DataFrame, bench_col: str, price_nav: pd.DataFrame) -> pd.DataFrame:
    a = nav[["date", "mark", bench_col]].rename(columns={bench_col: "tri"}).reset_index(drop=True)
    b = price_nav.rename(columns={"nav": "price"}).reset_index(drop=True)
    a["date"], b["date"] = pd.to_datetime(a["date"]), pd.to_datetime(b["date"])
    if len(a) != len(b) or not (a["date"].equals(b["date"]) and a["mark"].equals(b["mark"])):
        raise ValueError("benchmark price index is not on the strategy's clock")
    a["price"] = b["price"].to_numpy()
    return a


def load_inputs(inp: Path, suffix: str, nav_col: str, bench_col: str,
                run_final_test: bool = False, out: Path | None = None) -> tuple[dict, dict, dict]:
    """Inputs, where each came from ('folder', 'output folder', 'database', ...) and the
    file each was read from (None for a rebuilt one until it is written)."""
    trades = pd.read_csv(inp / f"trades{suffix}.csv")
    nav = pd.read_csv(inp / f"nav{suffix}.csv")
    nav["date"] = pd.to_datetime(nav["date"])
    if nav_col not in nav.columns:
        raise ValueError(f"nav{suffix}.csv has no column {nav_col!r}")
    got = {"trades": trades, "nav": nav}
    provenance, files = {}, {"trades": inp / f"trades{suffix}.csv",
                             "nav": inp / f"nav{suffix}.csv"}
    for k in DERIVED:
        got[k], provenance[k], files[k] = None, None, None
        for where, folder in (("folder", inp), ("output folder", out)):
            if folder is None:
                continue
            f = folder / f"{k}{suffix}.csv"
            obj = _read(k, f)
            if obj is not None:
                got[k], provenance[k], files[k] = obj, where, f
                break
    need = [k for k in DERIVED if got[k] is None]
    if "bench" in need and bench_col not in nav.columns:
        need.remove("bench")
        provenance["bench"] = "not requested"
    if need:
        log.info("rebuilding %s from the database", ", ".join(need))
        rebuilt = rebuild_inputs(trades, nav, bench_col, run_final_test)
        for k in need:
            got[k] = rebuilt.get(k)
            provenance[k] = "database" if got[k] is not None else "unavailable"
    return got, provenance, files


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _database_info(used: bool) -> dict:
    try:
        from zen.universe import pit
        st = os.stat(pit.DB_PATH)
        return {"path": str(pit.DB_PATH), "used_for_rebuild": used, "size_bytes": st.st_size,
                "mtime_utc": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat()}
    except Exception as e:           # no database on this machine: record why
        return {"used_for_rebuild": used, "error": repr(e)}


def _stats(frame: pd.DataFrame, col: str = "liquidation_value") -> dict:
    f = frame[["date", "mark", col]].rename(columns={col: "nav"}).copy()
    f["date"] = pd.to_datetime(f["date"])
    return metrics.summary(f)


_INSTALMENTS = ("instalments at the close of the last session whose sale settles by 15 Jun, "
                "15 Sep, 15 Dec and 15 Mar (T+2 before 27 Jan 2023, T+1 after): ")
_TRUE_UP = ("; true-up at the first session of the next year with s.234C and s.234B "
            "interest (base rounded down to Rs 100, Rule 119A) when the year's tax is "
            "Rs 10,000 or more; overpayments refunded then, without s.244A interest and "
            "earlier than a real refund, which can favour the strategy")
ADVANCE_TAX_LABELS = {
    "advance": _INSTALMENTS + "100% of the tax on income realised before that session "
               "less tax paid, when that is Rs 10,000 or more" + _TRUE_UP,
    "advance_minimum": _INSTALMENTS + "the cumulative 15/45/75/100% statutory minimum of "
                       "the year-to-date tax less tax paid, when that is Rs 10,000 or more"
                       + _TRUE_UP,
    "april": "sensitivity: no advance tax; each year's whole tax paid at the first session "
             "of the next year, with no interest (the earlier convention)",
}


# ---------------------------------------------------------------- run
def run(inp: Path, out: Path, suffix: str = "", nav_col: str = "strategy",
        bench_col: str = "nifty500", capital: float | None = None,
        p: it.Params = it.Params(), order: float = 50_000.0,
        slippage_grid=(0.0, 0.0005, 0.001, 0.002), run_final_test: bool = False) -> dict:
    x, provenance, files = load_inputs(inp, suffix, nav_col, bench_col, run_final_test, out)
    out.mkdir(parents=True, exist_ok=True)
    for k in DERIVED:
        if provenance[k] == "database":
            f = out / f"{k}{suffix}.csv"
            _write(k, x[k], f)
            files[k] = f
    fingerprint = {k: {"path": str(f), "sha256": _sha256(f)}
                   for k, f in files.items() if f is not None}
    # Which database built the rebuilt inputs survives a later run that reuses
    # them from the output folder: it is recorded once, at rebuild time.
    side = out / f"inputs_provenance{suffix}.json"
    rebuilt = [k for k in DERIVED if provenance[k] == "database"]
    if rebuilt:
        side.write_text(json.dumps({"database": _database_info(True),
                                    "files": {k: fingerprint[k] for k in rebuilt}},
                                   indent=1, default=str), encoding="utf-8")
    rebuilt_provenance = None
    if side.exists():
        rebuilt_provenance = json.loads(side.read_text(encoding="utf-8"))
        same = all(fingerprint.get(k, {}).get("sha256") == v["sha256"]
                   for k, v in rebuilt_provenance["files"].items() if k in fingerprint)
        rebuilt_provenance["hashes_match_this_run"] = bool(same)
        if not same:
            log.warning("inputs in %s no longer match %s", out, side.name)

    chk = it.replay_check(x["trades"], x["nav"], x["dividends"], x["close_px"], x["end_px"],
                          nav_col, bench=x["bench"], bench_col=bench_col)
    if chk["worst_rel_error"] > 1e-6 or chk["trade_value_worst_rel"] > 1e-6:
        raise RuntimeError(f"inputs do not reproduce the engine's NAV: {chk}")
    b_err = (chk.get("bench") or {}).get("tri_vs_nav_worst_rel")
    if b_err is not None and b_err > 1e-9:
        raise RuntimeError(f"benchmark TRI does not reproduce nav[{bench_col!r}]: {chk['bench']}")

    res = it.after_tax_strategy(x["trades"], x["nav"], x["dividends"], x["close_px"],
                                x["end_px"], p, nav_col=nav_col, capital=capital,
                                bonuses=x["bonuses"])
    res.nav.to_csv(out / "after_tax_nav.csv", index=False)
    res.years.to_csv(out / "tax_by_year.csv", index=False)
    res.payments.to_csv(out / "tax_payments.csv", index=False)
    res.pieces.to_csv(out / "lots_realised.csv", index=False)
    res.costs.to_csv(out / "costs_by_trade.csv", index=False)
    it.cost_table(order, p=p).to_csv(out / "cost_per_side.csv", index=False)

    initial = res.final["capital"]
    stats = {"basis": "liquidation_value at every mark: the value after exit costs and the "
                      "tax due if everything were sold at that mark",
             "strategy": _stats(res.nav)}
    bench, bench_navs = {}, {}
    if x["bench"] is not None:
        variants = (("growth", "bench", "growth", None),
                    ("gross_tri", "bench_gross_tri", "growth", 0.0),
                    ("distributed", "bench_distributed", "distributed", None))
        for name, pre, mode, ter in variants:
            bn, by, bf = it.benchmark_after_tax(x["bench"], initial, p, mode=mode, ter=ter)
            bn.to_csv(out / f"{pre}_after_tax_nav.csv", index=False)
            by.to_csv(out / f"{pre}_tax_by_year.csv", index=False)
            bench[name], bench_navs[name] = bf, bn
            stats[f"benchmark_{name}"] = _stats(bn)
        bench["headline"] = "growth"
        bench["labels"] = {
            "growth": "HEADLINE. Growth-option Nifty 500 index fund, bought at the first mark, "
                      "held throughout and sold at the end (A6), NAV = TRI less the expense "
                      "ratio accrued daily; files bench_after_tax_nav.csv, bench_tax_by_year.csv",
            "gross_tri": "The same fund at TER 0: the gross TRI; files bench_gross_tri_*",
            "distributed": "Sensitivity, close to a direct holding of the index: the index's "
                           "dividends paid out and reinvested at each mark, s.115R distribution "
                           "tax on payouts before 1 Apr 2020, slab tax with advance tax from "
                           "1 Apr 2020; files bench_distributed_*"}
        bench["notes"] = {
            "first_nifty500_index_fund": it.FUND_SOURCES["first_fund"],
            "hypothetical_before": str(it.FIRST_NIFTY500_FUND),
            "ter": it.FUND_SOURCES["ter"], "stamp_duty": it.FUND_SOURCES["stamp"],
            "ignored": "redemption STT, exit loads, tracking error beyond the TER, and s.94 "
                       "on the fund's units"}
        g = bench_navs["growth"]
        stats["strategy_vs_benchmark_growth"] = metrics.active(
            res.nav[["date", "mark", "liquidation_value"]].rename(
                columns={"liquidation_value": "nav"}),
            g[["date", "mark", "liquidation_value"]].rename(columns={"liquidation_value": "nav"}))

    sens = []
    for s in slippage_grid:
        q = replace(p, slippage=float(s))
        f = it.after_tax_strategy(x["trades"], x["nav"], x["dividends"], x["close_px"],
                                  x["end_px"], q, nav_col=nav_col, capital=capital,
                                  bonuses=x["bonuses"], series=False).final
        sens.append({"slippage": s, **{k: f[k] for k in (
            "after_tax_end_held", "after_tax_end_liquidated", "tax_paid_during",
            "interest_paid", "tax_due_at_end", "itemised_costs_total", "flat_costs_total")}})
    pd.DataFrame(sens).to_csv(out / "slippage_sensitivity.csv", index=False)
    labels = {"advance": "advance tax: 100% of the year-to-date tax at each instalment, "
                         "s.234B/234C interest, refunds without interest (the headline rule)",
              "advance_minimum": "SENSITIVITY: advance tax at the cumulative statutory share "
                                 "(15/45/75/100%) of the year-to-date tax, the least that meets "
                                 "the s.234C proviso",
              "april": "SENSITIVITY: each year's whole tax paid at the first session of the "
                       "next year, no advance tax and no interest (the pre-fix convention)"}
    timing = []
    for other in (t for t in labels if t != p.tax_timing):
        tt = it.after_tax_strategy(x["trades"], x["nav"], x["dividends"], x["close_px"],
                                   x["end_px"], replace(p, tax_timing=other), nav_col=nav_col,
                                   capital=capital, bonuses=x["bonuses"], series=False).final
        timing.append({"tax_timing": other, "label": labels[other],
                       **{k: tt[k] for k in ("after_tax_end_held", "after_tax_end_liquidated",
                                             "tax_paid_during", "interest_paid", "refunds",
                                             "tax_due_at_end")}})

    sessions = x["nav"]["date"].unique()
    near = it.near_long_term(res.pieces, sessions, within=5, p=p)
    corp = it.corporate_action_exposure(x["corp_unmodelled"], res.pieces)

    summary = {"input": str(inp), "suffix": suffix, "nav_col": nav_col, "bench_col": bench_col,
               "input_provenance": provenance, "input_fingerprint": fingerprint,
               "rebuilt_inputs_provenance": rebuilt_provenance,
               "database": _database_info(any(v == "database" for v in provenance.values())),
               "replay_check": chk, "params": asdict(p),
               "conventions": {
                   "statistics": "every after-tax statistic is computed from liquidation_value",
                   "after_tax_end_held": "end book less the tax already owed on income "
                                         "realised in the open year, net of advance tax paid",
                   "after_tax_end_liquidated": "everything sold at the end mark, exit costs and "
                                               "all tax due deducted",
                   "tax_timing": p.tax_timing,
                   "advance_tax": ADVANCE_TAX_LABELS[p.tax_timing],
                   "bonus_shares": "on the ex-date (allotment-date proxy, slightly "
                                   "taxpayer-favourable): originals keep full cost and date, "
                                   "bonus lot nil cost dated the ex-date; s.94(8) and s.94(7) "
                                   "with the ex-date as the record-date proxy",
                   "rounding": "s.288A/288B to the nearest Rs 10" if p.statutory_rounding
                               else "off"},
               "strategy": res.final, "benchmark": bench, "after_tax_stats": stats,
               "sensitivities": {"tax_timing": timing, "slippage": sens},
               "short_term_within_5_sessions_of_long_term": near,
               "corporate_actions_not_modelled": {
                   "limitation": "Demergers, amalgamations, schemes of arrangement and bonus "
                                 "debentures or preference shares are not reflected in the lot "
                                 "book: the engine shows a demerger's price drop as a loss and "
                                 "force-sells an absorbed company, so the layer books a larger "
                                 "loss on the parent and no cost for the new shares.",
                   **corp},
               "cost_sources": it.COST_SOURCES, "tax_sources": it.TAX_SOURCES,
               "fund_sources": it.FUND_SOURCES}
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    return summary


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    a = parse_args(argv)
    inp = Path(a.inp)
    out = Path(a.out) if a.out else inp / f"after_tax{a.suffix}"
    p = it.Params(slippage=a.slippage, slab=a.slab, tax_timing=a.tax_timing, fund_ter=a.ter)
    grid = tuple(float(s) for s in a.slippage_grid.split(",") if s.strip())
    s = run(inp, out, a.suffix, a.nav_col, a.bench, a.capital, p, a.order, grid,
            a.run_final_test)
    log.info("replay check %s", s["replay_check"])
    log.info("written to %s", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
