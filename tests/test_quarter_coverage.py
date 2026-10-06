"""The quarter coverage gate (strategy v3 spec, section 15.2), as a test.

The March 2025 quarter once held 1,516 companies against about 2,100 either
side of it, and nothing noticed for weeks. This is the check that would have.

For every quarter and basis, the HOLE SHARE is the companies with a stored
filing for the quarter before and the quarter after (same basis) but none for
this quarter, over the companies with both neighbours. It is tested for
standalone figures from June 2018 and consolidated figures from September 2019
(quarterly consolidated results were not required before the June 2019
quarter), up to the last quarter that has a quarter after it. A quarter above
3% passes only if every one of its holes is a DOCUMENTED PERMANENT GAP: a filing
NSE does not carry, listed one company at a time in
data/reference/quarter_source_gaps.csv. Nothing is invented to close a gap.

The company count against the mean of its two neighbours is checked alongside
(at least 90%), because it is the plainest way to see a short quarter.

It reads the committed parquet through zen.data.quarter_coverage, so it runs on
a fresh clone with no database. It touches no prices and computes no return.
"""

from __future__ import annotations

import pandas as pd
import pytest

from zen.data import quarter_coverage as qc

NEIGHBOUR_FLOOR = 0.90


@pytest.fixture(scope="module")
def table():
    return qc.quarter_table(qc.stored_quarters())


@pytest.fixture(scope="module")
def gaps():
    return qc.documented_gaps()


# ---------------------------------------------------------------- the real archive

def test_no_quarter_above_the_hole_gate_without_documented_gaps(table, gaps):
    bad = qc.gate_failures(table, gaps)
    assert not bad, ("quarters above 3% whose holes are not documented permanent gaps "
                     "(refill them with jobs.backfill_quarter, or confirm NSE does not carry "
                     "them and add them to data/reference/quarter_source_gaps.csv):\n  "
                     + "\n  ".join(bad))


def test_no_quarter_short_of_its_neighbours_without_documented_gaps(table, gaps):
    documented = set(zip(gaps["quarter"], gaps["basis"]))
    t = qc.tested(table)
    undocumented = pd.Series([(q, b) not in documented for q, b in zip(t["quarter"], t["basis"])],
                             index=t.index, dtype=bool)
    short = t[(t["vs_neighbours"] < NEIGHBOUR_FLOOR) & undocumented]
    assert short.empty, "quarters under 90% of their neighbours' mean, undocumented:\n" + \
        short[["quarter", "basis", "companies", "vs_neighbours"]].to_string(index=False)


def test_every_documented_gap_is_still_a_hole(table, gaps):
    """The list cannot rot: a gap NSE later fills, or a name that is not a hole,
    has to come out of the file."""
    holes = {(r.quarter, r.basis, s) for r in table.itertuples() for s in r.hole_list}
    stale = sorted(set(zip(gaps["quarter"], gaps["basis"], gaps["symbol"])) - holes)
    assert not stale, f"documented gaps that are no longer holes ({len(stale)}): {stale[:10]}"


# What NSE can answer for a company and quarter, none of which is a usable document we could still fetch:
#   not_listed          the filing is not in the listing at all
#   listed_no_document  the listing carries the filing but no XBRL document ("-")
#   listed_document_404 the listing names a document and NSE's archive answers 404 (kept in
#                       data/financials/legacy_missing.parquet)
#   document_shared_with_later_quarter
#                       NSE lists ONE document for this quarter and a later one; the archive keeps a
#                       document once, under the later quarter (financials.rebuild_from_parquet)
#   held_under_renamed_symbol
#                       the company was renamed after most of its filings were stored; NSE now lists
#                       everything under the new symbol and the quarter is held under it (SANGINITA to
#                       AGASTYAEN, 25 Sep 2026, ISIN INE753W01010)
ANSWERS = {"not_listed", "listed_no_document", "listed_document_404", "document_shared_with_later_quarter",
           "held_under_renamed_symbol"}


def test_the_gap_file_is_well_formed(gaps):
    assert list(gaps.columns) == ["quarter", "basis", "symbol", "nse_answer", "listing_answer", "confirmed"]
    assert set(gaps["basis"]) <= {"sa", "cons"}
    assert set(gaps["nse_answer"]) <= ANSWERS and set(gaps["listing_answer"]) <= ANSWERS
    # a gap is confirmed only if the per-company query and the date-range listing agree that there is nothing to fetch
    assert (gaps["nse_answer"] == gaps["listing_answer"]).all()
    assert gaps["confirmed"].str.fullmatch(r"\d{4}-\d{2}-\d{2}").all()
    assert not gaps.duplicated(["quarter", "basis", "symbol"]).any()
    assert gaps["quarter"].str.fullmatch(r"\d{4}-(03|06|09|12)").all()


def test_documented_gaps_are_only_for_quarters_the_gate_tests(gaps):
    for r in gaps.itertuples():
        start = qc.STANDALONE_FROM if r.basis == "sa" else qc.CONSOLIDATED_FROM
        assert r.quarter >= start, f"{r.quarter} {r.basis} {r.symbol} is outside the gate's range"


# ---------------------------------------------------------------- the gate itself

def _stored(rows):
    """rows: (symbol, consolidated, quarter-index)."""
    return pd.DataFrame(rows, columns=["symbol", "consolidated", "q"])


Q0 = 2019 * 4 + 3        # 2019-12; the quarters below run 2019-12 to 2021-06, all in range


def _panel(n=100, missing=(), start=Q0, quarters=7, consolidated=False):
    """n companies in every quarter, minus the (symbol, quarter-offset) pairs in `missing`."""
    return _stored([(f"S{i:03d}", consolidated, start + k) for i in range(n) for k in range(quarters)
                    if (i, k) not in set(missing)])


def test_a_complete_panel_has_no_holes():
    table = qc.quarter_table(_panel())
    assert table["holes"].sum() == 0
    assert qc.gate_failures(table, pd.DataFrame(columns=["quarter", "basis", "symbol"])) == []


def test_a_short_quarter_is_caught_and_documenting_its_holes_clears_it():
    missing = [(i, 3) for i in range(10)]                  # 10% of the companies lose one quarter
    table = qc.quarter_table(_panel(missing=missing))
    none = pd.DataFrame(columns=["quarter", "basis", "symbol"])
    bad = qc.gate_failures(table, none)
    assert len(bad) == 1 and "10.0%" in bad[0] and "S000" in bad[0]
    label = qc.quarter_label(Q0 + 3)
    documented = pd.DataFrame([dict(quarter=label, basis="sa", symbol=f"S{i:03d}") for i in range(10)])
    assert qc.gate_failures(table, documented) == []
    partial = documented.iloc[:9]                          # one hole left undocumented
    assert len(qc.gate_failures(table, partial)) == 1


def test_a_quarter_at_or_under_three_percent_passes():
    table = qc.quarter_table(_panel(missing=[(i, 3) for i in range(3)]))      # 3 of 100
    assert qc.gate_failures(table, pd.DataFrame(columns=["quarter", "basis", "symbol"])) == []


def test_new_and_departed_companies_are_not_holes():
    rows = [(f"S{i:03d}", False, Q0 + k) for i in range(50) for k in range(7)]
    rows += [(f"NEW{i}", False, Q0 + k) for i in range(50) for k in range(3, 7)]     # listed in the 4th quarter
    rows += [(f"OLD{i}", False, Q0 + k) for i in range(50) for k in range(0, 3)]     # gone after the 3rd
    t = qc.quarter_table(_stored(rows))
    assert t["holes"].sum() == 0


def test_the_first_and_last_quarters_have_no_hole_share():
    t = qc.quarter_table(_panel())
    assert t["hole_share"].isna().sum() == 2 and len(qc.tested(t)) == len(t) - 2


def test_consolidated_is_tested_only_from_september_2019():
    # 20% of consolidated companies miss 2019-06: a hole before the requirement, not a gap
    q = 2019 * 4 + 1                                                           # 2019-06
    rows = [(f"S{i:03d}", True, q + k) for i in range(100) for k in range(-1, 4)
            if not (i < 20 and k == 0)]
    t = qc.quarter_table(_stored(rows))
    assert t.loc[t["quarter"] == "2019-06", "holes"].item() == 20
    assert "2019-06" not in set(qc.tested(t)["quarter"])


def test_a_company_with_only_a_refused_filing_still_counts_as_held(tmp_path):
    """The gate asks whether the archive holds the document, not whether its figures are usable."""
    rows = [dict(symbol="AAA", period_end=pd.Timestamp(d), broadcast_dt=pd.Timestamp(d) + pd.Timedelta(days=30),
                 consolidated=False, xbrl_url=f"u{d}")
            for d in ("2019-12-31", "2020-03-31", "2020-06-30")]
    path = tmp_path / "f.parquet"
    pd.DataFrame(rows).to_parquet(path)
    s = qc.stored_quarters([path])
    assert len(s) == 3
    assert qc.quarter_table(s)["holes"].sum() == 0
