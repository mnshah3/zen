"""Fetch NSE's delivery positions, bulk and block deals, FII/DII flows and surveillance lists
(zen.data.market_daily). Run each evening after the close by "4. daily indices".

    python -m jobs.update_market_daily                          # catch up delivery, read the rest
    python -m jobs.update_market_daily --start 2015-01-01 --only delivery   # the delivery backfill

Each part runs on its own: one source failing does not stop the others, and the job exits
non-zero if any failed, after whatever was fetched has been written.
"""

from __future__ import annotations

import argparse
import logging
import time
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from zen.data import financials, financials_legacy, market_daily as md

log = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))
RATE_S = 0.35                  # seconds between archive requests, under the project's 8 a second
FLUSH_EVERY = 60               # sessions between writes during a backfill


def delivery(start: date, end: date) -> int:
    s = financials_legacy.document_session()
    have = md.stored_delivery_days()
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    todo = [d for d in days if d.weekday() < 5 and d not in have]
    log.info("delivery: %d sessions to read, %s to %s", len(todo), start, end)
    got, frames = 0, []
    for i, d in enumerate(todo, 1):
        df = md.fetch_delivery(s, d)
        if df is not None and not df.empty:
            frames.append(df)
            got += 1
        if len(frames) >= FLUSH_EVERY:
            md.write_delivery(pd.concat(frames, ignore_index=True))
            frames = []
            log.info("delivery: %d/%d read, %d sessions with a file", i, len(todo), got)
        time.sleep(RATE_S)
    if frames:
        md.write_delivery(pd.concat(frames, ignore_index=True))
    log.info("delivery: %d sessions written", got)
    return got


def deals(now: datetime) -> int:
    s = financials_legacy.document_session()
    n = 0
    for kind, url in md.DEAL_URLS.items():
        r = s.get(url, timeout=30)
        r.raise_for_status()
        df = md.parse_deals(r.text, kind, now)
        md.write_yearly(df, md.DEALS_DIR, ["date", "kind", "symbol", "client", "side", "qty", "price"], "date", md.DEAL_COLUMNS)
        n += len(df)
        time.sleep(RATE_S)
    log.info("deals: %d rows", n)
    return n


def site_session():
    s = financials._session()
    s.get(financials_legacy.WARMUP, timeout=30)
    return s


def flows(now: datetime, s) -> int:
    r = s.get(md.FII_DII_URL, timeout=30)
    r.raise_for_status()
    df = md.parse_fii_dii(r.json(), now)
    md.write_yearly(df, md.FLOWS_DIR, ["date", "category"], "date", md.FLOW_COLUMNS)
    log.info("fii_dii: %s", df[["date", "category", "net_cr"]].to_dict("records"))
    return len(df)


def surveillance(now: datetime, s) -> int:
    r = s.get(md.ASM_URL, timeout=30)
    r.raise_for_status()
    asm = md.parse_asm(r.json(), now)
    time.sleep(RATE_S)
    r = s.get(md.GSM_URL, timeout=30)
    r.raise_for_status()
    gsm = md.parse_gsm(r.json(), now)
    df = pd.concat([asm, gsm], ignore_index=True)
    if asm.empty or gsm.empty:
        raise RuntimeError(f"a surveillance list came back empty (ASM {len(asm)}, GSM {len(gsm)})")
    md.write_yearly(df, md.SURV_DIR, ["observed_dt", "list", "symbol"], "observed_dt", md.SURV_COLUMNS)
    log.info("surveillance: %s", df.groupby("list").size().to_dict())
    return len(df)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=date.fromisoformat)
    ap.add_argument("--end", type=date.fromisoformat)
    ap.add_argument("--days", type=int, default=10, help="delivery: look back this far for missing sessions")
    ap.add_argument("--only", choices=["delivery", "deals", "flows", "surveillance"])
    a = ap.parse_args(argv)
    now = datetime.now(IST).replace(tzinfo=None, microsecond=0)
    end = a.end or now.date()
    start = a.start or end - timedelta(days=a.days)
    failed = []
    site = None
    for name in ("delivery", "deals", "flows", "surveillance"):
        if a.only and name != a.only:
            continue
        try:
            if name == "delivery":
                delivery(start, end)
            elif name == "deals":
                deals(now)
            else:
                site = site or site_session()
                (flows if name == "flows" else surveillance)(now, site)
        except Exception as e:                                       # noqa: BLE001
            failed.append(name)
            log.error("%s failed: %s", name, e)
    if failed:
        log.error("failed: %s", ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
