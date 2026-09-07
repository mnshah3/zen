"""Cross-sectional momentum (12-1) -- deliberately chosen as a CALIBRATION
baseline, not as the strategy we intend to run.

Why start with something this well known: momentum is one of the most heavily
documented equity effects there is, including in Indian equities, and its
rough magnitude is known from the literature. That makes it a test of the
harness rather than of the idea. If our validation says this earns 40% a year,
the harness is broken. If it says "modest, regime-dependent, painful in
reversals", the harness is telling the truth and can be trusted on ideas whose
answer we do not already know.

The real strategy comes after the harness has been calibrated. Not before.

Look-ahead surface, stated explicitly so it can be audited:
  - every price used is strictly <= asof
  - the formation window ends 21 trading days before asof (the standard skip,
    which also means the most recent month cannot leak into the ranking)
  - liquidity is measured over the same window, never over the holding period
  - entry is assumed at the NEXT session's open, never at the asof close
"""

from __future__ import annotations

import logging
from datetime import date

import pandas as pd

from zen.signals.base import Signal

log = logging.getLogger(__name__)


class Momentum:
    name = "momentum_12_1"
    min_history_days = 260

    def __init__(self, lookback: int = 252, skip: int = 21, top_n: int = 15,
                 min_turnover_cr: float = 2.0, min_price: float = 20.0):
        self.lookback = lookback
        self.skip = skip
        self.top_n = top_n
        self.min_turnover_cr = min_turnover_cr
        self.min_price = min_price

    def _panel(self, con, asof: date) -> pd.DataFrame:
        """Prices strictly up to asof. The WHERE clause is the leak boundary."""
        return con.execute(
            """
            SELECT date, symbol, close, turnover
            FROM prices
            WHERE date <= ?
              AND isin_code LIKE 'INE%'
              AND series IN ('EQ', 'BE')
              AND close > 0
            ORDER BY symbol, date
            """,
            [asof],
        ).df()

    def generate(self, con, asof: date) -> list[Signal]:
        px = self._panel(con, asof)
        if px.empty:
            return []

        sessions = sorted(px["date"].unique())
        if len(sessions) < self.lookback + self.skip:
            log.warning("need %d sessions, have %d -- backfill first",
                        self.lookback + self.skip, len(sessions))
            return []

        form_end = sessions[-(self.skip + 1)]
        form_start = sessions[-(self.lookback + self.skip)]

        window = px[(px["date"] >= form_start) & (px["date"] <= form_end)]

        first = window.groupby("symbol").first()
        last = window.groupby("symbol").last()
        counts = window.groupby("symbol").size()
        liq = window.groupby("symbol")["turnover"].median() / 1e7

        df = pd.DataFrame({
            "start_px": first["close"],
            "end_px": last["close"],
            "sessions": counts,
            "turnover_cr": liq,
        })

        # A name must have traded through the whole window. This is also what
        # keeps recently listed stocks out of the ranking.
        df = df[df["sessions"] >= self.lookback * 0.9]
        df = df[(df["turnover_cr"] >= self.min_turnover_cr) &
                (df["end_px"] >= self.min_price)]
        if df.empty:
            return []

        df["momentum"] = (df["end_px"] / df["start_px"] - 1) * 100
        df = df.sort_values("momentum", ascending=False)

        latest = px[px["date"] == sessions[-1]].set_index("symbol")["close"]
        picks = df.head(self.top_n)
        breadth = len(df)

        signals = []
        for rank, (symbol, row) in enumerate(picks.iterrows(), start=1):
            pctile = round(100 * (1 - rank / breadth), 1)
            signals.append(Signal(
                symbol=symbol,
                action="buy",
                asof=asof,
                strategy=self.name,
                conviction=round(1 - (rank - 1) / max(len(picks), 1), 3),
                facts={
                    "Rank": f"{rank} of {breadth} eligible",
                    "Formation return": f"{row['momentum']:+.1f}% "
                                        f"({form_start} to {form_end})",
                    "Percentile": f"{pctile}th",
                    "Last close": f"Rs {latest.get(symbol, float('nan')):,.1f}",
                    "Median turnover": f"Rs {row['turnover_cr']:.1f} cr/day",
                    "Entry assumption": "next session open",
                },
                rationale=[
                    f"Formation-window return of {row['momentum']:+.1f}% ranks "
                    f"{rank} of {breadth} liquid names.",
                    f"The most recent {self.skip} sessions are excluded from the "
                    "ranking, so this is not a short-term chase.",
                    f"Traded {int(row['sessions'])} of the window's sessions with "
                    f"median turnover of Rs {row['turnover_cr']:.1f} cr, so the "
                    "position is exitable.",
                ],
                against=[
                    "Momentum crashes hard at regime turns; the worst drawdowns "
                    "come precisely when the ranking looks strongest.",
                    "A high formation return may already reflect news now fully "
                    "priced, leaving nothing to capture.",
                    "This baseline uses no valuation or quality screen, so an "
                    "expensive or deteriorating business can rank highly.",
                ],
            ))
        return signals
