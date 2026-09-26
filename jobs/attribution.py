"""Factor attribution and benchmark margins of a backtest run (v2-spec A2, A4, A5).

Generic over a run folder (jobs/backtest_v1.py's layout), so v1 and v2 are
measured by the same code:

    python -m jobs.attribution --run data/backtest/v1_final --label v1

Writes <run>/attribution.json (or --out FILE):

  iima_four_factor   the strategy's monthly returns, built exactly as the IIMA
                     regression in jobs/libcheck_v1.py builds them (last value
                     of each calendar month, the IIMA file decides which months
                     count), regressed on IIMA's market, size, value and
                     momentum factors: statsmodels OLS with Newey-West errors,
                     3 lags; alpha annualised as (1 + monthly alpha)^12 - 1.
  quality            the same regression without and with the A5 quality factor
                     (data/study/quality_factor.parquet, complete months only),
                     on identical months, checked in code: version 'margin'
                     over the whole period, version 'roce' over its months from
                     March 2023; the '_no_r6' versions (loss-makers kept) as a
                     labelled robustness line, never the headline (Clarification
                     to A5).
  benchmarks         for each benchmark column of nav.csv, the strategy's excess
                     CAGR (difference of calendar-day CAGRs) and information
                     ratio (annualised mean of the daily return difference over
                     its annualised standard deviation, 252 periods) over the
                     whole period, computed by zen/portfolio/metrics.py exactly
                     as metrics.json computes them and checked against it; and,
                     for the five factor indices, over each index's live part
                     (Clarification to A4).

Live part of a factor index. The task was to use NSE's published launch date
from an official NSE Indices factsheet or methodology document. On 2026-09-26
niftyindices.com and nseindia.com could not be read from this machine's fetch
tool (every request timed out or failed the TLS handshake), so no launch date
could be quoted from an NSE document. As fixed in advance for that case, the
live part starts at the first date NSE printed an open for the index: Nifty200
Momentum 30 on 12 Oct 2020 and Nifty500 Value 50 on 16 Dec 2024. Quality 30,
Low Volatility 30 and Alpha 50 have printed opens at the clock start, so their
live part is the whole period. Each is labelled with that basis. The live part
is measured from the CLOSE of the first live date, so every value in it was
published while NSE printed a full open-high-low-close for the index.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jobs import libcheck_v1 as lc  # noqa: E402
from zen.portfolio import metrics  # noqa: E402

QUALITY = ROOT / "data" / "study" / "quality_factor.parquet"
ENDPOINTS = ROOT / "data" / "external" / "nifty_price_endpoints.csv"
AGREE_TOL = 1e-9

INDEX_NAMES = {"nifty500": "Nifty 500", "midcap150": "Nifty Midcap 150",
               "smallcap250": "Nifty Smallcap 250", "momentum30": "Nifty200 Momentum 30",
               "value50": "Nifty500 Value 50", "quality30": "Nifty200 Quality 30",
               "lowvol30": "Nifty100 Low Volatility 30", "alpha50": "Nifty Alpha 50"}
# NSE's names in nifty_price_endpoints.csv
ENDPOINT_NAMES = {"momentum30": "NIFTY200 MOMENTUM 30", "value50": "NIFTY500 VALUE 50",
                  "quality30": "NIFTY200 QUALITY 30", "lowvol30": "NIFTY100 LOW VOLATILITY 30",
                  "alpha50": "NIFTY ALPHA 50"}
FACTOR_INDICES = tuple(ENDPOINT_NAMES)

_NO_DOC = ("no launch date could be read from an NSE document: niftyindices.com and "
           "nseindia.com timed out or failed the TLS handshake for every fetch on 2026-09-26")
# col -> (first live date or None for 'on or before the clock start', basis, source)
LIVE_START = {
    "momentum30": ("2020-10-12", "first date NSE printed an open (fallback)",
                   _NO_DOC + "; data/external/SOURCES.md: 'Momentum 30 has opens from 12 Oct "
                   "2020'. A search listed an NSE Indices press release titled 'NSE Indices "
                   "launches Nifty200 Momentum 30 Index' at https://www.niftyindices.com/"
                   "Press_Release/ind_prs25082020.pdf, but its text could not be read, so no "
                   "launch sentence is quoted and its date is not used"),
    "value50": ("2024-12-16", "first date NSE printed an open (fallback)",
                _NO_DOC + "; data/external/SOURCES.md: 'Value 50 from 16 Dec 2024'"),
    "quality30": (None, "NSE printed an open at the clock start (fallback): live over the "
                        "whole period", _NO_DOC + "; open printed on 15 Feb 2019 in "
                        "data/external/nifty_price_endpoints.csv"),
    "lowvol30": (None, "NSE printed an open at the clock start (fallback): live over the "
                       "whole period", _NO_DOC + "; open printed on 15 Feb 2019 in "
                       "data/external/nifty_price_endpoints.csv"),
    "alpha50": (None, "NSE printed an open at the clock start (fallback): live over the "
                      "whole period", _NO_DOC + "; open printed on 15 Feb 2019 in "
                      "data/external/nifty_price_endpoints.csv"),
}
SOURCES_TRIED = [
    "https://www.niftyindices.com/Factsheet/Factsheet_Nifty200_Momentum30.pdf (timeout)",
    "https://www.niftyindices.com/Press_Release/ind_prs25082020.pdf (timeout)",
    "https://niftyindices.com/Methodology/Method_NIFTY_Equity_Indices.pdf (timeout)",
    "https://nsearchives.nseindia.com/content/indices/Method_NIFTY_Equity_Indices.pdf (timeout)",
    "https://www1.nseindia.com/content/indices/Factsheet_NIFTY200_Quality30.pdf (TLS error)",
    "https://www1.nseindia.com/content/indices/Nifty100_LowVolatility30.pdf (TLS error)",
    "https://www.nseindia.com/static/products-services/indices-strategy (timeout)",
    "https://www.niftyindices.com/indices/equity/strategy-indices/nifty200-quality-30 (timeout)",
]


# ---------------------------------------------------------------- quality factor
def quality_factor(version: str, path: Path = QUALITY) -> pd.Series:
    """The A5 factor's monthly return for one version, complete months only."""
    q = pd.read_parquet(path)
    q = q[(q["version"] == version) & q["complete"].astype(bool)]
    if q.empty:
        raise ValueError(f"{path}: no complete months for version {version!r}")
    s = pd.Series(q["factor"].to_numpy(float), index=pd.PeriodIndex(q["month"], freq="M"),
                  name=version).sort_index()
    if not s.index.is_unique:
        raise ValueError(f"{path}: duplicate months for version {version!r}")
    return s


def with_without(monthly: pd.Series, q: pd.Series, iima: pd.DataFrame | None = None) -> dict:
    """Four-factor alpha without and with the quality factor, on identical months.

    Each regression's sample is built on its own and the two are required to
    be the same months, so a month the factor lacks cannot silently leave only
    one of them."""
    d_with = lc.factor_frame(monthly, {"QUAL": q}, iima)
    d_without = lc.factor_frame(monthly, None, iima)
    d_without = d_without[d_without.index.isin(q.index)]
    if not d_with.index.equals(d_without.index):
        raise AssertionError("the regressions with and without the quality factor are not "
                             "on identical months")
    without = lc.ols_nw(d_without, lc.FACTORS)
    with_ = lc.ols_nw(d_with, lc.FACTORS + ["QUAL"])
    return {"months": int(len(d_with)), "first": str(d_with.index[0]),
            "last": str(d_with.index[-1]), "identical_months": True,
            "without_quality": without, "with_quality": with_,
            "alpha_annual_change_pct": round((with_["exact"]["alpha_annual"]
                                              - without["exact"]["alpha_annual"]) * 100, 2)}


# ---------------------------------------------------------------- benchmarks
def nav_frame(clock: pd.DataFrame, col: str) -> pd.DataFrame:
    """A column of the clock as metrics.py's NAV frame (date, mark, nav)."""
    return pd.DataFrame({"date": clock.index.normalize(), "mark": clock["mark"].to_numpy(),
                         "nav": clock[col].to_numpy(float)}).reset_index(drop=True)


def excess(clock: pd.DataFrame, col: str) -> dict:
    """The strategy against one benchmark column over the clock given."""
    a, b = nav_frame(clock, "strategy"), nav_frame(clock, col)
    act = metrics.active(a, b)
    return {"from": f"{clock.index[0].date()} {clock['mark'].iloc[0]}",
            "to": f"{clock.index[-1].date()} {clock['mark'].iloc[-1]}",
            "years": lc.years_between(clock.index[0], clock.index[-1]),
            "n_returns": int(len(clock) - 1),
            "strategy_cagr": metrics.cagr(a), "benchmark_cagr": metrics.cagr(b),
            "excess_cagr": act["cagr_diff"], "information_ratio": act["information_ratio"],
            "tracking_error": act["tracking_error"]}


def endpoint_opens(path: Path = ENDPOINTS) -> pd.DataFrame:
    """NSE's printed opens for the factor indices; NaN where NSE printed '-'."""
    e = pd.read_csv(path, dtype=str)
    o = e["Open"].str.strip()
    return pd.DataFrame({"index_name": e["IndexName"].str.strip(),
                         "date": pd.to_datetime(e["Date"].str.strip(), format="%d %b %Y"),
                         "open": pd.to_numeric(o.where(o != "-"), errors="raise")})


def check_live_basis(first_date: pd.Timestamp, path: Path = ENDPOINTS) -> dict:
    """The fallback's premise from NSE's own prints: no open at the clock start for
    an index given a later live date, an open there for one live throughout."""
    e = endpoint_opens(path)
    out = {}
    for col, (live, _, _) in LIVE_START.items():
        row = e[(e["index_name"] == ENDPOINT_NAMES[col]) & (e["date"] == first_date)]
        if len(row) != 1:
            raise ValueError(f"{path}: no row for {ENDPOINT_NAMES[col]} on {first_date.date()}")
        has_open = bool(row["open"].notna().iloc[0])
        if has_open != (live is None):
            raise AssertionError(f"{col}: NSE {'printed' if has_open else 'did not print'} an "
                                 f"open on {first_date.date()}, contrary to its live start")
        out[col] = {"open_printed_at_clock_start": has_open}
    return out


def live_clock(clock: pd.DataFrame, live: str | None) -> pd.DataFrame | None:
    """The clock from the close of the first live date (the whole clock if none);
    None when the index went live after the run's last close."""
    if live is None or pd.Timestamp(live) <= clock.index[0].normalize():
        return clock
    t0 = pd.Timestamp(live) + lc.CLOSE_AT
    if t0 >= clock.index[-1]:
        return None
    if t0 not in clock.index:
        raise ValueError(f"{live} is not a session close on the run's clock")
    return clock.loc[clock.index >= t0]


def benchmarks(clock: pd.DataFrame, metrics_json: dict | None = None) -> dict:
    out = {}
    vs = (metrics_json or {}).get("vs", {})
    for col in lc.index_columns(clock):
        base = col.replace("_no_overnight", "")
        whole = excess(clock, col)
        if col in vs:
            for k, mk in (("excess_cagr", "cagr_diff"), ("information_ratio", "information_ratio"),
                          ("tracking_error", "tracking_error")):
                if abs(whole[k] - vs[col][mk]) > AGREE_TOL:
                    raise AssertionError(f"{col} {k}: {whole[k]} here, {vs[col][mk]} in metrics.json")
            whole["agrees_with_metrics_json"] = True
        row = {"name": INDEX_NAMES.get(base, base)
                       + (" (no overnight move where NSE printed no open)"
                          if col.endswith("_no_overnight") else ""),
               "whole_period": whole}
        if base in LIVE_START:
            live, basis, source = LIVE_START[base]
            lc_ = live_clock(clock, live)
            meta = {"live_start": live or f"on or before {clock.index[0].date()}",
                    "basis": basis, "source": source}
            row["live_part"] = ({**excess(lc_, col), **meta,
                                 "same_as_whole_period": bool(len(lc_) == len(clock))}
                                if lc_ is not None else
                                {**meta, "note": "not live within this run"})
        out[col] = row
    return out


# ---------------------------------------------------------------- main
def run_attribution(run: Path, label: str, quality_path: Path = QUALITY,
                    endpoints_path: Path = ENDPOINTS) -> dict:
    run = Path(run)
    clock = lc.load_clock(run)
    s = clock["strategy"].dropna()
    monthly = lc.monthly_returns(s)
    iima = lc.load_iima()
    full = lc.ols_nw(lc.factor_frame(monthly, None, iima), lc.FACTORS)

    quality = {}
    for version, label_q in (("margin", "headline: operating margin, whole period"),
                             ("roce", "headline: ROCE, its months from March 2023"),
                             ("margin_no_r6", "robustness only: margin, loss-makers kept"),
                             ("roce_no_r6", "robustness only: ROCE, loss-makers kept")):
        r = with_without(monthly, quality_factor(version, quality_path), iima)
        r["label"] = label_q
        if version.startswith("margin"):
            r["covers_whole_period"] = bool(r["months"] == full["months"]
                                            and r["first"] == full["first"]
                                            and r["last"] == full["last"])
        quality[version] = r

    mj = json.loads((run / "metrics.json").read_text()) if (run / "metrics.json").exists() else None
    return {"run": str(run), "label": label,
            "monthly_returns": {
                "definition": "last NAV value of each calendar month on the run's clock, "
                              "month-on-month change (jobs/libcheck_v1.monthly_returns)",
                "first": str(monthly.index[0]), "last": str(monthly.index[-1]),
                "n": int(len(monthly)),
                "values": {str(k): float(v) for k, v in monthly.items()}},
            "iima_four_factor": full,
            "quality": quality,
            "benchmarks": benchmarks(clock, mj),
            "live_start_check": check_live_basis(clock.index[0].normalize(), endpoints_path),
            "launch_date_sources_tried": SOURCES_TRIED}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default="data/backtest/v1_final", help="backtest output folder")
    ap.add_argument("--label", default=None, help="name for this run (default: folder name)")
    ap.add_argument("--out", default=None, help="output file (default <run>/attribution.json)")
    args = ap.parse_args(argv)
    run = Path(args.run)
    rep = run_attribution(run, args.label or run.name)
    out = Path(args.out) if args.out else run / "attribution.json"
    out.write_text(json.dumps(rep, indent=2))

    f = rep["iima_four_factor"]
    print(f"{rep['label']}: IIMA four-factor alpha {f['alpha_annual_pct']}%/yr, t {f['alpha_t']} "
          f"({f['months']} months {f['first']}..{f['last']})")
    for v, q in rep["quality"].items():
        print(f"  {v:13} {q['months']:3} months {q['first']}..{q['last']}: alpha "
              f"{q['without_quality']['alpha_annual_pct']}% (t {q['without_quality']['alpha_t']}) "
              f"-> {q['with_quality']['alpha_annual_pct']}% (t {q['with_quality']['alpha_t']}), "
              f"quality loading {q['with_quality']['loadings']['QUAL']}")
    for col, b in rep["benchmarks"].items():
        w = b["whole_period"]
        line = (f"  {col:24} whole: excess {w['excess_cagr'] * 100:+.2f}pp, IR "
                f"{w['information_ratio']:.2f}")
        if "live_part" in b:
            lp = b["live_part"]
            line += (f" | live from {lp['from']}: excess {lp['excess_cagr'] * 100:+.2f}pp, "
                     f"IR {lp['information_ratio']:.2f} ({lp['years']:.2f} yrs)")
        print(line)
    print(f"written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
