"""Leak test of v3's screen at every review (v3 spec, section 15.5).

For each of the 27 reviews D the production screen is built twice, by the code
jobs/backtest_v3.py uses (engine_v3.compute_review):

  1. on the full archive (the frozen database, opened read-only);
  2. on a copy of it physically cut to what was knowable before the open of D,
     by jobs/leak_v1_all_dates.truncated_archive (prices and indices dated
     before D, filings and announcements broadcast before D 00:00, corporate
     actions with an ex-date before D).

Every stock's figures (basis, quarters, TTM sums, market cap, P/E, interest
cover, balance sheet, DOWN0 and DOWN1, each filter F0 to F6 and `eligible`),
every ranking measure and rank, and the funnel counts must be identical: exact
equality, NaN equal to NaN, the same members in the same order.

What is held fixed, as section 3.9 and 15.5 declare: pit.StaticLabels, the
sector keys from industry_nse.parquet and nse_list.json are loaded once from
the frozen inputs and given to both runs. They are outside this test's scope.

Proof that the test can fail: the two leaky variants of jobs/leak_v1_all_dates
(filings broadcast on D itself; prices through D's own session) go through the
same comparison and must be caught wherever their leak changes the screen.

    python -m jobs.leak_v3_all_dates --frozen data/frozen/v3 [--dates D ...] [--out X.json]

Reads no price after D and computes no return. Exit code 1 if any review
differs, or a leaky variant goes uncaught at a review where its leak changes
the screen.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import tempfile
import time
from pathlib import Path

import pandas as pd

from jobs import backtest_v3
from jobs import leak_v1_all_dates as base
from zen.portfolio import engine_v2, engine_v3
from zen.universe import pit

log = logging.getLogger(__name__)

STATIC_NOTE = ("pit.StaticLabels, the sector keys (industry_nse.parquet) and nse_list.json are "
               "loaded once from the frozen inputs and given to both runs, as jobs/backtest_v3.py "
               "does. v3 spec sections 3.9 and 15.5 declare them static and outside this test.")


def _plain(df: pd.DataFrame) -> pd.DataFrame:
    """Nullable columns (a flag that can be unknown) as objects with None for unknown, so
    that the exact comparison treats unknown as equal to unknown."""
    df = df.copy()
    for c in df.columns:
        if pd.api.types.is_extension_array_dtype(df[c].dtype):
            df[c] = df[c].astype(object).where(df[c].notna(), None)
    return df


def screen(con, D, static, keys, tickers) -> dict:
    """engine_v3.compute_review for one review, as three frames."""
    rv = engine_v3.compute_review(con, pd.Timestamp(D), static, keys, tickers)
    figs = rv.figs.reset_index()
    figs = figs.rename(columns={figs.columns[0]: "symbol"})
    ranks = rv.ranks.reset_index()
    ranks = ranks.rename(columns={ranks.columns[0]: "symbol"})
    return {"figs": _plain(figs), "ranks": _plain(ranks), "funnel": pd.DataFrame([rv.funnel])}


def compare_screens(a: dict, b: dict) -> dict:
    f = base.compare_frames(a["figs"], b["figs"], key="symbol")
    r = base.compare_frames(a["ranks"], b["ranks"], key="symbol")
    u = base.compare_frames(a["funnel"], b["funnel"], key=None)
    return {"figs": f, "ranks": r, "funnel": u,
            "identical": bool(f["identical"] and r["identical"] and u["identical"])}


def check_date(con, D, static, keys, tickers, src: Path, tmp_dir: Path | None = None,
               leaky=tuple(base.LEAKY)) -> dict:
    D = pd.Timestamp(D).normalize()
    t0 = time.time()
    rep = {"D": str(D.date())}
    full = screen(con, D, static, keys, tickers)
    with tempfile.TemporaryDirectory(dir=tmp_dir) as tmp:
        path = Path(tmp) / f"truncated_{D.date()}.duckdb"
        info = base.truncated_archive(src, D, path)
        rep["truncated_archive"] = info
        bad = base.check_truncation(info, D)
        if bad:
            raise RuntimeError("truncation failed: " + "; ".join(bad))
        tcon = pit.connect(path, read_only=True)
        try:
            trunc = screen(tcon, D, static, keys, tickers)
            rep["stocks"] = {"full": int(len(full["figs"])), "truncated": int(len(trunc["figs"]))}
            rep["eligible"] = {"full": int(len(full["ranks"])), "truncated": int(len(trunc["ranks"]))}
            rep["honest"] = compare_screens(full, trunc)
            rep["filings_broadcast_on_D"] = base.filings_broadcast_on(con, D)
            rep["leaky"] = {}
            for name in leaky:
                cls = base.LEAKY[name]
                wf, wt = cls(con, D), cls(tcon, D)
                lf, lt = screen(wf, D, static, keys, tickers), screen(wt, D, static, keys, tickers)
                cmp = compare_screens(lf, lt)
                effective = not compare_screens(lf, full)["identical"]
                rep["leaky"][name] = {
                    "caught": not cmp["identical"],
                    "leak_changes_screen": effective,
                    "missed": bool(effective and cmp["identical"]),
                    "rewrites_full": wf.rewrites, "rewrites_truncated": wt.rewrites,
                    "truncated_equals_honest": compare_screens(lt, trunc)["identical"],
                    "differing_columns": sorted(cmp["figs"].get("differing_columns", {})),
                }
        finally:
            tcon.close()
    rep["passed"] = bool(rep["honest"]["identical"])
    rep["seconds"] = round(time.time() - t0, 1)
    return rep


def summarise(rows: list[dict]) -> dict:
    failed = [r["D"] for r in rows if not r["passed"]]
    out = {"dates": len(rows), "passed": len(rows) - len(failed), "failed": failed,
           "leaky_variants": {}}
    for name in base.LEAKY:
        v = [(r["D"], r["leaky"][name]) for r in rows if name in r.get("leaky", {})]
        out["leaky_variants"][name] = {
            "dates": len(v),
            "leak_changes_screen": sum(1 for _, x in v if x["leak_changes_screen"]),
            "caught": sum(1 for _, x in v if x["caught"]),
            "missed": [d for d, x in v if x["missed"]],
            "no_rewrite": [d for d, x in v if x["rewrites_full"] == 0],
            "truncated_not_equal_honest": [d for d, x in v if not x["truncated_equals_honest"]],
        }
    return out


def ok(summary: dict) -> bool:
    if summary["failed"]:
        return False
    for v in summary["leaky_variants"].values():
        if v["missed"] or v["no_rewrite"] or v["truncated_not_equal_honest"]:
            return False
    return True


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--frozen", default=None, help="the freeze directory (spec 15.2.4)")
    ap.add_argument("--dates", nargs="*", default=None, help="only these reviews (YYYY-MM-DD)")
    ap.add_argument("--out", default=None, help="report path")
    ap.add_argument("--tmp", default=None, help="folder for the truncated archive copies")
    a = ap.parse_args(argv)

    paths = backtest_v3.input_paths(a.frozen)
    if a.frozen:
        backtest_v3.verify_frozen(a.frozen)
    con = pit.connect(paths["db"])
    cal = pit.sessions(con)
    static = backtest_v3.static_labels(con, paths["nse_list"])
    keys = engine_v3.sector_keys(engine_v2.nse_sectors(pd.read_parquet(paths["labels"]), static.ids))
    tickers = engine_v2.Tickers(static.ids)
    decisions = pit.decision_dates(cal, first=engine_v3.FIRST_REVIEW, last=engine_v3.LAST_REVIEW)
    if a.dates:
        want = {pd.Timestamp(d).normalize() for d in a.dates}
        decisions = [d for d in decisions if d in want]
    tmp = Path(a.tmp) if a.tmp else None
    if tmp:
        tmp.mkdir(parents=True, exist_ok=True)
    rows = []
    for D in decisions:
        r = check_date(con, D, static, keys, tickers, paths["db"], tmp)
        rows.append(r)
        log.info("%s: %s; stocks %d, eligible %d; leaky caught %s (%.0fs)", r["D"],
                 "identical" if r["passed"] else "DIFFERENT", r["stocks"]["full"],
                 r["eligible"]["full"],
                 {k: v["caught"] for k, v in r["leaky"].items()}, r["seconds"])
    summary = summarise(rows)
    report = {"spec": backtest_v3.spec_info(), "frozen": str(a.frozen) if a.frozen else None,
              "static_note": STATIC_NOTE, "summary": summary, "ok": ok(summary), "dates": rows}
    out = Path(a.out) if a.out else backtest_v3.BACKTEST / "v3_dev" / "leak_all_dates.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    print(json.dumps(summary, indent=1))
    print(f"{'OK' if report['ok'] else 'FAILED'}: {out}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
