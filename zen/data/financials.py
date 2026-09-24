"""Quarterly financials: the 2025-onward scheme, and the shared XBRL parser.

TWO ENDPOINTS, TWO ERAS

/api/integrated-filing-results is the scheme NSE introduced in 2025. It carries
income statement and balance sheet as tagged Ind-AS XBRL and stamps every
filing with the moment NSE broadcast it.

Everything before 2025 comes from the older endpoint, handled in
financials_legacy.py. That one was written off here as a dead end -- the note
this docstring used to carry said it "serves a single quarter" -- which was
wrong. It returns nothing unless `period=Quarterly` is passed, and with it
returns 87,449 filings back to 2016. Reading a docstring for a conclusion
someone else reached is how a wrong one survives; this one survived for weeks.

WHAT THE COMBINED ARCHIVE SUPPORTS -- measured, not assumed

  Income statement   2018 onward, quarterly, standalone and consolidated
  Balance sheet      Sep 2022 onward, half-yearly, as SEBI requires
  Broadcast stamps   throughout, so point-in-time filtering holds

So a live screen works on everything. A backtest of income-statement factors
has about 29 quarters; one of leverage or return-on-capital has about 14, which
is one market regime and worth saying out loud whenever a number comes out of
it.

POINT-IN-TIME

`broadcast_dt` is when NSE published the filing, not when the period ended. A
December quarter is published in mid-January; a screen filtering on period end
would grant itself six weeks of foresight. known_at() filters on broadcast_dt,
and the table keeps every revision so that a query about November cannot see a
correction published in January.

EXCEPTIONAL ITEMS

Indian companies book one-off land sales and write-offs above the profit line
often enough that raw net profit is a poor guide to earning power.
`profit_normalised` strips them, `profit_reported` keeps them, and both are
stored so a screen's choice is visible rather than implicit.
"""

from __future__ import annotations

import logging
import re
import time
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

# quarter_span_days is LAST, after xbrl_url, and must stay there. The table is
# rebuilt with a positional INSERT ... SELECT * over read_parquet(union_by_name),
# and union_by_name appends a column missing from the first file at the end. A
# file written before the column existed then lines up with one written after
# only if the new column is the last one in both.
COLUMNS = ["symbol", "company", "period_end", "broadcast_dt", "consolidated",
           "audited", *dict.fromkeys(TAGS.values()), *DERIVED, "xbrl_url",
           "quarter_span_days"]

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
    -- Days spanned by the context the income-statement figures came from;
    -- NULL when none were taken (a filing tagging only a half-year or year has
    -- its duration figures left empty) and in rows parsed before the column.
    quarter_span_days INTEGER,
    -- Keyed on the DOCUMENT, not on (symbol, period, basis). A company files a
    -- quarter and then files it again: 572 keys in this archive, 132 of them
    -- with revised figures. Keying on the period would keep only the last
    -- version, and a backtest asking what was known in November would then see
    -- a correction published in January. Every version is kept and known_at()
    -- picks the latest one that had actually been broadcast by the as-of date.
    PRIMARY KEY (xbrl_url)
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
            # A failed page used to log a warning and stop, returning whatever
            # had arrived so far as if it were the whole window. That is how
            # the March 2025 quarter came to hold 1,516 companies against about
            # 2,200 either side of it: results season is when pages fail, and
            # a short list looks exactly like a quiet week. Retry, then raise.
            batch, last_err = None, None
            for attempt in range(3):
                try:
                    r = s.get(url, timeout=90)
                    r.raise_for_status()
                    payload = r.json()
                    # A 200 carrying something other than rows (an error page,
                    # a bot check) is a failure, not an empty page.
                    if not isinstance(payload, (list, dict)) or (
                            isinstance(payload, dict) and "data" not in payload):
                        raise ValueError(f"unexpected payload: {str(payload)[:120]}")
                    batch = _rows(payload)
                    break
                except Exception as e:                               # noqa: BLE001
                    last_err = e
                    log.warning("page %d attempt %d failed (%s)", page, attempt + 1, e)
                    time.sleep(2 * (attempt + 1))
                    try:
                        s.get(WARMUP, timeout=25)
                    except Exception:                                # noqa: BLE001
                        pass
            if batch is None:
                raise RuntimeError(
                    f"financials listing {start}..{end} failed on page {page} after "
                    f"3 attempts ({last_err}); refusing to return a partial window")
            if not batch:
                break
            raw.extend(batch)
            if len(batch) < page_size:
                break
        else:
            raise RuntimeError(f"financials listing {start}..{end} still had rows after "
                               f"{max_pages} pages; narrow the window rather than truncate")

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


# The longest span whose figures are accepted as "the quarter". Calendar
# quarters run 89 to 92 days; 100 leaves room for a quarter that ends a few
# days off the calendar and is still far below a half-year (181-183) or a year.
QUARTER_MAX_DAYS = 100


def parse_xbrl(content: bytes, detail: dict | None = None) -> dict:
    """Extract tagged figures for the period being reported.

    A filing carries several contexts: the quarter just ended, the year-ago
    quarter, cumulative periods, and instant contexts for balance-sheet items.
    Duration facts are taken from the shortest span ending latest -- the
    quarter itself rather than a nine-month cumulative -- and instant facts
    from the latest instant, which is the balance-sheet date.

    THE QUARTER MUST BE A QUARTER. "Shortest span ending latest" is only the
    quarter when the filing tags one. DELTACORP's first Sep-2025 filing tags
    its income statement for 2025-04-01 to 2025-09-30 and nothing shorter, so
    the rule returned a half-year as the quarter (2,619.5m against a real
    quarter of 1,310.5m in its own revised filing three days later). A March
    filing carrying only the year would have been stored four times too large.
    So duration facts are accepted only from a span of at most
    QUARTER_MAX_DAYS. When the chosen span is longer, the filing's income
    statement is treated as NOT REPORTED: left empty, never derived (no H1
    minus Q1), because a derivation silently mixes two documents. Instant
    (balance-sheet) facts are still taken; a half-year filing's balance sheet
    is exactly as valid as a quarter's.

    `quarter_span_days` is returned with the figures: the length in days of the
    span the duration facts came from, absent when none were taken.

    `detail`, when a dict is passed, is filled with why: duration_source
    ("declared", "OneD", "audit_fallback" or None), rejected_span_days (the
    length of a refused period; an undated OneD is accepted as the quarter, see
    below, so it is never refused), and dropped_columns
    -- figures present in the document that this rule refused.
    """
    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        log.debug("unparseable XBRL: %s", e)
        return {}

    spans: dict[str, tuple[date, date]] = {}
    instants: dict[str, date] = {}
    # Contexts whose ONLY dimension is the audit-qualification axis. Used to
    # fill a tag that has no undimensioned fact at all, never to override one.
    audited: dict[str, tuple] = {}
    # Periods of the declared, dimensioned "One..." contexts. In the 2018-2024
    # documents these carry the same reporting-period dates the undeclared
    # OneD stands for; they date OneD when its own date facts are missing.
    one_dim_spans: set[tuple[date, date]] = set()
    for ctx in root.iter():
        if not (ctx.tag.endswith("}context") or ctx.tag == "context"):
            continue
        cid = ctx.get("id")
        if not cid:
            continue
        start = end = inst = None
        dimensioned = False
        axes = []
        for node in ctx.iter():
            tag = node.tag.split("}")[-1]
            txt = (node.text or "").strip()
            if tag == "startDate" and txt:
                start = txt
            elif tag == "endDate" and txt:
                end = txt
            elif tag == "instant" and txt:
                inst = txt
            elif tag in ("explicitMember", "typedMember"):
                axes.append(((node.get("dimension") or "").split(":")[-1],
                             txt.split(":")[-1]))
                # A dimensioned context reports one SEGMENT, not the company:
                # ReportableSegmentsAxis, DetailsOfOtherExpensesAxis and the
                # rest all carry the same period as the company total, so a
                # rule that selects on dates alone ties with them and picks
                # whichever appears first in the document. That silently
                # returned one business division's revenue as if it were the
                # whole company. Company totals are always undimensioned.
                dimensioned = True
        try:
            if start and end:
                period = ("span", date.fromisoformat(start), date.fromisoformat(end))
            elif inst:
                period = ("instant", date.fromisoformat(inst))
            else:
                continue
        except ValueError:
            continue

        if dimensioned:
            if period[0] == "span" and cid.startswith("One"):
                one_dim_spans.add((period[1], period[2]))
            # One exception, and only one. AuditedOrAdjustedAxis is SEBI's
            # statement-of-impact-of-audit-qualifications disclosure, not a
            # business segment: AuditedMember is the company as reported and
            # AdjustedMember is the same company after the auditor's
            # qualifications. Some small caps tag Assets and Liabilities under
            # it and nowhere else, so treating it like a segment discards a
            # company total that is not in dispute -- PLATIND's two Assets
            # facts are byte-identical across both members.
            #
            # Held as a FALLBACK rather than a preference. Of 114 documents
            # carrying the axis, 44 have both an undimensioned Assets and an
            # AuditedMember Assets, and 19 of those disagree -- one reports
            # undimensioned liabilities of 29.1bn against an audited figure of
            # 558m that equals its own assets and is plainly a filer error.
            # Undimensioned wins wherever it exists.
            if axes == [("AuditedOrAdjustedAxis", "AuditedMember")]:
                audited[cid] = period
            continue

        if period[0] == "span":
            spans[cid] = (period[1], period[2])
        else:
            instants[cid] = period[1]

    def pick_duration(candidates: dict) -> tuple[set, int | None, set, int | None]:
        """(accepted refs, their span, refused refs, the refused span)."""
        if not candidates:
            return set(), None, set(), None
        latest_end = max(b for _, b in candidates.values())
        shortest = min((b - a).days for a, b in candidates.values() if b == latest_end)
        chosen = {c for c, (a, b) in candidates.items()
                  if b == latest_end and (b - a).days == shortest}
        if shortest <= QUARTER_MAX_DAYS:
            return chosen, shortest, set(), None
        return set(), None, chosen, shortest

    dur, span_days, rejected, refused_days = pick_duration(spans)
    rejected_days = [refused_days] if rejected else []
    inst: set[str] = set()
    if instants:
        latest = max(instants.values())
        inst |= {c for c, d in instants.items() if d == latest}

    # The 2018-2024 filings declare ONLY dimensioned segment contexts and then
    # reference the company-level figures through a context they never declare
    # -- "OneD" for the period and "OneI" for the balance-sheet instant. It is
    # invalid XBRL, but it is what NSE served for seven years, and a parser
    # that ignores it reads a filing as segment data with no company totals.
    # An undeclared reference carries no segment by construction, so those two
    # are safe to accept as the company. Any other undeclared reference is left
    # alone: missing a figure is recoverable, attributing a division's revenue
    # to the company is not.
    #
    # An undeclared OneD has no dates of its own, so its length is not taken on
    # trust. The document states the period as facts under OneD --
    # DateOfStartOfReportingPeriod and DateOfEndOfReportingPeriod -- and those
    # decide. Banks' documents omit the start date; for them the declared
    # "One..." segment contexts decide instead (the longest, if they differ):
    # in the 515 sampled documents carrying both, the two sources agreed every
    # time. A OneD whose length the document does not establish, or that is
    # longer than a quarter, is refused like any other long span. Measured on
    # 577 legacy documents (2018-2024): every one that dates OneD dates a
    # quarter, and the figures reconcile with the neighbouring quarters -- in
    # 165 of 168 testable September filings the stored June quarter plus OneD
    # equals the half-year figure the same document reports, to within 3%.
    declared = set(spans) | set(instants)
    referenced = {n.get("contextRef") for n in root.iter()} - {None}
    if "OneI" in referenced and "OneI" not in declared:
        inst.add("OneI")
    duration_source = "declared" if dur else None
    if "OneD" in referenced and "OneD" not in declared:
        stated: dict[str, str] = {}
        for node in root.iter():
            if node.get("contextRef") == "OneD":
                tag = node.tag.split("}")[-1]
                if tag in ("DateOfStartOfReportingPeriod", "DateOfEndOfReportingPeriod"):
                    stated.setdefault(tag, (node.text or "").strip()[:10])
        oned_days = None
        try:
            d = (date.fromisoformat(stated["DateOfEndOfReportingPeriod"])
                 - date.fromisoformat(stated["DateOfStartOfReportingPeriod"])).days
            oned_days = d if d >= 0 else None
        except (KeyError, ValueError):
            pass
        if oned_days is None and one_dim_spans:
            oned_days = max((b - a).days for a, b in one_dim_spans)
        if oned_days is not None and oned_days <= QUARTER_MAX_DAYS:
            dur.add("OneD")
            if span_days is None:
                span_days, duration_source = oned_days, "OneD"
        elif oned_days is None:
            # The document does not date OneD at all. Refusing it, as this
            # rule first did, dropped real quarterly figures: in all 577 legacy
            # documents where OneD CAN be dated it is exactly the quarter, and
            # the old parser always took it. So an undated OneD is accepted as
            # the quarter, and recorded as such so it can be told apart.
            # quarter_span_days stays empty because no length was established.
            dur.add("OneD")
            if duration_source is None:
                duration_source = "OneD_undated"
        else:
            # Dated, and longer than a quarter: refused like any long span.
            rejected.add("OneD")
            rejected_days.append(oned_days)

    # Audit-axis contexts for the same period, kept separate so they can only
    # ever fill a gap. The quarter rule applies to them exactly as above.
    a_spans = {c: (p[1], p[2]) for c, p in audited.items() if p[0] == "span"}
    a_inst = {c: p for c, p in audited.items() if p[0] == "instant"}
    fb_dur, fb_days, fb_rejected, fb_refused_days = pick_duration(a_spans)
    if fb_rejected:
        rejected |= fb_rejected
        rejected_days.append(fb_refused_days)
    fb_inst: set[str] = set()
    if a_inst:
        latest = max(p[1] for p in a_inst.values())
        fb_inst |= {c for c, p in a_inst.items() if p[1] == latest}

    facts: dict = {}
    source: dict[str, str] = {}

    def collect(refs: set) -> None:
        # The first value in document order wins, as setdefault did.
        for node in root.iter():
            col = TAGS.get(node.tag.split("}")[-1])
            ref = node.get("contextRef")
            if not col or col in facts or ref not in refs or not node.text:
                continue
            try:
                facts[col] = float(re.sub(r"[,\s]", "", node.text))
            except ValueError:
                continue
            source[col] = ref

    # OneD is the reported quarter. Some legacy filings also declare FourD with
    # the SAME dates while it holds year-to-date values (240 of 910 sampled).
    # The quarter used to win only because OneD comes first in the document.
    # It now wins by rule: OneD is collected first, and FourD is not used at
    # all when OneD is present, since its figures are not the quarter.
    if "OneD" in dur:
        dur = dur - {"FourD"}
        collect({"OneD"} | inst)
    if dur or inst:
        collect(dur | inst)
    if fb_dur or fb_inst:
        # Second pass only: anything already found from an undimensioned
        # context stands, so this can add a missing figure and never replace one.
        collect(fb_dur | fb_inst)

    took = set(source.values())
    if took & dur:
        facts["quarter_span_days"] = span_days
    elif took & fb_dur:
        facts["quarter_span_days"] = fb_days
        duration_source = "audit_fallback"
    else:
        duration_source = None

    if detail is not None:
        dropped = set()
        if rejected:
            for node in root.iter():
                col = TAGS.get(node.tag.split("}")[-1])
                if (col and col not in facts and node.get("contextRef") in rejected
                        and (node.text or "").strip()):
                    dropped.add(col)
        detail.update(duration_source=duration_source,
                      quarter_span_days=facts.get("quarter_span_days"),
                      rejected_span_days=max(rejected_days) if rejected_days else None,
                      dropped_columns=sorted(dropped))
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
    # Migration: quarter_span_days arrived on 2026-09-24. A database built
    # before then has a 41-column table, and every positional insert into it
    # would fail, so the column is added in place. It goes last, which is
    # where COLUMNS puts it.
    con.execute("ALTER TABLE financials ADD COLUMN IF NOT EXISTS quarter_span_days INTEGER")


_COLS_SQL = ", ".join(COLUMNS)


def upsert(con, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    ensure_schema(con)
    con.register("incoming_fin", df)
    before = con.execute("SELECT count(*) FROM financials").fetchone()[0]
    con.execute(f"INSERT OR IGNORE INTO financials ({_COLS_SQL}) "
                f"SELECT {_COLS_SQL} FROM incoming_fin")
    after = con.execute("SELECT count(*) FROM financials").fetchone()[0]
    con.unregister("incoming_fin")
    return after - before


def write_parquet(df: pd.DataFrame, out_dir: Path = PARQUET_DIR) -> list[Path]:
    """Append filings to one parquet per quarter, keeping every revision.

    De-duplicated on the DOCUMENT (`xbrl_url`), like the table's primary key.
    It used to de-duplicate on (symbol, period_end, consolidated) keeping the
    last, which silently threw away every earlier version of a revised filing
    each time it wrote. A backtest asking what was known in November would then
    see January's correction. It had not fired only because the updater never
    fetched revisions; the 2026-09-23 backfill added about 2,200 of them.

    `period_end` is normalised to datetime64 on both sides. The backfill wrote
    timestamps and this writer wrote dates, and pyarrow refuses to mix them.
    """
    if df.empty:
        return []
    df = df.copy()
    df["period_end"] = pd.to_datetime(df["period_end"])
    written = []
    for q, chunk in df.groupby(df["period_end"].map(
            lambda d: f"{d:%Y-%m}" if pd.notna(d) else "unknown")):
        p = out_dir / f"{q}.parquet"
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            old = pd.read_parquet(p)
            old["period_end"] = pd.to_datetime(old["period_end"])
            chunk = pd.concat([old, chunk], ignore_index=True)
        chunk = chunk.drop_duplicates(subset=["xbrl_url"], keep="first")
        chunk.to_parquet(p, index=False, compression="zstd")
        written.append(p)
    return written


# Bookkeeping that lives alongside the statements and is not a statement:
# the filing index, and the ledger of documents NSE confirmed it does not hold.
# A bare *.parquet glob swept both into the table and the insert failed with
# "41 columns but 45 values supplied" -- an error about column counts that was
# really about two files having no business being read at all.
NON_STATEMENT = ("legacy_index", "legacy_missing")


def statement_files(out_dir: Path = PARQUET_DIR) -> list[Path]:
    """Parquet files that actually hold financial statement rows."""
    return sorted(p for p in out_dir.glob("*.parquet")
                  if p.stem not in NON_STATEMENT)


def rebuild_from_parquet(con, out_dir: Path = PARQUET_DIR) -> int:
    ensure_schema(con)
    files = statement_files(out_dir) if out_dir.exists() else []
    if not files:
        return 0
    listed = ", ".join(f"'{p.as_posix()}'" for p in files)
    # One row per document. The same XBRL can appear in two quarter files when
    # a filing is re-broadcast across a boundary, so the insert is deduplicated
    # on the document itself rather than trusting the files not to overlap.
    # 813 documents appear twice with the same figures but different period
    # ends (almost always one year or one quarter apart, e.g. a Mar-2018 filing
    # broadcast in May 2018 also stored as 2017-03-31, one second later). The
    # copy with the LATEST period_end is the document's own period; the other
    # is a mislabel. Ordering on broadcast_dt alone picked the mislabel in 685
    # cases and was arbitrary in 150 exact ties (strategy v1 Clarification 22).
    # Delete and insert in one transaction, with columns named rather than
    # positional. Before 2026-09-24 a failed insert (a column-count mismatch
    # after a schema change) left the table deleted and empty, and rebuild_db
    # only logged a warning.
    con.execute("BEGIN TRANSACTION")
    try:
        con.execute("DELETE FROM financials")
        con.execute(f"""INSERT INTO financials ({_COLS_SQL})
            SELECT {_COLS_SQL} FROM (
                SELECT *, row_number() OVER (PARTITION BY xbrl_url
                                             ORDER BY period_end DESC, broadcast_dt DESC) AS rn
                FROM read_parquet([{listed}], union_by_name=true)
                WHERE xbrl_url IS NOT NULL) WHERE rn = 1""")
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
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
