# Volume anomaly: does unusual volume predict anything?

**No. If anything it is slightly negative.**

Tested over 2016-2026, split-adjusted, delisted names retained, measured
against Nifty 500 over identical windows. Random selection of liquid stocks on
the same dates is the comparison; every number below should be read against
that row, not against zero.

## The result, at 12 months

| Signal | Beat benchmark | Median stock return | Multibagger rate |
|--------|---------------:|--------------------:|-----------------:|
| **Random baseline** | **42.6%** | **+4.9%** | **9.1%** |
| Volume 3x normal | 40.4% | +3.2% | 8.8% |
| Volume 5x normal | 40.8% | +0.9% | 10.0% |
| Volume 10x normal | 39.0% | +0.5% | 11.5% |
| Volume 5x + price up | 42.5% | +5.3% | 10.5% |

Stocks spiking on volume beat the benchmark LESS often than stocks picked at
random, and their median return is materially worse. The best variant, volume
plus a same-day price rise, is indistinguishable from random.

The one thing volume raises slightly is the multibagger rate: 11.5% at 10x
against 9.1% random. Read alongside the collapsing median, that describes a
lottery, not an edge -- more extreme winners bought with a much worse typical
outcome. Buying a volume spike is buying excitement.

## What this kills

The Sterlite Technologies case looked like accumulation: 113x normal volume in
June 2025, then a sevenfold move. This study says the volume was not the
signal. Thousands of stocks spiked on volume over eleven years and most went
nowhere. Whatever made Sterlite work, it was not that.

That matters because the volume spike was the most visually compelling part of
the case study, and the easiest thing to build a screen around.

## Two things worth noticing in the baseline itself

Only 42.6% of randomly chosen liquid stocks beat Nifty 500 over twelve months.
The median stock LAGS the index, because the index is dominated by the winners
it holds. Beating it is harder than a coin flip, before any skill is applied.

Between 294 and 392 names in each sample delisted during the measurement
window, with worst outcomes of -95% to -99%. Those are precisely the
observations a survivorship-biased study drops, and dropping them would have
lifted every number in this table.

## Trials

Six hypotheses consumed, logged to `state/trials.jsonl`. The count matters:
test enough variants and one looks excellent by chance. Reporting the best
without the count is the most common way a backtest lies.

## Next

Volume is out as a standalone signal. The remaining components -- filing
category, prior decline, and the combination -- are still untested, and the
announcements archive is being backfilled to 2022 for exactly that purpose.
