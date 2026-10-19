"""Insurers' results are read from IRDAI's formats, life and general each by its own rules.

THE BUG THESE PIN DOWN

Insurers file their quarterly results in IRDAI's formats, which name profit, EPS and
net worth differently from the Ind-AS statement. None of those names were read, so
until 2026-10-18 no insurer in the archive had a profit, an EPS or a net worth, and
none could be valued. The two formats also use one name for different lines: a life
insurer's ShareholdersFunds is its net worth, a general insurer's is only the
shareholders' fair value change account. The figures below are HDFCLIFE's and
ICICIGI's quarter to 30 Sep 2026 as filed.
"""

from __future__ import annotations

from zen.data import financials as fin

HEAD = ('<?xml version="1.0" encoding="UTF-8"?>'
        '<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" '
        'xmlns:in-capmkt="http://www.sebi.gov.in/xbrl/2025-05-31/in-capmkt">')
TAIL = "</xbrli:xbrl>"
EPS_BEFORE = "BasicAndDilutedEPSBeforeExtraordinaryItemsNetOfTaxExpenseForThePeriodNotToBeAnnualized"
EPS_AFTER = "BasicAndDilutedEPSAfterExtraordinaryItemsNetOfTaxExpenseForThePeriodNotToBeAnnualized"


def ctx(cid, start=None, end=None, instant=None):
    period = (f"<xbrli:instant>{instant}</xbrli:instant>" if instant else
              f"<xbrli:startDate>{start}</xbrli:startDate><xbrli:endDate>{end}</xbrli:endDate>")
    return (f'<xbrli:context id="{cid}"><xbrli:entity><xbrli:identifier '
            f'scheme="http://www.nseindia.com">X</xbrli:identifier></xbrli:entity>'
            f"<xbrli:period>{period}</xbrli:period></xbrli:context>")


def fact(tag, ref, value):
    return f'<in-capmkt:{tag} contextRef="{ref}" unitRef="INR">{value}</in-capmkt:{tag}>'


def doc(*facts) -> bytes:
    contexts = ctx("OneD", "2026-07-01", "2026-09-30") + ctx("OneI", instant="2026-09-30")
    return (HEAD + contexts + "".join(facts) + TAIL).encode()


def test_a_life_insurers_results():
    facts = fin.parse_xbrl(doc(
        fact("GrossPremiumIncome", "OneD", "224024900000"),
        fact("Income", "OneD", "240803600000"),
        fact("ProvisionForTax", "OneD", "369600000"),           # the policyholders' account's
        fact("ProfitLossBeforeTax", "OneD", "5233800000"),
        fact("ProfitLossAfterTaxBeforeExtraordinaryItems", "OneD", "5080000000"),
        fact("ProfitLossAfterTaxAndExtraordinaryItems", "OneD", "5080000000"),
        fact(EPS_BEFORE, "OneD", "2.34"),
        fact(EPS_AFTER, "OneD", "2.34"),
        fact("ShareCapital", "OneI", "21725900000"),
        fact("ReservesAndSurplus", "OneI", "169233800000"),
        fact("ShareholdersFunds", "OneI", "193977400000"),
    ))
    assert facts["total_income"] == 240803600000.0
    assert facts["pbt"] == 5233800000.0
    assert facts["profit_continuing"] == facts["profit_reported"] == 5080000000.0
    assert facts["tax"] == 5233800000.0 - 5080000000.0         # not the policyholders' tax
    assert facts["eps_basic"] == facts["eps_diluted"] == 2.34
    assert facts["equity"] == 193977400000.0                   # its net worth, as stated
    assert facts["equity_capital"] == 21725900000.0
    row = fin._derive({"symbol": "HDFCLIFE", **facts})
    assert round(row["shares_implied"] / 1e7, 1) == 217.1      # crore shares
    assert row["profit_normalised"] == 5080000000.0


def test_a_general_insurers_results():
    facts = fin.parse_xbrl(doc(
        fact("GrossPremiumsWritten", "OneD", "77020900000"),
        fact("OperatingIncome", "OneD", "78437200000"),
        fact("ProfitOrLossBeforeExtraordinaryItems", "OneD", "12474100000"),   # before tax
        fact("ProfitOrLossBeforeTax", "OneD", "12474100000"),
        fact("ProvisionForTax", "OneD", "3010100000"),
        fact("ProfitLossAfterTax", "OneD", "9464000000"),
        fact(EPS_BEFORE, "OneD", "18.95"),
        fact(EPS_AFTER, "OneD", "18.95"),
        fact("ShareCapital", "OneI", "4996100000"),
        fact("ReservesAndSurplus", "OneI", "175056900000"),
        fact("PolicyholdersFunds", "OneI", "20681800000"),
        fact("ShareholdersFunds", "OneI", "8146100000"),       # the shareholders' fair value change
    ))
    assert facts["total_income"] == 78437200000.0
    assert facts["pbt"] == 12474100000.0
    assert facts["tax"] == 3010100000.0
    assert facts["profit_reported"] == 9464000000.0
    assert "profit_continuing" not in facts                    # its "before extraordinary" is pre-tax
    assert facts["eps_basic"] == facts["eps_diluted"] == 18.95
    assert facts["equity"] == 4996100000.0 + 175056900000.0 + 8146100000.0
    assert "_fair_value_shareholders" not in facts
    row = fin._derive({"symbol": "ICICIGI", **facts})
    assert row["profit_normalised"] == 9464000000.0
    assert round(row["shares_implied"] / 1e7, 1) == 49.9


def test_a_general_insurers_quarter_without_a_balance_sheet_has_no_net_worth():
    facts = fin.parse_xbrl(doc(
        fact("GrossPremiumsWritten", "OneD", "1"),
        fact("ProfitOrLossBeforeTax", "OneD", "10"),
        fact("ProfitLossAfterTax", "OneD", "8"),
        fact(EPS_AFTER, "OneD", "1"),
        fact("ShareholdersFunds", "OneI", "5"),
    ))
    assert "equity" not in facts and "_fair_value_shareholders" not in facts
    assert facts["profit_reported"] == 8.0


def test_an_ind_as_filing_does_not_read_the_insurers_names():
    facts = fin.parse_xbrl(doc(
        fact("RevenueFromOperations", "OneD", "100"),
        fact("ProfitBeforeTax", "OneD", "20"),
        fact("ProfitLossForPeriod", "OneD", "15"),
        fact("ProfitLossAfterTax", "OneD", "999"),
        fact("ShareholdersFunds", "OneI", "999"),
        fact("ShareCapital", "OneI", "999"),
        fact("Equity", "OneI", "500"),
    ))
    assert facts["profit_reported"] == 15.0
    assert facts["pbt"] == 20.0
    assert facts["equity"] == 500.0
    assert "equity_capital" not in facts and "eps_diluted" not in facts and "tax" not in facts


def test_the_insurance_list_is_read_into_the_equities_columns():
    from datetime import date
    from zen.data import financials_legacy as fl

    class Resp:
        def __init__(self, data): self.data = data
        def raise_for_status(self): pass
        def json(self): return self.data

    class Session:
        def get(self, url, timeout=None):
            return Resp([{"symbol": "HDFCLIFE", "companyName": "HDFC Life", "periodEnd": "30-Jun-2024",
                          "broadCastDate": "04-Oct-2024 20:44:16", "consolidated": "Consolidated", "audited": "Un-Audited",
                          "xbrl": "https://nsearchives.nseindia.com/corporate/xbrl/LI_109003_1259467_04102024084415.xml"}]
                        if "01-07-2024" in url else [])

    df = fl.insurance_listing(Session(), date(2024, 7, 1), date(2024, 8, 29))
    r = df.iloc[0]
    assert len(df) == 1 and r["symbol"] == "HDFCLIFE" and r["period_end"] == date(2024, 6, 30)
    assert bool(r["consolidated"]) and not bool(r["audited"]) and bool(r["has_xbrl"])
    assert str(r["broadcast_dt"]).startswith("2024-10-04 20:44")
