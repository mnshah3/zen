# External data, and how to get it again

None of this is committed. It is free to download and it belongs to whoever
published it, so the repo records the source rather than the file.

## NSE total-return indices (`niftyindices/`, `nifty_tri.parquet`)

Gross total return, daily, for the benchmarks:

| Index | From | To |
|---|---|---|
| Nifty 500 (also net total return) | 2015-01-01 | 2026-09-23 |
| Nifty Midcap 150 | 2016-09-21 | 2026-09-23 |
| Nifty Smallcap 250 | 2018-09-21 | 2026-09-23 |
| Nifty 200 Momentum 30, Nifty 500 Value 50, Nifty 200 Quality 30, Nifty 100 Low Volatility 30, Nifty Alpha 50 (v2 amendment A4) | 2018-09-21 | 2026-09-23 |

From niftyindices.com, Reports, Historical Data, "Total returns Index Values".
The site silently returns nothing for a range longer than a year, so each
export covers a year or less. Some years were exported as CSV; the rest were
read from the page's results table and written in the same CSV layout, with a
row count, value sum and hash of the page table matched against the file.

`python -m jobs.build_nifty_tri` turns the folder into the parquet. It refuses
a session missing against the Nifty 500 calendar (or one Nifty 500 does not
have), a gap over 7 days, two exports that disagree about the same session,
and any change to a row already in the parquet. On 2026-09-24 all 72 files
agreed and every factor index matched the Nifty 500 calendar session for
session.

Why it matters: the index series NSE publishes in its daily archive is price
only, so it leaves out dividends and understates the benchmark by about 1.0 to
1.25% a year. Measuring a strategy that collects dividends against an index
that does not is a free head start of about a percentage point a year.

Checked on download: every overlapping row of the price-only series equals the
close already stored in `data/indices`, exactly.

## Factor-index opens (`nifty_price_endpoints.csv`)

The benchmark clock starts and ends at a market open (v1 Clarification 17), so
each factor index needs its price-only open and the previous close on those
dates. `data/indices` does not carry the factor indices, so these 40 rows (five
indices, 14-15 Feb 2019, 14-15 Feb 2023, 17-18 Sep 2026, 22-23 Sep 2026) were
read from the same site's "Index Values" table. The same table for Nifty 500 on
13-15 Feb 2019 equals `data/indices` exactly on open, high, low and close.

NSE prints "-" for the open, high and low before it calculates an index live:
Momentum 30 has opens from 12 Oct 2020 and Value 50 from 16 Dec 2024. How a
missing open is handled is fixed in the v2 spec, clarification to A4.

## IIM Ahmedabad Indian factor data (`iima/`)

Fama-French and momentum factor returns for India, from Agarwalla, Jacob and
Varma at IIM Ahmedabad: https://faculty.iima.ac.in/iffm/Indian-Fama-French-Momentum/

Used once, to check our own factor returns against published ones. Our momentum
series correlates 0.72 to 0.81 with theirs, which is what said the price
archive and the corporate-action adjustment are sound.

Cite them if you use it: Agarwalla, Jacob and Varma (2013), "Four factor model
in Indian equities market", IIM Ahmedabad Working Paper.
