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


MISSING_LEDGER = PARQUET_DIR / "legacy_missing.parquet"

# NSE's cookies go stale within minutes and the endpoint then degrades rather
# than failing outright: requests keep returning correct answers but take
# twenty times longer. Measured 0.2s per document on a fresh session against
# 8.6s on a session an hour old.
#
# Re-warming is timed rather than counted. A counter reset every quarter and
# never reached its threshold on the small batches a resumed run produces, so
# the re-warm this constant was supposed to trigger never fired once. Elapsed
# time is what actually correlates with staleness, so elapsed time is what
# drives it.
REWARM_SECONDS = 240
REWARM_AFTER_FAILURES = 3


def document_session():
    """A throwaway session for nsearchives.

    The archive host wants nothing from the NSE cookie dance -- a bare request
    with a user agent returns the document -- so document fetching does not
    share the cookied session the listing API needs. That matters because the
    failure being guarded against here is a POISONED CONNECTION POOL: when NSE
    drops keep-alive connections badly, every later request on that session
    blocks until timeout, and so does any attempt to revive it. Re-warming
    cookies cannot fix a wedged pool, which is why the previous fix failed
    silently for two and a half hours.

    The cure is a new session object, not a new cookie.
    """
    import requests
    from requests.adapters import HTTPAdapter

    s = requests.Session()
    s.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0 Safari/537.36"),
        "Accept": "application/xml,text/xml,*/*",
    })
    # Keep-alive, deliberately. An earlier version sent Connection: close to
    # stop pools rotting, which worked and cost a full TCP and TLS handshake on
    # every request: measured 0.42s per document against 0.12s reusing one
    # connection, so five-sixths of the run time was handshakes. Replacing the
    # session on a timer already prevents the wedge; closing every connection
    # was belt on top of braces, and the belt cost three and a half times the
    # throughput.
    s.mount("https://", HTTPAdapter(pool_connections=4, pool_maxsize=8,
                                    max_retries=0))
    return s


def _fetch_one(session, url: str, tries: int = 3):
    """One document, with backoff. Returns (facts, outcome).

    Outcomes are deliberately distinguished because they need opposite
    handling. A 404 is permanent -- NSE lists filings whose XBRL was never
    published, and retrying those forever would stall every future run. A
    timeout or a 5xx is transient and deserves another attempt. Treating the
    two the same is how a run either loses data silently or never finishes.
    """
    for attempt in range(tries):
        try:
            r = session.get(url, timeout=20)
            if r.status_code == 404:
                return {}, "missing"
            if r.status_code == 200:
                facts = parse_xbrl(r.content)
                return (facts, "ok") if facts else ({}, "empty")
        except Exception:                                        # noqa: BLE001
            pass
        time.sleep(1.5 * (attempt + 1))
    return {}, "failed"


def fetch_documents(session, index: pd.DataFrame, sleep: float = 0.15,
                    rewarm=None, workers: int = 5,
                    max_rate: float = 8.0) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Download and parse each filing's XBRL.

    Concurrent, because the work is entirely network-bound: the archive host
    serves static files and needs no cookies, so there is no session state to
    serialise around. Each worker keeps its own connection and replaces it on
    the same timer a single-threaded run used.

    `max_rate` caps the COMBINED request rate across workers. Concurrency here
    is to stop waiting on latency, not to lean on someone else's server, and a
    shared ceiling makes the load predictable regardless of worker count.

    Returns (rows, unresolved). `unresolved` carries what could not be parsed
    and why, so the caller can tell a permanent gap from one worth retrying
    rather than writing a quarter that quietly lacks a hundred companies.
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    todo = index[index["has_xbrl"] & index["period_end"].notna()]
    total = len(todo)
    log.info("fetching %d XBRL documents on %d workers", total, workers)

    local = threading.local()
    gate = threading.Lock()
    next_slot = [time.monotonic()]
    interval = 1.0 / max_rate if max_rate > 0 else 0.0

    def throttle():
        """One shared ceiling on request rate, whatever the worker count."""
        with gate:
            now = time.monotonic()
            wait = max(0.0, next_slot[0] - now)
            next_slot[0] = max(now, next_slot[0]) + interval
        if wait:
            time.sleep(wait)

    def sess():
        born = getattr(local, "born", 0)
        if not getattr(local, "s", None) or time.monotonic() - born > REWARM_SECONDS:
            if getattr(local, "s", None):
                local.s.close()
            local.s = document_session()
            local.born = time.monotonic()
        return local.s

    def one(meta):
        throttle()
        facts, outcome = _fetch_one(sess(), meta["xbrl_url"])
        if outcome == "failed":
            # A failure is the signal the connection has gone bad; the next
            # call on this thread builds a new one.
            if getattr(local, "s", None):
                local.s.close()
                local.s = None
        if outcome != "ok":
            return None, {"symbol": meta["symbol"], "xbrl_url": meta["xbrl_url"],
                          "period_end": meta["period_end"], "outcome": outcome}
        row = {"symbol": meta["symbol"], "company": meta.get("company"),
               "period_end": meta["period_end"], "broadcast_dt": meta["broadcast_dt"],
               "consolidated": bool(meta["consolidated"]),
               "audited": bool(meta.get("audited", False)),
               "xbrl_url": meta["xbrl_url"], **facts}
        return _derive(row), None

    out, unresolved, done = [], [], 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for row, bad in pool.map(one, (m for _, m in todo.iterrows())):
            if row is not None:
                out.append(row)
            if bad is not None:
                unresolved.append(bad)
            done += 1
            if done % 200 == 0:
                log.info("  %d/%d fetched, %d unresolved", done, total, len(unresolved))

    log.info("parsed %d filings; %d unresolved", len(out), len(unresolved))
    return pd.DataFrame(out), pd.DataFrame(unresolved)


def known_missing(path: Path = MISSING_LEDGER) -> set[str]:
    """URLs NSE has confirmed it does not hold, so runs stop chasing them."""
    if not path.exists():
        return set()
    return set(pd.read_parquet(path)["xbrl_url"])


def record_missing(rows: pd.DataFrame, path: Path = MISSING_LEDGER) -> None:
    """Append confirmed-absent documents. Only 404s: a timeout is not evidence."""
    gone = rows[rows["outcome"] == "missing"] if not rows.empty else rows
    if gone.empty:
        return
    if path.exists():
        gone = pd.concat([pd.read_parquet(path), gone], ignore_index=True)
    gone.drop_duplicates(subset=["xbrl_url"]).to_parquet(path, index=False)


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

    df, unresolved = fetch_documents(s, idx, rewarm=lambda x: x.get(WARMUP, timeout=30))
    if not unresolved.empty:
        record_missing(unresolved)
    if df.empty:
        return df

    out_dir.mkdir(parents=True, exist_ok=True)
    df["period_end"] = pd.to_datetime(df["period_end"])
    for period, chunk in df.groupby(df["period_end"].dt.to_period("Q")):
        path = out_dir / f"legacy_{period}.parquet"
        chunk.to_parquet(path, index=False)
        log.info("wrote %s (%d rows)", path.name, len(chunk))
    return df
