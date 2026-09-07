# Zen

Automated research infrastructure for Indian equities.

A survivorship-bias-free price archive going back to 2015, a daily brief that
connects market news to what the market actually did, and a validation harness
built to break its own backtests before they can mislead anyone.

Runs unattended on GitHub Actions. Places no orders — it produces evidence, and
the allocation decision stays human.

```
5,328,811 rows   ·   2,884 sessions   ·   4,044 securities   ·   Jan 2015 – Sep 2026
```

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

## Status

Working: the archive, corporate filings, both briefs, the news-data-filings
bridge, and look-ahead detection.

Next: quarterly financials from exchange XBRL, then walk-forward backtesting
with a persistent trial counter, corporate-action adjustment, and
point-in-time index membership.

The momentum screen currently wired in is a **calibration baseline, not a
strategy intended for capital**. Momentum is heavily documented and its rough
magnitude is known, which makes it a test of the harness rather than of the
idea. If validation reports an implausible return, the harness is broken. The
real strategy comes after the harness has been shown to report honestly.
