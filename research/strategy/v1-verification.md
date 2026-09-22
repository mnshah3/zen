# Is the v1 result skill or luck?

Run on 2026-09-22, after the held-back test, with
[`jobs/verify_v1.py`](../../jobs/verify_v1.py). Full period, Feb 2019 to
Sep 2026: strategy 28.3% a year, the same universe equally weighted 21.8%.

**Short answer: the ranking beats chance, but the evidence is one step above
borderline, and a large part of the return is known factors rather than
anything new.**

## 1. The monkey test

2,000 random ten-stock portfolios, drawn from the same universe on the same
dates and run through the same machinery: same costs, same buffer, same sector
cap, same forced exits. Only the ranking is replaced by a coin toss.

| | Return a year |
|---|---|
| The strategy | 28.3% |
| Best of 2,000 random | 44.3% |
| 95th percentile of random | 28.3% |
| Median random | 19.3% |
| 5th percentile of random | 10.6% |

**The strategy lands at the 95th percentile. 100 of 2,000 random portfolios
beat it.** So the ranking is doing something, and the something is worth about
9 points a year over picking at random from the same filtered list. But one in
twenty monkeys did better, which is the honest size of the evidence.

## 2. Is it just known factors?

Monthly returns regressed on IIM Ahmedabad's published Indian factors, with
Newey-West standard errors:

| | Alpha a year | t | Market | Size | Value | Momentum |
|---|---|---|---|---|---|---|
| Strategy | 9.5% | **1.96** | 0.86 | 0.25 | 0.46 | 0.35 |
| The universe, equal weight | 6.8% | **5.54** | 0.96 | 0.67 | 0.34 | -0.05 |

Two things follow.

**The ranking's alpha is borderline.** t = 1.96 sits just under the
conventional bar of 2, and far under the t > 3 that Harvey, Liu and Zhu (2016)
argue for after many trials. Meaningful value and momentum loadings, both
strongly significant, say a real part of the return is exposure to factors
anyone can buy.

**The filters matter more than the ranking.** Simply holding everything that
passes the filters, equally weighted, earns 6.8% a year of alpha at t = 5.54,
which is far stronger evidence than the ranking's own. Being liquid,
profitable, not a lender and having four quarters of published accounts is
most of the edge here. That is a finding in its own right, and it was not what
I expected.

## 3. Deflated Sharpe

The project has logged 193 trials. With that many attempts the best Sharpe you
would expect from pure luck is 1.01 a year. The strategy's is 1.31.

**Probability of genuine skill: 0.78.** The usual bar is 0.95. So the Sharpe
alone does not clear it.

(`trials.deflated_sharpe` was mis-specified, comparing an annualised Sharpe
with a per-period spread, so it returned roughly zero for anything. The version
in `verify_v1.py` follows Bailey and Lopez de Prado (2014) in per-period units.)

## 4. One good year?

| Year | Strategy | Universe, equal weight |
|---|---|---|
| 2019 | +10.8% | -2.9% |
| 2020 | +26.5% | +31.8% |
| 2021 | +93.8% | +72.4% |
| 2022 | -4.3% | +4.6% |
| 2023 | +73.6% | +51.6% |
| 2024 | +32.6% | +27.3% |
| 2025 | +16.3% | -12.7% |
| 2026 to date | -10.5% | +7.4% |

Ahead in five years of eight. Not one lucky year, but not steady either. 2025
is the standout, when the strategy gained 16% while the same universe lost 13%.
2026 is the reverse and is happening now.

## 5. Could the trades be done?

At the backtest's Rs 5 lakh, a trade is a median of **0.02%** of the stock's
daily turnover. The 95th percentile is 1.3%, and 7 of 458 trades exceed 5%.

At Rs 50 lakh those figures multiply by ten, and the largest trades start to
move the price. **Capacity is roughly Rs 50 lakh to Rs 1 crore** before the
fills in this backtest stop being realistic, which is a limit worth knowing
before believing the numbers at a larger size.

## What this changes

1. **The filters earn their place**, with stronger statistical support than the
   ranking. Keep them, and do not loosen them casually.
2. **The ranking adds value beyond chance, but it is not proven.** Reporting it
   as "beat the index by 15 points" without the monkey test and the factor
   regression would be dishonest.
3. **Part of the return is value and momentum**, which are buyable through
   cheaper means. The genuinely new part is the borderline 9.5% alpha.
4. **The next version should try to lift the alpha's significance**, not the
   headline return: more positions to cut noise, ROCE-based quality which the
   backtest never had, and a market trend filter to cut drawdown. All written
   down before testing, as before.
