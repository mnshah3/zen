"""Recent news headlines about one company, from Google News search (the morning brief's own
keyless source).

A search by name returns stories that only mention the company in passing, or are about a
different company with a similar name. So a headline is kept only when it names the company
itself, as one phrase: the name without its legal suffix ("Tata Consultancy Services"), a short
form that stays distinctive (its first two words, when the name is three or more and the two
are not generic), or the NSE symbol in capitals as a whole word ("TCS", never "oil" for OIL).
Stock-quote listing pages are not news and are dropped. Headlines are de-duplicated (outlets
re-publish the same story), newest first.

Only the headline, the outlet, the link and the time are kept: the dashboard links out, it
does not republish articles. Network failures return an empty list, never an error.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from urllib.parse import quote_plus

import feedparser
import requests

GNEWS = "https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"
UA = "Mozilla/5.0 (compatible; zen-research/1.0)"
SUFFIX = re.compile(r"\b(limited|ltd\.?|private|pvt\.?|corporation|corp\.?|company|co\.?|inc\.?|plc)\b\.?", re.I)
GENERIC = {"india", "indian", "the", "and", "of", "industries", "bank", "finance", "financial", "tata", "adani",
           "bajaj", "hindustan", "national", "general", "new", "first", "global", "international", "power",
           "energy", "infra", "infrastructure", "steel", "motors", "capital", "services", "technologies", "systems"}


def core_name(name: str) -> str:
    """The company's name without its legal suffix or brackets: 'Tata Consultancy Services'."""
    n = re.sub(r"\(.*?\)", " ", name or "")
    n = SUFFIX.sub(" ", n)
    return " ".join(n.split()).strip(" .,-")


def _phrase(text: str) -> str:
    """Lower case with '&' read as 'and' and runs of spaces collapsed; punctuation is kept, so a
    name never matches across it ('cheap oil: india's' does not name Oil India)."""
    t = (text or "").lower().replace("’", "'").replace("&", " and ")
    return " ".join(t.split())


def _has(text: str, phrase: str) -> bool:
    return bool(phrase) and re.search(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])", text) is not None


def _short_form(core: str) -> str | None:
    words = core.split()
    if len(words) >= 3 and not ({w.lower() for w in words[:2]} <= GENERIC):
        return " ".join(words[:2])
    return None


NOT_NEWS = re.compile(r"stock price, news, quote|share price (today|live)|share price update|stock quote", re.I)


def mentions(title: str, name: str, symbol: str) -> bool:
    """Whether a headline names the company: its name or a distinctive short form as one phrase,
    or its NSE symbol written in capitals as a whole word."""
    t = _phrase(title)
    core = core_name(name)
    if _has(t, _phrase(core)):
        return True
    short = _short_form(core)
    if short and _has(t, _phrase(short)):
        return True
    sym = (symbol or "").upper()
    return len(sym) >= 3 and re.search(r"(?<![A-Za-z0-9])" + re.escape(sym) + r"(?![A-Za-z0-9])", title or "") is not None


def _key(title: str) -> str:
    return re.sub(r"[^a-z0-9]", "", title.lower())[:80]


def headlines(name: str, symbol: str, days: int = 7, limit: int = 6, timeout: int = 15) -> list[dict]:
    """[{t, src, u, dt}] newest first: headline, outlet, link, ISO time (UTC)."""
    core = core_name(name)
    if not core:
        return []
    url = GNEWS.format(q=quote_plus(f'"{core}" when:{int(days)}d'))
    try:
        r = requests.get(url, headers={"User-Agent": UA, "Accept": "application/rss+xml,*/*"}, timeout=timeout)
        r.raise_for_status()
        feed = feedparser.parse(r.content)
    except Exception:                                                # noqa: BLE001
        return []
    out, seen = [], set()
    for e in feed.entries or []:
        raw = (e.get("title") or "").strip()
        src = ((e.get("source") or {}).get("title") or "").strip()
        title = raw[: -len(src) - 3].strip() if src and raw.endswith(" - " + src) else raw
        if not title or NOT_NEWS.search(title) or not mentions(title, name, symbol):
            continue
        k = _key(title)
        if k in seen:
            continue
        seen.add(k)
        when = None
        if e.get("published_parsed"):
            when = datetime.fromtimestamp(time.mktime(e.published_parsed), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        link = e.get("link") or ""
        if not link.startswith("https://"):
            continue
        out.append({"t": title, "src": src or None, "u": link, "dt": when})
    out.sort(key=lambda x: x["dt"] or "", reverse=True)
    return out[:limit]


def for_companies(companies: dict[str, str], days: int = 7, limit: int = 6, pause: float = 0.6) -> dict[str, list]:
    """{symbol: headlines} for {symbol: name}, one search each, politely spaced."""
    out = {}
    for i, (sym, name) in enumerate(companies.items()):
        if i:
            time.sleep(pause)
        out[sym] = headlines(name, sym, days=days, limit=limit)
    return out
