"""Fail loudly when any part of the archive has gone stale.

    python -m jobs.data_health

Every daily job is built to keep going when one source fails: the brief still
sends, a dead feed means fewer stories. That is right for the email and wrong
for the archive, because a failure inside a job that never fails is invisible.
From 10 September 2026 the announcement refresh failed on every run for three
weeks behind a green badge.

This job checks two things for each source, and exits 1 if either is stale:
the newest row in the data itself, and the last time its update job recorded
success (zen.data.freshness). Workflows run it last, so the brief is still
sent but the run turns red.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

import numpy as np

from zen.data import freshness, store

# How far each source may lag before it counts as stale. Prices and indices
# are counted in weekdays, so a weekend never counts and up to two weekday
# holidays together (13 to 18 Apr 2022 had a five-day gap) do not raise a false
# alarm. Filings arrive on weekends too, so they are counted in calendar days.
# Results are filed in seasons, so the financials table itself may go quiet for
# weeks; only its job's success is held to a daily standard.
MAX_LAG = {
    "prices": 3,
    "indices": 3,
    "announcements": 4,
    "financials": 30,
}
WEEKDAY_LAG = {"prices", "indices"}
MAX_JOB_AGE = {"announcements": 2, "financials": 3, "corpactions": 3}


def check(con, now: datetime | None = None) -> list[tuple[str, str, bool]]:
    """(source, what was found, ok) for every check."""
    now = now or datetime.now(timezone.utc)
    today = now.date()
    out = []

    def lag_row(name: str, sql: str) -> None:
        try:
            latest = con.execute(sql).fetchone()[0]
        except Exception as e:                                   # noqa: BLE001
            out.append((name, f"unreadable ({e})", False))
            return
        if latest is None:
            out.append((name, "no rows", False))
            return
        d = latest.date() if isinstance(latest, datetime) else latest
        if name in WEEKDAY_LAG:
            lag, unit = int(np.busday_count(d, today)), "weekdays"
        else:
            lag, unit = (today - d).days, "days"
        out.append((name, f"latest {d} ({lag} {unit} ago, limit {MAX_LAG[name]})",
                    lag <= MAX_LAG[name]))

    lag_row("prices", "SELECT max(date) FROM prices")
    lag_row("indices", "SELECT max(date) FROM indices")
    lag_row("announcements", "SELECT max(an_dt) FROM announcements")
    lag_row("financials", "SELECT max(broadcast_dt) FROM financials")

    stamps = freshness.read()
    for name, limit in MAX_JOB_AGE.items():
        raw = stamps.get(name)
        if not raw:
            out.append((f"{name} job", "never recorded a successful run", False))
            continue
        when = datetime.fromisoformat(raw)
        age = now - when
        out.append((f"{name} job",
                    f"last success {when:%Y-%m-%d %H:%M} UTC ({age.days} days ago, "
                    f"limit {limit})", age <= timedelta(days=limit)))
    return out


def main() -> int:
    con = store.connect()
    rows = check(con)
    con.close()
    width = max(len(r[0]) for r in rows)
    for name, found, ok in rows:
        print(f"{'ok   ' if ok else 'STALE'}  {name:<{width}}  {found}")
    bad = [r for r in rows if not r[2]]
    if bad:
        print(f"\n{len(bad)} source(s) stale: {', '.join(r[0] for r in bad)}")
        return 1
    print("\nall sources fresh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
