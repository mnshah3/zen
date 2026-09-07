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

# Sections in the order they appear in the email. Small limits on purpose:
# the brief is meant to be a two-minute read, not an archive.
#
# "Breaking" and "Connected to your data" are not keyword sections -- the
# first is assigned from corroboration and recency, the second is computed in
# the job from stocks the archive flagged. Both are declared here only so the
# email renders them in the right order.
SECTIONS: dict[str, dict] = {
    "Breaking": {"keywords": [], "limit": 3},
    "Connected to your data": {"keywords": [], "limit": 3},
    "Capital markets": {
        "keywords": ["nifty", "sensex", "index", "equities", "ipo", "listing",
                     "fii", "dii", "flows", "rupee", "bond", "yield", "midcap",
                     "smallcap", "valuation", "rally", "selloff", "bse", "nse"],
        "limit": 4,
    },
    "Macro and economy": {
        "keywords": ["rbi", "inflation", "cpi", "gdp", "repo", "budget",
                     "fiscal", "monetary", "deficit", "gst", "iip", "policy",
                     "sebi", "tax", "growth", "unemployment", "manufacturing"],
        "limit": 4,
    },
    "Companies and earnings": {
        "keywords": ["results", "earnings", "profit", "revenue", "order book",
                     "acquisition", "merger", "qip", "stake", "guidance",
                     "margin", "demerger", "buyback", "capacity", "expansion"],
        "limit": 4,
    },
    "Geopolitics": {
        "keywords": ["tariff", "sanction", "war", "trade deal", "china",
                     "russia", "opec", "crude", "geopolit", "border",
                     "defence", "export ban", "supply chain"],
        "limit": 3,
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
