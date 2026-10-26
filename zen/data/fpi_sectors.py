"""Foreign portfolio investors' holdings and net investment by sector, fortnight by fortnight (NSDL,
the depository that reports FPI investment for SEBI).

SOURCE, AS MEASURED ON 25 OCT 2026
  https://www.fpi.nsdl.co.in/web/Reports/FPI_Fortnightly_Selection.aspx lists every report (354, from
  2015); each is a static page, .../StaticReports/Fortnightly_Sector_wise_FII_Investment_Data/
  FIIInvestSector_Oct152026.html, of one table: for each sector, the assets under custody at the
  end of the fortnight before, the net investment of that fortnight and of this one, and the assets
  at this fortnight's end, each in Rs crore and USD million, split into equity, debt (general
  limit, VRR, FAR), hybrid, mutual funds, AIFs and the total. The server answers an ordinary
  browser's request and gives no publication date (no Last-Modified).

KEPT: the reports from 30 Apr 2022, when NSDL moved to the exchanges' common industry classification
(22 sectors, with Sovereign and Others); the ones before use NSDL's older sectors and do not join
up. Equity and total, Rs crore and USD million, net investment of the fortnight and assets at its
end. observed_dt is when zen first read the report: NSDL does not say when it published one, so
nothing earlier can be claimed (a backtest sees a report only from the day zen did)."""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

BASE = "https://www.fpi.nsdl.co.in/web/"
SELECT_URL = BASE + "Reports/FPI_Fortnightly_Selection.aspx"
FROM = date(2022, 4, 30)
DIR = Path("data/fpi_sectors")
FILE = "fpi_sectors.parquet"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/130.0 Safari/537.36",
           "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "Accept-Language": "en-US,en;q=0.9"}
COLUMNS = ["period_end", "period_start", "sector", "net_eq_cr", "net_total_cr", "net_eq_usd", "net_total_usd",
           "auc_eq_cr", "auc_total_cr", "auc_eq_usd", "auc_total_usd", "observed_dt", "url"]
SCHEMA = """CREATE TABLE IF NOT EXISTS fpi_sectors (period_end DATE, period_start DATE, sector VARCHAR,
    net_eq_cr DOUBLE, net_total_cr DOUBLE, net_eq_usd DOUBLE, net_total_usd DOUBLE, auc_eq_cr DOUBLE,
    auc_total_cr DOUBLE, auc_eq_usd DOUBLE, auc_total_usd DOUBLE, observed_dt TIMESTAMP, url VARCHAR);"""
_FILE_DATE = re.compile(r"FIIInvestSector_([A-Za-z]+)(\d{1,2})(\d{4})\.html?$", re.I)
_DATE = re.compile(r"([A-Za-z]+)\.?\s+(\d{1,2})\s*,?\s*(\d{4})")
_RANGE = re.compile(r"([A-Za-z]+)\.?\s+(\d{1,2})\s*-\s*(\d{1,2})\s*,?\s*(\d{4})")


def _month(name: str) -> int:
    return datetime.strptime(name[:3].title(), "%b").month


def report_date(url: str) -> date | None:
    m = _FILE_DATE.search(url or "")
    if not m:
        return None
    try:
        return date(int(m.group(3)), _month(m.group(1)), int(m.group(2)))
    except ValueError:
        return None


def list_reports(html: str) -> list[tuple[date, str]]:
    """(fortnight end, absolute address) of every report the selection page lists, newest first."""
    from lxml import html as lh
    out = []
    for o in lh.fromstring(html).xpath("//select[@id='ddlfortnighly']/option"):
        v = (o.get("value") or "").strip()
        d = report_date(v)
        if d:
            out.append((d, BASE + v.lstrip("~/")))
    return sorted(set(out), reverse=True)


def _num(s: str):
    s = (s or "").strip().replace(",", "")
    if s in ("", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_report(html: str, observed: datetime, url: str | None = None) -> pd.DataFrame:
    """One report's table: a row per sector (and the grand total, sector "Total")."""
    from lxml import html as lh
    table = lh.fromstring(html).xpath("//table")[0]
    rows = table.xpath("./tr|./tbody/tr|./thead/tr")
    head = []                                     # the header rows, each cell spread over the columns it spans
    for tr in rows:
        cells = tr.xpath("./td|./th")
        if cells and " ".join(cells[0].text_content().split()).isdigit():
            break                                 # the first sector (Sr. No. 1)
        line = []
        for td in cells:
            line += [" ".join(td.text_content().split())] * int(td.get("colspan") or 1)
        head.append(line)
    block, money, name = head[0], head[1], head[-1]
    width = len(name)
    off = [width - len(x) for x in (block, money)]   # Sr. No. and Sectors span the header rows
    cols = [(block[i - off[0]] if i >= off[0] else "", money[i - off[1]] if i >= off[1] else "", name[i])
            for i in range(width)]

    blocks = [b for b in dict.fromkeys(c[0] for c in cols) if b]
    auc = [b for b in blocks if b.lower().startswith("auc")]
    net = [b for b in blocks if b.lower().startswith("net investment")]
    if not auc or not net:
        raise ValueError(f"unexpected header: {blocks}")
    auc_b, net_b = auc[-1], net[-1]
    m = _DATE.search(auc_b)
    end = date(int(m.group(3)), _month(m.group(1)), int(m.group(2)))
    r = _RANGE.search(net_b)
    start = date(int(r.group(4)), _month(r.group(1)), int(r.group(2))) if r else None

    def idx(b, cur, nm):
        """The first such column of the block and currency: FPIs' own equity comes before mutual funds'."""
        for i, c in enumerate(cols):
            if c[0] == b and c[1].upper().startswith(cur) and c[2].lower() == nm:
                return i
        raise ValueError(f"no column {b} / {cur} / {nm}")
    pick = {"net_eq_cr": idx(net_b, "IN INR", "equity"), "net_total_cr": idx(net_b, "IN INR", "total"),
            "net_eq_usd": idx(net_b, "IN USD", "equity"), "net_total_usd": idx(net_b, "IN USD", "total"),
            "auc_eq_cr": idx(auc_b, "IN INR", "equity"), "auc_total_cr": idx(auc_b, "IN INR", "total"),
            "auc_eq_usd": idx(auc_b, "IN USD", "equity"), "auc_total_usd": idx(auc_b, "IN USD", "total")}
    out = []
    for tr in rows[len(head):]:
        cells = [" ".join(td.text_content().split()) for td in tr.xpath("./td|./th")]
        if len(cells) < width:
            continue
        sector = cells[1]
        if not sector or sector.lower().startswith("note"):
            continue
        rec = {"period_end": end, "period_start": start, "sector": "Total" if sector.lower() == "grand total" else sector,
               "observed_dt": observed, "url": url}
        rec.update({k: _num(cells[i]) for k, i in pick.items()})
        out.append(rec)
    return pd.DataFrame(out, columns=COLUMNS)


def session():
    import requests
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def stored(out_dir: Path | None = None) -> pd.DataFrame:
    p = (out_dir or DIR) / FILE
    return pd.read_parquet(p) if p.exists() else pd.DataFrame(columns=COLUMNS)


def write(df: pd.DataFrame, out_dir: Path | None = None) -> Path | None:
    """Add reports to the one file; a fortnight already stored keeps its first reading."""
    if df.empty:
        return None
    d = out_dir or DIR
    d.mkdir(parents=True, exist_ok=True)
    old = stored(d)
    allr = pd.concat([old, df], ignore_index=True) if len(old) else df.copy()
    allr["period_end"] = pd.to_datetime(allr["period_end"]).dt.date
    allr["period_start"] = pd.to_datetime(allr["period_start"]).dt.date
    allr = allr.drop_duplicates(["period_end", "sector"], keep="first").sort_values(["period_end", "sector"])
    allr[COLUMNS].to_parquet(d / FILE, index=False)
    return d / FILE


def update(s=None, since: date = FROM, out_dir: Path | None = None, pause: float = 1.0, now: datetime | None = None) -> int:
    """Read every listed report from `since` not yet stored. Returns the rows written."""
    import time
    s = s or session()
    r = s.get(SELECT_URL, timeout=60)
    r.raise_for_status()
    have = set(pd.to_datetime(stored(out_dir)["period_end"]).dt.date)
    todo = [(d, u) for d, u in list_reports(r.text) if d >= since and d not in have]
    frames = []
    for d, u in todo:
        try:
            p = s.get(u, timeout=60)
            p.raise_for_status()
            df = parse_report(p.text, (now or datetime.now()).replace(microsecond=0), u)
            if len(df) and df["period_end"].iloc[0] != d:
                log.warning("fpi_sectors: %s is for %s", u, df["period_end"].iloc[0])
            frames.append(df)
        except Exception as e:                                   # noqa: BLE001 - one report never stops the rest
            log.warning("fpi_sectors: %s: %s", u, e)
        time.sleep(pause)
    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=COLUMNS)
    write(df, out_dir)
    log.info("fpi_sectors: %d reports listed from %s, %d read, %d rows", len(todo), since, len(frames), len(df))
    return len(df)


def rebuild(con) -> int:
    for stmt in SCHEMA.split(";"):
        if stmt.strip():
            con.execute(stmt)
    con.execute("DELETE FROM fpi_sectors")
    p = DIR / FILE
    if p.exists():
        con.execute(f"INSERT INTO fpi_sectors SELECT {', '.join(COLUMNS)} FROM read_parquet('{p.as_posix()}')")
    return con.execute("SELECT count(*) FROM fpi_sectors").fetchone()[0]
