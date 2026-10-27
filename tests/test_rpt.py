"""zen.data.rpt: related-party transactions from a half year's standalone results filing."""
import duckdb
import pandas as pd

from zen.data import rpt
from zen.data.rpt import parse_rpt

HEAD = ('<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:xbrldi="http://xbrl.org/2006/xbrldi" '
        'xmlns:in-capmkt="http://www.sebi.gov.in/xbrl/2025-05-31/in-capmkt">')


def ctx(cid, member=None, start=None, end=None, instant=None):
    scen = (f'<xbrli:scenario><xbrldi:typedMember dimension="in-capmkt:RelatedPartyTransactionAxis">'
            f'<in-capmkt:RelatedPartyTransactionDomain>{member}</in-capmkt:RelatedPartyTransactionDomain></xbrldi:typedMember></xbrli:scenario>'
            if member else "")
    per = f"<xbrli:instant>{instant}</xbrli:instant>" if instant else f"<xbrli:startDate>{start}</xbrli:startDate><xbrli:endDate>{end}</xbrli:endDate>"
    return (f'<xbrli:context id="{cid}"><xbrli:entity><xbrli:identifier scheme="x">1</xbrli:identifier></xbrli:entity>'
            f'<xbrli:period>{per}</xbrli:period>{scen}</xbrli:context>')


def fact(tag, ref, value):
    return f'<in-capmkt:{tag} contextRef="{ref}">{value}</in-capmkt:{tag}>'


def doc(*parts):
    return (HEAD + "".join(parts) + "</xbrli:xbrl>").encode()


def two_transactions():
    """ITC's shape (March 2026): contexts dated to the quarter, balances at its start and end."""
    parts = [ctx("OneD", start="2026-01-01", end="2026-03-31")]
    for k, (cp, rel, typ, amt, opening, closing) in enumerate([
            ("ITC Infotech India Limited", "Subsidiary", "Sale of goods or services", "69900000", "5000000", "6800000"),
            ("  Gujarat   Hotels Limited ", "Associate", "Any other transaction", "1200000", "0", "300000")], start=1):
        m = f"RelatedPartyTransactionDomain{k}"
        parts += [ctx(f"D_{k}", m, "2026-01-01", "2026-03-31"), ctx(f"I0_{k}", m, instant="2025-12-31"), ctx(f"I1_{k}", m, instant="2026-03-31"),
                  fact("NameOfListedEntityOrSubsidiaryEnteringIntoTheTransaction", f"D_{k}", "ITC Limited"),
                  fact("NameOfCounterParty", f"D_{k}", cp), fact("RelationshipOfTheCounterpartyWithTheListedEntityOrItsSubsidiary", f"D_{k}", rel),
                  fact("TypeOfRelatedPartyTransaction", f"D_{k}", typ),
                  fact("AmountOfRelatedPartyTransactionDuringTheReportingPeriod", f"D_{k}", amt),
                  fact("AmountOfRelatedPartyTransaction", f"I0_{k}", opening), fact("AmountOfRelatedPartyTransaction", f"I1_{k}", closing)]
    parts.append(fact("DetailsOfOtherRelatedPartyTransaction", "D_2", "Rent paid"))
    parts.append(fact("RevenueFromOperations", "OneD", "999"))                  # the company's own figure, not a transaction
    return doc(*parts)


def test_transactions_are_read_by_member_with_the_closing_balance():
    p = parse_rpt(two_transactions())
    assert p["period_end"] == "2026-03-31" and [i["item"] for i in p["items"]] == ["RelatedPartyTransactionDomain1", "RelatedPartyTransactionDomain2"]
    a, b = p["items"]
    assert (a["counterparty"], a["relationship"], a["type"], a["amount"], a["closing"]) == \
        ("ITC Infotech India Limited", "Subsidiary", "Sale of goods or services", 69900000.0, 6800000.0)
    assert (b["counterparty"], b["details"], b["closing"], b["entity"]) == ("Gujarat Hotels Limited", "Rent paid", 300000.0, "ITC Limited")


def test_a_filing_without_transactions_gives_none_and_rows_keep_to_the_filings_quarter():
    assert parse_rpt(doc(ctx("OneD", start="2026-01-01", end="2026-03-31"), fact("RevenueFromOperations", "OneD", "5"))) is None
    assert parse_rpt(b"<not xml") is None
    p = parse_rpt(two_transactions())
    meta = {"symbol": "ITC", "consolidated": False, "period_end": "2026-03-31", "broadcast_dt": "2026-05-21 18:32", "xbrl_url": "u"}
    assert len(rpt.rows_for(meta, p)) == 2
    assert rpt.rows_for({**meta, "period_end": "2025-12-31"}, p) == [] and rpt.rows_for(meta, None) == []


def test_storage_round_trip_keeps_one_row_per_document_and_transaction(tmp_path):
    p = parse_rpt(two_transactions())
    meta = {"symbol": "ITC", "consolidated": False, "period_end": "2026-03-31", "broadcast_dt": "2026-05-21 18:32", "xbrl_url": "u"}
    rows = pd.DataFrame(rpt.rows_for(meta, p))
    rpt.write_parquet(rows, tmp_path)
    rpt.write_parquet(rows, tmp_path)                                  # the same filing again: no duplicates
    con = duckdb.connect()
    assert rpt.rebuild(con, tmp_path) == 2
    assert con.execute("SELECT sum(amount) FROM rpt").fetchone()[0] == 71100000.0
