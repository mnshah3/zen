# Zen: automated research infrastructure for Indian equities

[![tests](https://github.com/mnshah3/zen/actions/workflows/tests.yml/badge.svg)](https://github.com/mnshah3/zen/actions/workflows/tests.yml)

I built this to answer one question properly: if I screen Indian stocks on
fundamentals, does it actually work? Most retail backtests can't answer that
honestly, because they test against the companies that are still listed today.
So the first job was building an archive that doesn't cheat, and a test harness
that tries to break its own results.

<!-- numbers:headline:start -->
The answer, on 3.6 years the design had never seen: **34.3% a year against
13.5% for the Nifty 500 including dividends**, and 21.0% for the same universe
equally weighted. That is the held-back test as re-run on 25-26 Sep 2026 on a
repaired archive, with the rules for picking stocks unchanged; the first run
stays on record [below](#the-result). Over the whole 7.6 years from Feb 2019
it made 30.7% a year, and its worst fall was -33.6% against -38.1% for the
Nifty 500 and -47.9% for the same universe.
<!-- numbers:headline:end -->

The rules were public before the test. It beats random picks from the same
list, but most of the return is the market and two well-known factors. [What
that does and does not prove](#the-result) is below.

It runs unattended on GitHub Actions and emails me two briefs. It places no
orders. It produces evidence, and I make the decisions.

<!-- numbers:archive:start -->
```
prices        5,360,616 rows · 2,895 sessions · 4,066 ticker symbols · Jan 2015 to Sep 2026
filings         783,510 announcements · 2,539 symbols · Jan 2022 to Sep 2026
fundamentals    105,818 quarterly results filings · 2,642 symbols · quarters ending Mar 2018 to Jun 2026
indices              23 NSE index series · Jan 2015 to Sep 2026
```

Counted up to 23 Sep 2026.
<!-- numbers:archive:end -->

Most of the backtest and archive figures in this README are written by
[`jobs/readme_numbers.py`](jobs/readme_numbers.py) from the committed results
and the database, and
[`tests/test_readme_numbers.py`](tests/test_readme_numbers.py) fails if the
README drifts from them. The few written by hand name the file or
clarification they come from.

### Where to look first

| If you want | Go to |
|---|---|
| What the emails look like | [sample brief](https://htmlpreview.github.io/?https://github.com/mnshah3/zen/blob/main/docs/sample-brief.html) |
| **The result, and what it does not prove** | [The result](#the-result) below |
| The headline and acceptance figures, each with the file and key it comes from | [`data/backtest/v1_final/baseline_report.md`](data/backtest/v1_final/baseline_report.md) and its `.json`; the rest are in `stats.json`, `verify.json`, `libcheck.json` and `nav.csv` beside it |
| The rules, fixed before the test | [`research/strategy/v1-spec.md`](research/strategy/v1-spec.md) |
| Every hypothesis I've formally tested, including the failures | [`state/trials.jsonl`](state/trials.jsonl) |
| The look-ahead detector | [`zen/validation/leak.py`](zen/validation/leak.py) |
| What I got wrong | [Mistakes](#mistakes) below |

---

## The morning brief

**[Open a real one](https://htmlpreview.github.io/?https://github.com/mnshah3/zen/blob/main/docs/sample-brief.html)** · sent every weekday at 07:00 IST

It isn't a news digest. It reads the archive first, works out what the session
actually did, then goes looking for stories that explain it.

**The data comes first, not the headlines.** Breadth, the gap between the
heavyweights and the median stock, 52-week extremes, size rotation, and every
stock that traded far above its own 60-day volume. Whatever the archive flags
is what decides which stories get promoted. A filing that explains an unusual
move beats a better-sourced story about something that didn't happen.

**It's ranked for one reader.** Trust, corroboration and recency will give you a
decent front page, but two people with opposite portfolios would get the same
email. So stories are also scored against my own themes: AI and data centres,
nuclear, solar, water, infrastructure, defence, import substitution. Each one
carries a badge saying which theme matched. Theme affinity gets added to the
score rather than multiplied, because a brief that only shows me what I already
believe is worse than a generic one. I still need to know the market fell.

**Keyword matching is the weak part, so it's bounded.** Each theme separates
terms that count on their own from terms that need backing up. One "data centre"
is enough. One "power" isn't. Where the news API supplies entity tags the
guessing stops, and the story carries real NSE tickers with sentiment.

**The charts are HTML, not images.** Gmail, Outlook and Apple Mail all block
images by default for a sender you haven't whitelisted, so a brief built on
attached PNGs is a wall of text for a lot of opens. The advance/decline bar, the
30-session breadth chart, the divergence gauges and the rotation table are all
built from table cells and background colours. They render everywhere, with
images off. Every bar carries its number as well, because colour on its own is
unreadable for about one man in twelve.

**Nothing breaks the send.** No model key means extracted facts instead of
prose. A dead feed means fewer stories. A broken chart means no chart. The email
goes out.

### The second email

**[A calibration run](https://htmlpreview.github.io/?https://github.com/mnshah3/zen/blob/main/docs/sample-calibration.html)**

Silent by default. It only sends when something fires, because an alert that
arrives every day stops being an alert.

Strategies carry a `validated` flag. It defaults to false and only becomes true
after a walk-forward backtest that survives the look-ahead detector and reports
its trial count. While anything unvalidated is in the mix, the subject line says
CALIBRATION, the sections read "ranked highest by the test screen" instead of
"to buy", and a banner sits at the top telling me not to trade on it.

That's there because an earlier version headed a calibration run "To buy (15)"
and I read it exactly as it was written.

---

## Why survivorship bias is the whole problem

Most retail backtests in India run against whatever tickers are listed **today**.
Companies that delisted, collapsed or got absorbed are just missing from the
sample. So the strategy only ever gets measured against the survivors, and every
result comes out better than it was.

<!-- numbers:survivorship:start -->
It isn't a small effect. Counting ordinary equity only (ISIN security type 01,
so no ETFs or rights entitlements), this archive holds **3,325 ticker
symbols**. Linked across renames and ISIN changes by
[`zen/universe/identity.py`](zen/universe/identity.py), they belong to **3,050
companies**, and 2,560 of those traded in NSE's main equity series in Sep
2026, up to the 23rd. That leaves **490 companies** that were tradeable at
some point since 1 Jan 2015 and are no longer quoted there. Of those, 35 still
trade in NSE's trade-for-trade segment; the other **455** traded in no NSE
series at all that month.
<!-- numbers:survivorship:end -->

The universe on each date is whatever actually traded that day:

<!-- numbers:survivorship_months:start -->
| Month | Ordinary equity symbols trading |
|------|---------------------------|
| Mar 2015 | 1,476 |
| Jun 2023 | 1,875 |
| Sep 2026, to the 23rd | 2,563 |
<!-- numbers:survivorship_months:end -->

It's built from NSE daily bhavcopy files, which record every security that
traded that day. The universe on any past date is what actually existed then,
not what made it to now.

---

## Decisions I'd defend

**The database is derived, not stored.** Git tracks one Parquet file a month and
DuckDB gets rebuilt from them in seconds. Committing a growing binary every day
would have bloated the repo inside a month. Monthly partitions instead of daily
cut the file count from about 2,500 to 120 per decade, and they compress
much better.

**Signals have to argue against themselves.** The `Signal` class won't construct
without a stated counter-argument. If I can't name how a position fails, I
haven't thought it through, so the constructor enforces that instead of relying
on my discipline.

**Rules decide, models explain.** Story selection, deduplication and ranking are
deterministic and I can inspect all of it. The language model only rewrites the
chosen stories in plain English. If it's unavailable the brief still goes out.

**Claims stay inside their evidence.** The brief says "volume spikes our news
sources do not account for". It does not say "no news exists". The feeds carry
market news, not company filings, and the wording says exactly what was checked.

---

## How I test it

The main test is hard to fool:

> Run the strategy as of date **T** against the full archive.
> Run it again against an archive **physically truncated** at T.
> Both answers have to be identical.

Any look-ahead changes the answer. A mis-shifted column, a normalisation
computed over the whole sample, a join that quietly pulls tomorrow's row. They
all fail the same test and I don't have to anticipate them one by one.

`tests/test_leak.py` holds a strategy that ranks on forward returns, and **it has
to fail the detector**. A detector that never fires proves nothing, so the suite
asserts the failure. If that test ever passes, the harness is broken and
everything downstream of it is worthless.

This exists because an earlier version of this work reported a 30% CAGR that
turned out to be a one-day look-ahead bug. I built the harness before the
strategy this time.

---

## The result

**A fundamental screen, committed to this repo before it was tested, beat the
market on data it had never seen, and beat random picks from the same list.
Most of its return is the market and two well-known factors, and what is left
after them is not yet statistically significant.**

The rules were written into
[research/strategy/v1-spec.md](research/strategy/v1-spec.md) and committed on
21 September 2026. Everything from 15 February 2023 onward was locked in code,
and on 22 September the configuration chosen in advance was run on it. The
first attempt that day had a bug: it held the November 2022 portfolio frozen
for four years instead of rebalancing. That attempt was discarded, the bug
fixed and the test run again the same day with no rule changed (commit
6223419; v1 disclosure 40). That run stays on record as the pre-registered
result.

Two faults in the archive came to light after it, both in the data rather than
the rules. The code listing NSE's filings stopped at the first page that
failed to load, so the archive held the March 2025 quarter for only 1,516
companies. It now holds 2,166, and the same pass fetched about 2,200 later
documents, almost all revisions the regular updater had never collected
(Clarification 36); the run was repeated on 23 September. And some filings tag
their income statement only for a half-year or a year, which the parser stored
as the quarter (Clarification 38, 24 September). A figure now counts as a
quarter only if its period is at most 100 days long, and Clarification 39
settles how a quarter with no such figure is read, a case the two engines had
read differently; the strategy's holdings are the same under both readings.
The whole run, the held-back years included, was repeated on 25 and 26
September 2026 with no rule for choosing or weighting stocks changed. One exit detail was
completed at the same time: a session on which a held stock trades only in
NSE's trade-for-trade segment now counts as a trading day for the 20-session
exit, as the 22 September fixes had intended but never delivered to the engine
(v1 disclosure 34; v2-spec, Clarification to A2).

<!-- numbers:history:start -->
| Run | What changed | Held back, a year | Same universe, equal weight, held back | Whole period, a year |
|---|---|---|---|---|
| 22 Sep 2026 | the one-shot test, as run | 29.43%\* | 20.37%\* | 28.35% |
| 23 Sep 2026 | March 2025 quarter refilled, revised filings added (Clarification 36) | 32.97% | 21.03% | 30.06% |
| 25-26 Sep 2026 | only quarter-length figures read as a quarter (Clarifications 38 and 39); the current result | **34.31%** | 20.96% | **30.68%** |

\* From the close of 15 Feb 2023, the clock used at the time. The later runs
start at the open, as Clarification 16 requires (Clarification 37).

At the 17 Nov 2025 rebalance the one-shot run bought five stocks, and three of
them, CESC, FMGOETZE and IMPAL, were bought while the archive held their
April-to-September figures as the September quarter; the 23 Sep run still
bought IMPAL that way. The current run holds ASHOKLEY in place of IMPAL for
that quarter and picks the same stocks as the 23 Sep run at every other
rebalance. Counting trade-for-trade sessions as trading days changed five
holdings of the equal-weight universe and none of the strategy's
([`trade_for_trade.csv`](data/backtest/v1_final/trade_for_trade.csv)).
<!-- numbers:history:end -->

The current run, on the repaired archive:

<!-- numbers:results:start -->
Open of 15 Feb 2019 to open of 18 Sep 2026, 7.6 years, of which the last 3.6,
from the open of 15 Feb 2023, were held back:

| | Whole period, a year | Worst fall | Held back, a year | Worst fall |
|---|---|---|---|---|
| **The strategy** | **30.68%** | **-33.6%** | **34.31%** | **-23.8%** |
| The same universe, equally weighted | 22.03% | -47.9% | 20.96% | -31.3% |
| Nifty 500 | 14.36% | -38.1% | 13.50% | -18.6% |
| Nifty Midcap 150 | 20.38% | -38.5% | 21.42% | -20.9% |
| Nifty Smallcap 250 | 20.14% | -45.5% | 21.88% | -26.0% |
| Nifty200 Momentum 30 † | 15.62% | -34.0% | 15.92% | -31.6% |
| Nifty500 Value 50 † | 22.97% | -53.4% | 27.30% | -22.2% |
| Nifty200 Quality 30 | 11.91% | -29.1% | 11.34% | -22.4% |
| Nifty100 Low Volatility 30 | 13.31% | -30.6% | 12.75% | -18.1% |
| Nifty Alpha 50 | 23.48% | -38.3% | 24.45% | -31.3% |

Indices are NSE's total return indices, dividends included, on the strategy's
clock; where NSE printed no open for a factor index, its parent index's
overnight move stands in (v2-spec, Clarification to A4). The strategy is ahead
of every one of them in both periods.

† A factor index's figures from before it went live are NSE's back-test of its
own rules, not returns anyone could have earned. NSE's launch dates could not
be read from an NSE document, so each live part starts at the first date NSE
printed an open for the index. Nifty200 Momentum 30 is live from 12 Oct 2020,
and over those 5.9 years the strategy made 36.4% a year against the index's
17.5%. Nifty500 Value 50 is live from 16 Dec 2024, and over those 1.8 years
the strategy made 7.0% a year against the index's 5.9%, which is too short to
judge; the first 1.8 of its 3.6 held-back years are back-test. The other three
factor indices are live over the whole period.
<!-- numbers:results:end -->

**What backs it up.** The figures in this section come from the run's own
output files in [`data/backtest/v1_final`](data/backtest/v1_final). The
measured ones are listed with their definition, file and key in
[`baseline_report.md`](data/backtest/v1_final/baseline_report.md) and its
`.json`; the held-back worst falls above are worked out from the run's
`nav.csv` by the same rule as the whole period's. The method behind each
check is explained in
[research/strategy/v1-verification.md](research/strategy/v1-verification.md),
written on 23 September for the previous run, whose figures it still shows.

<!-- numbers:backing:start -->
- **Two engines agree exactly.** The production engine and an independent
  re-implementation written from the specification alone
  ([`jobs/crosscheck_v1.py`](jobs/crosscheck_v1.py)) hold the same stocks with
  the same ranks on all 31 decision dates, 310 positions in all, and their
  daily values agree on all 15 series (the strategy, the equal-weight universe
  and 13 index series) to within 1e-14 of each other
  ([`v1_final_check`](data/backtest/v1_final_check)).
- **It beats random picks from the same list.** Against 500 random portfolios
  run through the same engine and rules with one random ranking kept for the
  whole run, it lands at the 96th percentile: 20 did at least as well, and the
  median random portfolio made 20.7% a year. Against 500 with a fresh random
  ranking each quarter it lands at the 98.6th: 7 did as well, and the median
  random portfolio made 19.3%. The first kind turned over 0.5 times a year,
  the second 3.95, the strategy 1.96.
- **It fell less than the market over the whole period.** In the months the
  Nifty 500 rose, the strategy rose 1.30 times as much on average; in the
  months it fell, 0.66 times as much. Over rolling twelve-month windows it was
  ahead of the Nifty 500 84% of the time and of the same universe 72%. That
  did not hold within the held-back period on its own, where its worst fall,
  -23.8%, was deeper than the Nifty 500's -18.6%.
- **The margin over the same universe is probably real, but not proven.** In
  the held-back period it is 13.35 points a year. A 90% interval from a
  stationary bootstrap of the paired daily returns runs from -2.68 to +29.23
  points, and 8.4% of the resamples put the margin at zero or below.
<!-- numbers:backing:end -->

**What it does not show:**

<!-- numbers:limits:start -->
- **Skill beyond known factors.** Regressed on IIM Ahmedabad's published
  Indian four factors over the 82 months from Mar 2019 to Dec 2025, the last
  month IIMA has published, the strategy's alpha is 5.74% a year at t = 1.21
  (1.17 with a small-sample correction), which is not significant. Its value
  and momentum tilts are, strongly (t = 4.78 and 3.61), and the four factors
  explain 72% of its monthly returns' variation. Most of the return is the
  market plus factors that can be bought more cheaply.
- **That what is left is more than a quality tilt.** Adding a quality factor
  built from the archive (v2-spec A5, operating-margin version, the same 82
  months) lowers the alpha to 5.04% (t = 1.11). Over the 34 months from Mar
  2023, where the factor can use return on capital, the alpha is 11.27% (t =
  2.61) without the factor, which on its own would count as significant, and
  3.31% (t = 0.74) with it: quality accounts for 71% of the alpha in that
  window, which is only 2.8 years long.
- **Robustness to the search.** After 195 logged trials, the deflated Sharpe
  probability of skill is between 0.838 and 0.995 depending on how the spread
  across trials is estimated, around a usual bar of 0.95.
- **A smooth ride.** The worst falls were -33.6% from Jan 2020 to Mar 2020,
  back to its peak by Nov 2020, and -30.7% from Apr 2022 to Jun 2022, back to
  its peak by Jul 2023. The deepest that started inside the held-back period
  was -23.8% from Sep 2024 to Feb 2025, back to its peak by Sep 2025. From the
  end of 2025 to the open of 18 Sep 2026 it is down 3.3% while the same
  universe is up 8.7%, and it is still below its Feb 2026 peak, having been
  16.5% below it at the worst in Mar 2026.
<!-- numbers:limits:end -->

<!-- numbers:after_tax:start -->
**After Indian costs and tax.** From Rs 5 lakh at the open of 15 Feb 2019,
with itemised Indian trading costs, an assumed 0.1% slippage a side and
capital gains tax by lot at the rates in force at the time
([`jobs/after_tax.py`](jobs/after_tax.py), v2-spec A6), the strategy would
have been worth Rs 28.6 lakh if sold at the open of 18 Sep 2026, 25.8% a year,
after paying Rs 4.6 lakh of tax along the way; its worst fall on that basis
was -31.7%. A Nifty 500 index fund with a 0.16% expense ratio, held throughout
and sold at the end, gives Rs 12.7 lakh, 13.1% a year. Before 6 Sep 2019, the
start of the index fund whose costs are used, that fund is hypothetical.
<!-- numbers:after_tax:end -->

**Correction, 23 September 2026.** Statistics I published alongside the
one-shot result on 22 September were wrong. They were my own work, done after
the audited build had finished and never checked by anyone else. The worst was
a factor regression that subtracted the risk-free rate twice and turned an
insignificant alpha into a nearly significant one, on which I then built a
claim that the filters carried the strongest evidence. That claim is
withdrawn. The headline statistics are now cross-checked against standard
libraries (statsmodels, quantstats, arch) in
[`jobs/libcheck_v1.py`](jobs/libcheck_v1.py), and a second independent review
checked the corrections themselves.

### What the strategy actually is

At each quarterly date, a few weeks after the results deadline so the numbers
are fresh:

**Filters** (a stock must pass all of them): ordinary equity that traded
recently, at least Rs 20 lakh of daily turnover, at least 200 sessions traded
in the past year, not a bank, NBFC or insurer, four consecutive quarters of
results already published, profitable over those four quarters, and a
computable market capitalisation.

<!-- numbers:funnel:start -->
Across the 31 decision dates that takes an average of 1,853 ordinary-equity
symbols that traded recently down to an average of 903: 237 on the first date,
when few companies had four quarters of tagged results yet, up to 1,316, and
1,158 on average through the held-back period.
<!-- numbers:funnel:end -->

**Ranking** (five groups, equally weighted, no tuning): profitability and its
stability, revenue and profit growth, earnings and sales yield, 12-month
momentum skipping the latest month, and low volatility.

**Portfolio:** the best 10, at most 3 from any sector, equal weighted, a
holding kept while it stays inside the top 20, 0.20% cost charged per side,
dividends credited as cash.

Full definitions, including every ambiguity resolved during the build and the
date it was resolved, are in the specification.

### How it was checked

- **Two engines.** One production engine and one independent re-implementation,
  written from the specification without sight of each other. They agree on
  every stock, every date and every return.
- **Four audits**, for look-ahead, survivorship, trade accounting and
  statistics. They raised 17 findings and an independent sceptic had to
  reproduce each one before it was accepted. 16 were real and were fixed,
  including share counts that were wrong by 100x, a lender filter that used
  today's list of lenders, and companies that changed ticker being treated as
  dead.
- **A cross-check against published data.** Our momentum factor correlates 0.72
  to 0.81 with IIM Ahmedabad's published Indian factor returns.
- **Selection by rule.** 36 portfolio variants were run in-sample, and the
  plateau rule picked one before the held-back data was unlocked. It picked the
  configuration that was written down first.
- **195 trials** are logged in [`state/trials.jsonl`](state/trials.jsonl),
  including every failure below.

### What came before, and failed

Kept because they are the reason the result above is worth anything.

- **The multibagger study found nothing.** Low-margin small caps looked like
  they tripled twice as often. The base rate was wrong, overlapping windows
  inflated the statistics threefold, and the effect turned out to be
  dispersion: those stocks halve more often too.
  [Write-up](research/studies/multibagger.md).
- **A filing study was void**: the `orders` category turned out to be about a
  quarter regulatory penalties, carrying the opposite sign.
- **Volume spikes may predict negative returns**, but an audit re-measurement
  found almost no effect and the disagreement is unresolved.
  [Write-up](research/studies/volume_anomaly.md).
- **Only about 42.6% of randomly chosen liquid stocks beat the Nifty 500** over
  twelve months, with an honest range of 38.5% to 46.7%. That, not zero, is the
  bar any strategy has to clear.

---

## Layout

| Path | What's in it |
|------|---------|
| `zen/data/` | bhavcopy and filings download, normalisation, DuckDB and Parquet store |
| `zen/monitor/` | news collection, ranking, archive analytics, the news to data bridge |
| `zen/signals/` | screens and strategies |
| `zen/validation/` | look-ahead detection, event studies, the trial log |
| `zen/notify/` | email rendering, charts, SMTP delivery |
| `jobs/` | entry points that GitHub Actions calls |
| `research/` | exploration only, never scheduled |

Anything that has to run on a schedule is a module. Notebooks are for looking,
not for running.

| Workflow | When | What it does |
|----------|------|------|
| `daily_prices.yml` | 13:30 UTC, weekdays | fetch bhavcopy, store, commit |
| `daily_brief.yml` | 01:30 UTC, weekdays | build and send the brief |
| `signal_check.yml` | after the price update | evaluate strategies, alert if triggered |

Signal evaluation is chained off the price update rather than given its own
cron, so strategies never run against a stale archive.

---

## Data

| Source | Coverage | Cost |
|--------|----------|------|
| NSE bhavcopy | daily OHLCV, everything listed including later-delisted | free |
| NSE corporate filings | every announcement, timestamped by the exchange | free |
| NSE XBRL financials | quarterly income statement and balance sheet | free |
| NSE indices | daily levels of the index series counted above, and total-return series for the eight benchmarks in the result | free |
| RSS and a news API | Indian market, macro and global news | free tiers |
| Google Gemini | plain-English explanations, optional | free tier |

**Getting the fundamentals took longer than it should have.** I wrote off the
older NSE endpoint as empty, twice, and it wasn't. It returns nothing unless you
pass `period=Quarterly`. With that one parameter it returns 87,449 filings going
back to late 2016, and the ones usable for screening start in 2018. That mistake cost about two weeks.

**A stock's real news is filed with the exchange, not written by a journalist.**
The filing shows up within minutes of a board approving it. A newspaper covers
it days later, if at all.

Every filing carries `an_dt`, the moment NSE published it. Anything published
after the 15:30 close belongs to the next session, not the one that just ended.
`session_date` is the first session the stock actually traded after that,
which is the one the filing could affect. Getting that
backwards is a one-day look-ahead, the same shape of error that produced the
fake 30% CAGR.

Two thirds of the filings feed is statutory noise. "Copy of Newspaper
Publication" alone was 409 of 1,943 filings across four sessions. Routine
compliance gets matched and discarded first, so a newspaper advert announcing
results can't be mistaken for the results.

One category gets special handling. When the exchange formally asks a company to
explain an unusual move, that query arrives after the close, so it confirms the
move rather than explaining it. The brief says that, instead of presenting it as
a cause.

NSE changed the bhavcopy format on 8 July 2024. Both layouts are parsed into one
schema.

---

## Running it

```bash
python -m venv .venv
.venv/Scripts/activate            # Windows
pip install -r requirements.txt

python -m jobs.update_prices --days 7      # fetch recent sessions
python -m jobs.daily_brief --dry-run       # render locally, send nothing
python -m pytest tests/                    # includes the leak detector
python -m jobs.readme_numbers --check      # README figures still match their sources
```

Backfill history:

```bash
python -m jobs.update_prices --start 2015-01-01 --end 2026-09-07
```

Secrets never get committed. Locally they go in `.env`, which is gitignored, and
there's an `.env.example` to copy. On GitHub they live under Settings, Secrets
and variables, Actions: `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`
(a Gmail App Password, never the account password), `MAIL_TO`, and optionally
`GEMINI_API_KEY` and `MARKETAUX_KEY`.

---

## Mistakes

I keep these in public because they're more useful than the findings. Every one
produced a confident, wrong answer before I caught it.

| What broke | What it did | How I found it |
|---|---|---|
| Control group wasn't liquidity-matched | The baseline was 6.4x more liquid than the event stocks, so a size effect looked like a signal. An apparent +20pp edge dropped to +6.2pp and five categories turned out to be noise | Went back over how the control was built |
| Same-day split and bonus | Only one factor got applied, a 5x price error on DELPHIFX | A single-session return that couldn't be real |
| Leak detector only truncated prices | A strategy could read tomorrow's filings and still pass | Read the detector against the actual table list |
| Filing categoriser matched substrings | `order` missed "orders" and "expansion" missed "expands". The most important filing in the case study that started this got binned as `other` | Hand-checked a case I already knew the answer to |
| XBRL parser read a segment context | One division's revenue came through as the whole company's, because segment contexts share the parent's period | Fields came back null where I could see figures in the document |
| Backfill skipped quarters on restart | 171 filings became permanent holes that no rerun could fill | Diffed written rows against the index |
| Session re-warm keyed on a counter | Never fired once. Throughput fell from 1.8 docs/sec to 0.09 with no error in the log | Watched a live run stall |
| Connection reuse turned off | My own fix for the last bug added a TLS handshake to every request. 0.42s per document against 0.12s reusing a connection | Timed both against the same URL |
| Categorising filings by regex on free text | The `orders` bucket held 14,955 filings. Only about 24% were real order wins and about 27% were regulatory penalties, the opposite sign. The mix also shifted between periods, so my headline finding compared two different things. NSE labels every filing itself, and on that basis the correct bucket is 3,220 | Checked NSE's own filing subtypes |
| Joining filings to prices on equality | `trade_date` is a calendar weekday, which need not be a session the stock traded. 70,506 filings were dropped, skewed towards those filed before long weekends | Counted the join both ways |
| Adding a column to the data but not the schema | `session_date` went into 783,510 rows and not into the CREATE TABLE. Both daily jobs failed for four days. The same shape had shipped a week earlier in another table | CI, which builds from nothing. A working laptop cannot see it |
| Trusting my own calibration | I measured a text pattern at 87% recall. Three quarters of the filings I tested it on simply restate their own label, so the pattern was matching the label, not the text. Real recall was 49% | An independent reader checked what the test was actually measuring |
| Publishing statistics nobody had checked | After the audited build finished I ran the verification alone and published it the same day. The factor regression subtracted the risk-free rate twice and turned an insignificant alpha (4.7%, t = 0.99) into a nearly significant one (9.5%, t = 1.96), and I built a conclusion on it | A second independent audit, run specifically on the work that had skipped review |

The trial log in [`state/trials.jsonl`](state/trials.jsonl) records the
hypotheses I've formally tested, including the ones I abandoned, because how
many I tried decides whether the best result means anything. It doesn't yet
include about 50 exploratory statistics from piloting the news-reaction study,
so the true count is higher than the line count.

---

## Where it's up to

**Working:** the price archive, corporate filings, quarterly fundamentals, both
emails, the news to data bridge, corporate-action adjustment, matched controls,
the trial log, look-ahead detection, and schema-parity tests that stop the data
and the database definitions drifting apart. Strategy v1 is built, tested on
its held-back years, re-run on the repaired archive and measured, as
[above](#the-result).

**Strategy v2 was tested and did not replace v1.** v2 added my own hard
filters (ROCE at least 10%, debt to equity below 1.5, market cap above Rs 100
crore, P/E at most 70), twelve positions, a graded cash filter, three-tranche
entry, volatility sizing and NSE sector caps, all fixed in
[research/strategy/v2-spec.md](research/strategy/v2-spec.md) before it was
run. Two engines, one written from the spec without reading the other, built
it and agree on every holding, trade and daily value; the rules and code were
public on GitHub before the one full-period run. It passed two of the five
bars set in advance and failed three
([`data/backtest/v2_verdict.md`](data/backtest/v2_verdict.md), every figure
with its file and key):

| | v1 | v2 |
|---|---|---|
| Growth a year | 30.7% | 21.3% |
| Worst fall | -33.6% | -26.3% |
| Sortino, Calmar | 1.94, 0.94 | 1.74, 0.83 |
| IIMA four-factor alpha a year, t | 5.7%, 1.21 | 1.1%, 0.23 |
| After costs and tax, a year | 25.8% | 17.8% |

It made the ride smoother, as intended, but gave up too much return to do it,
so v1 stays. Buying every stock that passed v2's filters in equal amounts made
23.8% a year, more than v2's own ranking and sizing on top of them: the filters
picked a strong pool, and the extra machinery cost return. The technical entry
rule it tested (variant b) bought slightly cheaper but ended lower and was not
adopted. v2 is weaker evidence than v1 by construction: its rules were written
after v1's results were known, so it had no unseen data to be tested on.

**Next:** tracking v1 forward, on data that does not exist yet, is the only
evidence still to be had.

**Limits I'd rather state than have found:**

- Income statements go back to **2018**, balance sheets only to **Sep 2022**,
  because that is when SEBI's half-yearly requirement started producing tagged
  data. I can screen on leverage and return on capital today but cannot
  backtest them properly yet.
- NSE only began labelling filing types on **2024-09-23**, and the change was a
  single day rather than a phase-in. Before it, the exchange's own
  classification does not exist, so a filing study reaching back further is
  measuring a labelling convention. Each category records its first trustworthy
  year in code (`USABLE_FROM` in `zen/data/filing_types.py`). Studies have to
  apply it themselves, and not all of them do yet.
- Promoter pledge percentages are not machine-readable here. Only 34 filings
  carry a figure, so that limit is a manual check rather than a filter.

The momentum screen currently wired in is a **calibration baseline, not a
strategy for real money**. Momentum is well documented and I know roughly what
it should return, so it tests whether the harness reports honestly. If
validation comes back with an implausible number, the harness is broken.
Neither v1 nor v2 is wired into the emails.
