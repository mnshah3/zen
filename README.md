# Zen: automated research infrastructure for Indian equities

I built this to answer one question properly: if I screen Indian stocks on
fundamentals, does it actually work? Most retail backtests can't answer that
honestly, because they test against the companies that are still listed today.
So the first job was building an archive that doesn't cheat, and a test harness
that tries to break its own results.

The answer, on 3.6 years the design had never seen: **33.0% a year against
13.4% for the Nifty 500 including dividends**, and 21.0% for the same universe
equally weighted, with a smaller drawdown than the universe. The rules were
public before the test. It beats random picks from the same list, but most of
the return is the market and two well-known factors. [What that does and does
not prove](#the-result) is below.

It runs unattended on GitHub Actions and emails me two briefs. It places no
orders. It produces evidence, and I make the decisions.

```
prices        5,351,915 rows · 2,892 sessions · 4,060 ticker symbols · Jan 2015 to Sep 2026
filings         783,510 announcements · 2,539 companies · 2022 to 2026
fundamentals    102,711 quarterly filings · 2,629 companies · 2016 to 2026
```

### Where to look first

| If you want | Go to |
|---|---|
| What the emails look like | [sample brief](https://htmlpreview.github.io/?https://github.com/mnshah3/zen/blob/main/docs/sample-brief.html) |
| **The result, and what it does not prove** | [The result](#the-result) below |
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

It isn't a small effect. Counting ordinary equity only, this archive holds
**3,548 ticker symbols**, and **2,563** of them traded in September 2026. That
leaves **985 symbols** that were tradeable and are no longer quoted. About 200
of those are renames of companies that still trade, which leaves roughly
**780 that genuinely left the market**.

The universe on each date is whatever actually traded that day:

| Month | Equity securities trading |
|------|---------------------------|
| Mar 2015 | 1,476 |
| Jun 2023 | 1,880 |
| Sep 2026 | 2,563 |

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
market on data it had never seen, by a margin unlikely to be luck. Most of its
return is the market and two well-known factors, and what is left after them
is not yet statistically significant.**

The rules were written into
[research/strategy/v1-spec.md](research/strategy/v1-spec.md) and committed on
21 September 2026. Everything from 15 February 2023 onward was locked in code
and the configuration chosen in advance was run on it on 22 September. That
one-shot run gave **29.3% a year against 20.5%** for the same universe equally
weighted, and it stays on record as the pre-registered result.

An independent audit then found the archive was missing the March 2025
quarter for about 650 companies, because the code listing NSE's filings
stopped at the first page that failed to load. That was fixed and the quarter
refilled. The same pass also collected about 2,200 revised filings for the
later quarters, which the regular updater had never fetched. The engine only
uses a revision after the date it was published, so both are legitimate, but
they move the result: the March refill alone gives about 33.8% a year, and
with the revisions as well it gives 33.0%, which is the figure below. The same
configuration was re-run with no rule changed. This is the held-back period on
the corrected archive, 3.6 years, open 15 February 2023 to open 18 September
2026 (index returns run from the close before each of those opens):

| | Return a year | Worst fall |
|---|---|---|
| **The strategy** | **33.0%** | **-23.8%** |
| The same universe, equally weighted | 21.0% | -31.1% |
| Nifty 500, dividends included | 13.4% | -18.6% |
| Nifty Midcap 150 | 21.3% | -20.9% |
| Nifty Smallcap 250 | 21.8% | -26.0% |

**What backs it up**, all in
[research/strategy/v1-verification.md](research/strategy/v1-verification.md):

- **Two engines agree exactly.** An independent re-implementation, extended to
  2026 without sight of the production code, picks the same stocks on all 31
  rebalance dates and matches the daily portfolio value to fifteen decimal
  places. A rebuild from raw exchange prices matched all 226 trades of the
  one-shot run.
- **It beats random picks from the same list.** Against 500 random ten-stock
  portfolios run through the same machinery, it lands at the 94th to 96th
  percentile, whether the random portfolios trade more than it does or less.
- **It fell less than the market over the full period.** From 2019, against
  the Nifty 500 total return, it captured 129% of the rising months and 68% of
  the falling ones. That did not hold within the held-back period on its own,
  where its worst fall, -23.8%, was deeper than the Nifty 500's -18.6%.
- **The margin is probably real, but not proven.** Over the equal-weight
  universe it is 12.0 points a year. A 90% interval from a stationary
  bootstrap runs from -3.9 to +27.4, and about 1 resample in 9 put the margin
  at zero or below.

**What it does not show:**

- **Skill beyond known factors.** Regressed on IIM Ahmedabad's published Indian
  factors, the strategy's alpha is 5.4% a year at t = 1.15, which is not
  significant. Its value and momentum tilts are, strongly. Most of the return
  is the market plus factors that can be bought more cheaply.
- **Robustness to the search.** After 195 logged trials, the deflated Sharpe
  probability of skill is between 0.82 and 0.995 depending on the method, around
  a usual bar of 0.95.
- **A smooth ride.** The worst falls were -33.6% in 2020 and -30.7% over 15
  months from April 2022, and it is 4.8% down in 2026 while the same universe is
  up 8.8%.

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
computable market capitalisation. That takes about 2,000 traded symbols down to
about 900 on average: 237 on the first date, when few companies had four
quarters of tagged results yet, up to 1,334, and about 1,170 through the
held-back period.

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
| NSE indices | 18 benchmark series | free |
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
and the database definitions drifting apart.

**Next:** a pre-registered backtest of published factors (profitability,
value, growth, momentum and low volatility) on a point-in-time universe that
includes the stocks that later died, with 2023 onward held back and tested
once. The spec is in [research/strategy/v1-spec.md](research/strategy/v1-spec.md).

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
validation comes back with an implausible number, the harness is broken. The
real strategy comes after I've shown the harness can be trusted.
