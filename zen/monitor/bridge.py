"""Connect the news to the archive.

Section one says what people are talking about. Section two says what the
market actually did. This module looks for places where the two agree,
disagree, or explain each other -- which is the only part of the brief that
could not be obtained by reading a website.

All rules, no model. Each observation states the evidence it rests on so a
wrong one is obvious rather than persuasive.
"""

from __future__ import annotations

import logging
import re

from zen.data import announcements
from zen.data import names as names_mod

log = logging.getLogger(__name__)

# Publishers append their own name to headlines; it adds nothing when the
# headline is being quoted inside a sentence.
_SUFFIX = re.compile(
    r"\s*[-|–—]\s*(Reuters|Bloomberg\.com|Bloomberg|Moneycontrol\.com|"
    r"Moneycontrol|The Hindu BusinessLine|Business Standard|Mint|Livemint|"
    r"Economic Times|ET Now|NDTV Profit|CNBC ?TV18)\s*$", re.I)


# Readable names for the filing buckets.
LABELS = {
    "volume_query": "the exchange asked them to explain the move",
    "results": "quarterly results",
    "guidance": "guidance or investor update",
    "expansion": "capacity or expansion",
    "orders": "an order win",
    "mna": "M&A or restructuring",
    "capital": "fund raising",
    "ratings": "a credit rating action",
    "litigation": "a legal or regulatory matter",
}


def _clean(title: str) -> str:
    return _SUFFIX.sub("", (title or "").strip()).strip()


def matched_articles(insights: dict, articles: list, names: dict | None = None) -> list:
    """Articles that mention a stock the archive flagged today.

    Used to promote those stories into their own section: a headline about a
    company whose volume just went to 200 times normal is worth more than a
    headline about anything else in the feed.
    """
    names = names if names is not None else names_mod.load()
    uv = insights.get("unusual_volume")
    if uv is None or len(uv) == 0 or not articles:
        return []

    symbols = list(uv["symbol"])
    out, claimed = [], set()
    for a in articles:
        hits = names_mod.find_in_text(f"{a.title} {a.summary}", names, symbols)
        fresh = [h for h in hits if h not in claimed]
        if fresh:
            claimed.update(fresh)
            out.append(a)
    return out


def _volume_explained_by_news(insights: dict, articles: list,
                              names: dict, skip: set | None = None) -> list[str]:
    """A stock trading at many times normal volume, and a story that says why."""
    uv = insights.get("unusual_volume")
    if uv is None or len(uv) == 0 or not articles:
        return []

    symbols = [s for s in uv["symbol"] if s not in (skip or set())]
    out: list[str] = []
    claimed: set[str] = set()

    for a in articles:
        hits = names_mod.find_in_text(f"{a.title} {a.summary}", names, symbols)
        for sym in hits:
            # One line per stock. Reporting the same spike against three
            # different headlines says nothing three times.
            if sym in claimed:
                continue
            claimed.add(sym)
            row = uv[uv["symbol"] == sym].iloc[0]
            out.append(
                f"<b>{names.get(sym, sym)}</b> traded at {row.vol_x:,.0f} times its "
                f"normal volume and moved {row.ret:+.1f}% &mdash; and there is a story "
                f"today: &ldquo;{_clean(a.title)}&rdquo;."
            )
            if len(out) >= 3:
                return out
    return out


def _filings_explain_volume(con, insights: dict, asof, names: dict) -> tuple[list[str], set[str]]:
    """Match today's volume spikes against what companies told the exchange.

    This is the strongest link in the brief. Filings carry the company's own
    account of events, arrive within minutes of a board approving them, and
    join to prices on ticker rather than fuzzy name matching. A newspaper, if
    it covers the story at all, does so days later.

    Returns (lines, symbols explained).
    """
    uv = insights.get("unusual_volume")
    if uv is None or len(uv) == 0 or asof is None:
        return [], set()

    symbols = list(uv["symbol"])
    try:
        filed = announcements.for_session(con, asof, symbols=symbols,
                                          material_only=True)
    except Exception as e:
        log.warning("could not read announcements: %s", e)
        return [], set()
    if filed.empty:
        return [], set()

    lines, seen = [], set()
    for r in filed.itertuples():
        if r.symbol in seen:
            continue
        seen.add(r.symbol)
        row = uv[uv["symbol"] == r.symbol].iloc[0]
        label = LABELS.get(r.category, r.category.replace("_", " "))
        subject = _clean(str(r.subject))[:190]
        lines.append(
            f"<b>{names.get(r.symbol, r.symbol)}</b> traded at "
            f"{row.vol_x:,.0f} times normal volume and moved {row.ret:+.1f}%. "
            f"It filed with the exchange &mdash; {label}: &ldquo;{subject}&rdquo;"
        )
        if len(lines) >= 3:
            break
    return lines, seen


def _exchange_queried(con, insights: dict, asof, names: dict) -> str | None:
    """Companies the exchange asked to explain today's move.

    These arrive after the close, so they are attributed to the next session
    and cannot explain today's volume -- the query is a consequence of it, not
    a cause. That makes them useless as an explanation and valuable as
    corroboration: the exchange's own surveillance flagged the same names our
    archive did.
    """
    uv = insights.get("unusual_volume")
    if uv is None or len(uv) == 0 or asof is None:
        return None

    symbols = list(uv["symbol"])
    placeholders = ", ".join("?" * len(symbols))
    try:
        rows = con.execute(
            f"""
            SELECT DISTINCT symbol FROM announcements
            WHERE category = 'volume_query'
              AND CAST(an_dt AS DATE) = ?
              AND symbol IN ({placeholders})
            """,
            [asof, *symbols],
        ).fetchall()
    except Exception as e:
        log.warning("exchange-query lookup failed: %s", e)
        return None
    if not rows:
        return None

    listed = ", ".join(names.get(r[0], r[0]) for r in rows[:3])
    plural = "companies" if len(rows) > 1 else "company"
    return (f"The exchange has formally asked {len(rows)} {plural} to explain "
            f"today's move: {listed}. Those queries were filed after the close, "
            f"so they confirm the move rather than explain it &mdash; NSE's own "
            f"surveillance flagged the same names.")


def _unexplained_volume(insights: dict, explained: list[str], names: dict,
                        filed_syms: set | None = None) -> str | None:
    """Big volume that none of our feeds accounts for.

    The claim is deliberately narrow. Our sources are market-news feeds, not
    company filings, so a corporate announcement that moved a stock may never
    appear in them. Saying "no news exists" would be false; saying "our
    sources do not explain it" is true and still useful -- it points at the
    exact names worth checking on the exchange site.
    """
    uv = insights.get("unusual_volume")
    if uv is None or len(uv) == 0:
        return None

    named = {n.split("</b>")[0].replace("<b>", "") for n in explained}
    filed = filed_syms or set()
    rest = [r for r in uv.itertuples()
            if names.get(r.symbol, r.symbol) not in named and r.symbol not in filed]
    if len(rest) < 2:
        return None

    listed = ", ".join(f"{names.get(r.symbol, r.symbol)} ({r.vol_x:,.0f}x)"
                       for r in rest[:3])
    return (f"No explanation found for: {listed}. Neither the news feeds nor "
            f"the company's own exchange filings account for these moves, which "
            f"often means a sector story rather than a company one.")


def _breadth_vs_headlines(insights: dict, market: dict) -> str | None:
    """The index and the average stock often tell different stories."""
    div = insights.get("divergence")
    if not div or not div.get("diverging"):
        return None

    gap = div["gap"]
    if gap > 0:
        return (f"Headlines will read off the index, but the median stock moved "
                f"{div['median_stock']:+.2f}% against {div['large_cap_proxy']:+.2f}% "
                f"for the heavyweights. A {abs(gap):.1f} point gap &mdash; the market "
                f"was narrower than it will sound.")
    return (f"The broader market outpaced the heavyweights by {abs(gap):.1f} points "
            f"({div['median_stock']:+.2f}% median against "
            f"{div['large_cap_proxy']:+.2f}%). Participation was wide.")


def _extremes_context(insights: dict) -> str | None:
    ext = insights.get("extremes") or {}
    if not ext or not ext.get("eligible"):
        return None

    hi, lo, net = ext["at_52w_high"], ext["at_52w_low"], ext["net"]
    if hi + lo < 5:
        return None
    if net <= -10:
        return (f"{lo} stocks sit at 52-week lows against {hi} at highs. New lows "
                f"outnumbering new highs this clearly is usually a late signal, "
                f"not an early one.")
    if net >= 10:
        return (f"{hi} stocks at 52-week highs against {lo} at lows &mdash; "
                f"broad participation on the upside.")
    return None


def _rotation_note(insights: dict) -> str | None:
    rot = insights.get("rotation")
    if rot is None or len(rot) < 3:
        return None

    vals = list(rot["median_ret"])
    labels = [str(t) for t in rot["tier"]]
    spread = vals[0] - vals[-1]
    if abs(spread) < 0.4:
        return None
    if spread > 0:
        return (f"Over five sessions {labels[0]} led at {vals[0]:+.2f}% while "
                f"{labels[-1]} lagged at {vals[-1]:+.2f}%. Money moved up the "
                f"size curve.")
    return (f"Over five sessions {labels[-1]} led at {vals[-1]:+.2f}% against "
            f"{vals[0]:+.2f}% for {labels[0]}. Risk appetite was in the "
            f"smaller names.")


def build(insights: dict, market: dict, articles: list, con=None) -> str:
    """One short paragraph tying the two halves of the brief together.

    Evidence is ordered by how directly it bears on the move: what the company
    itself told the exchange, then what the press reported, then what remains
    unaccounted for.
    """
    names = names_mod.load()
    asof = insights.get("asof")

    filing_lines, filed_syms = ([], set())
    if con is not None:
        filing_lines, filed_syms = _filings_explain_volume(con, insights, asof, names)

    news_lines = _volume_explained_by_news(insights, articles, names,
                                           skip=filed_syms)
    parts: list[str] = []

    if (b := _breadth_vs_headlines(insights, market)):
        parts.append(b)
    parts.extend(filing_lines)
    parts.extend(news_lines)
    if con is not None and (q := _exchange_queried(con, insights, asof, names)):
        parts.append(q)

    accounted = filed_syms | {
        n.split("</b>")[0].replace("<b>", "") for n in news_lines}
    if (u := _unexplained_volume(insights, list(accounted), names,
                                 filed_syms=filed_syms)):
        parts.append(u)
    if (e := _extremes_context(insights)):
        parts.append(e)
    if (r := _rotation_note(insights)):
        parts.append(r)

    if not parts:
        return ""
    return "<br><br>".join(parts[:5])
