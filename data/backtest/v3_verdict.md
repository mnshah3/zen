# Strategy v3: the verdict

Window: the open of 2020-02-17 to the open of 2026-09-18. v1 is its replay on the same frozen inputs, over the same window. Every rule and every test below was fixed in `research/strategy/v3-spec.md` before v3's results existed.

## 1. The primary test

Alpha after IIMA's four factors and the quality factor, 70 months (2020-03 to 2025-12): **-2.49% a year, t = -0.49**. H0 (alpha ≤ 0) is **not rejected** at the one-sided 5% level (t above 1.645).

70 months can only detect a large alpha: about 8.2% a year half the time, or 12.6% a year 80% of the time, at v1's residual volatility (section 14.1). Failing to reject H0 is not evidence that v3 has no edge; it means the sample is too short to tell.

## 2. Against v1 on the five A2 bars

**v3 does not beat v1 on A2: 1 of 5 bars passed.**

| Bar | v1 | v3 | Passed |
|---|---|---|---|
| 1. Maximum drawdown, 5 points smaller | -32.89% | -44.51% | no |
| 2. Sortino | 1.92 | 1.31 | no |
| 2. Calmar | 1.00 | 0.55 | no |
| 3. Four-factor alpha a year | +3.69% | -0.59% | no |
| 3. Its t-statistic | 0.80 | -0.10 | no |
| 4. Turnover a year | 1.97x | 0.52x | yes |
| 5. Capacity, 95th percentile | 1.089% | 2.032% | no |

CAGR over the window: v1 32.10%, v3 24.10%.

v1 reproduction check: the replay is not identical to `v1_final`; v1's side above is the replay.

## 3. Nifty 500 plus 15 points

| | CAGR | Nifty 500 TRI | Excess | Met |
|---|---|---|---|---|
| v3 | 24.10% | 14.50% | +9.61% | no |
| v1 | 32.10% | 14.50% | +17.60% | yes |

Rolling windows (return minus the Nifty 500's, percentage points, not annualised):

| Months | Windows | v3 median | v3 10th | v3 90th | v3 beat Nifty 500 | v3 beat v1 |
|---|---|---|---|---|---|---|
| 3 | 76 | 2.28 | -9.85 | 16.61 | 57% | 43% |
| 6 | 73 | 2.42 | -11.43 | 32.73 | 56% | 37% |
| 12 | 67 | 5.63 | -11.53 | 42.91 | 58% | 22% |
| 24 | 55 | 16.41 | -12.30 | 51.01 | 75% | 11% |

v3's margin over v1: -7.99 points a year (90% interval -23.28 to +8.93). Over its own eligible list held equal-weight: -2.75 points (-11.92 to +8.12). The eligible list equal-weight grew 26.85% a year; v1's universe equal-weight 25.47%.

Doubled while held: v3 25 of 81 holding episodes, v1 7 of 128.

## 4. Does the score carry signal?

Rank IC of the score against the next review period's return, 26 reviews: mean 0.0524, t = 3.05, positive at 73% of reviews.
Top quintile minus bottom quintile: +22.03% a year (information ratio 1.97).

## 5. Earnings growth and re-rating

| | Earnings a year | Re-rating a year | Share from earnings | Remainder |
|---|---|---|---|---|
| v3 | -9.62% | +32.84% | -55% | 1.239x |
| v1 | +45.72% | -9.43% | 136% | 1.004x |

## v3's own figures

- Cash after the planned trades: mean 0.99%, maximum 9.21% of NAV.
- Exits by code: {'E1': 5, 'E2': 7, 'E3': 1, 'E4': 47, 'E5': 1, 'V': 2}.
- Trims: 0 valuation, 12 cap.
- Slots left empty: 16 ({'half_slot_minimum': 12}).
- Holdings with a test left untested: 44 holding-reviews, 43 of them with a quarter missing from the archive.
