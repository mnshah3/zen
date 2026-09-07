"""Brief A -- the daily market and news email.

Three parts, in this order:

  1. the bridge   where the news and the archive agree or disagree
  2. the data     breadth, divergence, extremes, rotation, unusual volume
  3. the news     ranked, deduplicated, explained, with the numbers pulled out

Everything degrades rather than fails: no Gemini key means extracted facts
instead of prose, a broken chart means no chart, a dead feed means fewer
stories. The email goes out either way.

    python -m jobs.daily_brief --dry-run     # writes preview_brief.html
    python -m jobs.daily_brief
"""

from __future__ import annotations

import argparse
import logging
from datetime import datetime
from pathlib import Path

from zen.data import store
from zen.monitor import bridge, explain, extract, insights, market, news
from zen.notify import charts, mailer, render

log = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--hours", type=int, default=24, help="window shown in the brief")
    p.add_argument("--match-hours", type=int, default=48,
                   help="wider window used only for matching stock moves to news")
    p.add_argument("--dry-run", action="store_true",
                   help="render locally without sending or consuming memory")
    p.add_argument("--no-ai", action="store_true",
                   help="skip Gemini even if a key is present")
    return p.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args()

    # --- archive first ----------------------------------------------------
    # The archive is computed before the sections are built, because which
    # stocks it flagged determines which stories get promoted.
    con = store.connect()
    try:
        asof = con.execute("SELECT max(date) FROM prices").fetchone()[0]
        derived = insights.collect(con, asof) if asof else {}
    finally:
        con.close()

    summary = market.summary()

    # --- news -------------------------------------------------------------
    # Collected once over the wider window. The brief shows the last `hours`;
    # stock-move matching uses the full pool, because a filing on the previous
    # evening routinely drives the next session's volume and would otherwise
    # look unexplained.
    pool = news.collect(args.match_hours)
    connected = bridge.matched_articles(derived, pool)
    sections = news.build(lookback_hours=args.hours,
                          remember=not args.dry_run,
                          pool=pool, connected=connected)
    ordered = [a for arts in sections.values() for a in arts]
    log.info("%d stories shown (pool %d over %dh, %d matched to the archive)",
             len(ordered), len(pool), args.match_hours, len(connected))

    # Numbers and jargon come out of the text itself -- free and deterministic.
    facts_map, glossary_seen = {}, {}
    for i, a in enumerate(ordered, start=1):
        text = f"{a.title} {a.summary}"
        facts_map[i] = extract.facts(text)
        for term, meaning in extract.jargon(text):
            glossary_seen.setdefault(term, meaning)

    explanations = {} if args.no_ai else explain.explain(ordered)

    # Anything the model did not cover falls back to the article's own opening
    # sentence, so every story carries some context even with no key at all.
    for i, a in enumerate(ordered, start=1):
        if not explanations.get(i):
            if (s := extract.first_sentence(a.summary, a.title)):
                explanations[i] = s
    images = charts.build_all(derived)
    bridge_text = bridge.build(derived, summary, pool)

    # --- render -----------------------------------------------------------
    subject, html, text = render.daily_brief(
        sections=sections,
        market=summary,
        insights=derived,
        charts=images,
        explanations=explanations,
        facts_map=facts_map,
        bridge_text=bridge_text,
        glossary_terms=sorted(glossary_seen.items())[:6],
        when=datetime.now(),
    )

    if args.dry_run:
        out = Path("preview_brief.html")
        # cid: images cannot render in a browser, so inline them for the preview.
        preview = html
        for name, blob in images.items():
            import base64
            b64 = base64.b64encode(blob).decode()
            preview = preview.replace(f"cid:{name}", f"data:image/png;base64,{b64}")
        out.write_text(preview, encoding="utf-8")
        log.info("dry run -- wrote %s (%s)", out, subject)
        log.info("charts: %s | explained: %d | bridge: %s",
                 list(images) or "none", len(explanations),
                 "yes" if bridge_text else "no")
        return 0

    return 0 if mailer.send(subject, html, text, images=images) else 1


if __name__ == "__main__":
    raise SystemExit(main())
