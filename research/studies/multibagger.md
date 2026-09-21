# What came before a multibagger?

**Verdict: nothing I measured predicts which stocks will triple. What looked
like a finding came from the wrong base rate, overlapping windows that
overstated the evidence, and reporting only the quintiles that looked good.**

## The question

Take every liquid NSE stock at every month-end from 2015, and ask whether it
at least tripled over the next 24 months. Then check whether anything visible
at the start (momentum, distance from its high, size, volatility, revenue
growth, margins, order and expansion filings) made a triple more likely.

The panel is 79,732 stock-months. 6.4% of them tripled.

[`jobs/study_multibagger.py`](../../jobs/study_multibagger.py) builds the
panel and the quintile hit rates, and all 11 variants are in the trial log
under `multibagger_hit_rate`. The robustness checks in sections 2 to 6 were run
as separate one-off analyses and are not yet committed as code.

## What it first appeared to show

The first pass said low-margin, small, beaten-down companies tripled about
twice as often as the average stock. Low operating margin looked strongest,
with a 1.94x lift and a t-statistic of -7.0.

## Why that was wrong

**1. The wrong base rate.** Margins only exist for companies filing tagged
results from 2018 onward. That subsample triples 8.75% of the time, not 6.4%.
Compared with its own base rate the margin lift falls from 1.94x to 1.42x.
More than half of the "finding" was which stocks had margin data at all.

**2. Overlapping windows.** Consecutive month-ends share 23 of their 24 months
of outcome, so the same price move gets counted up to 24 times. Correcting for
that overlap (Fama-MacBeth regressions, with Newey-West standard errors over
24 lags) cuts the t-statistics by a median factor of 2.76. Margin goes from
-7.02 to -2.50.

**3. It's dispersion, not direction.** Small and low-margin stocks triple more
often, and they also halve more often. Average returns are flat across margin
quintiles. Buying them gets you more variance, not more return.

"Both tails" tests whether a feature predicts a big move in either direction,
tripling or halving. "Up minus down" tests whether it predicts tripling more
than halving, and only that one would help pick stocks. Negative values mean
lower values of the feature go with more of the outcome.

| Feature | Both tails (t) | Up minus down (t) |
|---|---|---|
| Size (turnover) | -11.83 | -1.19 |
| Volatility | 4.43 | -1.12 |
| Margin | -2.71 | -2.09 |

**4. Picked buckets.** Revenue growth is U-shaped, and I had reported only the
bottom quintile. Margin change is flat noise across quintiles. The expansion
filing count has a negative coefficient once all quintiles are used.

**5. Filing counts carry no information.** 98.8% of stock-months have zero
order filings and 99.0% have zero expansion filings, partly because the filings
archive only starts in 2022. NSE only started labelling filing types in
September 2024, after this panel ends. As a placebo I replaced order count with
a meaningless number derived from the ticker, and it did better than order
count 30% of the time.

**6. The multiple-testing bar.** The project has logged 129 trials, 11 of them
in this study. With that many attempts, pure noise would be expected to produce
a best lift of about 1.74x on this panel. The honest margin result, 1.42x, sits
below that. No fundamental feature clears the stricter t > 3.0 bar proposed by
Harvey, Liu and Zhu (2016).

## What survives

Only size and volatility clear a placebo test with shuffled labels, and both
predict how wide the outcomes are rather than which way they go. Neither is a
fundamental signal.

## What this changes

Discovering patterns from scratch burns trials faster than the data can support
them. The next step tests a small number of factors with published evidence
(profitability, value, momentum, low volatility) on this archive, decided in
advance, rather than searching for new ones. Order wins need to be measured by
size relative to revenue, not counted, before they can be tested at all.
