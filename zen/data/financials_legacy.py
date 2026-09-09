"""Quarterly financials from NSE's PRE-2025 results endpoint.

WHY THIS MODULE EXISTS

financials.py uses /api/integrated-filing-results, which NSE introduced in
2025. It works, but it only knows about the new scheme, so the archive it
builds starts in March 2025 -- eighteen months of history, which is not enough
to backtest anything.

The older endpoint, /api/corporates-financial-results, was previously written
off as a dead end. That was wrong, and the reason is worth recording: it
returns an empty list unless `period=Quarterly` is passed. Without that one
parameter the endpoint looks like a source with no data; with it, the same URL
returns thousands of filings per quarter going back years.

WHAT IT ACTUALLY CARRIES -- measured, not assumed

  Income statement   Mar 2018 onward. Quarterly, standalone and consolidated,
                     tagged Ind-AS XBRL under the INDAS_*.xml naming scheme.
  Balance sheet      Sep 2022 onward, half-yearly (March and September only),
                     which mirrors what SEBI actually requires.
  Filing metadata    back to 2005, and broadcast timestamps back to 2007, but
                     with no XBRL attached -- the `xbrl` field on those rows is
                     the literal placeholder ending in a bare hyphen, which
                     404s. Those rows are indexed and skipped, not fetched.

So the honest boundary: revenue, margins, profit and EPS can be tested over
seven years; leverage and book value over four. Anything needing a balance
sheet before September 2022 still cannot be answered from this source, and
pretending otherwise is how a backtest starts lying.

POINT-IN-TIME

`broadCastDate` is when NSE published the filing. A December quarter is
published in mid-January, so a screen filtering on period end would grant
itself six weeks of foresight. Rows carry both dates and every consumer must
filter on broadcast_dt, exactly as known_at() does for the new scheme.

The XBRL parser and the derived-field logic are imported from financials.py
rather than copied: both schemes emit the same Ind-AS taxonomy, and two
divergent parsers would eventually disagree about what EBITDA means.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from zen.data.bhavcopy import _session
from zen.data.financials import PARQUET_DIR, _derive, parse_xbrl

log = logging.getLogger(__name__)

WARMUP = "https://www.nseindia.com/companies-listing/corporate-filings-financial-results"
LISTING = ("https://www.nseindia.com/api/corporates-financial-results"
           "?index=equities&period=Quarterly&from_date={frm}&to_date={to}")

# XBRL documents exist from here; earlier filings are metadata only.
XBRL_FROM = date(2018, 1, 1)

_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def _d(raw):
    """Parse NSE's '31-Dec-2024' day stamps."""
    if not raw:
        return None
    try:
        p = str(raw).strip().split()[0].split("-")
        return date(int(p[2]), _MONTHS[p[1][:3].title()], int(p[0]))
    except (ValueError, KeyError, IndexError):
        return None


def _dt(raw):
    """Parse '16-Jan-2025 20:20:21' broadcast stamps."""
    if not raw:
        return None
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M"):
        try:
            return datetime.strptime(str(raw).strip(), fmt)
        except ValueError:
            continue
    return None


def _has_xbrl(url) -> bool:
    """A real filing document, not the placeholder older rows carry."""
    u = str(url or "").strip()
    return u.lower().endswith(".xml")


def listing(session, start: date, end: date, window_days: int = 45) -> pd.DataFrame:
    """Index every quarterly filing announced between two dates.

    Queried in windows rather than as one span because NSE truncates large
    responses silently -- a single 2018-2026 request returns a plausible but
    incomplete list, which is the kind of quiet loss nobody notices until a
    backtest already has a hole in it.
    """
    rows, cur = [], start
    while cur <= end:
        stop = min(cur + timedelta(days=window_days), end)
        url = LISTING.format(frm=cur.strftime("%d-%m-%Y"), to=stop.strftime("%d-%m-%Y"))
        try:
            r = session.get(url, timeout=60)
            batch = r.json() if r.status_code == 200 else []
        except Exception as e:                                   # noqa: BLE001
            log.warning("listing %s..%s failed: %s", cur, stop, e)
            batch = []
        if isinstance(batch, list):
            rows.extend(batch)
            log.info("listing %s..%s: %d filings", cur, stop, len(batch))
        cur = stop + timedelta(days=1)
        time.sleep(0.3)

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df["period_end"] = df.get("toDate").map(_d)
    df["broadcast_dt"] = df.get("broadCastDate").map(_dt)
    df["consolidated"] = df["consolidated"].astype(str).str.strip().str.lower().eq("consolidated")
    df["audited"] = df.get("audited", pd.Series(dtype=str)).astype(str).str.lower().eq("audited")
    df["has_xbrl"] = df["xbrl"].map(_has_xbrl)
    df = df.rename(columns={"companyName": "company", "xbrl": "xbrl_url"})
    keep = ["symbol", "company", "isin", "period_end", "broadcast_dt", "consolidated",
            "audited", "xbrl_url", "has_xbrl", "seqNumber"]
    df = df[[c for c in keep if c in df.columns]]
    # Adjacent windows overlap on filings announced near a boundary; seqNumber
    # is NSE's own filing id and is the only reliable key for de-duplication.
    if "seqNumber" in df.columns:
        df = df.drop_duplicates(subset=["seqNumber"])
    return df.reset_index(drop=True)


def fetch_documents(session, index: pd.DataFrame, sleep: float = 0.15) -> pd.DataFrame:
    """Download and parse each filing's XBRL into one row of figures."""
    todo = index[index["has_xbrl"] & index["period_end"].notna()]
    log.info("fetching %d XBRL documents", len(todo))
    out, failed = [], 0
    for n, (_, meta) in enumerate(todo.iterrows(), 1):
        try:
            r = session.get(meta["xbrl_url"], timeout=45)
            facts = parse_xbrl(r.content) if r.status_code == 200 else {}
        except Exception:                                        # noqa: BLE001
            facts = {}
        if not facts:
            failed += 1
        else:
            row = {"symbol": meta["symbol"], "company": meta.get("company"),
                   "period_end": meta["period_end"], "broadcast_dt": meta["broadcast_dt"],
                   "consolidated": bool(meta["consolidated"]),
                   "audited": bool(meta.get("audited", False)),
                   "xbrl_url": meta["xbrl_url"], **facts}
            out.append(_derive(row))
        if n % 250 == 0:
            log.info("  %d/%d fetched, %d without figures", n, len(todo), failed)
        time.sleep(sleep)
    log.info("parsed %d filings; %d returned no figures", len(out), failed)
    return pd.DataFrame(out)


def backfill(start: date = XBRL_FROM, end: date | None = None,
             out_dir: Path = PARQUET_DIR) -> pd.DataFrame:
    """Index, download and persist, one parquet per quarter."""
    end = end or date.today()
    s = _session()
    s.get(WARMUP, timeout=30)

    idx = listing(s, start, end)
    if idx.empty:
        log.error("listing returned nothing -- check whether period=Quarterly "
                  "is still required by the endpoint")
        return idx
    log.info("indexed %d filings, %d with a real XBRL document, %s to %s",
             len(idx), int(idx["has_xbrl"].sum()),
             idx["period_end"].min(), idx["period_end"].max())

    df = fetch_documents(s, idx)
    if df.empty:
        return df

    out_dir.mkdir(parents=True, exist_ok=True)
    df["period_end"] = pd.to_datetime(df["period_end"])
    for period, chunk in df.groupby(df["period_end"].dt.to_period("Q")):
        path = out_dir / f"legacy_{period}.parquet"
        chunk.to_parquet(path, index=False)
        log.info("wrote %s (%d rows)", path.name, len(chunk))
    return df
