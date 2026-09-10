"""Rebuild the DuckDB archive from committed parquet.

The database is derived data and is never committed, so any fresh checkout --
including every GitHub Actions run -- starts without one. This reconstructs it.

REBUILD EVERY TABLE, NOT THE ONES SOMEONE REMEMBERED

An earlier version rebuilt prices and announcements only. That was invisible
locally, where the database had accumulated corpactions and indices from
backfills run months earlier, and fatal in CI, where nothing exists that this
job does not create. The daily brief died on

    CatalogException: Table with name corpactions does not exist

the first morning a chart needed corporate-action adjustment -- a bug that
could only ever appear on the runner, in a table this job silently declined to
build.

So the list below is the archive. A table with parquet on disk and no entry
here is a failure waiting for the first query that needs it.
"""

from __future__ import annotations

import logging

from zen.data import announcements, corpactions, financials, indices, store

log = logging.getLogger(__name__)

# Tables the daily brief and the signal check read directly. A failure in one
# of these is fatal, because the alternative is an email built on a table that
# silently came back empty.
REQUIRED = ("prices", "announcements", "corpactions", "indices")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    con = store.connect()
    counts, failed = {}, []

    builders = [
        ("prices", lambda: store.rebuild_from_parquet(con)),
        ("announcements", lambda: announcements.rebuild_from_parquet(con)),
        ("corpactions", lambda: corpactions.rebuild_from_parquet(con)),
        ("indices", lambda: indices.rebuild_from_parquet(con)),
        ("financials", lambda: financials.rebuild_from_parquet(con)),
    ]

    try:
        for name, build in builders:
            try:
                counts[name] = build()
            except Exception as e:                               # noqa: BLE001
                failed.append(name)
                log.error("%s: rebuild failed -- %s", name, e)
        cov = store.coverage(con)
    finally:
        con.close()

    log.info("rebuilt %s | %s to %s | %s sessions | %s symbols",
             " | ".join(f"{k} {v:,}" for k, v in counts.items()),
             cov["first"], cov["last"], f"{cov['trading_days']:,}",
             f"{cov['symbols']:,}")

    fatal = [t for t in failed if t in REQUIRED]
    if fatal:
        log.error("required tables missing: %s -- refusing to continue", ", ".join(fatal))
        return 1
    if failed:
        log.warning("optional tables missing: %s", ", ".join(failed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
