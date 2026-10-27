"""zen.data.segments: revenue and profit by reportable segment from a results filing."""
from zen.data.segments import parse_segments

HEAD = ('<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:xbrldi="http://xbrl.org/2006/xbrldi" '
        'xmlns:in-capmkt="http://www.sebi.gov.in/xbrl/2025-05-31/in-capmkt">')


def ctx(cid, start, end, axis=None, member=None):
    seg = (f'<xbrli:segment><xbrldi:explicitMember dimension="in-capmkt:{axis}">in-capmkt:{member}</xbrldi:explicitMember></xbrli:segment>'
           if axis else "")
    return (f'<xbrli:context id="{cid}"><xbrli:entity><xbrli:identifier scheme="x">1</xbrli:identifier>{seg}</xbrli:entity>'
            f'<xbrli:period><xbrli:startDate>{start}</xbrli:startDate><xbrli:endDate>{end}</xbrli:endDate></xbrli:period></xbrli:context>')


def fact(tag, ref, value):
    return f'<in-capmkt:{tag} contextRef="{ref}">{value}</in-capmkt:{tag}>'


def doc(*parts):
    return (HEAD + "".join(parts) + "</xbrli:xbrl>").encode()


Q = ("2026-04-01", "2026-06-30")


def test_segments_are_named_matched_by_member_and_reconciled():
    """ITC's shape: two segments, their names and revenue in their own contexts, profit under the
    parallel axis, and the company's total segment revenue and inter-segment sales undimensioned."""
    d = doc(ctx("OneD", *Q),
            ctx("OneReportable1D", *Q, "ReportableSegmentsAxis", "ReportableSegments1Member"),
            ctx("OneReportable2D", *Q, "ReportableSegmentsAxis", "ReportableSegments2Member"),
            ctx("OneReportableFinance1D", *Q, "ReportableSegmentsFinanceCostsAxis", "ReportableSegments1Member"),
            ctx("OneReportableFinance2D", *Q, "ReportableSegmentsFinanceCostsAxis", "ReportableSegments2Member"),
            fact("DescriptionOfReportableSegment", "OneReportable2D", "Agri Business"),
            fact("SegmentRevenue", "OneReportable2D", "300"),
            fact("DescriptionOfReportableSegment", "OneReportable1D", " FMCG -  Cigarettes "),
            fact("SegmentRevenue", "OneReportable1D", "700"),
            fact("SegmentProfitLossBeforeTaxAndFinanceCosts", "OneReportableFinance1D", "250"),
            fact("SegmentProfitLossBeforeTaxAndFinanceCosts", "OneReportableFinance2D", "-20"),
            fact("SegmentRevenue", "OneD", "1000"), fact("InterSegmentRevenue", "OneD", "90"))
    s = parse_segments(d)
    assert s["period_end"] == "2026-06-30" and s["reconciles"] is True and s["total"] == 1000 and s["inter"] == 90
    assert [(x["name"], x["revenue"], x["pbit"]) for x in s["segments"]] == [("FMCG - Cigarettes", 700, 250), ("Agri Business", 300, -20)]


def test_a_year_to_date_context_dated_as_the_quarter_is_not_used():
    """A "Four..." context can hold the year to date under the quarter's dates: the "One..." ones win."""
    d = doc(ctx("OneD", *Q),
            ctx("OneReportable1D", *Q, "ReportableSegmentsAxis", "ReportableSegments1Member"),
            ctx("FourReportable1D", *Q, "ReportableSegmentsAxis", "ReportableSegments1Member"),
            fact("SegmentRevenue", "FourReportable1D", "2800"),
            fact("DescriptionOfReportableSegment", "OneReportable1D", "Steel"),
            fact("SegmentRevenue", "OneReportable1D", "700"),
            fact("SegmentRevenue", "OneD", "700"))
    s = parse_segments(d)
    assert [x["revenue"] for x in s["segments"]] == [700] and s["reconciles"] is True


def test_segments_that_do_not_sum_to_the_total_are_flagged_and_a_half_year_is_refused():
    d = doc(ctx("OneD", *Q),
            ctx("OneReportable1D", *Q, "ReportableSegmentsAxis", "ReportableSegments1Member"),
            ctx("OneReportable2D", *Q, "ReportableSegmentsAxis", "ReportableSegments2Member"),
            fact("SegmentRevenue", "OneReportable1D", "500"), fact("SegmentRevenue", "OneReportable2D", "100"),
            fact("SegmentRevenue", "OneD", "0"))
    assert parse_segments(d)["reconciles"] is False
    half = doc(ctx("OneD", "2026-04-01", "2026-09-30"),
               ctx("OneReportable1D", "2026-04-01", "2026-09-30", "ReportableSegmentsAxis", "ReportableSegments1Member"),
               fact("SegmentRevenue", "OneReportable1D", "500"))
    assert parse_segments(half) is None


def test_a_filing_without_segments_gives_none():
    assert parse_segments(doc(ctx("OneD", *Q), fact("SegmentRevenue", "OneD", "10"))) is None
    assert parse_segments(b"<not-xml") is None


def test_rows_are_stored_per_segment_and_rebuilt_into_the_table(tmp_path):
    import duckdb
    import pandas as pd
    from zen.data import segments
    d = doc(ctx("OneD", *Q),
            ctx("OneReportable1D", *Q, "ReportableSegmentsAxis", "ReportableSegments1Member"),
            ctx("OneReportable2D", *Q, "ReportableSegmentsAxis", "ReportableSegments2Member"),
            fact("DescriptionOfReportableSegment", "OneReportable1D", "Steel"), fact("SegmentRevenue", "OneReportable1D", "700"),
            fact("DescriptionOfReportableSegment", "OneReportable2D", "Power"), fact("SegmentRevenue", "OneReportable2D", "300"),
            fact("SegmentRevenue", "OneD", "1000"))
    meta = {"symbol": "AAA", "consolidated": True, "period_end": "2026-06-30", "broadcast_dt": "2026-07-20 18:00", "xbrl_url": "u1"}
    rows = segments.rows_for(meta, parse_segments(d))
    assert [(r["name"], r["revenue"], r["reconciles"]) for r in rows] == [("Steel", 700, True), ("Power", 300, True)]
    assert segments.rows_for({**meta, "period_end": "2026-03-31"}, parse_segments(d)) == []     # another quarter's filing
    segments.write_parquet(pd.DataFrame(rows), tmp_path)
    segments.write_parquet(pd.DataFrame(rows), tmp_path)                                        # written twice, kept once
    con = duckdb.connect()
    assert segments.rebuild(con, tmp_path) == 2
    assert con.execute("SELECT sum(revenue) FROM segments").fetchone()[0] == 1000
