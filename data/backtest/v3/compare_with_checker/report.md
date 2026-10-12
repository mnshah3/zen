# Run comparison: production vs checker

- production: `data\backtest\v3`
- checker: `data\backtest\v3_dev_check\full`
- tolerance: |a-b| <= 1e-09 x max(|a|,|b|) + 1e-12; nav.csv columns divided by their first value

**0 differences, 0 of class value, blank_vs_value, only_in_\* or missing_column**

## Notes

- eligibility.csv checker renamed: b_star -> basis, N -> shares, N_src -> shares_fix, fc_plus -> fc_pos, F0 -> f0, F1 -> f1, F2 -> f2, F3 -> f3, F4 -> f4, F5 -> f5, F6 -> f6, N_ref -> shares_ref, N_ref_over_N -> shares_ref_over_n, bs_equity -> equity, bs_debt -> debt
- eligibility.csv: working columns only one engine writes, not compared: A_P, A_R, M, PEG, bs_usable, close_adj, close_factor, g, held_at_open, in_universe, latest_period_end_c4, sector_key, ttm4_e, ttm_e, window_complete
- ranks.csv checker renamed: sector -> sector_key
- ranks.csv: working columns only one engine writes, not compared: pe, r_M
- decisions.csv checker renamed: cash_after_step0 -> cash_open, B_left -> budget_left, v1_after_r1 -> r1_ordinary_equity_recent_trade, v1_after_r2 -> r2_turnover_20L, v1_after_r3 -> r3_200_sessions_365d, v1_after_r4 -> r4_not_lender, v1_after_r5 -> r5_four_quarters_fresh, v1_after_r6 -> r6_ttm_profit_ebitda_positive, v1_after_r7 -> r7_mcap_computable, v3_after_F1 -> n_f1, v3_after_F2 -> n_f2, v3_after_F3 -> n_f3, v3_after_F4 -> n_f4, v3_after_F5 -> n_f5, v3_after_F6 -> n_f6, holdings_at_open -> n_held_open, kept -> n_kept, exits_E1 -> n_exit_E1, exits_E2 -> n_exit_E2, exits_E3 -> n_exit_E3, exits_E4 -> n_exit_E4, exits_E5 -> n_exit_E5, v_sales -> n_exit_V, trims_valuation -> n_trim_valuation, trims_cap -> n_trim_cap, buys_placed -> n_buys, untested_holdings -> n_untested, untested_E1 -> n_untested_E1, untested_E2 -> n_untested_E2, untested_E3 -> n_untested_E3, untested_E4 -> n_untested_E4, untested_trim -> n_untested_trim, holdings_outside_v1_universe -> n_held_outside_universe
- decisions.csv: working columns only one engine writes, not compared: archive_symbols, cash_share_after_plan, n_held_unclassified, v1_universe
- holdings.csv checker renamed: sector -> sector_key, in_v1_universe -> in_universe
- holdings.csv: working columns only one engine writes, not compared: buy_value, eligible
- fills.csv checker renamed: units_adjusted -> units, price_adjusted -> price, status -> event
- fills.csv: `event` compared by meaning (fill / cancelled); not compared: exit_code, reason
- fills.csv: working columns only one engine writes, not compared: scheduled_date

## By file

| file | rows a | rows b | joined | differences | by class |
|---|---|---|---|---|---|
| universe.csv | 26329 | 26329 | 26329 | 0 | none |
| eligibility.csv | 26365 | 26365 | 26365 | 0 | none |
| ranks.csv | 11501 | 11501 | 11501 | 0 | none |
| decisions.csv | 27 | 27 | 27 | 0 | none |
| holdings.csv | 533 | 533 | 533 | 0 | none |
| fills.csv | 157 | 157 | 157 | 0 | none |
| nav.csv | 1629 | 1629 | 1629 | 0 | none |

- eligibility.csv: columns only in production: ['in_universe', 'held_at_open', 'close_factor', 'close_adj', 'sector_key', 'ttm_e', 'ttm4_e', 'A_R', 'A_P', 'M', 'g', 'PEG', 'latest_period_end_c4']; only in checker: ['window_complete', 'bs_usable'] (not compared)
- ranks.csv: columns only in production: ['pe']; only in checker: ['r_M'] (not compared)
- decisions.csv: columns only in production: ['archive_symbols', 'n_held_unclassified']; only in checker: ['v1_universe', 'cash_share_after_plan'] (not compared)
- holdings.csv: columns only in production: ['buy_value', 'eligible']; only in checker: [] (not compared)
- fills.csv: columns only in production: []; only in checker: ['scheduled_date', 'exit_code'] (not compared)
