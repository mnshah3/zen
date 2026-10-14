"""The balance sheet in detail, the cash flow statement and the financial year, read from
the half-year and year-end filings.

WHY A SEPARATE TABLE

`financials` holds each quarter's income statement and eleven summary balance-sheet lines,
and zen's strategies read it through the point-in-time engine. The same documents also carry
the whole balance sheet, the cash flow statement for the year to date and the dates of the
company's financial year. Those go to their own table, `statements`, which only the research
pages and the MCP server read, so nothing a strategy sees changes.

WHAT IS READ

The documents are the ones `financials` already lists with a balance sheet
(`has_balance_sheet`): the March and September filings of a company whose year ends in March.
Before Sep 2022 the filings tag no balance sheet, but from the year to Mar 2021 they tag the
cash flow statement, so the March and September filings from Sep 2020 to Mar 2022 are read
too, for their cash flow alone (`has_detail` false). Downloads go through xbrl_cache, so a
document is fetched from NSE once. From each:

  balance sheet   the instant facts at the balance-sheet date, through financials.parse_xbrl
                  with BALANCE_TAGS (NBFC_BALANCE_TAGS for a finance company's format), so the
                  period rules are exactly those of the summary lines in `financials`
  cash flow       the year to date: see below
  financial year  DateOfStartOfFinancialYear and DateOfEndOfFinancialYear, as stated
  year to date    revenue, total income, profit before tax and profit for the same span as
                  the cash flow, so a year built from four quarters can be checked against
                  the year the company filed

THE CASH FLOW IS FOR THE YEAR TO DATE

SEBI's format has one cash flow column: the financial year so far, a half-year in the
September filing and the full year in the March one. It is tagged under the context the
income statement uses for its cumulative column. That context is found from the facts, not
from its name: of the undimensioned contexts the cash flow totals refer to, the one ending
latest, and the longest of those.

Its dates are taken from what the document STATES about it (DateOfStartOfReportingPeriod and
DateOfEndOfReportingPeriod, tagged under that context) before what the context declares. The
2023 and 2024 documents declare the cumulative context with the quarter's dates while tagging
the year to date under it: Infosys's March 2023 filing declares it as 2023-01-01 to 2023-03-31
and states it, correctly, as starting 2022-04-01, and the Rs 22,467 crore of operating cash
flow under it is the year's. Where a document neither states nor declares the dates, the cash
flow is left empty rather than given a span it does not claim.

The year-end filings before Sep 2022 go further: they state the fourth quarter's dates for the
context and tag that quarter's income under it beside the whole year's cash flow. TCS's March
2021 filing has Rs 43,705 crore of revenue (the quarter) and Rs 38,802 crore of operating cash
flow (the year, opening cash Rs 8,646 crore at 31 Mar 2020) under one context dated Jan to Mar.
A cash flow ending on the financial year's last day is the year's, since SEBI asks for one only
at the half year and the year end, so it is dated from the financial year's start and the
quarter's income lines are not kept as year-to-date figures.

TWO CHECKS, STORED WITH THE FIGURES

`cf_adds_up`    operating + investing + financing + the exchange-rate effect = the net change
`cash_ties`     opening cash + the net change = closing cash

Both allow for rounding: the figures are filed in lakhs, millions or crores, so a sum of
four of them can be out by a couple of units. The unit is read from the figures themselves
(the largest power of ten dividing every total). A cash flow that fails a check is kept as
filed and marked, never adjusted.

UNITS AND SIGNS

Rupees, as in `financials`. Totals are signed as filed (an outflow is negative). The lines
under them are filed as positive amounts whichever way the cash moved: `capex_ppe` is cash
paid for property, plant and equipment, `borrowings_repaid` cash repaid. Nothing here nets
or re-signs them.
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import date
from pathlib import Path
from xml.etree import ElementTree as ET

import pandas as pd

from zen.data import financials, financials_legacy, xbrl_cache

log = logging.getLogger(__name__)

OUT_DIR = Path("data/statements")

# Commercial and industrial companies (Ind-AS, Schedule III Division II).
BALANCE_TAGS = {
    # non-current assets
    "PropertyPlantAndEquipment": "ppe",
    "CapitalWorkInProgress": "cwip",
    "InvestmentProperty": "investment_property",
    "Goodwill": "goodwill",
    "OtherIntangibleAssets": "intangibles",
    "IntangibleAssetsUnderDevelopment": "intangibles_dev",
    "InvestmentsAccountedForUsingEquityMethod": "equity_method_investments",
    "NoncurrentInvestments": "investments_noncurrent",
    "TradeReceivablesNoncurrent": "receivables_noncurrent",
    "LoansNoncurrent": "loans_noncurrent",
    "OtherNoncurrentFinancialAssets": "other_fin_assets_noncurrent",
    "NoncurrentFinancialAssets": "fin_assets_noncurrent",
    "DeferredTaxAssetsNet": "deferred_tax_assets",
    "OtherNoncurrentAssets": "other_noncurrent_assets",
    "NoncurrentAssets": "noncurrent_assets",
    # current assets
    "Inventories": "inventories",
    "CurrentInvestments": "investments_current",
    "TradeReceivablesCurrent": "receivables",
    "CashAndCashEquivalents": "cash",
    "BankBalanceOtherThanCashAndCashEquivalents": "bank_balances",
    "LoansCurrent": "loans_current",
    "OtherCurrentFinancialAssets": "other_fin_assets_current",
    "CurrentFinancialAssets": "fin_assets_current",
    "CurrentTaxAssets": "current_tax_assets",
    "OtherCurrentAssets": "other_current_assets",
    "CurrentAssets": "current_assets",
    "NoncurrentAssetsClassifiedAsHeldForSale": "assets_held_for_sale",
    "Assets": "assets",
    # equity
    "EquityShareCapital": "equity_capital",
    "OtherEquity": "other_equity",
    "EquityAttributableToOwnersOfParent": "equity_owners",
    "NonControllingInterest": "minority_interest",
    "Equity": "equity",
    # non-current liabilities
    "BorrowingsNoncurrent": "borrowings_noncurrent",
    "TradePayablesNoncurrent": "payables_noncurrent",
    "OtherNoncurrentFinancialLiabilities": "other_fin_liabilities_noncurrent",
    "NoncurrentFinancialLiabilities": "fin_liabilities_noncurrent",
    "ProvisionsNoncurrent": "provisions_noncurrent",
    "DeferredTaxLiabilitiesNet": "deferred_tax_liabilities",
    "OtherNoncurrentLiabilities": "other_noncurrent_liabilities",
    "NoncurrentLiabilities": "noncurrent_liabilities",
    # current liabilities
    "BorrowingsCurrent": "borrowings_current",
    "TradePayablesCurrent": "payables",
    "OtherCurrentFinancialLiabilities": "other_fin_liabilities_current",
    "CurrentFinancialLiabilities": "fin_liabilities_current",
    "OtherCurrentLiabilities": "other_current_liabilities",
    "ProvisionsCurrent": "provisions_current",
    "CurrentTaxLiabilities": "current_tax_liabilities",
    "CurrentLiabilities": "current_liabilities",
    "Liabilities": "liabilities",
    "EquityAndLiabilities": "equity_and_liabilities",
}

# Finance companies (Ind-AS, Schedule III Division III): assets and liabilities are split into
# financial and non-financial, not current and non-current. "FinanicalAssets" is the taxonomy's
# own spelling.
NBFC_BALANCE_TAGS = {
    "CashAndCashEquivalents": "cash",
    "BankBalanceOtherThanCashAndCashEquivalents": "bank_balances",
    "DerivativeFinancialInstrumentsFinancialAssets": "derivative_assets",
    "TradeReceivables": "receivables",
    "OtherReceivables": "other_receivables",
    "Loans": "loans",
    "Investments": "investments",
    "OtherFinancialAssets": "other_fin_assets",
    "FinanicalAssets": "fin_assets",
    "Inventories": "inventories",
    "CurrentTaxAssetsNet": "current_tax_assets",
    "DeferredTaxAssetsNet": "deferred_tax_assets",
    "InvestmentProperty": "investment_property",
    "PropertyPlantAndEquipment": "ppe",
    "CapitalWorkInProgress": "cwip",
    "IntangibleAssetsUnderDevelopment": "intangibles_dev",
    "Goodwill": "goodwill",
    "OtherIntangibleAssets": "intangibles",
    "OtherNonFinancialAssets": "other_non_fin_assets",
    "NonFinancialAssets": "non_fin_assets",
    "Assets": "assets",
    "EquityShareCapital": "equity_capital",
    "OtherEquity": "other_equity",
    "EquityAttributableToOwnersOfParent": "equity_owners",
    "NonControllingInterest": "minority_interest",
    "Equity": "equity",
    "DerivativeFinancialInstrumentsFinancialLiabilities": "derivative_liabilities",
    "Payables": "payables",
    "DebtSecurities": "debt_securities",
    "Borrowings": "borrowings",
    "Deposits": "deposits",
    "SubordinatedLiabilities": "subordinated_liabilities",
    "OtherFinancialLiabilities": "other_fin_liabilities",
    "FinancialLiabilities": "fin_liabilities",
    "CurrentTaxLiabilities": "current_tax_liabilities",
    "Provisions": "provisions",
    "DeferredTaxLiabilitiesNet": "deferred_tax_liabilities",
    "OtherNonFinancialLiabilities": "other_non_fin_liabilities",
    "NonFinancialLiabilities": "non_fin_liabilities",
    "Liabilities": "liabilities",
    "EquityAndLiabilities": "equity_and_liabilities",
}

# The same in both formats. Lease payments changed name in the 2025 taxonomy.
CASH_FLOW_TAGS = {
    "AdjustmentsForDepreciationAndAmortisationExpense": "cf_depreciation",
    "CashFlowsFromUsedInOperations": "cfo_before_tax",
    "IncomeTaxesPaidRefundClassifiedAsOperatingActivities": "taxes_paid",
    "CashFlowsFromUsedInOperatingActivities": "cfo",
    "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities": "capex_ppe",
    "ProceedsFromSalesOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities": "ppe_sales",
    "PurchaseOfIntangibleAssetsClassifiedAsInvestingActivities": "capex_intangibles",
    "PurchaseOfIntangibleAssetsUnderDevelopment": "capex_intangibles_dev",
    "PurchaseOfInvestmentPropertyClassifiedAsInvestingActivities": "capex_investment_property",
    "CashFlowsUsedInObtainingControlOfSubsidiariesOrOtherBusinessesClassifiedAsInvestingActivities": "acquisitions",
    "OtherCashPaymentsToAcquireEquityOrDebtInstrumentsOfOtherEntitiesClassifiedAsInvestingActivities": "investments_bought",
    "OtherCashReceiptsFromSalesOfEquityOrDebtInstrumentsOfOtherEntitiesClassifiedAsInvestingActivities": "investments_sold",
    "InterestReceivedClassifiedAsInvestingActivities": "interest_received",
    "DividendsReceivedClassifiedAsInvestingActivities": "dividends_received",
    "CashFlowsFromUsedInInvestingActivities": "cfi",
    "ProceedsFromIssuingSharesClassifiedAsFinancingActivities": "shares_issued",
    "PaymentsToAcquireOrRedeemEntitysShares": "shares_bought_back",
    "ProceedsFromBorrowingsClassifiedAsFinancingActivities": "borrowings_raised",
    "ProceedsFromIssuingDebenturesNotesBondsEtc": "debentures_issued",
    "RepaymentsOfBorrowingsClassifiedAsFinancingActivities": "borrowings_repaid",
    "PaymentsOfLeaseLiabilitiesClassifiedAsFinancingActivities": "lease_payments",
    "PaymentsOfFinanceLeaseLiabilitiesClassifiedAsFinancingActivities": "lease_payments",
    "DividendsPaidClassifiedAsFinancingActivities": "dividends_paid",
    "InterestPaidClassifiedAsFinancingActivities": "interest_paid",
    "CashFlowsFromUsedInFinancingActivities": "cff",
    "EffectOfExchangeRateChangesOnCashAndCashEquivalents": "fx_effect",
    "IncreaseDecreaseInCashAndCashEquivalents": "net_change",
}
# The income statement for the same span as the cash flow.
YTD_TAGS = {
    "RevenueFromOperations": "ytd_revenue",
    "Income": "ytd_total_income",
    "ProfitBeforeTax": "ytd_pbt",
    "ProfitLossForPeriod": "ytd_profit",
}
CASH_TAG = "CashAndCashEquivalentsCashFlowStatement"
CF_TOTALS = ("cfo", "cfi", "cff")

BALANCE = list(dict.fromkeys([*BALANCE_TAGS.values(), *NBFC_BALANCE_TAGS.values()]))
CASH_FLOW = [*dict.fromkeys(CASH_FLOW_TAGS.values()), "cash_start", "cash_end"]
YTD = list(YTD_TAGS.values())
COLUMNS = ["symbol", "company", "period_end", "broadcast_dt", "consolidated", "format",
           "fy_start", "fy_end", "ytd_start", "ytd_days", *BALANCE, *CASH_FLOW, *YTD,
           "cf_adds_up", "cash_ties", "has_detail", "has_cash_flow", "xbrl_url"]
_DATES = ("period_end", "fy_start", "fy_end", "ytd_start")
_FLAGS = ("consolidated", "cf_adds_up", "cash_ties", "has_detail", "has_cash_flow")


def _sql_type(col: str) -> str:
    if col in _DATES:
        return "DATE"
    if col in _FLAGS:
        return "BOOLEAN"
    if col == "broadcast_dt":
        return "TIMESTAMP"
    if col == "ytd_days":
        return "INTEGER"
    if col in ("symbol", "company", "format"):
        return "VARCHAR"
    return "VARCHAR PRIMARY KEY" if col == "xbrl_url" else "DOUBLE"


SCHEMA = ("CREATE TABLE IF NOT EXISTS statements (\n    "
          + ",\n    ".join(f"{c} {_sql_type(c)}" for c in COLUMNS) + "\n)")


def ensure_schema(con) -> None:
    con.execute(SCHEMA)


def is_nbfc(url: str) -> bool:
    """A finance company's format, from the document's name (INDAS_... or NBFC_INDAS_...)."""
    return "NBFC" in str(url).rsplit("/", 1)[-1].upper()


def _number(text) -> float | None:
    try:
        return float(re.sub(r"[,\s]", "", text or ""))
    except ValueError:
        return None


def _day(text) -> date | None:
    try:
        return date.fromisoformat((text or "").strip()[:10])
    except ValueError:
        return None


def _unit(values) -> float:
    """The unit the figures were rounded to: the largest power of ten dividing every one of them
    (a crore at most). Figures filed to the rupee give 1."""
    unit = 1e7
    for v in values:
        if v is None or v == 0:
            continue
        n, u = abs(int(round(v))), 1.0
        while u < unit and n % 10 == 0:
            n //= 10
            u *= 10
        unit = min(unit, u)
    return unit


def _close(a: float, b: float, unit: float) -> bool:
    """Equal but for rounding: a few units of the filing's rounding, or a millionth of the size."""
    return abs(a - b) <= 2.5 * unit + 1e-6 * max(abs(a), abs(b))


def parse_ytd(content: bytes) -> dict:
    """The cash flow statement, the financial year and the year-to-date income lines of one
    document. Empty where the document is unreadable; cash flow columns are absent where the
    document tags none or does not date them (see the module docstring)."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        log.debug("unparseable XBRL: %s", e)
        return {}

    spans: dict[str, tuple[date, date]] = {}
    instants: dict[str, date] = {}
    dimensioned: set[str] = set()
    for ctx in root.iter():
        if not (ctx.tag.endswith("}context") or ctx.tag == "context"):
            continue
        cid = ctx.get("id")
        if not cid:
            continue
        start = end = inst = None
        for node in ctx.iter():
            tag = node.tag.split("}")[-1]
            if tag == "startDate":
                start = _day(node.text)
            elif tag == "endDate":
                end = _day(node.text)
            elif tag == "instant":
                inst = _day(node.text)
            elif tag in ("explicitMember", "typedMember"):
                dimensioned.add(cid)        # a segment or a breakdown, never the company total
        if cid in dimensioned:
            continue
        if start and end:
            spans[cid] = (start, end)
        elif inst:
            instants[cid] = inst

    wanted = {**CASH_FLOW_TAGS, **YTD_TAGS}
    by_ref: dict[str, dict[str, float]] = {}       # context -> column -> the first figure in the document
    stated: dict[str, dict[str, date]] = {}        # context -> the reporting period it states
    cash: list[tuple[str, float]] = []             # every cash balance of the cash flow statement
    out: dict = {}
    for node in root.iter():
        ref = node.get("contextRef")
        if ref is None or ref in dimensioned:
            continue
        tag = node.tag.split("}")[-1]
        if tag in ("DateOfStartOfFinancialYear", "DateOfEndOfFinancialYear"):
            d = _day(node.text)
            if d:
                out.setdefault("fy_start" if "Start" in tag else "fy_end", d)
        elif tag in ("DateOfStartOfReportingPeriod", "DateOfEndOfReportingPeriod"):
            d = _day(node.text)
            if d:
                stated.setdefault(ref, {}).setdefault("start" if "Start" in tag else "end", d)
        elif tag == CASH_TAG:
            v = _number(node.text)
            if v is not None:
                cash.append((ref, v))
        elif tag in wanted:
            v = _number(node.text)
            if v is not None:
                # the first figure in the document, except that a line with two names (lease
                # payments) is not left at the zero filed under the name the company does not use
                cols = by_ref.setdefault(ref, {})
                if cols.get(wanted[tag]) in (None, 0.0):
                    cols[wanted[tag]] = v

    def period(ref: str):
        s = stated.get(ref, {})
        if "start" in s and "end" in s and s["start"] <= s["end"]:
            return s["start"], s["end"]            # what the document says about it comes first
        return spans.get(ref)

    # The context the cash flow totals sit in: the one ending latest, and the longest of those.
    dated = [(ref, period(ref)) for ref, cols in by_ref.items() if any(c in cols for c in CF_TOTALS)]
    dated = [(ref, p) for ref, p in dated if p is not None]
    if not dated:
        return out
    ref, (start, end) = max(dated, key=lambda x: (x[1][1], (x[1][1] - x[1][0]).days))
    figures = by_ref[ref]
    out.update(figures)
    out["ytd_start"], out["ytd_days"] = start, (end - start).days + 1
    # A cash flow statement ending on the financial year's last day is the whole year's: SEBI
    # asks for one at the half year and the year end only. Year-end filings before Sep 2022 put
    # it in the fourth quarter's context, with that quarter's dates and its income lines (TCS,
    # Mar 2021: the year's operating cash flow beside the quarter's revenue). The year's start
    # is taken from the financial year then, and the quarter's income lines are not kept as
    # year-to-date figures.
    fs, fe = out.get("fy_start"), out.get("fy_end")
    if fs and fe and end == fe and fs < start:
        out["ytd_start"], out["ytd_days"] = fs, (end - fs).days + 1
        for c in YTD:
            out.pop(c, None)

    # Opening and closing cash. Newer documents tag them at the two balance-sheet dates; the 2023
    # and 2024 ones tag both under the cumulative context, opening first.
    at = [(instants[r], v) for r, v in cash if r in instants]
    net = figures.get("net_change")
    unit = _unit([figures.get(c) for c in (*CF_TOTALS, "net_change")])
    closing = [v for d, v in at if d == end]
    opening = sorted(((d, v) for d, v in at if d < end), key=lambda x: x[0])
    if closing and opening:
        out["cash_start"], out["cash_end"] = opening[-1][1], closing[0]
    else:
        same = [v for r, v in cash if r == ref]
        if len(same) == 2 and net is not None:
            a, b = same
            if _close(a + net, b, unit):
                out["cash_start"], out["cash_end"] = a, b
            elif _close(b + net, a, unit):
                out["cash_start"], out["cash_end"] = b, a

    if all(figures.get(c) is not None for c in CF_TOTALS) and net is not None:
        total = sum(figures[c] for c in CF_TOTALS) + (figures.get("fx_effect") or 0.0)
        out["cf_adds_up"] = _close(total, net, unit)
    if net is not None and out.get("cash_start") is not None and out.get("cash_end") is not None:
        out["cash_ties"] = _close(out["cash_start"] + net, out["cash_end"], unit)
    return out


def parse(content: bytes, nbfc: bool = False) -> dict:
    """Everything `statements` holds about one document (no identity columns)."""
    balance = financials.parse_xbrl(content, tags=NBFC_BALANCE_TAGS if nbfc else BALANCE_TAGS)
    row = {k: v for k, v in balance.items() if k in BALANCE}
    row.update(parse_ytd(content))
    row["format"] = "nbfc" if nbfc else "indas"
    lines = ("loans", "investments") if nbfc else ("ppe", "inventories", "receivables", "cash")
    row["has_detail"] = any(row.get(c) is not None for c in lines)
    row["has_cash_flow"] = all(row.get(c) is not None for c in CF_TOTALS) and row.get("ytd_start") is not None
    return row


def documents(con, latest: bool = False) -> pd.DataFrame:
    """The filings `financials` lists with a balance sheet, oldest broadcast first. `latest`
    keeps only the last revision of each (symbol, basis, period end)."""
    df = con.execute(
        """
        SELECT symbol, company, period_end, broadcast_dt, consolidated, xbrl_url
        FROM financials
        WHERE xbrl_url IS NOT NULL AND (has_balance_sheet
              OR (month(period_end) IN (3, 9) AND period_end BETWEEN DATE '2020-09-30' AND DATE '2022-06-30'))
        ORDER BY broadcast_dt
        """).df()
    if latest and len(df):
        df = df.drop_duplicates(["symbol", "consolidated", "period_end"], keep="last")
    return df


def stored_urls(out_dir: Path = OUT_DIR) -> set[str]:
    files = sorted(out_dir.glob("*.parquet")) if out_dir.exists() else []
    if not files:
        return set()
    return set(pd.concat([pd.read_parquet(f, columns=["xbrl_url"]) for f in files])["xbrl_url"])


def _download(session, url: str) -> bytes:
    cached = xbrl_cache.get(url)
    if cached is not None:
        return cached
    r = session.get(url, timeout=20)
    r.raise_for_status()
    if not xbrl_cache._well_formed(r.content):
        # an error page served with a 200: a failed download, so the next run tries it again
        raise ValueError("not an XBRL document")
    xbrl_cache.put(url, r.content)
    return r.content


_now = time.monotonic                              # the clock sessions are aged by


def fetch(todo: pd.DataFrame, session=None, pause: float = 0.35, give_up_after: int = 25,
          make_session=financials_legacy.document_session) -> tuple[pd.DataFrame, int]:
    """Read every document in `todo` (rows of documents()). Returns (rows, failed downloads).

    A failed download is left out, so the next run tries it again. `give_up_after` failures in
    a row stop the run: NSE is refusing, and hammering it helps nobody. What was read is kept.

    A `session` passed in is used as it is. Otherwise sessions come from `make_session` and are
    replaced on a timer and after every failed download. NSE's archive host drops keep-alive
    connections badly, and once a session's pool is wedged every later request on it blocks
    until it times out: the first run of this job stopped dead after seven minutes on one
    session. The cure is a new session object (financials_legacy.document_session has the
    history), and the archive host needs no cookies."""
    rows, failed, streak = [], 0, 0
    own, born = session is None, 0.0
    for i, meta in enumerate(todo.to_dict("records"), 1):
        hit = xbrl_cache.get(meta["xbrl_url"]) is not None
        if own and not hit and (session is None or _now() - born > financials_legacy.REWARM_SECONDS):
            if session is not None:
                session.close()
            session, born = make_session(), _now()
        try:
            content = _download(session, meta["xbrl_url"])
            streak = 0
        except Exception as e:                                       # noqa: BLE001
            failed += 1
            streak += 1
            log.debug("statements: %s not fetched (%s)", meta["xbrl_url"], e)
            if own and session is not None:                          # the connection has gone bad
                session.close()
                session = None
            if streak >= give_up_after:
                log.warning("statements: %d downloads failed in a row, stopping at %d of %d", streak, i, len(todo))
                break
            time.sleep(pause * min(streak, 10))
            continue
        rows.append({**meta, **parse(content, is_nbfc(meta["xbrl_url"]))})
        if not hit:
            time.sleep(pause)
        if i % 500 == 0:
            log.info("statements: %d of %d documents read", i, len(todo))
    return pd.DataFrame(rows, columns=COLUMNS), failed


def write(df: pd.DataFrame, out_dir: Path = OUT_DIR) -> list[Path]:
    if df.empty:
        return []
    df = df.copy()
    for c in _DATES:                               # one type whether the value came from SQL or the parser
        df[c] = pd.to_datetime(df[c])
    for c in _FLAGS:
        df[c] = df[c].astype("boolean")
    df["ytd_days"] = df["ytd_days"].astype("Int64")
    written = []
    years = pd.to_datetime(df["period_end"]).dt.year
    for year, chunk in df.groupby(years):
        p = out_dir / f"{int(year)}.parquet"
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            chunk = pd.concat([pd.read_parquet(p), chunk], ignore_index=True)
        chunk = chunk.reindex(columns=COLUMNS).drop_duplicates(subset=["xbrl_url"], keep="last")
        chunk = chunk.sort_values(["symbol", "period_end", "consolidated", "xbrl_url"])
        # Whole or not at all. Two runs once wrote the same year at the same moment and left a
        # file nothing could read; a file put in place by one rename is never half written.
        tmp = p.with_name(p.name + ".tmp")
        chunk.to_parquet(tmp, index=False, compression="zstd")
        os.replace(tmp, p)
        written.append(p)
    return written


def rebuild(con, out_dir: Path = OUT_DIR) -> int:
    # a table built before a column was added is replaced, not altered: it is rebuilt from the
    # parquet files every time anyway
    cols = [r[0] for r in con.execute("SELECT column_name FROM information_schema.columns "
                                      "WHERE table_name = 'statements'").fetchall()]
    if cols and set(cols) != set(COLUMNS):
        con.execute("DROP TABLE statements")
    ensure_schema(con)
    if not out_dir.exists() or not any(out_dir.glob("*.parquet")):
        return 0
    con.execute("DELETE FROM statements")
    cols = ", ".join(COLUMNS)
    con.execute(f"INSERT OR IGNORE INTO statements ({cols}) "
                f"SELECT {cols} FROM read_parquet('{out_dir.as_posix()}/*.parquet', union_by_name=true)")
    return con.execute("SELECT count(*) FROM statements").fetchone()[0]
