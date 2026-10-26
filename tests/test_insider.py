"""Insider trades (zen.data.insider): both NSE systems read into one table, ESOP exercises left out."""

from datetime import date

import pandas as pd
import pytest

from zen.data import insider

OLD = [
    {"acqMode": "Market Purchase", "acqName": "A PROMOTER", "acqfromDt": "03-Sep-2024", "acqtoDt": "04-Sep-2024",
     "afterAcqSharesPer": "51.20", "befAcqSharesPer": "51.10", "anex": "7(2)", "company": "X Limited",
     "date": "05-Sep-2024 19:06", "exchange": "NSE", "intimDt": "04-Sep-2024", "personCategory": "Promoter Group",
     "secAcq": "16300", "secType": "Equity Shares", "secVal": "59155768", "symbol": "XYZ",
     "tdpTransactionType": "Buy", "xbrl": "https://nsearchives.nseindia.com/corporate/xbrl/IT_1.xml"},
    {"acqMode": "ESOP", "acqName": "AN EMPLOYEE", "acqfromDt": "01-Sep-2024", "acqtoDt": "01-Sep-2024",
     "date": "02-Sep-2024 10:00", "personCategory": "Employees/Designated Employees", "secAcq": "100",
     "secType": "Equity Shares", "secVal": "-", "symbol": "XYZ", "tdpTransactionType": "Buy"},
    {"acqMode": "Market Sale", "acqName": "A DIRECTOR", "acqfromDt": "-", "acqtoDt": "-", "date": "bad date",
     "symbol": "XYZ", "tdpTransactionType": "Sell"},
]

XBRL = """<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:in-bse-co="http://www.bseindia.com/xbrl/co">
  <in-bse-co:Symbol contextRef="OneD">GRANULES</in-bse-co:Symbol>
  <in-bse-co:ISINCode contextRef="OneD">INE101D01020</in-bse-co:ISINCode>
  <in-bse-co:NameOfTheCompany contextRef="OneD">GRANULES INDIA LIMITED</in-bse-co:NameOfTheCompany>
  <in-bse-co:DisclosureUnderRegulation contextRef="OneD">Regulation 7 (2)</in-bse-co:DisclosureUnderRegulation>
  <in-bse-co:NameOfThePerson contextRef="D1">Chigurupati Uma Devi</in-bse-co:NameOfThePerson>
  <in-bse-co:CategoryOfPerson contextRef="D1">Promoter Group</in-bse-co:CategoryOfPerson>
  <in-bse-co:TypeOfInstrument contextRef="D1">Equity Shares</in-bse-co:TypeOfInstrument>
  <in-bse-co:SecuritiesAcquiredOrDisposedNumberOfSecurity contextRef="D1">10000</in-bse-co:SecuritiesAcquiredOrDisposedNumberOfSecurity>
  <in-bse-co:SecuritiesAcquiredOrDisposedValueOfSecurity contextRef="D1">5500000</in-bse-co:SecuritiesAcquiredOrDisposedValueOfSecurity>
  <in-bse-co:SecuritiesAcquiredOrDisposedTransactionType contextRef="D1">Buy</in-bse-co:SecuritiesAcquiredOrDisposedTransactionType>
  <in-bse-co:SecuritiesHeldPriorToAcquisitionOrDisposalPercentageOfShareholding contextRef="D1">0.0910</in-bse-co:SecuritiesHeldPriorToAcquisitionOrDisposalPercentageOfShareholding>
  <in-bse-co:SecuritiesHeldPostAcquistionOrDisposalPercentageOfShareholding contextRef="D1">0.0912</in-bse-co:SecuritiesHeldPostAcquistionOrDisposalPercentageOfShareholding>
  <in-bse-co:DateOfAllotmentAdviceOrAcquisitionOfSharesOrSaleOfSharesSpecifyFromDate contextRef="D1">2026-10-21</in-bse-co:DateOfAllotmentAdviceOrAcquisitionOfSharesOrSaleOfSharesSpecifyFromDate>
  <in-bse-co:DateOfAllotmentAdviceOrAcquisitionOfSharesOrSaleOfSharesSpecifyToDate contextRef="D1">2026-10-21</in-bse-co:DateOfAllotmentAdviceOrAcquisitionOfSharesOrSaleOfSharesSpecifyToDate>
  <in-bse-co:ModeOfAcquisitionOrDisposal contextRef="D1">Market Purchase</in-bse-co:ModeOfAcquisitionOrDisposal>
  <in-bse-co:DateOfIntimationToCompany contextRef="D1">2026-10-22</in-bse-co:DateOfIntimationToCompany>
  <in-bse-co:ExchangeOnWhichTheTradeWasExecuted contextRef="D1">NSE</in-bse-co:ExchangeOnWhichTheTradeWasExecuted>
  <in-bse-co:NameOfThePerson contextRef="D2">An Employee</in-bse-co:NameOfThePerson>
  <in-bse-co:CategoryOfPerson contextRef="D2">Employees/Designated Employees</in-bse-co:CategoryOfPerson>
  <in-bse-co:ModeOfAcquisitionOrDisposal contextRef="D2">ESOP</in-bse-co:ModeOfAcquisitionOrDisposal>
</xbrli:xbrl>"""


def test_the_old_system_one_company_at_a_time():
    df = insider.parse_old(OLD)
    assert len(df) == 1                                      # the ESOP exercise and the undated row are left out
    r = df.iloc[0]
    assert r["broadcast_dt"] == pd.Timestamp("2024-09-05 19:06") and r["category"] == "Promoter Group"
    assert (r["txn"], r["mode"], r["qty"], r["value"]) == ("Buy", "Market Purchase", 16300.0, 59155768.0)
    assert (r["pct_before"], r["pct_after"]) == (51.1, 51.2)                       # percent, as NSE states it
    assert (r["trade_from"], r["trade_to"], r["intimated"]) == (date(2024, 9, 3), date(2024, 9, 4), date(2024, 9, 4))
    assert r["source"] == "pit" and list(df.columns) == insider.COLUMNS


def test_the_new_system_filing_xbrl():
    df = insider.parse_xbrl(XBRL, pd.Timestamp("2026-10-23 21:18:34"), "https://x/IT.xml")
    assert len(df) == 1                                      # the employee's ESOP is left out
    r = df.iloc[0]
    assert (r["symbol"], r["isin"], r["person"], r["category"]) == ("GRANULES", "INE101D01020", "Chigurupati Uma Devi", "Promoter Group")
    assert (r["txn"], r["mode"], r["qty"], r["value"], r["exchange"]) == ("Buy", "Market Purchase", 10000.0, 5500000.0, "NSE")
    assert r["pct_before"] == pytest.approx(9.10) and r["pct_after"] == pytest.approx(9.12)   # fractions made percent
    assert r["trade_from"] == date(2026, 10, 21) and r["broadcast_dt"] == pd.Timestamp("2026-10-23 21:18:34")
    assert r["source"] == "pit-gg" and r["url"] == "https://x/IT.xml"


def test_stored_once_a_year_a_file(tmp_path):
    a = insider.parse_old(OLD)
    insider.write(a, tmp_path)
    insider.write(a, tmp_path)                               # the same disclosure read twice is stored once
    b = insider.parse_xbrl(XBRL, pd.Timestamp("2026-10-23 21:18:34"), "https://x/IT.xml")
    insider.write(b, tmp_path)
    assert sorted(p.name for p in tmp_path.glob("*.parquet")) == ["2024.parquet", "2026.parquet"]
    assert len(pd.read_parquet(tmp_path / "2024.parquet")) == 1
    assert insider.stored_urls(tmp_path) == {"https://nsearchives.nseindia.com/corporate/xbrl/IT_1.xml", "https://x/IT.xml"}


class _Resp:
    def __init__(self, text, ok=True):
        self.text, self.ok = text, ok

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError("503")


class _Session:
    def __init__(self, pages):
        self.pages, self.asked = pages, []

    def get(self, url, timeout=None):
        self.asked.append(url)
        return self.pages[url]


def test_each_filing_is_read_once(tmp_path):
    s = _Session({"u/full.xml": _Resp(XBRL), "u/esop.xml": _Resp(XBRL.split('<in-bse-co:NameOfThePerson contextRef="D1">')[0]
                                                                  + XBRL.split("</in-bse-co:ExchangeOnWhichTheTradeWasExecuted>")[1]),
                  "u/down.xml": _Resp("", ok=False)})
    filings = [{"xmlFileName": u, "broadcastDateTime": "23-Oct-2026 21:18:34"} for u in ("u/full.xml", "u/esop.xml", "u/down.xml")]
    df = insider.fetch_new(s, filings, insider.stored_urls(tmp_path), pause=0, out_dir=tmp_path)
    insider.write(df, tmp_path)
    assert len(df) == 1 and df["url"].tolist() == ["u/full.xml"]
    assert insider.stored_urls(tmp_path) == {"u/full.xml", "u/esop.xml"}       # the failed one is tried again
    s.asked.clear()
    insider.fetch_new(s, filings, insider.stored_urls(tmp_path), pause=0, out_dir=tmp_path)
    assert s.asked == ["u/down.xml"]


def test_a_fresh_session_after_failures_in_a_row(tmp_path):
    dead = _Session({f"u/{i}.xml": _Resp("", ok=False) for i in range(5)})
    live = _Session({f"u/{i}.xml": _Resp(XBRL) for i in range(5)})
    filings = [{"xmlFileName": f"u/{i}.xml", "broadcastDateTime": "23-Oct-2026 21:18:34"} for i in range(5)]
    df = insider.fetch_new(dead, filings, set(), pause=0, out_dir=tmp_path, renew=lambda: live)
    assert dead.asked == ["u/0.xml", "u/1.xml", "u/2.xml"] and live.asked == ["u/3.xml", "u/4.xml"]
    assert df["url"].tolist() == ["u/3.xml", "u/4.xml"]


def test_a_disclosure_filed_twice_keeps_the_first_time_and_the_last_values(tmp_path):
    a = insider.parse_xbrl(XBRL, pd.Timestamp("2026-08-17 23:08:04"), "u/first.xml")
    b = insider.parse_xbrl(XBRL.replace(">5500000<", ">5600000<"), pd.Timestamp("2026-08-17 23:09:02"), "u/again.xml")
    insider.write(b, tmp_path)
    insider.write(a, tmp_path)                               # the earlier one read second
    d = pd.read_parquet(tmp_path / "2026.parquet")
    assert len(d) == 1 and d["broadcast_dt"].iloc[0] == pd.Timestamp("2026-08-17 23:08:04")
    assert d["value"].iloc[0] == 5600000 and d["url"].iloc[0] == "u/again.xml"


def test_filed_again_years_later_stays_in_the_first_year_and_untouched_files_are_not_rewritten(tmp_path):
    a = insider.parse_old(OLD)                                                       # broadcast Sep 2024
    insider.write(a, tmp_path)
    b = insider.parse_xbrl(XBRL, pd.Timestamp("2026-10-23 21:18:34"), "u/x.xml")
    assert [p.name for p in insider.write(b, tmp_path)] == ["2026.parquet"]           # 2024 not rewritten
    late = a.assign(broadcast_dt=pd.Timestamp("2026-03-18 11:24"), url="u/late.xml")   # the same trade filed again
    insider.write(late, tmp_path)
    d24, d26 = pd.read_parquet(tmp_path / "2024.parquet"), pd.read_parquet(tmp_path / "2026.parquet")
    assert len(d24) == 1 and d24["url"].iloc[0] == "u/late.xml" and d24["broadcast_dt"].iloc[0] == pd.Timestamp("2024-09-05 19:06")
    assert d26["url"].tolist() == ["u/x.xml"]
