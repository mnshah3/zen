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

## Secrets

Never committed. Locally in `.env` (gitignored, see `.env.example`); on GitHub
under Settings -> Secrets and variables -> Actions.

## Schedule

| Workflow | When | Does |
|----------|------|------|
| `heartbeat.yml` | 02:00 UTC daily | proves schedule + secrets + delivery |
| `daily_prices.yml` | 13:30 UTC weekdays | fetch, store, commit, report |

## Status

Archive and delivery working. Validation harness and signals next.
