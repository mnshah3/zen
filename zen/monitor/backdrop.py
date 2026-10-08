"""The market backdrop for the morning brief: index closes and sector moves from our own
archive, and an overnight global strip from free public sources.

Indian indices come from the `indices` table zen fills every evening from NSE, so they are
the same numbers the rest of the archive uses. The global strip is a convenience, not
research data: each figure carries the date it is from, anything older than MAX_AGE days is
dropped, and any source that fails simply leaves its line out. The brief goes out either way.

Global sources, both free and keyless:
  FRED (Federal Reserve Bank of St. Louis), fredgraph.csv
      SP500 and NASDAQCOM index closes, DGS10 (US 10-year Treasury yield, percent),
      DCOILBRENTEU (Brent, USD a barrel), VIXCLS (CBOE volatility index)
  Frankfurter (ECB reference rates), api.frankfurter.dev
      USD/INR
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import date, timedelta

import requests

log = logging.getLogger(__name__)

HEADLINE = ["Nifty 50", "Nifty Bank", "Nifty Midcap 150", "Nifty Smallcap 250"]
SECTORS = ["Nifty Auto", "Nifty Bank", "Nifty Energy", "Nifty FMCG", "Nifty IT", "Nifty Infrastructure",
           "Nifty Metal", "Nifty Pharma", "Nifty PSU Bank", "Nifty Realty"]
MAX_AGE = 5          # calendar days
TIMEOUT = 8
FRED = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}&cosd={start}"
FX = "https://api.frankfurter.dev/v1/{start}..?base=USD&symbols=INR"

# label, FRED id, kind: "level" (show % change) or "yield" (show change in basis points)
GLOBAL = [
    ("S&P 500", "SP500", "level"),
    ("Nasdaq", "NASDAQCOM", "level"),
    ("US 10-year", "DGS10", "yield"),
    ("Brent", "DCOILBRENTEU", "level"),
    ("VIX", "VIXCLS", "level"),
]


# --------------------------------------------------------------------------- Indian indices

def indices(con, asof) -> dict:
    """{'headline': [...], 'sectors': [...], 'nifty_pe': float | None} for the session `asof`.
    Each row: name, close, pct (percent change on the previous close)."""
    if asof is None:
        return {}
    names = sorted(set(HEADLINE + SECTORS))
    try:
        df = con.execute(
            f"""
            SELECT index_name, date, close, pct_change, pe
            FROM indices
            WHERE index_name IN ({", ".join("?" * len(names))}) AND date <= ? AND date >= ?
            ORDER BY index_name, date
            """, [*names, asof, asof - timedelta(days=10)]).df()
    except Exception as e:
        log.warning("index read failed: %s", e)
        return {}
    if df.empty:
        return {}
    rows = {}
    for name, g in df.groupby("index_name"):
        g = g.sort_values("date")
        last = g.iloc[-1]
        d = last["date"].date() if hasattr(last["date"], "date") else last["date"]
        if d != asof:
            continue                       # no print for this session: leave it out, never carry a stale one
        pct = last["pct_change"]
        if (pct is None or pct != pct) and len(g) > 1 and g.iloc[-2]["close"]:
            pct = (last["close"] / g.iloc[-2]["close"] - 1) * 100
        rows[name] = {"name": name, "close": float(last["close"]),
                      "pct": float(pct) if pct == pct and pct is not None else None,
                      "pe": float(last["pe"]) if last["pe"] == last["pe"] and last["pe"] is not None else None}
    out = {"headline": [rows[n] for n in HEADLINE if n in rows],
           "sectors": sorted((rows[n] for n in SECTORS if n in rows and rows[n]["pct"] is not None),
                             key=lambda r: -r["pct"]),
           "nifty_pe": (rows.get("Nifty 50") or {}).get("pe")}
    return out


# --------------------------------------------------------------------------- global strip

def _fred(sid: str, start: date) -> list[tuple[date, float]]:
    r = requests.get(FRED.format(sid=sid, start=start.isoformat()), timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for row in csv.reader(io.StringIO(r.text)):
        if len(row) < 2 or not row[0][:1].isdigit():
            continue
        try:
            out.append((date.fromisoformat(row[0]), float(row[1])))
        except ValueError:
            continue                        # FRED writes "." or "" for a missing day
    return out


def _fx(start: date) -> list[tuple[date, float]]:
    r = requests.get(FX.format(start=start.isoformat()), timeout=TIMEOUT)
    r.raise_for_status()
    rates = r.json().get("rates") or {}
    return sorted((date.fromisoformat(d), float(v["INR"])) for d, v in rates.items() if "INR" in v)


def _item(label: str, kind: str, pts: list[tuple[date, float]], today: date) -> dict | None:
    if len(pts) < 2:
        return None
    (d0, v0), (d1, v1) = pts[-2], pts[-1]
    if (today - d1).days > MAX_AGE or not v0:
        return None
    change = (v1 - v0) * 100 if kind == "yield" else (v1 / v0 - 1) * 100
    return {"label": label, "value": v1, "change": change, "kind": kind, "date": d1}


def global_backdrop(today: date | None = None) -> list[dict]:
    """The overnight strip, in a fixed order. Never raises."""
    today = today or date.today()
    start = today - timedelta(days=14)
    out = []
    for label, sid, kind in GLOBAL:
        try:
            it = _item(label, kind, _fred(sid, start), today)
        except Exception as e:
            log.warning("global backdrop: %s unavailable (%s)", label, e)
            it = None
        if it:
            out.append(it)
        if label == "US 10-year":
            try:
                fx = _item("USD/INR", "fx", _fx(start), today)
            except Exception as e:
                log.warning("global backdrop: USD/INR unavailable (%s)", e)
                fx = None
            if fx:
                out.append(fx)
    return out
