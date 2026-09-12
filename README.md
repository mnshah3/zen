# Zen - Automated research infrastructure for Indian equities.

A survivorship-bias-free price archive going back to 2015, quarterly company
fundamentals from exchange XBRL, a daily brief that connects market news to
what the market actually did, and a validation harness built to break its own
backtests before they can mislead anyone.

Runs unattended on GitHub Actions. Places no orders — it produces evidence, and
the allocation decision stays human.

```
prices        5,334,585 rows · 2,886 sessions · 4,046 securities · Jan 2015 – Sep 2026
filings         782,485 announcements · 2,537 companies · 2022 – 2026
fundamentals     87,449 quarterly filings · 2,403 companies · 2018 – 2026
```

### Where to start reading

| If you want | Go to |
|---|---|
| What it measures and how honestly | [`research/studies/`](research/studies/) |
| Every hypothesis ever tested, including the failures | [`state/trials.jsonl`](state/trials.jsonl) |
| The look-ahead detector | [`zen/validation/leak.py`](zen/validation/leak.py) |
| Why the fundamentals archive exists at all | [`zen/data/financials_legacy.py`](zen/data/financials_legacy.py) |
| What broke and what it cost | [What this has got wrong](#what-this-has-got-wrong) below |

---

## The morning brief

**[Open a real one →](https://htmlpreview.github.io/?https://github.com/mnshah3/zen/blob/main/docs/sample-brief.html)**  ·  built and sent by GitHub Actions every weekday at 07:00 IST

Not a news digest. It reads the archive first, decides what the session
actually did, and only then goes looking for stories that explain it.

**It starts from the data, not the headlines.** Breadth, the gap between
heavyweights and the median stock, 52-week extremes, size rotation, and every
stock that traded far above its own 60-day volume. Which stocks the archive
flags is what determines which stories get promoted — a filing that explains an
unusual move outranks a better-corroborated story about something that did not
happen.

**It is ranked for one reader.** Trust, corroboration and recency make a decent
front page and an indifferent personal brief: two readers with opposite
portfolios would get the identical email. Stories are also scored against the
reader's own stated theses — AI and data centres, nuclear, solar, water,
infrastructure, defence, import substitution — and carry a badge saying which
one matched and why. Theme affinity is *added* to the score, never multiplied,
because a brief that only shows what you already believe is worse than a
generic one.

**Keyword matching is the weak link, so it is bounded.** Each theme separates
terms that count alone from terms that need corroboration: one "data centre" is
enough, one "power" is not. Where a news API supplies entity tags, the guessing
stops entirely and the story carries real NSE tickers with sentiment.

**The charts are HTML, not images.** Gmail, Outlook and Apple Mail all block
images by default for an unknown sender, so a brief whose visual layer is
attached PNGs is, for a large share of opens, a wall of text. The advance/
decline bar, thirty sessions of net breadth, the divergence gauges and the
rotation heat table are built from table cells and background colours. They
render everywhere, at every width, with images off. Every bar is labelled with
its number too, since colour alone is unreadable to about one man in twelve.

**Everything degrades rather than fails.** No model key means extracted facts
instead of prose. A dead feed means fewer stories. A broken chart means no
chart. The email goes out.

### The second email

**[A calibration run →](https://htmlpreview.github.io/?https://github.com/mnshah3/zen/blob/main/docs/sample-calibration.html)**

Silent by default — it only sends when something fires, because an alert that
arrives daily stops being an alert.

Strategies carry a `validated` flag, default false, set true only after a
walk-forward backtest that survives the look-ahead detector and reports its
trial count. While anything unvalidated is contributing, the subject line says
CALIBRATION, the sections read "ranked highest by the test screen" rather than
"to buy", and a banner sits above everything saying not to trade on it. That
exists because an earlier version headed a calibration run "To buy (15)" and
was read exactly as it was written.

---

## The problem this exists to solve

Most retail backtests in India are run against whatever tickers are listed
**today**. Companies that delisted, collapsed, or were absorbed are simply
absent from the sample. The strategy is therefore measured only against
survivors, and every result it produces is inflated.

This is not a small effect. This archive holds **4,044 securities**, while
roughly 2,900 trade today — about **1,150 companies** that existed, were
tradeable, and are missing from any present-day ticker list.

The count grows with time, which is what an unbiased sample looks like:

| Date | Securities in the universe |
|------|---------------------------|
| Mar 2015 | 1,475 |
| Jun 2023 | 2,015 |
| Sep 2026 | 2,884 |

The archive is assembled from NSE daily bhavcopy files, which record every
security that traded on a given day. The universe on any past date is what
actually existed on that date, not what survived to now.

---

## Design decisions worth explaining

**The database is derived, not stored.** Git tracks one Parquet file per month;
DuckDB is rebuilt from them in seconds. Committing a growing binary daily would
have bloated the repository within weeks. Monthly rather than daily partitions
cut the file count from ~2,500 to ~140 per decade and compress far better.

**Signals must argue against themselves.** The `Signal` class refuses to be
constructed without a stated counter-argument. A position whose failure mode you
cannot name has not been thought through, and the constructor enforces that
rather than trusting discipline.

**Rules decide, models explain.** Story selection, deduplication and ranking are
deterministic and inspectable. A language model only rewrites the chosen stories
in plain English. If the model is unavailable the brief still goes out with
extracted facts — delivery never depends on a third party being up.

**Claims are scoped to their evidence.** The brief says "volume spikes our news
sources do not account for", not "no news exists". The feeds carry market news,
not company filings, and the wording reflects exactly what was checked.

---

## Validation

The central test is deliberately hard to fool:

> Run the strategy as of date **T** against the full archive.
> Run it again against an archive **physically truncated** at T.
> The two answers must be identical.

Any look-ahead changes the answer — a mis-shifted column, a normalisation
computed over the full sample, a join that quietly pulls tomorrow's row. All of
them fail the same test, without needing to be anticipated individually.

`tests/test_leak.py` contains a strategy that ranks on forward returns. **It must
fail the detector.** A detector that never fires proves nothing, so the suite
asserts the failure. If that test ever passes, the harness is broken and every
result downstream of it is worthless.

This exists because an earlier iteration of this work reported a 30% CAGR that
turned out to be a one-day look-ahead error. The harness was built before the
strategy, deliberately.

---

## What it sends

**Daily brief** — every weekday morning, three parts:

1. **Today's read** — where the news and the archive agree or disagree
2. **What the data says** — breadth, index-vs-median divergence, 52-week
   extremes, size-tier rotation, and stocks trading at multiples of their own
   normal volume, with charts
3. **What happened** — ranked and deduplicated news, explained in plain
   English, with the numbers extracted and jargon defined

The third section is available from any news site. The first two are not — they
come from the archive, cover the whole market rather than the twenty names in
the headlines, and measure each stock against its own history rather than a
market-wide threshold.

**Signal alert** — silent by default. Sent only when a strategy fires, and
suppressed when the signal set is unchanged since the last alert. An alert that
arrives every day stops being an alert.

---

## Architecture

| Path | Purpose |
|------|---------|
| `zen/data/` | bhavcopy download, normalisation, DuckDB + Parquet store |
| `zen/monitor/` | news collection, ranking, archive analytics, the news–data bridge |
| `zen/signals/` | screens and strategies |
| `zen/validation/` | look-ahead detection |
| `zen/notify/` | email rendering, charts, SMTP delivery |
| `jobs/` | entry points invoked by GitHub Actions |
| `research/` | notebooks — exploration only, never scheduled |

Anything that must run on a schedule is a module. Notebooks are for looking, not
for running.

### Scheduling

| Workflow | When | Does |
|----------|------|------|
| `daily_prices.yml` | 13:30 UTC, weekdays | fetch bhavcopy, store, commit |
| `daily_brief.yml` | 01:30 UTC, weekdays | build and send the brief |
| `signal_check.yml` | after the price update | evaluate strategies, alert if triggered |

Signal evaluation is **chained off** the price update rather than given its own
cron, so strategies are never run against a stale archive.

---

## Engineering notes

Real defects found and fixed while building the archive, kept here because they
are the interesting part:

- **NSE's date stamps are not stable.** `13-Jul-20` on one session,
  `14-JUL-2020` on the next. This killed a six-year backfill at July 2020. The
  requested date is now authoritative and the file's own stamp is only
  sanity-checked against it.
- **`feedparser`'s default user-agent is silently rejected** by some
  publishers, which return HTTP 200 with an empty document. Business Standard
  was contributing zero stories and nothing looked wrong. Feeds are now fetched
  through `requests` with a real user-agent.
- **A single malformed session could abandon a multi-year backfill.** Failures
  are now caught per day, and because the loop only fetches dates missing from
  the archive, a skipped day self-heals on the next run.
- **`isin` shadows `DataFrame.isin`.** Renamed before it could cause a silent
  wrong answer rather than a loud error.
- **News and price data are not on the same clock.** Bhavcopy publishes ~18:00
  IST for a session that ended at 15:30; the brief runs at 07:00 the next day.
  Stock-move matching therefore uses a 48-hour news window while the brief
  displays 24 hours, because an evening filing routinely drives the next
  session's volume.

---

## Data sources

| Source | Coverage | Cost |
|--------|----------|------|
| NSE bhavcopy | daily OHLCV, all listed including later-delisted | free |
| NSE corporate announcements | every filing, timestamped by the exchange | free |
| NSE equity list | symbol to company name | free |
| RSS (13 feeds) | Indian market, macro, and global news | free |
| Google Gemini | plain-English explanations, optional | free tier |

### Corporate filings

A stock's real news is filed with the exchange, not written by a journalist.
The filing appears within minutes of a board approving it; a newspaper covers
it days later, if at all.

Each filing is stamped with `an_dt`, the moment NSE published it, and
attributed to `trade_date` -- the first session it could actually affect.
A filing at 17:59 belongs to the next session, not the one that just closed.
Getting that backwards is a one-day look-ahead, the same error shape that
produced a fictitious 30% CAGR once already.

Two thirds of the feed is statutory noise: "Copy of Newspaper Publication"
alone was 409 of 1,943 filings over four sessions. Routine compliance is
matched and discarded before anything else, so a newspaper advertisement
announcing results can never be mistaken for the results themselves.

One category earns its own treatment. When the exchange formally asks a
company to explain an unusual move, that query arrives after the close --
so it confirms the move rather than explaining it. The brief says exactly
that, rather than presenting it as a cause.

Every component runs on free infrastructure. NSE changed the bhavcopy format on
8 July 2024; both layouts are parsed and normalised to one schema.

---

## Setup

```bash
python -m venv .venv
.venv/Scripts/activate            # Windows
pip install -r requirements.txt

python -m jobs.update_prices --days 7      # fetch recent sessions
python -m jobs.daily_brief --dry-run       # render locally, send nothing
python -m pytest tests/                    # including the leak detector
```

Backfill history:

```bash
python -m jobs.update_prices --start 2015-01-01 --end 2026-09-07
```

Secrets are never committed — locally in `.env` (gitignored, see
`.env.example`), on GitHub under Settings → Secrets and variables → Actions:
`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD` (a Gmail App Password,
never the account password), `MAIL_TO`, and optionally `GEMINI_API_KEY`.

---

## What this has got wrong

The corrections matter more than the findings, so they are kept in public
rather than quietly patched. Each of these produced a confident, wrong result
before it was caught.

| Bug | What it did | How it was caught |
|---|---|---|
| Control group not liquidity-matched | Baseline was 6.4× more liquid than the event stocks, turning a size effect into a signal. An apparent +20pp edge fell to +6.2pp and five categories became noise | Audit of the control construction |
| Same-day split **and** bonus | Only one factor applied, a 5× price error on DELPHIFX | Implausible single-session return |
| Look-ahead detector truncated only prices | A strategy could read tomorrow's filings and still pass | Reading the detector against the table list |
| Filing categoriser matched substrings | `order` missed "orders"; "expansion" missed "expands". The most important filing in the motivating case study was binned as `other` | Hand-checking a known case |
| Regex `orders` bucket | Only ~24% were genuine order wins; ~27% were regulatory penalties, the opposite sign. Composition shifted between periods, so the headline "+6.2pp that did not replicate" compared two different things | Checking NSE's own filing subtypes |
| XBRL parsed a segment context | One division's revenue read as the whole company's, because segment contexts share the parent's period | Fields returning null where figures existed |
| Backfill skipped quarters on restart | 171 filings became permanent holes no rerun could fill | Diffing written rows against the index |
| Session re-warm keyed on a counter | Never fired once. Throughput fell from 1.8 docs/sec to 0.09 with no error in the log | Watching a live run stall |

The trial log in [`state/trials.jsonl`](state/trials.jsonl) records every
hypothesis tested, including abandoned ones, because the count of attempts is
what determines whether the best result means anything.

---

## Status

Working: the price archive, corporate filings, quarterly fundamentals, both
briefs, the news-data-filings bridge, corporate-action adjustment, matched
controls, the trial log, and look-ahead detection.

In progress: the point-in-time investable universe, then walk-forward
backtesting of a fundamental screen.

Known boundaries, stated rather than discovered later:

- Income statements reach back to **2018**; balance sheets only to **Sep 2022**,
  because that is when SEBI's half-yearly requirement produced tagged data.
  So leverage and return-on-capital can be screened live but not yet backtested.
- Announcements begin **2022**, so any news-based factor is limited to a
  shorter window than the fundamental ones.
- Promoter pledge percentages are not machine-readable in this archive — only
  34 filings carry a figure — so that limit is a manual check, not a filter.

The momentum screen currently wired in is a **calibration baseline, not a
strategy intended for capital**. Momentum is heavily documented and its rough
magnitude is known, which makes it a test of the harness rather than of the
idea. If validation reports an implausible return, the harness is broken. The
real strategy comes after the harness has been shown to report honestly.
