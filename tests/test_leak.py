"""The leak detector has to be shown to fail on a known-bad strategy.

A test that only ever passes proves nothing, so this file contains a strategy
that cheats deliberately. If Leaky ever passes future_blindness, the detector
is broken and every result downstream of it is worthless.
"""

from __future__ import annotations

import logging
from datetime import date

import pytest

from zen.data import store
from zen.signals.base import Signal
from zen.signals.momentum import Momentum
from zen.validation.leak import LeakDetected, future_blindness

logging.basicConfig(level=logging.INFO)

# Far enough inside the archive that a year of formation history exists.
ASOF = date(2019, 6, 28)


class Leaky:
    """Ranks by what happens NEXT -- the canonical look-ahead bug.

    This is the same shape of error as reading tomorrow's close into today's
    signal: it looks superb in a backtest and is unimplementable in life.
    """

    name = "leaky_baseline"
    min_history_days = 30

    def generate(self, con, asof: date) -> list[Signal]:
        df = con.execute(
            """
            WITH fwd AS (
                SELECT symbol, close,
                       lead(close, 5) OVER (PARTITION BY symbol ORDER BY date) AS future_close,
                       date
                FROM prices
                WHERE isin_code LIKE 'INE%' AND close > 50
            )
            SELECT symbol, close, future_close
            FROM fwd
            WHERE date = ? AND future_close IS NOT NULL
            ORDER BY future_close / close DESC
            LIMIT 10
            """,
            [asof],
        ).df()

        return [
            Signal(
                symbol=r.symbol,
                action="buy",
                asof=asof,
                strategy=self.name,
                conviction=1.0,
                facts={"Close": f"{r.close:.1f}"},
                rationale=["Ranked on forward return, which is cheating."],
                against=["Unimplementable: the information does not exist yet."],
            )
            for r in df.itertuples()
        ]


@pytest.fixture(scope="module")
def con():
    c = store.connect()
    yield c
    c.close()


def test_detector_catches_a_known_leak(con):
    """The whole harness rests on this failing."""
    with pytest.raises(LeakDetected):
        future_blindness(Leaky(), con, ASOF)


def test_momentum_is_future_blind(con):
    report = future_blindness(Momentum(), con, ASOF, raise_on_fail=False)
    assert report["signals_full"] > 0, "no signals generated -- check archive coverage"
    assert report["passed"], f"momentum is leaking: {report}"
