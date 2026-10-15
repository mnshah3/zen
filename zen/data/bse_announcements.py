"""BSE corporate announcements, with BSE's own category and subcategory on every filing.

WHY BSE AS WELL AS NSE

A listed company files the same disclosure with both exchanges, usually within minutes. Each
exchange classifies it. NSE's classification has two holes that no amount of text matching
fills honestly (zen.data.filing_types): its order, capacity and regulatory labels are absent
from January 2023 to 22 September 2024, and the NSE archive here starts in 2022. BSE has
labelled every filing throughout. "Award of Order / Receipt of Order" exists in 2018, 2023 and
today; the same day in June 2023 carries 597 filings, every one with a subcategory. So the
labels come from an exchange, not from guessing at a company's prose, for the whole period.

WHAT IS STORED

One row a filing, keyed by BSE's NEWSID:

  dissem_dt     when BSE disseminated it, the moment it became public (DissemDT; NEWS_DT where
                that is missing). A filing can only be acted on from here.
  trade_date    the first session it could affect, by the same 15:30 rule as NSE filings
  scrip_cd      BSE's company code; `isin` comes from BSE's own scrip list (active, suspended and
                delisted companies), so a filing joins to NSE symbols by ISIN, not by name
  category      BSE's category and subcategory, as BSE wrote them (stripped of spaces only)
  subcategory
  zen_category  the same categories zen.data.filing_types gives NSE filings, from the mapping
                below. Derived, so it can be recomputed from the two columns before it.
  headline      BSE's one-line summary, at most 300 characters, and none for housekeeping
                (zen_category "routine"), whose headline is boilerplate. Kept lean on purpose:
                about 1.9 million filings since 2018.
  attachment    the PDF's file name on BSE

Fetched a day at a time, every page of the day, and a day only counts as fetched when the rows
add up to the count BSE reports for it (`days.csv` records it). A partial day is retried on the
next run rather than stored as complete.
"""

from __future__ import annotations

import csv
import logging
import re
import time
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import requests

from zen.data.announcements import _trade_date

log = logging.getLogger(__name__)

API = ("https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w?pageno={page}&strCat=-1"
       "&strPrevDate={d}&strScrip=&strSearch=P&strToDate={d}&strType=C&subcategory=-1")
SCRIPS_API = ("https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w?Group=&Scripcode=&industry="
              "&segment=Equity&status={status}")
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/129.0 Safari/537.36"),
    "Referer": "https://www.bseindia.com/",
    "Origin": "https://www.bseindia.com",
    "Accept": "application/json, text/plain, */*",
}
PAGE = 50                     # BSE's page size; a shorter page is the last one
HEADLINE_CHARS = 300          # enough to say what happened; the PDF holds the rest

OUT_DIR = Path("data/bse_announcements")
DAYS_FILE = OUT_DIR / "days.csv"
SCRIPS_FILE = Path("data/reference/bse_scrips.parquet")
START = date(2018, 1, 1)

COLUMNS = ["news_id", "dissem_dt", "trade_date", "scrip_cd", "isin", "company", "category",
           "subcategory", "zen_category", "headline", "attachment"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS bse_announcements (
    news_id      VARCHAR PRIMARY KEY,
    dissem_dt    TIMESTAMP NOT NULL,
    trade_date   DATE NOT NULL,
    scrip_cd     INTEGER NOT NULL,
    isin         VARCHAR,
    company      VARCHAR,
    category     VARCHAR,
    subcategory  VARCHAR,
    zen_category VARCHAR,
    headline     VARCHAR,
    attachment   VARCHAR
);
"""


# --------------------------------------------------------------------------------------------
# BSE's labels in zen's categories. Written out in full, as in filing_types, so the mapping can
# be read and argued with. The names are BSE's, spelling and spacing included.
# --------------------------------------------------------------------------------------------

_RAW: dict[str, tuple[str, ...]] = {
    "orders": (
        "Award of Order / Receipt of Order",
        "Bagging/Receiving of orders/contracts (Sub-para 4-Para B)",
        "Awarding of order(s)/contract(s) (Sub-para 4-Para B)",
        "Reg. 30 - Awarding of orders/contracts",
    ),
    "expansion": (
        "Capacity addition (Sub-para 3-Para B)",
        "Product launch (Sub-para 3-Para B)",
        "Adoption of new line(s) of business (Sub-para 2-Para B)",
        "Arrangements (Sub-para 2-Para B)",
        "Commencement or postponement (Sub-para 1-Para B)",
    ),
    "contraction": (
        "Closure of operations (Sub-para 2-Para B)",
        "Disruption of operations (Sub-para 6-Para B)",
        "Strike",
        "Strikes /Lockouts / Disturbances",
        "Amendment or termination of orders/contracts (Sub-para 4-Para B)",
    ),
    "regulatory": (
        "Pendency of any litigation(s) or dispute(s) (Sub-para 8-Para B)",
        "Delay or default in the payment of fines (Sub-para 13-Para B)",
        "Frauds/defaults/arrest - employee (Sub-para 9-Para B)",
        "Intimation on Fraud",
        "Change in regulatory framework (Sub-para 7-Para B)",
        "Corporate Insolvency Resolution Process",
        "Initiation of Corporate Insolvency Resolution Process (CIRP) by Corporate Applicant",
        "Initiation of Corporate Insolvency Resolution Process (CIRP) by Financial Creditors",
        "Admission of application by Tribunal",
        "Appointment of Interim Resolution Professional (IRP)",
        "Replacement of Interim Resolution Professional (IRP)",
        "Approval of Resolution plan by Tribunal",
        "Filing of Resolution plan with Tribunal",
        "Rejection of Resolution pan by Tribunal",
        "Resolution Plans received by Resolution Professional",
        "Invitation of Resolution Plan",
        "Intimation of meeting of Committee of Creditors",
        "Outcome of meeting of Committee of Creditors",
        "List of Creditors",
        "Liquidation - Corporate Insolvency Resolution Process (CIRP)",
        "Updates - Corporate Insolvency Resolution Process (CIRP)",
        "Corporate Debt Restructuring",
        "Intimation on OTS and Restructuring Loans or Borrowings",
        "Winding-up",
        "Public Announcement-Delisting",
        "Disclosures by listed entities of defaults on payment of interest/ repayment of principal amount "
        "for loans including revolving facilities like cash credit from banks / financial institutions.",
        "Disclosures by listed entities of defaults on payment of interest/ repayment of principal amount "
        "for unlisted debt securities i.e. NCDs and NCRPS.",
        "Quarterly Disclosures by listed entities of defaults on payment of interest/ repayment of principal "
        "amount on loans from banks / financial institutions and unlisted debt securities.",
    ),
    "licenses": (
        "Key licenses/regulatory approvals (Sub-para 12-Para B)",
    ),
    "mna": (
        "Acquisition",
        "Amalgamation/ Merger",
        "Amalgamation / Merger / Demerger",
        "Scheme of Arrangement",
        "De-merger",
        "Open Offer",
        "Open Offer - Updates",
        "Public Announcement-Open Offer",
        "Post Offer Public Announcement",
        "Sale or disposal",
        "Diversification / Disinvestment",
        "Restructuring",
        "Other Restructuring",
        "Update-Acquisition/Scheme/Sale/Disposal/Reg30",
        "Slump Sale",
        "Joint Venture",
        "Voluntary Delisting",
        "Change in Management Control",
    ),
    "capital": (
        "Allotment of Equity Shares",
        "Allotment of Warrants",
        "Alteration of Capital",
        "Increase of Authorised Capital",
        "Issue of Securities",
        "Preferential Issue",
        "Qualified Institutional Placement",
        "Offer for Sale",
        "Institutional Placement Programme",
        "FCCBs",
        "Raising of Funds",
        "Debt Securities",
        "Funds raising by issuance of Debt Securities by Large Entities",
        "Options to purchase securities ESOPS (Sub-para 10-Para B)",
        "Annulment / Re-issue of forfeited shares",
        "Bonds / Right issue",
        "Capital Reduction",
    ),
    "ratings": (
        "Credit Rating",
        "Reg. 55 - Credit Rating",
    ),
    "results": (
        "Financial Results",
        "Auditors Report",
        "Limited Review Report",
        "Impact of Audit Qualifications",
        "Reg. 52 - Financial Result",
        "Reg. 52 - Declaration for Audit Report/s with Unmodified Opinion(s)",
        "Reg. 52 - Statement of Impact on Audit Qualifications",
        "Integrated Filing (Financial)",
    ),
    "guidance": (
        "Investor Presentation",
        "Earnings Call Transcript",
        "Monthly Business Updates",
        "Monthly AUM Disclosure",
        "Quarterly AUM Disclosure",
    ),
    "exchange_query": (
        "Clarification",
        "Clarification of News Item",
    ),
    "governance": (
        "Appointment of Chairman", "Appointment of Chairman and Managing Director",
        "Appointment of Chief Executive Officer (CEO)", "Appointment of Chief Financial Officer (CFO)",
        "Appointment of Company Secretary / Compliance Officer", "Appointment of Director",
        "Appointment of Managing Director", "Appointment of Managing Director & CEO",
        "Appointment of Statutory Auditor/s",
        "Resignation of Chairman", "Resignation of Chairman and Managing Director",
        "Resignation of Chief Executive Officer (CEO)", "Resignation of Chief Financial Officer (CFO)",
        "Resignation of Company Secretary / Compliance Officer", "Resignation of Director",
        "Resignation of Managing Director", "Resignation of Statutory Auditors",
        "Cessation", "Retirement", "Demise", "Change in Management", "Change in Directorate",
        "Change in Auditors",
        "Change in Directors/ Key Managerial Personnel/ Auditor/ Compliance Officer/ Share Transfer Agent",
        "Giving of guarantees or indemnity (Sub-para 11-Para B)",
        "Integrated Filing (Governance)",
    ),
    "corp_action": (
        "Bonus", "Book Closure", "Dividend", "Record Date", "Sub-division / Stock Split",
        "Consolidation of Shares", "Buy back", "Closure of Buy Back", "Public Announcement-Buyback of Shares",
        "Post Buyback Public Announcement", "Daily Buy Back of equity shares",
        "Declaration of Book Closure/ Record Date", "Cancellation of Dividend", "Allotment of ESOP / ESPS",
        "Delisting",
    ),
}

# Labels that say nothing about the event. Kept apart from "unmapped" so a new BSE label surfaces.
VAGUE = (
    "General", "Others", "Press Release / Media Release", "Press Release / Media Release (Revised)",
    "Press release (Revised)", "Agreement", "Agreements (Sub-para 5-Para B)",
    "Agreements, Contracts, Arrangements, MOU", "Intimation on Agreements",
    "Memorandum of Understanding /Agreements", "Meeting Updates", "Revision of outcome",
    "Public Announcement", "Sale of shares", "Outcome without intimation",
    "Reg. 51 (1), (2) - Price Sensitive information / disclosure of event / Information",
)

# Categories whose every unlisted subcategory is housekeeping: meetings, insider-trading and
# takeover-code returns. "Company Update" is not here: a new label under it falls to "other" and
# is reported by unmapped(). "Others" is debt-market and compliance returns once its few
# material labels (defaults, ratings, results) are mapped above.
ROUTINE_CATEGORIES = ("Board Meeting", "AGM/EGM", "Insider Trading / SAST", "New Listing")


def _norm(s: str | None) -> str:
    """BSE's labels with spacing collapsed: "List of Creditors " and "(CIRP)" with two spaces
    before it are the same labels as without."""
    return re.sub(r"\s+", " ", s if isinstance(s, str) else "").strip().lower()


SUB_TO_CATEGORY = {_norm(name): cat for cat, names in _RAW.items() for name in names}
_VAGUE = {_norm(x) for x in VAGUE}
_ROUTINE = {_norm(x) for x in ROUTINE_CATEGORIES}


def categorise(category: str | None, subcategory: str | None, headline: str | None = None) -> str:
    """zen's category for one BSE filing. An analyst meet is guidance when BSE's subject says it
    is the outcome (the transcript, the presentation given) and routine when it is the schedule,
    as NSE's labels split them."""
    sub, cat = _norm(subcategory), _norm(category)
    if sub == _norm("Analyst / Investor Meet"):
        return "guidance" if "outcome" in _norm(headline) else "routine"
    if sub in SUB_TO_CATEGORY:
        return SUB_TO_CATEGORY[sub]
    if sub in _VAGUE:
        return "other"
    if cat in _ROUTINE:
        return "routine"
    if cat == _norm("Corp. Action") or cat == _norm("Corp Action"):
        return "corp_action"
    if cat == _norm("Result"):
        return "results"
    if cat == _norm("Others"):          # debt-market and compliance returns, once the few above are taken
        return "routine" if sub else "other"
    if cat == _norm("Company Update"):
        return "routine" if sub in _ROUTINE_SUBS else "other"
    return "other"


# Company Update and Others labels that are housekeeping. Listed, so an unlisted one is reported.
_ROUTINE_SUBS = {_norm(x) for x in (
    "Analyst / Investor Meet", "Newspaper Publication", "Certificate under Reg. 74 (5) of SEBI (DP) Regulations, 2018",
    "Closure of Trading Window", "Code of Conduct under SEBI (PIT) Regulations, 2015",
    "Trading Plan under SEBI (PIT) Regulations, 2015", "Amendments to Memorandum & Articles of Association",
    "Change of Name", "Change in Registered Office", "Change in Registered Office Address",
    "Change in Corporate Office Address", "Change in Financial Year", "Board Meeting Adjourned",
    "Board Meeting Cancelled", "Board Meeting Deferred", "Board Meeting Postponed", "Board Meeting Rescheduled",
    "Loss of Share Certificate and Issue of Duplicate Share Certificate or Letter of Confirmation",
    "Notice Of Shareholders Meetings - Annual General Meeting",
    "Notice Of Shareholders Meetings - Extra-ordinary Meeting",
    "Notice Of Shareholders Meetings - NCLT/ Court Convened Meeting",
    "Notice Of Shareholders Meetings - Postal Ballot", "NCLT/ Court Convened Meeting",
    "Reg. 13(3) - Statement of Investor Complaints", "Annual Disclosure – Investor Complaints",
    "Reg. 32 (1), (3) - Statement of Deviation & Variation",
    "Reg. 40 (10) - PCS Certificate for Transfer / Transmission / Transposition",
    "Reg. 7(3) – Compliance Certificate (RTA & Compliance Officer)", "Reg.24(A)-Annual Secretarial Compliance",
    "Utilisation of Funds", "Monitoring Agency Report", "Shareholding", "Incorporation",
    "Declaration of NAV", "NAV Declaration", "Disclosure of IDR Holding",
    "Disclosure under Clause 35A of the Listing Agreement",
    "Reg. 39 (3) - Details of Loss of Certificate / Duplicate Certificate", "Reg. 34 (1) Annual Report",
    "Business Responsibility and Sustainability Reporting (BRSR)", "57 (5) : intimation after the end of quarter",
    "57 (4) : Prior intimation to the beginning of the quarter", "Certificate from CEO/CFO",
    "Details of Compliance officer and RTA of the Company", "Reg. 53 - Annual Report",
    "Record Date/Book Closure as per Regulation (60)",
)}


def unmapped(df: pd.DataFrame) -> pd.Series:
    """Labels that fell to "other" without being listed as vague, with their counts: a new BSE
    label must surface here rather than join the noise unnoticed."""
    if df.empty:
        return pd.Series(dtype=int)
    sub = df["subcategory"].map(_norm)
    hit = (df["zen_category"] == "other") & ~sub.isin(_VAGUE)
    return (df.loc[hit, "category"].fillna("") + " | " + df.loc[hit, "subcategory"].fillna("")).value_counts()


# --------------------------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------------------------

def session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def _dt(raw) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw)[:26])
    except ValueError:
        return None


def _clean(text, limit: int = 500) -> str | None:
    t = re.sub(r"<br\s*/?>", " ", str(text or ""), flags=re.I)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:limit] or None


def parse_rows(rows: list[dict]) -> pd.DataFrame:
    """BSE's rows for a day as stored rows (ISIN is joined later, from the scrip list)."""
    out = []
    for r in rows:
        when = _dt(r.get("DissemDT")) or _dt(r.get("NEWS_DT"))
        news_id, scrip = (r.get("NEWSID") or "").strip(), r.get("SCRIP_CD")
        if not when or not news_id or scrip in (None, ""):
            continue
        category = _clean(r.get("CATEGORYNAME"), 120)
        subcategory = _clean(r.get("SUBCATNAME"), 300)
        subject = _clean(r.get("NEWSSUB"), 500)
        zen_category = categorise(category, subcategory, subject)
        out.append({
            "news_id": news_id,
            "dissem_dt": when,
            "trade_date": _trade_date(when),
            "scrip_cd": int(scrip),
            "isin": None,
            "company": _clean(r.get("SLONGNAME"), 200),
            "category": category,
            "subcategory": subcategory,
            "zen_category": zen_category,
            # A housekeeping filing's headline is boilerplate; the label says all of it.
            "headline": None if zen_category == "routine" else (_clean(r.get("HEADLINE"), HEADLINE_CHARS)
                                                                or _clean(subject, HEADLINE_CHARS)),
            "attachment": _clean(r.get("ATTACHMENTNAME"), 200),
        })
    if not out:
        return pd.DataFrame(columns=COLUMNS)
    return pd.DataFrame(out, columns=COLUMNS).drop_duplicates("news_id", keep="last")


def fetch_day(d: date, s: requests.Session | None = None, pause: float = 0.25, tries: int = 4) -> pd.DataFrame | None:
    """Every filing BSE disseminated on `d`, or None when the day could not be read in full: a
    request that kept failing, or pages that do not add up to the count BSE reports."""
    s = s or session()
    rows, page, expected = [], 1, None
    while True:
        url = API.format(page=page, d=f"{d:%Y%m%d}")
        payload = None
        for attempt in range(tries):
            try:
                r = s.get(url, timeout=45)
                if r.status_code in (403, 429, 500, 502, 503, 504):
                    raise requests.HTTPError(f"HTTP {r.status_code}")
                r.raise_for_status()
                payload = r.json()
                break
            except (requests.RequestException, ValueError) as e:
                log.info("%s page %d: %s (try %d)", d, page, e, attempt + 1)
                time.sleep(2.0 * (attempt + 1) ** 2)
        if payload is None:
            return None
        table = payload.get("Table") or []
        if expected is None:
            expected = ((payload.get("Table1") or [{}])[0] or {}).get("ROWCNT")
        rows += table
        if len(table) < PAGE:
            break
        page += 1
        time.sleep(pause)
    df = parse_rows(rows)
    if expected is not None and len(rows) != int(expected):
        log.warning("%s: %d rows read, BSE reports %s; the day is left for a later run", d, len(rows), expected)
        return None
    return df


# --------------------------------------------------------------------------------------------
# Material labels by month
# --------------------------------------------------------------------------------------------
#
# Fetching every filing a day at a time is about 30,000 requests since 2018, and BSE answers a
# full day's page in about three seconds. Research needs the labels that carry an event, and
# BSE's filter takes one label over one calendar month (a year range returns nothing) with an
# exact count: June 2023's "Award of Order / Receipt of Order" is 60 either way, and 2023's
# months add up to 882, the year NSE labelled none. The pairs below are BSE's own strings,
# spacing included, from its subcategory lists (DDLSubCategoryData), first the categories NSE
# leaves unlabelled from January 2023 to 22 September 2024.

GAP_LABELS = (           # orders, expansion, contraction, regulatory, licenses
    ("Company Update", "Award of Order / Receipt of Order"),
    ("Company Update", "Bagging/Receiving of orders/contracts (Sub-para 4-Para B)"),
    ("Company Update", "Awarding of order(s)/contract(s) (Sub-para 4-Para B)"),
    ("Company Update", "Reg. 30 - Awarding of orders/contracts"),
    ("Company Update", "Capacity addition (Sub-para 3-Para B)"),
    ("Company Update", "Product launch (Sub-para 3-Para B)"),
    ("Company Update", "Adoption of new line(s) of business (Sub-para 2-Para B)"),
    ("Company Update", "Arrangements (Sub-para 2-Para B)"),
    ("Company Update", "Commencement or postponement (Sub-para 1-Para B)"),
    ("Company Update", "Closure of operations (Sub-para 2-Para B)"),
    ("Company Update", "Disruption of operations (Sub-para 6-Para B)"),
    ("Company Update", "Strike"),
    ("Company Update", "Strikes /Lockouts / Disturbances"),
    ("Company Update", "Amendment or termination of orders/contracts (Sub-para 4-Para B)"),
    ("Company Update", "Pendency of any litigation(s) or dispute(s) (Sub-para 8-Para B)"),
    ("Company Update", "Delay or default in the payment of fines (Sub-para 13-Para B)"),
    ("Company Update", "Frauds/defaults/arrest - employee (Sub-para 9-Para B)"),
    ("Company Update", "Intimation on Fraud"),
    ("Company Update", "Change in regulatory framework (Sub-para 7-Para B)"),
    ("Company Update", "Corporate Insolvency Resolution Process"),
    ("Company Update", "Initiation of Corporate Insolvency Resolution Process (CIRP) by Corporate Applicant"),
    ("Company Update", "Initiation of Corporate Insolvency Resolution Process (CIRP) by Financial Creditors"),
    ("Company Update", "Admission of application by Tribunal"),
    ("Company Update", "Appointment of Interim Resolution Professional (IRP)"),
    ("Company Update", "Replacement of Interim Resolution Professional (IRP)"),
    ("Company Update", "Approval of Resolution plan by Tribunal"),
    ("Company Update", "Filing of Resolution plan with Tribunal"),
    ("Company Update", "Rejection of Resolution pan by Tribunal"),
    ("Company Update", "Resolution Plans received by Resolution Professional"),
    ("Company Update", "Invitation of Resolution Plan"),
    ("Company Update", "Intimation of meeting of Committee of Creditors"),
    ("Company Update", "Outcome of meeting of Committee of Creditors"),
    ("Company Update", "List of Creditors "),
    ("Company Update", "Liquidation - Corporate Insolvency Resolution Process  (CIRP)"),
    ("Company Update", "Updates - Corporate Insolvency Resolution Process  (CIRP)"),
    ("Company Update", "Corporate Debt Restructuring"),
    ("Company Update", "Intimation on OTS and Restructuring Loans or Borrowings"),
    ("Company Update", "Winding-up"),
    ("Company Update", "Public Announcement-Delisting"),
    ("Others", "Disclosures by listed entities of defaults on payment of interest/ repayment of principal amount "
               "for loans including revolving facilities like cash credit from banks / financial institutions."),
    ("Others", "Disclosures by listed entities of defaults on payment of interest/ repayment of principal amount "
               "for unlisted debt securities i.e. NCDs and NCRPS."),
    ("Others", "Quarterly Disclosures by listed entities of defaults on payment of interest/ repayment of principal "
               "amount on loans from banks / financial institutions and unlisted debt securities."),
    ("Company Update", "Key licenses/regulatory approvals (Sub-para 12-Para B)"),
)
OTHER_MATERIAL_LABELS = (  # mna, capital, ratings: NSE labels these from 2022, BSE adds 2018 to 2021
    ("Company Update", "Acquisition"),
    ("Company Update", "Amalgamation/ Merger"),
    ("Corp. Action", "Amalgamation / Merger / Demerger"),
    ("Company Update", "Scheme of Arrangement"),
    ("Company Update", "De-merger"),
    ("Company Update", "Open Offer"),
    ("Company Update", "Open Offer - Updates"),
    ("Company Update", "Public Announcement-Open Offer"),
    ("Company Update", "Post Offer Public Announcement"),
    ("Company Update", "Sale or disposal"),
    ("Company Update", "Diversification / Disinvestment"),
    ("Company Update", "Restructuring"),
    ("Company Update", "Other Restructuring"),
    ("Company Update", "Update-Acquisition/Scheme/Sale/Disposal/Reg30"),
    ("Company Update", "Slump Sale"),
    ("Company Update", "Joint Venture"),
    ("Company Update", "Voluntary Delisting"),
    ("Company Update", "Change in Management Control"),
    ("Company Update", "Allotment of Equity Shares"),
    ("Company Update", "Allotment of Warrants"),
    ("Company Update", "Alteration of Capital"),
    ("Company Update", "Increase of Authorised Capital"),
    ("Company Update", "Issue of Securities"),
    ("Company Update", "Preferential Issue"),
    ("Company Update", "Qualified Institutional Placement"),
    ("Company Update", "Offer for Sale"),
    ("Company Update", "Institutional Placement Programme"),
    ("Company Update", "FCCBs"),
    ("Company Update", "Raising of Funds"),
    ("Company Update", "Debt Securities"),
    ("Company Update", "Funds raising by issuance of Debt Securities by Large Entities"),
    ("Company Update", "Options to purchase securities ESOPS (Sub-para 10-Para B)"),
    ("Company Update", "Annulment / Re-issue of forfeited shares"),
    ("Corp. Action", "Bonds / Right issue"),
    ("Corp. Action", "Capital Reduction"),
    ("Company Update", "Credit Rating"),
    ("Others", "Reg. 55 - Credit Rating"),
)
MATERIAL_LABELS = GAP_LABELS + OTHER_MATERIAL_LABELS
RANGES_FILE = OUT_DIR / "ranges.csv"
FILTER_URL = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"


def fetch_label(category: str, subcategory: str, a: date, b: date, s: requests.Session | None = None,
                pause: float = 0.25, tries: int = 4) -> pd.DataFrame | None:
    """Every filing BSE labelled `subcategory` between a and b (one calendar month at most), or
    None when the pages did not add up to BSE's count or a request kept failing."""
    s = s or session()
    rows, page, expected = [], 1, None
    while True:
        params = {"pageno": page, "strCat": category, "strPrevDate": f"{a:%Y%m%d}", "strScrip": "", "strSearch": "P",
                  "strToDate": f"{b:%Y%m%d}", "strType": "C", "subcategory": subcategory}
        payload = None
        for attempt in range(tries):
            try:
                r = s.get(FILTER_URL, params=params, timeout=45)
                if r.status_code in (403, 429, 500, 502, 503, 504):
                    raise requests.HTTPError(f"HTTP {r.status_code}")
                r.raise_for_status()
                payload = r.json()
                break
            except (requests.RequestException, ValueError) as e:
                log.info("%s %s page %d: %s (try %d)", subcategory[:40], a, page, e, attempt + 1)
                time.sleep(2.0 * (attempt + 1) ** 2)
        if payload is None:
            return None
        table = payload.get("Table") or []
        if expected is None:
            expected = ((payload.get("Table1") or [{}])[0] or {}).get("ROWCNT")
        rows += table
        if len(table) < PAGE:
            break
        page += 1
        time.sleep(pause)
    if expected is not None and len(rows) != int(expected):
        log.warning("%s %s: %d rows read, BSE reports %s", subcategory[:40], a, len(rows), expected)
        return None
    if expected is None and rows:
        return None                           # rows without a count: not a reply this module trusts
    return parse_rows(rows)


def done_ranges(path: Path = RANGES_FILE) -> set[tuple[str, str]]:
    if not path.exists():
        return set()
    with path.open(newline="", encoding="utf-8") as f:
        return {(r["month"], r["subcategory"]) for r in csv.DictReader(f)}


def mark_ranges(done: list[tuple[str, str, str, int]], path: Path = RANGES_FILE) -> None:
    """Append (month, category, subcategory, rows) lines for fetched label-months."""
    new = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["month", "category", "subcategory", "rows"])
        w.writerows(done)


def fetch_scrips(s: requests.Session | None = None) -> pd.DataFrame:
    """BSE's scrip list (active, suspended and delisted), for the ISIN of each scrip code."""
    s = s or session()
    frames = []
    for status in ("Active", "Suspended", "Delisted"):
        r = s.get(SCRIPS_API.format(status=status), timeout=120)
        r.raise_for_status()
        rows = r.json()
        frames.append(pd.DataFrame([{
            "scrip_cd": int(x["SCRIP_CD"]),
            "isin": (x.get("ISIN_NUMBER") or "").strip() or None,
            "name": (x.get("Issuer_Name") or x.get("Scrip_Name") or "").strip() or None,
            "bse_symbol": (x.get("scrip_id") or "").strip() or None,
            "status": status,
        } for x in rows if str(x.get("SCRIP_CD") or "").isdigit()]))
    out = pd.concat(frames, ignore_index=True)
    if len(out) < 5000:
        raise RuntimeError(f"BSE scrip list came back short ({len(out)} rows)")
    return out.drop_duplicates("scrip_cd", keep="first")


def save_scrips(df: pd.DataFrame, path: Path = SCRIPS_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.sort_values("scrip_cd").to_parquet(path, index=False, compression="zstd")


def load_scrips(path: Path = SCRIPS_FILE) -> pd.DataFrame:
    return pd.read_parquet(path) if path.exists() else pd.DataFrame(columns=["scrip_cd", "isin"])


def with_isin(df: pd.DataFrame, scrips: pd.DataFrame) -> pd.DataFrame:
    if df.empty or scrips.empty:
        return df
    m = dict(zip(scrips["scrip_cd"].astype(int), scrips["isin"]))
    out = df.copy()
    out["isin"] = out["isin"].where(out["isin"].notna(), out["scrip_cd"].astype(int).map(m))
    return out


# --------------------------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------------------------

def _typed(df: pd.DataFrame) -> pd.DataFrame:
    out = df.reindex(columns=COLUMNS).copy()
    out["dissem_dt"] = pd.to_datetime(out["dissem_dt"]).astype("datetime64[us]")
    out["trade_date"] = pd.to_datetime(out["trade_date"]).astype("datetime64[us]")
    out["scrip_cd"] = out["scrip_cd"].astype("int64")
    for c in COLUMNS:
        if c not in ("dissem_dt", "trade_date", "scrip_cd"):
            out[c] = out[c].astype("object")
    return out


def write(df: pd.DataFrame, out_dir: Path = OUT_DIR) -> list[Path]:
    """Merge rows into one parquet per calendar month of dissemination, keyed by news_id."""
    if df.empty:
        return []
    written = []
    months = pd.to_datetime(df["dissem_dt"]).dt.strftime("%Y-%m")
    for month, chunk in df.groupby(months):
        p = out_dir / month[:4] / f"{month}.parquet"
        p.parent.mkdir(parents=True, exist_ok=True)
        parts = ([pd.read_parquet(p)] if p.exists() else []) + [chunk]
        merged = pd.concat([_typed(x) for x in parts if len(x)], ignore_index=True)
        merged = merged.drop_duplicates("news_id", keep="last").sort_values(["dissem_dt", "news_id"])
        merged.to_parquet(p, index=False, compression="zstd")
        written.append(p)
    return written


def done_days(path: Path = DAYS_FILE) -> dict[date, int]:
    if not path.exists():
        return {}
    with path.open(newline="") as f:
        return {date.fromisoformat(r["date"]): int(r["rows"]) for r in csv.DictReader(f)}


def mark_done(days: dict[date, int], path: Path = DAYS_FILE) -> None:
    held = done_days(path)
    held.update(days)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "rows"])
        for d in sorted(held):
            w.writerow([d.isoformat(), held[d]])


def ensure_schema(con) -> None:
    con.execute(SCHEMA)


def rebuild(con, out_dir: Path = OUT_DIR) -> int:
    """Recreate the table from committed parquet."""
    ensure_schema(con)
    con.execute("DELETE FROM bse_announcements")
    files = sorted(out_dir.glob("*/*.parquet")) if out_dir.exists() else []
    if not files:
        return 0
    pattern = str(out_dir / "*" / "*.parquet")
    con.execute(f"INSERT INTO bse_announcements SELECT {', '.join(COLUMNS)} FROM read_parquet('{pattern}', union_by_name=true)")
    return con.execute("SELECT count(*) FROM bse_announcements").fetchone()[0]
