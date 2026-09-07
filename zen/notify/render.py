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

    return f"""<div style="background:{PAGE};padding:20px 12px;">
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
  <b style="color:{INK};">Unusual volume</b> &mdash; versus each stock's own 60-day median
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


def _data_section(market: dict, insights: dict, charts: dict) -> str:
    b = market.get("breadth", {})
    body = _heading(f"What the data says — {market.get('session', '')}")
    body += _stat_row(b)

    if "breadth" in charts:
        body += _chart("breadth", "advancers minus decliners, last 30 sessions")

    div = insights.get("divergence")
    if div and div.get("diverging"):
        body += (
            f'<div style="font-size:13px;margin:8px 0 4px;">'
            f'Heavyweights moved {_pct(div["large_cap_proxy"])} while the median stock '
            f'moved {_pct(div["median_stock"])} &mdash; {_esc(div["direction"])}.</div>'
        )

    ext = insights.get("extremes") or {}
    if ext:
        body += (
            f'<div style="font-size:13px;margin:6px 0;">'
            f'<b style="color:{UP};">{ext["at_52w_high"]}</b> stocks at 52-week highs '
            f'against <b style="color:{DOWN};">{ext["at_52w_low"]}</b> at lows, '
            f'of {ext["eligible"]:,} liquid names.</div>'
        )

    if "rotation" in charts:
        body += _chart("rotation", "median return by size tier over five sessions")

    body += _volume_table(insights.get("unusual_volume"))
    return f'<div style="margin-bottom:26px;">{body}</div>'


def _story(a, explanation: str | None, facts: list[str]) -> str:
    src = _esc(a.source)
    if a.also:
        src += f" &middot; +{len(a.also)} other{'s' if len(a.also) > 1 else ''}"

    out = (f'<div style="margin-bottom:15px;">'
           f'<a href="{_esc(a.link)}" style="color:{INK};text-decoration:none;'
           f'font-size:14px;font-weight:600;line-height:1.4;">{_esc(a.title)}</a>')

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


def _verdict(market: dict, insights: dict) -> str:
    """One line under the masthead summarising the session."""
    b = market.get("breadth") or {}
    if not b.get("advancers"):
        return ""
    adv, dec = b["advancers"], b["decliners"]
    div = insights.get("divergence") or {}

    if div.get("diverging") and div.get("gap", 0) > 0:
        return f"{adv:,} up, {dec:,} down — the index flattered a narrow market"
    if adv > dec * 1.3:
        return f"{adv:,} up, {dec:,} down — broad advance"
    if dec > adv * 1.3:
        return f"{adv:,} up, {dec:,} down — broad decline"
    return f"{adv:,} up, {dec:,} down — mixed session"


def _glossary(terms: list[tuple[str, str]]) -> str:
    if not terms:
        return ""
    rows = "".join(
        f'<div style="margin-bottom:7px;font-size:12px;">'
        f'<b>{_esc(t)}</b> &mdash; <span style="color:{MUTED};">{_esc(d)}</span></div>'
        for t, d in terms
    )
    return f'<div style="margin-top:22px;">{_heading("Jargon")}{rows}</div>'


def daily_brief(sections: dict, market: dict, insights: dict, charts: dict,
                explanations: dict, facts_map: dict, bridge_text: str,
                glossary_terms: list, when: datetime) -> tuple[str, str, str]:
    """Returns (subject, html, plain text)."""
    body = _bridge(bridge_text)
    body += _data_section(market, insights, charts)
    body += _news_section(sections, explanations, facts_map)
    body += _glossary(glossary_terms)

    n = sum(len(v) for v in sections.values())
    b = market.get("breadth", {})
    tone = ""
    if b.get("advancers") is not None:
        tone = f" | {b['advancers']:,} up / {b['decliners']:,} down"
    subject = f"Zen brief {when:%d %b}{tone}"
    subtitle = f"{when:%A %d %B %Y} · {n} stories"

    text = "\n".join(
        f"[{name}] {a.title} ({a.source})\n  {a.link}"
        for name, arts in sections.items() for a in arts
    )
    html_out = _shell("Market Brief", subtitle, body,
                      verdict=_verdict(market, insights))
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
    body = ""
    if context.get("note"):
        body += (f'<div style="background:{TINT};border:1px solid {RULE};'
                 f'border-radius:4px;padding:11px 14px;margin-bottom:18px;'
                 f'font-size:13px;">{_esc(context["note"])}</div>')

    buys = [s for s in signals if s["action"].lower() == "buy"]
    sells = [s for s in signals if s["action"].lower() == "sell"]

    for label, group in (("To buy", buys), ("To sell", sells)):
        if not group:
            continue
        colour = UP if label == "To buy" else DOWN
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

    subject = f"Zen SIGNAL {when:%d %b} | {len(buys)} buy, {len(sells)} sell"
    subtitle = f"{when:%A %d %B %Y} | {_esc(context.get('strategy', 'strategy'))}"
    text = "\n".join(f"{s['action'].upper()} {s['symbol']}" for s in signals)
    return subject, _shell("Zen Signal Alert", subtitle, body), text
