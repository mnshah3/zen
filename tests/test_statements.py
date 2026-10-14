"""The detailed balance sheet and the cash flow statement (zen.data.statements)."""

from __future__ import annotations

from datetime import date

import duckdb
import pandas as pd

from zen.data import statements

HEAD = """<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:xbrldi="http://xbrl.org/2006/xbrldi"
            xmlns:in-capmkt="http://www.sebi.gov.in/xbrl/in-capmkt">"""


def _span(cid, start, end, member=None):
    seg = (f'<xbrli:segment><xbrldi:explicitMember dimension="in-capmkt:SegmentsAxis">in-capmkt:{member}'
           "</xbrldi:explicitMember></xbrli:segment>") if member else ""
    return (f'<xbrli:context id="{cid}"><xbrli:entity><xbrli:identifier scheme="s">X</xbrli:identifier>{seg}'
            f"</xbrli:entity><xbrli:period><xbrli:startDate>{start}</xbrli:startDate><xbrli:endDate>{end}"
            "</xbrli:endDate></xbrli:period></xbrli:context>")


def _instant(cid, day):
    return (f'<xbrli:context id="{cid}"><xbrli:entity><xbrli:identifier scheme="s">X</xbrli:identifier>'
            f"</xbrli:entity><xbrli:period><xbrli:instant>{day}</xbrli:instant></xbrli:period></xbrli:context>")


def _fact(tag, ref, value):
    return f'<in-capmkt:{tag} contextRef="{ref}">{value}</in-capmkt:{tag}>'


def _doc(*parts) -> bytes:
    return (HEAD + "".join(parts) + "</xbrli:xbrl>").encode("utf-8")


# The 2025 format: every context dated, opening and closing cash at the two balance-sheet dates.
NEW = _doc(
    _span("OneD", "2026-01-01", "2026-03-31"), _span("FourD", "2025-04-01", "2026-03-31"),
    _span("PrevD", "2024-04-01", "2025-03-31"), _span("SegD", "2025-04-01", "2026-03-31", "RetailMember"),
    _instant("OneI", "2026-03-31"), _instant("PY_I", "2025-03-31"),
    _fact("DateOfStartOfFinancialYear", "OneD", "2025-04-01"), _fact("DateOfEndOfFinancialYear", "OneD", "2026-03-31"),
    _fact("DateOfStartOfReportingPeriod", "OneD", "2026-01-01"), _fact("DateOfEndOfReportingPeriod", "OneD", "2026-03-31"),
    _fact("DateOfStartOfReportingPeriod", "FourD", "2025-04-01"), _fact("DateOfEndOfReportingPeriod", "FourD", "2026-03-31"),
    _fact("RevenueFromOperations", "OneD", "3,000"), _fact("RevenueFromOperations", "FourD", "10,000"),
    _fact("ProfitLossForPeriod", "OneD", "300"), _fact("ProfitLossForPeriod", "FourD", "1,000"),
    _fact("PropertyPlantAndEquipment", "OneI", "4000"), _fact("PropertyPlantAndEquipment", "PY_I", "3500"),
    _fact("Inventories", "OneI", "900"), _fact("TradeReceivablesCurrent", "OneI", "1200"),
    _fact("CashAndCashEquivalents", "OneI", "650"), _fact("TradePayablesCurrent", "OneI", "800"),
    _fact("Equity", "OneI", "5000"), _fact("Assets", "OneI", "9000"),
    _fact("CashFlowsFromUsedInOperatingActivities", "SegD", "77"),           # a segment: never the company
    _fact("CashFlowsFromUsedInOperatingActivities", "PrevD", "1100"),        # last year's column
    _fact("CashFlowsFromUsedInOperatingActivities", "FourD", "1500"),
    _fact("PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities", "FourD", "700"),
    _fact("CashFlowsFromUsedInInvestingActivities", "FourD", "-900"),
    _fact("PaymentsOfFinanceLeaseLiabilitiesClassifiedAsFinancingActivities", "FourD", "0"),
    _fact("PaymentsOfLeaseLiabilitiesClassifiedAsFinancingActivities", "FourD", "60"),
    _fact("DividendsPaidClassifiedAsFinancingActivities", "FourD", "200"),
    _fact("CashFlowsFromUsedInFinancingActivities", "FourD", "-500"),
    _fact("EffectOfExchangeRateChangesOnCashAndCashEquivalents", "FourD", "50"),
    _fact("IncreaseDecreaseInCashAndCashEquivalents", "FourD", "150"),
    _fact("CashAndCashEquivalentsCashFlowStatement", "PY_I", "500"),
    _fact("CashAndCashEquivalentsCashFlowStatement", "OneI", "650"),
)

# The 2023 and 2024 format: the cumulative context is DECLARED with the quarter's dates, stated
# with the right ones, and carries both cash balances, opening first.
OLD = _doc(
    _span("OneD", "2023-07-01", "2023-09-30"), _span("FourD", "2023-07-01", "2023-09-30"), _instant("OneI", "2023-09-30"),
    _fact("DateOfStartOfFinancialYear", "OneD", "2023-04-01"), _fact("DateOfEndOfFinancialYear", "OneD", "2024-03-31"),
    _fact("DateOfStartOfReportingPeriod", "OneD", "2023-07-01"), _fact("DateOfEndOfReportingPeriod", "OneD", "2023-09-30"),
    _fact("DateOfStartOfReportingPeriod", "FourD", "2023-04-01"), _fact("DateOfEndOfReportingPeriod", "FourD", "2023-09-30"),
    _fact("RevenueFromOperations", "OneD", "5969200000"), _fact("RevenueFromOperations", "FourD", "11907300000"),
    _fact("PropertyPlantAndEquipment", "OneI", "946400000"), _fact("CashAndCashEquivalents", "OneI", "754400000"),
    _fact("Equity", "OneI", "9000000000"), _fact("Assets", "OneI", "14000000000"),
    _fact("CashFlowsFromUsedInOperatingActivities", "FourD", "2200100000"),
    _fact("CashFlowsFromUsedInInvestingActivities", "FourD", "-826800000"),
    _fact("CashFlowsFromUsedInFinancingActivities", "FourD", "-1328400000"),
    _fact("EffectOfExchangeRateChangesOnCashAndCashEquivalents", "FourD", "-2800000"),
    _fact("IncreaseDecreaseInCashAndCashEquivalents", "FourD", "42100000"),
    _fact("CashAndCashEquivalentsCashFlowStatement", "FourD", "712300000"),
    _fact("CashAndCashEquivalentsCashFlowStatement", "FourD", "754400000"),
)


def test_the_year_to_date_cash_flow_of_the_company_not_a_segment_or_last_year():
    r = statements.parse(NEW)
    assert r["cfo"] == 1500 and r["cfi"] == -900 and r["cff"] == -500 and r["capex_ppe"] == 700
    assert r["ytd_start"] == date(2025, 4, 1) and r["ytd_days"] == 365
    assert r["fy_start"] == date(2025, 4, 1) and r["fy_end"] == date(2026, 3, 31)
    assert r["ytd_revenue"] == 10000 and r["ytd_profit"] == 1000            # the year, not the quarter
    assert r["cash_start"] == 500 and r["cash_end"] == 650
    assert r["cf_adds_up"] is True and r["cash_ties"] is True and r["has_cash_flow"] and r["has_detail"]
    assert r["lease_payments"] == 60                                        # not the zero under the older name
    assert r["dividends_paid"] == 200                                       # as filed: a positive amount paid


def test_the_balance_sheet_is_at_the_balance_sheet_date():
    r = statements.parse(NEW)
    assert r["ppe"] == 4000 and r["inventories"] == 900 and r["receivables"] == 1200 and r["payables"] == 800
    assert r["cash"] == 650 and r["equity"] == 5000 and r["assets"] == 9000 and r["format"] == "indas"


def test_a_cumulative_context_declared_as_the_quarter_is_read_by_what_it_states():
    r = statements.parse(OLD)
    assert r["ytd_start"] == date(2023, 4, 1) and r["ytd_days"] == 183        # the half-year, as stated
    assert r["cfo"] == 2200100000 and r["ytd_revenue"] == 11907300000
    assert r["cash_start"] == 712300000 and r["cash_end"] == 754400000        # opening first, checked by the sum
    assert r["cf_adds_up"] is True and r["cash_ties"] is True
    assert r["fy_end"] == date(2024, 3, 31)


def test_a_cash_flow_that_does_not_add_up_is_kept_and_marked():
    doc = NEW.replace(b">150</in-capmkt:IncreaseDecreaseInCashAndCashEquivalents>",
                      b">900</in-capmkt:IncreaseDecreaseInCashAndCashEquivalents>")
    r = statements.parse(doc)
    assert r["cfo"] == 1500 and r["net_change"] == 900
    assert r["cf_adds_up"] is False and r["cash_ties"] is False and r["has_cash_flow"]


def test_rounding_in_the_filing_is_not_a_failed_check():
    # filed in crores: each total rounded, so the four are out by a crore
    doc = _doc(_span("FourD", "2025-04-01", "2026-03-31"), _instant("OneI", "2026-03-31"), _instant("PY_I", "2025-03-31"),
               _fact("CashFlowsFromUsedInOperatingActivities", "FourD", "1230000000"),
               _fact("CashFlowsFromUsedInInvestingActivities", "FourD", "-450000000"),
               _fact("CashFlowsFromUsedInFinancingActivities", "FourD", "-560000000"),
               _fact("IncreaseDecreaseInCashAndCashEquivalents", "FourD", "230000000"),
               _fact("CashAndCashEquivalentsCashFlowStatement", "PY_I", "100000000"),
               _fact("CashAndCashEquivalentsCashFlowStatement", "OneI", "320000000"))
    r = statements.parse_ytd(doc)
    assert r["cf_adds_up"] is True and r["cash_ties"] is True                 # 22 against 23, 32 against 33
    assert statements._unit([1230000000, -450000000, 230000000]) == 1e7
    assert statements._unit([123456789.0]) == 1.0 and statements._unit([None, 0]) == 1e7


def test_an_undated_cash_flow_is_left_empty():
    doc = _doc(_instant("OneI", "2026-03-31"), _fact("PropertyPlantAndEquipment", "OneI", "4000"),
               _fact("Equity", "OneI", "5000"), _fact("Assets", "OneI", "9000"),
               _fact("CashFlowsFromUsedInOperatingActivities", "FourD", "1500"),     # FourD: never declared or stated
               _fact("CashFlowsFromUsedInInvestingActivities", "FourD", "-900"),
               _fact("CashFlowsFromUsedInFinancingActivities", "FourD", "-500"))
    r = statements.parse(doc)
    assert "cfo" not in r and r["has_cash_flow"] is False and r["has_detail"] is True and r["ppe"] == 4000
    assert statements.parse_ytd(b"not xml") == {}


def test_a_finance_company_has_its_own_balance_sheet():
    doc = _doc(_span("FourD", "2025-04-01", "2025-09-30"), _instant("OneI", "2025-09-30"),
               _fact("Loans", "OneI", "70000"), _fact("Investments", "OneI", "9000"), _fact("FinanicalAssets", "OneI", "82000"),
               _fact("DebtSecurities", "OneI", "30000"), _fact("Borrowings", "OneI", "25000"), _fact("Deposits", "OneI", "8000"),
               _fact("Equity", "OneI", "15000"), _fact("Assets", "OneI", "85000"))
    r = statements.parse(doc, nbfc=True)
    assert r["format"] == "nbfc" and r["loans"] == 70000 and r["fin_assets"] == 82000 and r["debt_securities"] == 30000
    assert r["has_detail"] is True and r["has_cash_flow"] is False
    assert statements.is_nbfc("https://x/INTEGRATED_FILING_NBFC_INDAS_1_2_WEB.xml")
    assert not statements.is_nbfc("https://x/corporate/xbrl/INDAS_90708_818546_14042023103840.xml")


class _Resp:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        return None


class _Session:
    def __init__(self, content):
        self.content, self.calls = content, 0

    def get(self, url, timeout=None):
        self.calls += 1
        return _Resp(self.content)


def _docs(n=1):
    return pd.DataFrame([{"symbol": "XCO", "company": "X Co", "period_end": pd.Timestamp("2026-03-31"),
                          "broadcast_dt": pd.Timestamp("2026-05-10 16:00") + pd.Timedelta(days=i), "consolidated": True,
                          "xbrl_url": f"https://example.invalid/INTEGRATED_FILING_INDAS_{i}.xml"} for i in range(n)])


def _no_cache(monkeypatch):
    monkeypatch.setattr(statements.xbrl_cache, "get", lambda url, root=None: None)
    monkeypatch.setattr(statements.xbrl_cache, "put", lambda url, content, root=None: False)


def test_read_written_and_rebuilt_as_a_table(tmp_path, monkeypatch):
    _no_cache(monkeypatch)
    rows, failed = statements.fetch(_docs(), _Session(NEW), pause=0)
    assert failed == 0 and list(rows.columns) == statements.COLUMNS and rows.loc[0, "cfo"] == 1500
    out = tmp_path / "statements"
    assert [p.name for p in statements.write(rows, out)] == ["2026.parquet"]
    assert [p.name for p in out.iterdir()] == ["2026.parquet"]                # put in place whole, nothing left over
    statements.write(rows, out)                                               # the same document again: one row
    con = duckdb.connect(str(tmp_path / "t.duckdb"))
    assert statements.rebuild(con, out) == 1
    got = con.execute("SELECT symbol, period_end, fy_end, ytd_days, cfo, capex_ppe, cash_end, cf_adds_up, has_cash_flow, "
                      "ppe, format FROM statements").fetchone()
    assert got == ("XCO", date(2026, 3, 31), date(2026, 3, 31), 365, 1500.0, 700.0, 650.0, True, True, 4000.0, "indas")
    assert statements.stored_urls(out) == {"https://example.invalid/INTEGRATED_FILING_INDAS_0.xml"}
    con.close()


def test_an_error_page_is_a_failed_download_and_a_run_of_them_stops_the_job(monkeypatch):
    _no_cache(monkeypatch)
    monkeypatch.setattr(statements.time, "sleep", lambda s: None)
    session = _Session(b"<html>Access denied</html>")
    rows, failed = statements.fetch(_docs(8), session, pause=0, give_up_after=3)
    assert rows.empty and failed == 3 and session.calls == 3                  # stopped, not hammering


def test_documents_are_the_balance_sheet_filings_and_latest_keeps_the_last_revision(tmp_path):
    con = duckdb.connect(str(tmp_path / "t.duckdb"))
    con.execute("CREATE TABLE financials (symbol VARCHAR, company VARCHAR, period_end DATE, broadcast_dt TIMESTAMP, "
                "consolidated BOOLEAN, has_balance_sheet BOOLEAN, xbrl_url VARCHAR)")
    con.executemany("INSERT INTO financials VALUES (?, ?, ?, ?, ?, ?, ?)", [
        ("A", "A Ltd", "2026-03-31", "2026-05-01 10:00", True, True, "u1"),
        ("A", "A Ltd", "2026-03-31", "2026-05-20 10:00", True, True, "u2"),          # a revision
        ("A", "A Ltd", "2026-03-31", "2026-05-01 10:05", False, True, "u3"),
        ("A", "A Ltd", "2025-12-31", "2026-01-20 10:00", True, False, "u4"),         # a quarter: no balance sheet
    ])
    assert list(statements.documents(con)["xbrl_url"]) == ["u1", "u3", "u2"]
    assert sorted(statements.documents(con, latest=True)["xbrl_url"]) == ["u2", "u3"]
    con.close()


def test_the_job_reads_the_newest_filings_first_so_dead_old_ones_cannot_block_them(monkeypatch):
    from jobs import update_statements as job
    asked = []

    class Con:
        def execute(self, sql):
            return self

        def fetchone(self):
            return (0, 0, None, None)

        def close(self):
            pass

    def fetch(batch, pause=None):
        asked.extend(batch["xbrl_url"])
        return pd.DataFrame(columns=["has_cash_flow"]), 0

    monkeypatch.setattr(job, "_archive", lambda read_only: Con())
    monkeypatch.setattr(statements, "documents", lambda con, latest=False: _docs(5))     # u0 oldest .. u4 newest
    monkeypatch.setattr(statements, "stored_urls", lambda: {_docs(5)["xbrl_url"][3]})
    monkeypatch.setattr(statements, "fetch", fetch)
    monkeypatch.setattr(statements, "write", lambda rows: [])
    monkeypatch.setattr(statements, "rebuild", lambda con: 0)
    urls = list(_docs(5)["xbrl_url"])
    job.main(["--limit", "3"])
    assert asked == [urls[4], urls[2], urls[1]]
    asked.clear()
    job.main([])
    assert asked == [urls[4], urls[2], urls[1], urls[0]]


def test_sessions_are_replaced_after_a_failure_and_on_a_timer(monkeypatch):
    # NSE's archive host wedges a long-lived connection pool: only a new session object cures it
    _no_cache(monkeypatch)
    monkeypatch.setattr(statements.time, "sleep", lambda s: None)
    made = []

    class Flaky(_Session):
        def __init__(self, fail):
            super().__init__(NEW)
            self.fail, self.closed = fail, False

        def get(self, url, timeout=None):
            if self.fail:
                raise TimeoutError("wedged")
            return super().get(url, timeout)

        def close(self):
            self.closed = True

    def make():
        made.append(Flaky(fail=len(made) == 0))            # the first session is the wedged one
        return made[-1]

    rows, failed = statements.fetch(_docs(3), pause=0, make_session=make)
    assert failed == 1 and len(rows) == 2 and len(made) == 2 and made[0].closed and not made[1].closed
    clock = iter([0.0, 1000.0, 1000.0])                    # made at 0, looked at again at 1000: too old
    monkeypatch.setattr(statements, "_now", lambda: next(clock))
    made.clear()
    made.append(None)                                      # so that no session made here fails
    rows, failed = statements.fetch(_docs(2), pause=0, make_session=make)
    assert failed == 0 and len(rows) == 2 and len(made) == 3            # a fresh one once the first is old


# A year-end filing before Sep 2022 (TCS, Mar 2021, in crores): one context dated as the fourth
# quarter carries the quarter's income beside the whole year's cash flow.
YEAR_END_2021 = _doc(
    _span("FourD", "2021-01-01", "2021-03-31"), _instant("OneI", "2021-03-31"), _instant("PY_I", "2020-03-31"),
    _fact("DateOfStartOfFinancialYear", "FourD", "2020-04-01"), _fact("DateOfEndOfFinancialYear", "FourD", "2021-03-31"),
    _fact("DateOfStartOfReportingPeriod", "FourD", "2021-01-01"), _fact("DateOfEndOfReportingPeriod", "FourD", "2021-03-31"),
    _fact("RevenueFromOperations", "FourD", "43705"),
    _fact("CashFlowsFromUsedInOperatingActivities", "FourD", "38802"),
    _fact("CashFlowsFromUsedInInvestingActivities", "FourD", "-8129"),
    _fact("CashFlowsFromUsedInFinancingActivities", "FourD", "-32634"),
    _fact("IncreaseDecreaseInCashAndCashEquivalents", "FourD", "-1788"),
    _fact("CashAndCashEquivalentsCashFlowStatement", "PY_I", "8646"),
    _fact("CashAndCashEquivalentsCashFlowStatement", "OneI", "6858"),
)


def test_a_year_end_cash_flow_before_sep_2022_is_the_years_not_the_quarters():
    r = statements.parse_ytd(YEAR_END_2021)
    assert r["ytd_start"] == date(2020, 4, 1) and r["ytd_days"] == 365
    assert r["cfo"] == 38802 and r["cash_start"] == 8646 and r["cash_end"] == 6858 and r["cash_ties"]
    assert "ytd_revenue" not in r                              # the quarter's revenue, not the year's
    # a half-year one is dated as it states (Sep 2021: the stated span already starts in April)
    half = statements.parse_ytd(_doc(
        _span("FourD", "2021-04-01", "2021-09-30"),
        _fact("DateOfStartOfFinancialYear", "FourD", "2021-04-01"), _fact("DateOfEndOfFinancialYear", "FourD", "2022-03-31"),
        _fact("RevenueFromOperations", "FourD", "91000"), _fact("CashFlowsFromUsedInOperatingActivities", "FourD", "19310")))
    assert half["ytd_days"] == 183 and half["ytd_revenue"] == 91000
