"""A persistent count of every hypothesis ever tested.

The reason this exists: test enough variants and one will look excellent by
chance alone. With twenty independent tries, the best result at a 5% threshold
is roughly what you would expect from noise. Reporting that best result
without saying how many were tried is the most common way a backtest lies, and
it lies most convincingly because every individual number in it is correct.

So every run appends here, whether it looked good or not, and the count is
carried into the reporting. A variant that is quietly abandoned still counts:
the search happened, and the search is what inflates the winner.

The log is committed to the repository deliberately. It is the one file that
makes the difference between a strategy and a story, and it only works if it
is inconvenient to lose.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from math import erf, log as ln, sqrt
from pathlib import Path

log = logging.getLogger(__name__)

LOG_PATH = Path("state/trials.jsonl")


def record(study: str, variant: dict, result: dict,
           path: Path = LOG_PATH) -> int:
    """Append one tested hypothesis. Returns the running total for this study."""
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "study": study,
        "variant": variant,
        "result": result,
        # Stamped by the caller when determinism matters; otherwise now.
        "at": result.pop("_at", None) or datetime.now().isoformat(timespec="seconds"),
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")
    return count(study, path)


def count(study: str | None = None, path: Path = LOG_PATH) -> int:
    if not path.exists():
        return 0
    n = 0
    with path.open(encoding="utf-8") as f:
        for line in f:
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if study is None or e.get("study") == study:
                n += 1
    return n


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + erf(x / sqrt(2)))


def deflated_sharpe(observed_sharpe: float, n_trials: int, n_obs: int,
                    skew: float = 0.0, kurtosis: float = 3.0) -> float:
    """Probability the result is real once the search is accounted for.

    Bailey and Lopez de Prado's deflated Sharpe ratio. The intuition is simple
    even where the algebra is not: the more variants you tried, the higher the
    best one has to be before it means anything. A Sharpe of 1.5 from a single
    hypothesis is interesting; the same 1.5 selected as the best of two hundred
    is roughly what noise produces.

    Returns a probability. Below about 0.95, the result has not cleared the
    search that produced it.
    """
    if n_trials < 1 or n_obs < 2:
        return float("nan")

    # Expected maximum Sharpe across n independent trials of zero true skill.
    euler = 0.5772156649
    e_max = sqrt(2 * ln(max(n_trials, 2))) * (1 - euler) + \
        euler * sqrt(2 * ln(max(n_trials, 2) / 2.71828))
    # Variance of the Sharpe estimator, adjusted for non-normal returns.
    denom = sqrt(1 - skew * observed_sharpe +
                 ((kurtosis - 1) / 4) * observed_sharpe ** 2)
    if denom <= 0:
        return float("nan")
    z = (observed_sharpe - e_max) * sqrt(n_obs - 1) / denom
    return _norm_cdf(z)


def summary(path: Path = LOG_PATH) -> dict:
    """Every study and how many hypotheses it has consumed."""
    if not path.exists():
        return {}
    counts: dict[str, int] = {}
    with path.open(encoding="utf-8") as f:
        for line in f:
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            s = e.get("study", "unknown")
            counts[s] = counts.get(s, 0) + 1
    return counts
