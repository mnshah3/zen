# External data, and how to get it again

None of this is committed. It is free to download and it belongs to whoever
published it, so the repo records the source rather than the file.

## NSE total-return indices (`niftyindices/`, `nifty_tri.parquet`)

Nifty 500, Nifty Midcap 150 and Nifty Smallcap 250, gross total return, daily.
From niftyindices.com, Reports, Historical Data, "Total returns Index Values".
One year per download, exported as CSV.

Why it matters: the index series NSE publishes in its daily archive is price
only, so it leaves out dividends and understates the benchmark by about 1.0 to
1.25% a year. Measuring a strategy that collects dividends against an index
that does not is a free head start of about a percentage point a year.

Checked on download: every overlapping row of the price-only series equals the
close already stored in `data/indices`, exactly.

## IIM Ahmedabad Indian factor data (`iima/`)

Fama-French and momentum factor returns for India, from Agarwalla, Jacob and
Varma at IIM Ahmedabad: https://faculty.iima.ac.in/iffm/Indian-Fama-French-Momentum/

Used once, to check our own factor returns against published ones. Our momentum
series correlates 0.72 to 0.81 with theirs, which is what said the price
archive and the corporate-action adjustment are sound.

Cite them if you use it: Agarwalla, Jacob and Varma (2013), "Four factor model
in Indian equities market", IIM Ahmedabad Working Paper.
