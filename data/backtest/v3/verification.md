# v3 independent verification

Written by `jobs/verify_v3.py` from the spec text and the run folders only (section 15.8). 1007 items compared: **998 match, 9 do not**; 5 items not verified (listed below).

| Part | Items | Match |
|---|---|---|
| A0 | 12 | 12 |
| A1 | 7 | 7 |
| A2 | 38 | 38 |
| A3 | 112 | 112 |
| A4 | 233 | 233 |
| A5 | 461 | 461 |
| A6 | 104 | 104 |
| B | 6 | 6 |
| B6 | 10 | 10 |
| C | 24 | 15 |

## In short

- Verdict figures (A0 to A6): 967 of 967 items recomputed from the run folders match.
- Rule checks on the run's records (B, B6): 16 of 16 check groups pass with no violation.
- Re-derivation from the frozen database (C): 15 of 24 items match. The rest are the counts taken with the spec's rules alone on the frozen table; each differing stock is a filing that pit's unit-scale screen set aside (plumbing, not reproduced). With that list applied, every member of all three reviews matches on every flag and field, and the ranking given the eligible set and measures matches exactly.

## Items that do not match

- **C** `2021-06-01: universe members matching on every flag F1-F6 and eligible (spec rules on the frozen table, no screen)`: verdict 767.0, recomputed 765.0, difference -2.0, tolerance 0. verdict side = members; every non-match is listed in the detail with its diagnosis
- **C** `2021-06-01: members matching on every derived field (basis, L, run, TTM sums, ICR, D/E, DOWN0/1, flags, measures, P/E)`: verdict 767.0, recomputed 764.0, difference -3.0, tolerance 0.
- **C** `2022-06-01: universe members matching on every flag F1-F6 and eligible (spec rules on the frozen table, no screen)`: verdict 910.0, recomputed 905.0, difference -5.0, tolerance 0. verdict side = members; every non-match is listed in the detail with its diagnosis
- **C** `2022-06-01: members matching on every derived field (basis, L, run, TTM sums, ICR, D/E, DOWN0/1, flags, measures, P/E)`: verdict 910.0, recomputed 900.0, difference -10.0, tolerance 0.
- **C** `2022-06-01: eligible members only in the recorded set (no screen)`: verdict 0.0, recomputed 3.0, difference 3.0, tolerance 0. ['ICIL', 'JINDALSAW', 'SHANTIGEAR']
- **C** `2022-06-01: eligible members only in my set (no screen)`: verdict 0.0, recomputed 1.0, difference 1.0, tolerance 0. ['CAPLIPOINT']
- **C** `2023-08-16: universe members matching on every flag F1-F6 and eligible (spec rules on the frozen table, no screen)`: verdict 1032.0, recomputed 1025.0, difference -7.0, tolerance 0. verdict side = members; every non-match is listed in the detail with its diagnosis
- **C** `2023-08-16: members matching on every derived field (basis, L, run, TTM sums, ICR, D/E, DOWN0/1, flags, measures, P/E)`: verdict 1032.0, recomputed 1020.0, difference -12.0, tolerance 0.
- **C** `2023-08-16: eligible members only in my set (no screen)`: verdict 0.0, recomputed 4.0, difference 4.0, tolerance 0. ['ICIL', 'IRCON', 'LODHA', 'SHANTIGEAR']

## Not verified

- A0 `runs.v3 / runs.v1_window_view paths, power_caveat, a2.sources, rehearsal_not_the_verdict`: text or labels, nothing to recompute
- A0 `v1_reproduction (replay against v1_final: NAV differences, holdings, trades)`: needs data/backtest/v1_final, outside the requested comparison; the A2 bars use the replay as section 14.2 says
- A2 `a2.v1_final_window_figures`: needs v1_final's own window view; the requested comparison is v3 against the v1 replay's window view
- A2 `a2.bars_against_v1_final`: needs v1_final's own window view; the requested comparison is v3 against the v1 replay's window view
- A4 `score_signal.quintiles.information_ratio / tracking_error`: needs engine.run_quantile's execution (20-session exit, fills); only the spread was approximated

## Rule checks on the run's records (B)

- B1 exits: holdings rows whose exit codes follow from section 8: 452 rows/orders/marks checked; 0 violations.
- B2 trims, caps, U0, u_V, u_C, units_to_sell, status, untested (sections 9.1, 9.2): 452 rows/orders/marks checked; 0 violations.
- B3 buys, slots, budget, empty-slot reasons, decisions.csv counts (sections 7, 10): 54 rows/orders/marks checked; 0 violations.
- B4 fills: date, price, units, kind, cost, sells before buys, cash never negative, forced exits: 157 rows/orders/marks checked; 0 violations.
- B5 NAV close marks reproduced within 1e-6 relative: 1627 rows/orders/marks checked; 0 violations.
- B0 records agree with the replayed book (units, NAV_open, cash_open, rebalance_open): 27 rows/orders/marks checked; 0 violations.
- NAV: 1627 close marks and both open marks rebuilt from fills, dividends and panel closes; largest relative error 6.66e-16 (60-mark sample: 4.44e-16).

## Re-derivation from the frozen database (C)

- 2021-06-01: 767 members; 765 match on every flag; 764 match on every derived field; 3 stocks differ.
  - GAEL (GAEL): ['missing_quarters', 'run', 'ttm4_r', 'ttm4_p', 'ttm4_e', 'A_R', 'A_P', 'M', 'g', 'down0'] -- unit-scale screen (C30a, plumbing not reproduced): pit set aside std 2019-09-30; with those filings treated as not filed my figures agree on every field
  - JINDALSAW (JINDALSAW): ['basis', 'c4_basis', 'missing_quarters', 'ttm_p', 'ttm_ebit', 'fc_pos', 'icr', 'ttm_r', 'ttm_e', 'pe'] -- unit-scale screen (C30a, plumbing not reproduced): pit set aside cons 2021-03-31; with those filings treated as not filed my figures agree on every field
  - PIDILITIND (PIDILITIND): ['basis', 'ttm_p', 'ttm_ebit', 'fc_pos', 'icr', 'ttm_r', 'ttm4_r', 'ttm4_p', 'ttm_e', 'ttm4_e'] -- unit-scale screen (C30a, plumbing not reproduced): pit set aside cons 2019-09-30; with those filings treated as not filed my figures agree on every field
- 2022-06-01: 910 members; 905 match on every flag; 900 match on every derived field; 10 stocks differ.
  - CAPLIPOINT (CAPLIPOINT): ['missing_quarters', 'run', 'ttm4_r', 'ttm4_p', 'ttm4_e', 'A_R', 'A_P', 'M', 'g', 'PEG'] -- unit-scale screen (C30a, plumbing not reproduced): pit set aside std 2020-06-30, cons 2020-06-30; with those filings treated as not filed my figures agree on every field; revision rule (plumbing): a quarter in the window has more than one filing before D
  - GAEL (GAEL): ['run'] -- unit-scale screen (C30a, plumbing not reproduced): pit set aside std 2019-09-30; with those filings treated as not filed my figures agree on every field
  - GRAPHITE (GRAPHITE): ['basis', 'c4_basis', 'L', 'run', 'ttm_p', 'ttm_ebit', 'fc_pos', 'icr', 'ttm_r', 'ttm4_r'] -- unit-scale screen (C30a, plumbing not reproduced): pit set aside std 2022-03-31; with those filings treated as not filed my figures agree on every field
  - HCC (HCC): ['basis', 'run', 'ttm_p', 'ttm_ebit', 'fc_pos', 'icr', 'ttm_r', 'ttm4_r', 'ttm4_p', 'ttm_e'] -- unit-scale screen (C30a, plumbing not reproduced): pit set aside cons 2020-09-30; with those filings treated as not filed my figures agree on every field
  - ICIL (ICIL): ['basis', 'c4_basis', 'ttm_p', 'ttm_ebit', 'fc_pos', 'icr', 'ttm_r', 'ttm4_r', 'ttm4_p', 'ttm_e'] -- unit-scale screen (C30a, plumbing not reproduced): pit set aside cons 2021-09-30, cons 2021-12-31; with those filings treated as not filed my figures agree on every field
  - IRCON (IRCON): ['L', 'run', 'ttm_p', 'ttm_ebit', 'fc_pos', 'icr', 'ttm_r', 'ttm4_r', 'ttm4_p', 'ttm_e'] -- unit-scale screen (C30a, plumbing not reproduced): pit set aside std 2022-03-31, cons 2022-03-31; with those filings treated as not filed my figures agree on every field
  - JINDALSAW (JINDALSAW): ['basis', 'ttm_p', 'ttm_ebit', 'fc_pos', 'icr', 'ttm_r', 'ttm4_r', 'ttm4_p', 'ttm_e', 'ttm4_e'] -- unit-scale screen (C30a, plumbing not reproduced): pit set aside cons 2021-03-31; with those filings treated as not filed my figures agree on every field
  - PIDILITIND (PIDILITIND): ['run'] -- unit-scale screen (C30a, plumbing not reproduced): pit set aside cons 2019-09-30; with those filings treated as not filed my figures agree on every field
  - SHANTIGEAR (SHANTIGEAR): ['L', 'run', 'ttm_p', 'ttm_ebit', 'ttm_r', 'ttm4_r', 'ttm4_p', 'ttm_e', 'ttm4_e', 'A_R'] -- unit-scale screen (C30a, plumbing not reproduced): pit set aside std 2022-03-31; with those filings treated as not filed my figures agree on every field
  - UBL (UBL): ['L', 'run', 'ttm_p', 'ttm_ebit', 'fc_pos', 'icr', 'ttm_r', 'ttm4_r', 'ttm4_p', 'ttm_e'] -- unit-scale screen (C30a, plumbing not reproduced): pit set aside std 2022-03-31, cons 2022-03-31; with those filings treated as not filed my figures agree on every field
- 2023-08-16: 1032 members; 1025 match on every flag; 1020 match on every derived field; 12 stocks differ.
  - ADANIENSOL (ADANITRANS): ['basis', 'missing_quarters', 'run', 'ttm_p', 'ttm_ebit', 'fc_pos', 'icr', 'equity', 'debt', 'de'] -- unit-scale screen (C30a, plumbing not reproduced): pit set aside std 2022-06-30; with those filings treated as not filed my figures agree on every field
  - ARROWGREEN (ARROWGREEN): ['run'] -- unit-scale screen (C30a, plumbing not reproduced): pit set aside cons 2019-09-30; with those filings treated as not filed my figures agree on every field
  - CAPLIPOINT (CAPLIPOINT): ['run'] -- unit-scale screen (C30a, plumbing not reproduced): pit set aside std 2020-06-30, cons 2020-06-30; with those filings treated as not filed my figures agree on every field
  - GAEL (GAEL): ['run'] -- unit-scale screen (C30a, plumbing not reproduced): pit set aside std 2019-09-30; with those filings treated as not filed my figures agree on every field
  - GRAPHITE (GRAPHITE): ['basis', 'missing_quarters', 'run', 'ttm_p', 'ttm_ebit', 'fc_pos', 'icr', 'equity', 'debt', 'de'] -- unit-scale screen (C30a, plumbing not reproduced): pit set aside std 2022-03-31; with those filings treated as not filed my figures agree on every field
  - ICIL (ICIL): ['basis', 'c4_basis', 'ttm_p', 'ttm_ebit', 'fc_pos', 'icr', 'equity', 'debt', 'de', 'ttm_r'] -- unit-scale screen (C30a, plumbing not reproduced): pit set aside cons 2021-09-30, cons 2021-12-31, cons 2022-09-30; with those filings treated as not filed my figures agree on every field
  - IRCON (IRCON): ['missing_quarters', 'run', 'ttm4_r', 'ttm4_p', 'ttm4_e', 'A_R', 'A_P', 'M', 'g', 'PEG'] -- unit-scale screen (C30a, plumbing not reproduced): pit set aside std 2022-03-31, cons 2022-03-31; with those filings treated as not filed my figures agree on every field
  - LODHA (LODHA): ['basis', 'missing_quarters', 'run', 'ttm_p', 'ttm_ebit', 'fc_pos', 'icr', 'equity', 'debt', 'de'] -- unit-scale screen (C30a, plumbing not reproduced): pit set aside std 2022-03-31; with those filings treated as not filed my figures agree on every field
  - PIDILITIND (PIDILITIND): ['run'] -- unit-scale screen (C30a, plumbing not reproduced): pit set aside cons 2019-09-30; with those filings treated as not filed my figures agree on every field
  - SFL (SFL): ['missing_quarters', 'run', 'down1'] -- unit-scale screen (C30a, plumbing not reproduced): pit set aside std 2020-06-30, std 2020-09-30, std 2020-12-31, std 2021-06-30, cons 2020-06-30, cons 2020-09-30, cons 2020-12-31, cons 2021-06-30; with those filings treated as not filed my figures agree on every field
  - SHANTIGEAR (SHANTIGEAR): ['missing_quarters', 'run', 'ttm4_r', 'ttm4_p', 'ttm4_e', 'A_R', 'A_P', 'M', 'g', 'PEG'] -- unit-scale screen (C30a, plumbing not reproduced): pit set aside std 2022-03-31; with those filings treated as not filed my figures agree on every field
  - UBL (UBL): ['basis', 'missing_quarters', 'run', 'ttm_p', 'ttm_ebit', 'icr', 'equity', 'ttm_r', 'ttm4_r', 'ttm4_p'] -- unit-scale screen (C30a, plumbing not reproduced): pit set aside std 2022-03-31, cons 2022-03-31; with those filings treated as not filed my figures agree on every field

## Supporting detail

- capacity (eqbe): v3 p95 0.020316, v1 p95 scaled 0.010894 (unscaled 0.013322); v3 passes: False.
- capacity (eqbe_plus_bz): v3 p95 0.020316, v1 p95 scaled 0.010894 (unscaled 0.013322); v3 passes: False.
- quintile spread (approximation, equal-weight fifths of the section-6 order, no costs): top 0.3560, bottom 0.1356, spread 0.2203; the verdict's engine-based figures are 0.3560, 0.1356, 0.2203. Sign agrees: True.
- earnings/re-rating: TTM_P and market cap came from eligibility.csv except for the (review, stock) pairs listed in the JSON (`A5_recomputed_pairs`); intervals touching them: {'v3': ['2026-09-18'], 'v1': ['2020-06-01', '2021-11-15', '2022-08-16', '2023-06-01', '2024-08-16', '2026-09-18']}. Disagreements between my TTM_P and eligibility.csv's for any pair used: 0.
- share of held slots unclassified: 6 of 470 slots held after each review's decisions (kept + trimmed + new rows of holdings.csv (status != exited)).
- fills: 157 fill rows in fills.csv, 156 orders derived from the rules plus the forced exits; cancelled orders: 0; buys scaled pro rata: 0; lowest cash balance -0.000000.
- ranking rule on the recorded measures, 27 reviews: 189 sorted-on columns x reviews equal exactly, 0 differ; reported percentiles within 1.11e-16.
- 2021-06-01 ranking (i) my pipeline, no screen, my eligible set: 270 of 270 stocks equal exactly through the rank chain; measures equal for 270.
- 2021-06-01 ranking (ii) my measures, no screen, on the recorded eligible set: 270 of 270 stocks equal exactly through the rank chain; measures equal for 270.
- 2021-06-01 ranking (iii) my measures with pit's set-aside list applied, on the recorded eligible set: 270 of 270 stocks equal exactly through the rank chain; measures equal for 270.
- 2022-06-01 ranking (i) my pipeline, no screen, my eligible set: 0 of 497 stocks equal exactly through the rank chain; measures equal for 495.
- 2022-06-01 ranking (ii) my measures, no screen, on the recorded eligible set: 1 of 500 stocks equal exactly through the rank chain; measures equal for 495.
- 2022-06-01 ranking (iii) my measures with pit's set-aside list applied, on the recorded eligible set: 500 of 500 stocks equal exactly through the rank chain; measures equal for 500.
- 2023-08-16 ranking (i) my pipeline, no screen, my eligible set: 0 of 357 stocks equal exactly through the rank chain; measures equal for 357.
- 2023-08-16 ranking (ii) my measures, no screen, on the recorded eligible set: 357 of 357 stocks equal exactly through the rank chain; measures equal for 357.
- 2023-08-16 ranking (iii) my measures with pit's set-aside list applied, on the recorded eligible set: 357 of 357 stocks equal exactly through the rank chain; measures equal for 357.
