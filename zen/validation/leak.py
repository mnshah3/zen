"""Look-ahead detection.

The central test here is deliberately simple and very hard to fool:

    Run the strategy as of date T against the full archive.
    Run the same strategy as of date T against an archive that has been
    physically truncated at T.
    The two answers must be identical.

If they differ, the strategy read data that did not exist on T. It does not
matter whether the leak came from a mis-shifted column, a normalisation
computed over the whole sample, or a join that quietly pulled tomorrow's row --
any of them change the answer, and this catches all of them without needing to
know which one happened.

This is the test that a one-day shift error fails immediately.
"""

from __future__ import annotations

import logging
import tempfile
from datetime import date
from pathlib import Path

import duckdb

from zen.data import store

log = logging.getLogger(__name__)


class LeakDetected(AssertionError):
    pass


# Every table a strategy could read, with the column that dates each row.
# Truncating only prices would leave a strategy free to read tomorrow's
# announcements or an unpublished results filing and still pass the test --
# which is exactly the leak this detector exists to catch, so the table list
# must stay in step with what the archive actually holds.
TRUNCATABLE = [
    ("prices", "date"),
    ("announcements", "an_dt"),
    ("financials", "broadcast_dt"),
    ("indices", "date"),
    ("corpactions", "ex_date"),
]


def _truncated_copy(con, asof: date, path: Path):
    """A physical archive containing nothing after asof.

    Truncation is real rather than a filter in the query, so a strategy that
    ignores its asof argument has no future rows available to find.

    Every table present is truncated on its own date column. A table the
    archive does not yet have is skipped, but a table that exists and is NOT
    truncated would be a hole in the test, so each one is logged.
    """
    out = duckdb.connect(str(path))
    copied = []

    for table, date_col in TRUNCATABLE:
        try:
            rows = con.execute(
                f"SELECT * FROM {table} WHERE CAST({date_col} AS DATE) <= ?",
                [asof]).df()
        except Exception:
            continue          # table absent from this archive
        out.register("chunk", rows)
        out.execute(f"CREATE TABLE {table} AS SELECT * FROM chunk")
        out.unregister("chunk")
        copied.append(f"{table}({len(rows):,})")

    if "prices" not in " ".join(copied):
        raise RuntimeError("prices table missing; cannot run a leak test")
    log.debug("truncated archive at %s: %s", asof, ", ".join(copied))
    return out


def _fingerprint(signals) -> list[tuple]:
    """Comparable, order-independent representation of a signal set."""
    return sorted(
        (s.symbol, s.action, round(s.conviction, 6),
         tuple(sorted(s.facts.items())))
        for s in signals
    )


def future_blindness(strategy, con, asof: date, *, raise_on_fail: bool = True) -> dict:
    """Assert the strategy cannot see past asof.

    Returns a report dict; raises LeakDetected on failure unless told not to.
    """
    full = _fingerprint(strategy.generate(con, asof))

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "truncated.duckdb"
        tcon = _truncated_copy(con, asof, path)
        try:
            truncated = _fingerprint(strategy.generate(tcon, asof))
        finally:
            tcon.close()

    passed = full == truncated
    report = {
        "test": "future_blindness",
        "strategy": strategy.name,
        "asof": asof,
        "passed": passed,
        "signals_full": len(full),
        "signals_truncated": len(truncated),
    }

    if not passed:
        only_full = [s[0] for s in full if s not in truncated]
        only_trunc = [s[0] for s in truncated if s not in full]
        report["only_with_future_data"] = only_full[:10]
        report["only_without_future_data"] = only_trunc[:10]
        msg = (
            f"{strategy.name} produced different signals on {asof} depending on "
            f"whether data after {asof} was present. It is reading the future.\n"
            f"  only when future data present: {only_full[:6]}\n"
            f"  only when it is absent:        {only_trunc[:6]}"
        )
        log.error(msg)
        if raise_on_fail:
            raise LeakDetected(msg)
    else:
        log.info("%s: future-blind at %s (%d signals both ways)",
                 strategy.name, asof, len(full))

    return report


def stability(strategy, con, asofs: list[date]) -> list[dict]:
    """Run the blindness test across several dates.

    A single passing date proves little -- a leak can be conditional on a
    month boundary, an expiry, or a corporate action.
    """
    return [future_blindness(strategy, con, d, raise_on_fail=False) for d in asofs]
