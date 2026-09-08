# Do corporate filings predict returns?

**Yes, and the effect is concentrated almost entirely in ORDER WINS.**

Tested on 2022-2023 filings with complete outcomes through 2025, split-adjusted,
delisted names retained, measured against Nifty 500 over identical windows.
Random selection of liquid stocks on comparable dates is the comparison.

## At 12 months

| Signal | Beat benchmark | Median return | Multibagger | Hit +15pp |
|--------|---------------:|--------------:|------------:|----------:|
| **Random baseline** | **43.3%** | **+6.1%** | **7.9%** | **30.9%** |
| **Order wins** | **63.2%** | **+36.6%** | **21.1%** | **51.3%** |
| Capacity expansion | 53.4% | +24.9% | 14.9% | 41.0% |
| Results | 52.0% | +26.2% | 13.2% | 40.0% |
| Credit rating action | 51.4% | +23.6% | 12.4% | 37.8% |
| Guidance / investor update | 50.9% | +23.7% | 11.5% | 38.0% |
| M&A | 48.3% | +18.4% | 11.1% | 33.7% |
| Fund raising | 46.2% | +21.1% | 9.1% | 33.0% |
| Exchange volume query | 47.0% | +18.3% | 13.5% | 36.4% |

Order wins beat the benchmark twenty points more often than random selection,
with a median return six times higher and a multibagger rate 2.7x. The effect
holds across every horizon and strengthens with time: 59.6% beat rate at 24
months with a median of +67.3% and a 39.7% multibagger rate.

Statistically the gap is not ambiguous -- z = +13.4 against the baseline on
n = 2,379. Capital raising and exchange volume queries do not clear noise.

## Why this is plausible rather than a fluke

An order win is a contracted future cash flow from a named counterparty. It is
the least ambiguous good news a company can file, it is verifiable, and it
changes forward revenue rather than describing the past. Compare that with a
fund raising, which is dilution wearing good-news clothes and which the data
scores at essentially nothing.

Capacity expansion sits second, which fits the same logic one step earlier:
the company is committing capital because it expects demand.

## What this does NOT establish

**The period is one regime.** 2022-2023 was an exceptional run for Indian small
and mid caps -- the RANDOM baseline itself returned +22.1% median over 24
months. Every category looks positive in absolute terms because the market was.
The comparison is relative and the relative gap is real, but whether order wins
work in a flat or falling market is untested and cannot be tested until the
archive extends further back. This is the single largest caveat.

**Order-filing companies delist less.** 90 delisted in the orders sample against
134 in the random baseline. Some of the edge is simply that companies winning
contracts are healthier. That is a real effect and part of why the signal works,
but it means the signal partly measures survival rather than upside.

**No fundamental filter has been applied yet.** These are raw filing events with
only a liquidity floor. The screen still has to add valuation and leverage.

**Nothing here is a strategy.** It is one component measured in isolation, which
is what was asked for. Combining components multiplies the search, and the
trial count is already at nine for this study.

## Contrast with the volume study

Volume spikes beat the benchmark LESS often than random. Order filings beat it
twenty points more often. Both were part of the Sterlite case study, and the
data says only one of them carried information. The visually compelling half
was the worthless half.

## Trials

Nine hypotheses for this study; six previously for volume. Logged to
`state/trials.jsonl`. Fifteen and counting -- the count matters, because the
best of many looks better than it is.
