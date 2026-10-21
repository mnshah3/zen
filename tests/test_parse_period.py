"""The XBRL parser must return the QUARTER's income statement or none at all.

THE BUG THESE PIN DOWN

parse_xbrl took duration facts from "the shortest span ending latest". When a
filing tags no quarter-length context, the shortest is a half-year or a year
and its figures were stored as the quarter: DELTACORP's first Sep-2025 filing
tags revenue only for 2025-04-01 to 2025-09-30, and 2,619.5m of half-year
revenue went into the archive as one quarter. The rule is now that duration
facts come only from a span of at most QUARTER_MAX_DAYS (100); otherwise they
are left empty and the balance sheet is still taken.

The 2018-2024 documents reference an undeclared "OneD". Its length is taken
from the document's own DateOfStart/EndOfReportingPeriod facts, or from the
declared "One..." segment contexts when those facts are missing (banks). A
OneD dated longer than a quarter is refused. A OneD that cannot be dated at all
is taken as the quarter, because in all 577 legacy documents where it can be
dated, it is one.
"""

from __future__ import annotations

from datetime import datetime

import duckdb
import pandas as pd
import pytest

from zen.data import financials as fin

HEAD = ('<?xml version="1.0" encoding="UTF-8"?>'
        '<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" '
        'xmlns:xbrldi="http://xbrl.org/2006/xbrldi" '
        'xmlns:in-bse-fin="http://www.bseindia.com/xbrl/fin/2020-03-31/in-bse-fin">')
TAIL = "</xbrli:xbrl>"


def ctx(cid, start=None, end=None, instant=None, dim=None):
    period = (f"<xbrli:instant>{instant}</xbrli:instant>" if instant else
              f"<xbrli:startDate>{start}</xbrli:startDate><xbrli:endDate>{end}</xbrli:endDate>")
    seg = ""
    if dim:
        axis, member = dim
        seg = (f'<xbrli:segment><xbrldi:explicitMember dimension="in-bse-fin:{axis}">'
               f"in-bse-fin:{member}</xbrldi:explicitMember></xbrli:segment>")
    return (f'<xbrli:context id="{cid}"><xbrli:entity><xbrli:identifier '
            f'scheme="http://www.nseindia.com">X</xbrli:identifier>{seg}</xbrli:entity>'
            f"<xbrli:period>{period}</xbrli:period></xbrli:context>")


def fact(tag, ref, value):
    return f'<in-bse-fin:{tag} contextRef="{ref}" unitRef="INR">{value}</in-bse-fin:{tag}>'


def doc(*parts) -> bytes:
    return (HEAD + "".join(parts) + TAIL).encode()


def parse(content):
    detail = {}
    return fin.parse_xbrl(content, detail), detail


# ---------------------------------------------------------------- new-endpoint

def test_quarter_alongside_half_year_takes_the_quarter():
    facts, detail = parse(doc(
        ctx("FourD", "2025-04-01", "2025-09-30"),
        ctx("OneD", "2025-07-01", "2025-09-30"),
        ctx("OneI", instant="2025-09-30"),
        fact("RevenueFromOperations", "FourD", "2619500000"),
        fact("RevenueFromOperations", "OneD", "1310500000"),
        fact("ProfitLossForPeriod", "FourD", "90"),
        fact("ProfitLossForPeriod", "OneD", "40"),
        fact("Equity", "OneI", "5000"),
    ))
    assert facts["revenue"] == 1310500000.0
    assert facts["profit_reported"] == 40.0
    assert facts["equity"] == 5000.0
    assert facts["quarter_span_days"] == 91
    assert detail["duration_source"] == "declared"
    assert detail["dropped_columns"] == []


def test_only_half_year_leaves_income_statement_empty_and_keeps_balance_sheet():
    # DELTACORP's first Sep-2025 filing, in miniature.
    facts, detail = parse(doc(
        ctx("OneD", "2025-04-01", "2025-09-30"),
        ctx("OneI", instant="2025-09-30"),
        ctx("PY_I", instant="2025-03-31"),
        fact("RevenueFromOperations", "OneD", "2619500000"),
        fact("ProfitLossForPeriod", "OneD", "90"),
        fact("ProfitBeforeExceptionalItemsAndTax", "OneD", "120"),
        fact("Equity", "OneI", "5000"),
        fact("Assets", "OneI", "9000"),
        fact("Equity", "PY_I", "4000"),
    ))
    for col in ("revenue", "profit_reported", "pbt_before_exceptional"):
        assert col not in facts
    assert "quarter_span_days" not in facts
    assert facts["equity"] == 5000.0 and facts["assets"] == 9000.0
    assert detail["rejected_span_days"] == 182
    assert {"revenue", "profit_reported", "pbt_before_exceptional"} <= set(
        detail["dropped_columns"])

    row = fin._derive({"symbol": "X", **facts})
    assert row["ebitda"] is None and row["profit_normalised"] is None
    assert row["has_balance_sheet"] is True
    assert row.get("quarter_span_days") is None


def test_only_full_year_leaves_income_statement_empty_and_keeps_balance_sheet():
    facts, detail = parse(doc(
        ctx("OneD", "2025-04-01", "2026-03-31"),
        ctx("OneI", instant="2026-03-31"),
        fact("RevenueFromOperations", "OneD", "4000"),
        fact("OtherIncome", "OneD", "40"),
        fact("Equity", "OneI", "5000"),
    ))
    assert "revenue" not in facts and "other_income" not in facts
    assert "quarter_span_days" not in facts
    assert facts == {"equity": 5000.0}
    assert detail["rejected_span_days"] == 364


def test_half_year_only_and_no_balance_sheet_parses_to_nothing():
    facts, detail = parse(doc(
        ctx("OneD", "2025-04-01", "2025-09-30"),
        fact("RevenueFromOperations", "OneD", "210"),
    ))
    assert facts == {}
    assert detail["dropped_columns"] == ["revenue"]


def test_span_of_exactly_the_limit_is_accepted_and_one_more_day_is_not():
    ok, _ = parse(doc(ctx("A", "2025-06-22", "2025-09-30"),
                      fact("RevenueFromOperations", "A", "1")))
    assert ok["quarter_span_days"] == fin.QUARTER_MAX_DAYS == 100
    refused, _ = parse(doc(ctx("A", "2025-06-21", "2025-09-30"),
                           fact("RevenueFromOperations", "A", "1")))
    assert refused == {}


def test_dimensioned_segment_contexts_are_still_ignored():
    # Segment context first in the document, same period as the company total.
    facts, _ = parse(doc(
        ctx("SegD", "2025-07-01", "2025-09-30", dim=("ReportableSegmentsAxis", "Seg1")),
        ctx("OneD", "2025-07-01", "2025-09-30"),
        fact("RevenueFromOperations", "SegD", "400"),
        fact("RevenueFromOperations", "OneD", "1000"),
    ))
    assert facts["revenue"] == 1000.0


def test_a_segment_quarter_never_stands_in_for_a_refused_half_year():
    # The only quarter-length context is a segment; the company total is H1.
    facts, detail = parse(doc(
        ctx("SegD", "2025-07-01", "2025-09-30", dim=("ReportableSegmentsAxis", "Seg1")),
        ctx("OneD", "2025-04-01", "2025-09-30"),
        ctx("OneI", instant="2025-09-30"),
        fact("RevenueFromOperations", "SegD", "400"),
        fact("RevenueFromOperations", "OneD", "2100"),
        fact("Equity", "OneI", "5000"),
    ))
    assert "revenue" not in facts
    assert facts == {"equity": 5000.0}
    assert detail["dropped_columns"] == ["revenue"]


def test_audit_axis_fallback_obeys_the_same_rule():
    audited = ("AuditedOrAdjustedAxis", "AuditedMember")
    facts, _ = parse(doc(
        ctx("AudD", "2025-04-01", "2025-09-30", dim=audited),
        ctx("AudI", instant="2025-09-30", dim=audited),
        fact("RevenueFromOperations", "AudD", "2100"),
        fact("Assets", "AudI", "9000"),
    ))
    assert "revenue" not in facts
    assert facts == {"assets": 9000.0}

    facts, _ = parse(doc(
        ctx("AudD", "2025-07-01", "2025-09-30", dim=audited),
        fact("RevenueFromOperations", "AudD", "1000"),
    ))
    assert facts == {"revenue": 1000.0, "quarter_span_days": 91}


# ---------------------------------------------------------- legacy undeclared OneD

def _legacy(start, end, *, dated=True, one_dims=(("2019-07-01", "2019-09-30"),),
            oned_rev="977", fourd_rev="1548", instant=False):
    parts = [ctx(f"OneOperatingExpenses0{i}D", a, b,
                 dim=("DetailsOfOtherExpensesAxis", f"OneOperatingExpenses0{i}Member"))
             for i, (a, b) in enumerate(one_dims, 1)]
    # The generator dates the "Four..." segment contexts with the quarter too.
    parts.append(ctx("FourOperatingExpenses01D", "2019-07-01", "2019-09-30",
                     dim=("DetailsOfOtherExpensesAxis", "FourOperatingExpenses01Member")))
    if dated:
        parts += [fact("DateOfStartOfReportingPeriod", "OneD", start),
                  fact("DateOfEndOfReportingPeriod", "OneD", end)]
    parts += [fact("RevenueFromOperations", "OneD", oned_rev),
              fact("RevenueFromOperations", "FourD", fourd_rev),
              fact("ProfitLossForPeriod", "OneD", "50")]
    if instant:
        parts.append(fact("Equity", "OneI", "700"))
    return doc(*parts)


def test_undeclared_oned_dated_as_a_quarter_is_taken():
    facts, detail = parse(_legacy("2019-07-01", "2019-09-30", instant=True))
    assert facts["revenue"] == 977.0          # never the undeclared FourD
    assert facts["equity"] == 700.0
    assert facts["quarter_span_days"] == 91
    assert detail["duration_source"] == "OneD"


def test_undeclared_oned_dated_as_a_half_year_is_refused():
    facts, detail = parse(_legacy("2019-04-01", "2019-09-30", instant=True))
    assert "revenue" not in facts and "profit_reported" not in facts
    assert facts == {"equity": 700.0}
    assert detail["rejected_span_days"] == 182


def test_undeclared_oned_without_date_facts_is_dated_by_its_segment_contexts():
    # Banks' documents carry DateOfEndOfReportingPeriod but no start date.
    facts, _ = parse(_legacy(None, None, dated=False))
    assert facts["revenue"] == 977.0 and facts["quarter_span_days"] == 91

    long_dims = (("2019-04-01", "2019-09-30"),)
    facts, _ = parse(_legacy(None, None, dated=False, one_dims=long_dims))
    assert "revenue" not in facts


def test_undeclared_oned_that_cannot_be_dated_is_taken_as_the_quarter():
    # Refusing it dropped real quarterly figures (NEUEON, GLOBE, AKSHAR). In
    # all 577 legacy documents where OneD can be dated, it is the quarter.
    facts, detail = parse(_legacy(None, None, dated=False, one_dims=(), instant=True))
    assert "revenue" in facts and facts["equity"] == 700.0
    assert detail["rejected_span_days"] is None
    assert detail.get("quarter_span_days") is None


def test_undeclared_references_other_than_oned_and_onei_stay_ignored():
    facts, _ = parse(doc(fact("RevenueFromOperations", "FourD", "1548"),
                         fact("Equity", "FourI", "1")))
    assert facts == {}


# ------------------------------------------------------------- schema and rebuild

def test_columns_added_later_are_last_in_both_definitions():
    # quarter_span_days (24 Sep 2026) and excise_duty (21 Oct 2026) arrived after the files began,
    # so they come last, in the order they arrived (see the note on COLUMNS)
    assert fin.COLUMNS[-2:] == ["quarter_span_days", "excise_duty"]
    con = duckdb.connect()
    con.execute(fin.SCHEMA)
    cols = [r[0] for r in con.execute("DESCRIBE financials").fetchall()]
    con.close()
    assert cols == fin.COLUMNS


def _frame(url, span, layout):
    r = {c: None for c in fin.COLUMNS}
    r.update(symbol="ABC", company="ABC Ltd", period_end=pd.Timestamp("2025-09-30"),
             broadcast_dt=datetime(2025, 11, 1), consolidated=True, audited=False,
             revenue=100.0, has_balance_sheet=False, xbrl_url=url, quarter_span_days=span)
    df = pd.DataFrame([r])
    for c in fin.COLUMNS:
        if c not in ("symbol", "company", "period_end", "broadcast_dt", "consolidated",
                     "audited", "has_balance_sheet", "xbrl_url"):
            df[c] = df[c].astype("float64")
    df["quarter_span_days"] = df["quarter_span_days"].astype("Int64")
    if layout == "old":
        df = df.drop(columns=["quarter_span_days"])
    return df


@pytest.mark.parametrize("first,second", [("old", "new"), ("new", "old")])
def test_rebuild_lines_up_files_with_and_without_the_column(tmp_path, first, second):
    """rebuild_from_parquet inserts POSITIONALLY from a union_by_name read."""
    _frame("u_first", 91, first).to_parquet(tmp_path / "2025-06.parquet", index=False)
    _frame("u_second", 92, second).to_parquet(tmp_path / "2025-09.parquet", index=False)
    con = duckdb.connect()
    assert fin.rebuild_from_parquet(con, tmp_path) == 2
    got = dict(con.execute("SELECT xbrl_url, quarter_span_days FROM financials").fetchall())
    rev = con.execute("SELECT DISTINCT revenue FROM financials").fetchall()
    con.close()
    expect = {"u_first": 91 if first == "new" else None,
              "u_second": 92 if second == "new" else None}
    assert got == expect
    assert rev == [(100.0,)]


# ---------------------------------------------------------------- re-parse job

def test_reparse_build_keeps_every_row_and_the_stored_values_of_failures():
    import json

    from jobs import reparse_financials as job

    old = pd.concat([_frame(u, None, "old") for u in ("ok", "h1", "gone", "ok")],
                    ignore_index=True)
    old["revenue"] = [1.0, 2619.5, 3.0, 1.0]
    old["equity"] = [None, 50.0, None, None]
    prog = pd.DataFrame([
        {"xbrl_url": "ok", "outcome": "ok", "dropped_columns": "",
         "rejected_span_days": None,
         "facts_json": json.dumps({"revenue": 5.0, "quarter_span_days": 91})},
        {"xbrl_url": "h1", "outcome": "ok", "dropped_columns": "revenue",
         "rejected_span_days": 182, "facts_json": json.dumps({"equity": 50.0})},
        {"xbrl_url": "gone", "outcome": "failed", "dropped_columns": "",
         "rejected_span_days": None, "facts_json": "{}"},
    ])
    out, acc = job.build(old, prog)
    job._check(old, out)
    assert list(out.columns) == fin.COLUMNS and len(out) == 4
    assert out["xbrl_url"].tolist() == ["ok", "h1", "gone", "ok"]
    assert out.loc[0, "revenue"] == 5.0
    assert pd.isna(out.loc[1, "revenue"]) and out.loc[1, "equity"] == 50.0
    assert out.loc[2, "revenue"] == 3.0                  # failure keeps stored value
    assert out["quarter_span_days"].tolist()[0] == 91
    assert pd.isna(out.loc[1, "quarter_span_days"]) and pd.isna(out.loc[2, "quarter_span_days"])
    assert out.loc[3, "revenue"] == 5.0                  # duplicate url, same document
    assert acc["duration_dropped"].tolist() == [False, True, False, False]
    assert acc["outcome"].tolist() == ["ok", "ok", "failed", "ok"]
    assert acc["revenue_changed"].tolist() == [True, True, False, True]


def test_declared_oned_beats_fourd_with_the_same_dates_whatever_the_order():
    # 240 of 910 sampled legacy filings declare FourD with the quarter's own
    # dates while it holds year-to-date values. The quarter must win by rule,
    # not because OneD happens to come first in the document.
    for order in ("oned_first", "fourd_first"):
        parts = [ctx("OneD", "2019-07-01", "2019-09-30"),
                 ctx("FourD", "2019-07-01", "2019-09-30")]
        facts_ = [fact("RevenueFromOperations", "OneD", "977"),
                  fact("RevenueFromOperations", "FourD", "1548"),
                  fact("OtherIncome", "FourD", "33")]
        if order == "fourd_first":
            facts_ = facts_[::-1]
        facts, _ = parse(doc(*(parts + facts_)))
        assert facts["revenue"] == 977.0, order
        # FourD's figures are year-to-date, so none of them fill a gap either.
        assert "other_income" not in facts, order


def test_excise_duty_is_read_from_the_quarters_other_expenses_breakdown():
    """ITC's Jun-2026 quarter, in the integrated filing's layout: revenue is filed gross of excise
    duty, which is a described line of other expenses. Only a line that is excise duty alone counts,
    and only in a breakdown dated the same as the quarter."""
    def doc(desc_a, desc_b, period_b=("2026-04-01", "2026-06-30")):
        ctx = lambda cid, a, b, member="": (                                    # noqa: E731
            f'<xbrli:context id="{cid}"><xbrli:entity><xbrli:identifier scheme="x">ITC</xbrli:identifier>'
            + (f'<xbrli:segment><xbrldi:explicitMember dimension="in-capmkt:DetailsOfOtherExpensesAxis">{member}</xbrldi:explicitMember></xbrli:segment>' if member else "")
            + f'</xbrli:entity><xbrli:period><xbrli:startDate>{a}</xbrli:startDate><xbrli:endDate>{b}</xbrli:endDate></xbrli:period></xbrli:context>')
        return ('<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:xbrldi="http://xbrl.org/2006/xbrldi" '
                'xmlns:in-capmkt="http://x">'
                + ctx("OneD", "2026-04-01", "2026-06-30") + ctx("OneExpenses1D", "2026-04-01", "2026-06-30", "m1")
                + ctx("OneExpenses2D", *period_b, "m2")
                + '<in-capmkt:RevenueFromOperations contextRef="OneD">295233000000</in-capmkt:RevenueFromOperations>'
                + '<in-capmkt:OtherExpenses contextRef="OneD">135746700000</in-capmkt:OtherExpenses>'
                + f'<in-capmkt:DescriptionOfOtherExpenses contextRef="OneExpenses1D">{desc_a}</in-capmkt:DescriptionOfOtherExpenses>'
                + '<in-capmkt:OtherExpenses contextRef="OneExpenses1D">104089300000</in-capmkt:OtherExpenses>'
                + f'<in-capmkt:DescriptionOfOtherExpenses contextRef="OneExpenses2D">{desc_b}</in-capmkt:DescriptionOfOtherExpenses>'
                + '<in-capmkt:OtherExpenses contextRef="OneExpenses2D">31657400000</in-capmkt:OtherExpenses>'
                + '</xbrli:xbrl>').encode()
    r = fin.parse_xbrl(doc("Excise Duty", "Other Expenses"))
    assert r["revenue"] == 295233000000 and r["other_expenses"] == 135746700000     # the company total untouched
    assert r["excise_duty"] == 104089300000                                          # 29,523 - 10,409 = 19,114 crore
    assert "excise_duty" not in fin.parse_xbrl(doc("Excise duty, VAT and TCS", "Other Expenses"))
    assert "excise_duty" not in fin.parse_xbrl(doc("Royalty, Cess &amp; Excise Duty", "Other Expenses"))
    assert fin.parse_xbrl(doc("Excise duty recovered", "Other Expenses"))["excise_duty"] == 104089300000   # Reliance's name for it
    assert fin.parse_xbrl(doc("Exise Duty", "Other Expenses"))["excise_duty"] == 104089300000              # misspelt, still excise
    assert "excise_duty" not in fin.parse_xbrl(doc("Excise Duty and Service Tax", "Other Expenses"))
    # a second excise line in another column (the year to date) is not the quarter's
    r2 = fin.parse_xbrl(doc("Excise duty on sale of goods", "Excise duty", period_b=("2025-04-01", "2026-03-31")))
    assert r2["excise_duty"] == 104089300000


def test_excise_is_the_quarters_own_breakdown_when_a_year_to_date_one_is_dated_as_the_quarter():
    """United Spirits' Dec-2022 filing dates its "Four..." (year to date) breakdown Oct to Dec while it
    holds nine months' excise (Rs 13,915 crore); the quarter's own "One..." line is Rs 3,831 crore."""
    def ctx(cid, a, b, member=""):
        return (f'<xbrli:context id="{cid}"><xbrli:entity><xbrli:identifier scheme="x">U</xbrli:identifier>'
                + (f'<xbrli:segment><xbrldi:explicitMember dimension="in-bse-fin:DetailsOfOtherExpensesAxis">{member}</xbrldi:explicitMember></xbrli:segment>' if member else "")
                + f'</xbrli:entity><xbrli:period><xbrli:startDate>{a}</xbrli:startDate><xbrli:endDate>{b}</xbrli:endDate></xbrli:period></xbrli:context>')
    doc = ('<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:xbrldi="http://xbrl.org/2006/xbrldi" xmlns:in-bse-fin="http://x">'
           + ctx("OneOperatingExpenses02D", "2022-10-01", "2022-12-31", "m2") + ctx("FourOperatingExpenses02D", "2022-10-01", "2022-12-31", "m2")
           + '<in-bse-fin:DateOfStartOfReportingPeriod contextRef="OneD">2022-10-01</in-bse-fin:DateOfStartOfReportingPeriod>'
           + '<in-bse-fin:DateOfEndOfReportingPeriod contextRef="OneD">2022-12-31</in-bse-fin:DateOfEndOfReportingPeriod>'
           + '<in-bse-fin:RevenueFromOperations contextRef="OneD">66100000000</in-bse-fin:RevenueFromOperations>'
           + '<in-bse-fin:DescriptionOfOtherExpenses contextRef="OneOperatingExpenses02D">Excise duty</in-bse-fin:DescriptionOfOtherExpenses>'
           + '<in-bse-fin:OtherExpenses contextRef="OneOperatingExpenses02D">38310000000</in-bse-fin:OtherExpenses>'
           + '<in-bse-fin:DescriptionOfOtherExpenses contextRef="FourOperatingExpenses02D">Excise duty</in-bse-fin:DescriptionOfOtherExpenses>'
           + '<in-bse-fin:OtherExpenses contextRef="FourOperatingExpenses02D">139150000000</in-bse-fin:OtherExpenses>'
           + '</xbrli:xbrl>').encode()
    r = fin.parse_xbrl(doc)
    assert r["revenue"] == 66100000000 and r["excise_duty"] == 38310000000
