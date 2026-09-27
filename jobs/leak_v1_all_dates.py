"""Leak test of the v1 ranking at every decision date of the v1 baseline.

For each decision date D in data/backtest/v1_final/metrics.json (31 dates,
Feb 2019 to Aug 2026) the production ranking is built twice, by the code
jobs/backtest_v1.py uses to write ranks.parquet (engine.build_ranks, which
calls zen.signals.composite.compute):

  1. on the full archive (data/zen.duckdb, opened read-only);
  2. on a copy of the archive physically cut to what was knowable before the
     open of D. Every table is cut by its own availability rule (v1 spec,
     "Information set at D"; Clarification 2 for split and bonus factors;
     Clarification 8 for industry labels, which come from announcements):

         prices, prices_other   date < D           (sessions before D)
         indices                date < D
         financials             broadcast_dt < D 00:00
         announcements          an_dt < D 00:00
         corpactions            ex_date < D

     The copy is a separate DuckDB file with only those rows in it, so code
     that ignored D would find nothing later to read. Every table in the
     archive must be in this list: a table that is not cut would be a hole in
     the test, so an unknown table stops the job.

The universe, every measure, every percentile, group score, composite and
rank, the funnel counts and the data-quality audit trail must be identical
(exact equality, NaN equal to NaN, and the same column types). Where the
run's ranks.parquet is present, the full-archive ranking must also equal it,
which shows the ranking tested is the one the backtest used.

What is held fixed. The inputs the spec declares static (pit.StaticLabels:
the first-ever industry label, the lender-taxonomy backfill, company names,
today's NSE list and the stock identity across renames; v1 Clarifications 8,
23, 28, 29 and 33) are loaded once from the full archive and given to both
runs, as production does. They are outside this test's scope by the spec's
own declaration, and the report says so.

Proof that the test can fail. Two deliberately leaky variants of the same
code path are run through the same comparison and must be caught:

  filings_on_D      the financials query reads filings broadcast before
                    D+1 00:00, i.e. also those broadcast on D itself;
  prices_through_D  the price queries read sessions up to and including D,
                    i.e. D's own open and close.

Each wraps the database connection and rewrites only the bound date of the
matching query, so the rest of the code path is the production one. A
variant is caught at D when its full-archive result differs from its
truncated-archive result. The report also counts the rewrites, so a variant
that silently stopped leaking (because a query's text changed) is visible.

    .venv/Scripts/python.exe -m jobs.leak_v1_all_dates
    .venv/Scripts/python.exe -m jobs.leak_v1_all_dates --dates 2019-06-03 2024-06-03 --out X.json

Writes data/backtest/v1_final/leak_all_dates.json (or --out). Exit code 1
if any date fails, if the full-archive ranking differs from ranks.parquet, or
if a leaky variant goes uncaught at a date where its leak changes the ranking.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from zen.portfolio import engine  # noqa: E402
from zen.universe import pit  # noqa: E402

log = logging.getLogger("leak_v1_all_dates")

RUN = ROOT / "data" / "backtest" / "v1_final"
OUT = RUN / "leak_all_dates.json"

# table -> (column, SQL condition with one ? for D, the rule in words).
# Timestamps are cut at D 00:00, dates strictly before D.
CUTS = {
    "prices": ("date", "date < ?", "sessions strictly before D"),
    "prices_other": ("date", "date < ?", "sessions strictly before D"),
    "indices": ("date", "date < ?", "index levels of sessions strictly before D"),
    "financials": ("broadcast_dt", "broadcast_dt < ?", "filings broadcast before D 00:00"),
    "announcements": ("an_dt", "an_dt < ?", "announcements disseminated before D 00:00"),
    "corpactions": ("ex_date", "ex_date < ?", "corporate actions with ex-date strictly before D"),
}
TIMESTAMP_CUTS = {"financials", "announcements"}

STATIC_NOTE = ("pit.StaticLabels is loaded once from the full archive and given to both runs, "
               "as jobs/backtest_v1.py does: the first-ever NSE industry label (v1 "
               "Clarification 8), the lender-taxonomy backfill (28), company names (29), today's "
               "NSE list (diagnostics only, 28) and the stock identity across renames (23, 33). "
               "The spec declares these static and outside the leak test's scope; they feed the "
               "sector cap, the lender flag and the mapping of tickers to stocks, never a price, "
               "a filing figure or a return.")


# ---------------------------------------------------------------- the truncated archive
def _cut_value(table: str, D: pd.Timestamp):
    return D.to_pydatetime() if table in TIMESTAMP_CUTS else D.date()


def archive_tables(con) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main' "
        "ORDER BY 1").fetchall()]


def truncated_archive(src: Path, D, dest: Path) -> dict:
    """Write a DuckDB file at `dest` holding only what was knowable before D.

    Returns, per table, the rows kept, the rows the cut removed (dated on or
    after D, or with no date at all) and the latest value of the cut column,
    which the caller checks is before D. Raises if the archive has a table
    or view this job does not know how to cut."""
    D = pd.Timestamp(D).normalize()
    out = duckdb.connect(str(dest))
    info = {}
    try:
        out.execute(f"ATTACH '{Path(src).as_posix()}' AS src (READ_ONLY)")
        tables = [r[0] for r in out.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_catalog = 'src' AND table_schema = 'main' ORDER BY 1").fetchall()]
        unknown = sorted(set(tables) - set(CUTS))
        if unknown:
            raise RuntimeError(f"the archive has tables this leak test does not cut: {unknown}")
        for t in tables:
            col, cond, _ = CUTS[t]
            v = _cut_value(t, D)
            out.execute(f"CREATE TABLE main.{t} AS SELECT * FROM src.main.{t} WHERE {cond}", [v])
            n, mx = out.execute(f"SELECT count(*), max({col}) FROM main.{t}").fetchone()
            total, undated = out.execute(
                f"SELECT count(*), count(*) FILTER (WHERE {col} IS NULL) FROM src.main.{t}"
            ).fetchone()
            info[t] = {"rows": int(n), "removed": int(total - n), "removed_undated": int(undated),
                       "latest": None if mx is None else str(mx)}
        out.execute("DETACH src")
    finally:
        out.close()
    return info


def check_truncation(info: dict, D) -> list[str]:
    """Every table's latest kept value must be before D (strictly)."""
    D = pd.Timestamp(D).normalize()
    bad = []
    for t, v in info.items():
        if v["latest"] is not None and pd.Timestamp(v["latest"]) >= D:
            bad.append(f"{t}: latest {v['latest']} is not before {D.date()}")
    return bad


# ---------------------------------------------------------------- the ranking (production path)
def ranking(con, D, static) -> dict:
    """engine.build_ranks for one date: the call jobs/backtest_v1.ranks_for makes."""
    ranks, funnel = engine.build_ranks(con, [pd.Timestamp(D)], static)
    dq = ranks.attrs.pop("data_quality", pd.DataFrame())
    return {"ranks": ranks.reset_index(drop=True), "funnel": funnel.reset_index(drop=True),
            "data_quality": dq.reset_index(drop=True)}


# ---------------------------------------------------------------- leaky variants
class _Rewriting:
    """A connection whose matching queries have one bound date moved later.

    Everything else is passed through to the real connection unchanged."""

    name = "rewriting"

    def __init__(self, con, D):
        self._con = con
        self.D = pd.Timestamp(D).normalize()
        self.rewrites = 0

    def _rewrite(self, q: str, params):
        return params

    def execute(self, q, params=None):
        if params is not None:
            new = self._rewrite(q, list(params))
            if new != list(params):
                self.rewrites += 1
            return self._con.execute(q, new)
        return self._con.execute(q)

    def __getattr__(self, name):
        return getattr(self._con, name)


class FilingsOnD(_Rewriting):
    """Reads filings broadcast on D itself: the financials cut moves from
    D 00:00 to D+1 00:00."""

    name = "filings_on_D"

    def _rewrite(self, q, params):
        if "FROM financials" in q and "broadcast_dt < ?" in q:
            cut = self.D.to_pydatetime()
            return [cut + timedelta(days=1) if isinstance(p, datetime) and p == cut else p
                    for p in params]
        return params


class PricesThroughD(_Rewriting):
    """Reads D's own session: every price query bounded by `date < D` becomes
    `date < D+1`."""

    name = "prices_through_D"

    def _rewrite(self, q, params):
        if "FROM prices" in q and "date < ?" in q:
            d = self.D.date()
            return [(self.D + timedelta(days=1)).date() if (not isinstance(p, datetime)
                                                            and p == d) else p
                    for p in params]
        return params


LEAKY = {"filings_on_D": FilingsOnD, "prices_through_D": PricesThroughD}


# ---------------------------------------------------------------- comparison
def _same(a: pd.Series, b: pd.Series) -> np.ndarray:
    """Element-wise exact equality with NaN equal to NaN, whatever the dtype."""
    na, nb = a.isna().to_numpy(), b.isna().to_numpy()
    va, vb = a.astype(object).to_numpy(), b.astype(object).to_numpy()
    eq = np.array([x == y for x, y in zip(va, vb)], dtype=bool)
    return (na & nb) | (~na & ~nb & eq)


def compare_frames(a: pd.DataFrame, b: pd.DataFrame, key: str | None = "symbol",
                   check_dtype: bool = True) -> dict:
    """Exact comparison of two frames. With `key`, rows are matched on it and
    the row order must also agree (the ranks frame is in rank order). With
    `check_dtype`, a column stored under a different type also counts as a
    difference; a frame read back from parquet is compared without it, since
    the round trip stores a text column as pandas' `str` type rather than
    `object`."""
    out = {"rows": [int(len(a)), int(len(b))]}
    if list(a.columns) != list(b.columns):
        out["columns_differ"] = {"only_first": [c for c in a.columns if c not in b.columns],
                                 "only_second": [c for c in b.columns if c not in a.columns]}
        out["identical"] = False
        return out
    dt = {c: [str(a[c].dtype), str(b[c].dtype)] for c in a.columns if a[c].dtype != b[c].dtype}
    if dt:
        out["dtypes_differ"] = dt
    if key is not None:
        ka, kb = list(a[key]), list(b[key])
        sa, sb = set(ka), set(kb)
        out["same_members"] = sa == sb
        out["only_first"] = sorted(sa - sb)[:25]
        out["only_second"] = sorted(sb - sa)[:25]
        out["same_order"] = ka == kb
        if sa != sb:
            out["identical"] = False
            return out
        b = b.set_index(key).loc[ka].reset_index()[a.columns]
    elif len(a) != len(b):
        out["identical"] = False
        return out
    a, b = a.reset_index(drop=True), b.reset_index(drop=True)
    diffs = {}
    for c in a.columns:
        ok = _same(a[c], b[c])
        if ok.all():
            continue
        d = {"rows": int((~ok).sum())}
        if key is not None:
            d["first"] = str(a.loc[~ok, key].iloc[0])
        if pd.api.types.is_numeric_dtype(a[c]) and pd.api.types.is_numeric_dtype(b[c]):
            x, y = a[c].to_numpy(float), b[c].to_numpy(float)
            with np.errstate(invalid="ignore"):
                ad = np.abs(x - y)
            d["max_abs_diff"] = float(np.nanmax(ad)) if np.isfinite(ad).any() else None
        diffs[c] = d
    out["differing_columns"] = diffs
    out["identical"] = (not diffs) and out.get("same_order", True) and not (check_dtype and dt)
    return out


def compare_rankings(full: dict, trunc: dict) -> dict:
    r = compare_frames(full["ranks"], trunc["ranks"], key="symbol")
    f = compare_frames(full["funnel"], trunc["funnel"], key=None)
    q = compare_frames(_dq_sorted(full["data_quality"]), _dq_sorted(trunc["data_quality"]),
                       key=None)
    return {"ranks": r, "funnel": f, "data_quality": q,
            "identical": bool(r["identical"] and f["identical"] and q["identical"])}


def _dq_sorted(dq: pd.DataFrame) -> pd.DataFrame:
    if dq.empty:
        return dq
    cols = [c for c in ("check", "symbol", "consolidated", "period_end") if c in dq]
    return dq.sort_values(cols, kind="stable").reset_index(drop=True)


def committed_ranks(run: Path, D) -> pd.DataFrame | None:
    p = Path(run) / "ranks.parquet"
    if not p.exists():
        return None
    r = pd.read_parquet(p)
    r["D"] = pd.to_datetime(r["D"])
    return r[r["D"] == pd.Timestamp(D)].reset_index(drop=True)


# ---------------------------------------------------------------- one date
def filings_broadcast_on(con, D) -> int:
    D = pd.Timestamp(D).normalize()
    return int(con.execute(
        "SELECT count(*) FROM financials WHERE broadcast_dt >= ? AND broadcast_dt < ?",
        [D.to_pydatetime(), (D + timedelta(days=1)).to_pydatetime()]).fetchone()[0])


def check_date(con, D, static, src: Path = pit.DB_PATH, run: Path = RUN,
               tmp_dir: Path | None = None, leaky=tuple(LEAKY)) -> dict:
    """The honest ranking on the full and the truncated archive, and each
    leaky variant through the same comparison. `con` is the full archive."""
    D = pd.Timestamp(D).normalize()
    t0 = time.time()
    rep = {"D": str(D.date())}
    full = ranking(con, D, static)
    committed = committed_ranks(run, D)
    if committed is not None:
        c = compare_frames(full["ranks"], committed, key="symbol", check_dtype=False)
        rep["full_archive_reproduces_committed_ranks"] = {
            "identical": c["identical"], "rows": c["rows"],
            "differing_columns": c.get("differing_columns", {}),
            "storage_types_differ": c.get("dtypes_differ", {}),
            "only_rebuilt": c.get("only_first", []), "only_committed": c.get("only_second", [])}
    with tempfile.TemporaryDirectory(dir=tmp_dir) as tmp:
        path = Path(tmp) / f"truncated_{D.date()}.duckdb"
        info = truncated_archive(src, D, path)
        rep["truncated_archive"] = info
        bad = check_truncation(info, D)
        if bad:
            raise RuntimeError("truncation failed: " + "; ".join(bad))
        tcon = pit.connect(path, read_only=True)
        try:
            trunc = ranking(tcon, D, static)
            rep["universe"] = {"full": int(len(full["ranks"])),
                               "truncated": int(len(trunc["ranks"]))}
            rep["honest"] = compare_rankings(full, trunc)
            rep["leaky"] = {}
            rep["filings_broadcast_on_D"] = filings_broadcast_on(con, D)
            for name in leaky:
                cls = LEAKY[name]
                wf, wt = cls(con, D), cls(tcon, D)
                lf, lt = ranking(wf, D, static), ranking(wt, D, static)
                cmp = compare_rankings(lf, lt)
                # Did the leak change anything at D? If the extra rows it reads
                # do not move the ranking, there is nothing for the test to see.
                effective = not compare_rankings(lf, full)["identical"]
                # the variant on the truncated archive has nothing on D to read,
                # so it must equal the honest ranking there
                same_trunc = compare_rankings(lt, trunc)["identical"]
                rep["leaky"][name] = {
                    "caught": not cmp["identical"],
                    "leak_changes_ranking": effective,
                    "missed": bool(effective and cmp["identical"]),
                    "rewrites_full": wf.rewrites, "rewrites_truncated": wt.rewrites,
                    "truncated_equals_honest": same_trunc,
                    "universe_full": int(len(lf["ranks"])),
                    "members_changed": (len(cmp["ranks"].get("only_first", [])) +
                                        len(cmp["ranks"].get("only_second", []))),
                    "differing_columns": sorted(cmp["ranks"].get("differing_columns", {})),
                    "funnel_identical": cmp["funnel"]["identical"],
                }
        finally:
            tcon.close()
    rep["passed"] = bool(rep["honest"]["identical"])
    rep["seconds"] = round(time.time() - t0, 1)
    return rep


# ---------------------------------------------------------------- main
def decision_dates(run: Path = RUN) -> list[pd.Timestamp]:
    m = json.loads((Path(run) / "metrics.json").read_text())
    return [pd.Timestamp(d) for d in m["decision_dates"]]


def _git_head() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                              text=True, check=True).stdout.strip()
    except Exception:
        return None


def summarise(rows: list[dict]) -> dict:
    s = {"dates": len(rows), "passed": sum(r["passed"] for r in rows),
         "failed": [r["D"] for r in rows if not r["passed"]]}
    rc = [r for r in rows if "full_archive_reproduces_committed_ranks" in r]
    s["full_archive_reproduces_committed_ranks"] = {
        "dates": len(rc),
        "identical": sum(r["full_archive_reproduces_committed_ranks"]["identical"] for r in rc),
        "differ": [r["D"] for r in rc if not r["full_archive_reproduces_committed_ranks"]["identical"]]}
    lk = {}
    for name in LEAKY:
        got = [(r["D"], r["leaky"][name]) for r in rows if name in r.get("leaky", {})]
        lk[name] = {"dates": len(got),
                    "leak_changes_ranking": sum(g["leak_changes_ranking"] for _, g in got),
                    "caught": sum(g["caught"] for _, g in got),
                    "missed": [d for d, g in got if g["missed"]],
                    "nothing_to_catch": [d for d, g in got if not g["leak_changes_ranking"]],
                    "no_rewrite": [d for d, g in got if g["rewrites_full"] == 0],
                    "truncated_not_equal_honest": [d for d, g in got
                                                   if not g["truncated_equals_honest"]]}
    s["leaky_variants"] = lk
    return s


def ok(summary: dict) -> bool:
    """Every date identical; the full-archive ranking equal to the committed
    ranks.parquet wherever that file is present; every leak that changed the
    ranking caught; every variant actually rewrote its query and read nothing
    extra when truncated."""
    return (not summary["failed"] and
            not summary["full_archive_reproduces_committed_ranks"]["differ"] and
            all(not v["missed"] and not v["no_rewrite"] and not v["truncated_not_equal_honest"]
                for v in summary["leaky_variants"].values()))


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("zen.portfolio.engine").setLevel(logging.WARNING)
    ap = argparse.ArgumentParser()
    ap.add_argument("--dates", nargs="*", default=None,
                    help="decision dates to test (default: all in the run's metrics.json)")
    ap.add_argument("--run", default=str(RUN))
    ap.add_argument("--out", default=None, help="report path (default <run>/leak_all_dates.json)")
    ap.add_argument("--tmp", default=None, help="folder for the truncated archive copies")
    args = ap.parse_args(argv)
    run = Path(args.run)
    all_dates = decision_dates(run)
    dates = [pd.Timestamp(d) for d in args.dates] if args.dates else all_dates
    missing = [str(d.date()) for d in dates if d not in all_dates]
    if missing:
        raise SystemExit(f"not decision dates of {run}: {missing}")

    con = pit.connect(read_only=True)
    static = pit.StaticLabels.load(con)
    rows = []
    for D in dates:
        r = check_date(con, D, static, run=run, tmp_dir=Path(args.tmp) if args.tmp else None)
        rows.append(r)
        lk = ", ".join(f"{k} " + ("caught" if v["caught"] else
                                  "MISSED" if v["missed"] else "changes nothing here")
                       for k, v in r["leaky"].items())
        log.info("%s: universe %d, honest %s; %s (%.0fs)", r["D"], r["universe"]["full"],
                 "identical" if r["passed"] else "DIFFERS", lk, r["seconds"])
    db = pit.DB_PATH.stat()
    rep = {
        "test": "v1 ranking, full archive against an archive truncated before each decision date",
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_head": _git_head(),
        "database": {"path": "data/zen.duckdb", "bytes": db.st_size,
                     "modified_utc": datetime.fromtimestamp(db.st_mtime, timezone.utc)
                     .isoformat(timespec="seconds"), "opened": "read-only"},
        "code_path": "jobs/backtest_v1.ranks_for -> zen.portfolio.engine.build_ranks -> "
                     "zen.signals.composite.compute (build_snapshot, universe, measures, score)",
        "truncation_rules": {t: rule for t, (_, _, rule) in CUTS.items()},
        "compared": "every column of the ranks frame (universe, measures, percentiles, group "
                    "scores, composite, rank, market cap, sector, fundamentals), the funnel and "
                    "the data-quality audit trail; exact equality, NaN equal to NaN, and the "
                    "same column types",
        "held_fixed": STATIC_NOTE,
        "leaky_variants": {k: v.__doc__.strip().replace("\n", " ") for k, v in LEAKY.items()},
        "summary": summarise(rows),
        "dates": rows,
    }
    out = Path(args.out) if args.out else run / "leak_all_dates.json"
    out.write_text(json.dumps(rep, indent=2, default=str) + "\n", encoding="utf-8")
    s = rep["summary"]
    print(f"\n{s['passed']}/{s['dates']} dates identical; failed: {s['failed'] or 'none'}")
    for k, v in s["leaky_variants"].items():
        print(f"{k}: changed the ranking at {v['leak_changes_ranking']}/{v['dates']} dates, "
              f"caught at {v['caught']}; missed: {v['missed'] or 'none'}")
    print(f"written to {out}")
    return 0 if ok(s) else 1


if __name__ == "__main__":
    raise SystemExit(main())
