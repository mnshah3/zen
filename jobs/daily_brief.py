"""Brief A -- the daily market and news email.

Runs every weekday morning whether or not anything interesting happened.
Curated, deduplicated, and filtered against a 7-day memory so the same story
does not arrive twice.

    python -m jobs.daily_brief --dry-run     # write preview.html, send nothing
    python -m jobs.daily_brief               # send
"""

from __future__ import annotations

import argparse
import logging
from datetime import datetime
from pathlib import Path

from zen.monitor import market, news
from zen.notify import mailer, render

log = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--hours", type=int, default=24, help="news lookback window")
    p.add_argument("--dry-run", action="store_true",
                   help="render to preview.html without sending or updating memory")
    return p.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args()

    # Memory is only committed on a real send; a dry run must not consume stories.
    sections = news.build(lookback_hours=args.hours, remember=not args.dry_run)
    summary = market.summary()

    subject, html, text = render.daily_brief(sections, summary, datetime.now())

    n = sum(len(v) for v in sections.values())
    log.info("%d stories across %d sections", n, len(sections))

    if args.dry_run:
        out = Path("preview_brief.html")
        out.write_text(html, encoding="utf-8")
        log.info("dry run -- wrote %s (%s)", out, subject)
        return 0

    return 0 if mailer.send(subject, html, text) else 1


if __name__ == "__main__":
    raise SystemExit(main())
