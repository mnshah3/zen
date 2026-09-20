# Do corporate filings predict returns?

> **SECOND CORRECTION, 14 September 2026. The +6.2 point result below is void,
> and this time the data was wrong rather than the control.**
>
> Every number in this file was measured on filings bucketed by a regex over
> free text. That bucket held 14,955 filings. Checked against NSE's own
> classification, roughly a quarter were genuine order wins and **roughly a
> quarter were regulatory penalties** -- companies being fined or sued, which
> move a price the other way. Averaging those together produces a number that
> describes nothing.
>
> The exchange labels every filing itself, in the subject line, with complete
> coverage. I never looked. Using its labels, the real order-win bucket is
> **3,220 filings, not 14,955** -- the regex was four and a half times
> over-inclusive.
>
> It is worse than a contaminated sample. NSE only began applying these labels
> on **2024-09-23**, and it was a single-day change rather than a phase-in, so
> the 2022-23 window this study covers has no reliable order-win data at all.
> The "+6.2 points that did not replicate later" was comparing two periods that
> had almost nothing in common.
>
> Measured properly on the labelled era, order wins do move prices, but not
> like this. They gain **0.85pp on the day** against a matched control and then
> **give back 0.47pp over the next three sessions**, with only 41.4% rising
> against 46.5% of comparable stocks. A short pop followed by profit-taking,
> not a multi-month edge. See `jobs/study_category_signs.py`.
>
> Kept rather than deleted, because how a plausible number survived two rounds
> of correction is more useful than the number ever was.


> **CORRECTION, 8 September 2026.** An earlier version of this file claimed
> order wins beat the benchmark 63.2% of the time against a 43.3% baseline,
> z = +13.4. **That was wrong.** The control group was not comparable: it was
> drawn on same-day turnover and was roughly six times more liquid than a
> typical event stock, so the comparison measured company size, not the signal.
> Against a control matched on both date and normal liquidity, the effect is
> **+6.2 points, z = +3.2** -- real, but a third of the claimed size. Five of
> the seven other categories reported as significant are, correctly measured,
> noise. The error was found by an adversarial audit of this project's own
> measurement code.

**Order wins are the only category that survives a fair comparison.**

Each signal is now compared against its own control: stocks trading on the
same session whose trailing-60-day median turnover is within 60% of the event
stock's. That holds calendar and size constant, so what remains is the signal.

## At 12 and 24 months, signal versus its matched control

| Signal | 12m gap | z | 24m gap | z | Verdict |
|--------|--------:|--:|--------:|--:|---------|
| **Order wins** | **+6.2pp** | +3.20 | **+8.1pp** | +3.11 | **real** |
| Capacity expansion | +3.0pp | +1.59 | +5.9pp | +2.51 | marginal at 24m only |
| Guidance / investor update | +0.0pp | 0.00 | +5.0pp | +2.29 | noise |
| Credit rating action | -0.1pp | -0.05 | +4.3pp | +1.84 | noise |
| Results | +0.3pp | +0.17 | -0.5pp | -0.23 | noise |
| M&A | +0.2pp | +0.11 | -3.5pp | -1.54 | noise |
| Fund raising | +2.4pp | +1.31 | +3.2pp | +1.43 | noise |
| Exchange volume query | -1.2pp | -0.65 | +0.0pp | 0.00 | noise (and -4.6pp at 3m, real) |

## How much confidence this deserves

Less than the numbers alone suggest. Thirty-two comparisons were made -- eight
categories at four horizons. Correcting for that many looks, the threshold for
significance at the 5% level rises to roughly z = 3.16. Order wins at 12 months
reaches z = 3.20. It clears the bar by a hair.

So the honest statement is: order wins are the single filing category with
evidence behind them, the effect is around six to eight points of hit rate, and
it only just survives the multiple-testing correction that seventeen logged
trials demand.

## Why order wins and nothing else

An order win is a contracted future cash flow from a named counterparty. It is
verifiable, it is forward-looking, and it changes expected revenue rather than
describing the past. Results and guidance describe what already happened or
what management hopes will happen; the market has usually priced both. Fund
raising is dilution. M&A is a coin flip on integration.

Capacity expansion sits second and is directionally consistent -- capital
committed because demand is expected -- but it only reaches significance at 24
months, which is what you would expect if the payoff arrives with the capacity.

## Standing caveats

**One regime.** 2022-2023 was an exceptional run for Indian small and mid caps.
The relative comparison controls for the market, but whether order wins work in
a falling market is untested and cannot be tested until the archive extends
back further.

**Clustering.** Events are not independent. Two thousand filings come from
several hundred companies, and market-wide moves hit whole clusters at once.
The z-statistics above treat events as independent and are therefore
optimistic. This is unfixed and is the largest remaining methodological gap.

**No fundamental filter yet.** These are raw filing events with a liquidity
floor. Valuation and leverage have not been applied.

## Trials

Seventeen for this study, six for volume. The count is the reason the
multiple-testing correction above matters.
