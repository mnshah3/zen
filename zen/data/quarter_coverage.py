"""Does every quarter hold the companies it should? The coverage gate of the v3 spec (section 15.2).

WHY THIS EXISTS

The March 2025 quarter once held 1,516 companies against about 2,100 before it
and 2,200 after. The listing code had stopped at the first page that failed to
load, and a short list in results season looks exactly like a normal one. Every
strategy reads this archive, and a company missing from one quarter is missing
from every eight-quarter window that contains it, so a gap that nobody can see
is a bias that nobody can see.

THE MEASURE

A HOLE is a company that has a stored filing for the quarter before and the
quarter after, on the same basis, and none for this quarter. A company that is
simply new or gone is not a hole: its neighbours are missing too. The HOLE SHARE
of a quarter and basis is the holes over the companies that have both
neighbours. Hole counts, not company counts, because company counts move with
listings and delistings and a hole share does not.

"Stored filing" means any row for the company, quarter and basis, including a
filing whose figures a later rule refused (strategy v3, Clarification 38 of the
v1 spec): the gate asks whether the archive HOLDS the document, and whether its
figures are usable is a separate question.

THE GATE

Standalone quarters are tested from June 2018; consolidated quarters from
September 2019, because quarterly consolidated results were not required before
the June 2019 quarter, so a consolidated "hole" before then is not a gap. Both
run up to the last quarter that has a quarter after it. A quarter whose hole
share is above 3% passes only if its holes are DOCUMENTED PERMANENT GAPS:
filings NSE does not carry, confirmed against NSE's own date-range listing and
a per-company query, listed one company at a time in
data/reference/quarter_source_gaps.csv (one row per hole, with both answers and
the date it was confirmed). Nothing fills such a gap from a second source, and
nothing is invented.

Read-only. It touches no prices and computes no return.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd

from zen.data import financials

GATE = 0.03
STANDALONE_FROM = "2018-06"
CONSOLIDATED_FROM = "2019-09"
GAPS_FILE = Path(__file__).resolve().parents[2] / "data" / "reference" / "quarter_source_gaps.csv"


def quarter_label(q: int) -> str:
    """'YYYY-MM' of the quarter-end month for the index year*4 + (month-1)//3."""
    y, m = divmod(int(q), 4)
    return f"{y}-{3 * m + 3:02d}"


def stored_quarters(files=None) -> pd.DataFrame:
    """One row per (symbol, basis, quarter index) the archive holds a filing for.

    Deduplicated exactly as financials.rebuild_from_parquet does it, one row per
    document with the latest period_end winning, so this describes the archive
    the database is built from. Quarters before March 2018 are left out: the
    first filing was broadcast in May 2018.
    """
    files = list(files) if files is not None else financials.statement_files()
    listed = ", ".join(f"'{Path(p).as_posix()}'" for p in files)
    con = duckdb.connect()
    try:
        return con.execute(f"""
            SELECT DISTINCT symbol, consolidated,
                   year(period_end) * 4 + (month(period_end) - 1) // 3 AS q
            FROM (SELECT *, row_number() OVER (PARTITION BY xbrl_url
                                               ORDER BY period_end DESC, broadcast_dt DESC) AS rn
                  FROM read_parquet([{listed}], union_by_name=true)
                  WHERE xbrl_url IS NOT NULL)
            WHERE rn = 1 AND period_end BETWEEN DATE '2018-03-31' AND DATE '2100-12-31'""").df()
    finally:
        con.close()


def quarter_table(stored: pd.DataFrame) -> pd.DataFrame:
    """Per quarter and basis: companies held, companies with both neighbours,
    holes, hole share, and the company count against the mean of its neighbours."""
    rows = []
    for cons, g in stored.groupby("consolidated"):
        have = set(zip(g["symbol"], g["q"]))
        symbols = g["symbol"].unique()
        quarters = sorted(g["q"].unique())
        held = {q: sum((s, q) in have for s in symbols) for q in quarters}
        for q in quarters:
            both = [s for s in symbols if (s, q - 1) in have and (s, q + 1) in have]
            holes = sorted(s for s in both if (s, q) not in have)
            nb = [held[x] for x in (q - 1, q + 1) if x in held]
            rows.append(dict(
                quarter=quarter_label(q), basis="cons" if cons else "sa", companies=held[q],
                both=len(both), holes=len(holes), hole_list=tuple(holes),
                hole_share=(len(holes) / len(both)) if both else None,
                vs_neighbours=(held[q] / (sum(nb) / len(nb))) if len(nb) == 2 else None))
    return pd.DataFrame(rows).sort_values(["basis", "quarter"]).reset_index(drop=True)


def tested(table: pd.DataFrame) -> pd.DataFrame:
    """The rows the gate applies to: standalone from June 2018, consolidated
    from September 2019, and only where both neighbours exist (so the last
    quarter, which has none after it, is out)."""
    start = table["basis"].map({"sa": STANDALONE_FROM, "cons": CONSOLIDATED_FROM})
    return table[(table["quarter"] >= start) & table["hole_share"].notna()]


def documented_gaps(path: Path = GAPS_FILE) -> pd.DataFrame:
    """The permanent gaps on record: quarter, basis, symbol, how NSE answered."""
    return pd.read_csv(path, dtype=str)


def gate_failures(table: pd.DataFrame, gaps: pd.DataFrame, limit: float = GATE) -> list[str]:
    """Quarters above the limit whose holes are not all documented permanent gaps."""
    known = set(zip(gaps["quarter"], gaps["basis"], gaps["symbol"]))
    bad = []
    for r in tested(table).itertuples():
        if r.hole_share <= limit:
            continue
        undocumented = [s for s in r.hole_list if (r.quarter, r.basis, s) not in known]
        if undocumented:
            bad.append(f"{r.quarter} {r.basis}: hole share {r.hole_share:.1%} ({r.holes} of {r.both}); "
                       f"{len(undocumented)} not documented, e.g. {', '.join(undocumented[:5])}")
    return bad
