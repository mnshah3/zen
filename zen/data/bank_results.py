"""Lenders' quarterly results, read from NSE's banking-format XBRL filings.

WHY A SEPARATE TABLE

Lenders that file in the banking format (SBI, HDFC Bank, the small finance banks) have only
total income in `financials`: the Ind-AS map in financials.TAGS finds nothing else in their
documents. zen's strategies read `financials` through the point-in-time engine, so putting
bank profit there would change what the strategies see. These figures go to their own
table, `bank_results`, which only the research pages and the MCP server read.

WHAT IS READ

The documents are the ones `financials` already lists for lenders: rows with total income
and no revenue from operations, each with its `xbrl_url`. Every document goes through
financials.parse_xbrl -- the same period rules (the quarter, not a cumulative span; company
totals, not segments) -- with BANK_TAGS instead of the Ind-AS map. Downloads go through
xbrl_cache, so a document is fetched from NSE once.

Insurers share the "total income only" pattern in `financials` but file a third format; their
documents carry none of these fields. They are kept with `has_figures` false, so they are not
fetched again, and the research pages ignore them.

BALANCE SHEET

The half-year filings (September and March) also carry the balance sheet at the period end:
capital, reserves and surplus (together the shareholders' equity), deposits, advances,
borrowings and total assets. They are empty in June and December filings.

UNITS

Amounts are in rupees, as in `financials`. The NPA ratios are fractions as filed (0.0117 is
1.17%). Expenses excluding provisions include interest expended: total income minus them is
the operating profit before provisions.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import pandas as pd

from zen.data import financials, xbrl_cache

log = logging.getLogger(__name__)

OUT_DIR = Path("data/bank_results")

BANK_TAGS = {
    "InterestEarned": "interest_earned",
    "InterestExpended": "interest_expended",
    "OtherIncome": "other_income",
    "Income": "total_income",
    "ExpenditureExcludingProvisionsAndContingencies": "expenses_ex_provisions",
    "OperatingProfitBeforeProvisionAndContingencies": "operating_profit",
    "ProvisionsOtherThanTaxAndContingencies": "provisions",
    "ProfitLossFromOrdinaryActivitiesBeforeTax": "pbt",
    "TaxExpense": "tax",
    "ProfitLossForThePeriod": "profit_reported",
    "ProfitLossAfterTaxesMinorityInterestAndShareOfProfitLossOfAssociates": "profit_owners",
    "BasicEarningsPerShareBeforeExtraordinaryItems": "eps_basic",
    "DilutedEarningsPerShareBeforeExtraordinaryItems": "eps_diluted",
    "PercentageOfGrossNpa": "gross_npa_pct",
    "PercentageOfNpa": "net_npa_pct",
    "PaidUpValueOfEquityShareCapital": "equity_capital",
    "FaceValueOfEquityShareCapital": "face_value",
    # balance sheet: in the half-year filings (September and March), at the balance-sheet date
    "Capital": "capital",
    "ReservesAndSurplus": "reserves",
    "Deposits": "deposits",
    "Advances": "advances",
    "Borrowings": "borrowings",
    "Assets": "total_assets",
}
FIGURES = list(dict.fromkeys(BANK_TAGS.values()))
COLUMNS = ["symbol", "company", "period_end", "broadcast_dt", "consolidated", *FIGURES,
           "quarter_span_days", "has_figures", "xbrl_url"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS bank_results (
    symbol VARCHAR, company VARCHAR, period_end DATE, broadcast_dt TIMESTAMP, consolidated BOOLEAN,
    interest_earned DOUBLE, interest_expended DOUBLE, other_income DOUBLE, total_income DOUBLE,
    expenses_ex_provisions DOUBLE, operating_profit DOUBLE, provisions DOUBLE, pbt DOUBLE, tax DOUBLE,
    profit_reported DOUBLE, profit_owners DOUBLE, eps_basic DOUBLE, eps_diluted DOUBLE,
    gross_npa_pct DOUBLE, net_npa_pct DOUBLE, equity_capital DOUBLE, face_value DOUBLE,
    capital DOUBLE, reserves DOUBLE, deposits DOUBLE, advances DOUBLE, borrowings DOUBLE, total_assets DOUBLE,
    quarter_span_days INTEGER, has_figures BOOLEAN, xbrl_url VARCHAR PRIMARY KEY
)
"""


def ensure_schema(con) -> None:
    con.execute(SCHEMA)


def documents(con) -> pd.DataFrame:
    """The lenders' (and insurers') filings `financials` already holds: total income, no revenue."""
    return con.execute(
        """
        SELECT symbol, company, period_end, broadcast_dt, consolidated, xbrl_url
        FROM financials
        WHERE revenue IS NULL AND total_income IS NOT NULL AND xbrl_url IS NOT NULL
        ORDER BY broadcast_dt
        """).df()


def stored_urls(out_dir: Path = OUT_DIR) -> set[str]:
    files = sorted(out_dir.glob("*.parquet")) if out_dir.exists() else []
    if not files:
        return set()
    return set(pd.concat([pd.read_parquet(f, columns=["xbrl_url"]) for f in files])["xbrl_url"])


def parse(content: bytes) -> dict:
    """The bank figures of one document for its reported quarter (empty when none are tagged)."""
    return financials.parse_xbrl(content, tags=BANK_TAGS)


def _download(session, url: str) -> bytes | None:
    cached = xbrl_cache.get(url)
    if cached is not None:
        return cached
    r = session.get(url, timeout=60)
    r.raise_for_status()
    if not xbrl_cache._well_formed(r.content):
        # an error page served with a 200: treat as a failed download, so the next run retries it
        # rather than storing the document as one with no figures
        raise ValueError("not an XBRL document")
    xbrl_cache.put(url, r.content)
    return r.content


def fetch(todo: pd.DataFrame, session, pause: float = 0.3) -> tuple[pd.DataFrame, int]:
    """Parse every document in `todo` (rows of documents()). Returns (rows, failed downloads).
    A failed download is left out, so the next run tries it again."""
    rows, failed = [], 0
    for i, meta in enumerate(todo.to_dict("records"), 1):
        hit = xbrl_cache.get(meta["xbrl_url"]) is not None
        try:
            content = _download(session, meta["xbrl_url"])
        except Exception as e:                                       # noqa: BLE001
            failed += 1
            log.debug("bank results: %s not fetched (%s)", meta["xbrl_url"], e)
            continue
        facts = parse(content) if content else {}
        row = {**meta, **{k: facts.get(k) for k in FIGURES}, "quarter_span_days": facts.get("quarter_span_days")}
        row["has_figures"] = facts.get("interest_earned") is not None or facts.get("profit_reported") is not None
        rows.append(row)
        if not hit:
            time.sleep(pause)
        if i % 100 == 0:
            log.info("bank results: %d of %d documents read", i, len(todo))
    df = pd.DataFrame(rows, columns=COLUMNS)
    return df, failed


def write(df: pd.DataFrame, out_dir: Path = OUT_DIR) -> list[Path]:
    if df.empty:
        return []
    written = []
    years = pd.to_datetime(df["period_end"]).dt.year
    for year, chunk in df.groupby(years):
        p = out_dir / f"{int(year)}.parquet"
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            chunk = pd.concat([pd.read_parquet(p), chunk], ignore_index=True)
        chunk = chunk.reindex(columns=COLUMNS).drop_duplicates(subset=["xbrl_url"], keep="last")
        chunk = chunk.sort_values(["symbol", "period_end", "consolidated", "xbrl_url"])
        chunk.to_parquet(p, index=False, compression="zstd")
        written.append(p)
    return written


def rebuild(con, out_dir: Path = OUT_DIR) -> int:
    # a table built before a column was added is replaced, not altered: it is rebuilt from the
    # parquet files every time anyway
    cols = [r[0] for r in con.execute("SELECT column_name FROM information_schema.columns "
                                      "WHERE table_name = 'bank_results'").fetchall()]
    if cols and set(cols) != set(COLUMNS):
        con.execute("DROP TABLE bank_results")
    ensure_schema(con)
    if not out_dir.exists() or not any(out_dir.glob("*.parquet")):
        return 0
    con.execute("DELETE FROM bank_results")
    cols = ", ".join(COLUMNS)
    con.execute(f"INSERT OR IGNORE INTO bank_results ({cols}) "
                f"SELECT {cols} FROM read_parquet('{out_dir}/*.parquet', union_by_name=true)")
    return con.execute("SELECT count(*) FROM bank_results").fetchone()[0]
