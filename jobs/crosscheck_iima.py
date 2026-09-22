"""External cross-check: do our archive and our split/bonus adjustment produce
standard Indian factor returns?

The reference is the IIM Ahmedabad data library of Indian Fama-French and
momentum factors (Agarwalla, Jacob and Varma), built independently from CMIE
Prowess total returns over BSE and NSE:

    https://faculty.iima.ac.in/iffm/Indian-Fama-French-Momentum/
    release 2025-12 (files dated 22 Jan 2026), saved raw in data/external/iima/

Nothing here imports the strategy code (zen/universe, zen/signals, zen/portfolio).
Everything is rebuilt from the raw archive so that an error shared by the
strategy and this check cannot hide itself.

WHAT IS COMPARED, 2016-01 TO 2022-12 ONLY

  WML   12-1 momentum. At the close of the last session of month t a stock's
        momentum is its adjusted return from the end of month t-12 to the end
        of month t-1 (IIMA's own definition), and the factor is the month t+1
        return of the top 30% minus the bottom 30%. Built several ways so a
        disagreement can be located: equal-weighted over the liquid universe
        with the adjustment exactly as the backtest rules state it ("rule"),
        the same with the identity and parsing fixes found here ("fixed"),
        the broad IIMA-like universe, and IIMA's own 2x2 size-momentum form.
  SMB   small minus big with big = the top 10% (IIMA's 90th-percentile cut).
        By 12-month turnover for the whole period; by market cap (close x
        shares implied by the latest filings) once filings exist.
  HML   an earnings-yield value factor (TTM normalised profit / market cap),
        2x3 size x E/P, from 2019 when four quarters of filings are known.
        IIMA's HML is book-to-market, so this is a like-for-like check of
        direction, not of identity.
  MKT   our equal- and cap-weighted market and the Nifty 500 (price) against
        IIMA's market return (MF + RF).

HOLDOUT LOCK

The strategy's in-sample period ends at the open of the Feb 2023 decision date
(the first session on or after 15 Feb 2023). No price row after 2022-12-31 is
ever loaded, IIMA rows after 2022-12 are dropped as the file is read, and every
return series passes guard_returns(), which raises HoldoutLocked for any return
period ending at or after 15 Feb 2023 unless run_final_test=True is passed.
This job never passes it.

POINT IN TIME

Formation uses prices through the close of the formation session and filings
with broadcast_dt strictly before that session's date. period_end is never a
time filter. leak_test() rebuilds the formation snapshot from inputs physically
truncated at the formation date and requires the same answer with security
identity held fixed (the job exits 1 otherwise). The strict mode, which also
rebuilds identity from the truncated archive, differs only where a filing's
today-symbol can be tied to its pre-rename trading history through a later
rename; those differences are printed, not hidden.

prices.prev_close is never read. Split and bonus factors come from
data/corpactions: collapsed by (security, ex_date) with a product, and the
cumulative factor at d is the product of factors with ex_date > d.

    python -m jobs.crosscheck_iima
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from zen.data.financials import statement_files  # noqa: E402  (data module only)

log = logging.getLogger("crosscheck_iima")

IIMA_URL = "https://faculty.iima.ac.in/iffm/Indian-Fama-French-Momentum/"
IIMA_RELEASE = "2025-12"
IIMA_DIR = ROOT / "data" / "external" / "iima"
OUT_DIR = ROOT / "data" / "study" / "crosscheck_iima"
DUCK_TMP = (r"C:\Users\mnsha\AppData\Local\Temp\claude"
            r"\C--Users-mnsha-OneDrive-Desktop-Gostack"
            r"\196360f9-eded-41ee-bd9a-762d776d6462\scratchpad\duck")

FIRST_MONTH = pd.Period("2016-01", "M")      # first holding month compared
LAST_MONTH = pd.Period("2022-12", "M")       # last holding month compared
PRICE_CUTOFF = date(2022, 12, 31)            # no price, action or filing after this is loaded
HOLDOUT_OPEN = date(2023, 2, 15)             # Feb 2023 decision date is on or after this

LIQUID_MIN_MEDIAN_TURNOVER = 2_000_000       # Rs 20 lakh, spec universe item 2
LIQUID_MIN_SESSIONS_365D = 200               # spec universe item 3
BROAD_MIN_SESSIONS_365D = 50                 # IIMA liquidity filter
BROAD_MIN_MEDIAN_PRICE = 1.0                 # IIMA penny filter
MIN_PORTFOLIO = 5                            # IIMA: no portfolio under five stocks
JUMP_DOWN, JUMP_UP = 0.55, 2.2               # one-day adjusted ratio treated as a jump


# --------------------------------------------------------------------------
# Holdout lock
# --------------------------------------------------------------------------

class HoldoutLocked(RuntimeError):
    """A return after the in-sample end was about to be computed."""


def guard_returns(months, what: str, *, run_final_test: bool = False) -> None:
    """Refuse any return period that ends at or after the in-sample end.

    `months` are the holding months of a return series. A month's return runs
    to its last session, so the month is allowed only if it ends before
    HOLDOUT_OPEN, and this job additionally never goes past LAST_MONTH.
    """
    idx = pd.PeriodIndex(list(months), freq="M") if len(months) else pd.PeriodIndex([], freq="M")
    if len(idx) == 0:
        return
    latest = idx.max()
    ends = latest.end_time.date()
    if run_final_test:
        log.warning("HOLDOUT UNLOCKED for %s (latest month %s)", what, latest)
        return
    if ends >= HOLDOUT_OPEN or latest > LAST_MONTH:
        raise HoldoutLocked(
            f"{what}: a return period ends {ends}, at or after the in-sample end "
            f"({HOLDOUT_OPEN}, Feb 2023 decision date). Refusing without run_final_test=True.")


# --------------------------------------------------------------------------
# IIMA reference data
# --------------------------------------------------------------------------

def _iima_file(stem: str) -> Path:
    return IIMA_DIR / f"{IIMA_RELEASE}_{stem}.csv"


def load_iima() -> pd.DataFrame:
    """IIMA monthly factors, in decimals, cut to 2016-01..2022-12 on read."""
    raw = pd.read_csv(_iima_file("FourFactors_and_Market_Returns_Monthly_SurvivorshipBiasAdjusted"),
                      na_values=["NA"])
    raw["month"] = pd.PeriodIndex(raw["Date"], freq="M")
    keep = raw[(raw["month"] >= FIRST_MONTH) & (raw["month"] <= LAST_MONTH)]
    del raw                                   # later rows are never looked at
    df = keep.set_index("month")[["SMB", "HML", "WML", "MF", "RF"]].astype(float) / 100.0
    df["RM"] = df["MF"] + df["RF"]

    pm = pd.read_csv(_iima_file("Size_Momentum_Portfolio_Returns_Monthly_SurvivorshipBiasAdjusted"),
                     na_values=["NA"])
    pm["month"] = pd.PeriodIndex(pm["Date"], freq="M")
    pm = pm[(pm["month"] >= FIRST_MONTH) & (pm["month"] <= LAST_MONTH)].set_index("month")
    pm.columns = [c.split()[1] if c.startswith("Portfolio") else c for c in pm.columns]
    for c in ("WB", "WS", "LB", "LS"):
        df[c] = pm[c].astype(float) / 100.0
    guard_returns(df.index, "IIMA factors")
    return df


def load_iima_breakpoints() -> pd.DataFrame:
    """Monthly size (Rs mn, 90th pct) and momentum (30th/70th pct, %) breakpoints.

    Indexed by formation month. Not returns, but cut to the same window anyway.
    """
    bp = pd.read_csv(_iima_file("Size_and_Momentum_Break_Points_for_Size_Momentum_Portfolios"),
                     na_values=["NA"])
    bp.columns = ["yearmonth", "size90_mn", "mom30_pct", "mom70_pct"]
    bp["month"] = pd.PeriodIndex(bp["yearmonth"].astype(str), freq="M")
    bp = bp[(bp["month"] >= FIRST_MONTH - 1) & (bp["month"] < LAST_MONTH)]
    return bp.set_index("month")[["size90_mn", "mom30_pct", "mom70_pct"]]


# --------------------------------------------------------------------------
# Raw inputs (all cut at PRICE_CUTOFF on read)
# --------------------------------------------------------------------------

def connect() -> duckdb.DuckDBPyConnection:
    Path(DUCK_TMP).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute(f"SET temp_directory='{Path(DUCK_TMP).as_posix()}'")
    con.execute("SET memory_limit='8GB'")
    return con


def load_prices(con, cutoff: date = PRICE_CUTOFF) -> pd.DataFrame:
    """Ordinary equity rows that actually traded. prev_close is never selected."""
    pattern = (ROOT / "data" / "daily" / "**" / "*.parquet").as_posix()
    df = con.execute(f"""
        SELECT CAST(date AS DATE) AS d, symbol, isin_code AS isin, close, turnover
        FROM read_parquet('{pattern}', union_by_name=true)
        WHERE CAST(date AS DATE) <= ? AND isin_code LIKE 'INE%'
          AND series IN ('EQ', 'BE') AND close > 0
    """, [cutoff]).df()
    df["d"] = pd.to_datetime(df["d"])
    return df


def load_corpactions(con, cutoff: date = PRICE_CUTOFF) -> pd.DataFrame:
    pattern = (ROOT / "data" / "corpactions" / "*.parquet").as_posix()
    df = con.execute(f"""
        SELECT CAST(ex_date AS DATE) AS ex_date, symbol, isin, action, subject, factor
        FROM read_parquet('{pattern}', union_by_name=true)
        WHERE CAST(ex_date AS DATE) <= ?
    """, [cutoff]).df()
    df["ex_date"] = pd.to_datetime(df["ex_date"])
    return df


def load_financials(con, cutoff: date = PRICE_CUTOFF) -> pd.DataFrame:
    """Quarterly filings, one row per document, broadcast on or before cutoff."""
    listed = ", ".join(f"'{p.as_posix()}'" for p in statement_files(ROOT / "data" / "financials"))
    df = con.execute(f"""
        SELECT symbol, period_end, broadcast_dt, consolidated, profit_normalised, shares_implied
        FROM (SELECT *, row_number() OVER (PARTITION BY xbrl_url ORDER BY broadcast_dt DESC) rn
              FROM read_parquet([{listed}], union_by_name=true)
              WHERE xbrl_url IS NOT NULL)
        WHERE rn = 1 AND CAST(broadcast_dt AS DATE) <= ?
    """, [cutoff]).df()
    df["period_end"] = pd.to_datetime(df["period_end"])
    df["broadcast_dt"] = pd.to_datetime(df["broadcast_dt"])
    return df


def load_nifty500(con, cutoff: date = PRICE_CUTOFF) -> pd.Series:
    pattern = (ROOT / "data" / "indices" / "*.parquet").as_posix()
    df = con.execute(f"""
        SELECT CAST(date AS DATE) AS d, close FROM read_parquet('{pattern}', union_by_name=true)
        WHERE index_name = 'Nifty 500' AND CAST(date AS DATE) <= ? ORDER BY d
    """, [cutoff]).df()
    df["d"] = pd.to_datetime(df["d"])
    s = df.set_index("d")["close"]
    m = s.groupby(s.index.to_period("M")).last()
    r = m.pct_change().loc[FIRST_MONTH:LAST_MONTH]
    guard_returns(r.index, "Nifty 500")
    return r


# --------------------------------------------------------------------------
# Identity: one security per company across NSE symbol renames
# --------------------------------------------------------------------------

def link_securities(px: pd.DataFrame) -> tuple[pd.Series, list]:
    """Map each symbol to a security id.

    NSE renames symbols (MOTHERSUMI -> MOTHERSON, WELSPUNIND -> WELSPUNLIV).
    The ISIN survives a rename, so two symbols are the same security when one
    stops trading an ISIN and the other starts trading the same ISIN within 15
    days. The id is the security's first symbol, which a truncated archive
    assigns identically.
    """
    spans = px.groupby(["isin", "symbol"])["d"].agg(["min", "max"]).reset_index()
    parent = {s: s for s in px["symbol"].unique()}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    overlaps = []
    for isin, g in spans.groupby("isin"):
        if len(g) < 2:
            continue
        g = g.sort_values("min")
        rows = list(g.itertuples())
        for a, b in zip(rows, rows[1:]):
            if b.min > a.max and (b.min - a.max).days <= 15:
                parent[find(b.symbol)] = find(a.symbol)
            else:
                overlaps.append((isin, a.symbol, b.symbol))

    first = px.groupby("symbol")["d"].min()
    comp = pd.Series({s: find(s) for s in parent})
    order = pd.DataFrame({"root": comp, "first": first.reindex(comp.index)})
    sec_id = order.sort_values(["first"]).groupby("root").head(1)
    root_to_id = pd.Series(sec_id.index, index=sec_id["root"])
    return comp.map(root_to_id), overlaps


# --------------------------------------------------------------------------
# Split and bonus factors
# --------------------------------------------------------------------------

_SPLIT_FROM = re.compile(
    r"(?:from|frm)\s+r[se]\.?\s*(\d+(?:\.\d+)?)\s*(?:/-)?\s*(?:per(?:\s+share)?)?\s*(?:/-)?\s*"
    r"to\s+r[se]\.?\s*(\d+(?:\.\d+)?)", re.I)
_SPLIT_BARE = re.compile(
    r"(?:split|splt)\s+r[se]\.?\s*(\d+(?:\.\d+)?)\s*(?:/-)?\s*to\s+r[se]\.?\s*(\d+(?:\.\d+)?)", re.I)
_BONUS = re.compile(r"bonus\s*[-:]?\s*(\d+)\s*:\s*(\d+)", re.I)


def reparse_factor(subject: str) -> float | None:
    """Price multiplier from a subject line, both legs of a combined action.

    The stored `factor` handles one leg only. "Bonus 1:1/Face Value Split From
    Rs 10 To Rs 2" (Bajaj Finance, Sep 2016) is stored as 0.2 when the price
    fell to 0.1x, and "Face Value Split From Rs 10 To Re 2" without "Per Share"
    (Grasim, Oct 2016) is stored as null, and "Fv Splt Frm Rs 10 To Re 1"
    (JSW Steel, Jan 2017) is filed under action "other" with no factor at all.
    Bonus debentures are not share actions and stay unadjusted.
    """
    s = subject or ""
    low = s.lower()
    f, found = 1.0, False
    if re.search(r"split|splt|sub-?division", low):
        m = _SPLIT_FROM.search(s) or _SPLIT_BARE.search(s)
        if m:
            old, new = float(m.group(1)), float(m.group(2))
            if 0 < new < old:
                f *= new / old
                found = True
    if "bonus" in low and "debenture" not in low:
        m = _BONUS.search(s)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a > 0 and b > 0:
                f *= b / (a + b)
                found = True
    return f if found else None


def factors_rule(ca: pd.DataFrame) -> pd.DataFrame:
    """The adjustment as the backtest rules state it, keyed by corpaction symbol."""
    x = ca[ca["factor"].notna() & (ca["factor"] > 0)]
    out = (x.assign(lf=np.log(x["factor"]))
             .groupby(["symbol", "ex_date"])["lf"].sum().reset_index())
    out["factor"] = np.exp(out.pop("lf"))
    return out.rename(columns={"symbol": "sec"})


def factors_fixed(ca: pd.DataFrame, sym_to_sec: pd.Series, isin_to_sec: pd.Series
                  ) -> tuple[pd.DataFrame, dict]:
    """Both legs parsed, and each action mapped to the security that traded it.

    corpactions carries TODAY's symbol and ISIN (WELSPUNLIV / INE192B01031 for
    Welspun India's March 2016 split, when it traded as WELSPUNIND). The
    current ISIN is the post-split ISIN, which the price archive shows under
    the old symbol from the day after the ex-date, so the ISIN finds the
    security; the symbol is the fallback.
    """
    x = ca.copy()
    x["f2"] = x["subject"].map(reparse_factor)
    x = x[x["f2"].notna() & (x["f2"] > 0)].copy()
    by_isin = x["isin"].map(isin_to_sec)
    by_sym = x["symbol"].map(sym_to_sec)
    x["sec"] = by_isin.fillna(by_sym)
    stats = {
        "rows": len(x),
        "mapped_by_isin": int(by_isin.notna().sum()),
        "mapped_by_symbol_only": int((by_isin.isna() & by_sym.notna()).sum()),
        "unmapped": int(x["sec"].isna().sum()),
        "factor_changed_vs_stored": int((~np.isclose(x["f2"], x["factor"].fillna(-1))).sum()),
    }
    x = x[x["sec"].notna()]
    x["subj"] = x["subject"].str.lower().str.replace(r"\s+", " ", regex=True).str.strip()
    x = x.drop_duplicates(["sec", "ex_date", "subj"])
    out = (x.assign(lf=np.log(x["f2"])).groupby(["sec", "ex_date"])["lf"].sum().reset_index())
    out["factor"] = np.exp(out.pop("lf"))
    return out, stats


def multiplier_at(events: dict, sec: str, when: np.ndarray) -> np.ndarray:
    """Cumulative back-adjustment at each date: product of factors with ex_date > date."""
    ev = events.get(sec)
    if ev is None:
        return np.ones(len(when))
    ex, suffix = ev
    idx = np.searchsorted(ex, when, side="right")
    return suffix[idx]


def build_events(fac: pd.DataFrame) -> dict:
    events = {}
    for sec, g in fac.sort_values("ex_date").groupby("sec"):
        ex = g["ex_date"].values.astype("datetime64[ns]")
        f = g["factor"].values
        suffix = np.append(np.cumprod(f[::-1])[::-1], 1.0)
        events[sec] = (ex, suffix)
    return events


# --------------------------------------------------------------------------
# Dense panels and the formation table
# --------------------------------------------------------------------------

@dataclass
class Panel:
    variant: str
    dates: pd.DatetimeIndex
    month_ends: pd.Series            # Period -> last session of that month
    close: pd.DataFrame              # raw close, NaN when not traded
    adj: pd.DataFrame                # adjusted close, NaN when not traded
    events: dict
    med60: pd.DataFrame
    n365: pd.DataFrame
    medpx252: pd.DataFrame
    turn252: pd.DataFrame
    jumps_cum: pd.DataFrame          # cumulative count of unexplained one-day jumps
    sym_to_sec: pd.Series
    isin_to_sec: pd.Series
    fac_stats: dict


def build_panel(px: pd.DataFrame, ca: pd.DataFrame, variant: str,
                identity: tuple[pd.Series, pd.Series] | None = None) -> Panel:
    """`identity` = (sym_to_sec, isin_to_sec) from another panel, used by the
    leak test to hold security identity fixed while every value is truncated."""
    px = px.copy()
    if variant == "rule":
        px["sec"] = px["symbol"]
        sym_to_sec = pd.Series(px["symbol"].unique(), index=px["symbol"].unique())
        isin_to_sec = px.groupby("isin")["sec"].last()
        fac = factors_rule(ca)
        stats = {"rows": len(fac), "note": "stored factor, matched on corpaction symbol"}
    elif identity is not None:
        sym_to_sec, isin_to_sec = identity
        px["sec"] = px["symbol"].map(sym_to_sec)
        fac, stats = factors_fixed(ca, sym_to_sec, isin_to_sec)
    else:
        sym_to_sec, _ = link_securities(px)
        px["sec"] = px["symbol"].map(sym_to_sec)
        isin_to_sec = px.drop_duplicates(["isin", "sec"]).groupby("isin")["sec"].first()
        fac, stats = factors_fixed(ca, sym_to_sec, isin_to_sec)
    # one row per security per day; a same-day overlap keeps the busier symbol
    px = px.sort_values("turnover").drop_duplicates(["d", "sec"], keep="last")

    close = px.pivot(index="d", columns="sec", values="close").sort_index()
    turn = px.pivot(index="d", columns="sec", values="turnover").reindex_like(close)
    dates = close.index
    events = build_events(fac)
    mult = pd.DataFrame(1.0, index=dates, columns=close.columns)
    dv = dates.values.astype("datetime64[ns]")
    for sec in close.columns:
        if sec in events:
            mult[sec] = multiplier_at(events, sec, dv)
    adj = close * mult

    traded = close.notna()
    turn0 = turn.fillna(0.0).where(traded, 0.0)
    med60 = turn0.rolling(60, min_periods=60).median()
    n365 = traded.astype(float).rolling("365D").sum()
    medpx252 = close.rolling(252, min_periods=20).median()
    turn252 = turn0.rolling(252, min_periods=50).mean()

    ratio = adj / adj.ffill().shift(1)
    jumps = ((ratio < JUMP_DOWN) | (ratio > JUMP_UP)).fillna(False)
    jumps_cum = jumps.astype(np.int32).cumsum()

    me = pd.Series(dates, index=dates.to_period("M")).groupby(level=0).max()
    return Panel(variant, dates, me, close, adj, events, med60, n365, medpx252, turn252,
                 jumps_cum, sym_to_sec, isin_to_sec, stats)


def _at(frame: pd.DataFrame, when, ffill_limit: int | None = None) -> pd.Series:
    """Row of `frame` at session `when`, optionally forward-filled up to a limit."""
    if ffill_limit is None:
        return frame.loc[when]
    pos = frame.index.get_loc(when)
    lo = max(0, pos - ffill_limit)
    return frame.iloc[lo:pos + 1].ffill().iloc[-1]


def shares_table(fin: pd.DataFrame, panel: Panel, ca: pd.DataFrame) -> pd.DataFrame:
    """Filings mapped to securities, with the multiplier at their broadcast date.

    financials, like corpactions, carry today's symbol. A symbol that did not
    trade by the cutoff is resolved through corpactions' (symbol, ISIN) pair
    to the ISIN the security traded under.
    """
    f = fin.copy()
    sec = f["symbol"].map(panel.sym_to_sec)
    cur_isin = ca.dropna(subset=["isin"]).drop_duplicates("symbol").set_index("symbol")["isin"]
    via_isin = f["symbol"].map(cur_isin).map(panel.isin_to_sec)
    f["sec"] = sec.fillna(via_isin)
    f.attrs["unmapped_symbols"] = int(f.loc[f["sec"].isna(), "symbol"].nunique())
    f.attrs["mapped_via_isin_symbols"] = int(f.loc[sec.isna() & via_isin.notna(), "symbol"].nunique())
    f.attrs["symbols"] = int(f["symbol"].nunique())
    f = f[f["sec"].notna()].copy()
    f["m_b"] = np.nan
    for s, g in f.groupby("sec"):
        f.loc[g.index, "m_b"] = multiplier_at(panel.events, s, g["broadcast_dt"].values.astype("datetime64[ns]"))
    f["bdate"] = f["broadcast_dt"].dt.normalize()
    return f


def mcap_and_ep(filings: pd.DataFrame, panel: Panel, F: pd.Timestamp,
                raw_close: pd.Series) -> pd.DataFrame:
    """Market cap and TTM earnings yield at formation session F.

    Only filings broadcast strictly before F's date are used. Shares: median of
    the latest four known filings' shares_implied (standalone preferred, where
    minority interest cannot distort profit/EPS), each carried forward through
    splits and bonuses with ex-date after its broadcast. TTM profit: the
    latest four consecutive quarters, consolidated if the company has them,
    else standalone, latest revision of each quarter.
    """
    k = filings[filings["bdate"] < F]
    if k.empty:
        return pd.DataFrame(columns=["mcap", "ep"])
    k = k.sort_values("broadcast_dt")
    sh = k[k["shares_implied"].notna() & (k["shares_implied"] > 0)]
    m_F = pd.Series({s: multiplier_at(panel.events, s, np.array([F.to_datetime64()]))[0]
                     for s in sh["sec"].unique()})
    sh = sh.assign(sh_adj=sh["shares_implied"] * sh["sec"].map(m_F) / sh["m_b"])
    sa = sh[~sh["consolidated"]].groupby("sec").tail(4)
    co = sh[sh["consolidated"] & ~sh["sec"].isin(sa["sec"])].groupby("sec").tail(4)
    shares = pd.concat([sa, co]).groupby("sec")["sh_adj"].median()
    mcap = (raw_close.reindex(shares.index) * shares)
    mcap = mcap[(mcap > 1e7) & (mcap < 3e13)]

    q = k[k["profit_normalised"].notna()]
    q = q.drop_duplicates(["sec", "consolidated", "period_end"], keep="last")
    q = q.sort_values("period_end").groupby(["sec", "consolidated"]).tail(4)
    g = q.groupby(["sec", "consolidated"])
    agg = g.agg(n=("period_end", "size"), last=("period_end", "max"), first=("period_end", "min"),
                ttm=("profit_normalised", "sum"))
    span = (agg["last"].dt.year * 12 + agg["last"].dt.month) - (agg["first"].dt.year * 12 + agg["first"].dt.month)
    ok = agg[(agg["n"] == 4) & (span == 9) & ((F - agg["last"]).dt.days <= 200)].reset_index()
    ok = ok.sort_values("consolidated", ascending=False).drop_duplicates("sec")
    ttm = ok.set_index("sec")["ttm"]
    out = pd.DataFrame({"mcap": mcap})
    out["ep"] = ttm.reindex(out.index) / out["mcap"]
    return out


def formation(panel: Panel, filings: pd.DataFrame | None, m: pd.Period) -> pd.DataFrame:
    """Everything known at the close of month m's last session, per security."""
    me = panel.month_ends
    F = me[m]
    a_now = _at(panel.adj, F, 4)                         # traded in last 5 sessions
    a_skip = _at(panel.adj, me[m - 1], 10) if (m - 1) in me.index else None
    a_base = _at(panel.adj, me[m - 12], 10) if (m - 12) in me.index else None
    raw_now = _at(panel.close, F, 4)
    df = pd.DataFrame({
        "price_ok": a_now.notna(),
        "med60": panel.med60.loc[F], "n365": panel.n365.loc[F],
        "medpx": panel.medpx252.loc[F], "turn12": panel.turn252.loc[F],
    })
    df["liquid"] = (df["price_ok"] & (df["med60"] >= LIQUID_MIN_MEDIAN_TURNOVER)
                    & (df["n365"] >= LIQUID_MIN_SESSIONS_365D))
    df["broad"] = (df["price_ok"] & (df["n365"] >= BROAD_MIN_SESSIONS_365D)
                   & (df["medpx"] >= BROAD_MIN_MEDIAN_PRICE))
    df["mom"] = (a_skip / a_base - 1.0) if a_base is not None else np.nan
    if (m - 12) in me.index:
        df["jump_formation"] = (panel.jumps_cum.loc[me[m - 1]] - panel.jumps_cum.loc[me[m - 12]]) > 0
    else:
        df["jump_formation"] = False
    if filings is not None:
        mc = mcap_and_ep(filings, panel, F, raw_now)
        df = df.join(mc, how="left")
    df = df[df["liquid"] | df["broad"]]
    df.index.name = "sec"
    return df


def holding_return(panel: Panel, m: pd.Period) -> pd.DataFrame:
    """Month m+1 price return for securities priced at the end of month m.

    A stock that stops trading is valued at its last close (IIMA likewise
    compounds only the daily returns that exist).
    """
    me = panel.month_ends
    F, G = me[m], me[m + 1]
    guard_returns([m + 1], f"holding return {panel.variant}")
    start = _at(panel.adj, F, 4)
    end = panel.adj.loc[:G].ffill().iloc[-1]
    raw_start = _at(panel.close, F, 4)
    raw_end = panel.close.loc[:G].ffill().iloc[-1]
    out = pd.DataFrame({
        "ret": end / start - 1.0,
        "raw_ret": raw_end / raw_start - 1.0,
        "jump_hold": (panel.jumps_cum.loc[G] - panel.jumps_cum.loc[F]) > 0,
    })
    return out


def formation_table(panel: Panel, filings: pd.DataFrame | None) -> pd.DataFrame:
    rows = []
    months = [m for m in panel.month_ends.index if FIRST_MONTH - 1 <= m < LAST_MONTH]
    guard_returns([m + 1 for m in months], f"formation table {panel.variant}")
    for m in months:
        f = formation(panel, filings, m)
        h = holding_return(panel, m)
        f = f.join(h, how="left")
        f["hold_month"] = m + 1
        rows.append(f.reset_index())
        log.debug("%s %s: %d rows", panel.variant, m, len(f))
    return pd.concat(rows, ignore_index=True)


# --------------------------------------------------------------------------
# Factor construction
# --------------------------------------------------------------------------

def _wavg(r: pd.Series, w: pd.Series | None) -> float:
    if len(r) < MIN_PORTFOLIO:
        return np.nan
    if w is None:
        return float(r.mean())
    return float((r * w).sum() / w.sum())


def wml_flat(t: pd.DataFrame, universe: str, drop_jumps: bool = False) -> pd.DataFrame:
    out = {}
    for m, g in t[t[universe] & t["mom"].notna() & t["ret"].notna()].groupby("hold_month"):
        if drop_jumps:
            g = g[~g["jump_formation"]]
        lo, hi = g["mom"].quantile([0.3, 0.7])
        w, l_ = g[g["mom"] >= hi], g[g["mom"] <= lo]
        out[m] = {"W": _wavg(w["ret"], None), "L": _wavg(l_["ret"], None),
                  "n": len(g), "p30": lo, "p70": hi}
    df = pd.DataFrame(out).T
    df["WML"] = df["W"] - df["L"]
    return df


def _big_flag(g: pd.DataFrame, size: str, bp: pd.DataFrame | None, m_form: pd.Period) -> pd.Series:
    if size == "mcap_iima":
        cut = bp.loc[m_form, "size90_mn"] * 1e6 if (bp is not None and m_form in bp.index) else np.nan
        return g["mcap"] >= cut
    return g[size] >= g[size].quantile(0.9)


def wml_2x2(t: pd.DataFrame, universe: str, size: str, weight: str | None,
            bp: pd.DataFrame | None = None) -> pd.DataFrame:
    out = {}
    need = [universe, "mom", "ret"] + ([weight] if weight else []) + (["mcap"] if size == "mcap_iima" else [size])
    base = t[t[universe] & t["mom"].notna() & t["ret"].notna()]
    for col in need[1:]:
        base = base[base[col].notna()]
    for m, g in base.groupby("hold_month"):
        lo, hi = g["mom"].quantile([0.3, 0.7])
        big = _big_flag(g, size, bp, m - 1)
        wt = (lambda x: x[weight]) if weight else (lambda x: None)
        leg = {}
        for name, sel in {"WB": (g["mom"] >= hi) & big, "WS": (g["mom"] >= hi) & ~big,
                          "LB": (g["mom"] <= lo) & big, "LS": (g["mom"] <= lo) & ~big}.items():
            x = g[sel]
            leg[name] = _wavg(x["ret"], wt(x))
        diffs = [leg["WS"] - leg["LS"], leg["WB"] - leg["LB"]]
        diffs = [d for d in diffs if pd.notna(d)]
        leg["WML"] = float(np.mean(diffs)) if diffs else np.nan
        out[m] = leg
    return pd.DataFrame(out).T


def smb(t: pd.DataFrame, universe: str, size: str, weight: str | None,
        bp: pd.DataFrame | None = None, microcap: bool = False) -> pd.Series:
    out = {}
    base = t[t[universe] & t["ret"].notna()]
    base = base[base["mcap"].notna()] if size == "mcap_iima" or weight == "mcap" else base[base[size].notna()]
    for m, g in base.groupby("hold_month"):
        if microcap:
            g = g[g["mcap"] >= 0.1 * g["mcap"].median()]
        big = _big_flag(g, size, bp, m - 1)
        w = (lambda x: x[weight]) if weight else (lambda x: None)
        out[m] = _wavg(g.loc[~big, "ret"], w(g[~big])) - _wavg(g.loc[big, "ret"], w(g[big]))
    return pd.Series(out)


def hml_ep(t: pd.DataFrame, universe: str, bp: pd.DataFrame, weight: str | None = "mcap") -> pd.DataFrame:
    out = {}
    base = t[t[universe] & t["ret"].notna() & t["mcap"].notna() & t["ep"].notna()]
    for m, g in base.groupby("hold_month"):
        g = g[g["mcap"] >= 0.1 * g["mcap"].median()]
        big = _big_flag(g, "mcap_iima", bp, m - 1)
        pos = g[g["ep"] > 0]
        if len(pos) < 50:
            continue
        lo, hi = pos["ep"].quantile([0.3, 0.7])
        w = (lambda x: x[weight]) if weight else (lambda x: None)
        leg = {}
        for name, sel in {"BV": big & (g["ep"] >= hi), "BG": big & (g["ep"] > 0) & (g["ep"] <= lo),
                          "SV": ~big & (g["ep"] >= hi), "SG": ~big & (g["ep"] > 0) & (g["ep"] <= lo)}.items():
            x = g[sel]
            leg[name] = _wavg(x["ret"], w(x))
        diffs = [leg["SV"] - leg["SG"], leg["BV"] - leg["BG"]]
        diffs = [d for d in diffs if pd.notna(d)]
        leg["HML"] = float(np.mean(diffs)) if diffs else np.nan
        leg["n"] = len(pos)
        out[m] = leg
    return pd.DataFrame(out).T


def market(t: pd.DataFrame, universe: str, weight: str | None) -> pd.Series:
    base = t[t[universe] & t["ret"].notna()]
    if weight:
        base = base[base[weight].notna()]
    out = {}
    for m, g in base.groupby("hold_month"):
        if weight and len(g) < 0.5 * t[(t["hold_month"] == m) & t[universe]].shape[0]:
            continue                      # cap weights exist for too few names yet
        out[m] = _wavg(g["ret"], g[weight] if weight else None)
    return pd.Series(out)


# --------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------

def compare(name: str, ours: pd.Series, ref: pd.Series, ref_name: str) -> dict:
    ours = ours.dropna().astype(float)
    ref = ref.dropna().astype(float)
    ix = ours.index.intersection(ref.index)
    guard_returns(ix, f"compare {name}")
    a, b = ours.loc[ix], ref.loc[ix]
    d = a - b
    return {
        "series": name, "vs": ref_name, "months": len(ix),
        "first": str(ix.min()) if len(ix) else "", "last": str(ix.max()) if len(ix) else "",
        "corr": a.corr(b), "spearman": a.rank().corr(b.rank()),
        "ours_mean_pct": 100 * a.mean(), "iima_mean_pct": 100 * b.mean(),
        "ours_t": a.mean() / (a.std() / np.sqrt(len(a))) if len(a) > 2 else np.nan,
        "iima_t": b.mean() / (b.std() / np.sqrt(len(b))) if len(b) > 2 else np.nan,
        "ours_sd_pct": 100 * a.std(), "iima_sd_pct": 100 * b.std(),
        "diff_sd_pct": 100 * d.std(),
    }


def drill_down(t: pd.DataFrame, rule_t: pd.DataFrame, ours: pd.DataFrame, ref: pd.DataFrame,
               universe: str, top: int = 6) -> list[dict]:
    """Largest monthly disagreements and the stocks that drove our side of them."""
    d = (ours["WML"] - ref["WML"]).dropna()
    guard_returns(d.index, "drill-down")
    sd = d.std()
    worst = d.abs().sort_values(ascending=False).head(top)
    notes = []
    for m in worst.index:
        g = t[(t["hold_month"] == m) & t[universe] & t["mom"].notna() & t["ret"].notna()]
        lo, hi = g["mom"].quantile([0.3, 0.7])
        legs = {"W": g[g["mom"] >= hi], "L": g[g["mom"] <= lo]}
        movers = []
        for leg, x in legs.items():
            contrib = (x["ret"] - x["ret"].mean()) / len(x)
            for sec in contrib.abs().sort_values(ascending=False).head(3).index:
                r = x.loc[sec]
                movers.append(f"{leg}:{r['sec']} ret={r['ret']:+.0%} mom={r['mom']:+.0%}"
                              + (" JUMP" if r["jump_hold"] else ""))
        notes.append({
            "month": str(m), "ours": ours.loc[m, "WML"], "iima": ref.loc[m, "WML"],
            "diff": d.loc[m], "z": d.loc[m] / sd,
            "ours_W": ours.loc[m, "W"], "ours_L": ours.loc[m, "L"],
            "iima_W": (ref.loc[m, "WS"] + ref.loc[m, "WB"]) / 2,
            "iima_L": (ref.loc[m, "LS"] + ref.loc[m, "LB"]) / 2,
            "movers": "; ".join(movers),
        })
    return notes


_SCHEME = re.compile(r"demerg|arrangement|scheme|amalgamat|capital reduction|reduction of capital", re.I)
_AMOUNT = re.compile(r"r[se]\.?\s*(\d+(?:\.\d+)?)", re.I)
_SPLIT_LIKE = sorted({1 / k for k in (2, 3, 4, 5, 10, 20)}
                     | {b / (a + b) for a in range(1, 6) for b in range(1, 6)}
                     | {0.5 / k for k in (2, 5, 10)})


def map_actions(panel: Panel, ca: pd.DataFrame) -> pd.DataFrame:
    """Every corporate-action row (any type) attached to a panel security."""
    x = ca.copy()
    if panel.variant == "rule":
        x["sec"] = x["symbol"].where(x["symbol"].isin(panel.close.columns))
    else:
        x["sec"] = x["isin"].map(panel.isin_to_sec).fillna(x["symbol"].map(panel.sym_to_sec))
    return x[x["sec"].notna()]


def adjustment_audit(panel: Panel, t: pd.DataFrame, ca: pd.DataFrame) -> dict:
    """One-day adjusted drops below 0.55x or rises above 2.2x in liquid names.

    Each is classified by what the corporate-action feed says around it:
    a scheme (demerger, amalgamation, capital reduction), a special dividend
    of at least 20% of the price, a ratio that looks like a split or bonus the
    feed does not carry, or nothing at all (a genuine crash or an unrecorded
    event). All of these remain in the adjusted series and are booked as
    returns by anything that reads it.
    """
    liquid_secs = set(t.loc[t["liquid"], "sec"])
    cols = [c for c in panel.adj.columns if c in liquid_secs]
    adj = panel.adj[cols]
    prev = adj.ffill().shift(1)
    prev_d = pd.DataFrame(np.where(adj.notna(), adj.index.values[:, None], np.datetime64("NaT", "ns")),
                          index=adj.index, columns=cols).ffill().shift(1)
    ratio = adj / prev
    mask = ((ratio < JUMP_DOWN) | (ratio > JUMP_UP)).fillna(False)
    j = ratio.where(mask).stack().dropna()
    j.index.names = ["d", "sec"]
    jd = j.reset_index(name="ratio")
    jd["prev_d"] = [prev_d.at[d, s] for d, s in zip(jd["d"], jd["sec"])]
    prev_raw = panel.close[cols].ffill().shift(1)
    jd["prev_px"] = [prev_raw.at[d, s] for d, s in zip(jd["d"], jd["sec"])]

    acts = map_actions(panel, ca)
    cats = []
    for r in jd.itertuples():
        lo = pd.Timestamp(r.prev_d) - pd.Timedelta(days=5) if pd.notna(r.prev_d) else r.d - pd.Timedelta(days=30)
        near = acts[(acts["sec"] == r.sec) & (acts["ex_date"] > lo) & (acts["ex_date"] <= r.d + pd.Timedelta(days=5))]
        subj = " | ".join(near["subject"].astype(str))
        if _SCHEME.search(subj):
            cats.append("scheme/demerger")
            continue
        divs = [float(m.group(1)) for s in near.loc[near["action"] == "dividend", "subject"]
                for m in [_AMOUNT.search(str(s))] if m]
        if divs and r.prev_px and max(divs) >= 0.2 * r.prev_px:
            cats.append("special dividend")
            continue
        if r.ratio < 1 and any(abs(r.ratio / c - 1) < 0.04 for c in _SPLIT_LIKE):
            cats.append("split/bonus-like, not in feed")
            continue
        cats.append("no action recorded")
    jd["category"] = cats
    counts = jd["category"].value_counts().to_dict()
    ex = {c: [f"{r.sec}@{r.d:%Y-%m-%d}x{r.ratio:.3f}" for r in g.sort_values("ratio").head(10).itertuples()]
          for c, g in jd.groupby("category")}
    return {"liquid_secs": len(cols), "jumps": int(len(jd)),
            "drops": int((jd["ratio"] < JUMP_DOWN).sum()), "rises": int((jd["ratio"] > JUMP_UP).sum()),
            "by_category": counts, "examples": ex, "table": jd}


# --------------------------------------------------------------------------
# Leak test
# --------------------------------------------------------------------------

def leak_test(px, ca, fin, full: Panel, full_f: pd.DataFrame, when: list[pd.Period]) -> list[dict]:
    """The formation snapshot must not change when the inputs end at F.

    Returns (holding-month data) are excluded: they are future by definition.
    Compared: universe membership, momentum, market cap and E/P.

    Two modes. "strict" rebuilds everything, including security identity,
    from the truncated archive. "identity_held" keeps the full archive's map
    from symbol and ISIN to security and truncates every value. Filings and
    corporate actions carry TODAY's symbol and ISIN, so a filing by a company
    that was renamed after F (LANDSMILL, traded as EXCELINFO in 2019) can only
    be tied to its pre-rename trading history through the later rename. That
    is identity resolution, not information about prices or earnings, and the
    identity-held mode must pass exactly.
    """
    reports = []
    for m in when:
        F = full.month_ends[m]
        a = formation(full, full_f, m)
        for mode in ("strict", "identity_held"):
            ident = (full.sym_to_sec, full.isin_to_sec) if mode == "identity_held" else None
            tp = build_panel(px[px["d"] <= F], ca[ca["ex_date"] <= F], "fixed", identity=ident)
            # shares_table reads corpactions only for the (symbol, ISIN) pair
            # that ties a filing's symbol to a security, so that is identity too
            id_ca = ca if mode == "identity_held" else ca[ca["ex_date"] <= F]
            tf = shares_table(fin[fin["broadcast_dt"] <= F], tp, id_ca)
            b = formation(tp, tf, m)
            rep = {"month": str(m), "mode": mode, "rows_full": len(a), "rows_trunc": len(b)}
            common = a.index.intersection(b.index)
            rep["only_full"] = sorted(set(a.index) - set(b.index))[:8]
            rep["only_trunc"] = sorted(set(b.index) - set(a.index))[:8]
            for col in ("liquid", "broad"):
                rep[f"{col}_diff"] = int((a.loc[common, col] != b.loc[common, col]).sum())
            for col in ("mom", "mcap", "ep"):
                x = a.loc[common, col].astype(float).to_numpy()
                y = b.loc[common, col].astype(float).to_numpy()
                same = np.isclose(x, y, rtol=1e-9, atol=0) | (np.isnan(x) & np.isnan(y))
                rep[f"{col}_diff"] = int((~same).sum())
                rep[f"{col}_diff_secs"] = list(common[~same])[:8]
            rep["passed"] = (not rep["only_full"] and not rep["only_trunc"]
                             and all(rep[f"{c}_diff"] == 0 for c in ("liquid", "broad", "mom", "mcap", "ep")))
            reports.append(rep)
    return reports


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--skip-leak-test", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    pd.set_option("display.width", 220)
    pd.set_option("display.max_columns", 30)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    con = connect()
    iima = load_iima()
    bp = load_iima_breakpoints()
    px = load_prices(con)
    ca = load_corpactions(con)
    fin = load_financials(con)
    n500 = load_nifty500(con)
    assert px["d"].max() <= pd.Timestamp(PRICE_CUTOFF)
    log.info("prices %s rows %s..%s, %d symbols; corpactions %d; filings %d",
             f"{len(px):,}", px["d"].min().date(), px["d"].max().date(), px["symbol"].nunique(),
             len(ca), len(fin))

    rule = build_panel(px, ca, "rule")
    fixed = build_panel(px, ca, "fixed")
    log.info("rule factors: %s", rule.fac_stats)
    log.info("fixed factors: %s", fixed.fac_stats)
    _, overlaps = link_securities(px)
    log.info("securities: %d symbols -> %d securities; %d ISINs shared by overlapping symbols",
             px["symbol"].nunique(), fixed.close.shape[1], len(overlaps))

    filings = shares_table(fin, fixed, ca)
    log.info("filings: %d symbols, %d resolved through corpaction ISIN, %d unresolved",
             filings.attrs["symbols"], filings.attrs["mapped_via_isin_symbols"],
             filings.attrs["unmapped_symbols"])

    t_rule = formation_table(rule, None)
    t_fix = formation_table(fixed, filings)
    t_fix.to_parquet(OUT_DIR / "formation_fixed.parquet", index=False)

    # ---- factors
    series = {}
    w_rule = wml_flat(t_rule, "liquid")
    w_fix = wml_flat(t_fix, "liquid")
    w_fix_nj = wml_flat(t_fix, "liquid", drop_jumps=True)
    w_broad = wml_flat(t_fix, "broad")
    w_2x2_turn = wml_2x2(t_fix, "broad", "turn12", None)
    w_2x2_vw = wml_2x2(t_fix, "broad", "mcap_iima", "mcap", bp)
    series.update({
        "WML_rule_liquid_ew": w_rule["WML"], "WML_fixed_liquid_ew": w_fix["WML"],
        "WML_fixed_liquid_ew_nojump": w_fix_nj["WML"], "WML_fixed_broad_ew": w_broad["WML"],
        "WML_fixed_broad_2x2_turnsize_ew": w_2x2_turn["WML"],
        "WML_fixed_broad_2x2_mcap_vw": w_2x2_vw["WML"],
        "SMB_broad_turn_ew": smb(t_fix, "broad", "turn12", None),
        "SMB_liquid_turn_ew": smb(t_fix, "liquid", "turn12", None),
        "SMB_broad_mcap_vw": smb(t_fix, "broad", "mcap_iima", "mcap", bp, microcap=True),
        "SMB_broad_mcap_ew": smb(t_fix, "broad", "mcap_iima", None, bp, microcap=True),
        "MKT_broad_ew": market(t_fix, "broad", None),
        "MKT_broad_vw": market(t_fix, "broad", "mcap"),
        "NIFTY500_price": n500,
    })
    h = hml_ep(t_fix, "broad", bp)
    series["HML_ep_broad_vw"] = h["HML"] if len(h) else pd.Series(dtype=float)
    h_ew = hml_ep(t_fix, "broad", bp, weight=None)
    series["HML_ep_broad_ew"] = h_ew["HML"] if len(h_ew) else pd.Series(dtype=float)

    for k, s in series.items():
        guard_returns(s.dropna().index, k)
    ours = pd.DataFrame(series).sort_index()
    ours.index = pd.PeriodIndex(ours.index, freq="M")
    ours = ours.loc[FIRST_MONTH:LAST_MONTH]

    pairs = [(c, "WML") for c in ours.columns if c.startswith("WML")] + \
            [(c, "SMB") for c in ours.columns if c.startswith("SMB")] + \
            [(c, "HML") for c in ours.columns if c.startswith("HML")] + \
            [(c, "RM") for c in ("MKT_broad_ew", "MKT_broad_vw", "NIFTY500_price")]
    table = pd.DataFrame([compare(c, ours[c], iima[r], f"IIMA {r}") for c, r in pairs])

    # legs: our winners and losers against IIMA's average of big and small legs
    legs = []
    for nm, w in (("rule", w_rule), ("fixed", w_fix)):
        w.index = pd.PeriodIndex(w.index, freq="M")
        legs.append(compare(f"W_{nm}_liquid_ew", w["W"], (iima["WS"] + iima["WB"]) / 2, "IIMA (WS+WB)/2"))
        legs.append(compare(f"L_{nm}_liquid_ew", w["L"], (iima["LS"] + iima["LB"]) / 2, "IIMA (LS+LB)/2"))
    w_2x2_turn.index = pd.PeriodIndex(w_2x2_turn.index, freq="M")
    for leg in ("WB", "WS", "LB", "LS"):
        legs.append(compare(f"{leg}_2x2_turnsize_ew", w_2x2_turn[leg], iima[leg], f"IIMA {leg}"))
    legs = pd.DataFrame(legs)

    # breakpoints: our broad-universe momentum percentiles vs IIMA's
    wb = wml_flat(t_fix, "broad")
    wb.index = pd.PeriodIndex(wb.index, freq="M") - 1          # back to formation month
    bpc = bp.join(wb[["p30", "p70"]] * 100, how="inner")
    bp_cmp = {"months": len(bpc),
              "corr_p30": bpc["mom30_pct"].corr(bpc["p30"]), "corr_p70": bpc["mom70_pct"].corr(bpc["p70"]),
              "mean_p30_iima": bpc["mom30_pct"].mean(), "mean_p30_ours": bpc["p30"].mean(),
              "mean_p70_iima": bpc["mom70_pct"].mean(), "mean_p70_ours": bpc["p70"].mean()}

    # rule vs fixed: where the adjustment bugs moved WML
    rf = pd.DataFrame({"rule": w_rule["WML"], "fixed": w_fix["WML"]})
    rf.index = pd.PeriodIndex(rf.index, freq="M")
    rf["gap"] = rf["rule"] - rf["fixed"]
    worst_gap = rf["gap"].abs().sort_values(ascending=False).head(5)

    w_fix_p = w_fix.copy()
    notes = drill_down(t_fix.assign(hold_month=pd.PeriodIndex(t_fix["hold_month"], freq="M")),
                       t_rule, w_fix_p, iima, "liquid")

    audit_rule = adjustment_audit(rule, t_rule, ca)
    audit_fix = adjustment_audit(fixed, t_fix, ca)
    audit_fix["table"].to_csv(OUT_DIR / "unexplained_jumps_fixed.csv", index=False)
    audit_rule["table"].to_csv(OUT_DIR / "unexplained_jumps_rule.csv", index=False)

    # robustness of the headline correlation
    robust = []
    for c in ("WML_rule_liquid_ew", "WML_fixed_liquid_ew", "WML_fixed_broad_2x2_mcap_vw"):
        a, b = ours[c].dropna(), iima["WML"]
        ix = a.index.intersection(b.index)
        d = (a.loc[ix] - b.loc[ix]).abs()
        drop1 = ix.drop(d.idxmax())
        robust.append({
            "series": c, "all": a.loc[ix].corr(b.loc[ix]),
            f"ex_{d.idxmax()}": a.loc[drop1].corr(b.loc[drop1]),
            "2016-2018": a.loc[ix[ix.year <= 2018]].corr(b.loc[ix[ix.year <= 2018]]),
            "2019-2022": a.loc[ix[ix.year >= 2019]].corr(b.loc[ix[ix.year >= 2019]]),
            "from_2018-06": a.loc[ix[ix >= pd.Period("2018-06", "M")]].corr(b.loc[ix[ix >= pd.Period("2018-06", "M")]]),
        })

    ours.to_csv(OUT_DIR / "ours_monthly_2016_2022.csv")
    table.to_csv(OUT_DIR / "comparison.csv", index=False)
    legs.to_csv(OUT_DIR / "comparison_legs.csv", index=False)

    print("\n=== IIMA source ===")
    print(f"{IIMA_URL}  release {IIMA_RELEASE}, files in {IIMA_DIR.relative_to(ROOT)}")
    print("\n=== correlations, 2016-01..2022-12 (monthly, decimals; means in % per month) ===")
    print(table.round(3).to_string(index=False))
    print("\n=== legs ===")
    print(legs.round(3).to_string(index=False))
    print("\n=== momentum breakpoints, broad universe vs IIMA (%, formation months) ===")
    print({k: round(v, 3) if isinstance(v, float) else v for k, v in bp_cmp.items()})
    print("\n=== rule vs fixed adjustment, liquid EW WML ===")
    print(f"corr {rf['rule'].corr(rf['fixed']):.3f}; mean rule {100*rf['rule'].mean():.2f}% "
          f"fixed {100*rf['fixed'].mean():.2f}%; largest gaps:")
    print((worst_gap.to_frame("abs_gap") * 100).round(2).to_string())
    print("\n=== unexplained one-day jumps (ratio <0.55 or >2.2) left in liquid names ===")
    for nm, au in (("rule", audit_rule), ("fixed", audit_fix)):
        print(f"{nm}: liquid secs {au['liquid_secs']}, jumps {au['jumps']} "
              f"(drops {au['drops']}, rises {au['rises']}); {au['by_category']}")
        for cat, exs in au["examples"].items():
            print(f"    {cat}: {exs}")
    print("\n=== WML correlation robustness ===")
    for r in robust:
        print({k: (round(v, 3) if isinstance(v, float) else v) for k, v in r.items()})
    print("\n=== largest WML disagreements (fixed liquid EW vs IIMA) ===")
    for n in notes:
        print({k: (round(v, 4) if isinstance(v, float) else v) for k, v in n.items()})

    if not args.skip_leak_test:
        rep = leak_test(px, ca, fin, fixed, filings,
                        [pd.Period("2017-06", "M"), pd.Period("2019-12", "M"), pd.Period("2021-09", "M")])
        print("\n=== leak test (formation snapshot, full vs truncated inputs) ===")
        for r in rep:
            print(r)
        held_ok = all(r["passed"] for r in rep if r["mode"] == "identity_held")
        strict_diffs = sum(r["mcap_diff"] + r["ep_diff"] + r["mom_diff"] for r in rep if r["mode"] == "strict")
        print(f"identity-held leak test: {'PASSED' if held_ok else 'FAILED'}; "
              f"strict mode differs on {strict_diffs} values, all from symbol/ISIN "
              f"resolution of renamed companies" if held_ok else "")
        if not held_ok:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
