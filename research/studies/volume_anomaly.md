# Volume anomaly: does unusual volume predict anything?

**Yes -- negatively. Stocks spiking on volume underperform comparable stocks,
and the effect grows with the size of the spike.**

> **Revised 8 September 2026.** The first version of this study used a control
> group drawn on same-day turnover, which was roughly six times more liquid
> than a typical event stock. That flaw was real and it mattered enormously
> elsewhere (see `filing_categories.md`, where a claimed effect shrank
> threefold). Re-run against a properly matched control, the volume conclusion
> is unchanged in direction and similar in magnitude -- but it now rests on a
> comparison that holds size constant.

## Signal versus its own matched control

Each event is compared against a stock trading the same session whose
trailing-60-day median turnover is within 60% of the event stock's.

| Volume threshold | 3m | 6m | 12m | 24m |
|------------------|---:|---:|----:|----:|
| 3x normal | -2.7pp | +0.8pp | -0.3pp | -1.0pp |
| **5x normal** | **-5.2pp** | **-6.1pp** | **-5.2pp** | -2.6pp |
| **10x normal** | **-7.6pp** | **-5.8pp** | **-4.6pp** | **-4.5pp** |
| 5x, liquid only (>5cr) | +0.5pp | -2.2pp | -3.7pp | -1.9pp |
| 5x, price also up | -4.2pp | -2.6pp | -2.0pp | -0.5pp |

Bold entries clear z = 2.5. The 10x variant is negative and significant at
every horizon out to two years.

The pattern is dose-responsive: 3x is noise, 5x is clearly negative, 10x is
worse still. A signal that strengthens monotonically with its own intensity is
harder to dismiss as chance than a single threshold that happens to work.

Restricting to genuinely liquid names (>5cr turnover) removes most of the
effect, which locates it in smaller, thinner stocks -- exactly where a retail
investor is most likely to notice a volume spike and be tempted by it.

## What this means practically

Buying a stock because it suddenly traded ten times its normal volume is worse
than buying a comparable stock at random, by four to eight points of hit rate.
This is one of the most visible things on any screener, and it is an active
negative.

## A disagreement worth recording

An adversarial audit of this project re-measured the same signal and concluded
the gap collapsed to nothing once matched (-0.2pp at 12 months). This re-run
finds -4.6pp. The most likely explanation is that the audit ran before the
event sampler was fixed: the old sampler silently truncated to the EARLIEST
events whenever the count sat between one and two times the cap, concentrating
its sample in 2016-2018, while this run samples evenly across 2016-2026.

Both cannot be right, and the disagreement is recorded rather than resolved by
preferring the more convenient number. The sampler fix is verifiable and the
audit predates it, which is why this version is the one carried forward -- but
anyone rebuilding this should re-derive it rather than trust either.

## What the audit got right, and it was the important part

The flaw it identified -- a control group not comparable to the events -- was
real, and in the filing study it had inflated a headline result threefold.
Finding a genuine methodological defect matters more than whether one
downstream number moved.

## Trials

Eleven for this study across both runs. Logged to `state/trials.jsonl`.
