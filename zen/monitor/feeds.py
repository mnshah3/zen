"""Feed sources and topic weighting for the daily market brief.

TRUST scores are editorial judgement, not fact -- raise or lower them as you
learn which outlets waste your time. They feed directly into ranking.
"""

from __future__ import annotations

GNEWS = "https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"

# name -> (url, trust 0-1)
FEEDS: dict[str, tuple[str, float]] = {
    # Indian markets
    "ET Markets": (
        "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms", 0.85),
    "ET Economy": (
        "https://economictimes.indiatimes.com/news/economy/rssfeeds/1373380680.cms", 0.85),
    "Mint Markets": ("https://www.livemint.com/rss/markets", 0.85),
    "Mint Companies": ("https://www.livemint.com/rss/companies", 0.80),
    "BS Markets": ("https://www.business-standard.com/rss/markets-106.rss", 0.85),
    # Moneycontrol's own RSS returns an empty document; use their site via Google News.
    "Moneycontrol": (GNEWS.format(q="when:1d+site:moneycontrol.com+markets"), 0.70),
    "Hindu BusinessLine": (
        "https://www.thehindubusinessline.com/markets/feeder/default.rss", 0.80),

    # Global context that moves Indian markets
    "Reuters Business": (GNEWS.format(q="when:1d+site:reuters.com+markets"), 0.90),
    "Fed and rates": (GNEWS.format(q="when:1d+Federal+Reserve+OR+FOMC+interest+rates"), 0.75),
    "Crude and commodities": (GNEWS.format(q="when:1d+crude+oil+prices+brent"), 0.70),
    "Geopolitics": (GNEWS.format(q="when:1d+geopolitics+OR+sanctions+OR+tariffs+india"), 0.70),

    # Policy and flows
    "RBI and SEBI": (GNEWS.format(q="when:2d+RBI+OR+SEBI+monetary+policy"), 0.80),
    "FII and DII flows": (GNEWS.format(q="when:2d+FII+DII+flows+india+equities"), 0.70),
}

# Sections in the order they appear in the email.
SECTIONS: dict[str, dict] = {
    "Markets": {
        "keywords": ["nifty", "sensex", "market", "rally", "selloff", "index", "midcap",
                     "smallcap", "valuation", "equities", "rupee", "bse", "nse"],
        "limit": 6,
    },
    "Macro and Policy": {
        "keywords": ["rbi", "sebi", "inflation", "cpi", "gdp", "repo", "budget", "fiscal",
                     "monetary", "tariff", "policy", "deficit", "gst"],
        "limit": 5,
    },
    "Companies": {
        "keywords": ["results", "earnings", "profit", "revenue", "order book", "acquisition",
                     "merger", "ipo", "qip", "stake", "guidance", "margin"],
        "limit": 5,
    },
    "Global": {
        "keywords": ["fed", "fomc", "treasury", "crude", "oil", "china", "dollar",
                     "geopolit", "sanction", "war", "tariff"],
        "limit": 4,
    },
}

# Titles that are not stories at all: Reuters quote pages, site landing pages,
# and section indexes that RSS feeds emit alongside real articles.
JUNK_PATTERNS = [
    r"^[A-Z0-9]{1,6}\.[A-Z]{1,3}(\s|$)",   # SNDO.NS, OPAD.OQ, EFGN.S
    r"stock price\s*&\s*latest news",
    r"^compare stocks",
    r"business news, economic news",
    r"latest news.*moneycontrol\.com",
    r"^\s*live updates?\s*$",
    r"share price today.*live",
    r"^markets? live",
]

# Headlines that are almost always noise.
NOISE = [
    "horoscope", "astrology", "stocks to watch today", "muhurat", "top gainers",
    "top losers", "technical view", "buy or sell", "share price target",
    "multibagger", "penny stock", "what to expect", "zodiac",
]
