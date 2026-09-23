# Is the v1 result skill or luck?

Rewritten on 2026-09-23 after a second independent audit. The first version of
this page, published the day before, carried a factor regression that
subtracted the risk-free rate twice and several smaller errors. It is kept in
the git history. What follows is recomputed on the corrected archive, with
every statistic cross-checked against a standard library.

**Short answer: the strategy beat the market and beat random picks from the
same list by a margin that is unlikely to be luck, with smaller drawdowns. But
most of its return is exposure to the market and to value and momentum, and
its alpha after those factors is not statistically significant.**

## Two results, and why there are two

**The one-shot result.** On 22 September the selected configuration was run
once on the held-back period, 15 February 2023 to 18 September 2026. That run
is the pre-registered test and it stays on record as it was run: **29.3% a
year against 20.5% for the same universe equally weighted**, measured open to
open.

**The corrected result.** The audit then found that the March 2025 quarter was
missing for about 650 companies, because the code that lists NSE's filings
stopped at the first page that failed to load and returned the rest as if it
were complete. The universe lost a third of its names at three rebalances. The
listing was fixed and the quarter refilled.

The same pass collected about 2,200 revised filings for the quarters from June
2025 on. They are later versions of filings already held, which the regular
updater had never fetched, and the engine uses each only after the date it was
published. A second independent review rebuilt the database with the March
refill alone and got about 33.8% for the strategy, so the revisions account
for roughly 0.8 points of the change, through one different pick in August
2026. The published figure includes both. No rule was changed:

| Held-back period, open 15 Feb 2023 to open 18 Sep 2026 | Return a year | Worst fall |
|---|---|---|
| **The strategy** | **33.0%** | **-23.8%** |
| The same universe, equally weighted | 21.0% | -31.1% |
| Nifty 500, dividends included | 13.4% | -18.6% |
| Nifty Midcap 150 | 21.3% | -20.9% |
| Nifty Smallcap 250 | 21.8% | -26.0% |

The margin over the equal-weight universe is 12.0 points a year in compound
growth. A stationary bootstrap of the paired daily returns, compounding each
resampled path on NSE's actual 246 sessions a year, gives a 90% interval of
**-3.9 to +27.4 points**, and about 1 resample in 9 puts the margin at zero or
below. Index returns in this table run from the close before each open.

**Both engines agree.** The production engine and the independent
re-implementation, which was extended to 2026 without sight of the production
code, pick the same stocks on all 31 rebalance dates of the corrected run and
match the daily portfolio value to fifteen decimal places. A separate rebuild
from raw exchange prices matched every one of the 226 trades in the original
held-back run.

The rest of this page uses the corrected run over the full period, February
2019 to September 2026: **30.1% a year against 22.1%** for the same universe
equally weighted.

## 1. Against random portfolios

Random ten-stock portfolios drawn from the same universe on the same dates,
run through the same machinery with the same buffer, sector cap, costs and
forced exits. Only the ranking is replaced by chance.

The first version of this test drew a fresh random order every quarter. Such
portfolios churn far more than the strategy and pay far more in costs, which
flatters the strategy. So there are now two versions, one trading more than
the strategy and one trading less:

| Random portfolios, 500 of each | Their turnover | Median return | Strategy's percentile |
|---|---|---|---|
| A fresh random order every quarter | 3.95x a year | 19.3% | **96th**, beaten by 20 |
| One random score per stock for the whole run | 0.49x a year | 20.5% | **94th**, beaten by 28 |

The strategy's turnover is 1.98x a year. Whether chance trades more or less
than it does, the strategy lands around the 95th percentile. So the ranking
adds something over picking at random from the same filtered list, and about
one random portfolio in twenty does as well.

## 2. How much of it is known factors?

Monthly returns regressed on IIM Ahmedabad's published Indian factors
(Agarwalla, Jacob and Varma), with Newey-West standard errors, computed with
statsmodels. IIMA's data ends in December 2025, so this covers March 2019 to
December 2025 and leaves out 2026.

| | Alpha a year | t | Market | Size | Value | Momentum |
|---|---|---|---|---|---|---|
| Strategy | 5.4% | **1.15** | 0.87 | 0.23 | 0.47 | 0.34 |
| The universe, equal weight | 1.6% | **1.40** | 0.96 | 0.67 | 0.34 | -0.05 |

**Neither alpha is statistically significant.** The value and momentum
loadings are, strongly (t = 4.6 and 3.5). So the honest reading is that the
strategy's return is mostly the market, plus deliberate tilts towards cheap
stocks and stocks already rising. Those tilts are the published factors the
design was built on, which is the point of the design, but they are also
available more cheaply than by running this strategy. What is left over after
them is positive and cannot yet be told apart from noise.

## 3. Correcting for the search

The project has logged 195 trials. The deflated Sharpe ratio (Bailey and Lopez
de Prado 2014) asks how likely the observed Sharpe of 1.37 is to reflect real
skill given how many things were tried. The answer depends on how the spread of
Sharpe ratios across trials is estimated, so all three reasonable versions are
reported:

| Method | Probability of skill |
|---|---|
| Sampling variance under the null, all 195 trials | 0.82 |
| Counting only the 36 portfolio variants in the grid | 0.94 |
| Observed spread of Sharpe across the 36 variants | 0.995 |

The usual bar is 0.95. One method clears it, one nearly does and one does not.
That spread is the honest answer.

## 4. Year by year

Each year measured from the previous year's final value.

| Year | Strategy | Universe, equal weight |
|---|---|---|
| 2019 (from 15 Feb) | +10.8% | -2.9% |
| 2020 | +28.3% | +32.8% |
| 2021 | +95.4% | +73.9% |
| 2022 | -2.5% | +6.2% |
| 2023 | +74.8% | +53.2% |
| 2024 | +33.9% | +28.7% |
| 2025 | +21.8% | -11.2% |
| 2026 to 18 Sep | -4.8% | +8.8% |

Ahead in five years of eight. Over any rolling twelve months it beat the
equal-weight universe 71% of the time and the Nifty 500 84% of the time. The
worst twelve months against the universe were 26.6 points behind it.

## 5. Risk

Computed by the project and checked against quantstats.

| | Strategy | Universe, equal weight | Nifty 500 TRI |
|---|---|---|---|
| Sharpe | 1.37 | 1.11 | 0.89 |
| Sortino | 1.91 | 1.47 | 1.20 |
| Calmar | 0.90 | 0.46 | 0.38 |
| Worst fall | -33.6% | -47.9% | -38.1% |

Against the Nifty 500 total return, the strategy captured **129% of its rising
months and 68% of its falling ones**, with a beta of 0.89. The equal-weight
universe captured 131% and 109%. That asymmetry is the most useful property
the strategy showed.

The drawdowns a holder would have lived through:

| Started | Bottom | Recovered | Depth | Length |
|---|---|---|---|---|
| Jan 2020 | Mar 2020 | Nov 2020 | -33.6% | 9 months |
| Apr 2022 | Jun 2022 | Jul 2023 | -30.7% | 15 months |
| Sep 2024 | Feb 2025 | Sep 2025 | -23.8% | 12 months |
| Feb 2026 | Mar 2026 | not yet | -16.5% | 7 months so far |

## 6. Positions

144 closed positions, 61% made money (price only, before dividends). The median
was +7.2%, winners averaged +30.6% and losers -12.9%. The five best made 20% of
the gross profit, so the result is not carried by one or two names.

## 7. Could the trades be done?

At Rs 5 lakh a trade is a median of 0.017% of the stock's daily turnover. The
95th percentile is 1.1%, and 6 of 454 trades exceed 5%. Those percentages
scale with capital. At Rs 50 lakh the 95th percentile would be about 11% of a
day's turnover, which is already enough to move the price, so the fills in
this backtest are realistic up to roughly Rs 25 to 50 lakh and optimistic
beyond it.

## 8. Checked and ruled out

**Later sector labels.** Labels come from announcements that start in 2022,
and the rules let a company borrow its first-ever label before that. Re-run
with labels restricted to those published before each date, the held-back
period's universe is identical, row for row, and not one sector label in it
came from later data. The only difference is a single holding for one quarter
in February 2023 (M&M against GHCL), inherited from 2019 to 2021, and the
held-back return is unchanged.

## Known limits that remain

- **June 2022 is missing for 214 companies at NSE's source**, and June 2018 and
  2019 are similarly short. The listing and per-company queries both come back
  without them, so it is not a fetch fault. The audit's rough estimate is that
  it understates the strategy slightly rather than overstating it.
- **Stocks moved to NSE's trade-for-trade segment are still force-sold** by the
  20-session rule. It never touched the strategy, whose only forced exit was a
  merger, but it may slightly affect the equal-weight benchmark. Fixed in both
  engines as part of v2.
- **Clarifications 22 to 27 were applied after in-sample returns had been
  seen**, which the specification now discloses. The held-back test ran
  afterwards on the fixed code.

## What this changes for v2

1. The filters are necessary but, on their own, not a source of alpha. The
   earlier claim that they were is withdrawn.
2. The ranking beats chance. Whether it beats the factors it is built from is
   unresolved, and that is the question v2 should try to answer, not a higher
   headline return.
3. The asymmetry, 129% up capture against 68% down, is worth protecting. The
   v2 trend filter and volatility sizing are aimed at it.
