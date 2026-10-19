"""zen/data/amfi_nav.py: what is kept from AMFI's NAV report."""

from __future__ import annotations

from datetime import date

import duckdb
import pandas as pd

from zen.data import amfi_nav as a

REPORT = """Scheme Code;NAV Name;Plan;Option;ISIN Div Payout/ISIN Growth;ISIN Div Reinvestment;Net Asset Value;Date

Open Ended Schemes ( Equity Scheme - Flexi Cap Fund )

Parag Parikh Mutual Fund
122639;Parag Parikh Flexi Cap Fund - Direct Plan - Growth;Direct Plan;Growth;INF879O01027;;24.4894;29-Jun-2018
122640;Parag Parikh Flexi Cap Fund - Regular Plan - Growth;Regular Plan;Growth;INF879O01019;;23.8433;29-Jun-2018
122641;Parag Parikh Flexi Cap Fund - Direct Plan - IDCW;Direct Plan;IDCW Option;INF879O01035;;20.10;29-Jun-2018

Open Ended Schemes ( Equity Schemes - ELSS- Tax Saver Fund )

Sahara Mutual Fund
120373;SAHARA TAX GAIN FUND- GROWTH - Direct;;;INF515L01AJ6;;66.0918;29-Jun-2018
120374;SAHARA TAX GAIN FUND-DIVIDEND - Direct;;;INF515L01AH0;INF515L01AI8;22.5511;29-Jun-2018
120375;SAHARA TAX GAIN FUND- GROWTH OPTION;;;INF515L01494;;N.A.;29-Jun-2018

Open Ended Schemes ( Debt Scheme - Liquid Fund )

Parag Parikh Mutual Fund
143269;Parag Parikh Liquid Fund- Direct Plan- Growth;Direct Plan;Growth;INF879O01068;;1008.2762;29-Jun-2018

Open Ended Schemes ( Other Scheme - Index Funds )

UTI Mutual Fund
120716;UTI Nifty 50 Index Fund - Growth Option- Direct;Direct Plan;Growth;INF789F01XA0;;71.20;29-Jun-2018
"""


def test_only_equity_and_index_growth_plans_are_kept_with_their_plan():
    df = a.parse(REPORT)
    assert list(df["scheme_code"]) == ["122639", "122640", "120373", "120716"]   # no IDCW, dividend, N.A. or debt
    assert list(df["plan"]) == ["Direct", "Regular", "Direct", "Direct"]          # an empty Plan: the name decides
    assert list(df["category"]) == ["Flexi Cap Fund", "Flexi Cap Fund", "ELSS", "Index Funds"]
    assert df["amc"].iloc[2] == "Sahara Mutual Fund" and df["date"].iloc[0] == date(2018, 6, 29)


def test_sector_funds_filed_three_ways_are_one_category():
    assert {a.category_of(x) for x in ("Equity Scheme - Sectoral/ Thematic", "Equity Schemes - Sectoral Fund",
                                       "Equity Schemes - Thematic Fund")} == {"Sectoral/Thematic"}


def test_store_and_rebuild_keep_one_row_a_scheme_a_day(tmp_path):
    df = a.parse(REPORT)
    a.store(df, tmp_path)
    a.store(df.assign(nav=df["nav"] * 2), tmp_path)                             # a re-read wins
    con = duckdb.connect()
    assert a.rebuild(con, tmp_path) == 4
    assert con.execute("SELECT nav FROM mf_nav WHERE scheme_code = '122639'").fetchone()[0] == 24.4894 * 2
