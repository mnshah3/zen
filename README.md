# Zen: automated research infrastructure for Indian equities

I built this to answer one question properly: if I screen Indian stocks on
fundamentals, does it actually work? Most retail backtests can't answer that
honestly, because they test against the companies that are still listed today.
So the first job was building an archive that doesn't cheat, and a test harness
that tries to break its own results.

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
| What the research has shown so far | [What I've found so far](#what-ive-found-so-far) below |
| Studies, including two I had to retract | [`research/studies/`](research/studies/) |
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

## What I've found so far

**Nothing that constitutes an edge yet.** The long-horizon studies I have run
so far either failed proper testing or are unresolved, and I've kept them in
instead of hiding them.

This project is aimed at long-term investment, so the question that matters is
whether a company's fundamentals and filings predict multi-year outperformance.
My first long-horizon filing study used a category that turned out to be
about a quarter regulatory penalties, which carry the opposite sign to the
order wins I thought I was measuring. Those results are void. See
[Mistakes](#mistakes).

What I do have is baseline measurements, not edge:

- **Volume spikes may predict negative returns**, growing with the size of the
  spike, against liquidity-matched controls (stocks with similar trading
  volume). An audit re-measurement found almost no effect, that disagreement is
  unresolved, and the spike ratio is still computed on unadjusted volume. See
  [research/studies/volume_anomaly.md](research/studies/volume_anomaly.md).
- **Only about 42.6% of randomly chosen liquid stocks beat the Nifty 500** over
  twelve months. It comes from about 40 entry dates, so the honest range is
  roughly 38.5% to 46.7%. Any strategy has to clear that bar, not zero.
- **Regulatory filings move prices by -0.04pp**, which is flat. I had written
  into the code that they were bad news. They are not.
- Order wins gain 0.85pp on the day a filing lands and give back 0.47pp over
  the next three sessions. That is a short-horizon fact about the reaction, and
  it is here because it corrected a wrong sign in the code. **It says nothing
  about whether order wins precede multi-year compounding**, which is the
  actual question and needs a different measurement entirely.

**The multibagger study found nothing.** I asked whether anything visible at a
month-end (margins, growth, momentum, size, order and expansion filings)
predicted a stock tripling over the next two years. The first pass said
low-margin small caps tripled twice as often. Proper testing took it apart.
The base rate was wrong, overlapping windows inflated the t-statistics about
threefold, and the effect turned out to be dispersion: those stocks halve more
often too, and their average return is no better. Full write-up in
[research/studies/multibagger.md](research/studies/multibagger.md).

**Next** is testing a few factors that already have published evidence
behind them, chosen in advance, instead of searching for new patterns. The
design is written down before any result exists, in
[research/strategy/v1-spec.md](research/strategy/v1-spec.md).

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
