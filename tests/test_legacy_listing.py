"""The legacy date-range listing keeps every filing NSE returns.

NSE's `seqNumber` is a running number that restarts, not a filing id: on 19 and 22 July 2022 two different
companies' filings carried the same value (AGASTYAEN and ISEC both 142). The listing once dropped on
seqNumber alone and silently lost the later filing of each pair, worst in the weeks with the most filings,
which is why the June quarters of 2018, 2019 and 2022 looked like gaps at NSE.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from zen.data import financials_legacy as fl


class _Resp:
    def __init__(self, payload):
        self._p = payload
        self.status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


class _Session:
    """Answers each date-range request from a table of {day: rows}."""

    def __init__(self, by_day):
        self.by_day = by_day

    def get(self, url, timeout=None):
        frm = url.split("from_date=")[1].split("&")[0]
        to = url.split("to_date=")[1].split("&")[0]
        d0, d1 = (pd.to_datetime(x, format="%d-%m-%Y").date() for x in (frm, to))
        rows = [r for d, rs in self.by_day.items() if d0 <= d <= d1 for r in rs]
        return _Resp(rows)


def _row(symbol, seq, day, cons="Non-Consolidated", doc=None):
    return {"symbol": symbol, "companyName": symbol.title(), "isin": "INE000A01010", "toDate": "30-Jun-2022",
            "broadCastDate": f"{day:%d-%b-%Y} 16:43:56", "consolidated": cons, "audited": "Un-Audited",
            "xbrl": doc or f"https://nsearchives.nseindia.com/corporate/xbrl/INDAS_{symbol}_WEB.xml",
            "seqNumber": str(seq)}


def test_two_filings_with_the_same_seq_number_are_both_kept(monkeypatch):
    monkeypatch.setattr(fl.time, "sleep", lambda s: None)
    day1, day2 = date(2022, 7, 19), date(2022, 7, 22)
    s = _Session({day1: [_row("AGASTYAEN", 142, day1), _row("TV18BRDCST", 143, day1)],
                  day2: [_row("ISEC", 142, day2), _row("ARIHANTCAP", 144, day2)]})
    L = fl.listing(s, date(2022, 7, 18), date(2022, 7, 24))
    assert sorted(L["symbol"]) == ["AGASTYAEN", "ARIHANTCAP", "ISEC", "TV18BRDCST"]


def test_one_filing_returned_twice_is_kept_once(monkeypatch):
    monkeypatch.setattr(fl.time, "sleep", lambda s: None)
    day = date(2022, 7, 19)
    r = _row("AGASTYAEN", 142, day)
    s = _Session({day: [r, dict(r)]})
    assert len(fl.listing(s, date(2022, 7, 18), date(2022, 7, 24))) == 1


def test_standalone_and_consolidated_of_one_company_are_both_kept(monkeypatch):
    monkeypatch.setattr(fl.time, "sleep", lambda s: None)
    day = date(2022, 7, 19)
    s = _Session({day: [_row("SHEMAROO", 147, day), _row("SHEMAROO", 148, day, cons="Consolidated",
                                                         doc="https://nsearchives.nseindia.com/corporate/xbrl/INDAS_SHEMAROO_C_WEB.xml")]})
    L = fl.listing(s, date(2022, 7, 18), date(2022, 7, 24))
    assert sorted(L["consolidated"]) == [False, True]
