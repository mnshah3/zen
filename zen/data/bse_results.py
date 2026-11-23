"""Quarterly results from BSE's own results database, for the years and companies NSE's XBRL does not
cover. Research data: kept on the machine that fetched it, never committed (BSE's terms prohibit
reproduction and redistribution of its data), and never read by a strategy.

SOURCE, AS MEASURED ON 19 OCT 2026 (from a home connection; BSE refuses GitHub's runners)

  api.bseindia.com/BseIndiaAPI/api/Result_Arch_ng/w?scrip_cd=<code>
      every results filing BSE holds for a company: Abbott India's 128 go back to March 2001. Each
      has BSE's quarter number (qtr: June 2026 is 130, one a quarter; ".5" marks a year), the
      quarter's name, whether it is a quarter or a year, the filing time (from June 2016 only) and
      the XBRL file (from June 2018 only, as on NSE).
  api.bseindia.com/BseIndiaAPI/api/Corp_detailedResult_Transpose_ng/w?Scrip_cd=<code>&Qtr=<qtr>
      that filing's figures "as furnished by the company at the relevant point of time", standalone,
      in Rs million: revenue, other income, expenditure, interest, depreciation, profit before tax,
      tax, net profit, equity capital, face value, EPS. The consolidated view of the page calls the
      same service and shows nothing: BSE's database holds standalone results (quarterly
      consolidated results were not required before April 2019 anyway).

AGREEMENT WITH NSE (40 random companies, 120 quarters, 2019 to 2024, standalone against zen's NSE
XBRL): revenue equal to 0.1% in 98.3%, net profit in 96.6% (98.3% within 1%), basic EPS in 95.7%
(99.1% within 1%). Rs million confirmed by every pair (ratio exactly 1e-6 before conversion).
Diluted EPS is sometimes wrong (Abbott June 2011: basic 12.50, diluted 8.05), so basic EPS is kept, and a
0.00 beside a profit is read as a blank line, not an EPS.

A FINANCE COMPANY'S FILINGS have no figures in that service; from 20 Nov 2026 they are read from the
filing's own XBRL with zen's NSE parser instead (THE XBRL FALLBACK, below).

WHEN EACH RESULT WAS PUBLIC: BSE gives the filing time from June 2016. Before that a row has none
here, and point-in-time use needs NSE's own broadcast time (financials_legacy.listing, from 2007);
a row with neither is never used as of a date.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

API = "https://api.bseindia.com/BseIndiaAPI/api"
DIR = Path("data/bse_results")
MILLION = 1e6
COLUMNS = ["scrip_cd", "qtr", "label", "kind", "period_start", "period_end", "audited", "filed_dt",
           "revenue", "other_income", "total_income", "expenditure", "interest", "depreciation", "pbt", "tax",
           "profit_continuing", "profit", "eps_basic", "equity_capital", "face_value", "shares", "source", "xbrl",
           "pbt_before_exceptional", "exceptional"]
# source: "xbrl" where the figures were read from the filing's XBRL (THE XBRL FALLBACK below), empty where
# from the detailed-results service; xbrl: the document's address; profit before exceptional items and
# the exceptional items themselves. All four last, added 20 Nov 2026 (a file written before has none of
# them, and union_by_name reads it with them empty).
SCHEMA = """CREATE TABLE IF NOT EXISTS bse_results (scrip_cd BIGINT, qtr DOUBLE, label VARCHAR, kind VARCHAR,
    period_start DATE, period_end DATE, audited BOOLEAN, filed_dt TIMESTAMP, revenue DOUBLE, other_income DOUBLE,
    total_income DOUBLE, expenditure DOUBLE, interest DOUBLE, depreciation DOUBLE, pbt DOUBLE, tax DOUBLE,
    profit_continuing DOUBLE, profit DOUBLE, eps_basic DOUBLE, equity_capital DOUBLE, face_value DOUBLE, shares DOUBLE,
    source VARCHAR, xbrl VARCHAR, pbt_before_exceptional DOUBLE, exceptional DOUBLE);"""

# label (lower case, spaces squeezed) -> field; the first match of each field wins unless noted
_LABELS = [
    (re.compile(r"^net sales/revenue from operations$"), "revenue"),
    (re.compile(r"^other income$"), "other_income"),
    (re.compile(r"^total income$"), "total_income"),
    (re.compile(r"^expenditure$"), "expenditure"),
    (re.compile(r"^(interest|finance costs?)$"), "interest"),
    (re.compile(r"^depreciation"), "depreciation"),
    (re.compile(r"^profit \(\+\)/ ?loss \(-\) from ordinary activities before tax$"), "pbt"),
    (re.compile(r"^tax$"), "tax"),
    (re.compile(r"^profit after interest but before exceptional items$"), "pbt_before_exceptional"),
    (re.compile(r"^exceptional items?$"), "exceptional"),
    (re.compile(r"^net profit \(\+\)/ ?loss \(-\) from ordinary activities after tax$"), "profit_continuing"),
    (re.compile(r"^net profit$"), "profit"),
    (re.compile(r"^equity capital$"), "equity_capital"),
    (re.compile(r"^face value"), "face_value"),
]
# basic EPS: the figure for all operations where one is given, else continuing, else the old label
_EPS = [re.compile(r"^basic for discontinued & continuing operation$"), re.compile(r"^basic eps after extraordinary items$"),
        re.compile(r"^basic eps for continuing operation$")]
RUPEES = {"revenue", "other_income", "total_income", "expenditure", "interest", "depreciation", "pbt", "tax",
          "profit_continuing", "profit", "equity_capital", "pbt_before_exceptional", "exceptional"}


def _num(v) -> float | None:
    try:
        return float(str(v).replace(",", "").strip())
    except ValueError:
        return None


def _day(v) -> datetime | None:
    for fmt in ("%d-%b-%y", "%d-%b-%Y"):
        try:
            return datetime.strptime(str(v).strip(), fmt)
        except ValueError:
            pass
    return None


def parse_detail(rows: list[dict]) -> dict:
    """One filing's figures from the detailed-results service, in rupees (EPS and face value as given)."""
    out, eps = {}, {}
    for r in rows:
        label = re.sub(r"\s+", " ", (r.get("fld_desc") or "").strip()).lower()
        value = r.get("Value")
        if label == "type":
            out["audited"] = str(value).strip().lower() == "audited"
            continue
        if label == "date begin":
            out["period_start"] = _day(value)
            continue
        if label == "date end":
            out["period_end"] = _day(value)
            continue
        x = _num(value)
        if x is None:
            continue
        for i, rx in enumerate(_EPS):
            if rx.match(label) and i not in eps:
                eps[i] = x
        for rx, field in _LABELS:
            if rx.match(label) and field not in out:
                out[field] = x * MILLION if field in RUPEES else x
    # a 0.00 beside a profit is a line the company left blank (from 2025 BSE often shows the total
    # line as 0.0000 and the figure under continuing operations: Abbott June 2026, 201.66), not an EPS
    for i in range(len(_EPS)):
        if i in eps and (eps[i] != 0 or not out.get("profit")):
            out["eps_basic"] = eps[i]
            break
    if out.get("equity_capital") and out.get("face_value"):
        out["shares"] = out["equity_capital"] / out["face_value"]
    return out


def parse_archive(table: list[dict]) -> pd.DataFrame:
    """The filings BSE lists for a company: standalone quarters and years, each with its standalone
    XBRL document's address where BSE gives one (`xbrl`, from June 2018)."""
    rows = []
    for a in table or []:
        q = (a.get("Quarter") or "").split(";")
        if not q or not q[0].startswith("Standalone") or a.get("qtr") is None or a.get("scripcode") is None:
            continue                                    # BSE lists some filings without a quarter number
        filed = a.get("Filing_Date_Time")
        rows.append({"scrip_cd": int(a["scripcode"]), "qtr": float(a["qtr"]), "label": q[0].split("-", 1)[1],
                     "kind": a.get("Fld_QuarterName") or "", "filed_dt": pd.Timestamp(filed) if filed else pd.NaT,
                     "xbrl": a.get("stand_xbrl_link") or None})
    return pd.DataFrame(rows, columns=["scrip_cd", "qtr", "label", "kind", "filed_dt", "xbrl"]).drop_duplicates(["qtr"])


# THE XBRL FALLBACK. The detailed-results service above has no figures for a finance company's filings
# (the NBFC layout): Kama Holdings' quarters from June 2019 and Bengal & Assam's from September 2020
# come back as three blank lines (Type, Date Begin, Date End), 721 quarters of 135 companies in zen's
# download of 19-22 Oct 2026. BSE still holds each filing's XBRL (the archive's stand_xbrl_link), in
# the Ind-AS taxonomy NSE's documents use, so zen's own parser reads it (financials.parse_xbrl, with
# the same quarter rule: figures only from a span of a quarter's length). Its lines are put in this
# table's columns and signs: expenses negative, as the service shows them, the expenditure line the
# total expenses including finance costs and depreciation (as the service's own line is in 90% of the
# 10,744 filings since 2018 where it can be told: total income plus expenditure equals profit before
# tax), and profit before tax less tax equal to net profit.
HOST = "https://www.bseindia.com"
# BSE's archive links a finance company's integrated filing (from the March 2025 quarter) to a folder
# that does not hold it: /XBRLFILES/NBFCUploadDocument/Integrated_Finance_NBFC_<code>_<stamp>.xml answers
# 404, while the same file name is served from /XBRLFILES/IFNBFCUploadDocument/ (20 Nov 2026, Kama
# Holdings' Sep 2026 standalone filing: a 404 page at the link, 774,679 bytes of XBRL at the other).
# Older NBFC documents (NBFC_<code>_...) are where the link says.
_NBFC_INTEGRATED = re.compile(r"^/XBRLFILES/NBFCUploadDocument/(Integrated_Finance_NBFC_[^/]+)$", re.IGNORECASE)
XBRL_SHARES = {"PaidUpValueOfEquityShareCapital": "equity_capital", "FaceValueOfEquityShareCapital": "face_value"}


def xbrl_address(link: str | None) -> str | None:
    """The full address of the XBRL document an archive row links to, with BSE's misnamed folder for a
    finance company's integrated filing put right; None without a link."""
    if not isinstance(link, str) or not link.strip():
        return None                     # (an archive row without a link reaches here as NaN from its frame)
    link = link.strip()
    if link.startswith("http"):
        return link
    m = _NBFC_INTEGRATED.match(link)
    if m:
        link = f"/XBRLFILES/IFNBFCUploadDocument/{m.group(1)}"
    return HOST + link


def _reporting_end(content: bytes):
    """The document's own DateOfEndOfReportingPeriod, as a date (the first one stated), or None."""
    m = re.search(rb"DateOfEndOfReportingPeriod[^>]*>\s*(\d{4}-\d{2}-\d{2})", content)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1).decode(), "%Y-%m-%d").date()
    except ValueError:
        return None


def from_xbrl(content: bytes, label: str | None = None) -> dict:
    """A quarter's figures from its XBRL document, in this table's columns and signs (THE XBRL FALLBACK
    above), or {} when the document has no quarter's income statement, or states an end of period
    that is not the month of `label` ("Sep-26")."""
    from zen.data import financials
    f = financials.parse_xbrl(content)
    if not any(f.get(k) is not None for k in ("revenue", "total_income", "profit_reported")):
        return {}                     # no quarter's income statement (parse_xbrl refuses a longer span)
    end = _reporting_end(content)
    if label:
        want = pd.Period(datetime.strptime(label, "%b-%y"), "M")
        if end is None or pd.Period(end, "M") != want:
            return {}
    neg = lambda v: None if v is None else -abs(float(v))                       # noqa: E731
    out = {"period_end": datetime(end.year, end.month, end.day) if end else None,
           "revenue": f.get("revenue"), "other_income": f.get("other_income"), "total_income": f.get("total_income"),
           "expenditure": neg(f.get("total_expenses")), "interest": neg(f.get("finance_costs")),
           "depreciation": neg(f.get("depreciation")), "pbt": f.get("pbt"), "tax": f.get("tax"),
           "pbt_before_exceptional": f.get("pbt_before_exceptional"), "exceptional": f.get("exceptional_items"),
           "profit_continuing": f.get("profit_continuing"), "profit": f.get("profit_reported"),
           "eps_basic": f.get("eps_basic"), "source": "xbrl"}
    if end is not None and f.get("quarter_span_days"):
        start = pd.Timestamp(end) - pd.Timedelta(days=int(f["quarter_span_days"]))
        out["period_start"] = start.to_pydatetime()
    sh = financials.parse_xbrl(content, tags=XBRL_SHARES)
    if sh.get("equity_capital") and sh.get("face_value"):
        out["equity_capital"], out["face_value"] = sh["equity_capital"], sh["face_value"]
        out["shares"] = sh["equity_capital"] / sh["face_value"]
    return {k: v for k, v in out.items() if v is not None}


def has_figures(d: dict | pd.Series) -> bool:
    """A filing read with figures in it: revenue, total income or net profit."""
    return any(d.get(k) is not None and pd.notna(d.get(k)) for k in ("revenue", "total_income", "profit"))


def archive(session, scrip: int, timeout: float = 30) -> pd.DataFrame:
    r = session.get(f"{API}/Result_Arch_ng/w", params={"scrip_cd": scrip}, timeout=timeout)
    r.raise_for_status()
    return parse_archive((r.json() or {}).get("Table") or [])


def detail(session, scrip: int, qtr: float, timeout: float = 30) -> dict:
    r = session.get(f"{API}/Corp_detailedResult_Transpose_ng/w", params={"Scrip_cd": scrip, "Qtr": f"{qtr:.2f}"},
                    timeout=timeout)
    r.raise_for_status()
    return parse_detail((r.json() or {}).get("table1") or [])


def session():
    """A session for BSE's services that keeps no cookies. From Nov 2026 BSE's bot manager sets two
    cookies (bm_s, bm_so) on the first answer and refuses every later request that sends them back
    with a 403, while a request without them is answered (20 Nov 2026: four archive requests in a row,
    200 then three 403s with the cookies; six in a row, all 200, without)."""
    import requests
    from http.cookiejar import DefaultCookiePolicy
    from zen.data import bse_announcements
    s = requests.Session()
    s.headers.update(bse_announcements.HEADERS)
    s.cookies.set_policy(DefaultCookiePolicy(allowed_domains=[]))
    return s


def path(scrip: int, root: Path = DIR) -> Path:
    return root / f"{scrip}.parquet"


def stored(scrip: int, root: Path = DIR) -> pd.DataFrame:
    p = path(scrip, root)
    return pd.read_parquet(p) if p.exists() else pd.DataFrame(columns=COLUMNS)


def save(scrip: int, rows: list[dict], root: Path = DIR) -> int:
    """Merge rows into <root>/<scrip>.parquet, one row per BSE quarter number (the newest read wins)."""
    if not rows:
        return 0
    root.mkdir(parents=True, exist_ok=True)
    new = pd.DataFrame(rows).reindex(columns=COLUMNS)
    df = pd.concat([stored(scrip, root), new], ignore_index=True).drop_duplicates(["qtr"], keep="last")
    df.sort_values("qtr").to_parquet(path(scrip, root), index=False, compression="zstd")
    return len(rows)


def fetch_xbrl(session, link: str | None, label: str, timeout: float = 60) -> dict:
    """A quarter's figures from the XBRL document an archive row links to (from_xbrl), kept in zen's local
    XBRL copy (zen/data/xbrl_cache.py) before it is read; {} without a document or a quarter in it."""
    from zen.data import xbrl_cache
    url = xbrl_address(link)
    if not url:
        return {}
    content = xbrl_cache.get(url)
    if content is None:
        r = session.get(url, timeout=timeout)
        if r.status_code != 200 or not r.content.lstrip()[:1] == b"<":
            return {}
        content = r.content
        xbrl_cache.put(url, content)
    return {**from_xbrl(content, label), "xbrl": url}


def fetch_company(session, scrip: int, want, pause: float = 1.0, root: Path = DIR) -> int:
    """Fetch the filings of `scrip` that `want(archive_row)` selects and are not stored with figures yet.

    A quarter the detailed-results service has no figures for is read from its XBRL document instead
    (THE XBRL FALLBACK). A quarter stored without figures is asked for again: before 20 Nov 2026 such a
    row was stored as it came (period, revenue and profit empty) and never retried."""
    arch = archive(session, scrip)
    time.sleep(pause)
    st = stored(scrip, root)
    have = set(st.loc[st.apply(has_figures, axis=1), "qtr"]) if len(st) else set()
    rows = []
    try:
        for a in arch.itertuples():
            if a.qtr in have or not want(a):
                continue
            d = _polite(lambda: detail(session, scrip, a.qtr), pause)
            if not has_figures(d) and a.qtr % 1 == 0 and xbrl_address(getattr(a, "xbrl", None)):
                x = _polite(lambda: fetch_xbrl(session, a.xbrl, a.label), pause)
                if has_figures(x):
                    d = {**{k: v for k, v in d.items() if k == "audited"}, **x}
            if not d:
                continue
            rows.append({"scrip_cd": scrip, "qtr": a.qtr, "label": a.label, "kind": a.kind, "filed_dt": a.filed_dt, **d})
    finally:                          # what was read is kept when BSE stops answering part of the way through
        save(scrip, rows, root)
    return len(rows)


def _polite(call, pause: float, tries: int = 3):
    """One request, then the pause; BSE refuses a request now and then (a 403 that clears within a
    minute), so a refusal is waited out twice, 20 and 40 seconds, before it is passed on."""
    for k in range(tries):
        try:
            out = call()
            time.sleep(pause)
            return out
        except Exception:                                            # noqa: BLE001
            if k == tries - 1:
                raise
            time.sleep(20 * (k + 1))


def rebuild(con, root: Path = DIR) -> int:
    # the table is rebuilt whole from the files: a database built before a column was added (20 Nov 2026,
    # 22 columns to 26) is dropped rather than refused. A column no file holds yet is read as empty.
    con.execute("DROP TABLE IF EXISTS bse_results")
    con.execute(SCHEMA)
    if root.exists() and any(root.glob("*.parquet")):
        src = f"read_parquet('{(root / '*.parquet').as_posix()}', union_by_name=true)"
        held = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {src}").fetchall()}
        cols = ", ".join(c if c in held else f"NULL AS {c}" for c in COLUMNS)
        con.execute(f"INSERT INTO bse_results SELECT {cols} FROM {src}")
    return con.execute("SELECT count(*) FROM bse_results").fetchone()[0]
