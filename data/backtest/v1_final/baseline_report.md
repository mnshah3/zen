# Baseline for the v2 comparison: v1

Run `data/backtest/v1_final`, 2019-02-15 to 2026-09-18, measured open to open. Every number below is read from the measurement jobs' own output files, listed with their definitions in `baseline_report.json`.

The strategy grew 30.7% a year, the same universe equally weighted 22.0%.

## What v2 has to beat (A2)

v2 replaces v1 only if all five hold.

| Measure | v1 | v2 must be |
|---|---|---|
| Maximum drawdown | -33.56% | no deeper than -28.56% |
| Sortino (quantstats) | 1.936 | above 1.936 |
| Calmar (quantstats) | 0.937 | above 0.937 |
| IIMA four-factor alpha, a year | 5.74% | at least 5.74% |
| t-statistic of that alpha | 1.21 | at least 1.21 |
| Turnover a year, incl. initial build | 1.958x | at most 1.958x |
| Capacity: 95th percentile trade as a share of 60-session median turnover | 1.159% | at most 1.159% |

The alpha is over 82 months (2019-03 to 2025-12), the last month IIMA has published. v2 must be measured over the same months. The t-statistic is statsmodels' Newey-West figure without a small-sample correction; with it, as the project's hand-written regression does, it is 1.17. Capacity counts all 452 buys and sells against the stock's median turnover in the 60 sessions before each trade; counting only trades filled on a decision date, as earlier reports did, gives 0.976%, but that version would not see v2's second and third tranches.

## Is the alpha a quality premium?

Adding the A5 quality factor (margin version, 82 months) moves alpha from 5.74% (t 1.21) to 5.04% (t 1.11); the strategy loads 0.22 on quality. Over the ROCE version's 34 months (2023-03 to 2025-12) alpha is 11.27% (t 2.61) without the factor and 3.31% (t 0.74) with it, a loading of 0.81: the factor accounts for 71% of the alpha in that window. 34 months is about 2.8 years.

## Against the indices

Excess is the strategy's annual growth minus the index's. The live part of a factor index starts at the close of its first live date.

| Benchmark | Whole period: excess, IR | Live part | Live: excess, IR |
|---|---|---|---|
| Nifty 500 | +16.3%, 0.97 | not a factor index |  |
| Nifty Midcap 150 | +10.3%, 0.66 | not a factor index |  |
| Nifty Smallcap 250 | +10.5%, 0.65 | not a factor index |  |
| Nifty200 Momentum 30 | +15.1%, 0.86 | from 2020-10-12 (5.9 years) | +18.9%, 1.05 |
| Nifty500 Value 50 | +7.7%, 0.38 | from 2024-12-16 (1.8 years) | +1.1%, 0.11 |
| Nifty200 Quality 30 | +18.8%, 1.01 | whole period | same |
| Nifty100 Low Volatility 30 | +17.4%, 0.96 | whole period | same |
| Nifty Alpha 50 | +7.2%, 0.38 | whole period | same |

NSE's launch dates could not be read from an NSE document (see `launch_date_sources_tried` in attribution.json), so, as fixed in advance, each live part starts at the first date NSE printed an open for the index. Later live starts: Nifty200 Momentum 30 from 2020-10-12, Nifty500 Value 50 from 2024-12-16; the others are live over the whole period. Too short to judge on its own (under 3 years live): Nifty500 Value 50 (1.8 years). With no overnight move where NSE printed no open, the whole-period excess changes by at most 0.03 percentage points.

## Held-back part, open of 2023-02-15 to open of 2026-09-18

Strategy 34.31% a year, equal weight 20.96% (margin 13.35 points, 90% interval -2.68 to 29.23). Indices, open to open: Nifty 500 13.50%, Nifty Midcap 150 21.42%, Nifty Smallcap 250 21.88%, Nifty200 Momentum 30 15.92%, Nifty500 Value 50 27.30%, Nifty200 Quality 30 11.34%, Nifty100 Low Volatility 30 12.75%, Nifty Alpha 50 24.45%.

## After Indian costs and tax

From Rs 500,000, the strategy would have been worth Rs 2,856,676 if sold at the end after costs and tax, 25.8% a year; a Nifty 500 index fund Rs 1,271,021, 13.1% a year. The strategy paid Rs 457,950 in tax along the way.

## Luck

Of 500 random portfolios run through the same rules with a random but persistent ranking, 20 did at least as well (median 20.7% a year); with a fresh random ranking each quarter, 7 of 500 (median 19.3%).

Deflated Sharpe, counting 195 lifetime trials: probability of skill between 0.838 and 0.995 depending on the variance used.

