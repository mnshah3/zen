"""NSE's own count of each company's issued shares, from its current quote page.

    tradeInfo.{issuedSize, faceValue, totalMarketCap, lastPrice} of the endpoint zen.data.industry reads

NSE's pledge page gives issued shares only for companies with promoters, and only as of the last
shareholding date; a company with no promoter (Eternal, BSE, Paytm, Swiggy) has none there. This covers
every traded symbol and is the count NSE itself multiplies by the price for its market cap.
The price and market cap are kept so that the count can be checked against NSE's own product.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import pandas as pd

from zen.data.industry import Blocked, Client, SERIES_ORDER, TRANSIENT_TRIES

log = logging.getLogger(__name__)

OUT = Path("data/reference/issued_nse.parquet")
COLUMNS = ["symbol", "series", "issued", "face_value", "nse_mcap", "last_price", "fetched_at", "note"]


def _num(v) -> float | None:
    try:
        x = float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return x if x == x else None


def parse(payload: dict) -> dict | None:
    """The trade fields of one getSymbolData payload, or None when it has no issued count."""
    rows = payload.get("equityResponse") if isinstance(payload, dict) else None
    if not rows:
        return None
    e = rows[0] or {}
    t, m = e.get("tradeInfo") or {}, e.get("metaData") or {}
    issued = _num(t.get("issuedSize"))
    if not issued or issued <= 0:
        return None
    return {"series": m.get("series"), "issued": issued, "face_value": _num(t.get("faceValue")),
            "nse_mcap": _num(t.get("totalMarketCap")), "last_price": _num(t.get("lastPrice"))}


def fetch_symbol(client: Client, symbol: str, series: tuple = SERIES_ORDER) -> dict:
    """One symbol's row; `note` is ok, or why it has no count."""
    base = {"symbol": symbol, "series": None, "issued": None, "face_value": None, "nse_mcap": None,
            "last_price": None, "fetched_at": pd.Timestamp.now(tz="UTC").tz_localize(None), "note": "unavailable"}
    for s in series:
        for attempt in range(TRANSIENT_TRIES):
            try:
                r = client.get(symbol, s)
            except Exception as e:                                # noqa: BLE001 - a timeout: try again, then record it
                base["note"] = f"error: {type(e).__name__}"
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code == 403:
                raise Blocked(f"NSE answered 403 at {symbol}")
            if r.status_code in (429, 500, 502, 503, 504):
                base["note"] = f"error: {r.status_code}"
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code != 200:
                base["note"] = "unavailable"
                break
            try:
                got = parse(r.json())
            except ValueError:
                got = None
            if got:
                return {**base, **got, "note": "ok"}
            base["note"] = "unavailable"
            break
    return base


def load(path: Path = OUT) -> pd.DataFrame:
    return pd.read_parquet(path) if Path(path).exists() else pd.DataFrame(columns=COLUMNS)


def save(df: pd.DataFrame, path: Path = OUT) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".parquet.tmp")
    df[COLUMNS].sort_values("symbol").to_parquet(tmp, index=False)
    tmp.replace(path)


def fetch(symbols: list[str], path: Path = OUT, save_every: int = 50, client: Client | None = None) -> pd.DataFrame:
    """Fetch `symbols`, replacing their rows in `path` and keeping the rest; saves as it goes, and on the way
    out whatever happens. A failed fetch does not replace a count already held. Raises Blocked on a 403."""
    done = load(path)
    client = client or Client()
    new: list[dict] = []
    t0 = time.monotonic()

    def flush():
        nonlocal done, new
        if not new:
            return
        fresh = pd.DataFrame(new, columns=COLUMNS)
        good = fresh[fresh["note"] == "ok"]
        held = set(done.loc[done["note"] == "ok", "symbol"]) if len(done) else set()
        keep_new = pd.concat([good, fresh[(fresh["note"] != "ok") & ~fresh["symbol"].isin(held)]], ignore_index=True)
        rest = done[~done["symbol"].isin(keep_new["symbol"])] if len(done) else done
        done = pd.concat([x for x in (rest, keep_new) if len(x)], ignore_index=True)
        save(done, path)
        new = []

    try:
        for i, sym in enumerate(dict.fromkeys(symbols), 1):
            new.append(fetch_symbol(client, sym))
            if i % save_every == 0:
                flush()
                log.info("%d/%d fetched (%.2f symbols/s, ~%.0f min left)", i, len(symbols), i / max(time.monotonic() - t0, 1e-9),
                         (len(symbols) - i) / max(i / max(time.monotonic() - t0, 1e-9), 1e-9) / 60)
    finally:
        flush()
    return done
