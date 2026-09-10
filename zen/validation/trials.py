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

# Anchored to the repository root, not the working directory. A relative path
# here meant count() returned 0 from any cwd but the repo root -- and it
# returned it silently, so a study run from anywhere else would report "trials
# recorded: 1" and every multiple-testing correction downstream would be
# computed against a search of size one. The ledger is the one thing in this
# repo that must not be able to fail quietly.
LOG_PATH = Path(__file__).resolve().parents[2] / "state" / "trials.jsonl"

# Studies that were renamed mid-flight. The trials already burned under the old
# key are part of the same search and must keep counting against the new one,
# otherwise a rename launders away the cost of every hypothesis tested before
# it. order_combination -> order_combination_v2 orphaned ten.
ALIASES: dict[str, set[str]] = {
    "order_combination_v2": {"order_combination", "order_combination_v2"},
    "order_combination": {"order_combination", "order_combination_v2"},
}


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
    keys = ALIASES.get(study, {study}) if study is not None else None
    n = 0
    with path.open(encoding="utf-8") as f:
        for line in f:
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if keys is None or e.get("study") in keys:
                n += 1
    return n


def lifetime(path: Path = LOG_PATH) -> int:
    """Every hypothesis this repository has ever tested, across all studies.

    The number that belongs in a published sentence. A per-study count
    understates the search: the same archive, the same four years of prices and
    the same 2,500 companies have been interrogated by every study in the log,
    and the winner of the whole search is what gets written up.
    """
    return count(None, path)


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

    # With a single hypothesis there is no selection to deflate for, and the
    # expected-maximum term below is undefined at n=1 -- ln(1/e) is negative,
    # so the sqrt raises. Fall back to the plain probabilistic Sharpe.
    if n_trials == 1:
        denom = sqrt(1 - skew * observed_sharpe +
                     ((kurtosis - 1) / 4) * observed_sharpe ** 2)
        if denom <= 0:
            return float("nan")
        return _norm_cdf(observed_sharpe * sqrt(n_obs - 1) / denom)

    # Expected maximum Sharpe across n independent trials of zero true skill
    # (Bailey and Lopez de Prado). The second term needs n/e > 1, i.e. n >= 3;
    # below that the first term alone is the right approximation.
    euler = 0.5772156649
    e = 2.718281828459045
    a = sqrt(2 * ln(n_trials))
    if n_trials / e > 1:
        e_max = a * (1 - euler) + euler * sqrt(2 * ln(n_trials / e))
    else:
        e_max = a * (1 - euler)

    denom = sqrt(1 - skew * observed_sharpe +
                 ((kurtosis - 1) / 4) * observed_sharpe ** 2)
    if denom <= 0:
        return float("nan")
    z = (observed_sharpe - e_max) * sqrt(n_obs - 1) / denom
    return _norm_cdf(z)


def effective_n(n_events: int, n_clusters: int) -> int:
    """Sample size after accounting for clustering.

    Filing events are not independent observations. Two thousand filings from
    six hundred companies, many overlapping in time and sector, carry nowhere
    near two thousand observations' worth of information -- market-wide moves
    hit whole clusters at once. Significance computed on the raw count is
    therefore overstated, sometimes by a lot.

    The conservative correction used here is the number of distinct clusters,
    which understates the information available but never overstates it. That
    is the right direction to err when the failure mode being guarded against
    is believing a result too readily.
    """
    return max(1, min(n_events, n_clusters))


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
