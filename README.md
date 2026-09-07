# Zen

Research infrastructure for Indian equities: a survivorship-bias-free price
archive, a validation harness that tries to break its own backtests, and a
daily signal engine that reports to Telegram.

It does **not** place orders. It produces evidence; the allocation decision
stays human.

## Why the archive matters

Most retail backtests in India are run against whatever tickers are listed
*today*. Companies that delisted, went to zero, or were absorbed simply are not
in the sample, so the strategy is measured only against survivors and every
result is inflated.

This archive is assembled from NSE daily bhavcopy files, which record every
security that traded on a given day, including those later delisted. The
universe on any past date is what actually existed on that date.

The security count grows with time -- 1,475 names in March 2015, 2,884 in
September 2026 -- which is what an unbiased sample looks like.

## Layout

| Path | Purpose |
|------|---------|
| `zen/data/` | bhavcopy download, normalisation, DuckDB + parquet store |
| `zen/universe/` | point-in-time index membership |
| `zen/signals/` | screens and strategies |
| `zen/validation/` | leak detection, walk-forward, deflated Sharpe |
| `zen/portfolio/` | allocation policy across trading and long-term sleeves |
| `zen/monitor/` | news and macro watch |
| `zen/notify/` | Telegram / email delivery |
| `jobs/` | entry points invoked by GitHub Actions |
| `research/` | notebooks -- exploration only, never scheduled |

Anything that must run on a schedule is a `.py` module. Notebooks are for
looking, not for running.

## Data

| Source | Coverage | Cost |
|--------|----------|------|
| NSE bhavcopy | daily OHLCV, all listed incl. later-delisted | free |
| NSE corporate actions | splits, bonuses, dividends | free |
| NSE index change logs | historical constituents | free |

NSE changed the bhavcopy format on 8 July 2024. Both layouts are parsed and
normalised to one schema.

## Storage

Git tracks one small parquet file per trading day under `data/daily/`.
The DuckDB file is derived and gitignored -- rebuild it with
`store.rebuild_from_parquet()`. Committing a growing binary daily would
bloat the repository within weeks.

## Setup

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows
pip install -r requirements.txt
python -m jobs.update_prices --days 7
```

Backfill history:

```bash
python -m jobs.update_prices --start 2015-01-01 --end 2015-12-31
```

## The two emails

**Brief A -- daily market brief.** Sent every weekday morning whether or not
anything happened. Market breadth computed from our own archive, then news
collected from RSS, deduplicated by headline similarity, ranked by source
trust, corroboration and recency, and filtered against a rolling 7-day memory
so the same story never arrives twice.

**Brief B -- signal alert.** Silent by default. Sent only when a strategy
actually fires, and suppressed when the signal set is unchanged from the last
alert. Every signal carries its facts, its rationale, and its counter-argument:
`Signal` refuses to be constructed without one, because a position whose
failure mode you cannot name has not been thought through.

## Validation

`zen/validation/leak.py` runs a strategy twice for the same date -- once
against the full archive, once against an archive physically truncated at that
date -- and requires identical output. Any look-ahead changes the answer, so a
mis-shifted column, a full-sample normalisation and a bad join all fail the
same test without needing to be anticipated individually.

`tests/test_leak.py` contains a strategy that ranks on forward returns. It must
fail the detector. If it ever passes, the harness is broken and nothing
downstream can be trusted.

## Secrets

Never committed. Locally in `.env` (gitignored, see `.env.example`); on GitHub
under Settings -> Secrets and variables -> Actions.

| Secret | Value |
|--------|-------|
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `587` |
| `SMTP_USER` | sending Gmail address |
| `SMTP_PASSWORD` | Gmail **App Password**, never the account password |
| `MAIL_TO` | recipient |

## Schedule

| Workflow | When | Does |
|----------|------|------|
| `daily_prices.yml` | 13:30 UTC, weekdays | fetch bhavcopy, store, commit |
| `daily_brief.yml` | 01:30 UTC, weekdays | Brief A, always sends |
| `signal_check.yml` | after the price update | Brief B, only if triggered |

Signal evaluation is chained off the price update rather than given its own
cron, so strategies are never run against a stale archive.

## Status

Archive, both briefs, and leak detection working. Walk-forward backtesting and
a real strategy next -- the momentum screen currently wired in is a calibration
baseline used to check that the harness reports honestly, not a strategy
intended for capital.
