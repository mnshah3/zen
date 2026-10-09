"""Lenders' results from banking-format filings (zen.data.bank_results)."""

from __future__ import annotations

import duckdb
import pandas as pd

from zen.data import bank_results, financials

DOC = b"""<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:xbrldi="http://xbrl.org/2006/xbrldi"
            xmlns:in-capmkt="http://www.sebi.gov.in/xbrl/in-capmkt">
  <xbrli:context id="Q"><xbrli:entity><xbrli:identifier scheme="s">X</xbrli:identifier></xbrli:entity>
    <xbrli:period><xbrli:startDate>2026-04-01</xbrli:startDate><xbrli:endDate>2026-06-30</xbrli:endDate></xbrli:period></xbrli:context>
  <xbrli:context id="Y"><xbrli:entity><xbrli:identifier scheme="s">X</xbrli:identifier></xbrli:entity>
    <xbrli:period><xbrli:startDate>2025-04-01</xbrli:startDate><xbrli:endDate>2026-03-31</xbrli:endDate></xbrli:period></xbrli:context>
  <xbrli:context id="SEG"><xbrli:entity><xbrli:identifier scheme="s">X</xbrli:identifier>
    <xbrli:segment><xbrldi:explicitMember dimension="in-capmkt:SegmentsAxis">in-capmkt:TreasuryMember</xbrldi:explicitMember></xbrli:segment></xbrli:entity>
    <xbrli:period><xbrli:startDate>2026-04-01</xbrli:startDate><xbrli:endDate>2026-06-30</xbrli:endDate></xbrli:period></xbrli:context>
  <in-capmkt:InterestEarned contextRef="SEG" unitRef="INR" decimals="-5">7</in-capmkt:InterestEarned>
  <in-capmkt:InterestEarned contextRef="Q" unitRef="INR" decimals="-5">1000</in-capmkt:InterestEarned>
  <in-capmkt:InterestEarned contextRef="Y" unitRef="INR" decimals="-5">3900</in-capmkt:InterestEarned>
  <in-capmkt:InterestExpended contextRef="Q" unitRef="INR" decimals="-5">600</in-capmkt:InterestExpended>
  <in-capmkt:Income contextRef="Q" unitRef="INR" decimals="-5">1,200</in-capmkt:Income>
  <in-capmkt:ProfitLossForThePeriod contextRef="Q" unitRef="INR" decimals="-5">150</in-capmkt:ProfitLossForThePeriod>
  <in-capmkt:BasicEarningsPerShareBeforeExtraordinaryItems contextRef="Q" unitRef="INRPerShare" decimals="2">1.5</in-capmkt:BasicEarningsPerShareBeforeExtraordinaryItems>
  <in-capmkt:PercentageOfGrossNpa contextRef="Q" unitRef="pure" decimals="4">0.0117</in-capmkt:PercentageOfGrossNpa>
</xbrli:xbrl>"""


def test_the_quarter_not_the_year_and_not_a_segment():
    r = bank_results.parse(DOC)
    assert r["interest_earned"] == 1000 and r["interest_expended"] == 600 and r["total_income"] == 1200
    assert r["profit_reported"] == 150 and r["eps_basic"] == 1.5 and r["gross_npa_pct"] == 0.0117
    assert r["quarter_span_days"] == 90


def test_the_ind_as_map_is_unchanged_by_the_option():
    assert financials.parse_xbrl(DOC) == financials.parse_xbrl(DOC, tags=financials.TAGS)
    assert "interest_earned" not in financials.parse_xbrl(DOC)


class _Resp:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        return None


class _Session:
    def __init__(self, content):
        self.content = content

    def get(self, url, timeout=None):
        return _Resp(self.content)


def _docs():
    return pd.DataFrame([{"symbol": "XBANK", "company": "X Bank", "period_end": pd.Timestamp("2026-06-30"),
                          "broadcast_dt": pd.Timestamp("2026-07-18 16:00"), "consolidated": False,
                          "xbrl_url": "https://example.invalid/test-bank-results-doc.xml"}])


def test_an_error_page_is_a_failed_download_not_an_empty_filing(tmp_path, monkeypatch):
    monkeypatch.setattr(bank_results.xbrl_cache, "get", lambda url, root=None: None)
    monkeypatch.setattr(bank_results.xbrl_cache, "put", lambda url, content, root=None: False)
    rows, failed = bank_results.fetch(_docs(), _Session(b"<html>Access denied</html>"), pause=0)
    assert failed == 1 and rows.empty


def test_rows_round_trip_into_the_table(tmp_path, monkeypatch):
    monkeypatch.setattr(bank_results.xbrl_cache, "get", lambda url, root=None: None)
    monkeypatch.setattr(bank_results.xbrl_cache, "put", lambda url, content, root=None: True)
    rows, failed = bank_results.fetch(_docs(), _Session(DOC), pause=0)
    assert failed == 0 and len(rows) == 1 and bool(rows.iloc[0]["has_figures"])
    bank_results.write(rows, tmp_path)
    bank_results.write(rows, tmp_path)                     # written twice, stored once
    con = duckdb.connect()
    assert bank_results.rebuild(con, tmp_path) == 1
    got = con.execute("SELECT symbol, interest_earned, profit_reported, has_figures FROM bank_results").fetchone()
    assert got == ("XBANK", 1000.0, 150.0, True)
    assert bank_results.stored_urls(tmp_path) == {"https://example.invalid/test-bank-results-doc.xml"}
