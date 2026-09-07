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

from zen.data import names as names_mod

log = logging.getLogger(__name__)


def _volume_explained_by_news(insights: dict, articles: list,
                              names: dict) -> list[str]:
    """A stock trading at many times normal volume, and a story that says why."""
    uv = insights.get("unusual_volume")
    if uv is None or len(uv) == 0 or not articles:
        return []

    symbols = list(uv["symbol"])
    out = []
    for a in articles:
        hits = names_mod.find_in_text(f"{a.title} {a.summary}", names, symbols)
        for sym in hits:
            row = uv[uv["symbol"] == sym].iloc[0]
            out.append(
                f"<b>{names.get(sym, sym)}</b> traded at {row.vol_x:,.0f} times its "
                f"normal volume and moved {row.ret:+.1f}% &mdash; and there is a story "
                f"today: &ldquo;{a.title}&rdquo;."
            )
            if len(out) >= 3:
                return out
    return out


def _unexplained_volume(insights: dict, explained: list[str], names: dict) -> str | None:
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
    rest = [r for r in uv.itertuples()
            if names.get(r.symbol, r.symbol) not in named]
    if len(rest) < 2:
        return None

    listed = ", ".join(f"{names.get(r.symbol, r.symbol)} ({r.vol_x:,.0f}x)"
                       for r in rest[:3])
    return (f"Volume spikes our news sources do not account for: {listed}. "
            f"These feeds carry market news, not company filings, so check the "
            f"exchange announcements before assuming there is no reason.")


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


def build(insights: dict, market: dict, articles: list) -> str:
    """One short paragraph tying the two halves of the brief together."""
    names = names_mod.load()

    explained = _volume_explained_by_news(insights, articles, names)
    parts: list[str] = []

    if (b := _breadth_vs_headlines(insights, market)):
        parts.append(b)
    parts.extend(explained)
    if (u := _unexplained_volume(insights, explained, names)):
        parts.append(u)
    if (e := _extremes_context(insights)):
        parts.append(e)
    if (r := _rotation_note(insights)):
        parts.append(r)

    if not parts:
        return ""
    return "<br><br>".join(parts[:4])
