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

---

## Sign-off

Approved by the owner on 2026-09-22: twelve positions, the graded trend filter
above (half in cash was rejected as too blunt, in favour of 20 to 35% scaled
by how deep and how persistent the fall is), three-tranche entry, volatility
sizing, the thesis-break exit, promoter holding as a live filter only, and the
acceptance rules above.

These rules are now fixed. Anything learned during implementation goes in a
dated Clarification, and nothing may be chosen by looking at a return.
