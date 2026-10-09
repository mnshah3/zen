"""Recent news headlines about one company, from Google News search (the morning brief's own
keyless source).

A search by name returns stories that only mention the company in passing, or are about a
different company with a similar name. So a headline is kept only when it names the company
itself, as one phrase: the name without its legal suffix ("Tata Consultancy Services"), a short
form that stays distinctive (its first two words, when the name is three or more and the two
are not generic), or the NSE symbol in capitals as a whole word ("TCS", never "oil" for OIL).
Pages that are not news are dropped:

  * stock-quote, option-chain and price-prediction pages;
  * outlets whose stories a program writes from the day's price move or a rating model
    (BOT_SOURCES), and items whose outlet is only a web address or that cite a Yahoo-style ticker
    ("BAJAJ-AUTO.NS"), the marks of machine-made pages;
  * photo galleries: a short caption of one part of a product ("Hero MotoCorp Xpulse 210
    Grille"), a run of such captions from one outlet, or a vehicle site's catalogue entry ("Hero
    MotoCorp Xoom 125 Color Matt Giallo Lime"; those sites write their news in sentence case).

When several outlets carry the same story only the newest is kept, so the panel shows different
stories rather than one six times. Headlines come newest first.

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


NOT_NEWS = re.compile(r"stock price, news, quote|share price (today|live)|share price update|stock quote"
                      r"|\b(share|stock) price( (nse|bse|today|live))?\s*$|\bprice prediction\b"
                      r"|\bprice target 20\d\d\b|\boption chain\b"
                      r"|\bphotos in india\b|\b(image|photo)s? gallery\b|\bwallpapers?\b", re.I)
# outlets that publish machine-written notes on each day's price move or a rating model's output
BOT_SOURCES = {"ad hoc news", "marketsmojo", "univest"}
TICKER = re.compile(r"\b[A-Z0-9&-]+\.(NS|BO)\b")
GALLERY_HEAD, GALLERY_TAIL = 4, 3      # shared leading words, and the longest tail that still reads as a caption
# a short title that ends in the name of a vehicle part or a view is a picture's caption
CAPTION = re.compile(r"\b(three quarter|side stand|grille|head ?lamps?|tail ?lamps?|tail ?lights?|instrument cluster"
                     r"|(alloy |front |rear )?wheels?|(front|rear|side|top) (view|profile))\s*$", re.I)
CAPTION_WORDS = 9
# vehicle sites whose model catalogue (colours, parts, views) shows up in news search; their own
# headlines are in sentence case, their catalogue entries capitalise every word
CATALOGUE_SOURCES = {"autocar india", "bikewale", "carwale", "bikedekho", "cardekho", "zigwheels"}
STOP = {"a", "an", "the", "and", "or", "of", "in", "on", "at", "to", "for", "with", "by", "from", "as", "is", "its",
        "it", "after", "over", "into", "up", "us", "rs", "inr", "crore", "cr", "lakh", "share", "shares", "stock",
        "stocks", "ltd", "limited"}


def _catalogue_entry(title: str, src: str) -> bool:
    if (src or "").strip().lower() not in CATALOGUE_SOURCES:
        return False
    words = [w for w in title.split() if w[:1].isalpha()]
    return len(title.split()) <= 10 and bool(words) and all(w[0].isupper() for w in words)


def _bad_source(src: str) -> bool:
    s = (src or "").strip().lower()
    return s in BOT_SOURCES or s.startswith(("http://", "https://", "www."))


def _galleries(items: list[dict]) -> set:
    """(outlet, leading words) of photo-gallery runs: two or more items from one outlet that share
    their first GALLERY_HEAD words and differ only in a caption of at most GALLERY_TAIL words."""
    groups: dict = {}
    for x in items:
        w = _phrase(x["t"]).split()
        if len(w) > GALLERY_HEAD:
            groups.setdefault((x["src"], tuple(w[:GALLERY_HEAD])), []).append(len(w) - GALLERY_HEAD)
    return {k for k, tails in groups.items() if len(tails) >= 2 and max(tails) <= GALLERY_TAIL}


def _gallery_key(x: dict) -> tuple:
    return (x["src"], tuple(_phrase(x["t"]).split()[:GALLERY_HEAD]))


def _words(title: str, core: str) -> set:
    """A headline's distinctive words: no stop words, none of the company's own name, '1,300' read
    as '1300', a plural's final s dropped."""
    own = set(re.findall(r"[a-z0-9]+", _phrase(core)))
    t = re.sub(r"(?<=\d),(?=\d)", "", _phrase(title))
    out = set()
    for w in re.findall(r"[a-z0-9]+", t):
        if len(w) < 2 or w in STOP or w in own:
            continue
        out.add(w[:-1] if len(w) > 3 and w.endswith("s") else w)
    return out


def same_story(a: set, b: set) -> bool:
    """Two headlines tell the same story when they share at least three distinctive words and those
    make up half or more of the shorter one."""
    n = len(a & b)
    return n >= 3 and n / min(len(a), len(b)) >= 0.5


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
        if (not title or _bad_source(src) or NOT_NEWS.search(title) or TICKER.search(title)
                or (len(title.split()) <= CAPTION_WORDS and CAPTION.search(title)) or _catalogue_entry(title, src)
                or not mentions(title, name, symbol)):
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
    gal = _galleries(out)
    out = [x for x in out if _gallery_key(x) not in gal]
    out.sort(key=lambda x: x["dt"] or "", reverse=True)
    kept, kept_words = [], []
    for x in out:
        w = _words(x["t"], core)
        if any(same_story(w, k) for k in kept_words):
            continue
        kept.append(x)
        kept_words.append(w)
    return kept[:limit]


def for_companies(companies: dict[str, str], days: int = 7, limit: int = 6, pause: float = 0.6) -> dict[str, list]:
    """{symbol: headlines} for {symbol: name}, one search each, politely spaced."""
    out = {}
    for i, (sym, name) in enumerate(companies.items()):
        if i:
            time.sleep(pause)
        out[sym] = headlines(name, sym, days=days, limit=limit)
    return out
