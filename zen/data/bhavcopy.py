"""Download and normalise NSE daily bhavcopy.

The bhavcopy is a full snapshot of everything that traded on a given day,
including securities that were later delisted. Assembling the archive is what
makes the universe survivorship-bias-free -- it records what existed *then*,
not what survives now.

NSE changed format in July 2024. Both are handled and normalised to one schema.
"""

from __future__ import annotations

import io
import logging
import zipfile
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests

log = logging.getLogger(__name__)

# NSE moved to the UDiFF format on this date; older dates use the legacy layout.
UDIFF_CUTOVER = date(2024, 7, 8)

BASE = "https://nsearchives.nseindia.com"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}

# normalised output schema
COLUMNS = ["date", "symbol", "series", "isin_code", "open", "high", "low",
           "close", "prev_close", "volume", "turnover", "trades"]


def _session() -> requests.Session:
    """NSE rejects bare requests; prime cookies against the homepage first."""
    s = requests.Session()
    s.headers.update(HEADERS)
    try:
        s.get("https://www.nseindia.com", timeout=15)
    except requests.RequestException as e:
        log.warning("cookie priming failed (%s); continuing anyway", e)
    return s


def _url(d: date) -> str:
    if d >= UDIFF_CUTOVER:
        return f"{BASE}/content/cm/BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"
    mon = d.strftime("%b").upper()
    return (f"{BASE}/content/historical/EQUITIES/{d:%Y}/{mon}/"
            f"cm{d:%d}{mon}{d:%Y}bhav.csv.zip")


def _file_date_matches(df: pd.DataFrame, d: date) -> bool:
    """Cheap guard against NSE serving a file for the wrong session.

    The stamp inside the file is not parsed for its value -- NSE has used at
    least '13-Jul-20' and '14-JUL-2020' on consecutive days -- only compared
    loosely against the date we asked for.
    """
    col = "TradDt" if "TradDt" in df.columns else "TIMESTAMP"
    if col not in df.columns or df.empty:
        return True
    raw = str(df[col].dropna().iloc[0]).upper()
    day, mon = f"{d.day:02d}", d.strftime("%b").upper()
    return (day in raw and mon in raw) or d.isoformat() in raw


def _normalise(df: pd.DataFrame, d: date) -> pd.DataFrame:
    """Map either NSE layout onto COLUMNS, equity series only.

    The date column is taken from the requested date rather than parsed out of
    the file. NSE's own stamp formatting is not stable across days, and the
    date we asked for is authoritative anyway.
    """
    df.columns = [c.strip() for c in df.columns]

    if not _file_date_matches(df, d):
        log.warning("%s: file's internal date looks wrong; skipping", d)
        return pd.DataFrame(columns=COLUMNS)

    if "TckrSymb" in df.columns:  # UDiFF
        df = df[df["FinInstrmTp"].isin(["STK", "EQ"])] if "FinInstrmTp" in df else df
        out = pd.DataFrame({
            "date": d,
            "symbol": df["TckrSymb"].str.strip(),
            "series": df["SctySrs"].str.strip(),
            "isin_code": df.get("ISIN", pd.Series(index=df.index, dtype=object)),
            "open": df["OpnPric"], "high": df["HghPric"], "low": df["LwPric"],
            "close": df["ClsPric"], "prev_close": df["PrvsClsgPric"],
            "volume": df["TtlTradgVol"], "turnover": df["TtlTrfVal"],
            "trades": df.get("TtlNbOfTxsExctd"),
        })
    else:  # legacy
        out = pd.DataFrame({
            "date": d,
            "symbol": df["SYMBOL"].str.strip(),
            "series": df["SERIES"].str.strip(),
            "isin_code": df.get("ISIN"),
            "open": df["OPEN"], "high": df["HIGH"], "low": df["LOW"],
            "close": df["CLOSE"], "prev_close": df["PREVCLOSE"],
            "volume": df["TOTTRDQTY"], "turnover": df["TOTTRDVAL"],
            "trades": df.get("TOTALTRADES"),
        })

    # EQ = normal equity, BE = trade-to-trade. Both are real equity lines.
    out = out[out["series"].isin(["EQ", "BE"])].copy()
    for c in ["open", "high", "low", "close", "prev_close", "volume", "turnover", "trades"]:
        out[c] = pd.to_numeric(out[c], errors="coerce")
    # Stored parquet holds datetime64; a plain date object here leaves the
    # column as `object` and a later concat mixes date with Timestamp. That
    # mixture is unsortable under pandas 3, so the daily job dies at the point
    # where new rows meet old ones. Match the stored type at the source.
    out["date"] = pd.to_datetime(out["date"])
    return out[COLUMNS].reset_index(drop=True)


def fetch_day(d: date, raw_dir: Path | None = None,
              session: requests.Session | None = None) -> pd.DataFrame | None:
    """Return one day's normalised bhavcopy, or None if NSE was closed."""
    s = session or _session()
    url = _url(d)
    try:
        r = s.get(url, timeout=30)
    except requests.RequestException as e:
        log.warning("%s: request failed (%s)", d, e)
        return None

    # Weekends and holidays simply have no file.
    if r.status_code == 404:
        log.info("%s: no file (market holiday or weekend)", d)
        return None
    if r.status_code != 200 or not r.content[:2] == b"PK":
        log.warning("%s: unexpected response %s", d, r.status_code)
        return None

    if raw_dir:
        raw_dir.mkdir(parents=True, exist_ok=True)
        (raw_dir / f"{d:%Y%m%d}.zip").write_bytes(r.content)

    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        name = z.namelist()[0]
        df = pd.read_csv(z.open(name), low_memory=False)

    return _normalise(df, d)


def fetch_range(start: date, end: date, raw_dir: Path | None = None) -> pd.DataFrame:
    """Fetch every trading day in [start, end]. Skips weekends without a request."""
    s = _session()
    frames, d = [], start
    while d <= end:
        if d.weekday() < 5:  # Mon-Fri
            df = fetch_day(d, raw_dir=raw_dir, session=s)
            if df is not None and not df.empty:
                frames.append(df)
                log.info("%s: %d rows", d, len(df))
        d += timedelta(days=1)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=COLUMNS)
