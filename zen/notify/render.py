"""HTML for the two briefs.

Email clients strip most CSS, so styles are inlined and layout stays simple.
Charts are referenced as cid: attachments; no external images or fonts, which
get blocked and leave holes.
"""

from __future__ import annotations

import html
from datetime import datetime

FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"
INK = "#16181d"        # headers, body text
ACCENT = "#1e4a8f"     # section rules and the verdict line
MUTED = "#6b7280"
FAINT = "#9ca3af"
RULE = "#e5e7eb"
PAGE = "#f4f5f7"       # tinted ground behind the white card
TINT = "#f9fafb"
UP = "#0f7b3f"
DOWN = "#b42318"


from zen.data import filing_types
from zen.notify import viz


def _esc(s) -> str:
    return html.escape(str(s), quote=False)


def _pct(v, bold: bool = True) -> str:
    if v is None:
        return "-"
    colour = UP if v > 0 else DOWN if v < 0 else MUTED
    weight = "600" if bold else "400"
    return f'<span style="color:{colour};font-weight:{weight};">{v:+.2f}%</span>'


def _shell(title: str, subtitle: str, body: str, verdict: str = "") -> str:
    """Outer frame.

    A tinted page behind a white card gives the mail some depth in both
    Gmail themes without relying on CSS the client might strip.
    """
    verdict_html = ""
    if verdict:
        verdict_html = (f'<div style="font-size:13px;color:{ACCENT};font-weight:600;'
                        f'margin-top:6px;">{_esc(verdict)}</div>')

    # The document is written and sent as UTF-8, but without this declaration a
    # client that sniffs rather than trusts the MIME header falls back to
    # Latin-1 and every em-dash and middle dot becomes mojibake: the header read
    # "THURSDAY 10 SEPTEMBER 2026 A. 24 STORIES" in the browser preview.
    return f"""<meta charset="utf-8">
<div style="background:{PAGE};padding:20px 12px;">
<div style="font-family:{FONT};max-width:640px;margin:0 auto;background:#ffffff;
border:1px solid {RULE};border-radius:6px;overflow:hidden;color:{INK};line-height:1.55;">

  <div style="background:{INK};padding:18px 24px 16px;">
    <div style="font-size:10px;font-weight:700;letter-spacing:0.16em;
    color:#9ca3af;text-transform:uppercase;">{_esc(subtitle)}</div>
    <div style="font-size:23px;font-weight:700;letter-spacing:-0.02em;
    color:#ffffff;margin-top:5px;">{_esc(title)}</div>
  </div>

  <div style="padding:22px 24px 26px;">
    {verdict_html}
    {body}
    <div style="margin-top:30px;padding-top:12px;border-top:1px solid {RULE};
    font-size:11px;color:{FAINT};line-height:1.5;">
      Built from your own NSE archive and public feeds.<br>
      Research only, not advice. This system places no orders.
    </div>
  </div>
</div>
</div>"""


def _heading(text: str) -> str:
    return (f'<div style="font-size:10px;font-weight:700;text-transform:uppercase;'
            f'letter-spacing:0.09em;color:{ACCENT};border-bottom:2px solid {RULE};'
            f'padding-bottom:5px;margin:0 0 14px;">{_esc(text)}</div>')


# --------------------------------------------------------------------------
# Brief A -- daily market and news
# --------------------------------------------------------------------------

def _stat_row(b: dict) -> str:
    """Four numbers, given room to breathe. This is the first thing read."""
    if not b:
        return ""
    cells = [
        ("Advancing", f'{b["advancers"]:,}', UP),
        ("Declining", f'{b["decliners"]:,}', DOWN),
        ("Median stock", f'{b.get("median_ret", 0):+.2f}%',
         UP if b.get("median_ret", 0) > 0 else DOWN),
        ("Turnover", f'{b["turnover_cr"]:,.0f}cr', INK),
    ]
    tds = ""
    for i, (k, v, c) in enumerate(cells):
        divider = "" if i == len(cells) - 1 else f"border-right:1px solid {RULE};"
        tds += (
            f'<td style="padding:11px 8px;text-align:center;vertical-align:middle;'
            f'{divider}">'
            f'<div style="font-size:9px;color:{FAINT};text-transform:uppercase;'
            f'letter-spacing:0.07em;font-weight:600;">{k}</div>'
            f'<div style="font-size:19px;font-weight:700;color:{c};'
            f'margin-top:3px;letter-spacing:-0.02em;">{v}</div></td>'
        )
    return (f'<table style="width:100%;border-collapse:collapse;background:{TINT};'
            f'border:1px solid {RULE};border-radius:4px;margin-bottom:4px;">'
            f'<tr>{tds}</tr></table>')


def _signed(v, dp: int = 2, unit: str = "%") -> str:
    """'+0.60%' in the up colour, '-1.20%' in the down colour, a true minus sign."""
    if v is None:
        return f'<span style="color:{FAINT};">-</span>'
    colour = UP if v > 0 else DOWN if v < 0 else MUTED
    text = f"{abs(v):,.{dp}f}{unit}"
    sign = "+" if v > 0 else "&minus;" if v < 0 else ""
    return f'<span style="color:{colour};font-weight:600;">{sign}{text}</span>'


def _index_strip(bd: dict) -> str:
    """The headline indices at the close, from our own archive."""
    rows = (bd or {}).get("headline") or []
    if not rows:
        return ""
    tds = ""
    for i, r in enumerate(rows):
        divider = "" if i == len(rows) - 1 else f"border-right:1px solid {RULE};"
        name = r["name"].replace("Nifty ", "") if r["name"] != "Nifty 50" else "Nifty 50"
        tds += (f'<td style="padding:11px 10px;vertical-align:top;width:{100 // len(rows)}%;{divider}">'
                f'<div style="font-size:9px;color:{FAINT};text-transform:uppercase;letter-spacing:0.07em;'
                f'font-weight:600;">{_esc(name)}</div>'
                f'<div style="font-size:17px;font-weight:700;color:{INK};margin-top:3px;'
                f'letter-spacing:-0.02em;">{r["close"]:,.2f}</div>'
                f'<div style="font-size:12px;margin-top:1px;">{_signed(r["pct"])}</div></td>')
    pe = bd.get("nifty_pe")
    note = (f'<div style="font-size:10px;color:{FAINT};margin:5px 0 14px;">Closing levels from NSE. '
            f'Nifty 50 trailing P/E {pe:.1f}.</div>' if pe else
            f'<div style="font-size:10px;color:{FAINT};margin:5px 0 14px;">Closing levels from NSE.</div>')
    return (f'<table style="width:100%;border-collapse:collapse;background:{TINT};border:1px solid {RULE};'
            f'border-radius:4px;"><tr>{tds}</tr></table>{note}')


def _sector_board(bd: dict) -> str:
    """Sector indices ranked by the day's move, as a diverging bar chart in plain HTML so it
    survives blocked images."""
    rows = (bd or {}).get("sectors") or []
    if not rows:
        return ""
    cap = max(abs(r["pct"]) for r in rows) or 1.0
    out = ""
    for r in rows:
        w = max(2, round(abs(r["pct"]) / cap * 100))
        colour = UP if r["pct"] > 0 else DOWN if r["pct"] < 0 else MUTED
        bar = (f'<div style="height:9px;width:{w}%;background:{colour};opacity:0.78;border-radius:2px;'
               f'{"margin-left:auto;" if r["pct"] < 0 else ""}"></div>')
        left = bar if r["pct"] < 0 else ""
        right = bar if r["pct"] >= 0 else ""
        out += (f'<tr><td style="padding:3px 10px 3px 0;font-size:12px;color:{INK};white-space:nowrap;'
                f'width:30%;">{_esc(r["name"].replace("Nifty ", ""))}</td>'
                f'<td style="padding:3px 0;width:27%;border-right:1px solid {RULE};">{left}</td>'
                f'<td style="padding:3px 0;width:27%;">{right}</td>'
                f'<td style="padding:3px 0 3px 10px;font-size:12px;text-align:right;white-space:nowrap;">'
                f'{_signed(r["pct"])}</td></tr>')
    return (f'<div style="font-size:12px;color:{MUTED};margin:4px 0 6px;"><b style="color:{INK};">'
            f'Sectors</b> &middot; NSE sector indices, ranked by the day&rsquo;s move</div>'
            f'<table style="width:100%;border-collapse:collapse;margin-bottom:16px;">{out}</table>')


def _global_strip(items: list) -> str:
    """Overnight markets abroad, each with the date it is from."""
    if not items:
        return ""

    def value(it):
        v = it["value"]
        if it["kind"] == "yield":
            return f"{v:.2f}%"
        if it["label"] == "Brent":
            return f"${v:,.2f}"
        return f"{v:,.2f}"

    def change(it):
        if it["kind"] == "yield":
            return _signed(it["change"], 0, " bp")
        return _signed(it["change"])

    cells = [(f'<div style="font-size:9px;color:{FAINT};text-transform:uppercase;letter-spacing:0.07em;'
              f'font-weight:600;">{_esc(it["label"])}</div>'
              f'<div style="font-size:14px;font-weight:700;color:{INK};margin-top:2px;">{value(it)}</div>'
              f'<div style="font-size:11px;">{change(it)} <span style="color:{FAINT};">'
              f'{it["date"]:%d %b}</span></div>') for it in items]
    trs = ""
    for i in range(0, len(cells), 3):
        row = cells[i:i + 3] + [""] * (3 - len(cells[i:i + 3]))
        trs += "<tr>" + "".join(f'<td style="padding:8px 10px 8px 0;vertical-align:top;width:33%;">{c}</td>'
                                for c in row) + "</tr>"
    return (f'<div style="font-size:12px;color:{MUTED};margin:2px 0 2px;"><b style="color:{INK};">'
            f'Overnight</b> &middot; last close abroad, with the date of each figure</div>'
            f'<table style="width:100%;border-collapse:collapse;margin-bottom:6px;">{trs}</table>'
            f'<div style="font-size:10px;color:{FAINT};margin-bottom:14px;">FRED, Federal Reserve Bank of '
            f'St. Louis, and ECB reference rates. Yield changes in basis points.</div>')


def _backdrop_section(bd: dict, global_items: list) -> str:
    body = _index_strip(bd) + _sector_board(bd) + _global_strip(global_items)
    if not body:
        return ""
    return f'<div style="margin-bottom:26px;">{_heading("Markets")}{body}</div>'


def _chart(cid: str, alt: str) -> str:
    return (f'<div style="margin:10px 0 14px;">'
            f'<img src="cid:{cid}" alt="{_esc(alt)}" '
            f'style="width:100%;max-width:620px;height:auto;display:block;"></div>')


def _volume_table(df) -> str:
    if df is None or len(df) == 0:
        return ""
    rows = "".join(
        f'<tr>'
        f'<td style="padding:4px 10px 4px 0;font-size:13px;"><b>{_esc(r.symbol)}</b></td>'
        f'<td style="padding:4px 10px;font-size:13px;text-align:right;">{r.close:,.1f}</td>'
        f'<td style="padding:4px 10px;font-size:13px;text-align:right;">{_pct(r.ret)}</td>'
        f'<td style="padding:4px 10px;font-size:13px;text-align:right;">'
        f'<b>{r.vol_x:,.0f}x</b></td>'
        f'<td style="padding:4px 0;font-size:12px;text-align:right;color:{MUTED};">'
        f'{r.turnover_cr:,.0f} cr</td></tr>'
        for r in df.itertuples()
    )
    return f"""
<div style="font-size:12px;color:{MUTED};margin:14px 0 4px;">
  <b style="color:{INK};">Unusual volume</b> &middot; against each stock's own 60-day median
</div>
<table style="width:100%;border-collapse:collapse;">
  <tr style="color:{FAINT};font-size:10px;text-transform:uppercase;letter-spacing:0.05em;">
    <td style="padding-bottom:3px;">Stock</td>
    <td style="text-align:right;padding-bottom:3px;">Close</td>
    <td style="text-align:right;padding-bottom:3px;">Move</td>
    <td style="text-align:right;padding-bottom:3px;">Volume</td>
    <td style="text-align:right;padding-bottom:3px;">Turnover</td>
  </tr>
  {rows}
</table>"""


def _session_label(d) -> str:
    """'Wed 9 Sep 2026' from a date or an ISO string."""
    try:
        d = datetime.fromisoformat(str(d)[:10])
        return f"{d:%a} {d.day} {d:%b %Y}"
    except ValueError:
        return str(d or "")


def _data_section(market: dict, insights: dict, charts: dict) -> str:
    b = market.get("breadth", {})
    body = _heading(f"The session in numbers · {_session_label(market.get('session'))}")
    body += _stat_row(b)

    # The proportional bar carries the day's shape even with images blocked,
    # which is the default in Gmail and Outlook for an unknown sender.
    if b.get("advancers") is not None and b.get("decliners") is not None:
        body += viz.split_bar(int(b["advancers"]), int(b["decliners"]),
                              int(b.get("unchanged") or 0))

    hist = insights.get("breadth_history")
    if hist is not None and len(hist):
        net = (hist["advancers"] - hist["decliners"]).tolist()
        days = [str(d)[:10] for d in hist["date"].tolist()]
        body += viz.spark_bars(net, labels=days)
        body += (f'<div style="font-size:10px;color:{FAINT};margin:-4px 0 12px;">'
                 f'net breadth, last {len(net)} sessions</div>')

    div = insights.get("divergence")
    if div and div.get("diverging"):
        body += viz.gauge(
            "Heavyweights", _pct(div["large_cap_proxy"]),
            "Median stock", _pct(div["median_stock"]),
            caption=f'{div["direction"]}. A gap this wide means the index is '
                    f'not describing what most stocks did.')

    ext = insights.get("extremes") or {}
    if ext:
        body += viz.gauge(
            "At 52-week highs", f'<span style="color:{UP};">{ext["at_52w_high"]}</span>',
            "At 52-week lows", f'<span style="color:{DOWN};">{ext["at_52w_low"]}</span>',
            caption=f'of {ext["eligible"]:,} liquid names.')

    rot = insights.get("rotation")
    if rot is not None and len(rot):
        body += viz.heat_table(rot, label_key="tier", value_key="median_ret",
                               extra_key="stocks", extra_label="stocks")
        body += (f'<div style="font-size:10px;color:{FAINT};margin:-8px 0 12px;">'
                 f'median return by size tier, five sessions</div>')

    body += _volume_table(insights.get("unusual_volume"))
    return f'<div style="margin-bottom:26px;">{body}</div>'


def _clean_title(title: str, source: str = "") -> str:
    """A headline without the publisher tacked on the end ('... - Reuters', '... | Mint'),
    which the source line already shows."""
    t = (title or "").strip()
    for sep in (" - ", " | ", " – ", " — "):
        head, found, tail = t.rpartition(sep)
        # Only a tail that names the publisher: "Nifty ends higher - banks lead gains" keeps its half.
        if found and head and source and len(tail) <= 40 and (
                tail.lower() in source.lower() or source.lower() in tail.lower()):
            return head.strip()
    return t


def _story(a, explanation: str | None, facts: list[str]) -> str:
    src = _esc(a.source)
    if a.also:
        src += f" &middot; +{len(a.also)} other{'s' if len(a.also) > 1 else ''}"

    title = _clean_title(a.title, a.source)
    out = (f'<div style="margin-bottom:15px;">'
           f'<a href="{_esc(a.link)}" style="color:{INK};text-decoration:none;'
           f'font-size:14px;font-weight:600;line-height:1.4;">{_esc(title)}</a>')

    if explanation:
        # A paragraph needs more air than a caption; this is the part actually
        # being read, so it gets close to body-copy treatment.
        out += (f'<div style="font-size:13px;color:#3a3f47;margin-top:6px;'
                f'line-height:1.62;">{_esc(explanation)}</div>')

    if facts:
        chips = "".join(
            f'<span style="display:inline-block;background:#f3f4f6;border-radius:3px;'
            f'padding:1px 6px;margin:0 4px 0 0;font-size:11px;color:#374151;">'
            f'{_esc(f)}</span>' for f in facts
        )
        out += f'<div style="margin-top:5px;">{chips}</div>'

    # Theme badge. Placed on the source line rather than above the headline so
    # it reads as provenance -- why this story is in front of him -- instead of
    # competing with the headline for the first glance.
    # Tickers the provider says the story is about, with its sentiment. These
    # are stated rather than inferred, so they are shown as fact where a theme
    # badge is shown as a guess.
    syms = getattr(a, "symbols", None)
    if syms:
        sent = getattr(a, "sentiment", None)
        mark = ""
        if sent is not None and abs(sent) >= 0.15:
            colour, arrow = (UP, "▲") if sent > 0 else (DOWN, "▼")
            mark = (f'<span style="color:{colour};font-weight:700;'
                    f'margin-left:5px;">{arrow} {sent:+.2f}</span>')
        chips = "".join(
            f'<span style="display:inline-block;background:{INK};color:#ffffff;'
            f'border-radius:3px;padding:1px 6px;margin:0 4px 0 0;font-size:10px;'
            f'font-weight:700;letter-spacing:0.03em;">{_esc(s)}</span>'
            for s in syms[:4])
        out += f'<div style="margin-top:6px;">{chips}{mark}</div>'

    # Only badge a theme an unambiguous term earned. A match built from two
    # generic words scores 0.45, and at that level the badges were actively
    # misleading: an oil-and-rupee story tagged "Import substitution" because
    # it said "imports", a Fed rate story tagged "AI & data centres". A badge
    # claims the story is about that theme, so it has to be right more often
    # than a keyword count can manage.
    themed = [t for t in (getattr(a, "themes", None) or []) if t[1] >= 0.6]
    if themed:
        name, strength, terms = themed[0]
        tip = ", ".join(terms[:3])
        badge = (f'<span style="display:inline-block;background:#eef2fb;'
                 f'border:1px solid #d6e0f5;border-radius:3px;padding:1px 6px;'
                 f'margin-right:6px;font-size:10px;font-weight:600;color:{ACCENT};'
                 f'letter-spacing:0.02em;" title="matched: {_esc(tip)}">'
                 f'{_esc(name)}</span>')
        src = badge + src

    out += (f'<div style="font-size:11px;color:{FAINT};margin-top:4px;">{src}</div>'
            f'</div>')
    return out


def _news_section(sections: dict, explanations: dict, facts_map: dict) -> str:
    if not sections:
        return (f'<div style="font-size:13px;color:{MUTED};">'
                f'Nothing cleared the filters today.</div>')

    body = _heading("What happened")
    idx = 0
    for name, articles in sections.items():
        body += (f'<div style="font-size:12px;font-weight:700;color:{INK};'
                 f'margin:16px 0 8px;">{_esc(name)}</div>')
        for a in articles:
            idx += 1
            body += _story(a, explanations.get(idx), facts_map.get(idx, []))
    return f'<div style="margin-bottom:24px;">{body}</div>'


def _bridge(text: str) -> str:
    """The synthesis card. Sits above everything because it is the point."""
    if not text:
        return ""
    return (f'<div style="background:{TINT};border:1px solid {RULE};'
            f'border-left:3px solid {ACCENT};border-radius:4px;'
            f'padding:14px 17px;margin:16px 0 26px;">'
            f'<div style="font-size:9px;font-weight:700;letter-spacing:0.12em;'
            f'color:{ACCENT};text-transform:uppercase;margin-bottom:8px;">'
            f'Today&rsquo;s read</div>'
            f'<div style="font-size:13.5px;line-height:1.6;color:#2d3138;">'
            f'{text}</div></div>')


def _verdict(market: dict, insights: dict, bd: dict | None = None) -> str:
    """One line under the masthead: where the Nifty 50 closed and whether the rest of the
    market went with it."""
    b = market.get("breadth") or {}
    if not b.get("advancers"):
        return ""
    adv, dec = b["advancers"], b["decliners"]
    div = insights.get("divergence") or {}
    n50 = next((r for r in (bd or {}).get("headline") or [] if r["name"] == "Nifty 50"), None)

    pct = n50.get("pct") if n50 else None
    up, down = pct is not None and pct >= 0.05, pct is not None and pct <= -0.05
    if div.get("diverging") and div.get("gap", 0) > 0 and dec > adv:
        tone = f"{'but' if up else 'and'} decliners led {dec:,} to {adv:,}"
    elif div.get("diverging") and div.get("gap", 0) > 0 and up:
        tone = "on narrow participation"
    elif adv > dec * 1.3:
        tone = (f"but advancers led {adv:,} to {dec:,}" if down
                else f"with broad buying, {adv:,} advancers to {dec:,} decliners")
    elif dec > adv * 1.3:
        tone = (f"but decliners led {dec:,} to {adv:,}" if up
                else f"with broad selling, {dec:,} decliners to {adv:,} advancers")
    else:
        tone = f"on mixed breadth, {adv:,} advancers to {dec:,} decliners"

    if n50 and n50.get("pct") is not None:
        p = n50["pct"]
        move = (f"rose {abs(p):.2f}% to" if p >= 0.05 else f"fell {abs(p):.2f}% to" if p <= -0.05
                else "was flat at")
        return f"Nifty 50 {move} {n50['close']:,.0f} {tone}"
    # No index print in the archive: breadth alone, said plainly.
    if div.get("diverging") and div.get("gap", 0) > 0:
        return f"{adv:,} up, {dec:,} down. The index flattered a narrow market"
    if adv > dec * 1.3:
        return f"{adv:,} up, {dec:,} down. A broad advance"
    if dec > adv * 1.3:
        return f"{adv:,} up, {dec:,} down. A broad decline"
    return f"{adv:,} up, {dec:,} down. A mixed session"


FILING_LABELS = {
    "exchange_query": "Exchange query on the move",
    "results": "Results",
    "guidance": "Guidance / investor update",
    "expansion": "Capacity / expansion",
    "contraction": "Closure / disruption",
    "orders": "Order win",
    "mna": "M&A / restructuring",
    "capital": "Fund raising",
    "ratings": "Credit rating",
    "regulatory": "Legal / regulatory",
    "licenses": "Licence / approval",
}


def _filings_section(filings) -> str:
    """What companies themselves told the exchange today.

    Kept separate from news because it is a different kind of evidence: the
    company's own statement, timestamped by the exchange, rather than a
    journalist's account of it.
    """
    if filings is None or len(filings) == 0:
        return ""

    rows = ""
    for r in filings.head(8).itertuples():
        label = FILING_LABELS.get(r.category, str(r.category).replace("_", " ").title())
        subject = filing_types.gist(str(r.subject or ""), 190)
        company = (r.company or r.symbol or "")[:44]
        link = str(r.url or "")
        title = _esc(subject[:190])
        if link:
            title = (f'<a href="{_esc(link)}" style="color:{INK};'
                     f'text-decoration:none;">{title}</a>')
        rows += (
            f'<div style="margin-bottom:12px;">'
            f'<span style="font-size:9px;font-weight:700;letter-spacing:0.06em;'
            f'text-transform:uppercase;color:{ACCENT};">{_esc(label)}</span>'
            f'<span style="font-size:12px;font-weight:700;margin-left:7px;">'
            f'{_esc(r.symbol)}</span>'
            f'<span style="font-size:11px;color:{MUTED};margin-left:6px;">'
            f'{_esc(company)}</span>'
            f'<div style="font-size:12.5px;color:#3a3f47;margin-top:3px;'
            f'line-height:1.5;">{title}</div></div>'
        )

    note = (f'<div style="font-size:11px;color:{FAINT};margin-top:-2px;'
            f'margin-bottom:10px;">Filed with NSE for this session. Routine '
            f'compliance filings are excluded.</div>')
    return (f'<div style="margin-bottom:26px;">'
            f'{_heading("What companies filed")}{note}{rows}</div>')


def daily_brief(sections: dict, market: dict, insights: dict, charts: dict,
                explanations: dict, facts_map: dict, bridge_text: str,
                filings, when: datetime, backdrop: dict | None = None,
                global_items: list | None = None) -> tuple[str, str, str]:
    """Returns (subject, html, plain text)."""
    body = _bridge(bridge_text)
    body += _backdrop_section(backdrop or {}, global_items or [])
    body += _data_section(market, insights, charts)
    body += _filings_section(filings)
    body += _news_section(sections, explanations, facts_map)

    n = sum(len(v) for v in sections.values())
    b = market.get("breadth", {})
    tone = ""
    n50 = next((r for r in (backdrop or {}).get("headline") or [] if r["name"] == "Nifty 50"), None)
    if n50 and n50.get("pct") is not None:
        tone += f" | Nifty 50 {n50['pct']:+.2f}%"
    if b.get("advancers") is not None:
        tone += f" | {b['advancers']:,} up, {b['decliners']:,} down"
    subject = f"Zen morning brief {when:%d %b}{tone}"
    subtitle = f"{when:%A %d %B %Y} · {n} stories"

    text = "\n".join(
        f"[{name}] {a.title} ({a.source})\n  {a.link}"
        for name, arts in sections.items() for a in arts
    )
    html_out = _shell("Morning Brief", subtitle, body,
                      verdict=_verdict(market, insights, backdrop))
    return subject, html_out, text


# --------------------------------------------------------------------------
# Brief B -- strategy signals, only sent when something fires
# --------------------------------------------------------------------------

def _signal_block(s: dict) -> str:
    action = s["action"].upper()
    colour = UP if action == "BUY" else DOWN if action == "SELL" else MUTED

    facts = "".join(
        f'<tr><td style="padding:3px 14px 3px 0;font-size:12px;color:{MUTED};'
        f'white-space:nowrap;vertical-align:top;">{_esc(k)}</td>'
        f'<td style="padding:3px 0;font-size:13px;">{_esc(v)}</td></tr>'
        for k, v in s.get("facts", {}).items()
    )
    reasons = "".join(
        f'<li style="margin-bottom:5px;font-size:13px;">{_esc(r)}</li>'
        for r in s.get("rationale", [])
    )
    against = "".join(
        f'<li style="margin-bottom:5px;font-size:13px;color:{MUTED};">{_esc(r)}</li>'
        for r in s.get("against", [])
    )
    against_html = ""
    if against:
        against_html = (f'<div style="font-size:10px;color:{FAINT};'
                        f'text-transform:uppercase;letter-spacing:0.05em;'
                        f'margin:13px 0 5px;">Case against</div>'
                        f'<ul style="margin:0;padding-left:18px;">{against}</ul>')

    return f"""
<div style="border:1px solid {RULE};border-left:3px solid {colour};
padding:15px 17px;margin-bottom:16px;">
  <div style="margin-bottom:9px;">
    <span style="font-size:10px;font-weight:700;color:{colour};
    letter-spacing:0.09em;">{action}</span>
    <span style="font-size:17px;font-weight:700;margin-left:8px;">{_esc(s["symbol"])}</span>
    <span style="font-size:12px;color:{MUTED};margin-left:8px;">{_esc(s.get("name", ""))}</span>
  </div>
  <table style="border-collapse:collapse;margin-bottom:11px;">{facts}</table>
  <div style="font-size:10px;color:{FAINT};text-transform:uppercase;
  letter-spacing:0.05em;margin-bottom:5px;">Why</div>
  <ul style="margin:0;padding-left:18px;">{reasons}</ul>
  {against_html}
</div>"""


def _signal_table(group: list[dict], colour: str) -> str:
    """Compact ranked list.

    When a screen returns fifteen names the per-name detail is nearly
    identical, and repeating it fifteen times buries the information rather
    than presenting it. The table carries what differs between names; what
    they share is stated once, above.
    """
    rows = ""
    for s in group:
        f = s.get("facts", {})
        rows += (
            f'<tr style="border-bottom:1px solid {RULE};">'
            f'<td style="padding:7px 8px 7px 0;font-size:13px;vertical-align:top;">'
            f'<b>{_esc(s["symbol"])}</b>'
            f'<div style="font-size:11px;color:{MUTED};margin-top:1px;">'
            f'{_esc((s.get("name") or "")[:34])}</div></td>'
            f'<td style="padding:7px 8px;font-size:13px;text-align:right;'
            f'vertical-align:top;color:{colour};font-weight:600;">'
            f'{_esc(f.get("Formation return", ""))}</td>'
            f'<td style="padding:7px 8px;font-size:12px;text-align:right;'
            f'vertical-align:top;">{_esc(f.get("Last close", ""))}</td>'
            f'<td style="padding:7px 0;font-size:12px;text-align:right;'
            f'vertical-align:top;color:{MUTED};">'
            f'{_esc(f.get("Median turnover", "").replace(" cr/day", "cr"))}</td>'
            f'</tr>'
        )
    return (
        f'<table style="width:100%;border-collapse:collapse;margin-bottom:6px;">'
        f'<tr style="color:{FAINT};font-size:9px;text-transform:uppercase;'
        f'letter-spacing:0.07em;">'
        f'<td style="padding-bottom:4px;">Stock</td>'
        f'<td style="padding-bottom:4px;text-align:right;">Formation</td>'
        f'<td style="padding-bottom:4px;text-align:right;">Close</td>'
        f'<td style="padding-bottom:4px;text-align:right;">Liquidity</td></tr>'
        f'{rows}</table>'
    )


def _shared_notes(group: list[dict]) -> str:
    """Whatever every signal in the group argues identically, argued once."""
    if not group:
        return ""
    common = group[0].get("against") or []
    if not common or not all((s.get("against") or []) == common for s in group):
        return ""
    items = "".join(f'<li style="margin-bottom:5px;font-size:12.5px;color:#78350f;">'
                    f'{_esc(r)}</li>' for r in common)
    return (f'<div style="background:#fffbeb;border:1px solid #fde68a;'
            f'border-radius:4px;padding:12px 15px;margin:16px 0 8px;">'
            f'<div style="font-size:9px;font-weight:700;letter-spacing:0.1em;'
            f'color:#92400e;text-transform:uppercase;margin-bottom:7px;">'
            f'Case against &mdash; applies to every name above</div>'
            f'<ul style="margin:0;padding-left:17px;">{items}</ul></div>')


def signal_alert(signals: list[dict], context: dict, when: datetime) -> tuple[str, str, str]:
    """Buy/sell note. Only called when signals is non-empty.

    Collapses to a table when a screen returns many names sharing one
    argument; expands per name when the signals are genuinely individual.
    """
    # An unvalidated strategy must not produce an email that reads like advice.
    # The previous version headed a momentum calibration run "To buy (15)" with
    # the caveat in small type at the bottom, and it was read exactly as its
    # headline said it should be.
    validated = bool(context.get("validated"))
    body = ""
    if not validated:
        body += (
            f'<div style="background:#7f1d1d;color:#ffffff;border-radius:4px;'
            f'padding:13px 16px;margin-bottom:18px;">'
            f'<div style="font-size:11px;font-weight:700;letter-spacing:0.1em;'
            f'text-transform:uppercase;">Calibration output — not a recommendation</div>'
            f'<div style="font-size:13px;margin-top:6px;line-height:1.55;">'
            f'This screen has never been walk-forward tested. It exists to check '
            f'that the measurement harness reports honestly, and its output is '
            f'evidence about the plumbing, not about these companies. '
            f'<b>Do not buy anything on this list.</b></div></div>')

    if context.get("note"):
        body += (f'<div style="background:{TINT};border:1px solid {RULE};'
                 f'border-radius:4px;padding:11px 14px;margin-bottom:18px;'
                 f'font-size:13px;">{_esc(context["note"])}</div>')

    buys = [s for s in signals if s["action"].lower() == "buy"]
    sells = [s for s in signals if s["action"].lower() == "sell"]

    labels = (("To buy", "Ranked highest by the test screen"),
              ("To sell", "Ranked lowest by the test screen"))
    for (real, test), group in zip(labels, (buys, sells)):
        if not group:
            continue
        label = real if validated else test
        colour = UP if group is buys else DOWN
        body += _heading(f"{label} ({len(group)})")
        shared = _shared_notes(group)
        if shared and len(group) > 4:
            body += _signal_table(group, colour)
            body += shared
        else:
            body += "".join(_signal_block(s) for s in group)

    body += (f'<div style="margin-top:22px;padding:11px 14px;background:#fffbeb;'
             f'border:1px solid #fde68a;font-size:12px;">'
             f'<b>Before acting:</b> these are screen outputs, not instructions. '
             f'Check sizing against your sleeve limits and confirm nothing material '
             f'has been announced since the last close.</div>')

    if validated:
        subject = f"Zen SIGNAL {when:%d %b} | {len(buys)} buy, {len(sells)} sell"
    else:
        subject = (f"Zen CALIBRATION {when:%d %b} | {len(buys) + len(sells)} names, "
                   f"not recommendations")
    subtitle = f"{when:%A %d %B %Y} | {_esc(context.get('strategy', 'strategy'))}"
    text = "\n".join(f"{s['action'].upper()} {s['symbol']}" for s in signals)
    title = "Zen Signal Alert" if validated else "Zen Calibration Run"
    return subject, _shell(title, subtitle, body), text
