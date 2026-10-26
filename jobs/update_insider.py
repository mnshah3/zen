"""Insider trades from NSE (zen.data.insider): the daily catch-up, and the one-off backfills.

    python -m jobs.update_insider                     # filings of the last 10 days (new system), daily
    python -m jobs.update_insider --new-all           # every filing since 3 May 2026 (new system)
    python -m jobs.update_insider --backfill-old      # every company, Nov 2015 to 30 Apr 2026 (old system)

The old system answers one company at a time, so its backfill asks for every symbol zen has priced
since 2015 (a company renamed since answers under its current symbol), resuming from a list of the
symbols done (data/insider/_old_done.txt). Requests are spaced RATE_S apart, under NSE's limit.
"""

from __future__ import annotations

import argparse
import logging
import time
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from zen.data import financials, financials_legacy, insider

log = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))
RATE_S = 0.35
OLD_START, OLD_END = date(2015, 1, 1), date(2026, 4, 30)
DONE = insider.DIR / "_old_done.txt"
FLUSH = 100


def session():
    s = financials._session()
    s.get(financials_legacy.WARMUP, timeout=30)
    return s


def daily(days: int = 10) -> int:
    s = session()
    end = datetime.now(IST).date()
    filings = insider.list_new(s, end - timedelta(days=days), end)
    df = insider.fetch_new(s, filings, insider.stored_urls(), renew=session)
    insider.write(df)
    log.info("insider: %d filings listed, %d disclosures written", len(filings), len(df))
    return len(df)


def new_all() -> int:
    s = session()
    filings = insider.list_new(s)
    have = insider.stored_urls()
    todo = [f for f in filings if f.get("xmlFileName") not in have]
    log.info("insider: %d filings since 3 May 2026, %d to read", len(filings), len(todo))
    n = 0
    for i in range(0, len(todo), FLUSH):
        if i:
            s = session()                                            # one warm-up request a batch, never a stale session
        df = insider.fetch_new(s, todo[i:i + FLUSH], have, renew=session)
        insider.write(df)
        n += len(df)
        log.info("insider: %d of %d filings read, %d disclosures", min(i + FLUSH, len(todo)), len(todo), n)
    return n


def backfill_old() -> int:
    import duckdb
    from zen.universe import pit
    con = duckdb.connect(str(pit.DB_PATH), read_only=True)
    symbols = [r[0] for r in con.execute("SELECT DISTINCT symbol FROM prices WHERE series IN ('EQ', 'BE', 'BZ') "
                                         "ORDER BY 1").fetchall()]
    con.close()
    done = set(DONE.read_text(encoding="utf-8").split()) if DONE.exists() else set()
    todo = [x for x in symbols if x not in done and insider.SYMBOL_RX.match(x)]
    log.info("insider old: %d symbols, %d to ask", len(symbols), len(todo))
    s, frames, n, fails, in_row = session(), [], 0, [], 0
    DONE.parent.mkdir(parents=True, exist_ok=True)
    for i, sym in enumerate(todo, 1):
        try:
            df = insider.fetch_old(s, sym, OLD_START, OLD_END, timeout=45)
            in_row = 0
            if len(df):
                frames.append(df)
            with DONE.open("a", encoding="utf-8") as f:
                f.write(sym + "\n")
        except Exception as e:                                       # noqa: BLE001 - retried on the next run
            fails.append(sym)
            in_row += 1
            log.warning("insider old: %s: %s", sym, e)
            if in_row >= insider.RENEW_AFTER:
                try:
                    s, in_row = session(), 0                         # a fresh session after refusals in a row
                except Exception as e2:                              # noqa: BLE001
                    log.warning("insider old: no fresh session: %s", e2)
        if len(frames) >= FLUSH or i == len(todo):
            if frames:
                part = pd.concat(frames, ignore_index=True)
                insider.write(part)
                n += len(part)
                frames = []
            log.info("insider old: %d of %d symbols, %d disclosures written, %d failed", i, len(todo), n, len(fails))
        time.sleep(RATE_S)
    if fails:
        log.warning("insider old: still failing, run again: %s", " ".join(fails[:50]))
    return n


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--new-all", action="store_true")
    ap.add_argument("--backfill-old", action="store_true")
    ap.add_argument("--days", type=int, default=10)
    a = ap.parse_args(argv)
    if a.backfill_old:
        backfill_old()
    elif a.new_all:
        new_all()
    else:
        daily(a.days)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
