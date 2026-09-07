"""Brief B -- the strategy alert.

Unlike the daily brief, this is silent by default. It runs on a schedule but
only sends when something actually fires, because an alert that arrives every
day stops being an alert.

    python -m jobs.signal_check --dry-run
    python -m jobs.signal_check
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import date, datetime
from pathlib import Path

from zen.data import store
from zen.notify import mailer, render
from zen.signals.momentum import Momentum

log = logging.getLogger(__name__)

# Last dispatched set, so an unchanged ranking does not re-alert every run.
SENT = Path("state/last_signals.json")

STRATEGIES = [Momentum()]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true",
                   help="send even if the signal set is unchanged")
    return p.parse_args()


def _previous() -> set[str]:
    if not SENT.exists():
        return set()
    try:
        return set(json.loads(SENT.read_text()).get("keys", []))
    except json.JSONDecodeError:
        return set()


def _remember(keys: set[str], asof: date) -> None:
    SENT.parent.mkdir(parents=True, exist_ok=True)
    SENT.write_text(json.dumps(
        {"asof": asof.isoformat(), "keys": sorted(keys)}, indent=0))


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args()

    con = store.connect()
    try:
        asof = con.execute("SELECT max(date) FROM prices").fetchone()[0]
        if asof is None:
            log.error("archive is empty -- run jobs.update_prices first")
            return 1

        signals = []
        for strat in STRATEGIES:
            found = strat.generate(con, asof)
            log.info("%s: %d signals", strat.name, len(found))
            signals.extend(found)
    finally:
        con.close()

    if not signals:
        log.info("nothing fired; staying quiet")
        return 0

    keys = {f"{s.strategy}:{s.action}:{s.symbol}" for s in signals}
    if not args.force and keys == _previous():
        log.info("signal set unchanged since last alert; staying quiet")
        return 0

    context = {
        "strategy": ", ".join(sorted({s.strategy for s in signals})),
        "note": (f"Ranking as of the {asof} close. Entry is assumed at the next "
                 "session open, which is what the backtest measures."),
    }
    subject, html, text = render.signal_alert(
        [s.to_dict() for s in signals], context, datetime.now())

    if args.dry_run:
        out = Path("preview_signal.html")
        out.write_text(html, encoding="utf-8")
        log.info("dry run -- wrote %s (%s)", out, subject)
        return 0

    if mailer.send(subject, html, text):
        _remember(keys, asof)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
