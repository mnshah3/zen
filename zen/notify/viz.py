"""HTML-native charts for the brief.

WHY NOT JUST USE THE PNGs

Gmail, Outlook and Apple Mail all block remote and attached images by default
for a sender you have not whitelisted. A brief whose entire visual layer is
matplotlib output is, for a large share of opens, a wall of text. Everything in
this module is built from table cells and background colours, so it renders in
every client, at every width, with images switched off and with no network.

The PNG charts stay -- they carry detail these cannot -- but nothing important
is allowed to exist only inside one.

CONSTRAINTS THIS CODE OBEYS

  Tables, not flexbox or grid. Outlook's engine is Word's, and it supports
  neither. `background-color` on a `<td>` works everywhere; a styled `<div>`
  with a height does not.

  No external fonts, no CSS classes, no <style> block. Gmail strips head
  styles, so every rule is inline.

  Colour must never be the only carrier of meaning. Every bar is also labelled
  with its number, because a red-green chart is unreadable to roughly one man
  in twelve and invisible in a plain-text client.
"""

from __future__ import annotations

INK = "#16181d"
MUTED = "#6b7280"
FAINT = "#9ca3af"
RULE = "#e5e7eb"
TINT = "#f9fafb"
UP = "#0f7b3f"
UP_SOFT = "#d9f0e2"
DOWN = "#b42318"
DOWN_SOFT = "#fbe3e0"
ACCENT = "#1e4a8f"


def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def split_bar(up: int, down: int, unchanged: int = 0) -> str:
    """Advancers against decliners as one proportional bar.

    Reads at a glance in a way "928 up / 1,579 down" does not: the eye takes
    the ratio before it reads either number.
    """
    total = max(1, up + down + unchanged)
    pu, pd = 100 * up / total, 100 * down / total
    pn = max(0.0, 100 - pu - pd)

    def cell(pct, colour, label, align):
        if pct < 4:                      # too thin to hold a label
            return (f'<td width="{pct:.1f}%" style="background:{colour};height:26px;'
                    f'font-size:1px;line-height:26px;">&nbsp;</td>')
        return (f'<td width="{pct:.1f}%" style="background:{colour};height:26px;'
                f'color:#ffffff;font-size:11px;font-weight:700;text-align:{align};'
                f'padding:0 8px;line-height:26px;white-space:nowrap;">{label}</td>')

    cells = cell(pu, UP, f"{up:,} up", "left")
    if pn >= 4:
        cells += (f'<td width="{pn:.1f}%" style="background:{RULE};height:26px;'
                  f'font-size:1px;line-height:26px;">&nbsp;</td>')
    cells += cell(pd, DOWN, f"{down:,} down", "right")

    return (f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
            f'width="100%" style="border-collapse:collapse;border-radius:4px;'
            f'overflow:hidden;margin:2px 0 10px;"><tr>{cells}</tr></table>')


def spark_bars(values, labels=None, height: int = 46) -> str:
    """A signed mini bar chart, one column per value.

    Used for thirty sessions of net breadth. Bars grow from a centre line so
    sign is visible without reading an axis; the tallest magnitude sets the
    scale, so the shape is comparative rather than absolute -- which is the
    honest way to show a series with no meaningful unit.
    """
    vals = [float(v) for v in values]
    if not vals:
        return ""
    peak = max(abs(v) for v in vals) or 1.0
    half = height // 2

    cols = []
    for i, v in enumerate(vals):
        h = max(2, int(round(half * abs(v) / peak)))
        colour = UP if v >= 0 else DOWN
        pad_top = half - h if v >= 0 else half
        top = (f'<div style="height:{pad_top}px;font-size:1px;">&nbsp;</div>'
               f'<div style="height:{h}px;background:{colour};font-size:1px;">&nbsp;</div>'
               if v >= 0 else
               f'<div style="height:{half}px;font-size:1px;">&nbsp;</div>'
               f'<div style="height:{h}px;background:{colour};font-size:1px;">&nbsp;</div>')
        title = f' title="{_esc(labels[i])}"' if labels and i < len(labels) else ""
        cols.append(f'<td valign="top" style="padding:0 1px;"{title}>{top}</td>')

    return (f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
            f'width="100%" style="border-collapse:collapse;height:{height}px;">'
            f'<tr>{"".join(cols)}</tr></table>')


def _shade(pct: float, cap: float = 2.0) -> tuple[str, str]:
    """Background and text colour for a percentage, saturating at `cap`.

    Saturating matters: without it a single -9% session washes every other cell
    to near-white and the grid stops distinguishing an ordinary day from a
    quiet one.
    """
    if pct is None:
        return TINT, MUTED
    f = min(1.0, abs(pct) / cap)
    if abs(pct) < 0.05:
        return TINT, MUTED
    if pct > 0:
        # interpolate UP_SOFT -> UP
        return (f"rgba(15,123,63,{0.10 + 0.55 * f:.2f})", INK if f < 0.55 else "#ffffff")
    return (f"rgba(180,35,24,{0.10 + 0.55 * f:.2f})", INK if f < 0.55 else "#ffffff")


def heat_table(rows, label_key="tier", value_key="median_ret",
               extra_key=None, extra_label="", scale: float = 1.0) -> str:
    """A labelled heat grid -- one row per bucket, shaded by its number.

    `rows` is a list of dicts or a DataFrame's records. The number is printed
    in every cell, so the shading is an accelerator rather than the message.

    `scale` multiplies each value before display, and defaults to 1.0 meaning
    the values are ALREADY percentages. An earlier version inferred this from
    magnitude -- anything below 1.0 was assumed to be a fraction and multiplied
    by a hundred -- which turned a median five-session return of +0.68% into
    +68.00% in the email. A caller knows its own units; a heuristic does not.
    """
    if rows is None or len(rows) == 0:
        return ""
    recs = rows.to_dict("records") if hasattr(rows, "to_dict") else list(rows)

    out = []
    for r in recs:
        v = r.get(value_key)
        v = None if v is None else float(v) * scale
        bg, fg = _shade(v)
        shown = "&mdash;" if v is None else f"{v:+.2f}%"
        extra = ""
        if extra_key and r.get(extra_key) is not None:
            extra = (f'<td style="padding:7px 10px;font-size:11px;color:{FAINT};'
                     f'text-align:right;white-space:nowrap;">{_esc(r[extra_key])}'
                     f'{" " + extra_label if extra_label else ""}</td>')
        out.append(
            f'<tr>'
            f'<td style="padding:7px 10px;font-size:12px;color:{INK};'
            f'border-bottom:1px solid {RULE};">{_esc(r.get(label_key, ""))}</td>'
            f'{extra}'
            f'<td width="86" style="padding:7px 10px;font-size:12px;font-weight:700;'
            f'text-align:right;background:{bg};color:{fg};'
            f'border-bottom:1px solid {RULE};">{shown}</td>'
            f'</tr>')

    return (f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
            f'width="100%" style="border-collapse:collapse;border:1px solid {RULE};'
            f'border-radius:4px;overflow:hidden;margin:2px 0 12px;">'
            f'{"".join(out)}</table>')


def gauge(left_label: str, left_val: str, right_label: str, right_val: str,
          caption: str = "") -> str:
    """Two numbers set against each other, for divergence and 52-week extremes.

    A comparison the reader is meant to make is laid out as a comparison,
    rather than as two statistics several lines apart.
    """
    cap = (f'<div style="font-size:11px;color:{MUTED};margin-top:7px;'
           f'line-height:1.5;">{_esc(caption)}</div>' if caption else "")
    return (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'width="100%" style="border-collapse:collapse;background:{TINT};'
        f'border:1px solid {RULE};border-radius:4px;margin:2px 0 12px;">'
        f'<tr>'
        f'<td width="50%" style="padding:12px 14px;border-right:1px solid {RULE};">'
        f'<div style="font-size:9px;font-weight:700;letter-spacing:0.08em;'
        f'text-transform:uppercase;color:{FAINT};">{_esc(left_label)}</div>'
        f'<div style="font-size:20px;font-weight:700;color:{INK};margin-top:3px;">'
        f'{left_val}</div></td>'
        f'<td width="50%" style="padding:12px 14px;">'
        f'<div style="font-size:9px;font-weight:700;letter-spacing:0.08em;'
        f'text-transform:uppercase;color:{FAINT};">{_esc(right_label)}</div>'
        f'<div style="font-size:20px;font-weight:700;color:{INK};margin-top:3px;">'
        f'{right_val}</div></td>'
        f'</tr></table>{cap}')
