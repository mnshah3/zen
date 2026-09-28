# Measurements of a v2 run: v2b

Run `data/backtest/v2b`, 2019-02-15 to 2026-09-18, measured open to open. Every number below is read from the measurement jobs' own output files, listed with their definitions in `baseline_report.json`.

The strategy grew 19.9% a year, the same universe equally weighted 23.8%.

## The five A2 quantities

The same quantities v1's baseline reports, measured on this run. Whether they clear v1's bars is decided in `data/backtest/v2_verdict.json`.

| Measure | v2b |
|---|---|
| Maximum drawdown | -26.34% |
| Sortino (quantstats) | 1.689 |
| Calmar (quantstats) | 0.774 |
| IIMA four-factor alpha, a year | 0.80% |
| t-statistic of that alpha | 0.17 |
| Turnover a year, incl. initial build | 1.949x |
| Capacity: 95th percentile trade as a share of 60-session median turnover | 0.373% |

The alpha is over 82 months (2019-03 to 2025-12), the last month IIMA has published. The t-statistic is statsmodels' Newey-West figure without a small-sample correction; with it, as the project's hand-written regression does, it is 0.17. Capacity counts all 897 buys and sells, every tranche included, against the stock's median turnover in the 60 sessions before each trade; counting only trades filled on a decision date gives 0.402%, which leaves out the second and third tranches.

## Is the alpha a quality premium?

Adding the A5 quality factor (margin version, 82 months) moves alpha from 0.80% (t 0.17) to 0.63% (t 0.14); the strategy loads 0.05 on quality. Over the ROCE version's 34 months (2023-03 to 2025-12) alpha is 6.17% (t 1.09) without the factor and -0.42% (t -0.07) with it, a loading of 0.70: the factor accounts for 107% of the alpha in that window. 34 months is about 2.8 years.

## Against the indices

Excess is the strategy's annual growth minus the index's. The live part of a factor index starts at the close of its first live date.

| Benchmark | Whole period: excess, IR | Live part | Live: excess, IR |
|---|---|---|---|
| Nifty 500 | +5.6%, 0.38 | not a factor index |  |
| Nifty Midcap 150 | -0.4%, -0.06 | not a factor index |  |
| Nifty Smallcap 250 | -0.2%, -0.07 | not a factor index |  |
| Nifty200 Momentum 30 | +4.3%, 0.21 | from 2020-10-12 (5.9 years) | +6.0%, 0.33 |
| Nifty500 Value 50 | -3.0%, -0.25 | from 2024-12-16 (1.8 years) | -0.2%, -0.07 |
| Nifty200 Quality 30 | +8.0%, 0.54 | whole period | same |
| Nifty100 Low Volatility 30 | +6.6%, 0.46 | whole period | same |
| Nifty Alpha 50 | -3.6%, -0.29 | whole period | same |

NSE's launch dates could not be read from an NSE document (see `launch_date_sources_tried` in attribution.json), so, as fixed in advance, each live part starts at the first date NSE printed an open for the index. Later live starts: Nifty200 Momentum 30 from 2020-10-12, Nifty500 Value 50 from 2024-12-16; the others are live over the whole period. Too short to judge on its own (under 3 years live): Nifty500 Value 50 (1.8 years). With no overnight move where NSE printed no open, the whole-period excess changes by at most 0.03 percentage points.

## After the in-sample end, open of 2023-02-15 to open of 2026-09-18

Not a held-back test for v2: its rules were written after v1's full-period results were known, so it reuses data v1 has already seen (v2-spec, 'How it gets tested, and the honest problem'). The split is reported only because v1's report has it.

Strategy 24.42% a year, equal weight 23.13% (margin 1.29 points, 90% interval -11.02 to 12.59). Indices, open to open: Nifty 500 13.50%, Nifty Midcap 150 21.42%, Nifty Smallcap 250 21.88%, Nifty200 Momentum 30 15.92%, Nifty500 Value 50 27.30%, Nifty200 Quality 30 11.34%, Nifty100 Low Volatility 30 12.75%, Nifty Alpha 50 24.45%.

## After Indian costs and tax

From Rs 500,000, the strategy would have been worth Rs 1,619,392 if sold at the end after costs and tax, 16.7% a year; a Nifty 500 index fund Rs 1,271,021, 13.1% a year. The strategy paid Rs 213,090 in tax along the way.

## Luck

Of 500 random portfolios run through the same rules with a random but persistent ranking, 126 did at least as well (median 17.0% a year); with a fresh random ranking each quarter, 0 of 500 (median 10.1%).

Deflated Sharpe, counting 197 lifetime trials: probability of skill between 0.705 and 0.984 depending on the variance used.

