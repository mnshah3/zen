"""Quarterly financials from NSE's integrated filings.

WHICH ENDPOINT, AND WHY IT MATTERS

The obvious endpoint, /api/corporates-financial-results, is a dead end: it
serves a single quarter (every row carries period end 31 Dec 2024) and
date-filtered queries return only late stragglers. Several other candidates
fail differently -- /api/corp-info is a route-level 404, /api/quote-equity is
blocked by the WAF, and /api/annual-reports returns PDF links with no figures.

/api/integrated-filing-results works. It carries both income statement and
balance sheet as tagged Ind-AS XBRL, covers roughly 1,700 companies, needs no
login, and stamps every filing with the moment NSE broadcast it.

WHAT IT CAN AND CANNOT SUPPORT -- verified, not assumed

Income statement: every quarter from the March 2025 quarter onward.
Balance sheet: HALF-YEARLY ONLY. March and September filings carry Assets,
Equity and Borrowings; June and December quarters do not. Confirmed directly
on Reliance, where debt-to-equity computes to 0.345, 0.331 and 0.344 for the
three half-years available, and the June and December filings return income
statement fields alone.

The consequence is a boundary worth stating plainly rather than discovering
later:

  A LIVE SCREEN works. Debt-to-equity, P/E, EV/EBITDA and margins can be
  computed today across the covered universe, which is what the strategy's
  hard filters need.

  A HISTORICAL BACKTEST of fundamental factors does NOT work. Three
  balance-sheet observations cannot answer whether low leverage predicted
  returns. Any claim of that kind would have to come from a source we do not
  have, and inventing one is how a backtest starts lying.

POINT-IN-TIME

`broadcast_dt` is when NSE published the filing, not when the period ended.
A December quarter is published in mid-January; a screen filtering on period
end would grant itself six weeks of foresight. known_at() filters on
broadcast_dt for exactly this reason.

EXCEPTIONAL ITEMS

Indian companies book one-off land sales and write-offs above the profit line
often enough that raw net profit is a poor guide to earning power.
`profit_normalised` strips them, `profit_reported` keeps them, and both are
stored so a screen's choice is visible rather than implicit.
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

WARMUP = "https://www.nseindia.com/companies-listing/corporate-filings-financial-results"
BY_SYMBOL = ("https://www.nseindia.com/api/integrated-filing-results"
             "?index=equities&symbol={symbol}")
BY_RANGE = ("https://www.nseindia.com/api/integrated-filing-results"
            "?index=equities&from_date={frm}&to_date={to}&size={size}&page={page}")

PARQUET_DIR = Path("data/financials")

# Ind-AS taxonomy tags. Balance-sheet tags appear only in March and September
# filings; their absence in a June or December filing is expected, not an error.
TAGS = {
    # income statement
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
    # balance sheet -- half-yearly only
    "BorrowingsNoncurrent": "debt_long",
    "BorrowingsCurrent": "debt_short",
    "Equity": "equity",
    "EquityShareCapital": "equity_capital",
    "OtherEquity": "other_equity",
    "Assets": "assets",
    "Liabilities": "liabilities",
    "CurrentAssets": "current_assets",
    "CurrentLiabilities": "current_liabilities",
    "NoncurrentAssets": "noncurrent_assets",
    "NoncurrentLiabilities": "noncurrent_liabilities",
}

DERIVED = ["ebitda", "profit_normalised", "shares_implied",
           "debt_total", "debt_to_equity", "has_balance_sheet"]

COLUMNS = ["symbol", "company", "period_end", "broadcast_dt", "consolidated",
           "audited", *dict.fromkeys(TAGS.values()), *DERIVED, "xbrl_url"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS financials (
    symbol       VARCHAR NOT NULL,
    company      VARCHAR,
    period_end   DATE    NOT NULL,
    broadcast_dt TIMESTAMP NOT NULL,
    consolidated BOOLEAN NOT NULL,
    audited      BOOLEAN,
    revenue DOUBLE, other_income DOUBLE, total_income DOUBLE,
    materials DOUBLE, employee_cost DOUBLE, finance_costs DOUBLE,
    depreciation DOUBLE, other_expenses DOUBLE, total_expenses DOUBLE,
    pbt_before_exceptional DOUBLE, exceptional_items DOUBLE,
    pbt DOUBLE, tax DOUBLE,
    profit_continuing DOUBLE, profit_reported DOUBLE,
    eps_basic DOUBLE, eps_diluted DOUBLE,
    debt_long DOUBLE, debt_short DOUBLE, equity DOUBLE,
    equity_capital DOUBLE, other_equity DOUBLE,
    assets DOUBLE, liabilities DOUBLE,
    current_assets DOUBLE, current_liabilities DOUBLE,
    noncurrent_assets DOUBLE, noncurrent_liabilities DOUBLE,
    ebitda DOUBLE, profit_normalised DOUBLE, shares_implied DOUBLE,
    debt_total DOUBLE, debt_to_equity DOUBLE, has_balance_sheet BOOLEAN,
    xbrl_url VARCHAR,
    PRIMARY KEY (symbol, period_end, consolidated)
);
"""


def _dt(raw) -> datetime | None:
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y",
                "%d-%B-%Y %H:%M:%S"):
        try:
            return datetime.strptime(str(raw or "").strip(), fmt)
        except ValueError:
            continue
    return None


def _period(raw) -> date | None:
    v = _dt(raw)
    return v.date() if v else None


def _rows(payload) -> list:
    return payload if isinstance(payload, list) else (payload or {}).get("data", [])


def listing(symbol: str | None = None, start: date | None = None,
            end: date | None = None, session=None, page_size: int = 100,
            max_pages: int = 200) -> pd.DataFrame:
    """Financial filings, by symbol or across a broadcast-date window.

    The feed mixes "Integrated Filing- Financials" with governance filings;
    only the former carry figures, so the rest are dropped here rather than
    silently producing empty XBRL parses downstream.
    """
    s = session or _session()
    s.get(WARMUP, timeout=25)

    raw = []
    if symbol:
        r = s.get(BY_SYMBOL.format(symbol=symbol), timeout=60)
        r.raise_for_status()
        raw = _rows(r.json())
    else:
        if not (start and end):
            raise ValueError("a date range is required when no symbol is given")
        for page in range(1, max_pages + 1):
            url = BY_RANGE.format(frm=f"{start:%d-%m-%Y}", to=f"{end:%d-%m-%Y}",
                                  size=page_size, page=page)
            try:
                r = s.get(url, timeout=90)
                r.raise_for_status()
                batch = _rows(r.json())
            except Exception as e:
                log.warning("page %d failed (%s)", page, e)
                break
            if not batch:
                break
            raw.extend(batch)
            if len(batch) < page_size:
                break

    out = []
    for row in raw:
        if "financial" not in str(row.get("type", "")).lower():
            continue
        bdt = _dt(row.get("broadcast_Date") or row.get("creation_Date"))
        pe = _period(row.get("qe_Date"))
        sym = (row.get("symbol") or "").strip()
        url = str(row.get("xbrl") or row.get("ixbrl") or "").strip()
        if not (bdt and pe and sym and url.startswith("http")):
            continue
        out.append({
            "symbol": sym,
            "company": (row.get("cmName") or row.get("smName") or "").strip() or None,
            "period_end": pe,
            "broadcast_dt": bdt,
            "consolidated": str(row.get("consolidated", "")).lower().startswith("cons"),
            "audited": str(row.get("audited", "")).lower().startswith("audit"),
            "xbrl_url": url,
        })
    return pd.DataFrame(out)


def parse_xbrl(content: bytes) -> dict:
    """Extract tagged figures for the period being reported.

    A filing carries several contexts: the quarter just ended, the year-ago
    quarter, cumulative periods, and instant contexts for balance-sheet items.
    Duration facts are taken from the shortest span ending latest -- the
    quarter itself rather than a nine-month cumulative -- and instant facts
    from the latest instant, which is the balance-sheet date.
    """
    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        log.debug("unparseable XBRL: %s", e)
        return {}

    spans: dict[str, tuple[date, date]] = {}
    instants: dict[str, date] = {}
    for ctx in root.iter():
        if not (ctx.tag.endswith("}context") or ctx.tag == "context"):
            continue
        cid = ctx.get("id")
        if not cid:
            continue
        start = end = inst = None
        for node in ctx.iter():
            tag = node.tag.split("}")[-1]
            txt = (node.text or "").strip()
            if tag == "startDate" and txt:
                start = txt
            elif tag == "endDate" and txt:
                end = txt
            elif tag == "instant" and txt:
                inst = txt
        try:
            if start and end:
                spans[cid] = (date.fromisoformat(start), date.fromisoformat(end))
            elif inst:
                instants[cid] = date.fromisoformat(inst)
        except ValueError:
            continue

    wanted = set()
    if spans:
        latest_end = max(v[1] for v in spans.values())
        shortest = min((v[1] - v[0]).days for v in spans.values() if v[1] == latest_end)
        wanted |= {c for c, (a, b) in spans.items()
                   if b == latest_end and (b - a).days == shortest}
    if instants:
        latest = max(instants.values())
        wanted |= {c for c, d in instants.items() if d == latest}
    if not wanted:
        return {}

    facts: dict[str, float] = {}
    for node in root.iter():
        col = TAGS.get(node.tag.split("}")[-1])
        if not col or node.get("contextRef") not in wanted or not node.text:
            continue
        try:
            facts.setdefault(col, float(re.sub(r"[,\s]", "", node.text)))
        except ValueError:
            continue
    return facts


def _derive(row: dict) -> dict:
    pbt_pre = row.get("pbt_before_exceptional")
    fin = row.get("finance_costs") or 0.0
    dep = row.get("depreciation") or 0.0
    other_inc = row.get("other_income") or 0.0
    # Operating EBITDA: other income is stripped because it is usually
    # treasury yield rather than the business earning anything.
    row["ebitda"] = (pbt_pre + fin + dep - other_inc) if pbt_pre is not None else None

    profit = row.get("profit_continuing")
    if profit is None:
        profit = row.get("profit_reported")
    exc = row.get("exceptional_items") or 0.0
    row["profit_normalised"] = (profit - exc) if profit is not None else None
    row["profit_reported"] = row.get("profit_reported") or profit

    eps = row.get("eps_basic")
    row["shares_implied"] = (profit / eps) if (eps and profit is not None and eps) else None

    dl, ds, eq = row.get("debt_long"), row.get("debt_short"), row.get("equity")
    if dl is not None or ds is not None:
        row["debt_total"] = (dl or 0.0) + (ds or 0.0)
    else:
        row["debt_total"] = None
    row["debt_to_equity"] = (row["debt_total"] / eq) if (row["debt_total"] is not None
                                                        and eq) else None
    row["has_balance_sheet"] = eq is not None and row.get("assets") is not None
    return row


def fetch(symbol: str | None = None, start: date | None = None,
          end: date | None = None, limit: int | None = None) -> pd.DataFrame:
    s = _session()
    index = listing(symbol=symbol, start=start, end=end, session=s)
    if index.empty:
        return pd.DataFrame(columns=COLUMNS)
    index = index.sort_values("broadcast_dt", ascending=False)
    if limit:
        index = index.head(limit)

    rows, failed = [], 0
    for meta in index.to_dict("records"):
        try:
            r = s.get(meta["xbrl_url"], timeout=60)
            r.raise_for_status()
            facts = parse_xbrl(r.content)
        except Exception:
            failed += 1
            continue
        if not facts:
            failed += 1
            continue
        rows.append(_derive({**meta, **facts}))

    if rows:
        df = pd.DataFrame(rows).reindex(columns=COLUMNS)
        log.info("financials: %d parsed (%d with balance sheet), %d unusable",
                 len(df), int(df["has_balance_sheet"].fillna(False).sum()), failed)
        return df
    log.warning("financials: nothing parsed of %d filings", len(index))
    return pd.DataFrame(columns=COLUMNS)


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
    for q, chunk in df.groupby(df["period_end"].map(
            lambda d: f"{d:%Y-%m}" if pd.notna(d) else "unknown")):
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
    con.execute(f"INSERT INTO financials SELECT * FROM "
                f"read_parquet('{out_dir}/*.parquet', union_by_name=true)")
    return con.execute("SELECT count(*) FROM financials").fetchone()[0]


def known_at(con, asof: date, consolidated: bool = True,
             require_balance_sheet: bool = False) -> pd.DataFrame:
    """Latest results per company that had actually been PUBLISHED by asof.

    Filtering on broadcast_dt rather than period end is the whole point. Set
    require_balance_sheet when a screen needs leverage, since only March and
    September filings carry it.
    """
    ensure_schema(con)
    sql = ["SELECT * FROM (SELECT *, row_number() OVER ("
           "PARTITION BY symbol ORDER BY period_end DESC, broadcast_dt DESC) AS rn",
           "FROM financials WHERE CAST(broadcast_dt AS DATE) <= ?"]
    params: list = [asof]
    sql.append("AND consolidated = ?")
    params.append(consolidated)
    if require_balance_sheet:
        sql.append("AND has_balance_sheet")
    sql.append(") WHERE rn = 1")
    return con.execute(" ".join(sql), params).df()
