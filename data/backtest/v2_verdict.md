# Strategy v2 against v1: A1's adoption, A2's verdict and safeguard 2

Written by `jobs/v2_verdict.py` from the measurement files of `data/backtest/v1_final` (v1), `data/backtest/v2a` (v2, variant a) and `data/backtest/v2b` (v2, variant b), all on the same clock: the open of 2019-02-15, every close, and the open of 2026-09-18 (1873 marks). No backtest was run to write it. Every figure below gives the file and key it was read from; the figures computed here (safeguard 2) are in `v2_verdict.json`, `data/backtest/v2_verdict_windows_3y.csv` and `data/backtest/v2_verdict_cscv_splits.csv`, and the sha256 of every file read is at the end. Each run's own measurements are summarised in its `baseline_report.md` (`data/backtest/v2a/baseline_report.md`, `data/backtest/v2b/baseline_report.md`).

## The answer

**v1 stays: v2 does not replace it.** A1's test adopts variant (a), staggered entry. Against v1 on the same data it passes 2 of A2's five rules (1 and 5) and fails 2, 3 and 4. The spec lets v2 replace v1 only if all five hold.

The evidence is weaker than v1's by construction: v2's rules were written after v1's full-period results were known, so every figure here is on data v1 had already seen, and there is no fresh holdout (v2-spec, 'How it gets tested, and the honest problem'). Each variant was run once on the full period, and both runs are counted in the deflated Sharpe below. Before that, both engines ran each variant on the in-sample period only, to check they agree (data/backtest/v2a_is, v2b_is and their _check twins), and the production engine's first full-period attempt stopped with an error before writing any result (disclosed in the v2 spec, 2026-09-28).

## A1: which way to enter

(b) is adopted only if all three hold; otherwise (a).

| Rule | (a) | (b) | Holds | Source |
|---|---|---|---|---|
| 1. the average price paid for new positions, relative to the adjusted close on the session before each position's decision date, is lower in (b) | 1.037586 | 1.033194 | yes | `data/backtest/v2a/a1.json: v_weighted_ratio` and the same key in `data/backtest/v2b/a1.json` |
| 2. annual return (full-period CAGR) is not lower in (b) | 21.253% | 19.928% | no | `data/backtest/v2a/libcheck.json: full_period.strategy.exact.cagr_calendar` and the same key in `data/backtest/v2b/libcheck.json` |
| 3. maximum drawdown (full period) is not worse in (b) | -26.338% | -26.341% | no | `data/backtest/v2a/libcheck.json: full_period.strategy.exact.max_drawdown` and the same key in `data/backtest/v2b/libcheck.json` |

Rule 1: new positions cost 1.0332 times the close before their decision date in (b) and 1.0376 in (a), so buying on A1's signal did lower the price paid. Rule 2: (b) grew 19.93% a year and (a) 21.25%, 1.33 points less. Rule 3: (b)'s worst fall was -26.341% and (a)'s -26.338%, 0.0033 points deeper. Rules 2 and 3 fail, so **(a), staggered entry is adopted**.

## A2: v2 (a) against v1

v1's figures are the baseline's (`data/backtest/v1_final/baseline_report.json`); v2's are read from the same file and key in `data/backtest/v2a`.

v1's key in that file is `a2_acceptance_quantities.<measure>.value`, with <measure> as in the second column; rule 1's bar is `a2_acceptance_quantities.max_drawdown.bar_for_v2.v2_max_drawdown_must_be_at_least`.

| Rule | Measure | v1 | v2 | Needed | Passes | Source of v2's figure |
|---|---|---|---|---|---|---|
| 1 | Maximum drawdown; `max_drawdown` | -33.56% | -26.34% | at least -28.56% | yes | `data/backtest/v2a/libcheck.json: full_period.strategy.exact.max_drawdown` |
| 2 | Sortino (quantstats); `sortino` | 1.936 | 1.744 | above 1.936 | no | `data/backtest/v2a/libcheck.json: full_period.strategy.exact.sortino` |
| 2 | Calmar (quantstats); `calmar` | 0.937 | 0.826 | above 0.937 | no | `data/backtest/v2a/libcheck.json: full_period.strategy.exact.calmar` |
| 3 | IIMA four-factor alpha, a year; `iima_alpha_annual` | 5.74% | 1.07% | at least 5.74% | no | `data/backtest/v2a/libcheck.json: regression_full_period.strategy.exact.alpha_annual` |
| 3 | t of that alpha (HAC, 3 lags); `iima_alpha_t` | 1.21 | 0.23 | at least 1.21 | no | `data/backtest/v2a/libcheck.json: regression_full_period.strategy.exact.alpha_t` |
| 4 | Turnover a year, incl. initial build; `turnover_annual_incl_initial` | 1.958 | 1.986 | at most 1.958 | no | `data/backtest/v2a/verify.json: turnover_annual_incl_initial` |
| 5 | Capacity: p95 trade / 60-session median turnover; `capacity_p95_share_of_turnover` | 1.159% | 0.418% | at most 1.159% | yes | `data/backtest/v2a/verify.json: capacity_all_trades.p95` |

Rule 1 holds: v2's worst fall is 7.22 points shallower than v1's, where at least 5 points shallower was needed. Rule 2 fails on Sortino (1.744 against v1's 1.936) and Calmar (0.826 against v1's 0.937). Rule 3 fails on alpha (1.07% against v1's 5.74%) and its t (0.23 against v1's 1.21). Rule 4 fails on turnover (1.986 against v1's 1.958). Rule 5 holds. Rule 4 fails narrowly (turnover 1.4% from v1's figure), so the verdict does not rest on it: rules 2 and 3 fail as well. The alphas are over the same 82 months, 2019-03 to 2025-12, the last month IIMA has published.

Beside the A2 figures, not used for them: the hand-written regression's t with the n/(n-k) correction is 0.22 for v2 and 1.17 for v1 (`data/backtest/v2a/verify.json: attribution.strategy.alpha_t`); capacity counting only fills on a decision date is 0.434% for v2 and 0.976% for v1 (`data/backtest/v2a/verify.json: capacity.p95_pct`).

## Safeguard 2: how much to trust it

### Is A1's choice overfit? Combinatorially symmetric cross-validation

The returns of (a) and (b) were cut into 10 contiguous blocks (188, 188, 187, 187, 187, 187, 187, 187, 187, 187 returns). For each of the 252 ways to choose 5 blocks as the training half, A1's rule picked a variant on those blocks and was then applied to the other five. It picked (b) on 0 training halves and (a) on 252. **In 0 of 252 splits (0.0%) the pick lost on the test half** (the rule, applied there, picked the other variant): the probability of backtest overfitting of this one choice. On all ten blocks the rule picks (a), as the run files do. New positions per block: 30, 16, 18, 16, 12, 20, 22, 18, 17, 17. A low figure here is close to automatic: (a) is the default and (b) must win all three tests, so this checks that the choice was not a fluke of one stretch of data, not that A1 has no value.

Each criterion alone on the test half, the pick against the other variant: lower growth in 1.2% of splits, a deeper worst fall in 50.0%, a higher entry price in 86.1%. Source: `data/backtest/v2_verdict.json: safeguard_2.cscv_a1_choice`, one row per split in `data/backtest/v2_verdict_cscv_splits.csv`.

### How sure is the gap? Stationary block bootstrap

v2's compound annual growth minus v1's over the whole clock is -9.42 points, 90% interval -17.98 to -2.29; 98.6% of 5,000 draws are at or below zero. The same bootstrap of each against its own equal-weight universe: v2 -2.57 points (-14.84 to +8.63), v1 +8.65 (-3.63 to +20.41). Source: `data/backtest/v2_verdict.json: safeguard_2.bootstrap`.

### Every three-year window

1135 windows, the first 2019-02-15 to 2022-02-15, the last 2023-09-18 to 2026-09-18; they overlap, so this describes one path rather than sampling many.

| | v2 | v1 |
|---|---|---|
| Three-year CAGR: worst / median / best | 15.4% / 26.5% / 34.3% | 21.8% / 39.4% / 54.6% |
| Worst fall within a window: worst / median / mildest | -26.3% / -23.8% / -21.3% | -33.6% / -30.7% / -23.8% |

v2 grew faster than v1 in 0.0% of windows and fell less in 100.0% (by 5 points or more in 69.1%). Source: `data/backtest/v2_verdict.json: safeguard_2.windows_3y`, every window in `data/backtest/v2_verdict_windows_3y.csv`.

## Could the owner just buy the index? (A4)

v2 (a) against each index's total return, whole period and the index's live part. A 'tie' means the reading with no overnight move where NSE printed no open gives the other answer. v1's whole-period excess is beside it.

| Index | Whole: v2 excess | v2 | v1 excess | Live part | Live: v2 excess | v2 | Source |
|---|---|---|---|---|---|---|---|
| Nifty 500 | +6.89% | beat | +16.32% | not a factor index |  |  | `data/backtest/v2a/attribution.json: benchmarks.nifty500.whole_period.excess_cagr` |
| Nifty Midcap 150 | +0.88% | beat | +10.30% | not a factor index |  |  | `data/backtest/v2a/attribution.json: benchmarks.midcap150.whole_period.excess_cagr` |
| Nifty Smallcap 250 | +1.11% | beat | +10.54% | not a factor index |  |  | `data/backtest/v2a/attribution.json: benchmarks.smallcap250.whole_period.excess_cagr` |
| Nifty200 Momentum 30 | +5.63% | beat | +15.06% | from 2020-10-12 (5.9 years) | +7.63% | beat | `data/backtest/v2a/attribution.json: benchmarks.momentum30.whole_period.excess_cagr` |
| Nifty500 Value 50 | -1.72% | did not beat | +7.70% | from 2024-12-16 (1.8 years, too short to judge) | -0.32% | did not beat | `data/backtest/v2a/attribution.json: benchmarks.value50.whole_period.excess_cagr` |
| Nifty200 Quality 30 | +9.34% | beat | +18.77% | whole period | same | beat | `data/backtest/v2a/attribution.json: benchmarks.quality30.whole_period.excess_cagr` |
| Nifty100 Low Volatility 30 | +7.94% | beat | +17.37% | whole period | same | beat | `data/backtest/v2a/attribution.json: benchmarks.lowvol30.whole_period.excess_cagr` |
| Nifty Alpha 50 | -2.23% | did not beat | +7.20% | whole period | same | did not beat | `data/backtest/v2a/attribution.json: benchmarks.alpha50.whole_period.excess_cagr` |

Over the whole period v2 beat Nifty200 Momentum 30, Nifty200 Quality 30 and Nifty100 Low Volatility 30 and did not beat Nifty500 Value 50 and Nifty Alpha 50. Each live part gives the same answer. A live part starts on the first date NSE printed an opening level for the index (jobs/attribution.py, LIVE_START), used because NSE's launch documents could not be retrieved; the three indices with an open at the clock's start count as live for the whole period. Too short to judge on its own (under 3 years live): Nifty500 Value 50 (1.8 years). With no overnight move where NSE printed no open, the excess changes by at most 0.035 points and no answer changes.

## Is the alpha a quality premium? (A5)

| Version | Months | v2 alpha without, t | v2 alpha with, t | v2 quality loading | v1 alpha without, t | v1 alpha with, t | Source (v2) |
|---|---|---|---|---|---|---|---|
| margin | 82 (2019-03 to 2025-12) | 1.07%, 0.23 | 0.69%, 0.15 | 0.12 | 5.74%, 1.21 | 5.04%, 1.11 | `data/backtest/v2a/attribution.json: quality.margin` |
| roce | 34 (2023-03 to 2025-12) | 7.30%, 1.33 | 0.12%, 0.02 | 0.76 | 11.27%, 2.61 | 3.31%, 0.74 | `data/backtest/v2a/attribution.json: quality.roce` |
| margin_no_r6 (robustness only) | 82 (2019-03 to 2025-12) | 1.07%, 0.23 | 0.31%, 0.07 | 0.24 | 5.74%, 1.21 | 5.18%, 1.13 | `data/backtest/v2a/attribution.json: quality.margin_no_r6` |
| roce_no_r6 (robustness only) | 34 (2023-03 to 2025-12) | 7.30%, 1.33 | 2.31%, 0.41 | 0.90 | 11.27%, 2.61 | 6.97%, 1.53 | `data/backtest/v2a/attribution.json: quality.roce_no_r6` |

Over the whole period (margin version) the quality factor moves v2's alpha from 1.07% (t 0.23) to 0.69% (t 0.15), a loading of 0.12; over the 34 ROCE months, 2023-03 to 2025-12, the quality factor moves v2's alpha from 7.30% (t 1.33) to 0.12% (t 0.02), a loading of 0.76. None of these four alphas has a t of 1.96 or more, the usual 5% level.

## After Indian costs and tax (A6)

| | v2 | v1 | Nifty 500 index fund |
|---|---|---|---|
| Value at the end, sold, after costs and tax, from Rs 500,000 | Rs 1,732,496 | Rs 2,856,676 | Rs 1,271,021 |
| After-tax CAGR | 17.79% | 25.81% | 13.08% |
| After-tax maximum drawdown | -24.80% | -31.67% | -37.24% |
| Tax paid along the way | Rs 244,510 | Rs 457,950 | |

After tax v2 beat the index fund (+4.71 points a year) and trailed v1 by 8.02 points a year. Sources: `data/backtest/v2a/after_tax/summary.json: after_tax_stats.strategy.cagr`, `data/backtest/v1_final/after_tax/summary.json: after_tax_stats.strategy.cagr`, `data/backtest/v2a/after_tax/summary.json: after_tax_stats.benchmark_growth.cagr` and the keys beside them in `v2_verdict.json`.

## Luck: the monkey test

The v2 engine replayed the run to 2.2e-16 of its own NAV, then ran 500 random rankings of each kind through the same rules. With a random but persistent ranking, 81 did at least as well as v2's 21.25% (median 17.05%, v2 at percentile 83.8; v1 was at 96.0); with a fresh random ranking each quarter, 0 of 500 (median 10.61%, percentile 100.0; v1 98.6). The random books' median turnover was 3.62 times a year with a fresh ranking and 0.65 with a persistent one, against v2's 1.99. So much of v2's lead over the fresh kind is their extra trading cost, and the persistent kind is the fairer comparison. Source: `data/backtest/v2a/verify.json: monkey_test`.

## The search: deflated Sharpe

Counting all 197 trials in `state/trials.jsonl` (both v2 runs are lines 196, 197), v2's Sharpe of 1.26 gives a probability of skill between 0.740 and 0.988 depending on the variance used (`data/backtest/v2a/verify.json: deflated_sharpe`); v1's, counted at 195 trials, was between 0.838 and 0.995.

## Both variants and v1, for the record

| Measure | v2 (a) | v2 (b) | v1 |
|---|---|---|---|
| CAGR | 21.25% | 19.93% | 30.68% |
| Equal-weight universe CAGR | 23.82% | 23.82% | 22.03% |
| Maximum drawdown | -26.34% | -26.34% | -33.56% |
| Sortino (quantstats) | 1.744 | 1.689 | 1.936 |
| Calmar (quantstats) | 0.826 | 0.774 | 0.937 |
| IIMA four-factor alpha, a year | 1.07% | 0.80% | 5.74% |
| t of that alpha (HAC, 3 lags) | 0.23 | 0.17 | 1.21 |
| Turnover a year, incl. initial build | 1.986 | 1.949 | 1.958 |
| Capacity: p95 trade / 60-session median turnover | 0.418% | 0.373% | 1.159% |

Each figure's file and key are under `variants` in `v2_verdict.json`. The two v2 variants trade the same names; the equal-weight universe is v2's (rules 1 to 7 and the hard filters), which is why it differs from v1's.

## Files read

| File | sha256 (line endings normalised) |
|---|---|
| `data/backtest/v1_final/after_tax/summary.json` | `c7554ad5e5b0cca7...` |
| `data/backtest/v1_final/attribution.json` | `053f434b8627902f...` |
| `data/backtest/v1_final/baseline_report.json` | `8cc80149afaf1c63...` |
| `data/backtest/v1_final/libcheck.json` | `b7fa7aad23578108...` |
| `data/backtest/v1_final/nav.csv` | `2db03ba74bb45f6d...` |
| `data/backtest/v1_final/verify.json` | `c1380b25d8347cec...` |
| `data/backtest/v2a/a1.json` | `a3a701a551d98e55...` |
| `data/backtest/v2a/after_tax/summary.json` | `298c439038009015...` |
| `data/backtest/v2a/attribution.json` | `26644f13d96d70a4...` |
| `data/backtest/v2a/baseline_report.json` | `67eaa40d5521a042...` |
| `data/backtest/v2a/libcheck.json` | `999f4272a0a9988b...` |
| `data/backtest/v2a/metrics.json` | `d7644a3423de3dbb...` |
| `data/backtest/v2a/nav.csv` | `7ce321e2267b4a91...` |
| `data/backtest/v2a/verify.json` | `149e92271415afd3...` |
| `data/backtest/v2b/a1.json` | `00b586291154fbf8...` |
| `data/backtest/v2b/libcheck.json` | `c3e61ff322a2c5da...` |
| `data/backtest/v2b/metrics.json` | `0cae1b9ff903f681...` |
| `data/backtest/v2b/nav.csv` | `11e8d1c30c10b517...` |
| `data/backtest/v2b/verify.json` | `839dca2e26906d39...` |

