"""Read-only, point-in-time queries over zen's archive, for AI assistants (zen/mcp_server.py).

Every function opens the DuckDB archive READ-ONLY, answers, and closes it. Any function that
takes `as_of` answers with only what was public by the end of that day: filings and results
broadcast on or before it, prices up to it, shareholding filings broadcast by then. Without
`as_of` it answers as of the last session in the archive. Nothing here writes anything, and
nothing here is used by a strategy.

Figures are in rupees unless a name says crore (_cr) or percent (_pct).
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import pandas as pd

from zen.data import filing_types
from zen.data.store import DB_PATH

MAX_ROWS = 1500
DB = DB_PATH                       # tests point this at a small archive


def _con(path: Path | None = None):
    path = path or DB
    if not Path(path).exists():
        raise FileNotFoundError(f"zen's archive is not built at {path}; run `python -m jobs.rebuild_db` in zen")
    return duckdb.connect(str(path), read_only=True)


def _clean(v):
    if v is None:
        return None
    if isinstance(v, float):
        return None if not math.isfinite(v) else round(float(v), 4)
    if hasattr(v, "item"):
        return _clean(v.item())
    if isinstance(v, (pd.Timestamp, datetime)):
        return v.isoformat(sep=" ") if isinstance(v, datetime) and (v.hour or v.minute) else v.strftime("%Y-%m-%d")
    if isinstance(v, date):
        return v.isoformat()
    return v


def _records(df: pd.DataFrame) -> list[dict]:
    return [{k: _clean(v) for k, v in r.items()} for r in df.to_dict("records")]


def _day(as_of) -> date | None:
    if as_of in (None, ""):
        return None
    return as_of if isinstance(as_of, date) else date.fromisoformat(str(as_of)[:10])


def _last_session(con, as_of: date | None) -> date:
    q = "SELECT max(date) FROM prices" + (" WHERE date <= ?" if as_of else "")
    d = con.execute(q, [as_of] if as_of else []).fetchone()[0]
    if d is None:
        raise ValueError("no prices on or before that date")
    return d


# --------------------------------------------------------------------------- lookups

def search_companies(query: str, limit: int = 10) -> list[dict]:
    """Companies whose symbol starts with, or whose name contains, `query` (case-insensitive),
    with NSE sector and industry where known."""
    q = (query or "").strip().lower()
    if not q:
        return []
    con = _con()
    try:
        df = con.execute(
            """
            WITH names AS (
                SELECT symbol, arg_max(company, broadcast_dt) AS company FROM financials
                WHERE company IS NOT NULL GROUP BY symbol
            ), recent AS (
                SELECT symbol, max(date) AS last_session FROM prices GROUP BY symbol
            )
            SELECT r.symbol, n.company, r.last_session FROM recent r LEFT JOIN names n USING (symbol)
            WHERE lower(r.symbol) LIKE ? OR lower(coalesce(n.company, '')) LIKE ?
            ORDER BY (lower(r.symbol) = ?) DESC, r.last_session DESC, r.symbol
            LIMIT ?
            """, [q + "%", "%" + q + "%", q, int(limit)]).df()
    finally:
        con.close()
    try:
        ind = pd.read_parquet("data/reference/industry_nse.parquet")[["symbol", "sector", "industry"]]
        df = df.merge(ind.drop_duplicates("symbol"), on="symbol", how="left")
    except Exception:                                                # noqa: BLE001
        pass
    return _records(df)


def price_history(symbol: str, start: str | None = None, end: str | None = None) -> dict:
    """Daily OHLC, volume and turnover from NSE's bhavcopy as traded (NOT adjusted for splits
    or bonuses; corporate_actions lists them). At most 1,500 sessions, the latest kept."""
    con = _con()
    try:
        end_d = _last_session(con, _day(end))
        start_d = _day(start) or (end_d - timedelta(days=365))
        df = con.execute(
            "SELECT date, open, high, low, close, volume, turnover FROM prices "
            "WHERE symbol = ? AND date BETWEEN ? AND ? AND series IN ('EQ', 'BE') ORDER BY date",
            [symbol.upper(), start_d, end_d]).df()
    finally:
        con.close()
    df = df.tail(MAX_ROWS)
    return {"symbol": symbol.upper(), "adjusted": False, "rows": _records(df)}


def corporate_actions(symbol: str, as_of: str | None = None) -> list[dict]:
    """Splits, bonuses and other actions with ex-dates on or before `as_of` (or any, without it)."""
    con = _con()
    try:
        d = _day(as_of)
        df = con.execute(
            "SELECT ex_date, action, subject, factor FROM corpactions WHERE symbol = ?"
            + (" AND ex_date <= ?" if d else "") + " ORDER BY ex_date DESC",
            [symbol.upper()] + ([d] if d else [])).df()
    finally:
        con.close()
    return _records(df)


def quarterly_results(symbol: str, as_of: str | None = None, quarters: int = 8,
                      consolidated: bool | None = None) -> dict:
    """Quarterly results as filed with NSE and known by the end of `as_of`: for each quarter the
    latest revision broadcast by then. Rupee figures in crore. Consolidated where the company
    files it, unless `consolidated` says otherwise. Lenders filing in the banking format carry
    only total income here; their full figures are in bank_results()."""
    d = _day(as_of)
    con = _con()
    try:
        cutoff = datetime.combine(d + timedelta(days=1), datetime.min.time()) if d else datetime(9999, 1, 1)
        df = con.execute(
            """
            SELECT period_end, consolidated, broadcast_dt, revenue, total_income, ebitda, profit_reported,
                   profit_normalised, eps_basic, quarter_span_days
            FROM financials WHERE symbol = ? AND broadcast_dt < ? AND quarter_span_days < 100
            """, [symbol.upper(), cutoff]).df()
    finally:
        con.close()
    if df.empty:
        return {"symbol": symbol.upper(), "as_of": _clean(d), "basis": None, "quarters": []}
    df = df.sort_values("broadcast_dt").drop_duplicates(["consolidated", "period_end"], keep="last")
    if consolidated is None:
        consolidated = bool(df["consolidated"].any())
    df = df[df["consolidated"] == consolidated].sort_values("period_end", ascending=False).head(int(quarters))
    out = pd.DataFrame({
        "period_end": df["period_end"], "filed": df["broadcast_dt"],
        "revenue_cr": (df["revenue"].fillna(df["total_income"]) / 1e7).round(2),
        "ebitda_cr": (df["ebitda"] / 1e7).round(2), "net_profit_cr": (df["profit_reported"] / 1e7).round(2),
        "profit_ex_exceptional_cr": (df["profit_normalised"] / 1e7).round(2), "eps": df["eps_basic"]})
    return {"symbol": symbol.upper(), "as_of": _clean(d), "basis": "consolidated" if consolidated else "standalone",
            "quarters": _records(out)}


def bank_results(symbol: str, as_of: str | None = None, quarters: int = 8, consolidated: bool | None = None) -> dict:
    """A lender's quarterly results from its banking-format filings, known by the end of `as_of`:
    total income, net interest income, operating profit before provisions, provisions, net
    profit (the shareholders' share where consolidated), EPS, and the gross and net NPA ratios
    (standalone, as the bank discloses them). Rupee figures in crore; NPA ratios in percent."""
    d = _day(as_of)
    con = _con()
    try:
        tabs = set(con.execute("SELECT table_name FROM information_schema.tables").df()["table_name"])
        if "bank_results" not in tabs:
            return {"symbol": symbol.upper(), "as_of": _clean(d), "basis": None, "quarters": [],
                    "note": "bank_results is not built in this archive (python -m jobs.update_bank_results)"}
        cutoff = datetime.combine(d + timedelta(days=1), datetime.min.time()) if d else datetime(9999, 1, 1)
        df = con.execute(
            """
            SELECT period_end, consolidated, broadcast_dt, total_income, interest_earned, interest_expended,
                   operating_profit, provisions, profit_reported, profit_owners, eps_basic, gross_npa_pct, net_npa_pct
            FROM bank_results WHERE symbol = ? AND broadcast_dt < ? AND has_figures
            """, [symbol.upper(), cutoff]).df()
    finally:
        con.close()
    if df.empty:
        return {"symbol": symbol.upper(), "as_of": _clean(d), "basis": None, "quarters": []}
    df = df.sort_values("broadcast_dt").drop_duplicates(["consolidated", "period_end"], keep="last")
    npa = df[~df["consolidated"]].set_index("period_end")[["gross_npa_pct", "net_npa_pct"]]
    if consolidated is None:
        consolidated = bool(df["consolidated"].any())
    df = df[df["consolidated"] == consolidated].sort_values("period_end", ascending=False).head(int(quarters))
    profit = df["profit_owners"].where(df["consolidated"] & df["profit_owners"].notna(), df["profit_reported"])
    g = npa.reindex(df["period_end"])
    out = pd.DataFrame({
        "period_end": df["period_end"].to_numpy(), "filed": df["broadcast_dt"].to_numpy(),
        "total_income_cr": (df["total_income"] / 1e7).round(2).to_numpy(),
        "net_interest_income_cr": ((df["interest_earned"] - df["interest_expended"]) / 1e7).round(2).to_numpy(),
        "operating_profit_cr": (df["operating_profit"] / 1e7).round(2).to_numpy(),
        "provisions_cr": (df["provisions"] / 1e7).round(2).to_numpy(),
        "net_profit_cr": (profit / 1e7).round(2).to_numpy(), "eps": df["eps_basic"].to_numpy(),
        "gross_npa_pct": (g["gross_npa_pct"].where(g["gross_npa_pct"] > 0) * 100).round(2).to_numpy(),
        "net_npa_pct": (g["net_npa_pct"].where(g["gross_npa_pct"] > 0) * 100).round(2).to_numpy()})
    return {"symbol": symbol.upper(), "as_of": _clean(d), "basis": "consolidated" if consolidated else "standalone",
            "quarters": _records(out)}


def filings(symbol: str, since: str | None = None, until: str | None = None, material_only: bool = True,
            limit: int = 30) -> list[dict]:
    """The company's filings with NSE broadcast between `since` and the end of `until`, newest
    first, each with zen's category, a cleaned summary and the link to the document."""
    from zen.data import announcements
    u = _day(until)
    s = _day(since) or ((u or date.today()) - timedelta(days=365))
    params: list = [symbol.upper(), datetime.combine(s, datetime.min.time())]
    sql = "SELECT an_dt, category, subject, url FROM announcements WHERE symbol = ? AND an_dt >= ?"
    if u:
        sql += " AND an_dt < ?"
        params.append(datetime.combine(u + timedelta(days=1), datetime.min.time()))
    if material_only:
        mats = sorted(announcements.MATERIAL)
        sql += f" AND (category IN ({', '.join('?' * len(mats))}) OR lower(subject) LIKE '%transcript%')"
        params += mats
    sql += " ORDER BY an_dt DESC LIMIT ?"
    params.append(int(limit))
    con = _con()
    try:
        df = con.execute(sql, params).df()
    finally:
        con.close()
    df["summary"] = [filing_types.gist(x, 300) for x in df["subject"]]
    return _records(df[["an_dt", "category", "summary", "url"]])


def shareholding(symbol: str, as_of: str | None = None, periods: int = 8) -> list[dict]:
    """Promoter, public and employee-trust holding by quarter, each quarter as last filed by the
    end of `as_of`."""
    from zen.data import shareholding as sh
    con = _con()
    try:
        d = _day(as_of) or _last_session(con, None)
        df = sh.asof(con, symbol.upper(), d, int(periods))
    finally:
        con.close()
    return _records(df)


def fundamentals(symbol: str, as_of: str | None = None) -> dict:
    """zen's own point-in-time fundamentals at the start of the day after `as_of`: the basis it
    chose, trailing-four-quarter revenue, EBITDA and profit with exceptional items added back,
    the latest and year-ago quarter, and the split-adjusted share count. Exactly what zen's
    strategies see. Rupees in crore."""
    from zen.universe import pit
    con = _con()
    try:
        last = _last_session(con, _day(as_of))
        D = pd.Timestamp(last) + pd.Timedelta(days=1)
        static = pit.StaticLabels.load(con)
        snap = pit.build_snapshot(con, D, static)
        f = pit.fundamentals(snap)
        px = con.execute("SELECT close FROM prices WHERE symbol = ? AND date = ?", [symbol.upper(), last]).fetchone()
    finally:
        con.close()
    sym = symbol.upper()
    if sym not in f.index:
        return {"symbol": sym, "as_of": _clean(last), "available": False}
    r = f.loc[sym]
    cr = lambda v: _clean(v / 1e7) if v is not None and pd.notna(v) else None    # noqa: E731
    return {"symbol": sym, "as_of": _clean(last), "available": True,
            "basis": "consolidated" if r["consolidated"] else "standalone",
            "latest_quarter": _clean(r["latest_period_end"]), "latest_filed": _clean(r["latest_broadcast"]),
            "ttm_revenue_cr": cr(r["ttm_revenue"]), "ttm_ebitda_cr": cr(r["ttm_ebitda"]),
            "ttm_profit_ex_exceptional_cr": cr(r["ttm_profit"]),
            "revenue_latest_cr": cr(r["rev_latest"]), "revenue_year_ago_cr": cr(r["rev_yago"]),
            "profit_latest_cr": cr(r["profit_latest"]), "profit_year_ago_cr": cr(r["profit_yago"]),
            "shares": _clean(r["shares"]), "close": _clean(px[0]) if px else None}


def market_on(day: str | None = None) -> dict:
    """The session's breadth from zen's own archive (advancers, decliners, median move, turnover)
    and the closing levels of the main NSE indices."""
    from zen.monitor import market
    con = _con()
    try:
        d = _last_session(con, _day(day))
        b = market.breadth(con, d)
        idx = con.execute(
            "SELECT index_name, close, pct_change, pe FROM indices WHERE date = ? ORDER BY index_name", [d]).df()
    finally:
        con.close()
    return {"session": _clean(d), "breadth": {k: _clean(v) for k, v in (b or {}).items()},
            "indices": _records(idx)}
