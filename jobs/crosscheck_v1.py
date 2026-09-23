"""Independent cross-check of strategy v1, written from research/strategy/v1-spec.md alone.

This is the CHECKER, not the production engine. It deliberately shares no code
with zen/universe, zen/signals or zen/portfolio (it has not read them) and it is
written in a different style: pandas panels and a numpy share-count simulator
rather than SQL. The two implementations are meant to be diffed row by row.

Only one import from zen is allowed: zen.data.financials.statement_files.

    .venv/Scripts/python.exe -m jobs.crosscheck_v1            # in-sample only
    .venv/Scripts/python.exe -m jobs.crosscheck_v1 --no-leak-test
    .venv/Scripts/python.exe -m jobs.crosscheck_v1 --run-final-test --end 2026-09-18 --out <dir>

The full-period mode was added on 2026-09-23 by an independent auditor who
extended this checker to 2026 without reading the production engine. It
reproduced the production final test to fifteen decimal places. The lender
name rule reads each company's latest name only, which is Clarification 29
read literally; the earlier version matched every name a company had ever
filed under and disagreed with production on one stock (TSFINV).

Outputs (data/backtest/v1_check/):
    universe.csv   D, symbol                           every in-sample decision date
    ranks.csv      D, symbol, 8 measures, 5 groups, composite, rank, sector
    holdings.csv   D, symbol, status                   BASE config target book
    nav.csv        date, mark, strategy, universe_ew   daily, from D0 open to END open
    metrics.json   metrics, diagnostics, sanity checks, ambiguity readings

HOLDOUT LOCK
Every date at which a price or return is read by the simulator passes through
_guard(), which raises for anything after the in-sample end (the Feb 2023
decision date, marked at its OPEN) unless --run-final-test is given. Price,
corporate-action, dividend and index data are also truncated at load time to
that date, so even a bug in the simulator cannot see the holdout. The flag
exists because the spec requires one. It is used only to reproduce the final
test after it has been run by the production engine, never to choose anything.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from zen.data.financials import statement_files  # noqa: E402  (the only zen import)

OUT = ROOT / "data" / "backtest" / "v1_check"
DUCK_TMP = (r"C:\Users\mnsha\AppData\Local\Temp\claude\C--Users-mnsha-OneDrive-Desktop-Gostack"
            r"\196360f9-eded-41ee-bd9a-762d776d6462\scratchpad\duck")

# ---------------------------------------------------------------- spec constants
ANCHORS = [(2, 15), (6, 1), (8, 15), (11, 15)]
FIRST_YEAR, LAST_YEAR = 2019, 2022           # 16 in-sample decision dates
END_ANCHOR = (2023, 2, 15)                   # returns measured up to this D's OPEN
PRICE_START = "2017-06-01"                   # >= 365 days + 253 sessions before Feb 2019

MIN_TURNOVER = 20e5                          # Rs 20 lakh
TURNOVER_WINDOW = 60
MIN_SESSIONS_365 = 200
TRADED_LAST = 5
MAX_STALE_DAYS = 200
MOM_NEAR, MOM_FAR, VOL_WINDOW = 21, 252, 252

N = 10
BUFFER = 2 * N
SECTOR_CAP = 3
COST = 0.0020
EXEC_WAIT = 5                                # sessions an order may wait after D
FORCED_EXIT_SESSIONS = 20

MEASURES = ["margin", "stability", "rev_growth", "profit_growth",
            "earnings_yield", "sales_yield", "mom_12_1", "low_vol"]
GROUPS = {"quality": ["margin", "stability"],
          "growth": ["rev_growth", "profit_growth"],
          "value": ["earnings_yield", "sales_yield"],
          "momentum": ["mom_12_1"],
          "risk": ["low_vol"]}

LENDER_LABEL = re.compile(r"bank|financ|insurance|nbfc", re.I)
LENDER_NAME = re.compile(
    r"\bbank\b|insurance|assurance|\bfinance\b|financial\b|fincorp|finserv|finvest|"
    r"\bcredit\b|leasing|housing fin", re.I)

# Rule 4 point-in-time lender flag (spec Clarification 28): the XBRL taxonomy
# named in each filing's document URL. NSE's NBFC taxonomy starts with the
# Dec-2019 quarter (Jan 2020 broadcasts); earlier filings carry no vote.
TAXONOMY_RX = r"/xbrl/(?:INTEGRATED_FILING_)?([A-Z_]+?)_\d"
LENDER_TAX = ("NBFC_INDAS", "BANKING", "GI", "LI")
NBFC_TAX_START = pd.Timestamp("2020-01-01")

# Data-quality screens (spec Clarification 30)
LOG_BAND = math.log10(30.0)          # 30x on a log scale
SCALE_NEIGH = 8
SH_REF_Q = 4
SH_ALT_TOL = 1.5

FULL_END: pd.Timestamp | None = None
LATEST_NAMES = {}
UNLOCK = False
END_DATE: pd.Timestamp | None = None


class HoldoutViolation(RuntimeError):
    pass


def _guard(d) -> None:
    """Refuse any price/return read after the in-sample end unless unlocked."""
    if END_DATE is None:
        raise HoldoutViolation("in-sample end not set")
    if pd.Timestamp(d) > END_DATE and not UNLOCK:
        raise HoldoutViolation(f"{pd.Timestamp(d).date()} is after the in-sample end "
                               f"{END_DATE.date()}; pass --run-final-test to run the holdout")


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ================================================================ loading
def duck() -> duckdb.DuckDBPyConnection:
    Path(DUCK_TMP).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute(f"SET temp_directory='{Path(DUCK_TMP).as_posix()}'")
    con.execute("SET memory_limit='8GB'")
    return con


def all_sessions(con, start: str = PRICE_START) -> pd.DatetimeIndex:
    s = con.execute(f"SELECT DISTINCT CAST(date AS DATE) d FROM read_parquet("
                    f"'{(ROOT / 'data/daily').as_posix()}/**/*.parquet', union_by_name=true) "
                    f"WHERE date >= '{start}' ORDER BY 1").df()["d"]
    return pd.DatetimeIndex(pd.to_datetime(s))


def decision_dates(sessions: pd.DatetimeIndex) -> tuple[list[pd.Timestamp], pd.Timestamp]:
    def first_on_or_after(y, m, d):
        t = pd.Timestamp(y, m, d)
        i = sessions.searchsorted(t)
        return sessions[i]
    end = first_on_or_after(*END_ANCHOR) if FULL_END is None else FULL_END
    ds = [first_on_or_after(y, m, d) for y in range(FIRST_YEAR, 2100) for m, d in ANCHORS
          if pd.Timestamp(y, m, d) <= sessions[-1]]
    ds = [x for x in ds if x < end]
    return ds, end


def load_prices(con, end: pd.Timestamp) -> pd.DataFrame:
    # NOTE: prev_close is never selected (it is unadjusted and wrong on ex-dates).
    p = con.execute(f"""
        SELECT CAST(date AS DATE) AS date, symbol, series, isin_code AS isin,
               open,
               -- the END session is marked at its OPEN only: its close and
               -- turnover are never loaded (holdout lock)
               CASE WHEN CAST(date AS DATE) < DATE '{end.date()}' THEN close END AS close,
               CASE WHEN CAST(date AS DATE) < DATE '{end.date()}' THEN turnover END AS turnover
        FROM read_parquet('{(ROOT / 'data/daily').as_posix()}/**/*.parquet', union_by_name=true)
        WHERE date >= '{PRICE_START}' AND date <= '{end.date()}'
          AND series IN ('EQ','BE')""").df()
    p["date"] = pd.to_datetime(p["date"])
    assert p["date"].max() <= end
    return p


def load_identity_keys(con) -> pd.DataFrame:
    """(date, symbol, isin) keys over the WHOLE archive -- no prices, no returns.

    Identity only: companies renamed after the in-sample end (ADANITRANS ->
    ADANIENSOL, AMARAJABAT -> ARE&M, ...) have their historical filings stored
    under the new symbol, so the rename chain must be known to join them.
    """
    k = con.execute(f"""
        SELECT symbol, isin_code AS isin, CAST(min(date) AS DATE) AS a, CAST(max(date) AS DATE) AS b
        FROM read_parquet('{(ROOT / 'data/daily').as_posix()}/**/*.parquet', union_by_name=true)
        WHERE series IN ('EQ','BE')
        GROUP BY 1, 2""").df()
    k["a"] = pd.to_datetime(k["a"]); k["b"] = pd.to_datetime(k["b"])
    # expand to the two end-point rows build_ids needs
    return pd.concat([k.rename(columns={"a": "date"})[["symbol", "isin", "date"]],
                      k.rename(columns={"b": "date"})[["symbol", "isin", "date"]]])


def build_ids(p: pd.DataFrame, sessions: pd.DatetimeIndex) -> pd.DataFrame:
    """Link renames into one stock id.

    A (symbol, isin) spell is a node. Two spells are the same stock when they
    share a symbol (an ISIN change on a split) or an ISIN (a symbol change on a
    rename) and one starts within 60 sessions of the other ending. A symbol
    reused years later by a different company is therefore NOT merged.

    A third pass (Clarification 33) joins two components of the same ISIN
    issuer when the company changed symbol AND ISIN on the same day, so it
    shares no key with itself (SUBEX -> SUBEXLTD, SUPPETRO -> SPLPETRO).
    Guards: ordinary-equity ISINs only (security-type digits '01', which
    excludes rights entitlements); no shared symbol or ISIN; component-level
    non-overlap within 60 sessions; and exactly one candidate pair per issuer.
    """
    GAP = 60
    sidx = pd.Series(np.arange(len(sessions)), index=sessions)
    sp = (p.groupby(["symbol", "isin"])["date"].agg(["min", "max"]).reset_index()
          .sort_values(["min", "symbol", "isin"]).reset_index(drop=True))
    sp["a"] = sidx.reindex(sp["min"]).values
    sp["b"] = sidx.reindex(sp["max"]).values
    parent = list(range(len(sp)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for key in ("symbol", "isin"):
        for _, g in sp.groupby(key):
            if len(g) < 2:
                continue
            g = g.sort_values("a")
            rows = list(g.itertuples())
            for i in range(len(rows)):
                for j in range(i + 1, len(rows)):
                    r1, r2 = rows[i], rows[j]
                    if r2.a - r1.b <= GAP:
                        a, b = find(r1.Index), find(r2.Index)
                        if a != b:
                            parent[b] = a

    # --- third pass: same ISIN issuer, both keys changed at once
    sp["_root"] = [find(i) for i in range(len(sp))]
    sp["_ok"] = sp["isin"].map(
        lambda s: len(str(s)) == 12 and str(s).startswith("INE") and str(s)[7:9] == "01")
    comp = sp.groupby("_root").agg(ca=("a", "min"), cb=("b", "max"))
    csym = sp.groupby("_root")["symbol"].apply(set)
    cisin = sp.groupby("_root")["isin"].apply(set)
    for _, g in sp[sp["_ok"]].groupby(sp["isin"].str[:7]):
        rs = sorted(set(g["_root"]))
        if len(rs) < 2:
            continue
        cand = [(x, y) for x in rs for y in rs
                if x != y and not (csym[x] & csym[y]) and not (cisin[x] & cisin[y])
                and 0 <= comp.at[y, "ca"] - comp.at[x, "cb"] <= GAP]
        if len(cand) != 1:            # ambiguous: link nothing
            continue
        x, y = find(cand[0][0]), find(cand[0][1])
        if x != y:
            parent[y] = x
    sp = sp.drop(columns=["_root", "_ok"])

    sp["root"] = [find(i) for i in range(len(sp))]
    # id label = the most recent symbol of the component
    last = sp.sort_values(["b", "a", "symbol"]).groupby("root")["symbol"].last()
    sp["cid"] = sp["root"].map(last)
    # a symbol could now label two components (reuse); disambiguate
    dup = sp.groupby("cid")["root"].nunique()
    for c in dup[dup > 1].index:
        order = sp[sp.cid == c].groupby("root")["a"].min().sort_values()
        for k, r in enumerate(order.index):
            if k:
                sp.loc[sp.root == r, "cid"] = f"{c}#{k}"
    return sp[["symbol", "isin", "min", "max", "cid"]]


def map_symbol_dates(df: pd.DataFrame, sym_col: str, date_col: str, spells: pd.DataFrame) -> pd.Series:
    """cid for an event keyed by (symbol, date): the spell of that symbol that had
    most recently started by the date, else the symbol's earliest spell."""
    sp = spells.groupby(["symbol", "cid"])["min"].min().reset_index().sort_values("min")
    left = df[[sym_col, date_col]].copy()
    left["_i"] = np.arange(len(left))
    left["_d"] = pd.to_datetime(left[date_col]).astype("datetime64[ns]")
    left = left.sort_values("_d")
    sp = sp.rename(columns={"symbol": sym_col})
    sp["min"] = sp["min"].astype("datetime64[ns]")
    m = pd.merge_asof(left, sp, left_on="_d", right_on="min", by=sym_col, direction="backward")
    first = sp.groupby(sym_col)["cid"].first()
    m["cid"] = m["cid"].fillna(m[sym_col].map(first))
    return m.sort_values("_i")["cid"].values


class Panels:
    """Wide (session x stock) arrays. Raw prices; adjustment factors separate."""

    def __init__(self, p: pd.DataFrame, spells: pd.DataFrame, sessions: pd.DatetimeIndex,
                 ca: pd.DataFrame):
        p = p.merge(spells[["symbol", "isin", "cid"]], on=["symbol", "isin"], how="left")
        assert p["cid"].notna().all()
        # one row per (cid, date): the one with the most turnover
        p = p.sort_values(["turnover", "series", "symbol", "isin"],
                          ascending=[False, False, True, True]).drop_duplicates(["cid", "date"])
        self.sessions = sessions
        self.cids = pd.Index(sorted(p["cid"].unique()))
        self.T, self.K = len(sessions), len(self.cids)
        ri = sessions.get_indexer(p["date"])
        ci = self.cids.get_indexer(p["cid"])
        assert (ri >= 0).all()

        def mat(vals, fill=np.nan, dtype=float):
            m = np.full((self.T, self.K), fill, dtype=dtype)
            m[ri, ci] = vals
            return m

        self.open = mat(p["open"].values)
        self.close = mat(p["close"].values)
        self.turn = mat(p["turnover"].values, 0.0)
        # traded = a bhavcopy row with a price (at END only the open is loaded)
        self.traded = mat(np.ones(len(p), bool), False, bool) & (
            np.isfinite(self.close) | np.isfinite(self.open))
        self.symbol_at = mat(p["symbol"].values, None, object)
        self.isin_at = mat(p["isin"].values, None, object)

        # split/bonus factor matrix F[t,k] (product of same-day factors)
        F = np.ones((self.T, self.K))
        self.ca_sessions = ca.copy()
        if len(ca):
            ti = np.minimum(sessions.searchsorted(ca["ex_date"]), self.T - 1)
            ok = (sessions.searchsorted(ca["ex_date"]) < self.T)
            ki = self.cids.get_indexer(ca["cid"])
            ok &= ki >= 0
            for t, k, f in zip(ti[ok], ki[ok], ca["factor"].values[ok]):
                F[t, k] *= f
        self.F = F
        # C[t] = product of factors with ex-session strictly after t
        rc = np.cumprod(F[::-1], axis=0)[::-1]          # product over j >= t
        self.C = np.vstack([rc[1:], np.ones((1, self.K))])  # product over j > t
        self.adj = self.close * self.C
        adj_ff = pd.DataFrame(self.adj).ffill().values
        # raw-equivalent last close, correct across a split with no trade
        self.mark = adj_ff / self.C
        # stale counter: consecutive sessions without a trade (0 on a trading day)
        cnt = np.zeros((self.T, self.K), dtype=np.int32)
        run = np.zeros(self.K, dtype=np.int32)
        for t in range(self.T):
            run = np.where(self.traded[t], 0, run + 1)
            cnt[t] = run
        self.stale = cnt
        self.dps = np.zeros((self.T, self.K))

    def add_dividends(self, dv: pd.DataFrame) -> None:
        ti = self.sessions.searchsorted(dv["ex_date"])
        ki = self.cids.get_indexer(dv["cid"])
        ok = (ti < self.T) & (ki >= 0)
        self.dropped_dividends = []
        for t, k, a in zip(ti[ok], ki[ok], dv["dps"].values[ok]):
            # Clarification 13: more than half the prior close (in ex-date share
            # units) is a parse error and is dropped
            prior = self.mark[t - 1, k] * self.F[t, k] if t > 0 else np.nan
            if np.isfinite(prior) and a > 0.5 * prior:
                self.dropped_dividends.append((str(self.sessions[t].date()), self.cids[k], a))
                continue
            self.dps[t, k] += a

    def idx(self, d) -> int:
        i = self.sessions.get_loc(pd.Timestamp(d))
        return int(i)


def load_corpactions(con, end, spells) -> tuple[pd.DataFrame, pd.DataFrame]:
    ca = con.execute(f"""
        SELECT CAST(ex_date AS DATE) ex_date, symbol, isin, action, subject, factor
        FROM read_parquet('{(ROOT / 'data/corpactions').as_posix()}/*.parquet', union_by_name=true)
        WHERE ex_date <= '{end.date()}'""").df()
    ca["ex_date"] = pd.to_datetime(ca["ex_date"])
    ca["cid"] = map_symbol_dates(ca, "symbol", "ex_date", spells)
    # A few split/bonus rows carry a null factor although the subject states it
    # plainly (e.g. AJANTPHARM 2022-06-22 'Bonus- 1:2', a real -33% ex-date gap).
    # Re-parse only equity actions; bonus debentures / NCRPS / preference stay out.
    miss = ca["action"].isin(["split", "bonus"]) & ca["factor"].isna()
    not_equity = re.compile(r"debenture|ncrps|preference", re.I)
    bonus_rx = re.compile(r"bonus\s*-?\s*(\d+)\s*:\s*(\d+)", re.I)
    split_rx = re.compile(r"from\s+r[se]\.?\s*(\d+(?:\.\d+)?)\S*\s*(?:per\s*(?:share)?\s*)?to\s+r[se]\.?\s*(\d+(?:\.\d+)?)", re.I)

    def _reparse(s: str) -> float:
        s = s or ""
        if not_equity.search(s):
            return np.nan
        mb = bonus_rx.search(s)
        if mb:
            a_, b_ = float(mb.group(1)), float(mb.group(2))
            return b_ / (a_ + b_) if a_ > 0 and b_ > 0 else np.nan
        ms = split_rx.search(s) or re.search(r"r[se]\.?\s*(\d+(?:\.\d+)?)\S*\s*to\s+r[se]\.?\s*(\d+(?:\.\d+)?)", s, re.I)
        if ms and "split" in s.lower():
            fr, to = float(ms.group(1)), float(ms.group(2))
            return to / fr if 0 < to < fr else np.nan
        return np.nan

    ca.loc[miss, "factor"] = ca.loc[miss, "subject"].map(_reparse)
    ca.attrs["reparsed"] = ca.loc[miss & ca["factor"].notna(), ["ex_date", "symbol", "subject", "factor"]] \
        .astype(str).values.tolist()
    sb = ca[ca["action"].isin(["split", "bonus"]) & ca["factor"].notna() & (ca["factor"] > 0)]
    # same-day split+bonus pairs: collapse with a product
    sb = sb.drop_duplicates(["cid", "ex_date", "subject"])
    sb = sb.groupby(["cid", "ex_date"], as_index=False)["factor"].prod()
    sb.attrs["reparsed"] = ca.attrs["reparsed"]

    dv = ca[ca["action"] == "dividend"].copy()
    # Clarifications 13 and 24: the amount of every 'dividend' clause, with or
    # without 'Rs' ('Special Dividend 243 Per Share', 'Dividend - 12.50 Per
    # Share'); percent-of-face clauses skipped; 'Rs.0125' read as 0.125; unit
    # distributions skipped. A same-day repeat of the same amount is one dividend.
    rx = re.compile(r"dividend\D*?(\d+(?:\.\d+)?)(\s*%)?", re.I)

    def _amt(s: str) -> float:
        s = s or ""
        if re.search(r"distribution|per\s+unit", s, re.I):
            return 0.0
        tot = 0.0
        for num, pct in rx.findall(s):
            if pct:
                continue
            tot += float("0." + num[1:]) if re.fullmatch(r"0\d+", num) else float(num)
        return tot

    dv["dps"] = dv["subject"].map(_amt)
    n_unparsed = int((dv["dps"] <= 0).sum())
    dv = dv[dv["dps"] > 0].drop_duplicates(["cid", "ex_date", "dps"])
    dv = dv.groupby(["cid", "ex_date"], as_index=False)["dps"].sum()
    dv.attrs["unparsed"] = n_unparsed
    return sb, dv


def load_financials(con, spells) -> pd.DataFrame:
    files = ", ".join(f"'{p.as_posix()}'" for p in statement_files(ROOT / "data" / "financials"))
    f = con.execute(f"""
        SELECT * EXCLUDE (rn) FROM (
          SELECT symbol, company, period_end, broadcast_dt, consolidated, revenue, ebitda,
                 profit_normalised, shares_implied, xbrl_url, total_income, employee_cost,
                 regexp_extract(xbrl_url, '{TAXONOMY_RX}', 1) AS taxonomy,
                 -- a document stored twice under two period ends is one filing; its
                 -- period is the LATEST of the two (spec Clarification 22)
                 row_number() OVER (PARTITION BY xbrl_url
                                    ORDER BY period_end DESC, broadcast_dt DESC) rn
          FROM read_parquet([{files}], union_by_name=true)
          WHERE xbrl_url IS NOT NULL) WHERE rn = 1""").df()
    f["period_end"] = pd.to_datetime(f["period_end"])
    f["broadcast_dt"] = pd.to_datetime(f["broadcast_dt"])
    f["qn"] = f["period_end"].dt.year * 12 + f["period_end"].dt.month   # month number
    f = f[f["period_end"].dt.month % 3 == 0]
    f["cid"] = map_symbol_dates(f, "symbol", "broadcast_dt", spells)
    return f


def load_labels(con, spells, panels: Panels) -> tuple[pd.DataFrame, dict, set]:
    a = con.execute(f"""
        SELECT an_dt, symbol, company, industry
        FROM read_parquet('{(ROOT / 'data/announcements').as_posix()}/**/*.parquet', union_by_name=true)
        """).df()
    a["an_dt"] = pd.to_datetime(a["an_dt"])
    a["cid"] = map_symbol_dates(a, "symbol", "an_dt", spells)
    names = a.dropna(subset=["company"]).groupby("cid")["company"].agg(lambda s: set(s)).to_dict()
    lab = a[a["industry"].notna() & (a["industry"].str.strip() != "-")][["cid", "an_dt", "industry"]]
    lab = lab.sort_values(["an_dt", "industry"])
    # Today's nse_list is 2025-26 filing rows: how a company files now, not what
    # it was at D. Kept for the log only; it does not decide rule 4 (Clarification 28).
    L = pd.DataFrame(json.loads((ROOT / "nse_list.json").read_text(encoding="utf-8")))
    L = L[L["bank"].isin(["B", "F"])]
    isin2cid = spells.drop_duplicates("isin").set_index("isin")["cid"]
    sym2cid = spells.sort_values("max").drop_duplicates("symbol", keep="last").set_index("symbol")["cid"]
    flagged = set(L["isin"].map(isin2cid).dropna()) | set(L["symbol"].map(sym2cid).dropna())
    return lab, names, flagged


def taxonomy_fill(fin: pd.DataFrame) -> set:
    """Static backfill (Clarification 28): stocks whose first four quarters filed
    once the NBFC taxonomy existed were at least half lender-taxonomy filings.
    Used only where none of a stock's latest-4-quarter filings known at D is
    that recent (decision dates to early 2020)."""
    f = fin[(fin["broadcast_dt"] >= NBFC_TAX_START) & fin["cid"].notna()]
    f = f.sort_values("broadcast_dt").drop_duplicates(["cid", "consolidated", "qn"])
    firstq = (f[["cid", "qn"]].drop_duplicates().sort_values(["cid", "qn"])
              .groupby("cid").head(4))
    f = f.merge(firstq, on=["cid", "qn"])
    v = f.assign(l=f["taxonomy"].isin(LENDER_TAX)).groupby("cid")["l"].agg(["sum", "count"])
    return set(v.index[2 * v["sum"] >= v["count"]])


def _scale_breaks(f: pd.DataFrame) -> pd.Series:
    """Clarification 30a, written independently: a filing is on the wrong unit
    scale when, against most of its (up to) 8 nearest decidable quarters of the
    same stock and basis known at D, its revenue, implied share count and
    (where both report it) employee cost all differ by 30x or more in the same
    direction. Returns a boolean Series on f's index."""
    rev = f["revenue"].fillna(f["total_income"])
    lr = np.log10(rev.where(rev > 0))
    ls = np.log10(f["shares_implied"].where(f["shares_implied"] > 0))
    le = np.log10(f["employee_cost"].where(f["employee_cost"] > 0))
    bad = pd.Series(False, index=f.index)
    work = pd.DataFrame({"cid": f["cid"], "cons": f["consolidated"], "qn": f["qn"],
                         "lr": lr, "ls": ls, "le": le})

    def brk(a, b):   # a, b: (lr, ls, le)
        dr, ds, de = a[0] - b[0], a[1] - b[1], a[2] - b[2]
        if not (abs(dr) >= LOG_BAND and abs(ds) >= LOG_BAND and np.sign(dr) == np.sign(ds)):
            return False
        return bool(np.isnan(de) or (abs(de) >= LOG_BAND and np.sign(de) == np.sign(dr)))

    size = work.groupby(["cid", "cons"])["qn"].transform("size")
    key = work.set_index(["cid", "cons", "qn"])
    for (cid, cons), g in work[size > 1].groupby(["cid", "cons"], sort=False):
        dec = g[g["lr"].notna() & g["ls"].notna()]
        vals = dec[["lr", "ls", "le"]].values
        qs = dec["qn"].values
        for i in range(len(dec)):
            others = [j for j in range(len(dec)) if j != i]
            others.sort(key=lambda j: (abs(qs[j] - qs[i]), qs[j]))
            others = others[:SCALE_NEIGH]
            if others and 2 * sum(brk(vals[i], vals[j]) for j in others) > len(others):
                bad[dec.index[i]] = True
    # a basis's only quarter: the other basis's filing for that quarter is the neighbour
    for i, r in work[size == 1].iterrows():
        k = (r["cid"], not r["cons"], r["qn"])
        if k in key.index and pd.notna(r["lr"]) and pd.notna(r["ls"]):
            o = key.loc[k]
            o = o.iloc[0] if isinstance(o, pd.DataFrame) else o
            if pd.notna(o["lr"]) and pd.notna(o["ls"]) and \
                    brk((r["lr"], r["ls"], r["le"]), (o["lr"], o["ls"], o["le"])):
                bad[i] = True
    return bad


# ================================================================ universe + measures
def sector_at(lab: pd.DataFrame, D) -> pd.Series:
    """Latest label known before D; if none known before D, the earliest label ever
    recorded for the stock (the archive's labels start Jan 2022)."""
    before = lab[lab["an_dt"] < D].groupby("cid")["industry"].last()
    earliest = lab.groupby("cid")["industry"].first()
    return before.combine_first(earliest)


def fundamentals_at(fin: pd.DataFrame, D, ca: pd.DataFrame | None = None) -> pd.DataFrame:
    f = fin[fin["broadcast_dt"] < D]
    f = f.sort_values(["broadcast_dt", "xbrl_url"]).drop_duplicates(["cid", "consolidated", "qn"], keep="last")
    # lender-taxonomy votes over the latest 4 known quarters (Clarification 28)
    lqn = f.groupby("cid")["qn"].transform("max")
    w = f[(f["qn"] > lqn - 12) & (f["broadcast_dt"] >= NBFC_TAX_START)]
    votes = w.assign(l=w["taxonomy"].isin(LENDER_TAX)).groupby("cid")["l"].agg(["sum", "count"])
    # mis-scaled filings count as not filed (Clarification 30a)
    f = f[~_scale_breaks(f)]
    # Basis: consolidated when the company's consolidated filings cover the four
    # consecutive quarters ending at its latest known quarter (any basis);
    # otherwise standalone for every quarter.
    latest = f.groupby("cid")["qn"].max()
    cons = f[f["consolidated"]][["cid", "qn"]]
    need = pd.DataFrame({"cid": np.repeat(latest.index.values, 4),
                         "qn": np.concatenate([[q, q - 3, q - 6, q - 9] for q in latest.values])
                         if len(latest) else []})
    cov = need.merge(cons, on=["cid", "qn"], how="inner").groupby("cid").size()
    use_cons = cov.reindex(latest.index).fillna(0) >= 4
    # Share count (spec Clarification 6): shares_implied of the latest known
    # quarter's STANDALONE filing where it has one, else the consolidated one,
    # whatever basis the figures use.
    lq = f[f["qn"] == f["cid"].map(latest)]
    sa = lq[~lq["consolidated"] & lq["shares_implied"].notna()].set_index("cid")
    co = lq[lq["consolidated"] & lq["shares_implied"].notna()].set_index("cid")
    sh = sa["shares_implied"].combine_first(co["shares_implied"])
    sh_bdt = sa["broadcast_dt"].combine_first(co["broadcast_dt"])
    sh_fix = pd.Series("", index=sh.index, dtype=object)
    # Clarification 30b: a count 30x or more from the median of up to four
    # earlier quarters' counts (standalone first, carried through splits and
    # bonuses between the two broadcasts) is replaced by the other basis if that
    # is within 1.5x of the reference, else by the reference.
    prev = f[(f["qn"] < f["cid"].map(latest)) & (f["shares_implied"] > 0)]
    prev = prev.sort_values(["cid", "qn", "consolidated"]).drop_duplicates(["cid", "qn"])
    prev = prev.sort_values("qn").groupby("cid").tail(SH_REF_Q)
    ca_by = {c: g for c, g in ca.groupby("cid")} if ca is not None and len(ca) else {}
    for c, g in prev.groupby("cid"):
        if c not in sh.index or not (sh[c] > 0):
            continue
        bl = pd.Timestamp(sh_bdt[c]).normalize()
        ev_all = ca_by.get(c)
        refs = []
        for r in g.itertuples():
            m = 1.0
            if ev_all is not None:
                ev = ev_all[(ev_all["ex_date"] > pd.Timestamp(r.broadcast_dt).normalize())
                            & (ev_all["ex_date"] <= bl)]
                if len(ev):
                    m = 1.0 / ev["factor"].prod()
            refs.append(r.shares_implied * m)
        ref = float(np.median(refs))
        if abs(math.log10(sh[c] / ref)) < LOG_BAND:
            continue
        alt = co if c in sa.index else sa
        if c in alt.index and alt.at[c, "shares_implied"] > 0 and \
                1 / SH_ALT_TOL <= alt.at[c, "shares_implied"] / ref <= SH_ALT_TOL:
            sh[c], sh_bdt[c], sh_fix[c] = alt.at[c, "shares_implied"], alt.at[c, "broadcast_dt"], "other_basis"
        else:
            sh[c], sh_fix[c] = ref, "carried_reference"
    f = f[f["consolidated"] == f["cid"].map(use_cons)]
    rows = []
    for cid, g in f.sort_values(["qn", "consolidated"], ascending=False).groupby("cid", sort=True):
        q = g["qn"].values
        run = 1
        while run < len(q) and q[run - 1] - q[run] == 3:
            run += 1
        g0 = g.iloc[0]
        r = {"cid": cid, "consolidated": bool(g0["consolidated"]), "run": run,
             "latest_pe": g0["period_end"], "latest_bdt": g0["broadcast_dt"],
             "shares_implied": g0["shares_implied"]}
        if run >= 4:
            top = g.iloc[:4]
            r["ttm_rev"] = top["revenue"].sum(min_count=4)
            r["ttm_ebitda"] = top["ebitda"].sum(min_count=4)
            r["ttm_pn"] = top["profit_normalised"].sum(min_count=4)
            w = g.iloc[:min(run, 8)]
            m = (w["ebitda"] / w["revenue"].where(w["revenue"] > 0)).dropna()
            r["stability"] = -m.std(ddof=1) if len(m) >= 4 else np.nan
            ya = g[g["qn"] == q[0] - 12]
            if len(ya):
                ya = ya.iloc[0]
                r["rev_growth"] = (g0["revenue"] / ya["revenue"] - 1
                                   if pd.notna(ya["revenue"]) and ya["revenue"] > 0 else np.nan)
                r["pn_delta"] = g0["profit_normalised"] - ya["profit_normalised"]
        rows.append(r)
    cols = ["consolidated", "run", "latest_pe", "latest_bdt", "shares_implied", "ttm_rev",
            "ttm_ebitda", "ttm_pn", "stability", "rev_growth", "pn_delta"]
    out = pd.DataFrame(rows).set_index("cid").reindex(columns=cols)
    out["shares_implied"] = sh.reindex(out.index)
    out["shares_bdt"] = sh_bdt.reindex(out.index)
    out["shares_fix"] = sh_fix.reindex(out.index).fillna("")
    out["tax_new"] = votes["count"].reindex(out.index).fillna(0)
    out["tax_lender"] = votes["sum"].reindex(out.index).fillna(0)
    return out


def universe_and_measures(D, P: Panels, fin, lab, names, taxfill, ca) -> pd.DataFrame:
    t = P.idx(D)
    prev = t - 1
    # --- rule 1: ordinary equity, traded in the last 5 sessions before D
    win5 = P.traded[t - TRADED_LAST:t].any(axis=0)
    last_isin = pd.DataFrame(P.isin_at[:t]).ffill().values[-1]
    last_sym = pd.DataFrame(P.symbol_at[:t]).ffill().values[-1]
    ine = np.array([isinstance(x, str) and x.startswith("INE") for x in last_isin])
    # --- rule 2: median turnover over the previous 60 sessions (untraded = 0)
    med_turn = np.median(P.turn[t - TURNOVER_WINDOW:t], axis=0)
    # --- rule 3: >= 200 trading sessions in the previous 365 calendar days
    lo = P.sessions.searchsorted(pd.Timestamp(D) - pd.Timedelta(days=365))
    n365 = P.traded[lo:t].sum(axis=0)
    df = pd.DataFrame({"symbol": last_sym, "r1": win5 & ine, "med_turn": med_turn,
                       "n365": n365}, index=P.cids)
    df["r2"] = df["med_turn"] >= MIN_TURNOVER
    df["r3"] = df["n365"] >= MIN_SESSIONS_365
    # --- rule 4: lenders / insurers
    sec = sector_at(lab, D)
    df["sector"] = sec.reindex(df.index)
    lab_lender = df["sector"].fillna("").str.contains(LENDER_LABEL)
    # Clarification 29 read literally: each stock's LATEST name only.
    _ln = LATEST_NAMES
    name_lender = pd.Series([bool(LENDER_NAME.search(_ln.get(c, "") or "")) for c in df.index], index=df.index)
    # Names are stored as at download (Clarification 29): the name net applies
    # only to stocks with no industry label at all.
    name_lender &= df["sector"].isna()
    # --- rules 5-6: fundamentals
    fu = fundamentals_at(fin, D, ca)
    df = df.join(fu, how="left")
    tn, tl = df["tax_new"].fillna(0), df["tax_lender"].fillna(0)
    df["lender"] = (lab_lender | name_lender | ((tn > 0) & (2 * tl >= tn)) |
                    ((tn == 0) & df.index.isin(list(taxfill))))
    df["r4"] = ~df["lender"]
    fresh = (pd.Timestamp(D) - df["latest_pe"]).dt.days <= MAX_STALE_DAYS
    df["r5"] = (df["run"] >= 4) & fresh
    df["r6"] = (df["ttm_pn"] > 0) & (df["ttm_ebitda"] > 0)
    # --- rule 7: market cap = raw close of the session before D x adjusted shares
    raw_close = P.mark[prev]
    df["close_prev"] = raw_close
    k = P.cids.get_indexer(df.index)
    adj_shares = df["shares_implied"].values.astype(float).copy()
    ca_before = ca[ca["ex_date"] <= P.sessions[prev]]
    for c, grp in ca_before.groupby("cid"):
        if c not in df.index:
            continue
        bdt = df.at[c, "shares_bdt"]
        if pd.isna(bdt):
            continue
        after = grp[grp["ex_date"].dt.normalize() > pd.Timestamp(bdt).normalize()]
        if len(after):
            adj_shares[df.index.get_loc(c)] /= after["factor"].prod()
    df["shares_adj"] = adj_shares
    df["mcap"] = df["close_prev"] * df["shares_adj"]
    df["r7"] = np.isfinite(df["mcap"]) & (df["mcap"] > 0)
    df["in_univ"] = df[["r1", "r2", "r3", "r4", "r5", "r6", "r7"]].all(axis=1)

    # --- measures (computed for everyone, ranked within the universe only)
    df["margin"] = df["ttm_ebitda"] / df["ttm_rev"].where(df["ttm_rev"] > 0)
    df["profit_growth"] = df["pn_delta"] / df["ttm_rev"].where(df["ttm_rev"] > 0)
    df["earnings_yield"] = df["ttm_pn"] / df["mcap"]
    df["sales_yield"] = df["ttm_rev"] / df["mcap"]
    adjff = pd.DataFrame(P.adj[:t]).ffill().values   # rows 0..t-1 only
    near = adjff[t - MOM_NEAR, k] if t - MOM_NEAR >= 0 else np.nan
    far = adjff[t - MOM_FAR, k] if t - MOM_FAR >= 0 else np.nan
    df["mom_12_1"] = near / far - 1
    # low vol: log returns between consecutive traded closes in the last 252 sessions
    w = P.adj[t - VOL_WINDOW:t][:, k]      # closes in the last 252 sessions (Clarification 3)
    vols = np.full(len(k), np.nan)
    for j in range(len(k)):
        x = w[:, j]
        x = x[np.isfinite(x)]
        if len(x) >= 3:
            vols[j] = np.std(np.diff(np.log(x)), ddof=1)
    df["low_vol"] = -vols
    df["D"] = pd.Timestamp(D)
    return df


def score(df: pd.DataFrame) -> pd.DataFrame:
    u = df[df["in_univ"]].copy()
    for m in MEASURES:
        v = u[m].replace([np.inf, -np.inf], np.nan)
        u[m + "_pct"] = v.rank(pct=True, method="average").fillna(0.5)
    for g, ms in GROUPS.items():
        u[g] = u[[m + "_pct" for m in ms]].mean(axis=1)
    u["composite"] = u[list(GROUPS)].mean(axis=1)
    u = u.rename_axis("cid").sort_values(["composite", "symbol", "cid"], ascending=[False, True, True])
    u["rank"] = np.arange(1, len(u) + 1)
    return u


# ================================================================ simulator
class Book:
    def __init__(self, P: Panels, cost: float, exit_mult: float = 1.0):
        self.P, self.cost, self.exit_mult = P, cost, exit_mult
        self.sh = np.zeros(P.K)
        self.cash = 1.0
        self.pending: dict[int, tuple[float, int]] = {}
        self.trades: list[tuple] = []
        self.divs = 0.0
        self.costs = 0.0
        self.forced: list[tuple] = []
        self.open_spells: dict[int, tuple[int, float]] = {}
        self.closed_spells: list[tuple] = []

    def _exec(self, t, k, price, tgt_val):
        new = tgt_val / price if tgt_val > 0 else 0.0
        before = self.sh[k]
        d = new - before
        if abs(d) < 1e-15:
            return
        val = d * price
        c = abs(val) * self.cost
        self.cash -= val + c
        self.costs += c
        self.sh[k] = new
        self.trades.append((t, k, d, price, val))
        adj_px = price * self.P.C[t, k]
        if before <= 0 and new > 0:
            self.open_spells[k] = (t, adj_px)
        elif new <= 0 and k in self.open_spells:
            t0, e = self.open_spells.pop(k)
            self.closed_spells.append((self.P.cids[k], self.P.sessions[t0], self.P.sessions[t],
                                       adj_px / e, False, e))

    def _fill(self, t, orders: dict):
        """Execute rupee targets at the open of t (spec Clarification 11): sells
        (and trims) first, then buys, scaled down pro rata if cash net of costs
        cannot cover every buy."""
        P = self.P
        buys = []
        for k, tgt in orders.items():
            cur = self.sh[k] * P.open[t, k]
            if tgt < cur - 1e-12:
                self._exec(t, k, P.open[t, k], tgt)
            elif tgt > cur + 1e-12:
                buys.append((k, tgt - cur))
        need = sum(d for _, d in buys) * (1 + self.cost)
        scale = min(1.0, self.cash / need) if need > 0 else 0.0
        for k, d in buys:
            if d * scale > 0:
                self._exec(t, k, P.open[t, k], self.sh[k] * P.open[t, k] + d * scale)

    def rebalance(self, t, targets: np.ndarray, slots: int):
        P = self.P
        self.pending.clear()
        ref = np.where(P.traded[t], P.open[t], P.mark[t])
        held = np.flatnonzero(self.sh > 0)
        V = self.cash + np.nansum(self.sh[held] * ref[held])
        inS = np.zeros(P.K, bool)
        inS[targets] = True
        # every selected name is resized to NAV(open of D) / N and costs come
        # out of cash, not out of the target (spec Clarification 11)
        T = V / slots
        now = {}
        for k in np.union1d(held, targets):
            tgt = T if inS[k] else 0.0
            if P.traded[t, k]:
                now[k] = tgt
            else:
                self.pending[k] = (tgt, t + EXEC_WAIT)
        self._fill(t, now)

    def run(self, plan: dict[int, tuple[np.ndarray, int]], t0: int, t_end: int) -> pd.DataFrame:
        P = self.P
        out = [(P.sessions[t0], "open", 1.0)]
        for t in range(t0, t_end + 1):
            _guard(P.sessions[t])
            # 1. splits/bonuses: raw share counts scale by 1/f
            f = P.F[t]
            chg = (f != 1) & (self.sh > 0)
            if chg.any():
                self.sh[chg] /= f[chg]
            # 2. cash dividends on shares held at the previous close
            if t > t0:
                d = np.nansum(self.sh * P.dps[t])
                self.cash += d
                self.divs += d
            if t == t_end:
                px = np.where(P.traded[t], P.open[t], P.mark[t])
                held = self.sh > 0
                out.append((P.sessions[t], "open", self.cash + np.nansum(self.sh[held] * px[held])))
                break
            # 3. orders at the open
            if t in plan:
                step = plan[t]
                tg, slots = step(self, t) if callable(step) else step
                self.rebalance(t, tg, slots)
            elif self.pending:
                now = {}
                for k in list(self.pending):
                    tgt, dl = self.pending[k]
                    if P.traded[t, k]:
                        now[k] = tgt
                        del self.pending[k]
                    elif t >= dl:
                        del self.pending[k]
                self._fill(t, now)
            # 4. forced exit after 20 sessions without a trade, at the last close
            stale = np.flatnonzero((self.sh > 0) & (P.stale[t] >= FORCED_EXIT_SESSIONS))
            for k in stale:
                self._exec(t, k, P.mark[t, k] * self.exit_mult, 0.0)
                self.pending.pop(k, None)
                self.forced.append((P.sessions[t], P.cids[k]))
            # 5. mark at the close
            held = self.sh > 0
            out.append((P.sessions[t], "close", self.cash + np.nansum(self.sh[held] * P.mark[t][held])))
        return pd.DataFrame(out, columns=["date", "mark", "nav"])

    def spells(self, t_end) -> pd.DataFrame:
        """Holding spells with adjusted entry/exit prices (price return only)."""
        P = self.P
        rows = list(self.closed_spells)
        for k, (t0, e) in self.open_spells.items():
            px = P.open[t_end, k] if P.traded[t_end, k] else P.mark[t_end, k]
            rows.append((P.cids[k], P.sessions[t0], P.sessions[t_end], px * P.C[t_end, k] / e, True, e))
        sp = pd.DataFrame(rows, columns=["cid", "entry", "exit", "gross", "open_at_end", "entry_adj"])
        # Clarification 18: doubled = an adjusted CLOSE during the episode reached
        # twice the (adjusted) entry price
        mx = []
        for r in sp.itertuples():
            k = P.cids.get_loc(r.cid)
            seg = P.adj[P.idx(r.entry):P.idx(r.exit) + 1, k]
            mx.append(np.nanmax(seg) if np.isfinite(seg).any() else np.nan)
        sp["max_adj_close"] = mx
        return sp


# ================================================================ metrics
def perf(nav: pd.Series, dates: pd.Series) -> dict:
    r = nav.pct_change().dropna()
    yrs = (dates.iloc[-1] - dates.iloc[0]).days / 365.25
    cagr = nav.iloc[-1] ** (1 / yrs) - 1
    vol = r.std(ddof=1) * math.sqrt(252)
    dd = (nav / nav.cummax() - 1).min()
    return {"cagr": cagr, "vol": vol, "sharpe_rf0": (r.mean() * 252) / vol if vol else None,
            "max_dd": dd, "total_return": nav.iloc[-1] / nav.iloc[0] - 1, "years": yrs}


def relative(a: pd.Series, b: pd.Series) -> dict:
    ra, rb = a.pct_change(), b.pct_change()
    x = (ra - rb).dropna()
    te = x.std(ddof=1) * math.sqrt(252)
    return {"tracking_error": te, "information_ratio": (x.mean() * 252) / te if te else None,
            "active_return_ann": x.mean() * 252}


def calendar_years(nav: pd.Series, dates: pd.Series) -> dict:
    s = pd.Series(nav.values, index=pd.DatetimeIndex(dates))
    out = {}
    prev = s.iloc[0]
    for y, g in s.groupby(s.index.year):
        out[str(y)] = g.iloc[-1] / prev - 1
        prev = g.iloc[-1]
    return out


# ================================================================ main
def main(argv=None) -> int:
    global UNLOCK, END_DATE
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-final-test", action="store_true",
                    help="required to compute anything after the in-sample end. Do not use.")
    ap.add_argument("--no-leak-test", action="store_true")
    ap.add_argument("--no-quintiles", action="store_true")
    ap.add_argument("--end", default=None, help="final NAV mark (open) date; must be a session")
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    global FULL_END, OUT
    if a.out:
        OUT = Path(a.out)
    if a.end:
        if not a.run_final_test:
            raise SystemExit("--end after the in-sample end needs --run-final-test")
        FULL_END = pd.Timestamp(a.end)
    UNLOCK = bool(a.run_final_test)

    con = duck()
    sessions_all = all_sessions(con)
    Ds, END = decision_dates(sessions_all)
    END_DATE = END
    assert END in sessions_all, "END must be a session"
    sessions = sessions_all[sessions_all <= END]
    log(f"decision dates: {[d.date().isoformat() for d in Ds]}  end(open)={END.date()}")
    assert len(Ds) == (16 if FULL_END is None else len(Ds))

    p = load_prices(con, END)
    log(f"prices {len(p):,} rows")
    spells = build_ids(load_identity_keys(con), all_sessions(con, "1990-01-01"))
    ca, dv = load_corpactions(con, END, spells)
    reparsed = list(ca.attrs.get("reparsed", []))
    P = Panels(p, spells, sessions, ca)
    P.add_dividends(dv)
    log(f"re-parsed null-factor split/bonus rows: {reparsed}")
    log(f"panels {P.T} x {P.K}; split/bonus events {len(ca)}; dividends {len(dv)} "
        f"(unparsed {dv.attrs['unparsed']})")
    fin = load_financials(con, spells)
    # static classification input (Clarification 28), like the label backfill
    taxfill = taxonomy_fill(fin)
    global LATEST_NAMES
    _f = fin.dropna(subset=["company"]).sort_values("broadcast_dt")
    LATEST_NAMES = _f.groupby("cid")["company"].last().to_dict()
    fin = fin[fin["broadcast_dt"] < END]
    lab, names, flagged = load_labels(con, spells, P)
    log(f"financial rows {len(fin):,}; labelled stocks {lab['cid'].nunique()}; "
        f"nse_list lender ids {len(flagged)} (diagnostic only); taxonomy backfill {len(taxfill)}")

    OUT.mkdir(parents=True, exist_ok=True)
    frames, scored = [], {}
    for D in Ds:
        df = universe_and_measures(D, P, fin, lab, names, taxfill, ca)
        u = score(df)
        frames.append(df)
        scored[D] = u
        log(f"{D.date()}  universe {len(u):4d}  (r1 {df.r1.sum()}, +turn {(df.r1 & df.r2).sum()}, "
            f"+365 {(df.r1 & df.r2 & df.r3).sum()}, +nonlender {(df.r1 & df.r2 & df.r3 & df.r4).sum()}, "
            f"+r5 {(df.r1 & df.r2 & df.r3 & df.r4 & df.r5).sum()}, "
            f"+r6 {(df.r1 & df.r2 & df.r3 & df.r4 & df.r5 & df.r6).sum()}, "
            f"cons {int(u['consolidated'].sum())})")

    # ---------------- outputs: universe + ranks
    uni = pd.concat([u[["D", "symbol"]] for u in scored.values()])
    uni["D"] = uni["D"].dt.date
    uni.to_csv(OUT / "universe.csv", index=False)
    cols = ["D", "symbol", "sector", "consolidated", "mcap", *MEASURES,
            *[m + "_pct" for m in MEASURES], *GROUPS, "composite", "rank"]
    rk = pd.concat([u.reset_index().rename(columns={"index": "cid"})[["cid", *cols]]
                    for u in scored.values()])
    rk["D"] = rk["D"].dt.date
    rk.to_csv(OUT / "ranks.csv", index=False)

    # ---------------- BASE portfolio
    t_idx = {D: P.idx(D) for D in Ds}
    t0, t_end = t_idx[Ds[0]], P.idx(END)
    base = Book(P, COST)
    hold_rows = []

    # The book must be simulated forward to know what is held at each D, so the
    # selection runs inside the simulation loop via a small driver.
    def pick(D, u: pd.DataFrame, held: set) -> list:
        sec = u["sector"].where(u["sector"].notna(), "UNLABELLED:" + u.index.to_series())
        keep = [c for c in u.index[:BUFFER] if c in held]
        count: dict = {}
        for c in keep:
            count[sec[c]] = count.get(sec[c], 0) + 1
        out = list(keep)
        for c in u.index:
            if len(out) >= N:
                break
            if c in out:
                continue
            s = sec[c]
            if count.get(s, 0) >= SECTOR_CAP:
                continue
            out.append(c)
            count[s] = count.get(s, 0) + 1
        return out, set(keep)

    def make_step(D):
        def step(book, t):
            held = {P.cids[k] for k in np.flatnonzero(book.sh > 0)}
            sel, keep = pick(D, scored[D], held)
            for c in sel:
                hold_rows.append((D.date(), scored[D].at[c, "symbol"], c,
                                  "kept" if c in keep else "new", int(scored[D].at[c, "rank"]),
                                  scored[D].at[c, "sector"]))
            return P.cids.get_indexer(sel), N
        return step

    strat = base.run({t_idx[D]: make_step(D) for D in Ds}, t0, t_end)
    _tr = pd.DataFrame(base.trades, columns=["t", "k", "d", "px", "val"])
    _tr["date"] = P.sessions[_tr["t"].values].date
    _tr["cid"] = P.cids[_tr["k"].values]
    _tr.to_csv(OUT / "trades.csv", index=False)
    hold = pd.DataFrame(hold_rows, columns=["D", "symbol", "cid", "status", "rank", "sector"])
    hold.to_csv(OUT / "holdings.csv", index=False)

    # ---------------- universe equal-weight benchmark (same costs)
    ew = Book(P, COST)
    plan = {t_idx[D]: (P.cids.get_indexer(scored[D].index), len(scored[D])) for D in Ds}
    ew_nav = ew.run(plan, t0, t_end)

    nav = strat.rename(columns={"nav": "strategy"})
    nav["universe_ew"] = ew_nav["nav"].values
    assert (nav["date"].values == ew_nav["date"].values).all()
    nav["date"] = nav["date"].dt.date
    nav.to_csv(OUT / "nav.csv", index=False)

    # ---------------- quintile diagnostics (no buffer, no costs)
    def quintile_navs(key: str) -> dict:
        res = {}
        for q in range(5):
            bk = Book(P, 0.0)
            pl = {}
            for D in Ds:
                u = scored[D].sort_values([key, "symbol", "cid"], ascending=[False, True, True])
                parts = np.array_split(np.arange(len(u)), 5)
                ids = P.cids.get_indexer(u.index[parts[q]])
                pl[t_idx[D]] = (ids, len(ids))
            res[f"Q{q + 1}"] = bk.run(pl, t0, t_end)["nav"].values
        return res

    dates = pd.to_datetime(strat["date"])
    quint = {}
    for key in ([] if a.no_quintiles else ["composite", *GROUPS]):
        qn = quintile_navs(key)
        qp = {k: perf(pd.Series(v), dates)["cagr"] for k, v in qn.items()}
        spread = relative(pd.Series(qn["Q1"]), pd.Series(qn["Q5"]))
        quint[key] = {"cagr_by_quintile": qp,
                      "top_minus_bottom_cagr": qp["Q1"] - qp["Q5"],
                      "top_minus_bottom_daily_ann": spread["active_return_ann"]}
        log(f"quintile[{key}] Q1..Q5 CAGR: " + ", ".join(f"{v:.3f}" for v in qp.values()))

    # ---------------- metrics
    s_nav, e_nav = nav["strategy"], nav["universe_ew"]
    m = {"strategy": perf(s_nav, dates), "universe_ew": perf(e_nav, dates)}
    m["strategy_vs_universe_ew"] = relative(s_nav, e_nav)
    m["calendar_years"] = {"strategy": calendar_years(s_nav, dates),
                           "universe_ew": calendar_years(e_nav, dates)}
    # index context: NSE's official Total Returns Index (gross `tri` column,
    # data/external/nifty_tri.parquet; Clarification 17), close-to-close on the
    # strategy's closes, rows before END only (holdout lock)
    idx = con.execute(f"""SELECT CAST(date AS DATE) AS date, index_name, tri AS close FROM read_parquet(
        '{(ROOT / 'data/external/nifty_tri.parquet').as_posix()}')
        WHERE CAST(date AS DATE) < DATE '{END.date()}' AND index_name IN
        ('NIFTY 500','NIFTY MIDCAP 150','NIFTY SMALLCAP 250')""").df()
    idx["date"] = pd.to_datetime(idx["date"])
    closes = strat[strat["mark"] == "close"].copy()
    closes["date"] = pd.to_datetime(closes["date"])
    closes["ew"] = ew_nav.loc[ew_nav["mark"] == "close", "nav"].values
    for nm, g in idx.groupby("index_name"):
        _guard(g["date"].max())
        s = g.set_index("date")["close"].reindex(closes["date"]).ffill()
        ss = pd.Series(closes["nav"].values)
        ii = pd.Series(s.values)
        m[f"index:{nm}"] = {"total_return_index": True, **perf(ii / ii.iloc[0], closes["date"].reset_index(drop=True)),
                            "strategy_vs": relative(ss, ii)}

    # turnover, holding period, doubles
    tr = pd.DataFrame(base.trades, columns=["t", "k", "d", "px", "val"])
    navser = pd.Series(nav["strategy"].values)
    yrs = m["strategy"]["years"]
    one_side_all = tr["val"].abs().sum() / 2 / navser.mean() / yrs
    # Clarification 18: excluding the initial build (the first D's orders, which
    # may fill up to 5 sessions later)
    one_side = tr.loc[tr["t"] > t0 + EXEC_WAIT, "val"].abs().sum() / 2 / navser.mean() / yrs
    sp = base.spells(t_end)
    hp = (sp["exit"] - sp["entry"]).dt.days
    m["strategy"].update({
        "turnover_one_sided_annual": one_side,
        "turnover_one_sided_annual_incl_initial": one_side_all,
        "avg_holding_period_days": float(hp.mean()),
        "n_positions_opened": int(len(sp)),
        "n_doubled_while_held": int((sp["max_adj_close"] >= 2 * sp["entry_adj"]).sum()),
        "avg_holdings": float(hold.groupby("D").size().mean()),
        "dividends_credited": base.divs, "costs_paid": base.costs,
        "forced_exits": [(str(d.date()), c) for d, c in base.forced],
    })
    m["universe_ew"].update({"dividends_credited": ew.divs, "costs_paid": ew.costs,
                             "n_forced_exits": len(ew.forced)})
    m["quintiles"] = quint
    sizes = {str(D.date()): int(len(u)) for D, u in scored.items()}
    m["universe_sizes"] = sizes
    m["universe_median_size"] = float(np.median(list(sizes.values())))

    # ---------------- sanity checks
    san = {}
    for D in [d for d in Ds if d.year == 2021]:
        u = frames[Ds.index(D)]
        san[str(D.date())] = {s: (None if s not in u.index else
                                  {"mcap_cr": round(float(u.at[s, "mcap"]) / 1e7, 0),
                                   "in_universe": bool(u.at[s, "in_univ"]),
                                   "pe": round(float(u.at[s, "mcap"] / u.at[s, "ttm_pn"]), 1)
                                   if u.at[s, "ttm_pn"] else None})
                              for s in ["RELIANCE", "TCS", "INFY"]}
    m["sanity_mcaps_2021"] = san
    r_s = s_nav.pct_change().abs().max()
    r_e = e_nav.pct_change().abs().max()
    m["sanity_max_abs_daily_return"] = {"strategy": float(r_s), "universe_ew": float(r_e)}
    # largest single-stock daily move inside held positions (adjusted)
    adjr = pd.DataFrame(P.adj).ffill().pct_change().values
    worst = []
    for c in set(hold["cid"]):
        k = P.cids.get_loc(c)
        x = adjr[t0:t_end, k]
        j = np.nanargmax(np.abs(x))
        worst.append((c, str(P.sessions[t0 + j].date()), float(x[j])))
    worst.sort(key=lambda r: -abs(r[2]))
    m["sanity_largest_held_stock_moves"] = worst[:8]

    # dividend yield of single events on held names (catches misparsed amounts)
    dy = []
    for c in set(hold["cid"]):
        k = P.cids.get_loc(c)
        for t in np.flatnonzero(P.dps[t0:t_end, k]) + t0:
            dy.append((c, str(P.sessions[t].date()), float(P.dps[t, k]),
                       float(P.dps[t, k] / P.mark[t - 1, k])))
    dy.sort(key=lambda r: -r[3])
    m["sanity_largest_dividend_yields_held"] = dy[:8]
    # holdout guard self-test: must raise for the first session after END
    try:
        _guard(END + pd.Timedelta(days=1))
        m["holdout_guard_selftest"] = "FAILED: guard did not raise"
    except HoldoutViolation:
        m["holdout_guard_selftest"] = "ok: raises after " + str(END.date())

    # ---------------- leak test
    if not a.no_leak_test:
        m["leak_test"] = leak_test(con, Ds[5], p, spells, sessions, fin, lab, names, taxfill, scored[Ds[5]])
        log(f"leak test: {m['leak_test']}")

    m["ambiguities"] = AMBIGUITIES
    m["reparsed_null_factor_actions"] = reparsed
    m["config"] = {"N": N, "buffer": BUFFER, "sector_cap": SECTOR_CAP, "cost_per_side": COST,
                   "rebalance": "quarterly", "dividends": "cash, zero interest",
                   "forced_exit": f"{FORCED_EXIT_SESSIONS} sessions, last close",
                   "in_sample_dates": [str(d.date()) for d in Ds], "end_open": str(END.date())}
    (OUT / "metrics.json").write_text(json.dumps(m, indent=2, default=_js), encoding="utf-8")
    log(f"strategy: {json.dumps(m['strategy'], default=_js)[:400]}")
    log(f"universe_ew: {json.dumps(m['universe_ew'], default=_js)[:300]}")
    log(f"vs EW: {m['strategy_vs_universe_ew']}")
    return 0


def _js(o):
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (pd.Timestamp,)):
        return str(o.date())
    return str(o)


def leak_test(con, D, p, spells, sessions, fin, lab, names, taxfill, ref: pd.DataFrame) -> dict:
    """Rebuild everything from inputs truncated to what was knowable at D and check
    the universe and every measure are unchanged. Classification inputs (industry
    labels, the taxonomy backfill, names) are static metadata and are held fixed."""
    p2 = p[p["date"] < D]
    s2 = sessions[sessions <= D]
    ca2, dv2 = load_corpactions(con, pd.Timestamp(D) - pd.Timedelta(days=1), spells)
    # D itself must exist as a session row for indexing; it carries no data
    P2 = Panels(p2, spells, s2, ca2)
    fin2 = fin[fin["broadcast_dt"] < D]
    df2 = universe_and_measures(D, P2, fin2, lab, names, taxfill, ca2)
    u2 = score(df2)
    same_univ = set(u2.index) == set(ref.index)
    diffs = {}
    common = ref.index.intersection(u2.index)
    for c in [*MEASURES, "composite", "mcap"]:
        a, b = ref.loc[common, c].astype(float), u2.loc[common, c].astype(float)
        both = a.notna() & b.notna()
        diffs[c] = float(np.nanmax(np.abs((a[both] - b[both]) / b[both].abs().clip(lower=1e-12))))\
            if both.any() else 0.0
        if (a.isna() != b.isna()).any():
            diffs[c + "_nan_mismatch"] = int((a.isna() != b.isna()).sum())
    ok = same_univ and all(v < 1e-9 for k, v in diffs.items() if not k.endswith("mismatch")) \
        and not any(k.endswith("mismatch") for k in diffs)
    return {"D": str(pd.Timestamp(D).date()), "same_universe": same_univ,
            "n": int(len(ref)), "max_rel_diff": diffs, "passed": bool(ok)}


AMBIGUITIES = [
    "Sessions = distinct bhavcopy dates (EQ/BE rows). D = first session on/after the anchor. "
    "END = first session on/after 15 Feb 2023; the final NAV mark is at that session's OPEN "
    "(held names not trading at END are marked at last close).",
    "Stock identity (spec Clarification 23): built from the (symbol, ISIN, first/last date) keys of the WHOLE bhavcopy "
    "archive (no prices), because companies renamed after 2023 have their old filings stored "
    "under the new symbol. (symbol, ISIN) spells linked when they share a symbol (ISIN change on "
    "split) or an ISIN (rename) with a gap <=10 sessions. Output symbol = symbol of the "
    "stock's last trade before D. Financials/corpactions/announcements mapped by symbol and date.",
    "Rule 1: ISIN/series tested on the stock's last trade row before D. "
    "Rule 2: median over the 60 market sessions before D with 0 for sessions the stock did not "
    "trade. Rule 3: trading days in [D-365 calendar days, D).",
    "Rule 4 lender/insurer = (a) NSE industry label matching bank|financ|insurance|nbfc "
    "(label = latest known before D, else the EARLIEST label ever recorded, because the "
    "announcements archive starts Jan 2022 -- a static-classification backfill), OR (b) for a "
    "stock with no label at all, any company name (from financials/announcements) matching "
    "bank/insurance/assurance/finance/financial/fincorp/finserv/finvest/credit/leasing, OR (c) "
    "the XBRL taxonomy vote of its latest-4-quarter filings known before D (from Jan 2020), else "
    "the static taxonomy backfill. Today's nse_list is logged, not used (Clarification 28). "
    "Delisted lenders are caught by (a) if they filed after 2022, by (b) by name, by (c) if they "
    "filed from 2020, and bank-format filings carry no revenue/EBITDA so they fail rule 6 anyway.",
    "Basis per company at D: consolidated if consolidated filings known before D cover all four "
    "consecutive quarters ending at the latest period_end known at D (either basis), else "
    "standalone; every quarter then comes from that basis. (Quarterly consolidated filing only "
    "became mandatory from the Jun-2019 quarter, so 'where the company filed them' is read as "
    "'filed them for every quarter the rules need'.) Revisions: "
    "latest broadcast_dt < D per (stock, basis, period_end).",
    "Rule 5: the four most recent quarters of that basis must be consecutive (3-month steps) "
    "ending at the latest quarter; latest period_end >= D-200 days. TTM = sum of those 4 "
    "(all four non-null). Rule 6 needs TTM profit_normalised>0 and TTM ebitda>0.",
    "Stability: sample std (ddof=1) of ebitda/revenue over the consecutive run from the latest "
    "quarter, capped at 8, needing >=4 non-null margins (revenue>0).",
    "Growth: year-ago quarter = same basis, period_end 12 months earlier, need not be in the "
    "consecutive run; missing -> 0.5 percentile. Revenue growth needs year-ago revenue > 0.",
    "Momentum: sessions are MARKET sessions; adjusted close at session t-21 and t-252 (D = t), "
    "forward-filled from the stock's last trade at or before that session.",
    "Low vol: sample std (ddof=1) of log returns between consecutive traded adjusted closes "
    "within the last 252 market sessions [t-252, t-1] (spec Clarification 3).",
    "Percentiles: pandas rank(pct=True, method='average') within the universe, higher value = "
    "higher percentile (so best -> 1); NaN/inf -> 0.5. Composite ties broken by symbol ascending.",
    "Market cap: raw close = last traded close at or before the session before D (stock traded "
    "in the last 5 sessions). Shares = shares_implied of the latest known quarter's STANDALONE "
    "filing where it has one, else consolidated (spec Clarification 6), divided by split/bonus "
    "factors with ex_date strictly after that filing's broadcast calendar date and on/before the "
    "session before D. Non-positive/NaN -> excluded.",
    "Portfolio: 'held' = positive shares at the open of D. Kept = held AND in universe AND "
    "rank <= 2N. Kept names count toward the sector cap (never force-sold for the cap). "
    "Vacancies filled in rank order skipping sectors at 3. Stocks with no NSE industry label "
    "are each their own sector (uncapped).",
    "Rebalance (spec Clarification 11): V = cash + holdings at the D open (last close if not "
    "trading); every target is V/N; sells and trims first, then buys, scaled pro rata if cash "
    "net of costs cannot cover them; unfilled slots stay cash. Orders for stocks not trading "
    "on D wait up to 5 sessions (executed at that session's open at the rupee target, same "
    "sells-first rule) then are cancelled.",
    "Dividends (spec Clarifications 13, 24): the amount of every 'dividend' clause, with or "
    "without 'Rs'; percent-of-face clauses and unit distributions skipped; same-day repeats "
    "of one amount count once; more than half the prior close is dropped as a parse error. "
    "Credited on the ex-session to shares held at the previous close. "
    "Splits/bonus scale share counts on the ex-session (ex_date on a non-session -> next session).",
    "Forced exit: when a held stock has not traded for 20 consecutive market sessions it is "
    "sold at its last close on that 20th session, paying the 0.20% cost.",
    "Universe EW benchmark uses the same simulator: all universe names at D, T = (V-costs)/|U|, "
    "same costs, dividends, delays and forced exit.",
    "Quintiles: universe sorted by composite desc (symbol asc), np.array_split into 5; Q1 top; "
    "no costs, no buffer, same simulator. Spread reported as CAGR(Q1)-CAGR(Q5) and as the "
    "annualised mean daily return difference. Same for each group score.",
    "Sharpe with rf = 0 on daily returns x sqrt(252). Turnover = sum |traded value| / 2 / mean "
    "NAV / years, excluding fills on or before the 5th session after the first D (spec "
    "Clarifications 18, 26; also reported including them). Holding period = calendar days "
    "from entry to exit (open spells censored at END). 'Doubled while held' = an adjusted close "
    "in the episode >= 2x the adjusted entry price (spec Clarification 18).",
    "Null-factor split/bonus rows: the task note says use non-null factors only, but a few "
    "rows state the ratio in the subject with a null factor (in-sample: AJANTPHARM 2022-06-22 "
    "'Bonus- 1:2', a real -34% ex-date gap). Read as: re-parse equity 'Bonus a:b' -> b/(a+b) "
    "and 'Face value split from Rs X to Rs Y' -> Y/X; bonus debentures, NCRPS and preference "
    "bonuses stay unadjusted. Duplicate rows removed on (stock, ex_date, subject) before the "
    "same-day product (no in-sample case where the same factor repeats under a different subject).",
    "END session: only its OPEN (and a trade flag) is loaded; its close and turnover are "
    "masked at load time.",
    "Trials are NOT recorded by this checker (it duplicates the maker's run; recording would "
    "double-count the search).",
    "Index benchmarks (spec Clarification 17, 2026-09-22): NSE's official Total Returns Index "
    "(gross, `tri`) for Nifty 500, Midcap 150 and Smallcap 250 from data/external/nifty_tri.parquet, "
    "close-to-close on the strategy's closes.",
    "Rule 4 (spec Clarifications 28-29, 2026-09-22): today's nse_list no longer flags lenders; the "
    "XBRL taxonomy of the stock's filings for its latest 4 known quarters votes (NBFC_INDAS, BANKING, "
    "GI, LI; filings broadcast from Jan 2020, when the NBFC taxonomy began), with a static backfill "
    "from the stock's first four quarters under it for earlier dates. The lender-name net applies "
    "only to stocks with no industry label.",
    "Data quality (spec Clarification 30, 2026-09-22): a filing whose revenue, implied shares and "
    "employee cost all break 30x in one direction against most of its 8 nearest known quarters is "
    "treated as not filed; a latest share count 30x from the median of up to 4 earlier quarters' "
    "counts is replaced by the other basis (if within 1.5x) or by that median.",
]


if __name__ == "__main__":
    raise SystemExit(main())
