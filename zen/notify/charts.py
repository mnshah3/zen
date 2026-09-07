"""Charts for the email.

Gmail blocks SVG and will not fetch remote images without the reader clicking
"display images". The one thing it renders immediately is an image attached to
the message itself and referenced by content-id, so every chart here is a PNG
returned as raw bytes for the mailer to attach that way.

Kept deliberately plain: no gridlines competing with the data, no chartjunk,
and colours that survive being read on a phone.
"""

from __future__ import annotations

import io
import logging

import matplotlib

matplotlib.use("Agg")  # no display available in Actions
import matplotlib.pyplot as plt  # noqa: E402

log = logging.getLogger(__name__)

INK = "#1a1a1a"
MUTED = "#6b7280"
UP = "#0f7b3f"
DOWN = "#b42318"
RULE = "#e5e7eb"

plt.rcParams.update({
    "figure.dpi": 220,
    "savefig.dpi": 220,
    "font.size": 8,
    "axes.edgecolor": RULE,
    "axes.labelcolor": MUTED,
    "text.color": INK,
    "xtick.color": MUTED,
    "ytick.color": MUTED,
    "axes.spines.top": False,
    "axes.spines.right": False,
})


def _png(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return buf.getvalue()


def breadth_chart(history) -> bytes | None:
    """Advancers minus decliners over the last month.

    Net breadth rather than an index line: it answers 'are most stocks
    participating', which an index cannot.
    """
    if history is None or len(history) < 5:
        return None

    net = history["advancers"] - history["decliners"]
    colours = [UP if v >= 0 else DOWN for v in net]

    fig, ax = plt.subplots(figsize=(6.4, 1.9))
    ax.bar(range(len(net)), net, color=colours, width=0.72)
    ax.axhline(0, color=MUTED, linewidth=0.6)

    step = max(1, len(history) // 6)
    ticks = list(range(0, len(history), step))
    ax.set_xticks(ticks)
    ax.set_xticklabels([history["date"].iloc[i].strftime("%d %b") for i in ticks])
    ax.set_ylabel("advancers - decliners")
    ax.margins(x=0.01)
    return _png(fig)


def rotation_chart(rotation) -> bytes | None:
    """Median return by size tier -- who led, who lagged."""
    if rotation is None or len(rotation) == 0:
        return None

    vals = rotation["median_ret"].tolist()
    labels = [str(t) for t in rotation["tier"]]
    colours = [UP if v >= 0 else DOWN for v in vals]

    fig, ax = plt.subplots(figsize=(6.4, 1.6))
    bars = ax.barh(labels, vals, color=colours, height=0.55)
    ax.axvline(0, color=MUTED, linewidth=0.6)
    ax.invert_yaxis()
    ax.set_xlabel("median return over 5 sessions (%)")

    span = max(abs(min(vals)), abs(max(vals))) or 1
    for bar, v in zip(bars, vals):
        off = span * 0.04
        ax.text(v + (off if v >= 0 else -off), bar.get_y() + bar.get_height() / 2,
                f"{v:+.2f}%", va="center",
                ha="left" if v >= 0 else "right", fontsize=7.5, color=INK)
    ax.set_xlim(-span * 1.45, span * 1.45)
    return _png(fig)


def build_all(data: dict) -> dict[str, bytes]:
    """Return {content_id: png_bytes} for whatever could be drawn."""
    out = {}
    try:
        if (b := breadth_chart(data.get("breadth_history"))) is not None:
            out["breadth"] = b
        if (r := rotation_chart(data.get("rotation"))) is not None:
            out["rotation"] = r
    except Exception as e:
        # A failed chart must never stop the brief going out.
        log.warning("chart generation failed: %s", e)
    return out
