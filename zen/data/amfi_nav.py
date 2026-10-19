"""Mutual fund NAVs from AMFI, for comparing zen's portfolios with the funds an Indian investor
could have bought instead. Research data for the dashboard; no strategy reads it.

SOURCE, AS MEASURED ON 19 OCT 2026

  AMFI's public NAV history report, https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx
  (frmdt, todt as 16-Oct-2026; mf empty for every fund house). One session for every scheme is about
  1.2 MB and 8,700 rows, answered in about 3 seconds. Rows are "Scheme Code;NAV Name;Plan;Option;
  ISIN Div Payout/ISIN Growth;ISIN Div Reinvestment;Net Asset Value;Date" under two context lines a
  row does not repeat: a section ("Open Ended Schemes ( Equity Scheme - Flexi Cap Fund )") and the
  fund house. The parsing follows jugaad-py/jugaad-data's amfi module (public domain, "do whatever
  you want with it").

  The report files every scheme under its CURRENT name and category, for past dates too: on 29 Jun
  2018 it lists "Parag Parikh Flexi Cap Fund" under Flexi Cap, a category SEBI created in November
  2020 (the fund was then Parag Parikh Long Term Equity Fund, a multi cap). So a category here is
  today's, applied backwards. A fund that has since closed or merged keeps the last name and
  category it had, and still appears on the dates it existed: comparisons by date are free of
  survivorship.

KEPT

  Open-ended equity schemes (every "Equity Scheme(s) - ..." section) and index funds, Growth option
  only (dividend and IDCW options pay out and their NAV drops; Growth reinvests), both Direct and
  Regular plans. ETFs are left out: their investors trade at the exchange price, not the NAV.
  Older schemes leave Plan and Option empty; then the name decides ("- Direct", "GROWTH").
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

URL = "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx"
NAV_DIR = Path("data/mf_nav")
COLUMNS = ["date", "scheme_code", "scheme_name", "amc", "category", "plan", "nav"]
SCHEMA = """CREATE TABLE IF NOT EXISTS mf_nav (date DATE, scheme_code VARCHAR, scheme_name VARCHAR,
    amc VARCHAR, category VARCHAR, plan VARCHAR, nav DOUBLE);"""

_SECTION = re.compile(r"^(Open Ended|Close Ended|Interval Fund)\s+Schemes?\s*\(\s*(.*?)\s*\)\s*$")
_EQUITY = re.compile(r"^Equity Schemes?\s*-\s*(.+)$", re.I)
_NOT_GROWTH = re.compile(r"idcw|dividend|bonus|payout|reinvest", re.I)


def category_of(section: str) -> str | None:
    """The kept category of a section, or None: 'Equity Schemes - ELSS- Tax Saver Fund' -> 'ELSS'."""
    m = _EQUITY.match(section.strip())
    if m:
        c = re.sub(r"\s+", " ", m.group(1)).strip().rstrip("*").strip()
        if c.upper().startswith("ELSS"):
            return "ELSS"
        if re.match(r"(sectoral|thematic)", c, re.I):       # AMFI files these three ways
            return "Sectoral/Thematic"
        return c
    if re.sub(r"\s+", " ", section).strip().lower() == "other scheme - index funds":
        return "Index Funds"
    return None


def plan_of(plan: str, name: str) -> str:
    return "Direct" if "direct" in (plan or "").lower() or re.search(r"\bdirect\b", name, re.I) else "Regular"


def is_growth(option: str, name: str) -> bool:
    text = option or name
    return bool(re.search(r"growth", text, re.I)) and not _NOT_GROWTH.search(text)


def parse(text: str) -> pd.DataFrame:
    """The kept rows of one report, as COLUMNS."""
    rows, category, amc = [], None, ""
    for line in text.splitlines():
        line = line.strip().lstrip("﻿").strip()
        if not line or line.startswith("Scheme Code"):
            continue
        sec = _SECTION.match(line)
        if sec:
            category = category_of(sec.group(2)) if sec.group(1) == "Open Ended" else None
            amc = ""
            continue
        if ";" not in line:
            amc = line
            continue
        p = [x.strip() for x in line.split(";")]
        if category is None or len(p) < 8 or not is_growth(p[3], p[1]):
            continue
        try:
            nav = float(p[6].replace(",", ""))
            day = datetime.strptime(p[7], "%d-%b-%Y").date()
        except ValueError:
            continue                                     # "N.A." or a malformed row: no figure that day
        if nav <= 0:
            continue
        rows.append((day, p[0], p[1], amc, category, plan_of(p[2], p[1]), nav))
    return pd.DataFrame(rows, columns=COLUMNS)


def fetch(session, start: date, end: date, timeout: float = 120.0) -> pd.DataFrame:
    r = session.get(URL, params={"mf": "", "frmdt": start.strftime("%d-%b-%Y"), "todt": end.strftime("%d-%b-%Y")},
                    timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    return parse(r.content.decode("utf-8", errors="replace"))


def store(df: pd.DataFrame, root: Path = NAV_DIR) -> int:
    """Merge rows into data/mf_nav/<year>.parquet (one row per date and scheme, the newest read
    winning). Returns the rows written."""
    if df.empty:
        return 0
    root.mkdir(parents=True, exist_ok=True)
    n = 0
    for year, g in df.groupby(pd.to_datetime(df["date"]).dt.year):
        f = root / f"{year}.parquet"
        old = pd.read_parquet(f) if f.exists() else pd.DataFrame(columns=COLUMNS)
        new = pd.concat([old, g[COLUMNS]], ignore_index=True)
        new["date"] = pd.to_datetime(new["date"]).dt.date
        new = new.drop_duplicates(["date", "scheme_code"], keep="last").sort_values(["date", "scheme_code"])
        new.to_parquet(f, index=False, compression="zstd")
        n += len(g)
    return n


def stored_dates(root: Path = NAV_DIR) -> set:
    if not root.exists() or not any(root.glob("*.parquet")):
        return set()
    return set(pd.to_datetime(pd.concat(pd.read_parquet(f, columns=["date"]) for f in root.glob("*.parquet"))["date"]).dt.date)


def rebuild(con, root: Path = NAV_DIR) -> int:
    con.execute(SCHEMA)
    con.execute("DELETE FROM mf_nav")
    if root.exists() and any(root.glob("*.parquet")):
        con.execute(f"INSERT INTO mf_nav SELECT {', '.join(COLUMNS)} FROM read_parquet('{(root / '*.parquet').as_posix()}')")
    return con.execute("SELECT count(*) FROM mf_nav").fetchone()[0]
