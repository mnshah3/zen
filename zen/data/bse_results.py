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
           "profit_continuing", "profit", "eps_basic", "equity_capital", "face_value", "shares"]
SCHEMA = """CREATE TABLE IF NOT EXISTS bse_results (scrip_cd BIGINT, qtr DOUBLE, label VARCHAR, kind VARCHAR,
    period_start DATE, period_end DATE, audited BOOLEAN, filed_dt TIMESTAMP, revenue DOUBLE, other_income DOUBLE,
    total_income DOUBLE, expenditure DOUBLE, interest DOUBLE, depreciation DOUBLE, pbt DOUBLE, tax DOUBLE,
    profit_continuing DOUBLE, profit DOUBLE, eps_basic DOUBLE, equity_capital DOUBLE, face_value DOUBLE, shares DOUBLE);"""

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
    (re.compile(r"^net profit \(\+\)/ ?loss \(-\) from ordinary activities after tax$"), "profit_continuing"),
    (re.compile(r"^net profit$"), "profit"),
    (re.compile(r"^equity capital$"), "equity_capital"),
    (re.compile(r"^face value"), "face_value"),
]
# basic EPS: the figure for all operations where one is given, else continuing, else the old label
_EPS = [re.compile(r"^basic for discontinued & continuing operation$"), re.compile(r"^basic eps after extraordinary items$"),
        re.compile(r"^basic eps for continuing operation$")]
RUPEES = {"revenue", "other_income", "total_income", "expenditure", "interest", "depreciation", "pbt", "tax",
          "profit_continuing", "profit", "equity_capital"}


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
    """The filings BSE lists for a company: standalone quarters and years."""
    rows = []
    for a in table or []:
        q = (a.get("Quarter") or "").split(";")
        if not q or not q[0].startswith("Standalone") or a.get("qtr") is None or a.get("scripcode") is None:
            continue                                    # BSE lists some filings without a quarter number
        filed = a.get("Filing_Date_Time")
        rows.append({"scrip_cd": int(a["scripcode"]), "qtr": float(a["qtr"]), "label": q[0].split("-", 1)[1],
                     "kind": a.get("Fld_QuarterName") or "", "filed_dt": pd.Timestamp(filed) if filed else pd.NaT})
    return pd.DataFrame(rows, columns=["scrip_cd", "qtr", "label", "kind", "filed_dt"]).drop_duplicates(["qtr"])


def archive(session, scrip: int, timeout: float = 30) -> pd.DataFrame:
    r = session.get(f"{API}/Result_Arch_ng/w", params={"scrip_cd": scrip}, timeout=timeout)
    r.raise_for_status()
    return parse_archive((r.json() or {}).get("Table") or [])


def detail(session, scrip: int, qtr: float, timeout: float = 30) -> dict:
    r = session.get(f"{API}/Corp_detailedResult_Transpose_ng/w", params={"Scrip_cd": scrip, "Qtr": f"{qtr:.2f}"},
                    timeout=timeout)
    r.raise_for_status()
    return parse_detail((r.json() or {}).get("table1") or [])


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


def fetch_company(session, scrip: int, want, pause: float = 1.0, root: Path = DIR) -> int:
    """Fetch the filings of `scrip` that `want(archive_row)` selects and are not stored yet."""
    arch = archive(session, scrip)
    time.sleep(pause)
    have = set(stored(scrip, root)["qtr"])
    rows = []
    for a in arch.itertuples():
        if a.qtr in have or not want(a):
            continue
        d = detail(session, scrip, a.qtr)
        time.sleep(pause)
        if not d:
            continue
        rows.append({"scrip_cd": scrip, "qtr": a.qtr, "label": a.label, "kind": a.kind, "filed_dt": a.filed_dt, **d})
    return save(scrip, rows, root)


def rebuild(con, root: Path = DIR) -> int:
    con.execute(SCHEMA)
    con.execute("DELETE FROM bse_results")
    if root.exists() and any(root.glob("*.parquet")):
        con.execute(f"INSERT INTO bse_results SELECT {', '.join(COLUMNS)} FROM read_parquet('{(root / '*.parquet').as_posix()}', union_by_name=true)")
    return con.execute("SELECT count(*) FROM bse_results").fetchone()[0]
