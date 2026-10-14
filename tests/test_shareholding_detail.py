"""Who holds the public float, from each shareholding filing's document (zen.data.shareholding_detail)."""

from __future__ import annotations

from datetime import date

import duckdb
import pandas as pd

from zen.data import shareholding_detail as detail

HEAD = """<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:xbrldi="http://xbrl.org/2006/xbrldi"
            xmlns:in-bse-shp="http://www.bseindia.com/xbrl/shp">"""


def _ctx(cid, *members):
    seg = "".join(f'<xbrldi:explicitMember dimension="in-bse-shp:{axis}">in-bse-shp:{m}</xbrldi:explicitMember>'
                  for axis, m in members)
    return (f'<xbrli:context id="{cid}"><xbrli:entity><xbrli:identifier scheme="s">X</xbrli:identifier>'
            f"<xbrli:segment>{seg}</xbrli:segment></xbrli:entity>"
            "<xbrli:period><xbrli:instant>2026-06-30</xbrli:instant></xbrli:period></xbrli:context>")


def _doc(shares: dict, counts: dict | None = None, extra: str = "") -> bytes:
    """One context a category; `shares` and `counts` map a category to what the document tags."""
    parts = [HEAD]
    for i, cat in enumerate({**shares, **(counts or {})}):
        parts.append(_ctx(f"C{i}", ("CategoryOfShareholdersAxis", cat)))
        if cat in shares:
            parts.append(f'<in-bse-shp:ShareholdingAsAPercentageOfTotalNumberOfShares contextRef="C{i}">'
                         f"{shares[cat]}</in-bse-shp:ShareholdingAsAPercentageOfTotalNumberOfShares>")
        if counts and cat in counts:
            parts.append(f'<in-bse-shp:NumberOfShareholders contextRef="C{i}">{counts[cat]}</in-bse-shp:NumberOfShareholders>')
    parts.append(extra + "</xbrli:xbrl>")
    return "".join(parts).encode("utf-8")


NEW = _doc({"ShareholdingOfPromoterAndPromoterGroupMember": "0.5048", "MutualFundsOrUTIMember": "0.1011",
            "InsuranceCompaniesMember": "0.092", "ProvidentFundsOrPensionFundsMember": "0.0155",
            "AlternativeInvestmentFundsMember": "0.004", "InstitutionsDomesticMember": "0.2119",
            "InstitutionsForeignPortfolioInvestorCategoryOneMember": "0.1652", "InstitutionsForeignMember": "0.172",
            "ResidentIndividualShareholdersHoldingNominalShareCapitalUpToRsTwoLakhMember": "0.071",
            "NonInstitutionsMember": "0.1104", "PublicShareholdingMember": "0.4952", "ShareholdingPatternMember": "1"},
           {"ShareholdingPatternMember": "4,651,863", "PublicShareholdingMember": "4651815"})

# Before 2023: percentages, and the older spelling of the categories.
OLD = _doc({"ShareholdingOfPromoterAndPromoterGroupMember": "72.3", "MutualFundsOrUtiMember": "3.31",
            "InstitutionsDomesticMember": "8.9", "InstitutionsForeignMember": "12.94",
            "PublicShareholdingMember": "27.7", "ShareholdingPatternMember": "100"},
           {"ShareholdingPatternMember": "2556970"})


def test_each_category_in_percent_as_the_document_totals_them():
    r = detail.parse(NEW)
    assert r["promoter_pct"] == 50.48 and r["public_pct"] == 49.52
    assert r["fii_pct"] == 17.2 and r["dii_pct"] == 21.19            # the format's own totals, nothing added up
    assert r["mf_pct"] == 10.11 and r["insurance_pct"] == 9.2 and r["pension_pct"] == 1.55 and r["aif_pct"] == 0.4
    assert r["retail_pct"] == 7.1 and r["non_institution_pct"] == 11.04 and r["shareholders"] == 4651863


def test_percentages_and_older_spellings_are_read_the_same_way():
    r = detail.parse(OLD)
    assert r["promoter_pct"] == 72.3 and r["fii_pct"] == 12.94 and r["dii_pct"] == 8.9 and r["mf_pct"] == 3.31
    assert "insurance_pct" not in r                                  # a line the filing does not tag stays unknown
    # no total tagged: nothing above 1 means fractions
    assert detail.parse(_doc({"InstitutionsForeignMember": "0.25", "PublicShareholdingMember": "1"}))["fii_pct"] == 25.0
    assert detail.parse(_doc({"InstitutionsForeignMember": "25", "PublicShareholdingMember": "100"}))["fii_pct"] == 25.0


def test_one_holders_line_is_not_a_category_total():
    # a context that names a shareholder as well as a category is that holder's own line
    one_fund = (_ctx("H1", ("CategoryOfShareholdersAxis", "MutualFundsOrUTIMember"), ("DetailsOfSharesAxis", "Holder1Member"))
                + '<in-bse-shp:ShareholdingAsAPercentageOfTotalNumberOfShares contextRef="H1">0.05'
                  "</in-bse-shp:ShareholdingAsAPercentageOfTotalNumberOfShares>")
    doc = _doc({"MutualFundsOrUTIMember": "0.1011", "ShareholdingPatternMember": "1"}, extra=one_fund)
    assert detail.parse(doc)["mf_pct"] == 10.11
    assert detail.parse(b"not xml") == {} and detail.parse(_doc({})) == {}


def test_the_document_is_checked_against_the_listing():
    r = detail.parse(NEW)
    assert detail._agrees(r, 50.48, 49.52) is True and detail._agrees(r, 50.5, 49.5) is True
    assert detail._agrees(r, 55.0, 45.0) is False
    no_promoter = detail.parse(_doc({"PublicShareholdingMember": "1", "ShareholdingPatternMember": "1"}))
    assert detail._agrees(no_promoter, 0.0, 100.0) is True           # no promoter line is zero, not a gap
    assert detail._agrees({}, None, None) is None


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

    def close(self):
        pass


def _filings(n=1):
    return pd.DataFrame([{"record_id": f"R{i}", "symbol": "XCO", "period_end": pd.Timestamp("2026-06-30"),
                          "broadcast_dt": pd.Timestamp("2026-07-16 19:24") + pd.Timedelta(days=i),
                          "promoter_pct": 50.48, "public_pct": 49.52,
                          "xbrl_url": f"https://example.invalid/SHP_{i}_WEB.xml"} for i in range(n)])


def test_read_without_keeping_the_documents_then_written_and_rebuilt(tmp_path, monkeypatch):
    monkeypatch.setattr(detail.xbrl_cache, "get", lambda url, root=None: None)
    stored = []
    monkeypatch.setattr(detail.xbrl_cache, "put", lambda url, content, root=None: stored.append(url))
    monkeypatch.setattr(detail.time, "sleep", lambda s: None)
    rows, failed = detail.fetch(_filings(2), _Session(NEW))
    assert failed == 0 and list(rows.columns) == detail.COLUMNS and stored == []      # read and discarded
    assert rows.loc[0, "fii_pct"] == 17.2 and bool(rows.loc[0, "agrees"]) and bool(rows.loc[0, "has_split"])
    out = tmp_path / "shareholding_detail"
    assert [p.name for p in detail.write(rows, out)] == ["2026.parquet"]
    detail.write(rows.head(1), out)                                                  # the same filing again: no new row
    assert [p.name for p in out.iterdir()] == ["2026.parquet"] and detail.stored_ids(out) == {"R0", "R1"}
    con = duckdb.connect(str(tmp_path / "t.duckdb"))
    assert detail.rebuild(con, out) == 2
    got = con.execute("SELECT symbol, period_end, fii_pct, dii_pct, mf_pct, shareholders, agrees FROM shareholding_detail "
                      "WHERE record_id = 'R1'").fetchone()
    assert got == ("XCO", date(2026, 6, 30), 17.2, 21.19, 10.11, 4651863.0, True)
    con.close()


def test_an_error_page_is_a_failed_download_and_a_run_of_them_stops_the_job(monkeypatch):
    monkeypatch.setattr(detail.xbrl_cache, "get", lambda url, root=None: None)
    monkeypatch.setattr(detail.time, "sleep", lambda s: None)
    session = _Session(b"<html>Access denied</html>")
    rows, failed = detail.fetch(_filings(8), session, give_up_after=3)
    assert rows.empty and failed == 3 and session.calls == 3


def test_the_filings_to_read_come_from_the_committed_files(tmp_path):
    sh = tmp_path / "shareholding"
    sh.mkdir()
    cols = ["symbol", "isin", "company", "period_end", "promoter_pct", "public_pct", "employee_trusts_pct",
            "broadcast_dt", "submission_date", "revised", "record_id", "xbrl_url"]
    rows = [("A", "I", "A Ltd", "2026-06-30", 50.0, 50.0, 0.0, "2026-07-10 10:00", None, None, "1", "https://x/SHP_1.xml"),
            ("A", "I", "A Ltd", "2026-03-31", 50.0, 50.0, 0.0, "2026-04-10 10:00", None, None, "2", None),          # no document
            ("B", "J", "B Ltd", "2026-06-30", 60.0, 40.0, 0.0, "2026-07-05 10:00", None, None, "3", "https://x/SHP_3.xml"),
            ("C", "K", "C Ltd", "2026-06-30", 70.0, 30.0, 0.0, "2026-07-06 10:00", None, None, "4", "https://x/xbrl/-")]  # NSE's "no document"
    df = pd.DataFrame(rows, columns=cols)
    df["period_end"], df["broadcast_dt"] = pd.to_datetime(df["period_end"]), pd.to_datetime(df["broadcast_dt"])
    df.to_parquet(sh / "2026.parquet", index=False)
    docs = detail.documents(sh)
    assert list(docs["record_id"]) == ["3", "1"]                     # oldest broadcast first, only those with a document
    assert detail.documents(tmp_path / "none").empty and detail.stored_ids(tmp_path / "none") == set()


def test_the_job_reads_the_newest_filings_first_so_dead_old_ones_cannot_block_them(monkeypatch):
    from jobs import update_shareholding_detail as job
    asked = []

    def fetch(batch, pause=None):
        asked.extend(batch["record_id"])
        return pd.DataFrame(columns=detail.COLUMNS), 0

    monkeypatch.setattr(detail, "documents", lambda: _filings(5))      # R0 oldest .. R4 newest
    monkeypatch.setattr(detail, "stored_ids", lambda complete=True: {"R3"})
    monkeypatch.setattr(detail, "fetch", fetch)
    monkeypatch.setattr(detail, "write", lambda rows: [])
    job.main(["--no-archive", "--limit", "3"])
    assert asked == ["R4", "R2", "R1"]
    asked.clear()
    job.main(["--no-archive"])
    assert asked == ["R4", "R2", "R1", "R0"]


def _promoter_doc(facts: dict, flags: dict | None = None) -> bytes:
    """A filing whose promoter line (context C0) tags `facts`; `flags` are its yes/no answers."""
    tags = "".join(f'<in-bse-shp:{k} contextRef="C0">{v}</in-bse-shp:{k}>' for k, v in facts.items())
    tags += "".join(f'<in-bse-shp:{k} contextRef="C9">{v}</in-bse-shp:{k}>' for k, v in (flags or {}).items())
    return _doc({"ShareholdingOfPromoterAndPromoterGroupMember": "0.4479", "PublicShareholdingMember": "0.5521",
                 "ShareholdingPatternMember": "1"}, extra=tags)


def test_promoter_pledges_are_read_from_share_counts_in_both_formats():
    # current format, every promoter share pledged (RACE, Sep 2026: NSE's pledge page says 100%)
    r = detail.parse(_promoter_doc({"NumberOfShares": "7730100", "NumberOfSharesEncumberedUnderPledged": "7730100",
                                    "NumberOfSharesEncumbered": "7730100",
                                    "EncumberedShareUnderPledgedAsPercentageOfTotalNumberOfShares": "1"}))
    assert r["promoter_shares"] == 7730100 and r["promoter_pledged_pct"] == 100.0 and r["promoter_encumbered_pct"] == 100.0
    # current format, some shares under a non-disposal undertaking and none pledged
    r = detail.parse(_promoter_doc({"NumberOfShares": "1000", "NumberOfSharesEncumbered": "250"},
                                   {"WhetherAnySharesHeldByPromotersAreEncumberedUnderPledged": "No",
                                    "WhetherAnySharesHeldByPromotersAreEncumberedUnderNonDisposalUndertaking": "Yes"}))
    assert r["promoter_pledged_pct"] == 0.0 and r["promoter_encumbered_pct"] == 25.0
    # older format: one "pledged or otherwise encumbered" figure (TEMBO, Dec 2022: the document says 8.74%)
    r = detail.parse(_promoter_doc({"NumberOfShares": "7255000", "PledgedOrEncumberedNumberOfShares": "634047",
                                    "PledgedOrEncumberedSharesHeldAsPercentageOfTotalNumberOfShares": "8.74"}))
    assert r["promoter_encumbered_pct"] == 8.7394 and r["promoter_pledged_pct"] is None   # not split in that format


def test_no_encumbrance_is_zero_but_silence_is_unknown():
    no = {"WhetherAnySharesHeldByPromotersAreEncumberedUnderPledged": "No",
          "WhetherAnySharesHeldByPromotersAreEncumberedOtherThanByWayOfPledgeOrNDU": "No"}
    r = detail.parse(_promoter_doc({"NumberOfShares": "6715496096"}, no))           # RELIANCE: answers no, tags no count
    assert r["promoter_pledged_pct"] == 0.0 and r["promoter_encumbered_pct"] == 0.0
    r = detail.parse(_promoter_doc({"NumberOfShares": "6715496096"}))               # neither counts nor answers
    assert r["promoter_pledged_pct"] is None and r["promoter_encumbered_pct"] is None
    r = detail.parse(_doc({"PublicShareholdingMember": "1", "ShareholdingPatternMember": "1"}))   # no promoter at all
    assert r["promoter_shares"] is None and r["promoter_encumbered_pct"] is None


def test_rows_read_before_the_pledge_figures_are_read_again_and_replaced(tmp_path, monkeypatch):
    monkeypatch.setattr(detail.xbrl_cache, "get", lambda url, root=None: None)
    monkeypatch.setattr(detail.time, "sleep", lambda s: None)
    out = tmp_path / "shareholding_detail"
    out.mkdir()
    old_cols = [c for c in detail.COLUMNS if c not in (*detail.PLEDGE_FIGURES, "pledge_read")]
    old = pd.DataFrame([{"record_id": "R0", "symbol": "XCO", "period_end": pd.Timestamp("2026-06-30"),
                         "broadcast_dt": pd.Timestamp("2026-07-16 19:24"), "fii_pct": 17.2, "agrees": True,
                         "has_split": True, "xbrl_url": "https://example.invalid/SHP_0_WEB.xml"}]).reindex(columns=old_cols)
    old.to_parquet(out / "2026.parquet", index=False)                 # the layout before pledges were read
    assert detail.stored_ids(out) == set()                            # so it counts as not yet read
    rows, failed = detail.fetch(_filings(1), _Session(NEW))
    assert failed == 0 and bool(rows.loc[0, "pledge_read"])
    detail.write(rows, out)
    back = pd.read_parquet(out / "2026.parquet")
    assert len(back) == 1 and bool(back.loc[0, "pledge_read"]) and detail.stored_ids(out) == {"R0"}
    con = duckdb.connect(str(tmp_path / "t.duckdb"))
    assert detail.rebuild(con, out) == 1
    assert con.execute("SELECT pledge_read, fii_pct FROM shareholding_detail").fetchone() == (True, 17.2)
    con.close()


def test_the_daily_run_reads_only_filings_never_read(tmp_path, monkeypatch):
    from jobs import update_shareholding_detail as job
    asked = []
    monkeypatch.setattr(detail, "documents", lambda: _filings(3))             # R0 .. R2
    monkeypatch.setattr(detail, "stored_ids", lambda complete=True: {"R2"} if complete else {"R1", "R2"})
    monkeypatch.setattr(detail, "fetch", lambda batch, pause=None: (asked.extend(batch["record_id"]),
                                                                     (pd.DataFrame(columns=detail.COLUMNS), 0))[1])
    monkeypatch.setattr(detail, "write", lambda rows: [])
    job.main(["--no-archive", "--new-only"])
    assert asked == ["R0"]                       # R1 was read before pledges were added: the backfill's job
    asked.clear()
    job.main(["--no-archive"])
    assert asked == ["R1", "R0"]


class _Gone:
    """NSE's answer for a document it no longer has."""
    status_code = 404

    def raise_for_status(self):
        import requests
        raise requests.HTTPError("404 Not Found", response=self)


def test_documents_nse_no_longer_has_do_not_stop_the_run(monkeypatch):
    monkeypatch.setattr(detail.xbrl_cache, "get", lambda url, root=None: None)
    monkeypatch.setattr(detail.time, "sleep", lambda s: None)

    class Mixed(_Session):
        def get(self, url, timeout=None):
            self.calls += 1
            return _Gone() if self.calls <= 30 else _Resp(self.content)

    s = Mixed(NEW)
    rows, failed = detail.fetch(_filings(32), s, give_up_after=25)
    assert failed == 30 and len(rows) == 2                    # thirty gone in a row, and the run went on
    rows, failed = detail.fetch(_filings(8), _Session(b"<html>Access denied</html>"), give_up_after=3)
    assert rows.empty and failed == 3                         # a refusal still stops it
