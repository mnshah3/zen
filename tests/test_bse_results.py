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
         "Filing_Date_Time": None, "scripcode": 500488},
        {"Quarter": "Standalone-Sep-16;SQ2016-2017;;D", "qtr": None, "Fld_QuarterName": "Quarter",
         "Filing_Date_Time": None, "scripcode": 500488}])                  # no quarter number: skipped
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


# ------------------------------------------------------------------ the XBRL fallback (20 Nov 2026)

HEAD = ('<?xml version="1.0" encoding="UTF-8"?>'
        '<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" '
        'xmlns:in-bse-fin="http://www.bseindia.com/xbrl/fin/2020-03-31/in-bse-fin">')


def ctx(cid, start, end):
    return (f'<xbrli:context id="{cid}"><xbrli:entity><xbrli:identifier scheme="http://www.bseindia.com">X'
            f'</xbrli:identifier></xbrli:entity><xbrli:period><xbrli:startDate>{start}</xbrli:startDate>'
            f'<xbrli:endDate>{end}</xbrli:endDate></xbrli:period></xbrli:context>')


def fact(tag, ref, value):
    return f'<in-bse-fin:{tag} contextRef="{ref}" unitRef="INR">{value}</in-bse-fin:{tag}>'


def kama_sep_2026(end="2026-09-30"):
    """Kama Holdings' standalone Sep 2026 filing, its lines as filed (Rs; the NBFC integrated layout)."""
    q, h = ("OneD", "2026-07-01", end), ("FourD", "2026-04-01", end)
    lines = {"RevenueFromOperations": 744244000, "OtherIncome": 0, "Income": 744244000, "FinanceCosts": 0,
             "DepreciationDepletionAndAmortisationExpense": 132000, "Expenses": 6133000,
             "ProfitBeforeExceptionalItemsAndTax": 738111000, "ExceptionalItemsBeforeTax": 8022773000,
             "ProfitBeforeTax": 8760884000, "TaxExpense": 5248000, "ProfitLossForPeriodFromContinuingOperations": 8755636000,
             "ProfitLossForPeriod": 8755636000, "BasicEarningsLossPerShareFromContinuingOperations": 272.84,
             "PaidUpValueOfEquityShareCapital": 320906000, "FaceValueOfEquityShareCapital": 10}
    body = ctx(*q) + ctx(*h) + fact("DateOfEndOfReportingPeriod", "OneD", end)
    body += "".join(fact(k, "OneD", v) for k, v in lines.items())
    body += fact("RevenueFromOperations", "FourD", 1500000000)                       # the half year beside it
    return (HEAD + body + "</xbrli:xbrl>").encode()


def test_a_finance_companys_quarter_is_read_from_its_xbrl_in_the_services_columns_and_signs():
    d = br.from_xbrl(kama_sep_2026(), "Sep-26")
    assert d["revenue"] == 744244000 and d["profit"] == 8755636000 and d["eps_basic"] == 272.84    # the quarter
    assert d["expenditure"] == -6133000 and d["depreciation"] == -132000                             # costs negative
    assert d["pbt"] == 8760884000 and d["pbt_before_exceptional"] == 738111000 and d["exceptional"] == 8022773000
    assert d["period_end"] == datetime(2026, 9, 30) and d["period_start"] == datetime(2026, 7, 1)
    assert d["shares"] == pytest.approx(32090600) and d["source"] == "xbrl"
    assert br.has_figures(d)


def test_an_xbrl_document_for_another_period_than_the_archive_says_is_refused():
    assert br.from_xbrl(kama_sep_2026(), "Jun-26") == {}
    half_only = (HEAD + ctx("FourD", "2026-04-01", "2026-09-30") + fact("DateOfEndOfReportingPeriod", "FourD", "2026-09-30")
                 + fact("RevenueFromOperations", "FourD", 1500000000) + "</xbrli:xbrl>").encode()
    assert br.from_xbrl(half_only, "Sep-26") == {}                                    # a half year is not a quarter


def test_bses_misnamed_folder_for_a_finance_companys_integrated_filing_is_put_right():
    assert br.xbrl_address("/XBRLFILES/NBFCUploadDocument/Integrated_Finance_NBFC_532468_5112026175430.xml") == \
        "https://www.bseindia.com/XBRLFILES/IFNBFCUploadDocument/Integrated_Finance_NBFC_532468_5112026175430.xml"
    assert br.xbrl_address("/XBRLFILES/NBFCUploadDocument/NBFC_532468_1422025123557.xml") == \
        "https://www.bseindia.com/XBRLFILES/NBFCUploadDocument/NBFC_532468_1422025123557.xml"    # older ones are where linked
    assert br.xbrl_address(None) is None and br.xbrl_address(" ") is None


def test_the_archive_carries_each_filings_standalone_xbrl_link():
    a = br.parse_archive([{"Quarter": "Standalone-Sep-26;SQ2026-2027;131.00;D", "qtr": 131.0, "Fld_QuarterName": "Quarter",
                           "Filing_Date_Time": "2026-11-05T17:54:31.64", "scripcode": 532468,
                           "stand_xbrl_link": "/XBRLFILES/NBFCUploadDocument/Integrated_Finance_NBFC_532468_5112026175430.xml"}])
    assert a["xbrl"].iloc[0].endswith("_5112026175430.xml")


def test_profit_before_exceptional_items_is_read_from_the_service():
    d = br.parse_detail(rows([("Profit after Interest but before Exceptional Items", "6104.7000"),
                              ("Exceptional Items", "-12.5000"),
                              ("Profit (+)/ Loss (-) from Ordinary Activities before Tax", "6092.2000")]))
    assert d["pbt_before_exceptional"] == pytest.approx(6104.7e6) and d["exceptional"] == pytest.approx(-12.5e6)


class _Answer:
    def __init__(self, payload=None, content=b"", status=200):
        self.payload, self.content, self.status_code = payload, content, status

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class _BSE:
    """BSE answering as it did for Kama Holdings on 20 Nov 2026: the archive lists the quarter, the
    detailed-results service has three blank lines for it, the XBRL is served from the right folder."""
    def __init__(self):
        self.asked = []

    def get(self, url, params=None, timeout=None):
        self.asked.append(url)
        if "Result_Arch_ng" in url:
            return _Answer({"Table": [{"Quarter": "Standalone-Sep-26;SQ2026-2027;131.00;D", "qtr": 131.0,
                                       "Fld_QuarterName": "Quarter", "Filing_Date_Time": "2026-11-05T17:54:31.64",
                                       "scripcode": 532468,
                                       "stand_xbrl_link": "/XBRLFILES/NBFCUploadDocument/Integrated_Finance_NBFC_532468_5112026175430.xml"}]})
        if "Corp_detailedResult" in url:
            return _Answer({"table1": [{"fld_desc": "Type", "Value": ""}, {"fld_desc": "Date Begin", "Value": ""},
                                       {"fld_desc": "Date End", "Value": ""}]})
        if "/IFNBFCUploadDocument/" in url:
            return _Answer(content=kama_sep_2026())
        return _Answer(status=404)


def test_a_blank_stored_quarter_is_asked_again_and_filled_from_its_xbrl(tmp_path, monkeypatch):
    from zen.data import xbrl_cache
    monkeypatch.setattr(xbrl_cache, "CACHE_DIR", tmp_path / "xbrl")
    br.save(532468, [{"scrip_cd": 532468, "qtr": 131.0, "label": "Sep-26", "kind": "Quarter", "filed_dt": None}], tmp_path)
    s = _BSE()
    assert br.fetch_company(s, 532468, lambda a: True, pause=0, root=tmp_path) == 1
    got = br.stored(532468, tmp_path)
    assert len(got) == 1 and got["revenue"].iloc[0] == 744244000 and got["source"].iloc[0] == "xbrl"
    assert any("/IFNBFCUploadDocument/" in u for u in s.asked)
    assert br.fetch_company(s, 532468, lambda a: True, pause=0, root=tmp_path) == 0     # stored with figures: not asked again


def test_the_quarter_due_counts_45_days_and_60_for_march():
    from jobs.update_bse_results import latest_due
    import pandas as pd
    assert str(latest_due(pd.Timestamp("2026-11-23"))) == "2026Q3"
    assert str(latest_due(pd.Timestamp("2026-11-14"))) == "2026Q2"                  # Sep quarter due by 14 Nov
    assert str(latest_due(pd.Timestamp("2026-05-31"))) == "2026Q1"                  # March: 60 days, to 30 May
    assert str(latest_due(pd.Timestamp("2026-05-30"))) == "2025Q4"


def test_gap_targets_reach_every_company_still_trading_and_past_nses_last_quarter(monkeypatch):
    import pandas as pd
    from jobs import update_bse_results as u
    con = duckdb.connect()
    days = pd.bdate_range("2025-01-01", "2026-11-20")
    px = pd.concat([pd.DataFrame({"symbol": s, "isin_code": i, "date": days, "series": "EQ", "turnover": t})
                    for s, i, t in [("STALE", "INE1", 5e7), ("THIN", "INE2", 1e4), ("NEWNAME", "INE3", 5e7)]]
                   + [pd.DataFrame({"symbol": "OLDNAME", "isin_code": "INE3", "date": days[:10], "series": "EQ", "turnover": 5e7})])
    con.register("p", px)
    con.execute("CREATE TABLE prices AS SELECT * FROM p")
    q = [("STALE", "2025-03-31"), ("STALE", "2025-06-30"), ("NEWNAME", "2025-03-31"), ("NEWNAME", "2026-09-30")]
    con.execute("CREATE TABLE financials (symbol VARCHAR, period_end DATE, revenue DOUBLE, total_income DOUBLE, "
                "profit_reported DOUBLE, quarter_span_days INTEGER)")
    con.executemany("INSERT INTO financials VALUES (?, ?, 1, 1, 1, 91)", q)
    monkeypatch.setattr(u, "isin_codes", lambda: pd.DataFrame({"isin": ["INE1", "INE2", "INE3"], "scrip_cd": [1, 2, 3]}))
    t = u.gap_targets(con, pd.Timestamp("2026-11-23"))
    # NSE's filings stop at June 2025 while it still trades: every quarter after, to the one due
    assert {str(p) for p in t[1]} == {"2025-09", "2025-12", "2026-03", "2026-06", "2026-09"}
    assert t[2] is None                                           # BSE-only and thinly traded: read in full now
    assert {str(p) for p in t[3]} == {f"{y}-{m:02d}" for y, m in [(2025, 6), (2025, 9), (2025, 12), (2026, 3), (2026, 6)]}
