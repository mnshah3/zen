"""Marketaux: news that already knows which company it is about.

WHY THIS AND NOT MORE RSS

The RSS feeds give headlines. Whether a headline concerns a company we care
about has been inferred by keyword, which is why the theme matcher had to
distinguish "one mention of data centre is enough" from "one mention of power
is not". That inference is the weakest link in the brief.

Marketaux returns entities: for each article, the tickers it is actually about,
each with an exchange, an industry, a match score and a sentiment score. The
guessing goes away for anything this covers.

THE FREE TIER IS SMALL, AND THE SHAPE OF THE CODE FOLLOWS FROM IT

  100 requests per day, 3 articles per request.

Three hundred articles a day is ample for one brief, but the request budget is
not something to discover by exhausting it -- an exhausted budget on a Tuesday
means no enriched news until Wednesday, silently. So the budget is explicit,
counted, and enforced here rather than left to the API to refuse.

DEGRADES, NEVER FAILS

No key, a dead endpoint, a changed payload: every one returns an empty list and
the brief goes out on RSS alone, exactly as it does today. Nothing in the daily
email is allowed to depend on a third party being up.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger(__name__)

BASE = "https://api.marketaux.com/v1/news/all"
KEY_ENV = "MARKETAUX_KEY"

# Free tier: 100/day. A brief and a signal check both run daily, so this leaves
# most of the allowance unspent rather than racing the ceiling.
MAX_REQUESTS = 14
PER_REQUEST = 3          # free-tier hard cap; asking for more is silently capped

# Theme -> the search string that finds it. Marketaux's search takes boolean
# operators, so these are narrower than the RSS queries can be.
THEME_QUERIES = {
    "AI & data centres": "data center OR datacentre OR hyperscaler",
    "Nuclear": "nuclear OR reactor OR uranium",
    "Green energy & solar": "solar OR renewable OR electrolyser OR battery storage",
    "Water": "water treatment OR desalination",
    "Infrastructure": "order book OR infrastructure OR capex",
    "Defence": "defence OR defense OR indigenisation",
    "Import substitution": "import substitution OR PLI OR manufacturing",
}


# Exchange is not in the `exchange` field -- Marketaux leaves that null for
# Indian equities and encodes the venue in the symbol suffix instead.
SUFFIX_EXCHANGE = {"NS": "NSE", "NSI": "NSE", "BO": "BSE", "BSE": "BSE"}


def _exchange_of(symbol: str) -> str:
    tail = symbol.rsplit(".", 1)[-1].upper() if "." in symbol else ""
    return SUFFIX_EXCHANGE.get(tail, "")


def _norm_name(name: str) -> str:
    """Company name reduced to comparable tokens."""
    import re as _re
    n = _re.sub(r"[^a-z0-9 ]", " ", (name or "").lower())
    drop = {"limited", "ltd", "inc", "incorporated", "plc", "corp", "corporation",
            "company", "co", "the", "and", "india", "indian", "private", "pvt"}
    return " ".join(t for t in n.split() if t and t not in drop)


_NAMES: dict | None = None


def _archive_names() -> dict:
    """symbol -> company name, from our own archive.

    Needed because Marketaux's entity matcher is unreliable on Indian tickers.
    It tagged a Macy's earnings story with M&M.NS and M&MFIN.NS -- both named
    "Macy's, Inc." in the payload, both scoring 64.7 -- because the US ticker
    is "M". Accepting that would have put Macy's results in the brief as
    Mahindra & Mahindra news.

    We already know what these symbols are, from filings the companies made
    themselves, so the claim is checkable rather than trusted.
    """
    global _NAMES
    if _NAMES is not None:
        return _NAMES
    _NAMES = {}
    try:
        import duckdb
        con = duckdb.connect()
        rows = con.execute("""
            SELECT symbol, any_value(company) AS company FROM (
                SELECT symbol, company FROM
                  read_parquet('data/financials/legacy_2*.parquet', union_by_name=true)
                  WHERE company IS NOT NULL
                UNION ALL
                SELECT symbol, company FROM
                  read_parquet('data/announcements/**/*.parquet', union_by_name=true)
                  WHERE company IS NOT NULL)
            GROUP BY symbol""").fetchall()
        con.close()
        _NAMES = {sym: _norm_name(co) for sym, co in rows}
    except Exception as e:                                       # noqa: BLE001
        log.warning("could not load archive names (%s); entity names unverified", e)
    return _NAMES


def _name_agrees(symbol: str, claimed: str) -> bool:
    """Does the provider's company name match what our archive calls it?

    Unknown symbols pass: the archive does not cover every listed name, and
    rejecting on absence would silently discard real coverage. Known symbols
    with a clearly different name are rejected.
    """
    from difflib import SequenceMatcher
    base = symbol.split(".")[0].upper()
    ours = _archive_names().get(base)
    if not ours:
        return True
    theirs = _norm_name(claimed)
    if not theirs:
        return True
    if set(ours.split()) & set(theirs.split()):
        return True
    return SequenceMatcher(None, ours, theirs).ratio() >= 0.6


@dataclass
class Entity:
    symbol: str
    name: str
    exchange: str = ""
    industry: str = ""
    match_score: float = 0.0
    sentiment: float | None = None


@dataclass
class Story:
    title: str
    url: str
    source: str
    published: datetime
    summary: str = ""
    entities: list[Entity] = field(default_factory=list)
    theme: str = ""

    @property
    def _indian(self) -> list:
        return [e for e in self.entities if e.exchange in ("NSE", "BSE")]

    @property
    def indian_symbols(self) -> list[str]:
        """Tickers on an Indian exchange, deduplicated, NSE preferred.

        A company is routinely returned twice, once per venue -- M&M.NS and
        M&M.BO -- and the brief should name it once.
        """
        seen, out = set(), []
        for e in sorted(self._indian, key=lambda x: x.exchange != "NSE"):
            base = e.symbol.split(".")[0]
            if base not in seen:
                seen.add(base)
                out.append(base)
        return out

    @property
    def sentiment(self) -> float | None:
        """Mean sentiment across the Indian entities, if any carry one."""
        vals = [e.sentiment for e in self._indian if e.sentiment is not None]
        return round(sum(vals) / len(vals), 3) if vals else None


def _key() -> str | None:
    k = (os.environ.get(KEY_ENV) or "").strip()
    return k or None


def _parse(item: dict, theme: str = "") -> Story | None:
    try:
        pub = item.get("published_at") or ""
        # Marketaux stamps ISO-8601 with a Z; fromisoformat wants an offset.
        when = datetime.fromisoformat(pub.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None

    ents = []
    for e in item.get("entities") or []:
        sym = (e.get("symbol") or "").strip()
        if not sym:
            continue
        name = (e.get("name") or "").strip()
        exch = (e.get("exchange") or "").strip() or _exchange_of(sym)
        # Drop an Indian tag whose company name disagrees with our archive.
        if exch in ("NSE", "BSE") and not _name_agrees(sym, name):
            log.debug("rejected %s tagged as %r", sym, name)
            continue
        ents.append(Entity(
            symbol=sym,
            name=name,
            exchange=exch,
            industry=(e.get("industry") or "").strip(),
            match_score=float(e.get("match_score") or 0),
            sentiment=(None if e.get("sentiment_score") is None
                       else float(e["sentiment_score"])),
        ))

    title = (item.get("title") or "").strip()
    if not title:
        return None
    return Story(
        title=title,
        url=(item.get("url") or "").strip(),
        source=(item.get("source") or "marketaux").strip(),
        published=when,
        summary=(item.get("description") or item.get("snippet") or "").strip(),
        entities=ents,
        theme=theme,
    )


class Budget:
    """A hard request ceiling, so an exhausted quota is a log line not a mystery."""

    def __init__(self, limit: int = MAX_REQUESTS):
        self.limit, self.used = limit, 0

    def take(self) -> bool:
        if self.used >= self.limit:
            return False
        self.used += 1
        return True


def _get(params: dict, budget: Budget) -> list[dict]:
    if not budget.take():
        log.warning("marketaux request budget of %d exhausted", budget.limit)
        return []
    try:
        r = requests.get(BASE, params=params, timeout=20)
    except requests.RequestException as e:
        log.warning("marketaux unreachable: %s", e)
        return []
    if r.status_code == 402:
        log.warning("marketaux daily quota spent (402); falling back to RSS")
        return []
    if r.status_code != 200:
        log.warning("marketaux returned %s", r.status_code)
        return []
    try:
        return r.json().get("data") or []
    except ValueError:
        log.warning("marketaux returned a non-JSON body")
        return []


def collect(hours: int = 36, themes: bool = True,
            budget: Budget | None = None) -> list[Story]:
    """Entity-tagged Indian market news, newest first.

    One sweep of general Indian coverage, then one query per theme. Returns an
    empty list rather than raising if anything at all is wrong.
    """
    key = _key()
    if not key:
        log.info("%s not set; skipping entity-tagged news", KEY_ENV)
        return []

    budget = budget or Budget()
    after = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M")
    common = {"api_token": key, "language": "en", "limit": PER_REQUEST,
              "published_after": after, "filter_entities": "true"}

    stories: dict[str, Story] = {}

    def absorb(items, theme=""):
        for it in items:
            s = _parse(it, theme)
            # Keyed on URL: the same story arrives from several theme queries,
            # and the first one to claim it keeps its theme label.
            if s and s.url and s.url not in stories:
                stories[s.url] = s

    # General Indian coverage. Pages rather than one big call because the free
    # tier caps articles per request at three regardless of what limit says.
    for page in range(1, 5):
        absorb(_get({**common, "countries": "in", "page": page}, budget))

    if themes:
        # One page per theme. Two pages cost fourteen of the day's hundred
        # requests and returned almost nothing the first page had not, which is
        # how a single day of testing exhausted the tier and the brief went out
        # with no entity tags at all.
        for name, query in THEME_QUERIES.items():
            absorb(_get({**common, "countries": "in", "search": query}, budget),
                   theme=name)

    out = sorted(stories.values(), key=lambda s: s.published, reverse=True)
    tagged = sum(1 for s in out if s.indian_symbols)
    log.info("marketaux: %d stories in %d requests, %d carry an Indian ticker",
             len(out), budget.used, tagged)
    return out


def for_symbols(symbols: list[str], hours: int = 48,
                budget: Budget | None = None) -> dict[str, list[Story]]:
    """News about specific companies, keyed by symbol.

    This is what the screen's finalists need: not "is this story about
    infrastructure" but "is this story about the company I am holding".
    """
    key = _key()
    if not key or not symbols:
        return {}

    budget = budget or Budget(limit=min(MAX_REQUESTS, len(symbols) + 2))
    after = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M")
    out: dict[str, list[Story]] = {}

    # Marketaux accepts a comma-separated symbol list, so a watchlist of ten
    # costs one request rather than ten.
    for chunk in (symbols[i:i + 10] for i in range(0, len(symbols), 10)):
        tickers = ",".join(f"{s}.NSE" for s in chunk)
        items = _get({"api_token": key, "symbols": tickers, "language": "en",
                      "limit": PER_REQUEST, "published_after": after,
                      "filter_entities": "true"}, budget)
        for it in items:
            s = _parse(it)
            if not s:
                continue
            for sym in s.indian_symbols:
                if sym in chunk:
                    out.setdefault(sym, []).append(s)
    return out
