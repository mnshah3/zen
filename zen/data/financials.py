"""Quarterly financials from NSE's XBRL filings.

Every quarterly result is filed as structured XBRL, which means revenue,
expenses and profit arrive as tagged numbers rather than a PDF to scrape.
This is what makes valuation screens possible: P/E, EV/EBITDA and margin
history all come from here.

Two things this module refuses to get wrong.

Point-in-time. Each row carries `broadcast_dt` -- when NSE published it -- as
well as the period it describes. A screen asking what was knowable on a date
must filter on `broadcast_dt`, never on period end. Results for the quarter
ending 31 December are typically published in late January or February; a
backtest that assumes they were available on 31 December has invented six
weeks of foresight.

Exceptional items. Indian companies book one-off land sales and write-offs
above the profit line routinely enough that raw net profit is a poor guide to
earning power. `profit_normalised` strips them; `profit_reported` keeps them.
Both are stored, so a screen can choose and the choice is visible.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from pathlib import Path
from xml.etree import ElementTree as ET

import pandas as pd

from zen.data.bhavcopy import _session

log = logging.getLogger(__name__)

LISTING = ("https://www.nseindia.com/api/corporates-financial-results"
           "?index=equities&period={period}")
WARMUP = "https://www.nseindia.com/companies-listing/corporate-filings-financial-results"

PARQUET_DIR = Path("data/financials")

# XBRL tag -> our column. Names are Ind-AS taxonomy tags, stable across filers.
TAGS = {
    "RevenueFromOperations": "revenue",
    "OtherIncome": "other_income",
    "Income": "total_income",
    "CostOfMaterialsConsumed": "materials",
    "EmployeeBenefitExpense": "employee_cost",
    "FinanceCosts": "finance_costs",
    "DepreciationDepletionAndAmortisationExpense": "depreciation",
    "OtherExpenses": "other_expenses",
    "Expenses": "total_expenses",
    "ProfitBeforeExceptionalItemsAndTax": "pbt_before_exceptional",
    "ExceptionalItemsBeforeTax": "exceptional_items",
    "ProfitBeforeTax": "pbt",
    "TaxExpense": "tax",
    "ProfitLossForPeriodFromContinuingOperations": "profit_continuing",
    "ProfitLossForPeriod": "profit_reported",
    "BasicEarningsLossPerShareFromContinuingOperations": "eps_basic",
    "DilutedEarningsLossPerShareFromContinuingOperations": "eps_diluted",
    "PaidUpValueOfEquityShareCapital": "equity_capital",
    "FaceValueOfEquityShareCapital": "face_value",
}

COLUMNS = [
    "symbol", "isin", "company", "period_start", "period_end", "quarter",
    "broadcast_dt", "consolidated", "audited", "is_bank",
    *dict.fromkeys(TAGS.values()),
    "ebitda", "profit_normalised", "shares_implied", "xbrl_url",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS financials (
    symbol        VARCHAR NOT NULL,
    isin          VARCHAR,
    company       VARCHAR,
    period_start  DATE,
    period_end    DATE    NOT NULL,
    quarter       VARCHAR,
    broadcast_dt  TIMESTAMP NOT NULL,
    consolidated  BOOLEAN,
    audited       BOOLEAN,
    is_bank       BOOLEAN,
    revenue       DOUBLE, other_income DOUBLE, total_income DOUBLE,
    materials     DOUBLE, employee_cost DOUBLE, finance_costs DOUBLE,
    depreciation  DOUBLE, other_expenses DOUBLE, total_expenses DOUBLE,
    pbt_before_exceptional DOUBLE, exceptional_items DOUBLE,
    pbt           DOUBLE, tax DOUBLE,
    profit_continuing DOUBLE, profit_reported DOUBLE,
    eps_basic     DOUBLE, eps_diluted DOUBLE,
    equity_capital DOUBLE, face_value DOUBLE,
    ebitda        DOUBLE, profit_normalised DOUBLE, shares_implied DOUBLE,
    xbrl_url      VARCHAR,
    PRIMARY KEY (symbol, period_end, consolidated)
);
"""


def _dt(raw: str) -> datetime | None:
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y"):
        try:
            return datetime.strptime((raw or "").strip(), fmt)
        except ValueError:
            continue
    return None


def _d(raw: str) -> date | None:
    v = _dt(raw)
    return v.date() if v else None


def listing(period: str = "Quarterly", session=None) -> pd.DataFrame:
    """Every result filing NSE currently exposes, with its XBRL link."""
    s = session or _session()
    s.get(WARMUP, timeout=25)
    r = s.get(LISTING.format(period=period), timeout=60)
    r.raise_for_status()
    payload = r.json()
    rows = payload if isinstance(payload, list) else payload.get("data", [])

    out = []
    for row in rows:
        bdt = _dt(row.get("broadCastDate") or row.get("filingDate") or "")
        sym = (row.get("symbol") or "").strip()
        # NSE writes "-" where no XBRL was filed, which is truthy and produced
        # a URL ending in /- that 404s. Mostly suspended or non-compliant names.
        xbrl = (row.get("xbrl") or "").strip()
        if not bdt or not sym or xbrl in ("", "-"):
            continue
        out.append({
            "symbol": sym,
            "isin": (row.get("isin") or "").strip() or None,
            "company": (row.get("companyName") or "").strip() or None,
            "period_start": _d(row.get("fromDate") or ""),
            "period_end": _d(row.get("toDate") or ""),
            "quarter": (row.get("relatingTo") or "").strip() or None,
            "broadcast_dt": bdt,
            "consolidated": str(row.get("consolidated", "")).lower().startswith("cons"),
            "audited": str(row.get("audited", "")).lower().startswith("audit"),
            "is_bank": str(row.get("bank", "N")).upper() == "Y",
            "xbrl_url": xbrl,
        })
    return pd.DataFrame(out)


def parse_xbrl(content: bytes) -> dict:
    """Pull the P&L out of one Ind-AS XBRL document.

    A filing carries several contexts -- the quarter just ended, the year-ago
    quarter, cumulative periods, standalone and consolidated. Each fact is
    kept against its context, and the context covering the shortest, most
    recent period wins, which is the quarter being reported.
    """
    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        log.warning("unparseable XBRL: %s", e)
        return {}

    # context id -> (start, end) for duration contexts
    spans: dict[str, tuple[date, date]] = {}
    for ctx in root.iter():
        if not ctx.tag.endswith("}context") and ctx.tag != "context":
            continue
        cid = ctx.get("id")
        start = end = None
        for node in ctx.iter():
            tag = node.tag.split("}")[-1]
            if tag == "startDate" and node.text:
                start = node.text.strip()
            elif tag == "endDate" and node.text:
                end = node.text.strip()
        if cid and start and end:
            try:
                spans[cid] = (date.fromisoformat(start), date.fromisoformat(end))
            except ValueError:
                continue
    if not spans:
        return {}

    # Shortest span, latest end: the quarter just reported rather than a
    # cumulative nine-month or year-ago figure.
    best = sorted(spans.items(), key=lambda kv: ((kv[1][1] - kv[1][0]).days, -kv[1][1].toordinal()))
    wanted = {cid for cid, (s, e) in spans.items()
              if (e - s).days == (best[0][1][1] - best[0][1][0]).days
              and e == max(v[1] for v in spans.values())}

    facts: dict[str, float] = {}
    for node in root.iter():
        name = node.tag.split("}")[-1]
        col = TAGS.get(name)
        if not col or node.get("contextRef") not in wanted or not node.text:
            continue
        try:
            value = float(re.sub(r"[,\s]", "", node.text))
        except ValueError:
            continue
        facts.setdefault(col, value)
    return facts


def _derive(row: dict) -> dict:
    """Figures a screen needs that the filing does not state directly."""
    rev = row.get("revenue")
    pbt_pre = row.get("pbt_before_exceptional")
    fin = row.get("finance_costs") or 0.0
    dep = row.get("depreciation") or 0.0
    other_inc = row.get("other_income") or 0.0

    # Operating EBITDA: profit before exceptional items, adding back interest
    # and depreciation, less other income -- which is usually treasury yield
    # rather than the business earning anything.
    row["ebitda"] = (pbt_pre + fin + dep - other_inc) if pbt_pre is not None else None

    profit = row.get("profit_continuing")
    if profit is None:
        profit = row.get("profit_reported")
    exc = row.get("exceptional_items") or 0.0
    # Exceptional items sit above tax; removing them gross overstates slightly,
    # which is the conservative direction for a value screen.
    row["profit_normalised"] = (profit - exc) if profit is not None else None
    row["profit_reported"] = row.get("profit_reported") or profit

    eps = row.get("eps_basic")
    row["shares_implied"] = (profit / eps) if (eps and profit is not None and eps != 0) else None
    return row


def fetch(limit: int | None = None, period: str = "Quarterly",
          since: date | None = None) -> pd.DataFrame:
    """Listing plus parsed XBRL for each filing."""
    s = _session()
    index = listing(period=period, session=s)
    if index.empty:
        return pd.DataFrame(columns=COLUMNS)
    if since is not None:
        index = index[index["broadcast_dt"].dt.date >= since]
    index = index.sort_values("broadcast_dt", ascending=False)
    if limit:
        index = index.head(limit)

    rows, failed = [], 0
    for meta in index.to_dict("records"):
        try:
            r = s.get(meta["xbrl_url"], timeout=40)
            r.raise_for_status()
            facts = parse_xbrl(r.content)
        except Exception as e:
            log.debug("%s: xbrl fetch failed (%s)", meta["symbol"], e)
            failed += 1
            continue
        if not facts:
            failed += 1
            continue
        rows.append(_derive({**meta, **facts}))

    log.info("financials: %d parsed, %d unusable of %d", len(rows), failed, len(index))
    if not rows:
        return pd.DataFrame(columns=COLUMNS)
    return pd.DataFrame(rows).reindex(columns=COLUMNS)


def ensure_schema(con) -> None:
    con.execute(SCHEMA)


def upsert(con, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    ensure_schema(con)
    con.register("incoming_fin", df)
    before = con.execute("SELECT count(*) FROM financials").fetchone()[0]
    con.execute("INSERT OR IGNORE INTO financials SELECT * FROM incoming_fin")
    after = con.execute("SELECT count(*) FROM financials").fetchone()[0]
    con.unregister("incoming_fin")
    return after - before


def write_parquet(df: pd.DataFrame, out_dir: Path = PARQUET_DIR) -> list[Path]:
    if df.empty:
        return []
    written = []
    quarters = df["period_end"].map(lambda d: f"{d:%Y-%m}" if pd.notna(d) else "unknown")
    for q, chunk in df.groupby(quarters):
        p = out_dir / f"{q}.parquet"
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            chunk = pd.concat([pd.read_parquet(p), chunk], ignore_index=True)
        chunk = chunk.drop_duplicates(subset=["symbol", "period_end", "consolidated"],
                                      keep="last")
        chunk.to_parquet(p, index=False, compression="zstd")
        written.append(p)
    return written


def rebuild_from_parquet(con, out_dir: Path = PARQUET_DIR) -> int:
    ensure_schema(con)
    if not out_dir.exists():
        return 0
    con.execute("DELETE FROM financials")
    con.execute(
        f"INSERT INTO financials SELECT * FROM "
        f"read_parquet('{out_dir}/*.parquet', union_by_name=true)"
    )
    return con.execute("SELECT count(*) FROM financials").fetchone()[0]


def known_at(con, asof: date, consolidated: bool | None = True) -> pd.DataFrame:
    """The latest results per company that had actually been published by asof.

    This is the point-in-time gate. Filtering on broadcast_dt rather than
    period end is the difference between a screen that could have been run on
    the day and one that quietly knows the future.
    """
    ensure_schema(con)
    sql = """
        SELECT * FROM (
            SELECT *, row_number() OVER (
                PARTITION BY symbol ORDER BY period_end DESC, broadcast_dt DESC
            ) AS rn
            FROM financials
            WHERE CAST(broadcast_dt AS DATE) <= ?
    """
    params: list = [asof]
    if consolidated is not None:
        sql += " AND consolidated = ?"
        params.append(consolidated)
    sql += ") WHERE rn = 1"
    return con.execute(sql, params).df()
