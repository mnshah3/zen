"""Daily job: fetch any bhavcopy days we are missing, store them, report.

Idempotent -- re-running never duplicates rows, so a failed day self-heals
on the next run.

    python -m jobs.update_prices              # last 7 days
    python -m jobs.update_prices --days 30
    python -m jobs.update_prices --start 2015-01-01 --end 2015-12-31
"""

from __future__ import annotations

import argparse
import logging
from datetime import date, timedelta
from pathlib import Path

from zen.data import bhavcopy, store
from zen.notify import telegram

log = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=7, help="lookback window")
    p.add_argument("--start", type=date.fromisoformat)
    p.add_argument("--end", type=date.fromisoformat)
    p.add_argument("--notify", action="store_true", help="send a Telegram summary")
    return p.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args()

    end = args.end or date.today()
    start = args.start or (end - timedelta(days=args.days))

    con = store.connect()
    have = store.stored_dates(con)

    session = bhavcopy._session()
    added_rows, added_days, d = 0, [], start
    while d <= end:
        if d.weekday() < 5 and d not in have:
            df = bhavcopy.fetch_day(d, raw_dir=Path("data/raw"), session=session)
            if df is not None and not df.empty:
                n = store.upsert(con, df)
                store.write_parquet(df)
                added_rows += n
                added_days.append(d)
                log.info("%s stored %d rows", d, n)
        d += timedelta(days=1)

    cov = store.coverage(con)
    con.close()

    msg = (
        f"<b>Zen | price update</b>\n"
        f"new days: {len(added_days)}  ({added_rows:,} rows)\n"
        f"archive: {cov['first']} to {cov['last']}\n"
        f"{cov['trading_days']:,} trading days | "
        f"{cov['symbols']:,} symbols | {cov['rows']:,} rows"
    )
    print(msg.replace("<b>", "").replace("</b>", ""))
    if args.notify:
        telegram.send(msg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
