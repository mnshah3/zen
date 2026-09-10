"""Signal contract shared by every strategy.

Two rules that exist to stop this becoming another 30%-CAGR fiction:

1. A signal must carry its own counter-argument. If a strategy cannot say
   what would make it wrong, it has not been thought through.
2. Nothing here executes. Signals are evidence for a human decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Protocol


@dataclass
class Signal:
    symbol: str
    action: str                       # "buy" | "sell"
    asof: date
    strategy: str
    name: str = ""                    # company name, when known
    facts: dict[str, str] = field(default_factory=dict)
    rationale: list[str] = field(default_factory=list)
    against: list[str] = field(default_factory=list)
    conviction: float = 0.0           # 0-1, comparable within a strategy only

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "action": self.action,
            "name": self.name,
            "facts": self.facts,
            "rationale": self.rationale,
            "against": self.against,
        }

    def __post_init__(self) -> None:
        if self.action not in ("buy", "sell"):
            raise ValueError(f"action must be buy or sell, got {self.action!r}")
        if not self.rationale:
            raise ValueError(f"{self.symbol}: a signal must state its rationale")
        if not self.against:
            raise ValueError(
                f"{self.symbol}: a signal must state the case against it. "
                "If you cannot name one, the idea is not ready."
            )


class Strategy(Protocol):
    """Anything that can look at the archive and emit signals."""

    name: str
    min_history_days: int

    # False until a walk-forward backtest has been run, survived the look-ahead
    # detector, and been reported with its trial count. Default False and
    # opt-in, because the failure that matters is a strategy quietly reaching
    # the inbox looking like advice -- which is exactly what happened with the
    # momentum baseline: an email headed "To buy (15)" from a screen whose
    # own README calls it a calibration test.
    validated: bool

    def generate(self, con, asof: date) -> list[Signal]:
        ...
