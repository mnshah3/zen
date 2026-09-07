"""Symbol to company-name lookup.

The bhavcopy carries tickers, not names -- it will tell you ANDHRAPAP moved
but never that this is Andhra Paper. Without names we cannot connect a story
about a company to what its stock actually did, which is the whole point of
the brief's third section.

NSE publishes the current equity list as a free CSV. It covers listed names
only, so it is used strictly for display and headline matching, never for
building a historical universe -- doing that would reintroduce exactly the
survivorship bias the archive exists to avoid.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from pathlib import Path

import requests

from zen.data.bhavcopy import _session

log = logging.getLogger(__name__)

URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
CACHE = Path("state/symbol_names.json")

# Words that carry no identifying information when matching a headline.
_STOP = {"limited", "ltd", "the", "india", "indian", "company", "corporation",
         "corp", "industries", "enterprises", "and", "of", "co"}


def refresh() -> dict[str, str]:
    """Download the current NSE equity list and cache symbol -> name."""
    s = _session()
    r = s.get(URL, timeout=30)
    r.raise_for_status()

    reader = csv.DictReader(io.StringIO(r.text))
    mapping = {}
    for row in reader:
        sym = (row.get("SYMBOL") or "").strip()
        name = (row.get("NAME OF COMPANY") or "").strip()
        if sym and name:
            mapping[sym] = name

    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(mapping, indent=0, sort_keys=True))
    log.info("cached %d symbol names", len(mapping))
    return mapping


def load(refresh_if_missing: bool = True) -> dict[str, str]:
    if CACHE.exists():
        try:
            return json.loads(CACHE.read_text())
        except json.JSONDecodeError:
            log.warning("symbol name cache unreadable; refetching")
    if refresh_if_missing:
        try:
            return refresh()
        except Exception as e:
            log.warning("could not fetch symbol names: %s", e)
    return {}


def tokens(name: str) -> list[str]:
    """Distinctive words in a company name, for headline matching."""
    words = [w.strip(".,&()").lower() for w in name.split()]
    return [w for w in words if len(w) > 3 and w not in _STOP]


def find_in_text(text: str, names: dict[str, str],
                 symbols: list[str]) -> list[str]:
    """Which of `symbols` are plausibly mentioned in `text`.

    Matching is on the leading distinctive word of the company name rather
    than the ticker, since headlines say "Andhra Paper", never "ANDHRAPAP".
    """
    low = f" {text.lower()} "
    hits = []
    for sym in symbols:
        name = names.get(sym)
        if not name:
            continue
        toks = tokens(name)
        if toks and toks[0] in low:
            hits.append(sym)
    return hits
