# Strategy v2: proposed rules

**Approved on 2026-09-22 and not yet tested.** Nothing here has been run. The
rules were fixed before any measurement, which is the entire point.

## Why v2 exists

v1 works but three things in it are unsatisfying, and one is a data gap:

1. The ranking's alpha is 9.5% a year at t = 1.96, just under the conventional
   bar. More positions would cut noise and raise the t without needing more
   return.
2. Drawdowns of 33.6% and 30.7%, the second taking 15 months to recover, are
   more than most people can sit through.
3. The quality group used operating margin, **not the rule the owner set,
   which is ROCE**. Balance sheets only begin in late 2022, so the backtest
   could never test what he asked for.
4. 29% of held slots were companies NSE never labelled, so the 3-per-sector cap
   often could not bind.

## What changes

### 1. Twelve positions instead of ten

Ten names is a small sample and much of the year-to-year noise is
single-stock. Twelve keeps the book concentrated enough to matter while cutting
that noise. The hold band widens with it: a holding is kept while it ranks
inside the top 24.

### 2. Quality becomes ROCE, where the data allows

`ROCE = TTM EBIT / (equity + total debt)`, using the latest balance sheet known
before the decision date. The quality group becomes ROCE plus margin stability.

**Honest limit:** balance sheets start in September 2022, so ROCE is available
for decision dates from about February 2023 onward and not before. Before then
the group falls back to operating margin, exactly as v1. Every run reports the
share of rows using each, and any claim about ROCE covers only the later
period.

### 3. Hard filters, the ones the owner set

Added to v1's filters, and applied where the data exists:

| Filter | From |
|---|---|
| ROCE at least 10% | Feb 2023 |
| Debt to equity below 1.5, with equity above zero | Feb 2023 |
| Market cap above Rs 100 crore | throughout |
| Trailing P/E at or below 70, and positive | throughout |
| Promoter holding at least 15% | **live only**, see below |

Promoter holding comes from NSE's shareholding pattern, which starts in 2022
and is fetched per company. It will be a live filter and a reported flag, not a
backtest filter, because applying it to 2019 would mean using data that did not
exist. Promoter pledge above 50% is a veto once the figure can be parsed
reliably from the shareholding XBRL.

### 4. A graded market trend filter

At each rebalance date, and only then, three things are checked about the
Nifty 500 total-return index using data up to the previous session:

| Condition | Meaning |
|---|---|
| Index closed below its 200-day average | the market is weak now |
| Index is 10% or more below its highest close of the last 12 months | the fall is deep |
| Index closed below its 200-day average on at least 60% of the last 126 sessions | the weakness has persisted |

Cash is held according to how many of the three are true:

| Conditions true | Cash |
|---|---|
| 0 | 0% |
| 1 | 20% |
| 2 | 27.5% |
| 3 | 35% |

The cash is set at the rebalance and left alone until the next one, so the rule
can never whipsaw and adds no trading of its own. It is deliberately mild: a
third of the book at worst, because being fully out of the market has cost
investors far more than it has saved them, and the horizon here is years.

The three conditions are fixed here and will not be tuned. Severity is graded
rather than binary because a shallow dip and a grinding bear market are not the
same risk. Evidence: Faber (2007) on moving-average timing and Moskowitz, Ooi
and Pedersen (2012) on time-series momentum, both of which find the benefit is
smaller drawdowns rather than higher returns.

### 5. Staggered entry

A new position is bought in **three equal tranches**, at the decision date and
about 21 and 42 sessions later. Existing holdings being resized trade in full
at the decision date, as now.

This makes no attempt to time anything. It removes the risk of one unlucky
entry day, which is a failure that has cost the owner three times.

### 6. Volatility-based sizing

Position weight is proportional to 1 / (252-day volatility), scaled so the book
is fully invested, and capped between 0.5x and 1.5x of an equal weight so no
single name dominates. A quieter business gets a slightly larger slot.

### 7. Sector labels filled properly

NSE's quote endpoint carries a four-level classification (macro, sector,
industry, basic industry) that our announcements archive lacks. Fetch it once
per symbol for currently listed companies and use the sector level for the cap.
Companies that have since delisted will still be unlabelled, and the run
reports how many, because pretending otherwise would hide a real gap.

### 8. A thesis-break exit

A holding is sold at the next rebalance, whatever its rank, if it fails any
hard filter: ROCE below 10%, D/E above 1.5, a trailing loss, or P/E above 70.
Rank slippage alone still only matters at the edge of the hold band.

## What does not change

The universe filters from v1, quarterly dates, equal treatment of the five
ranking groups, growth, value, momentum and risk definitions, 0.20% costs,
dividends as cash, and the 20-session forced exit.

## How it gets tested, and the honest problem

**v2 reuses data v1 has already seen.** There is no fresh holdout left, so the
evidence is weaker by construction and the write-up must say so. Three
safeguards:

1. **One run.** These rules, as written, measured once. No variants, no grid.
   Adding a grid here would reintroduce exactly the overfitting v1 avoided.
2. **Combinatorial purged cross-validation.** Instead of one path, many
   train-test splits with an embargo, giving a distribution of outcomes rather
   than a single flattering number.
3. **The same adversarial checks as v1**: monkey test, factor attribution
   against the IIMA factors, deflated Sharpe counting every trial in the
   project's history, and the independent re-implementation.

## What would make v2 the strategy to use

Decided now, before any number exists. v2 replaces v1 only if **all** hold:

1. Maximum drawdown improves by at least 5 percentage points.
2. Sortino and Calmar both improve.
3. Alpha against the IIMA factors does not fall, and its t-statistic does not
   drop below v1's 1.96.
4. Turnover does not rise.
5. Capacity stays above Rs 50 lakh.

If the drawdown improves but returns collapse, v2 fails. If returns improve but
drawdowns worsen, v2 fails. Anything else is picking the flattering half of
the result after the fact.

## What is deliberately not in v2

Not because they don't matter, but because a backtest of them would be
invented:

- **The owner's judgement, macro reads and policy views.** No historical record
  of them exists. They belong in the decision journal, where they will build a
  measured track record over the coming year.
- **Management guidance and concall reading.** Transcripts exist only from about
  2021 and reading them at scale needs a model. It goes into the dashboard as
  information, not into the backtest as a signal.
- **Technical entry timing.** Weak published evidence and a large number of ways
  to fool ourselves. Staggered entry gets the benefit without the search.
  *[Superseded on 2026-09-23 by amendment A1 below: one pre-registered entry
  rule, tested once against staggered entry.]*

---

## Sign-off

Approved by the owner on 2026-09-22: twelve positions, the graded trend filter
above (half in cash was rejected as too blunt, in favour of 20 to 35% scaled
by how deep and how persistent the fall is), three-tranche entry, volatility
sizing, the thesis-break exit, promoter holding as a live filter only, and the
acceptance rules above.

These rules are now fixed. Anything learned during implementation goes in a
dated Clarification, and nothing may be chosen by looking at a return.


## Amendments before any v2 measurement (2026-09-23)

Made on 2026-09-23, before any part of v2 had been built or run. Each says why.

### A1. One technical entry rule, tested once against staggered entry

Asked for by the owner: once a stock has passed the filters and been selected,
decide when to buy it from its price action rather than always at the
scheduled open. One rule is fixed here and tested once. It is not tuned.

Applies to each tranche of a NEW position (item 5). A tranche is due on its
scheduled session. From then on it is bought at the next session's open once,
at the previous session's close, both of these hold on corporate-action
adjusted closes:

- the close is no more than 10% above its own 50-session simple average, and
- the 14-session RSI, Wilder's smoothing, is below 70.

If neither day qualifies within 30 sessions (about six weeks) of the scheduled
session, the tranche is bought at the open of the 31st session regardless, so
a selected stock is never skipped entirely. Each tranche waits independently
from its own scheduled session. Cash for a waiting tranche is held uninvested.
If the position leaves the book before a tranche is bought, the tranche is
cancelled. Resizing an existing holding is unaffected and trades at the
decision date.

Why this rule: short-term reversal is among the most replicated patterns in
equity returns (Jegadeesh 1990, Lehmann 1990). Stocks that have just surged
tend to give some of it back over the following weeks, so declining to buy on
a spike has a basis. Its strength in Indian mid and small caps specifically is
not established, which is why it is tested rather than assumed.

**Two runs, decided now.** v2 is run twice, identical except for entry: (a)
staggered entry as item 5, (b) staggered entry with this rule. Both are logged.
(b) is adopted only if all three hold, otherwise (a) is used:

1. the average price paid for new positions, relative to the corporate-action
   adjusted close on the session before each position's decision date, is
   lower in (b) than in (a);
2. annual return is not lower in (b); and
3. maximum drawdown is not worse in (b).

The adopted variant is then judged against the acceptance rules. No other
threshold, lookback or indicator is tried.

### A2. Acceptance rules restated against v1 measured on the same data

The acceptance rules above were written on 2026-09-22 using v1 figures that a
later audit found wrong. Rule 3 names "v1's 1.96", which came from a factor
regression that subtracted the risk-free rate twice (the correct figure was
t = 1.15). Rule 5 named a Rs 50 lakh capacity without defining how it is
measured, and v1's corrected capacity is lower. A bar built on a wrong number
is not a bar, so the rules are restated. Their intent is unchanged: v2 must
beat v1 on risk without giving up return or evidence of skill.

Every comparison is v2 against **v1 re-run on the same final archive with the
same code and the same measurement jobs** (jobs/libcheck_v1.py, jobs/verify_v1.py,
jobs/stats_v1.py), after the parser fix in stage A. v2 replaces v1 only if all
hold:

1. Maximum drawdown over the full period is at least 5 percentage points
   smaller than v1's.
2. Sortino and Calmar, as computed by quantstats, are both higher than v1's.
3. Annual alpha against the IIMA four factors, computed with statsmodels and
   Newey-West errors over the same months, is not lower than v1's, and its
   t-statistic is not lower than v1's.
4. Annual one-way turnover, including the initial build, is not higher than
   v1's.
5. Capacity is not worse: the 95th percentile of trade value as a share of
   the stock's 60-session median turnover, at Rs 5 lakh, is not higher than
   v1's.

### A3. The owner's judgement, macro and policy views: live only, defined later

Confirmed by the owner on 2026-09-23. These are not part of any backtest,
because no historical record of them exists and any rule written now about
which policies mattered would be hindsight. They will be a layer on top of the
live strategy: each quarter the system proposes its book, the owner may veto
names or swap in others from the system's top 30 with a written, dated reason,
and both the system's book and the owner's are tracked so the value of the
judgement is measured going forward. The details are to be specified with the
dashboard, not here.

### A4. Factor-index benchmarks: could the owner just buy the ETF?

Approved by the owner on 2026-09-24. Most of v1's return is explained by the
market and by value and momentum tilts, which an investor can buy cheaply
through index funds. So v2 is also compared, on the same clock, with NSE's own
factor indices, total return: Nifty 200 Momentum 30, Nifty 500 Value 50,
Nifty 200 Quality 30, Nifty 100 Low Volatility 30 and Nifty Alpha 50. These
are reported beside the Nifty 500. They are not added to the acceptance rules,
because v1 was not held to them and v2 is judged against v1. But the write-up
must say plainly whether v2 beat each of them, because a strategy that cannot
beat a factor ETF should not be preferred to one.

### A5. A quality factor built from the archive

IIMA publishes market, size, value and momentum factors for India but no
profitability or quality factor. v2 ranks on ROCE and margin stability, so a
quality premium could otherwise be reported as alpha. A long-short quality
factor is built from the archive, point in time, on the same liquid universe:
operating margin for the whole period, and ROCE from February 2023 when balance
sheets allow, each sorted into top and bottom 30% within big and small halves
by market cap, in the manner of Fama and French. Attribution is reported both
with and without it. The factor's construction is fixed here and not tuned.

### A6. After Indian costs and taxes

The figures that land in an owner's account are after costs and tax. v2, v1
and a buy-and-hold Nifty 500 index fund are also reported after:

- costs itemised for delivery trades (securities transaction tax, stamp duty,
  exchange and SEBI charges, GST, depository charges) plus slippage, checked
  against the 0.20% per side assumed in the backtest; and
- capital gains tax by lot, with the rates and dates in force at the time:
  short-term 15% and long-term 10% above Rs 1 lakh before 23 July 2024, then
  20% and 12.5% above Rs 1.25 lakh; losses set off as the rules allow and
  carried forward; the index fund taxed as held throughout and sold at the end.

Reported, not added to the acceptance rules. A strategy that wins before tax
and loses after it has not won for its owner.

## Clarification to A4, before any v2 measurement (2026-09-24)

Written after the factor-index data was collected and before any v2 or v1
re-run figure exists.

**Where the open comes from.** Clarification 17's clock starts and ends each
benchmark at a market open: TRI_open(d) = TRI_close(d-1) x price_open(d) /
price_close(d-1). NSE prints no open, high or low for a factor index before it
starts calculating the index live, only a back-calculated close. Nifty 200
Momentum 30 has published opens from 12 Oct 2020 and Nifty 500 Value 50 from
16 Dec 2024, so the v1 clock's first open (15 Feb 2019) has none for either,
and Value 50 has none at the in-sample end (15 Feb 2023) either. Where the
factor index has no published open, the overnight move of its parent index
stands in:

    TRI_open(d) = TRI_close(d-1) x parent_open(d) / parent_close(d-1)

with parents Nifty 200 (Momentum 30, Quality 30), Nifty 500 (Value 50,
Alpha 50) and Nifty 100 (Low Volatility 30), from `data/indices`. Tested on
the 17 clock dates where both opens exist: the stand-in's overnight move
differs from the real one by 0.10 percentage points on average and 0.26 at
most, a single time per endpoint. Where a real open exists it is always used.
The comparison is also reported with no overnight move at the affected
endpoints (TRI_open(d) = TRI_close(d-1)); if v2's verdict against an index
differs between the two, the write-up says so and calls it a tie.

**Back-calculated history.** A factor index's figures before it went live are
the provider's back-test of its own rules, not returns anyone could have
earned, and indices tend to be launched after a strong back-test. The write-up
gives each index's first live-published date and reports the comparison both
over the whole period and over the live part only. Where the live part is too
short to judge, it says so rather than leaning on the back-test.

**Data.** `data/external/nifty_tri.parquet` holds the total-return closes for
all eight indices to 23 Sep 2026, built by `jobs/build_nifty_tri.py`, which
refuses any session missing against the Nifty 500 calendar, any conflicting
duplicate and any change to rows already in use. The opens and prior closes for
the five factor indices on 14-15 Feb 2019, 14-15 Feb 2023, 17-18 Sep 2026 and
22-23 Sep 2026 are in `data/external/nifty_price_endpoints.csv`. Another end
date needs those rows fetched first; the engine must refuse, never guess.
