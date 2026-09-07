"""Rebuild the DuckDB archive from committed parquet.

The database is derived data and is never committed, so any fresh checkout --
including every GitHub Actions run -- starts without one. This reconstructs it
in a few seconds.
"""

from __future__ import annotations

import logging

from zen.data import store


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    con = store.connect()
    try:
        rows = store.rebuild_from_parquet(con)
        cov = store.coverage(con)
    finally:
        con.close()
    logging.info("rebuilt %s rows | %s to %s | %s sessions | %s symbols",
                 f"{rows:,}", cov["first"], cov["last"],
                 f"{cov['trading_days']:,}", f"{cov['symbols']:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
