"""NSE's four-level industry classification, one row per symbol.

NSE classifies every listed company on four levels -- macro-economic sector,
sector, industry and basic industry. v2 (spec item 7) uses the `sector` level
for the 3-per-sector cap, replacing the patchy labels from the announcements
archive that left 29% of held v1 slots unlabelled.

TODAY'S LABEL APPLIED TO THE PAST. This is the classification NSE shows on the
day it was fetched (`fetched_at`), not the one in force on any historical
decision date. NSE has re-classified companies (and moved the whole market to
a new four-level scheme in 2023); a company that changed business mix is
labelled by what it is now. Using it on a 2019 rebalance is a known,
accepted look-ahead in the *grouping*, not in any return or signal, and every
result built on it must say so.

Where it comes from. The old `/api/quote-equity` endpoint now answers
403 Access Denied to the project's session (verified 2026-09-23, while other
`/api/` endpoints still answered 200), so this module reads the endpoint NSE's
current quote page itself calls:

    /api/NextApi/apiClient/GetQuoteApi?functionName=getSymbolData
        &marketType=N&series=<EQ|BE>&symbol=<SYMBOL>

and takes `equityResponse[0].secInfo.{macro, sector, industryInfo,
basicIndustry}` plus `metaData.isinCode`. A symbol trading in BE answers
with an all-null payload when asked for EQ, so BE is tried second.

Delisted and renamed symbols. Three outcomes, all recorded, none retried:
  * 404 -- NSE does not know the symbol at all (e.g. ADANITRANS, renamed).
  * 200 with a null or label-less payload on every series (e.g. CADILAHC).
  * 200 with a label for a suspended/delisted company (e.g. SATYAMCOMP). These
    carry NSE's OLD pre-2023 scheme ("IT" / "SOFTWARE"), whose vocabulary does
    not match the current one. `label_scheme` separates them: 'current' when
    the (macro, sector) pair is one that a currently Listed company carries,
    'legacy' otherwise. A sector cap should use only 'current' labels, or map
    legacy ones explicitly.

Network etiquette: www.nseindia.com is hit sequentially at no more than one
request per second (the warm-up included), through the project's own session
helper, re-warmed every `REWARM_EVERY` requests. A 403 stops the run -- it
means bot protection, and this module never tries to get round it.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

from zen.data.bhavcopy import _session

log = logging.getLogger(__name__)

API = "https://www.nseindia.com/api/NextApi/apiClient/GetQuoteApi"
OUT = Path("data/reference/industry_nse.parquet")

MIN_INTERVAL = 1.1        # seconds between any two requests to www.nseindia.com
REWARM_EVERY = 200        # requests per session before a fresh warm-up
SERIES_ORDER = ("EQ", "BE")
TRANSIENT_TRIES = 3       # attempts on a timeout / 5xx / 429 before giving up

COLUMNS = ["symbol", "isin", "macro", "sector", "industry", "basic_industry",
           "fetched_at", "http_status", "note",
           # extras, useful for auditing the label
           "series", "company", "sec_status", "label_scheme"]

# Outcomes that are final: a rerun skips these symbols. Anything else (a
# transient error) is fetched again next time.
FINAL_NOTES = ("ok", "unavailable")


class Blocked(RuntimeError):
    """NSE answered 403: stop, do not work around it."""


class Client:
    """Sequential, rate-limited access to www.nseindia.com via the project session."""

    def __init__(self, min_interval: float = MIN_INTERVAL,
                 rewarm_every: int = REWARM_EVERY):
        self.min_interval = min_interval
        self.rewarm_every = rewarm_every
        self._last = 0.0
        self._since_warm = 0
        self.requests = 0
        self.s: requests.Session | None = None

    def _wait(self) -> None:
        gap = time.monotonic() - self._last
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)

    def _warm(self) -> None:
        self._wait()
        self.s = _session()          # one GET to the homepage for cookies
        self._last = time.monotonic()
        self.requests += 1
        self._since_warm = 0

    def get(self, symbol: str, series: str) -> requests.Response:
        if self.s is None or self._since_warm >= self.rewarm_every:
            self._warm()
        self._wait()
        try:
            return self.s.get(API, timeout=25, params={
                "functionName": "getSymbolData", "marketType": "N",
                "series": series, "symbol": symbol})
        finally:
            self._last = time.monotonic()
            self.requests += 1
            self._since_warm += 1


def _clean(v) -> str | None:
    if v is None:
        return None
    v = str(v).strip()
    return v or None


def parse(payload: dict) -> dict | None:
    """Pull the label fields out of one getSymbolData payload, or None."""
    rows = payload.get("equityResponse") if isinstance(payload, dict) else None
    if not rows:
        return None
    e = rows[0] or {}
    meta, sec = e.get("metaData") or {}, e.get("secInfo") or {}
    out = {
        "isin": _clean(meta.get("isinCode")),
        "company": _clean(meta.get("companyName")),
        "series": _clean(meta.get("series")),
        "sec_status": _clean(sec.get("secStatus")),
        "macro": _clean(sec.get("macro")),
        "sector": _clean(sec.get("sector")),
        "industry": _clean(sec.get("industryInfo")),
        "basic_industry": _clean(sec.get("basicIndustry")),
    }
    return out


def _has_label(p: dict | None) -> bool:
    return bool(p) and any(p.get(k) for k in ("macro", "sector", "industry",
                                              "basic_industry"))


def fetch_symbol(client: Client, symbol: str) -> dict:
    """One output row for `symbol`. Raises Blocked on a 403."""
    row = {"symbol": symbol}
    notes, last_status, best = [], None, None
    transient = False

    for series in SERIES_ORDER:
        resp, err = None, None
        for attempt in range(TRANSIENT_TRIES):
            try:
                resp = client.get(symbol, series)
            except requests.RequestException as ex:
                err = f"{type(ex).__name__}"
                time.sleep(2 * (attempt + 1))
                continue
            if resp.status_code == 403:
                raise Blocked(f"{symbol}/{series}: HTTP 403 from NSE")
            if resp.status_code == 429 or resp.status_code >= 500:
                err = f"HTTP {resp.status_code}"
                time.sleep(5 * (attempt + 1))
                continue
            err = None
            break

        if resp is None or err:
            transient = True
            notes.append(f"{series}: {err}")
            continue

        last_status = resp.status_code
        if resp.status_code == 404:
            # the symbol is unknown to NSE; another series will not help
            notes.append(f"{series}: 404 unknown symbol")
            break
        if resp.status_code != 200:
            transient = True
            notes.append(f"{series}: HTTP {resp.status_code}")
            continue
        try:
            p = parse(resp.json())
        except ValueError:
            transient = True
            notes.append(f"{series}: non-JSON body")
            continue
        if _has_label(p):
            best = p
            break
        if p and p.get("isin") and best is None:
            best = p                      # identity without a label; keep looking
        notes.append(f"{series}: no label in payload")

    row["http_status"] = last_status
    row["fetched_at"] = pd.Timestamp(datetime.now(timezone.utc))
    if best:
        row.update(best)
    if _has_label(best):
        row["note"] = "ok"
    elif transient:
        row["note"] = "error: " + "; ".join(notes)
    else:
        row["note"] = "unavailable: " + "; ".join(notes)
    return row


def label_scheme(df: pd.DataFrame) -> pd.Series:
    """'current' / 'legacy' / None per row, by vocabulary.

    The current scheme is whatever (macro, sector) pairs Listed companies carry
    in this same fetch. Suspended and delisted companies still show NSE's old
    labels, which do not share that vocabulary.
    """
    listed = df[(df["sec_status"] == "Listed") & df["sector"].notna()]
    current = set(zip(listed["macro"], listed["sector"]))
    pair = list(zip(df["macro"], df["sector"]))
    return pd.Series(
        [None if s is None or pd.isna(s) else ("current" if pr in current else "legacy")
         for s, pr in zip(df["sector"], pair)],
        index=df.index, dtype="object")


def load(path: Path = OUT) -> pd.DataFrame:
    if path.exists():
        return pd.read_parquet(path)
    return pd.DataFrame(columns=COLUMNS)


def save(df: pd.DataFrame, path: Path = OUT) -> None:
    """Atomic write: a crash mid-write never leaves a truncated file."""
    df = df.drop_duplicates("symbol", keep="last").sort_values("symbol")
    df = df.reindex(columns=COLUMNS).reset_index(drop=True)
    df["label_scheme"] = label_scheme(df)
    df["http_status"] = df["http_status"].astype("Int64")
    df["fetched_at"] = pd.to_datetime(df["fetched_at"], utc=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".parquet.tmp")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, path)


def is_final(note) -> bool:
    return isinstance(note, str) and note.startswith(FINAL_NOTES)


def fetch_all(symbols: list[str], path: Path = OUT, save_every: int = 25,
              client: Client | None = None) -> pd.DataFrame:
    """Fetch every symbol not already final in `path`; resumable.

    Saves every `save_every` symbols, and on the way out whatever happens.
    Raises Blocked (after saving) if NSE answers 403.
    """
    done = load(path)
    have = set(done.loc[done["note"].map(is_final), "symbol"]) if len(done) else set()
    todo = [s for s in dict.fromkeys(symbols) if s not in have]
    log.info("%d symbols requested, %d already final, %d to fetch",
             len(set(symbols)), len(set(symbols) & have), len(todo))

    client = client or Client()
    new: list[dict] = []
    t0 = time.monotonic()

    def flush():
        nonlocal done, new
        if new:
            parts = [x for x in (done, pd.DataFrame(new)) if len(x)]
            done = pd.concat(parts, ignore_index=True)
            save(done, path)
            done = load(path)
            new = []

    try:
        for i, sym in enumerate(todo, 1):
            new.append(fetch_symbol(client, sym))
            if i % save_every == 0:
                flush()
                rate = i / max(time.monotonic() - t0, 1e-9)
                log.info("%d/%d fetched (%.2f symbols/s, %d requests, ~%.0f min left)",
                         i, len(todo), rate, client.requests,
                         (len(todo) - i) / max(rate, 1e-9) / 60)
    finally:
        flush()
    return done
