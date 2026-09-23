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
                    skew: float = 0.0, kurtosis: float = 3.0,
                    var_trial_sharpe: float | None = None) -> float:
    """Probability the result is real once the search is accounted for.

    Bailey and Lopez de Prado (2014). The more variants were tried, the higher
    the best one has to be before it means anything. A Sharpe of 1.5 from a
    single hypothesis is interesting; the same 1.5 selected as the best of two
    hundred is roughly what noise produces.

    EVERY Sharpe here is PER PERIOD (daily for a daily series), not annualised.
    The previous version scaled the expected maximum by nothing at all, so it
    compared a per-period Sharpe of about 0.08 with an expected maximum of about
    2.8 and returned 0 or 1 for any input. An audit caught it on 2026-09-23; the
    published v1 figure had come from a separate, correct function.

    `var_trial_sharpe` is the variance of the per-period Sharpe ratios across
    the trials that were tried. The paper uses the observed cross-trial
    variance. When it is not supplied, the sampling variance of a single
    Sharpe estimate under the null is used instead, (1 + SR^2/2) / n_obs. The
    two can give very different answers (0.42 to 0.99 on v1), so callers
    should report which one they used.

    Returns a probability. Below about 0.95 the result has not cleared the
    search that produced it.
    """
    if n_trials < 1 or n_obs < 2:
        return float("nan")
    denom = sqrt(max(1e-12, 1 - skew * observed_sharpe
                     + ((kurtosis - 1) / 4) * observed_sharpe ** 2))
    if n_trials == 1:
        return _norm_cdf(observed_sharpe * sqrt(n_obs - 1) / denom)
    if var_trial_sharpe is None:
        var_trial_sharpe = (1 + 0.5 * observed_sharpe ** 2) / n_obs
    euler = 0.5772156649015329
    e_max = sqrt(var_trial_sharpe) * ((1 - euler) * _norm_ppf(1 - 1.0 / n_trials)
                                      + euler * _norm_ppf(1 - 1.0 / (n_trials * 2.718281828459045)))
    return _norm_cdf((observed_sharpe - e_max) * sqrt(n_obs - 1) / denom)


def _norm_ppf(p: float) -> float:
    """Inverse normal CDF (Acklam), accurate to about 1e-9, no scipy needed."""
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    if p < 0.02425:
        q = sqrt(-2 * ln(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > 1 - 0.02425:
        q = sqrt(-2 * ln(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q, r = p - 0.5, (p - 0.5) ** 2
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


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
