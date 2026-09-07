"""Corporate actions, and the price adjustment they require.

Raw bhavcopy prices are unadjusted. A stock that splits from a face value of
Rs 10 to Rs 2 shows as an 80% fall on the ex-date, and a 1:1 bonus shows as a
50% fall. Neither is a loss to anyone holding it, but a return series computed
from raw closes records both as catastrophes.

This matters more here than in most places. The strategy measures forward
returns over 3 to 24 months, and a single unadjusted split inside that window
does not just add noise -- it reliably drags the measured return toward a large
negative number. Since companies tend to split AFTER the price has risen, the
error correlates with exactly the stocks a momentum or quality screen would
pick. Left uncorrected it would make good picks look terrible, which is the
kind of bias that quietly kills a working strategy.

Dividends are deliberately NOT adjusted for. Indian dividend yields are small
relative to the price moves being measured, the data is noisy, and adjusting
badly is worse than not adjusting. This is a stated limitation rather than an
oversight: reported returns are price returns, not total returns, and are
therefore slightly understated.

Conventions, which are easy to get backwards:
  Face value split from X to Y  ->  price multiplied by Y/X
  Bonus a:b  (a new shares for every b held)  ->  price multiplied by b/(a+b)
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from zen.data.bhavcopy import _session

log = logging.getLogger(__name__)

API = ("https://www.nseindia.com/api/corporates-corporateActions"
       "?index=equities&from_date={frm}&to_date={to}")
WARMUP = "https://www.nseindia.com/companies-listing/corporate-filings-actions"

PARQUET_DIR = Path("data/corpactions")
COLUMNS = ["ex_date", "symbol", "isin", "company", "action", "subject", "factor"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS corpactions (
    ex_date  DATE    NOT NULL,
    symbol   VARCHAR NOT NULL,
    isin     VARCHAR,
    company  VARCHAR,
    action   VARCHAR,
    subject  VARCHAR,
    factor   DOUBLE,
    PRIMARY KEY (ex_date, symbol, subject)
);
"""

# "From Rs 10/- Per Share To Re 1/- Per Share" -- note Rs and Re both appear,
# spacing is inconsistent, and the value may carry decimals.
_SPLIT = re.compile(
    r"from\s+rs?e?\.?\s*(\d+(?:\.\d+)?)\s*/?-?\s*per\s+share\s+to\s+"
    r"rs?e?\.?\s*(\d+(?:\.\d+)?)", re.I)

# "Bonus 3:1" -- a new shares for every b held.
_BONUS = re.compile(r"bonus\s+(\d+)\s*:\s*(\d+)", re.I)


def parse_factor(subject: str) -> tuple[str, float | None]:
    """Return (action, price multiplier on the ex-date).

    A factor below 1 means the quoted price steps down without any loss of
    value to the holder.
    """
    s = (subject or "").strip()
    low = s.lower()

    if "split" in low or "sub-division" in low or "subdivision" in low:
        m = _SPLIT.search(s)
        if m:
            old, new = float(m.group(1)), float(m.group(2))
            if old > 0 and new > 0 and new < old:
                return "split", new / old
        return "split", None

    if "bonus" in low:
        m = _BONUS.search(s)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a > 0 and b > 0:
                return "bonus", b / (a + b)
        return "bonus", None

    if "dividend" in low:
        # Not adjusted for -- see the module docstring.
        return "dividend", None
    if "rights" in low:
        # Rights need the subscription price and ratio, which this feed does
        # not carry reliably. Recorded so the event is visible, not adjusted.
        return "rights", None
    return "other", None


def _d(raw: str) -> date | None:
    for fmt in ("%d-%b-%Y", "%d-%B-%Y"):
        try:
            return datetime.strptime((raw or "").strip(), fmt).date()
        except ValueError:
            continue
    return None


def fetch(start: date, end: date, session=None, chunk_days: int = 365) -> pd.DataFrame:
    """Corporate actions with an ex-date in the range.

    Requested in chunks: the endpoint silently truncates long ranges, and a
    short result would look like "no splits happened".
    """
    s = session or _session()
    s.get(WARMUP, timeout=25)

    frames, cur = [], start
    while cur <= end:
        stop = min(cur + timedelta(days=chunk_days), end)
        try:
            r = s.get(API.format(frm=f"{cur:%d-%m-%Y}", to=f"{stop:%d-%m-%Y}"), timeout=90)
            r.raise_for_status()
            payload = r.json()
        except Exception as e:
            log.warning("%s to %s: corp actions failed (%s)", cur, stop, e)
            cur = stop + timedelta(days=1)
            continue

        rows = payload if isinstance(payload, list) else payload.get("data", [])
        parsed = []
        for row in rows:
            ex = _d(row.get("exDate") or "")
            sym = (row.get("symbol") or "").strip()
            if not ex or not sym:
                continue
            subject = re.sub(r"\s+", " ", (row.get("subject") or "")).strip()
            action, factor = parse_factor(subject)
            parsed.append({
                "ex_date": ex, "symbol": sym,
                "isin": (row.get("isin") or "").strip() or None,
                "company": (row.get("comp") or "").strip() or None,
                "action": action, "subject": subject[:300], "factor": factor,
            })
        if parsed:
            frames.append(pd.DataFrame(parsed))
            log.info("%s to %s: %d actions", cur, stop, len(parsed))
        cur = stop + timedelta(days=1)

    if not frames:
        return pd.DataFrame(columns=COLUMNS)
    out = pd.concat(frames, ignore_index=True)
    return out.drop_duplicates(subset=["ex_date", "symbol", "subject"])[COLUMNS]


def ensure_schema(con) -> None:
    con.execute(SCHEMA)


def upsert(con, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    ensure_schema(con)
    con.register("incoming_ca", df)
    before = con.execute("SELECT count(*) FROM corpactions").fetchone()[0]
    con.execute("INSERT OR IGNORE INTO corpactions SELECT * FROM incoming_ca")
    after = con.execute("SELECT count(*) FROM corpactions").fetchone()[0]
    con.unregister("incoming_ca")
    return after - before


def write_parquet(df: pd.DataFrame, out_dir: Path = PARQUET_DIR) -> list[Path]:
    if df.empty:
        return []
    written = []
    for year, chunk in df.groupby(df["ex_date"].map(lambda d: f"{d:%Y}")):
        p = out_dir / f"{year}.parquet"
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            chunk = pd.concat([pd.read_parquet(p), chunk], ignore_index=True)
        chunk = chunk.drop_duplicates(subset=["ex_date", "symbol", "subject"], keep="last")
        chunk.to_parquet(p, index=False, compression="zstd")
        written.append(p)
    return written


def rebuild_from_parquet(con, out_dir: Path = PARQUET_DIR) -> int:
    ensure_schema(con)
    if not out_dir.exists():
        return 0
    con.execute("DELETE FROM corpactions")
    con.execute(f"INSERT INTO corpactions SELECT * FROM "
                f"read_parquet('{out_dir}/*.parquet', union_by_name=true)")
    return con.execute("SELECT count(*) FROM corpactions").fetchone()[0]


def adjustment_series(con, symbol: str) -> pd.DataFrame:
    """Cumulative back-adjustment factor per ex-date for one symbol.

    Prices BEFORE an ex-date are multiplied by the factor so the series is
    continuous. Adjusting backwards rather than forwards keeps the most recent
    price equal to the actual traded price, which is what a reader expects.
    """
    ensure_schema(con)
    acts = con.execute(
        "SELECT ex_date, factor FROM corpactions "
        "WHERE symbol = ? AND factor IS NOT NULL AND factor > 0 "
        "ORDER BY ex_date",
        [symbol],
    ).df()
    if acts.empty:
        return acts
    # Cumulative product of all factors at or after each date, applied to
    # everything before it.
    acts["cum"] = acts["factor"][::-1].cumprod()[::-1]
    return acts


def adjust_prices(con, symbol: str, prices: pd.DataFrame,
                  date_col: str = "date", price_cols=("close", "open", "high", "low")
                  ) -> pd.DataFrame:
    """Back-adjust a price series for splits and bonuses."""
    acts = adjustment_series(con, symbol)
    if acts.empty or prices.empty:
        return prices

    out = prices.copy()
    dates = pd.to_datetime(out[date_col])
    factor = pd.Series(1.0, index=out.index)
    for r in acts.itertuples():
        factor[dates < pd.Timestamp(r.ex_date)] *= r.factor
    for c in price_cols:
        if c in out:
            out[c] = out[c] * factor
    out["adj_factor"] = factor
    return out
