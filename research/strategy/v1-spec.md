# Strategy v1: pre-registered specification

Written on 2026-09-21, before any backtest of this design was run. Everything
below is fixed in advance. If the build turns up a genuine ambiguity, the
resolution goes in the Clarifications section at the bottom with the date and
the reason, and it must not be chosen by looking at returns.

## Why this should work

The ranks are factors with published, out-of-sample evidence in many markets:
profitability and quality (Novy-Marx 2013, Asness, Frazzini and Pedersen 2019),
value (Fama and French 1992), momentum (Jegadeesh and Titman 1993) and low
volatility (Frazzini and Pedersen 2014). The question here is not whether they
exist but whether a small, long-only, cost-paying portfolio of Indian stocks
built on them beats simply holding the market. Nothing in this design was
discovered on this archive.

## Dates

**Rebalance decision date D:** the first NSE trading session on or after
15 Feb, 1 Jun, 15 Aug and 15 Nov each year. These fall just after the SEBI
results deadlines (45 days after quarter end, 60 for the March quarter), so
each rank uses a fresh quarter.

**Information set at D:** filings with `broadcast_dt` strictly before D
(00:00), and prices up to and including the close of the previous session.
`period_end` is never used as a time filter.

**Execution:** at the open of D, on corporate-action adjusted prices. If a
stock does not trade on D, the order waits for the next session it trades,
up to 5 sessions, and is otherwise cancelled.

**In-sample:** decision dates from Feb 2019 to Nov 2022 (16 rebalances), with
returns measured up to the open of the Feb 2023 decision date.

**Holdout:** everything after that. Code must refuse to compute any return
after the in-sample end unless an explicit unlock flag is passed, and the
holdout is run once, after the in-sample work is finished and reviewed.

Feb 2019 is the first date with enough history. Tagged results start in
early 2018 and year-on-year growth needs five quarters.

## Universe at D

1. Ordinary equity: ISIN starting `INE`, series `EQ` or `BE`, traded in at
   least one of the last 5 sessions before D.
2. Median daily turnover over the previous 60 sessions of at least Rs 20 lakh.
3. At least 200 trading sessions in the previous 365 calendar days.
4. Not a lender or insurer. Banks, NBFCs and insurers are excluded because
   operating margin and sales yield mean nothing for them and the archive has
   no NIM or NPA data. This is standard in the literature.
5. At least four consecutive quarters known at D, with the latest quarter's
   `period_end` no more than 200 days before D.
6. Trailing twelve-month (TTM) normalised profit above zero and TTM EBITDA above zero.
7. Market capitalisation computable.

For each quarter, consolidated figures are used where the company filed them,
otherwise standalone. The choice is made per company and held consistent
across its quarters at D. Where a quarter was revised, the latest version
known before D is used.

## Measures

Each measure becomes a cross-sectional percentile rank within the universe on
D, where 1 is best. A missing measure scores 0.5.

| Group | Measure | Definition |
|---|---|---|
| Quality | Margin | TTM EBITDA / TTM revenue |
| Quality | Stability | minus the standard deviation of quarterly EBITDA margin over the latest known quarters, up to 8, needing at least 4 |
| Growth | Revenue growth | latest quarter's revenue / same quarter a year earlier, minus 1 |
| Growth | Profit growth | (latest quarter's normalised profit minus the same quarter a year earlier) / TTM revenue |
| Value | Earnings yield | TTM normalised profit / market cap |
| Value | Sales yield | TTM revenue / market cap |
| Momentum | 12-1 month | adjusted close 21 sessions before D, divided by adjusted close 252 sessions before D, minus 1 |
| Risk | Low volatility | minus the standard deviation of daily adjusted log returns over the last 252 sessions |

**Group score** is the mean of its measures. The **composite** is the mean of
the five group scores, with equal weights that are not tuned. Ties go by symbol.

**Market cap** is the raw close of the session before D times the share count.
The share count is `shares_implied` from the latest known quarter, adjusted for
any split or bonus with an ex-date after that filing's `broadcast_dt`.

EBITDA is the archive's `ebitda` column, which is operating EBITDA with other
income stripped. Normalised profit is `profit_normalised`, which excludes
exceptional items.

## Portfolio (the investable configuration)

Built to the owner's own rules: Rs 5 lakh, at most 10 positions, at most
3 per sector.

- **N = 10**, equal-weighted at each rebalance.
- **Sector cap of 3**, using the company's NSE industry classification.
- **Buffer:** a holding is kept while it stays in the universe and ranks within
  the top 2N. Vacancies are filled with the best-ranked stocks not already held,
  respecting the sector cap.
- **Costs:** 0.20% of traded value per side. Sensitivity run at 0.40%.
- **Dividends:** cash dividends are credited on the ex-date and held as cash at
  zero interest until the next rebalance.
- **Stocks that stop trading:** a holding with no trade for 20 consecutive
  sessions is sold at its last close. A harsher sensitivity sells it at half
  its last close.

## Diagnostics (not investable, measuring whether the ranks carry signal)

- Composite quintile portfolios across the whole universe, equal-weighted,
  rebalanced on the same dates with no buffer and no costs. The top-minus-bottom
  quintile spread is the main measure of the signal.
- The same quintile spread for each of the five groups on its own.
- Leave-one-group-out composites (five of them).

Every diagnostic is logged as a trial.

## Benchmarks

1. **Universe equal-weight:** every stock in the universe at D, equal-weighted,
   rebalanced on the same dates, with the same costs. This asks whether picking
   beat not picking.
2. **Nifty 500**, with dividends if a total-return series or a dividend yield
   can be obtained, and stated as price-only if not.
3. **Nifty Midcap 150 and Smallcap 250** as context, to separate stock selection
   from a size effect.

## The grid (in-sample only)

Only portfolio construction varies. Nothing in the grid changes which stocks
rank highest.

| Parameter | Values |
|---|---|
| Positions N | 10, 15, 25 |
| Buffer | none (N), 2N, 3N |
| Rebalance | quarterly, half-yearly (Jun and Nov dates) |
| Sector cap | 3 per sector, none |

That's 36 variants, and every one is written to `state/trials.jsonl`.

**Selection rule:** for each variant, take the median in-sample information
ratio against the universe equal-weight benchmark across the variant itself and
every variant that differs from it in exactly one parameter. Choose the variant
with the highest neighbourhood median. This picks a plateau, not a spike. The
deflated Sharpe ratio is reported using the project's lifetime trial count.

## What counts as passing

Measured once on the holdout, for the selected variant, after costs:

1. Annualised return above the universe equal-weight benchmark, reported with
   a block-bootstrap 90% confidence interval.
2. Annualised return above the Nifty 500.
3. The composite top-minus-bottom quintile spread is positive.

All three are reported whatever they show. The limits are stated up front.
Four years in-sample and about three and a half out means wide intervals. This
test can show the strategy clearly works, clearly fails or is uncertain. It
cannot establish a small edge.

## Reported metrics

CAGR, volatility, Sharpe ratio, information ratio and tracking error against
each benchmark, maximum drawdown, returns by calendar year, turnover, average
holding period, and the number of holdings that doubled while held.

## Clarifications

_None yet._
