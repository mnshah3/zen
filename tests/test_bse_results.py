"""zen/data/bse_results.py: BSE's detailed results, parsed (rows as BSE's service returned them on 19 Oct 2026)."""

from __future__ import annotations

from datetime import datetime

import duckdb
import pytest

from zen.data import bse_results as br


def rows(pairs):
    return [{"fld_desc": d, "Value": v} for d, v in pairs]


ABBOTT_2011 = rows([("Type", "Un-Audited"), ("Date Begin", "01-Apr-11"), ("Date End", "30-Jun-11"),
                    ("Net Sales/Revenue From Operations", "3584.2000"), ("Net Sales", "3514.2000"),
                    ("Expenditure", "-3355.5000"), ("Depreciation & Amortization", "-32.1000"), ("Other Income", "53.1000"),
                    ("Profit (+)/ Loss (-) from Ordinary Activities before Tax", "281.8000"), ("Tax", "-110.8000"),
                    ("Net Profit (+)/ Loss (-) from Ordinary Activities after Tax", "171.0000"), ("Net Profit", "171.0000"),
                    ("Equity Capital", "136.8000"), ("Face Value (in Rs)", "10.0000"),
                    ("Basic EPS after Extraordinary items", "12.5000"), ("Diluted EPS after Extraordinary items", "8.0500")])
ABBOTT_2023 = rows([("Type", "Un-Audited"), ("Date Begin", "01-Oct-23"), ("Date End", "31-Dec-23"),
                    ("Net Sales/Revenue From Operations", "14371.4000"), ("Other Income", "556.1000"),
                    ("Total Income", "14927.5000"), ("Expenditure", "-10705.9000"), ("Finance Costs", "-31.7000"),
                    ("Profit (+)/ Loss (-) from Ordinary Activities before Tax", "4221.6000"), ("Tax", "-1111.8000"),
                    ("Current tax", "-1133.4000"), ("Net Profit", "3109.8000"),
                    ("Basic EPS for continuing operation", "146.3400"),
                    ("Basic for discontinued & continuing operation", "146.3400")])


def test_figures_come_in_rupees_from_bses_million_and_basic_eps_is_kept():
    d = br.parse_detail(ABBOTT_2011)
    assert d["revenue"] == pytest.approx(3584.2e6) and d["profit"] == pytest.approx(171.0e6)
    assert d["pbt"] == pytest.approx(281.8e6) and d["tax"] == pytest.approx(-110.8e6)
    assert d["eps_basic"] == 12.5                                        # not the diluted 8.05 BSE also shows
    assert d["shares"] == pytest.approx(13.68e6)                          # Rs 136.8 million of Rs 10 shares
    assert d["period_start"] == datetime(2011, 4, 1) and d["period_end"] == datetime(2011, 6, 30) and not d["audited"]


def test_the_ind_as_labels_of_later_years():
    d = br.parse_detail(ABBOTT_2023)
    assert d["revenue"] == pytest.approx(14371.4e6) and d["interest"] == pytest.approx(-31.7e6)
    assert d["tax"] == pytest.approx(-1111.8e6)                           # the total, not "Current tax"
    assert d["eps_basic"] == 146.34 and "shares" not in d                  # no equity capital line that quarter


def test_the_archive_keeps_standalone_filings_with_their_filing_time():
    a = br.parse_archive([
        {"Quarter": "Standalone-Jun-16;JQ2016-2017;90.00;D", "qtr": 90.0, "Fld_QuarterName": "Quarter",
         "Filing_Date_Time": "2016-08-27T16:15:27", "scripcode": 500488},
        {"Quarter": "Standalone-Mar-12;MQ2011-2012;73.50;D", "qtr": 73.5, "Fld_QuarterName": "Year",
         "Filing_Date_Time": None, "scripcode": 500488},
        {"Quarter": "Consolidated-Jun-16;JQ2016-2017;90.00;C", "qtr": 90.0, "Fld_QuarterName": "Quarter",
         "Filing_Date_Time": None, "scripcode": 500488}])
    assert list(a["qtr"]) == [90.0, 73.5] and list(a["label"]) == ["Jun-16", "Mar-12"]
    assert str(a["filed_dt"].iloc[0]) == "2016-08-27 16:15:27" and a["filed_dt"].isna().iloc[1]


def test_save_and_rebuild(tmp_path):
    d = br.parse_detail(ABBOTT_2011)
    br.save(500488, [{"scrip_cd": 500488, "qtr": 70.0, "label": "Jun-11", "kind": "Quarter", "filed_dt": None, **d}], tmp_path)
    br.save(500488, [{"scrip_cd": 500488, "qtr": 70.0, "label": "Jun-11", "kind": "Quarter", "filed_dt": None, **d}], tmp_path)
    con = duckdb.connect()
    assert br.rebuild(con, tmp_path) == 1
    assert con.execute("SELECT eps_basic FROM bse_results").fetchone()[0] == 12.5


def test_a_blank_total_eps_shown_as_zero_falls_through_to_continuing_operations():
    d = br.parse_detail(rows([("Net Profit", "4285.2000"), ("Basic EPS for continuing operation", "201.6600"),
                              ("Basic for discontinued & continuing operation", "0.0000")]))    # Abbott June 2026
    assert d["eps_basic"] == 201.66
    loss_free = br.parse_detail(rows([("Net Profit", "0.0000"), ("Basic for discontinued & continuing operation", "0.0000")]))
    assert loss_free["eps_basic"] == 0.0                                  # a true zero, with no profit
