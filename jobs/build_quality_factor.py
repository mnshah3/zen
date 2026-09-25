"""Build the A5 quality factor (research/strategy/v2-spec.md, amendment A5).

    python -m jobs.build_quality_factor [--workers 4] [--fresh] [--out DIR] [--cache DIR]

Writes to --out (default data/study):
  quality_factor.parquet             one row per (version, holding month): the
                                     factor, the six portfolio returns, counts
  quality_factor_portfolios.parquet  per formation and stock: market cap,
                                     signals, labels, return
  quality_factor_checks.json         the checks against theory (IIMA
                                     correlations, mean and t, the corner
                                     portfolios by year), also printed

The construction is zen/validation/factors.py's (v2-spec.md, Clarification to
A5) and is not tuned here. The headline versions "margin" and "roce" sort v1's
universe; the "_no_r6" versions are a robustness line. Each formation is cached
(default data/cache/quality_factor) as it is built, so an interrupted run
resumes where it stopped. The cache carries a fingerprint of the database file
(size and modification time) and of the source of factors.py, pit.py and
engine.py; any mismatch rebuilds every formation, so a changed archive or
changed code is never mixed with old formations. --fresh forces a rebuild.

This builds a factor. It does not run, and cannot run, a strategy backtest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from zen.universe import pit  # noqa: E402
from zen.validation import factors as qf  # noqa: E402

log = logging.getLogger("build_quality_factor")

OUT = ROOT / "data" / "study" / "quality_factor.parquet"
OUT_PORT = ROOT / "data" / "study" / "quality_factor_portfolios.parquet"
IIMA = ROOT / "data" / "external" / "iima" / \
    "2025-12_FourFactors_and_Market_Returns_Monthly_SurvivorshipBiasAdjusted.csv"
CACHE = ROOT / "data" / "cache" / "quality_factor"
# every module a formation runs: the point-in-time package, the factor, the engine
FINGERPRINT_SOURCES = sorted((ROOT / "zen" / "universe").glob("*.py")) + [
    ROOT / "zen" / "validation" / "factors.py", ROOT / "zen" / "portfolio" / "engine.py"]
FORMATION_GLOB = "[0-9][0-9][0-9][0-9]-[0-9][0-9].parquet"   # only the job's own files

_static = None
_con = None


def _init(db: str) -> None:
    global _static, _con
    logging.basicConfig(level=logging.WARNING)
    _con = pit.connect(Path(db))
    _static = pit.StaticLabels.load(_con)


def _one(D: str, cache: str) -> str:
    path = Path(cache) / f"{D[:7]}.parquet"
    f = qf.formation(_con, pd.Timestamp(D), _static)
    tmp = path.with_suffix(".tmp")               # atomic: a killed run leaves no half file
    f.to_parquet(tmp, index=False)
    tmp.replace(path)
    return D


def fingerprint(db: Path) -> dict:
    """What the cached formations depend on: the database file and the code."""
    st = Path(db).stat()
    fp = {"db": str(Path(db).resolve()), "db_size": st.st_size, "db_mtime_ns": st.st_mtime_ns}
    for f in FINGERPRINT_SOURCES:
        fp[f.name] = hashlib.sha256(f.read_bytes()).hexdigest()
    return fp


def cache_is_current(cache: Path, fp: dict) -> bool:
    try:
        return json.loads((cache / "fingerprint.json").read_text(encoding="utf-8")) == fp
    except (OSError, ValueError):
        return False


def build_formations(dates, db: Path, workers: int, fresh: bool,
                     cache: Path = CACHE) -> pd.DataFrame:
    cache.mkdir(parents=True, exist_ok=True)
    fp = fingerprint(db)
    if not fresh and not cache_is_current(cache, fp):
        log.warning("formation cache %s does not match the database or code: rebuilding all", cache)
        fresh = True
    if fresh:
        for old in list(cache.glob(FORMATION_GLOB)) + list(cache.glob("*.tmp")):
            old.unlink()
    # The fingerprint describes what the cache is being built from, so it is
    # written before building: an interrupted run then resumes with the
    # formations it finished, and a changed database or code still clears them.
    (cache / "fingerprint.json").write_text(json.dumps(fp, indent=1), encoding="utf-8")
    todo = [d for d in dates if not (cache / f"{d:%Y-%m}.parquet").exists()]
    log.info("formations: %d total, %d to build", len(dates), len(todo))
    if todo:
        t = time.time()
        with ProcessPoolExecutor(max_workers=workers, initializer=_init,
                                 initargs=(str(db),)) as ex:
            futs = [ex.submit(_one, d.strftime("%Y-%m-%d"), str(cache)) for d in todo]
            for i, fu in enumerate(as_completed(futs), 1):
                d = fu.result()
                if i % 10 == 0 or i == len(futs):
                    log.info("  %d/%d built (last %s, %.0fs)", i, len(futs), d, time.time() - t)
    return pd.concat([pd.read_parquet(cache / f"{d:%Y-%m}.parquet") for d in dates],
                     ignore_index=True)


# ---------------------------------------------------------------- checks
def load_iima() -> pd.DataFrame:
    x = pd.read_csv(IIMA, na_values=["NA"])
    x = x.rename(columns={"Date": "month"}).set_index("month")
    return x / 100.0               # percent per month -> decimal; MF is already excess


def tstat(x: pd.Series, nw_lags: int = 3) -> dict:
    import statsmodels.api as sm
    x = x.dropna().astype(float)
    n = len(x)
    plain = float(x.mean() / (x.std(ddof=1) / np.sqrt(n))) if n > 2 else np.nan
    m = sm.OLS(x.to_numpy(), np.ones(n)).fit(cov_type="HAC", cov_kwds={"maxlags": nw_lags})
    return {"n": n, "mean_monthly": float(x.mean()), "sd_monthly": float(x.std(ddof=1)),
            "annualised_mean": float(x.mean() * 12),
            "t_plain": plain, "t_newey_west": float(m.tvalues[0]),
            "share_positive": float((x > 0).mean())}


def regression(y: pd.Series, X: pd.DataFrame, nw_lags: int = 3) -> dict:
    import statsmodels.api as sm
    d = pd.concat([y.rename("y"), X], axis=1).dropna()
    m = sm.OLS(d["y"], sm.add_constant(d[X.columns])).fit(
        cov_type="HAC", cov_kwds={"maxlags": nw_lags})
    return {"n": int(len(d)), "alpha_monthly": float(m.params["const"]),
            "alpha_t": float(m.tvalues["const"]),
            "betas": {k: float(m.params[k]) for k in X.columns},
            "t": {k: float(m.tvalues[k]) for k in X.columns},
            "r2": float(m.rsquared)}


def checks(fac: pd.DataFrame, hold: pd.DataFrame) -> dict:
    iima = load_iima()
    rep: dict = {}
    for v in qf.VERSIONS:
        f = fac[(fac["version"] == v) & fac["complete"]].set_index("month")
        r = {"months": [f.index.min(), f.index.max()], "all": tstat(f["factor"])}
        both = f[["factor"]].join(iima, how="inner")
        r["iima_overlap"] = [both.index.min(), both.index.max(), int(len(both))]
        r["corr_with_iima"] = {k: float(both["factor"].corr(both[k]))
                               for k in ["MF", "SMB", "HML", "WML"]}
        r["on_iima_four"] = regression(both["factor"], both[["MF", "SMB", "HML", "WML"]])
        # corner and middle portfolios: average monthly return, and their market beta
        port = {}
        for p in qf.PORTS:
            pj = f[[p]].join(iima[["MF", "RF"]], how="inner").dropna()
            beta = float(np.polyfit(pj["MF"], pj[p] - pj["RF"], 1)[0]) if len(pj) > 3 else np.nan
            port[p] = {"mean_monthly": float(f[p].mean()), "beta_to_MF": beta,
                       "avg_n": float(f[f"n_{p}"].mean())}
        r["portfolios"] = port
        # calendar-year compounded returns of the six portfolios and the factor
        yr = f.assign(year=f.index.str[:4])
        r["by_year"] = {y: {c: float((1 + g[c]).prod() - 1) for c in qf.PORTS + ["factor"]}
                        for y, g in yr.groupby("year")}
        # does the sort sort? median signal in each portfolio, averaged over formations
        h = hold.dropna(subset=[f"label_{v}"])
        med = h.groupby(["formation", f"label_{v}"])[qf.SIGNAL[v]].median().unstack()
        r["median_signal_by_portfolio"] = {k: float(med[k].mean()) for k in qf.PORTS if k in med}
        r["missing_returns"] = int(f["n_missing_return"].sum())
        r["fallback_series_stock_months"] = int(f["n_fallback_series"].sum())
        rep[v] = r
    if {"margin", "roce"} <= set(fac["version"]):
        w = fac[fac["complete"]].pivot(index="month", columns="version", values="factor").dropna()
        rep["corr_margin_roce"] = {"n": int(len(w)), "corr": float(w["margin"].corr(w["roce"]))}
    return rep


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--fresh", action="store_true", help="rebuild every cached formation")
    ap.add_argument("--db", default=str(pit.DB_PATH))
    ap.add_argument("--cache", default=str(CACHE), help="where each formation is cached")
    ap.add_argument("--out", default=str(OUT.parent),
                    help="folder for the two parquet files and quality_factor_checks.json")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    con = pit.connect(Path(a.db))
    cal = pit.sessions(con)
    dates = qf.formation_sessions(cal)
    cache = Path(a.cache)
    forms = build_formations(dates, Path(a.db), a.workers, a.fresh, cache)
    log.info("formation rows: %d", len(forms))

    ids = pit.StaticLabels.load(con).ids
    panel = qf.build_return_panel(con, dates[0], cal[-1], ids)
    fac, hold = qf.monthly_factor(forms, panel, cal)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    fac.to_parquet(out / OUT.name, index=False)
    hold.to_parquet(out / OUT_PORT.name, index=False)
    log.info("wrote %s (%d rows) and %s (%d rows)", out / OUT.name, len(fac),
             out / OUT_PORT.name, len(hold))
    log.info("dividends dropped by the parse screen: %d", len(panel.dropped_dividends))

    rep = checks(fac, hold)
    rep["dividends_dropped"] = int(len(panel.dropped_dividends))
    v1u = forms[forms["r6_pass"].astype(bool)].groupby("formation").size()
    allu = forms.groupby("formation").size()
    rep["universe_size"] = {
        "v1_universe_mean": float(v1u.mean()), "v1_universe_min": int(v1u.min()),
        "no_r6_mean": float(allu.mean()), "no_r6_min": int(allu.min()),
        "loss_makers_share_of_no_r6": float((~forms["r6_pass"].astype(bool)).mean())}
    rep["fingerprint"] = fingerprint(Path(a.db))
    rp = out / "quality_factor_checks.json"
    rp.write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
    print(json.dumps(rep, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
