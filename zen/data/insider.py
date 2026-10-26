"""Insider trades: what a listed company's promoters, directors, key managerial people and employees
report buying and selling in its securities (SEBI's Prohibition of Insider Trading Regulations, 2015,
Regulation 7(2): within two trading days of a trade; NSE publishes each disclosure).

SOURCES, AS MEASURED ON 25 OCT 2026 (both answer zen's NSE session, financials._session)
  to 30 Apr 2026   https://www.nseindia.com/api/corporates-pit?index=equities&symbol=..&from_date=dd-mm-yyyy
                   &to_date=dd-mm-yyyy: one company at a time, every disclosure with its fields
                   (BAJAJFINSV: 526 from Jun 2016; HDFC Bank: 36,094 from Nov 2015, most of them ESOP
                   exercises). A market-wide query answers nothing; the endpoint stops at 30 Apr 2026.
  from 3 May 2026  https://www.nseindia.com/api/corporates-pit-gg?index=equities&from_date=..&to_date=..:
                   NSE's new filing system, market-wide: one row per filing with its broadcast time and
                   an XBRL file (in-bse-co taxonomy) holding each disclosure (person, category,
                   instrument, buy or sell, number and value, holdings before and after, trade dates,
                   mode, exchange). Without dates it lists every filing since 3 May 2026.

KEPT: every disclosure but an employee's ESOP exercise (the bulk of the rows, and no view on the
price); the broadcast time is when it was public. Values in rupees, holdings in percent of the
company. The same disclosure filed again (same company, person, trade dates, instrument, transaction
and number: a revision, or one form filed twice minutes apart, as 469 of the 3,214 new-system
filings to 23 Oct 2026 were) is stored once, with the latest filing's values and the earliest
filing's broadcast time, when it first became public."""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

OLD_URL = "https://www.nseindia.com/api/corporates-pit"
NEW_URL = "https://www.nseindia.com/api/corporates-pit-gg"
NEW_FROM = date(2026, 5, 3)
DIR = Path("data/insider")
RENEW_AFTER = 3
READ = "_read.txt"             # every filing read, so none is read twice (one may store no row of its own)
COLUMNS = ["broadcast_dt", "symbol", "isin", "company", "person", "category", "instrument", "txn", "mode",
           "qty", "value", "pct_before", "pct_after", "trade_from", "trade_to", "intimated", "exchange",
           "regulation", "source", "url"]
KEY = ["symbol", "person", "trade_from", "trade_to", "instrument", "txn", "qty"]
SCHEMA = """CREATE TABLE IF NOT EXISTS insider (broadcast_dt TIMESTAMP, symbol VARCHAR, isin VARCHAR,
    company VARCHAR, person VARCHAR, category VARCHAR, instrument VARCHAR, txn VARCHAR, mode VARCHAR,
    qty DOUBLE, value DOUBLE, pct_before DOUBLE, pct_after DOUBLE, trade_from DATE, trade_to DATE,
    intimated DATE, exchange VARCHAR, regulation VARCHAR, source VARCHAR, url VARCHAR);"""


def _num(x):
    try:
        v = float(str(x).replace(",", ""))
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def _date(x, fmt: str):
    try:
        return datetime.strptime(str(x).strip(), fmt).date()
    except (TypeError, ValueError):
        return None


def _text(x):
    s = str(x).strip() if x is not None else ""
    return None if s in ("", "-", "NA", "Nil", "None", "null") else s


def _keep(df: pd.DataFrame) -> pd.DataFrame:
    """Every disclosure but an employee's ESOP exercise, and none that names neither a person nor a number."""
    if df.empty:
        return df
    esop = df["mode"].fillna("").str.upper().eq("ESOP") & df["category"].fillna("").str.contains("Employee", case=False)
    blank = df["person"].isna() & df["qty"].isna()
    return df[~esop & ~blank].reset_index(drop=True)


# ---------------------------------------------------------------- to 30 Apr 2026: one company at a time
def parse_old(rows: list[dict]) -> pd.DataFrame:
    out = []
    for x in rows or []:
        txn = _text(x.get("tdpTransactionType"))
        qty = _num(x.get("secAcq"))
        out.append({"broadcast_dt": pd.to_datetime(x.get("date"), format="%d-%b-%Y %H:%M", errors="coerce"),
                    "symbol": _text(x.get("symbol")), "isin": None, "company": _text(x.get("company")),
                    "person": _text(x.get("acqName")), "category": _text(x.get("personCategory")),
                    "instrument": _text(x.get("secType")), "txn": txn, "mode": _text(x.get("acqMode")),
                    "qty": qty, "value": _num(x.get("secVal")),
                    "pct_before": _num(x.get("befAcqSharesPer")), "pct_after": _num(x.get("afterAcqSharesPer")),
                    "trade_from": _date(x.get("acqfromDt"), "%d-%b-%Y"), "trade_to": _date(x.get("acqtoDt"), "%d-%b-%Y"),
                    "intimated": _date(x.get("intimDt"), "%d-%b-%Y"), "exchange": _text(x.get("exchange")),
                    "regulation": _text(x.get("anex")), "source": "pit", "url": _text(x.get("xbrl"))})
    df = pd.DataFrame(out, columns=COLUMNS)
    return _keep(df.dropna(subset=["broadcast_dt", "symbol"]))


def fetch_old(session, symbol: str, start: date, end: date, timeout: float = 120.0) -> pd.DataFrame:
    r = session.get(OLD_URL, params={"index": "equities", "symbol": symbol, "from_date": f"{start:%d-%m-%Y}",
                                     "to_date": f"{end:%d-%m-%Y}"}, timeout=timeout)
    r.raise_for_status()
    return parse_old((r.json() or {}).get("data") or [])


# ---------------------------------------------------------------- from 3 May 2026: the filings and their XBRL
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_xbrl(xml_text: str, broadcast: datetime | None, url: str | None = None) -> pd.DataFrame:
    """One filing's XBRL (in-bse-co): a row per disclosure, each in its own context."""
    root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    head, by_ctx = {}, {}
    for el in root.iter():
        ctx = el.attrib.get("contextRef")
        if ctx is None or el.text is None:
            continue
        name, val = _local(el.tag), el.text.strip()
        if name in ("Symbol", "ISINCode", "NameOfTheCompany", "DisclosureUnderRegulation", "DateOfFiling"):
            head.setdefault(name, val)
        else:
            by_ctx.setdefault(ctx, {})[name] = val
    out = []
    for ctx, v in by_ctx.items():
        if "NameOfThePerson" not in v:
            continue
        out.append({"broadcast_dt": pd.Timestamp(broadcast) if broadcast is not None else pd.NaT,
                    "symbol": _text(head.get("Symbol")), "isin": _text(head.get("ISINCode")),
                    "company": _text(head.get("NameOfTheCompany")), "person": _text(v.get("NameOfThePerson")),
                    "category": _text(v.get("CategoryOfPerson")), "instrument": _text(v.get("TypeOfInstrument")),
                    "txn": _text(v.get("SecuritiesAcquiredOrDisposedTransactionType")),
                    "mode": _text(v.get("ModeOfAcquisitionOrDisposal")),
                    "qty": _num(v.get("SecuritiesAcquiredOrDisposedNumberOfSecurity")),
                    "value": _num(v.get("SecuritiesAcquiredOrDisposedValueOfSecurity")),
                    "pct_before": _pct(v.get("SecuritiesHeldPriorToAcquisitionOrDisposalPercentageOfShareholding")),
                    "pct_after": _pct(v.get("SecuritiesHeldPostAcquistionOrDisposalPercentageOfShareholding")),
                    "trade_from": _date(v.get("DateOfAllotmentAdviceOrAcquisitionOfSharesOrSaleOfSharesSpecifyFromDate"), "%Y-%m-%d"),
                    "trade_to": _date(v.get("DateOfAllotmentAdviceOrAcquisitionOfSharesOrSaleOfSharesSpecifyToDate"), "%Y-%m-%d"),
                    "intimated": _date(v.get("DateOfIntimationToCompany"), "%Y-%m-%d"),
                    "exchange": _text(v.get("ExchangeOnWhichTheTradeWasExecuted")),
                    "regulation": _text(head.get("DisclosureUnderRegulation")), "source": "pit-gg", "url": url})
    return _keep(pd.DataFrame(out, columns=COLUMNS))


def _pct(x):
    """The new system's holdings are fractions (0.0912 for 9.12%); the old one's are percent."""
    v = _num(x)
    return None if v is None else round(v * 100.0, 6)


def list_new(session, start: date | None = None, end: date | None = None, timeout: float = 120.0) -> list[dict]:
    params = {"index": "equities"}
    if start and end:
        params.update({"from_date": f"{start:%d-%m-%Y}", "to_date": f"{end:%d-%m-%Y}"})
    r = session.get(NEW_URL, params=params, timeout=timeout)
    r.raise_for_status()
    return (r.json() or {}).get("data") or []


def fetch_new(session, filings: list[dict], have: set | None = None, timeout: float = 20.0, pause: float = 0.35,
              out_dir: Path | None = None, renew=None) -> pd.DataFrame:
    """Each listed filing's XBRL not already read (by its XML address). Every filing read is listed in
    READ; a filing that fails is tried again on the next run. After RENEW_AFTER
    failures in a row, `renew()` gives a fresh session (on 25 Oct 2026 a session NSE had stopped
    answering timed out on every file while a new one read the same file in half a second)."""
    import time
    frames, done, failed = [], [], 0
    for f in filings:
        url = f.get("xmlFileName")
        if not url or (have and url in have):
            continue
        try:
            r = session.get(url, timeout=timeout)
            r.raise_for_status()
            when = pd.to_datetime(f.get("broadcastDateTime"), format="%d-%b-%Y %H:%M:%S", errors="coerce")
            df = parse_xbrl(r.text, when, url)
            if len(df):
                frames.append(df)
            done.append(url)
            failed = 0
        except Exception as e:                                   # noqa: BLE001 - one filing never stops the rest
            log.warning("insider: %s: %s", url, e)
            failed += 1
            if renew is not None and failed >= RENEW_AFTER:
                try:
                    session, failed = renew(), 0
                    log.info("insider: a fresh session after %d failures in a row", RENEW_AFTER)
                except Exception as e2:                          # noqa: BLE001
                    log.warning("insider: no fresh session: %s", e2)
        time.sleep(pause)
    if done:
        d = out_dir or DIR
        d.mkdir(parents=True, exist_ok=True)
        with (d / READ).open("a", encoding="utf-8") as fh:
            fh.write("".join(u + "\n" for u in done))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=COLUMNS)


# ---------------------------------------------------------------- storage
def write(df: pd.DataFrame, out_dir: Path | None = None) -> list[Path]:
    """Merge into the yearly files (by broadcast year). A disclosure filed again, in any year, keeps
    the latest values and the earliest broadcast time, in that time's year (AMBUJACEM's sale of
    21 Mar 2016 was filed in Mar 2016 and again in Mar 2020). Only files whose rows changed are
    written, so a day's few disclosures touch one file."""
    if df.empty:
        return []
    d = out_dir or DIR
    old = {int(f.stem): pd.read_parquet(f) for f in sorted(d.glob("*.parquet"))} if d.exists() else {}
    allr = pd.concat([*old.values(), df[COLUMNS]], ignore_index=True).sort_values("broadcast_dt", kind="stable")
    first = allr.groupby(KEY, dropna=False)["broadcast_dt"].transform("min")
    allr = allr.assign(broadcast_dt=first).drop_duplicates(KEY, keep="last")[COLUMNS]
    written = []
    d.mkdir(parents=True, exist_ok=True)
    for y, chunk in allr.groupby(pd.to_datetime(allr["broadcast_dt"]).dt.year):
        chunk = chunk.sort_values("broadcast_dt", kind="stable").reset_index(drop=True)
        prev = old.get(int(y))
        if prev is not None and len(prev) == len(chunk) and prev.sort_values("broadcast_dt", kind="stable").reset_index(drop=True).equals(chunk):
            continue
        chunk.to_parquet(d / f"{int(y)}.parquet", index=False)
        written.append(d / f"{int(y)}.parquet")
    for y in set(old) - set(pd.to_datetime(allr["broadcast_dt"]).dt.year.astype(int)):
        (d / f"{y}.parquet").unlink()                            # every row of the year moved to an earlier one
    return written


def stored_urls(out_dir: Path | None = None) -> set:
    """The XML addresses already read: those stored, and those listed in READ."""
    d = out_dir or DIR
    files = sorted(d.glob("*.parquet")) if d.exists() else []
    urls = set(pd.concat([pd.read_parquet(f, columns=["url"]) for f in files])["url"].dropna()) if files else set()
    if (d / READ).exists():
        urls |= set((d / READ).read_text(encoding="utf-8").split())
    return urls


def rebuild(con) -> int:
    for stmt in SCHEMA.split(";"):
        if stmt.strip():
            con.execute(stmt)
    con.execute("DELETE FROM insider")
    if DIR.exists() and any(DIR.glob("*.parquet")):
        con.execute(f"INSERT INTO insider SELECT {', '.join(COLUMNS)} "
                    f"FROM read_parquet('{(DIR / '*.parquet').as_posix()}', union_by_name=true)")
    return con.execute("SELECT count(*) FROM insider").fetchone()[0]


SYMBOL_RX = re.compile(r"^[A-Z0-9&\-]{1,20}$")
