"""Independent cross-check of strategy v2, written from the spec text alone.

Sources: research/strategy/v2-spec.md (items 1-8, "What does not change", A1,
A2, A4-A6 and every dated Clarification) and research/strategy/v1-spec.md
(rules, Clarifications 1-40), which v2 keeps except where v2 changes them.

This is the CHECKER, not the production engine. It was written without opening
zen/universe, zen/portfolio, zen/signals, zen/validation, jobs/backtest_v1.py
or jobs/backtest_v2.py. Its lineage is jobs/crosscheck_v1.py, whose loaders,
identity links, data screens and benchmark clock it copies unchanged. ROCE,
debt to equity and the balance-sheet choice (Clarification to A5) are written
here from the text, not imported. The only zen import is
zen.data.financials.statement_files.

    .venv/Scripts/python.exe -m jobs.crosscheck_v2 --variant a       # in-sample only
    .venv/Scripts/python.exe -m jobs.crosscheck_v2 --variant b --out data/backtest/v2b_is_check
    .venv/Scripts/python.exe -m jobs.crosscheck_v2 --variant a --run-final-test --end 2026-09-18 --out <dir>

What v2 changes (each read from the spec text; readings in AMBIGUITIES):
  * 12 names, kept while in the universe and ranked in the top 24, vacancies
    best rank first under 3 per sector; the sector is the `sector` column of
    data/reference/industry_nse.parquet joined by stock id, unlabelled = own sector;
  * hard filters as universe rules: market cap above Rs 100 crore, trailing
    P/E positive and at most 70, and from the February 2023 decision date ROCE
    at least 10% and debt / equity below 1.5 from the balance sheet the
    Clarification to A5 chooses (no usable balance sheet fails both);
  * quality group from February 2023 = ROCE and Stability (before: Margin and
    Stability, as v1);
  * graded trend filter on Nifty 500 TRI closes up to the session before D:
    cash 0 / 20 / 27.5 / 35% of NAV at D's open;
  * 1/sigma sizing held within 0.5x and 1.5x of (1 - cash)/12;
  * a new position is bought in three tranches of V/3 at D, D+21 and D+42
    sessions; variant (a) under v1's buying rule, variant (b) under A1's rule
    (close <= 1.10 x its 50-session mean and 14-session Wilder RSI < 70, at the
    previous close; the 31st session regardless);
  * kept holdings are resized in full at D; pending tranches are cancelled at
    the next decision date, and by a forced exit.

Outputs (--out): nav.csv, decisions.csv, holdings.csv, fills.csv, a1.json,
universe.csv, ranks.csv, plus bs_diag.csv, t4t_events.csv and metrics.json.

HOLDOUT LOCK
As in the v1 checker: every price or return read by the simulator passes
through _guard(), which raises after the in-sample end (the Feb 2023 decision
date, marked at its OPEN) unless --run-final-test is given, and the price,
corporate-action, dividend and index data are truncated at load time. The flag
is for the owner's one full-period run only. This checker never writes to
state/trials.jsonl.
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

OUT: Path | None = None                              # set from --out / --variant
DUCK_TMP = ROOT / "data" / "cache" / "duck"          # git-ignored spill directory

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

# v2 item 1 / Clarification before the v2 build: twelve names, hold band top 24
N = 12
BUFFER = 24
SECTOR_CAP = 3
COST = 0.0020
EXEC_WAIT = 5                                # sessions a buy/sell may wait (v1 Clarification 10)
FORCED_EXIT_SESSIONS = 20
START_CASH = 500_000.0                       # Rs 5 lakh (v1 portfolio section)

# v2 item 3 / Clarification before the v2 build: hard filters (universe rules)
MIN_MCAP = 100 * 1e7                         # Rs 100 crore, in rupees
MAX_PE = 70.0
MIN_ROCE = 0.10
MAX_DE = 1.5
ROCE_ANCHOR = (2023, 2, 15)                  # "from the February 2023 decision date"
BS_MAX_AGE_DAYS = 400                        # Clarification to A5

# v2 item 4: graded trend filter on the Nifty 500 total-return index
TREND_INDEX = "NIFTY 500"
TREND_SMA, TREND_HIGH, TREND_PERSIST, TREND_PERSIST_SHARE = 200, 252, 126, 0.60
TREND_DEPTH = 0.10
CASH_BY_COUNT = {0: 0.0, 1: 0.20, 2: 0.275, 3: 0.35}

# v2 item 5 / A1: tranches and the entry rule
TRANCHE_OFFSETS = (0, 21, 42)                # sessions after D
A1_SMA, A1_RSI = 50, 14
A1_MAX_ABOVE = 0.10                          # close no more than 10% above its 50-session mean
A1_RSI_MAX = 70.0                            # RSI below 70
A1_WINDOW = 30                               # sessions checked; the 31st buys regardless

# v2 item 6: sizing bounds as multiples of (1 - cash) / 12
W_LO, W_HI = 0.5, 1.5

MEASURES_ALL = ["margin", "roce", "stability", "rev_growth", "profit_growth",
                "earnings_yield", "sales_yield", "mom_12_1", "low_vol"]


def groups_at(roce_on: bool) -> dict:
    """The five equal-weight groups (v1), quality = ROCE + Stability from the
    February 2023 decision date, Margin + Stability before it."""
    return {"quality": ["roce" if roce_on else "margin", "stability"],
            "growth": ["rev_growth", "profit_growth"],
            "value": ["earnings_yield", "sales_yield"],
            "momentum": ["mom_12_1"],
            "risk": ["low_vol"]}


GROUP_NAMES = list(groups_at(False))

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

# Clarification 38: the income-statement figures that make a filing "report an
# income statement", and the per-quarter fields taken from that filing. The
# implied share count is profit / EPS, an income-statement figure itself.
IS_FIGURES = ("revenue", "total_income", "other_income", "employee_cost", "ebitda",
              "profit_normalised")
IS_CARRY = ["revenue", "total_income", "employee_cost", "ebitda", "profit_normalised",
            "shares_implied",
            # v2 (Clarification to A5): EBIT's inputs, from the same income-statement revision
            "pbt_before_exceptional", "finance_costs", "pbt", "exceptional_items"]
# balance-sheet fields (chosen separately from the income statement, Clarification to A5)
BS_FIELDS = ["equity", "equity_capital", "assets", "debt_long", "debt_short"]
QUARTER_MAX_DAYS = 100

# Benchmarks (Clarification 17/32 and the v2-spec Clarification to A4). NAV
# column -> (name in nifty_tri.parquet, price index in data/indices). The broad
# indices take their open from data/indices; a factor index takes its open and
# prior close from nifty_price_endpoints.csv, and where NSE printed no open the
# parent index's overnight move stands in.
BROAD_IDX = {"nifty500": ("NIFTY 500", "Nifty 500"),
             "midcap150": ("NIFTY MIDCAP 150", "Nifty Midcap 150"),
             "smallcap250": ("NIFTY SMALLCAP 250", "Nifty Smallcap 250")}
FACTOR_IDX = {"momentum30": ("NIFTY200 MOMENTUM 30", "Nifty 200"),
              "value50": ("NIFTY500 VALUE 50", "Nifty 500"),
              "quality30": ("NIFTY200 QUALITY 30", "Nifty 200"),
              "lowvol30": ("NIFTY100 LOW VOLATILITY 30", "Nifty 100"),
              "alpha50": ("NIFTY ALPHA 50", "Nifty 500")}
NAV_COLS = ["date", "mark", "strategy", "universe_ew", *BROAD_IDX, *FACTOR_IDX,
            *[c + "_no_overnight" for c in FACTOR_IDX]]

FULL_END: pd.Timestamp | None = None
ROCE_FROM: pd.Timestamp | None = None
LATEST_NAMES = {}
QUARTER_RULE_INFO: dict = {}
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
    DUCK_TMP.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute(f"SET temp_directory='{DUCK_TMP.as_posix()}'")
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


def load_bz(con, end: pd.Timestamp) -> pd.DataFrame:
    """NSE's trade-for-trade series BZ (the source of the prices_other table),
    read only for the v2-spec Clarification to A2's trade-for-trade rule. Only
    BZ counts. The END session is marked at its OPEN: its close is not loaded."""
    b = con.execute(f"""
        SELECT CAST(date AS DATE) AS date, symbol, isin_code AS isin, open,
               CASE WHEN CAST(date AS DATE) < DATE '{end.date()}' THEN close END AS close,
               turnover
        FROM read_parquet('{(ROOT / 'data/daily_other').as_posix()}/**/*.parquet', union_by_name=true)
        WHERE series = 'BZ' AND date >= '{PRICE_START}' AND CAST(date AS DATE) <= DATE '{end.date()}'
        """).df()
    b["date"] = pd.to_datetime(b["date"])
    assert b["date"].max() <= end
    # a sale on a BZ session is made at its open, so every BZ row must carry one
    assert b["open"].notna().all() and (b["open"] > 0).all(), "BZ row without an open"
    return b


def map_bz(b: pd.DataFrame, spells: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Stock id for each BZ row: its (symbol, ISIN) spell when that spell traded
    in EQ/BE, else the one stock id carrying that ISIN, else the symbol's spell
    by date (as for every other symbol-dated input)."""
    exact = spells.drop_duplicates(["symbol", "isin"]).set_index(["symbol", "isin"])["cid"]
    cid = pd.Series(exact.reindex(pd.MultiIndex.from_frame(b[["symbol", "isin"]])).values, index=b.index)
    n_exact = int(cid.notna().sum())
    per_isin = spells.groupby("isin")["cid"].nunique()
    isin1 = spells[spells["isin"].isin(per_isin.index[per_isin == 1])].drop_duplicates("isin") \
        .set_index("isin")["cid"]
    miss = cid.isna()
    cid[miss] = b.loc[miss, "isin"].map(isin1)
    n_isin = int((miss & cid.notna()).sum())
    miss = cid.isna()
    known_sym = b["symbol"].isin(set(spells["symbol"]))
    if (miss & known_sym).any():
        cid[miss & known_sym] = map_symbol_dates(b[miss & known_sym], "symbol", "date", spells)
    n_sym = int((miss & cid.notna()).sum())
    out = b.assign(cid=cid.values).dropna(subset=["cid"])
    return out, {"bz_rows": int(len(b)), "by_symbol_isin": n_exact, "by_isin": n_isin,
                 "by_symbol_date": n_sym, "unmapped": int(len(b) - len(out))}


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
                 ca: pd.DataFrame, bz: pd.DataFrame | None = None):
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
        # (EQ/BE only: the universe, the measures and market cap use this)
        self.mark = adj_ff / self.C
        del adj_ff

        # --- trade-for-trade (v2-spec Clarification to A2). A BZ session is a
        # session with a BZ trade and NO EQ/BE trade for the stock id. Such a
        # session is not a no-trade session, a holding is valued at its BZ
        # close, and a sale may be made at its BZ open. Buying never uses it.
        self.bz_traded = np.zeros((self.T, self.K), bool)
        self.bz_open = np.full((self.T, self.K), np.nan)
        hclose = self.close.copy()
        self.bz_info = {"bz_sessions": 0}
        if bz is not None and len(bz):
            b = bz[bz["cid"].isin(self.cids) & bz["date"].isin(sessions)]
            b = b.sort_values(["turnover", "symbol", "isin"], ascending=[False, True, True]) \
                .drop_duplicates(["cid", "date"])
            bri = sessions.get_indexer(b["date"])
            bci = self.cids.get_indexer(b["cid"])
            keep = ~self.traded[bri, bci]            # an EQ/BE trade that session wins
            bri, bci, b = bri[keep], bci[keep], b[keep]
            self.bz_traded[bri, bci] = True
            self.bz_open[bri, bci] = b["open"].values
            hclose[bri, bci] = b["close"].values     # NaN at END (open only)
            self.bz_info = {"bz_sessions": int(keep.sum()),
                            "bz_rows_same_session_as_eq": int((~keep).sum()),
                            "bz_stocks": int(len(np.unique(bci)))}
        # holding mark: last close in EQ/BE or, on BZ sessions, BZ
        self.hadj = hclose * self.C
        self.hmark = pd.DataFrame(self.hadj).ffill().values / self.C
        del hclose

        # stale counters: consecutive sessions without a trade (0 on a trading
        # day). `stale` counts BZ sessions as trading (the rule); `stale_eq`
        # counts EQ/BE only (the rule before the re-run, for the effect report)
        def _count(tr):
            cnt = np.zeros((self.T, self.K), dtype=np.int32)
            run = np.zeros(self.K, dtype=np.int32)
            for t in range(self.T):
                run = np.where(tr[t], 0, run + 1)
                cnt[t] = run
            return cnt
        self.stale_eq = _count(self.traded)
        self.stale = _count(self.traded | self.bz_traded)
        self.dps = np.zeros((self.T, self.K))

    def add_dividends(self, dv: pd.DataFrame) -> None:
        ti = self.sessions.searchsorted(dv["ex_date"])
        ki = self.cids.get_indexer(dv["cid"])
        ok = (ti < self.T) & (ki >= 0)
        self.dropped_dividends = []
        self.dividend_check_bz_diff = []
        for t, k, a in zip(ti[ok], ki[ok], dv["dps"].values[ok]):
            # Clarification 13: more than half the prior close (in ex-date share
            # units) is a parse error and is dropped. The prior close is the
            # holding's last close, which on a BZ session is the BZ close.
            prior = self.hmark[t - 1, k] * self.F[t, k] if t > 0 else np.nan
            prior_eq = self.mark[t - 1, k] * self.F[t, k] if t > 0 else np.nan
            if (np.isfinite(prior) and a > 0.5 * prior) != (np.isfinite(prior_eq) and a > 0.5 * prior_eq):
                self.dividend_check_bz_diff.append((str(self.sessions[t].date()), self.cids[k], a))
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
                 other_income, quarter_span_days,
                 -- v2 (Clarification to A5): EBIT's inputs and the balance sheet
                 pbt_before_exceptional, finance_costs, pbt, exceptional_items,
                 equity, equity_capital, assets, debt_long, debt_short,
                 -- a filing "has a balance sheet" when it reports equity and total
                 -- assets (the archive's own has_balance_sheet flag, re-derived here
                 -- and checked against it below)
                 (equity IS NOT NULL AND assets IS NOT NULL) AS has_bs,
                 coalesce(has_balance_sheet, false) AS has_bs_archive,
                 -- Clarification 38: the filing reports an income statement for
                 -- the quarter when it carries at least one of these figures
                 ({' OR '.join(c + ' IS NOT NULL' for c in IS_FIGURES)}) AS has_is,
                 regexp_extract(xbrl_url, '{TAXONOMY_RX}', 1) AS taxonomy,
                 -- a document stored twice under two period ends is one filing; its
                 -- period is the LATEST of the two (spec Clarification 22)
                 row_number() OVER (PARTITION BY xbrl_url
                                    ORDER BY period_end DESC, broadcast_dt DESC) rn
          FROM read_parquet([{files}], union_by_name=true)
          WHERE xbrl_url IS NOT NULL) WHERE rn = 1""").df()
    f["period_end"] = pd.to_datetime(f["period_end"])
    f["broadcast_dt"] = pd.to_datetime(f["broadcast_dt"])
    # Clarification 38's quarter rule is applied by the parser: an income-
    # statement figure is stored only from a period of at most 100 days (an
    # undated OneD period counts as the quarter). Check the files obey it: no
    # income-statement figure may sit on a filing whose labelled span is longer.
    span = f["quarter_span_days"]
    long_is = f["has_is"] & span.notna() & (span > QUARTER_MAX_DAYS)
    assert not long_is.any(), f"{int(long_is.sum())} filings carry income figures from a span > 100 days"
    QUARTER_RULE_INFO.clear()
    QUARTER_RULE_INFO.update({
        "filings": int(len(f)), "no_income_statement": int((~f["has_is"]).sum()),
        "no_income_statement_with_shares_implied": int((~f["has_is"] & f["shares_implied"].notna()).sum()),
        "dated_span_days_min_max": [None if span.dropna().empty else int(span.min()),
                                    None if span.dropna().empty else int(span.max())],
        "income_from_span_over_100_days": int(long_is.sum())})
    assert (f["has_bs"] == f["has_bs_archive"]).all(), "balance-sheet flag differs from the archive's"
    QUARTER_RULE_INFO["filings_with_balance_sheet"] = int(f["has_bs"].sum())
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


def load_nse_sectors(spells: pd.DataFrame) -> tuple[pd.Series, pd.Series, dict]:
    """v2 item 7 and the Clarification before the v2 build: the sector level of
    NSE's four-level classification (the `sector` column of
    data/reference/industry_nse.parquet, keyed by TODAY's symbol and ISIN),
    joined by stock id so a renamed company keeps its label. Today's labels
    applied to the past (disclosed). A row with no sector labels nothing.

    Join: the (symbol, ISIN) spell when the archive has it; else the one stock
    id that carries the ISIN; else the symbol's most recently started spell
    (the symbol is today's). Returns cid -> sector, cid -> label_scheme, info."""
    r = pd.read_parquet(ROOT / "data/reference/industry_nse.parquet",
                        columns=["symbol", "isin", "sector", "label_scheme"])
    r = r[r["sector"].notna() & (r["sector"].astype(str).str.strip() != "")].copy()
    exact = spells.drop_duplicates(["symbol", "isin"]).set_index(["symbol", "isin"])["cid"]
    cid = pd.Series(exact.reindex(pd.MultiIndex.from_frame(r[["symbol", "isin"]])).values, index=r.index)
    n_exact = int(cid.notna().sum())
    per_isin = spells.groupby("isin")["cid"].nunique()
    isin1 = spells[spells["isin"].isin(per_isin.index[per_isin == 1])].drop_duplicates("isin") \
        .set_index("isin")["cid"]
    miss = cid.isna()
    cid[miss] = r.loc[miss, "isin"].map(isin1)
    n_isin = int((miss & cid.notna()).sum())
    miss = cid.isna()
    latest_spell = spells.sort_values(["min", "max"]).drop_duplicates("symbol", keep="last") \
        .set_index("symbol")["cid"]
    cid[miss] = r.loc[miss, "symbol"].map(latest_spell)
    n_sym = int((miss & cid.notna()).sum())
    r["cid"] = cid
    unmapped = r[r["cid"].isna()]["symbol"].tolist()
    r = r.dropna(subset=["cid"])
    # one label per stock id: a 'current'-scheme row wins over a 'legacy' one,
    # then the row whose symbol is the stock id's own (latest) symbol
    r["_cur"] = (r["label_scheme"] == "current").astype(int)
    r["_own"] = (r["symbol"] == r["cid"]).astype(int)
    r = r.sort_values(["cid", "_cur", "_own", "symbol"])
    conflicts = {c: sorted(set(g["sector"])) for c, g in r.groupby("cid") if g["sector"].nunique() > 1}
    last = r.drop_duplicates("cid", keep="last").set_index("cid")
    info = {"rows_labelled": int(len(cid)), "by_symbol_isin": n_exact, "by_isin": n_isin,
            "by_symbol": n_sym, "unmapped_symbols": unmapped, "stock_ids_labelled": int(len(last)),
            "stock_ids_with_conflicting_labels": conflicts,
            "legacy_scheme_stock_ids": sorted(last.index[last["label_scheme"] != "current"].tolist())}
    return last["sector"], last["label_scheme"], info


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


def quarter_ebit(g: pd.DataFrame) -> pd.Series:
    """Clarification to A5 (the owner's standard ROCE): a quarter's EBIT is profit
    before exceptional items and tax plus finance costs. Where the filing gives
    no pre-exceptional profit, profit before tax less exceptional items stands
    in. Other income stays in; exceptional items stay out. A quarter with no
    finance-costs line has no EBIT (Clarification after the in-sample engine
    comparison: missing, not 0), and with neither profit line the quarter's
    EBIT is missing too; either way the TTM sum is missing (v1 Clarification 5)."""
    pre = g["pbt_before_exceptional"].astype(float)
    alt = g["pbt"].astype(float) - g["exceptional_items"].astype(float).fillna(0.0)
    base = pre.where(pre.notna(), alt)
    return base + g["finance_costs"].astype(float)


def _bs_scale_breaks(b: pd.DataFrame) -> pd.Series:
    """Clarification to A5: a balance sheet on a different unit scale from the
    company's other balance sheets -- total assets and paid-up share capital
    both 30x or more out, in the same direction, against more than half of its
    (up to) 8 nearest other balance sheets of the same stock and basis known at
    D (the band and majority rule of v1 Clarification 30a). Only balance sheets
    with positive assets and share capital are compared. A balance-sheet
    period is its exact period end (Clarification after the in-sample engine
    comparison), so "nearest" is by days between period ends, ties to the
    earlier period. b: one row per (cid, consolidated, period_end). Returns a
    boolean Series on b's index."""
    la = np.log10(b["assets"].astype(float).where(b["assets"] > 0))
    lc = np.log10(b["equity_capital"].astype(float).where(b["equity_capital"] > 0))
    bad = pd.Series(False, index=b.index)
    pday = (pd.to_datetime(b["period_end"]) - pd.Timestamp("1970-01-01")).dt.days
    work = pd.DataFrame({"cid": b["cid"], "cons": b["consolidated"], "pday": pday, "la": la, "lc": lc})
    work = work[work["la"].notna() & work["lc"].notna()]
    for _, g in work.groupby(["cid", "cons"], sort=False):
        if len(g) < 2:
            continue
        va, vc, qs = g["la"].values, g["lc"].values, g["pday"].values
        for i in range(len(g)):
            others = [j for j in range(len(g)) if j != i]
            others.sort(key=lambda j: (abs(qs[j] - qs[i]), qs[j]))
            others = others[:SCALE_NEIGH]
            nb = 0
            for j in others:
                da, dc = va[i] - va[j], vc[i] - vc[j]
                if abs(da) >= LOG_BAND and abs(dc) >= LOG_BAND and np.sign(da) == np.sign(dc):
                    nb += 1
            if others and 2 * nb > len(others):
                bad[g.index[i]] = True
    return bad


def balance_sheets_at(fa: pd.DataFrame, D, set_aside: set, use_cons: pd.Series,
                      diag: dict | None = None) -> pd.DataFrame:
    """Clarification to A5, "Which balance sheet": chosen separately from the
    income statement. Candidates are filings broadcast before D that carry a
    balance sheet (equity and total assets), including filings with no income
    statement and periods after the latest income quarter, less the filings the
    income scale screen set aside. For each (stock, basis, period) the latest
    revision is the period's balance sheet; a balance sheet off scale against
    most of its neighbours is excluded; then the latest period in the stock's
    basis (v1 Clarification 4, as chosen at D) is THE balance sheet. It is not
    usable when older than 400 days at D, when equity is not positive, or when
    equity plus debt is not positive.

    Clarification after the in-sample engine comparison: `set_aside` holds the
    (stock, basis, period end, broadcast time) of each filing the income scale
    screen set aside, and a filing is matched on those four, not by its URL; a
    balance-sheet period is its exact period end, not the calendar quarter.

    fa is already sorted by (broadcast_dt, xbrl_url), so keep='last' is the
    latest revision. Returns one row per stock id that has a balance sheet in
    its basis, with ROCE's capital, debt / equity and the reason it is unusable."""
    key = ["cid", "consolidated", "period_end"]
    b = fa[fa["has_bs"]]
    fkey = [(c, bool(k), pd.Timestamp(pe), pd.Timestamp(bd)) for c, k, pe, bd in
            zip(b["cid"], b["consolidated"], b["period_end"], b["broadcast_dt"])]
    aside = np.array([x in set_aside for x in fkey], dtype=bool)
    n_set_aside = int(aside.sum())
    b = b[~aside]
    b = b.drop_duplicates(key, keep="last")
    brk = _bs_scale_breaks(b)
    if diag is not None:
        diag["bs_filings_set_aside_by_income_screen"] = n_set_aside
        diag["bs_off_scale"] = b.loc[brk, ["cid", "consolidated", "period_end", "xbrl_url"]]             .astype(str).values.tolist()
    b = b[~brk]
    b = b[b["consolidated"] == b["cid"].map(use_cons)]
    b = b.sort_values(["cid", "period_end"]).drop_duplicates("cid", keep="last").set_index("cid")
    out = pd.DataFrame(index=b.index)
    out["bs_period_end"] = b["period_end"]
    out["bs_broadcast_dt"] = b["broadcast_dt"]
    out["bs_url"] = b["xbrl_url"]
    out["bs_has_is"] = b["has_is"]
    out["equity"] = b["equity"].astype(float)
    # "a balance sheet with no borrowings line has no debt"
    out["debt"] = b["debt_long"].astype(float).fillna(0.0) + b["debt_short"].astype(float).fillna(0.0)
    out["bs_age_days"] = (pd.Timestamp(D) - b["period_end"]).dt.days
    old = out["bs_age_days"] > BS_MAX_AGE_DAYS
    neg = ~(out["equity"] > 0)
    # Clarification after the in-sample engine comparison: a usable balance
    # sheet has equity above zero AND equity plus debt above zero
    negcap = ~(out["equity"] + out["debt"] > 0)
    out["bs_reason"] = np.where(old, "older_than_400_days",
                                np.where(neg, "equity_not_positive",
                                         np.where(negcap, "capital_not_positive", "ok")))
    out["bs_usable"] = ~old & ~neg & ~negcap
    return out


def fundamentals_at(fin: pd.DataFrame, D, ca: pd.DataFrame | None = None,
                    diag: dict | None = None) -> pd.DataFrame:
    fa = fin[fin["broadcast_dt"] < D].sort_values(["broadcast_dt", "xbrl_url"])
    key = ["cid", "consolidated", "qn"]
    # For the lender vote a quarter is known from any filing for it; its latest
    # revision (latest broadcast) carries the filing format. Everything built
    # from the income statement uses only quarters that have one (C39, below).
    f = fa.drop_duplicates(key, keep="last")
    # Clarification 38: the income-statement figures (and the implied share
    # count, which is profit / EPS) come from the latest filing broadcast before
    # D that reports at least one income-statement figure. A later filing with
    # no income statement is not a revision of it. v1 uses no balance-sheet
    # figure, so nothing else is chosen from the filings.
    fi = fa[fa["has_is"]].drop_duplicates(key, keep="last")
    fi = fi[key + IS_CARRY + ["broadcast_dt", "xbrl_url", "period_end"]].rename(
        columns={"broadcast_dt": "is_bdt", "xbrl_url": "is_url", "period_end": "is_pe"})
    f = f.drop(columns=IS_CARRY).merge(fi, on=key, how="left")
    c38 = f["is_url"].notna() & (f["is_url"] != f["xbrl_url"])     # an earlier revision's income statement
    # lender-taxonomy votes over the latest 4 known quarters (Clarification 28)
    lqn = f.groupby("cid")["qn"].transform("max")
    w = f[(f["qn"] > lqn - 12) & (f["broadcast_dt"] >= NBFC_TAX_START)]
    votes = w.assign(l=w["taxonomy"].isin(LENDER_TAX)).groupby("cid")["l"].agg(["sum", "count"])
    # Clarification 39: a (stock, basis, quarter) for which no filing broadcast
    # before D reports an income statement is not a known quarter for anything
    # built from the income statement -- the data screens of 30 (which compare
    # revenue, share count and employee cost), the basis choice (4), the run and
    # stability (5), rule 6's TTM sums, growth and the share count (profit / EPS).
    # The stock is ranked on its latest quarters that have one. The lender vote
    # above reads the filing format, not the income statement, so it still
    # counts every known quarter.
    no_is = f["is_url"].isna()
    if diag is not None:
        lat_any = f.groupby("cid")["qn"].max()
        lat_is = f[~no_is].groupby("cid")["qn"].max().reindex(lat_any.index)
        back = lat_any[lat_is.isna() | (lat_is < lat_any)]
        diag["c38_income_from_earlier_revision"] = int(c38.sum())
        diag["c38_quarters_without_income_statement"] = int(no_is.sum())
        diag["c38_cases"] = f.loc[c38, ["cid", "consolidated", "period_end", "xbrl_url", "is_url"]] \
            .astype(str).values.tolist()
        diag["c39_quarters_not_known"] = int(no_is.sum())
        diag["c39_stocks_latest_quarter_has_no_income_statement"] = int(len(back))
        diag["c39_stocks_with_no_quarter_left"] = int(lat_is.isna().sum())
    f = f[~no_is]
    # mis-scaled filings count as not filed (Clarification 30a), judged on the
    # income statement each quarter uses
    brk = _scale_breaks(f)
    if diag is not None:
        diag["c38_screened_earlier_revision"] = int((brk & c38.reindex(f.index)).sum())
    # the filings the scale screen set aside: the filings whose income
    # statement each screened quarter used (Clarification to A5: their balance
    # sheets are excluded too), identified by (stock, basis, period end,
    # broadcast time), not by URL (Clarification after the in-sample engine
    # comparison)
    sa_rows = f.loc[brk & f["is_url"].notna()]
    set_aside = {(c, bool(k), pd.Timestamp(pe), pd.Timestamp(bd)) for c, k, pe, bd in
                 zip(sa_rows["cid"], sa_rows["consolidated"], sa_rows["is_pe"], sa_rows["is_bdt"])}
    f = f[~brk]
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
    # the count's own filing: the one its income statement came from (C38)
    sh_bdt = sa["is_bdt"].combine_first(co["is_bdt"])
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
                ev = ev_all[(ev_all["ex_date"] > pd.Timestamp(r.is_bdt).normalize())
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
            sh[c], sh_bdt[c], sh_fix[c] = alt.at[c, "shares_implied"], alt.at[c, "is_bdt"], "other_basis"
        else:
            sh[c], sh_fix[c] = ref, "carried_reference"
    bs = balance_sheets_at(fa, D, set_aside, use_cons, diag)
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
            # Clarification to A5: TTM EBIT over the same four quarters, each from
            # the income-statement revision kept above
            r["ttm_ebit"] = quarter_ebit(top).sum(min_count=4)
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
            "ttm_ebitda", "ttm_pn", "ttm_ebit", "stability", "rev_growth", "pn_delta"]
    out = pd.DataFrame(rows).set_index("cid").reindex(columns=cols)
    out = out.join(bs, how="left")
    out["shares_implied"] = sh.reindex(out.index)
    out["shares_bdt"] = sh_bdt.reindex(out.index)
    out["shares_fix"] = sh_fix.reindex(out.index).fillna("")
    out["tax_new"] = votes["count"].reindex(out.index).fillna(0)
    out["tax_lender"] = votes["sum"].reindex(out.index).fillna(0)
    return out


def universe_and_measures(D, P: Panels, fin, lab, names, taxfill, ca, nse_sector: pd.Series,
                          diag: dict | None = None) -> pd.DataFrame:
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
    # --- rule 4: lenders / insurers (v1 unchanged: the announcements labels)
    sec = sector_at(lab, D)
    df["v1_label"] = sec.reindex(df.index)
    lab_lender = df["v1_label"].fillna("").str.contains(LENDER_LABEL)
    # Clarification 29 read literally: each stock's LATEST name only.
    _ln = LATEST_NAMES
    name_lender = pd.Series([bool(LENDER_NAME.search(_ln.get(c, "") or "")) for c in df.index], index=df.index)
    # Names are stored as at download (Clarification 29): the name net applies
    # only to stocks with no industry label at all.
    name_lender &= df["v1_label"].isna()
    # --- rules 5-6: fundamentals
    fu = fundamentals_at(fin, D, ca, diag)
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
    df["v1_univ"] = df[["r1", "r2", "r3", "r4", "r5", "r6", "r7"]].all(axis=1)

    # --- v2 hard filters (item 3; Clarification before the v2 build), universe rules
    # trailing P/E = market cap / TTM normalised profit, positive and at most 70
    df["pe"] = df["mcap"] / df["ttm_pn"]
    df["hf_mcap"] = df["mcap"] > MIN_MCAP
    df["hf_pe"] = (df["pe"] > 0) & (df["pe"] <= MAX_PE)
    # ROCE and debt / equity from the balance sheet the Clarification to A5 picks
    has_bs = df["bs_usable"].fillna(False).astype(bool)
    cap = df["equity"] + df["debt"]
    df["roce_value"] = (df["ttm_ebit"] / cap.where(cap > 0)).where(has_bs)
    df["de_value"] = (df["debt"] / df["equity"]).where(has_bs)
    df["bs_reason"] = df["bs_reason"].where(df["bs_reason"].notna(), "none_known")
    roce_on = ROCE_FROM is not None and pd.Timestamp(D) >= ROCE_FROM
    df["roce_on"] = roce_on
    if roce_on:
        # no usable balance sheet (none known, older than 400 days, equity not
        # positive) fails both; a ROCE that cannot be computed fails the floor
        df["hf_bs"] = has_bs
        df["hf_roce"] = has_bs & (df["roce_value"] >= MIN_ROCE)
        df["hf_de"] = has_bs & (df["de_value"] < MAX_DE)
    else:
        df["hf_bs"] = df["hf_roce"] = df["hf_de"] = True
    df["hard_ok"] = df[["hf_mcap", "hf_pe", "hf_roce", "hf_de"]].all(axis=1)
    df["in_univ"] = df["v1_univ"] & df["hard_ok"]
    # the cap's sector: NSE's sector level, by stock id (item 7)
    df["sector"] = nse_sector.reindex(df.index)

    # --- measures (computed for everyone, ranked within the universe only)
    df["margin"] = df["ttm_ebitda"] / df["ttm_rev"].where(df["ttm_rev"] > 0)
    df["profit_growth"] = df["pn_delta"] / df["ttm_rev"].where(df["ttm_rev"] > 0)
    df["earnings_yield"] = df["ttm_pn"] / df["mcap"]
    df["sales_yield"] = df["ttm_rev"] / df["mcap"]
    # ROCE is a measure only from the February 2023 decision date (item 2)
    df["roce"] = df["roce_value"] if roce_on else np.nan
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
    """v1's scoring on the v2 universe: each measure a percentile within the
    universe (missing 0.5), group = mean of its measures, composite = mean of the
    five groups, ties by the symbol traded at D then the id. Quality is ROCE and
    Stability from the February 2023 decision date, Margin and Stability before."""
    u = df[df["in_univ"]].copy()
    roce_on = bool(df["roce_on"].iloc[0]) if len(df) else False
    groups = groups_at(roce_on)
    used = [m for ms in groups.values() for m in ms]
    for m in MEASURES_ALL:
        if m in used:
            v = u[m].replace([np.inf, -np.inf], np.nan)
            u[m + "_pct"] = v.rank(pct=True, method="average").fillna(0.5)
        else:
            u[m + "_pct"] = np.nan
    for g, ms in groups.items():
        u[g] = u[[m + "_pct" for m in ms]].mean(axis=1)
    u["composite"] = u[list(groups)].mean(axis=1)
    u["quality_basis"] = "roce" if roce_on else "margin"
    u = u.rename_axis("cid").sort_values(["composite", "symbol", "cid"], ascending=[False, True, True])
    u["rank"] = np.arange(1, len(u) + 1)
    return u


# ================================================================ simulator
class Book:
    """Share-count simulator. With bz=True (the rule for the re-run) a BZ
    session counts as trading for the no-trade exit, a holding is valued at the
    BZ close, and a sale or trim may fill at the BZ open; buys need an EQ/BE
    trade. bz=False is the rule before the re-run, kept only to report the
    trade-for-trade rule's effect.

    The book starts with `start` rupees in cash: Rs 5 lakh, like the strategy
    (Clarification after the in-sample engine comparison: the equal-weight
    benchmark's NAV starts at Rs 5 lakh, as in v1). The rupee tolerance below
    which a difference is not traded scales with it (1e-12 of the start)."""

    def __init__(self, P: Panels, cost: float, exit_mult: float = 1.0, bz: bool = True,
                 name: str = "", start: float = START_CASH):
        self.P, self.cost, self.exit_mult, self.bz, self.name = P, cost, exit_mult, bz, name
        self.sh = np.zeros(P.K)
        self.start = float(start)
        self.cash = float(start)
        self.tol = 1e-12 * float(start)
        self.pending: dict[int, tuple[float, int]] = {}
        self.trades: list[tuple] = []
        self.divs = 0.0
        self.costs = 0.0
        self.forced: list[tuple] = []
        self.open_spells: dict[int, tuple[int, float]] = {}
        self.closed_spells: list[tuple] = []
        self.cmark = P.hmark if bz else P.mark        # close mark of a holding
        self.cadj = P.hadj if bz else P.adj
        self.stale = P.stale if bz else P.stale_eq
        self.t4t: list[tuple] = []                     # (t, k, event)

    # --- prices
    def _px(self, t, k) -> float:
        """Execution price at the open of t for an executable order."""
        P = self.P
        return P.open[t, k] if P.traded[t, k] else P.bz_open[t, k]

    def _val(self, t) -> np.ndarray:
        """Holding value per share at the open of t: the EQ/BE open, else (rule
        on) the BZ open, else the last close."""
        P = self.P
        if self.bz:
            return np.where(P.traded[t], P.open[t], np.where(P.bz_traded[t], P.bz_open[t], self.cmark[t]))
        return np.where(P.traded[t], P.open[t], P.mark[t])

    def _can(self, t, k, tgt) -> bool:
        """Can the order for k fill at the open of t? Any order on an EQ/BE
        session; with the rule on, a sale or trim on a BZ session."""
        P = self.P
        if P.traded[t, k]:
            return True
        return bool(self.bz and P.bz_traded[t, k] and tgt < self.sh[k] * P.bz_open[t, k] - self.tol)

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
        cannot cover every buy. Every order passed here is executable (_can)."""
        P = self.P
        buys = []
        for k, tgt in orders.items():
            px = self._px(t, k)
            cur = self.sh[k] * px
            if tgt < cur - self.tol:
                if not P.traded[t, k]:
                    self.t4t.append((t, k, "sale_at_bz_open"))
                self._exec(t, k, px, tgt)
            elif tgt > cur + self.tol:
                assert P.traded[t, k], "a buy must fill on an EQ/BE session"
                buys.append((k, tgt - cur))
        need = sum(d for _, d in buys) * (1 + self.cost)
        scale = min(1.0, self.cash / need) if need > 0 else 0.0
        for k, d in buys:
            if d * scale > 0:
                self._exec(t, k, P.open[t, k], self.sh[k] * P.open[t, k] + d * scale)

    def rebalance(self, t, targets: np.ndarray, slots: int):
        P = self.P
        self.pending.clear()
        ref = self._val(t)
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
            if self._can(t, k, tgt):
                now[k] = tgt
            else:
                self.pending[k] = (tgt, t + EXEC_WAIT)
        self._fill(t, now)

    def run(self, plan: dict[int, tuple[np.ndarray, int]], t0: int, t_end: int) -> pd.DataFrame:
        P = self.P
        out = [(P.sessions[t0], "open", self.cash)]
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
                px = self._val(t)
                held = self.sh > 0
                if self.bz:
                    for k in np.flatnonzero(held & P.bz_traded[t]):
                        self.t4t.append((t, k, "end_valued_at_bz_open"))
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
                    if self._can(t, k, tgt):
                        now[k] = tgt
                        del self.pending[k]
                    elif t >= dl:
                        del self.pending[k]
                self._fill(t, now)
            # 4. forced exit after 20 sessions without a trade, at the last close
            stale = np.flatnonzero((self.sh > 0) & (self.stale[t] >= FORCED_EXIT_SESSIONS))
            for k in stale:
                self._exec(t, k, self.cmark[t, k] * self.exit_mult, 0.0)
                self.pending.pop(k, None)
                self.forced.append((P.sessions[t], P.cids[k]))
            # 5. mark at the close
            held = self.sh > 0
            if self.bz:
                for k in np.flatnonzero(held & P.bz_traded[t]):
                    self.t4t.append((t, k, "valued_at_bz_close"))
                for k in np.flatnonzero(held & (P.stale_eq[t] >= FORCED_EXIT_SESSIONS)):
                    self.t4t.append((t, k, "eq_only_rule_would_have_force_sold"))
            out.append((P.sessions[t], "close", self.cash + np.nansum(self.sh[held] * self.cmark[t][held])))
        return pd.DataFrame(out, columns=["date", "mark", "nav"])

    def spells(self, t_end) -> pd.DataFrame:
        """Holding spells with adjusted entry/exit prices (price return only)."""
        P = self.P
        rows = list(self.closed_spells)
        val = self._val(t_end)
        for k, (t0, e) in self.open_spells.items():
            rows.append((P.cids[k], P.sessions[t0], P.sessions[t_end], val[k] * P.C[t_end, k] / e, True, e))
        sp = pd.DataFrame(rows, columns=["cid", "entry", "exit", "gross", "open_at_end", "entry_adj"])
        # Clarification 18: doubled = an adjusted CLOSE during the episode reached
        # twice the (adjusted) entry price (the holding's close: BZ on BZ sessions)
        mx = []
        for r in sp.itertuples():
            k = P.cids.get_loc(r.cid)
            seg = self.cadj[P.idx(r.entry):P.idx(r.exit) + 1, k]
            mx.append(np.nanmax(seg) if np.isfinite(seg).any() else np.nan)
        sp["max_adj_close"] = mx
        return sp

    def t4t_frame(self) -> pd.DataFrame:
        P = self.P
        return pd.DataFrame([(self.name, P.sessions[t].date(), P.cids[k], ev) for t, k, ev in self.t4t],
                            columns=["book", "date", "cid", "event"])


# ================================================================ v2: trend, entry rule, sizing
def trend_at(tri: pd.Series, D, prev_session) -> dict:
    """Item 4 as the Clarification before the v2 build fixes it. From Nifty 500
    total-return closes up to the session before D:
      (1) the last close is below the mean of the last 200 closes;
      (2) the last close is at least 10% below the highest of the last 252;
      (3) on at least 60% of the last 126 sessions the close was below its own
          trailing 200-close mean.
    Cash is 0, 20, 27.5 or 35% of NAV at D for 0 to 3 conditions true."""
    s = tri[tri.index < pd.Timestamp(D)]
    if not len(s) or s.index[-1] != pd.Timestamp(prev_session):
        raise SystemExit(f"refusing: no Nifty 500 TRI close on {pd.Timestamp(prev_session).date()}, "
                         f"the session before {pd.Timestamp(D).date()}")
    x = s.values.astype(float)
    if len(x) < max(TREND_SMA + TREND_PERSIST - 1, TREND_HIGH):
        raise SystemExit(f"refusing: too little Nifty 500 TRI history before {pd.Timestamp(D).date()}")
    last = x[-1]
    sma = float(np.mean(x[-TREND_SMA:]))
    high = float(np.max(x[-TREND_HIGH:]))
    below = 0
    for j in range(len(x) - TREND_PERSIST, len(x)):
        if x[j] < np.mean(x[j - TREND_SMA + 1:j + 1]):
            below += 1
    c1 = bool(last < sma)
    c2 = bool(last <= (1.0 - TREND_DEPTH) * high)
    c3 = bool(below >= TREND_PERSIST_SHARE * TREND_PERSIST)
    n = int(c1) + int(c2) + int(c3)
    return {"D": pd.Timestamp(D), "tri_date": s.index[-1], "tri_last": float(last), "tri_sma200": sma,
            "tri_high252": high, "tri_below_sma_sessions_of_126": below,
            "c1": c1, "c2": c2, "c3": c3, "n_true": n, "cash_fraction": CASH_BY_COUNT[n]}


class PreCloses:
    """EQ/BE closes on the archive's market sessions before PRICE_START (the
    archive starts 1 Jan 2015), read only to start A1's RSI at each stock's
    first EQ or BE close in the archive (Clarification after the in-sample
    engine comparison). Long form: date, cid, close; one row per (cid, date),
    chosen as Panels chooses (most turnover, then series, symbol, ISIN)."""

    def __init__(self, frame: pd.DataFrame, sessions: pd.DatetimeIndex):
        self.frame, self.sessions = frame, sessions


def load_pre_closes(con, spells: pd.DataFrame) -> PreCloses:
    q = con.execute(f"""
        SELECT CAST(date AS DATE) AS date, symbol, series, isin_code AS isin, close, turnover
        FROM read_parquet('{(ROOT / 'data/daily').as_posix()}/**/*.parquet', union_by_name=true)
        WHERE date < '{PRICE_START}' AND series IN ('EQ','BE')""").df()
    q["date"] = pd.to_datetime(q["date"])
    q = q.merge(spells[["symbol", "isin", "cid"]], on=["symbol", "isin"], how="left")
    assert q["cid"].notna().all()
    q = q.sort_values(["turnover", "series", "symbol", "isin"],
                      ascending=[False, False, True, True]).drop_duplicates(["cid", "date"])
    s = con.execute(f"SELECT DISTINCT CAST(date AS DATE) d FROM read_parquet("
                    f"'{(ROOT / 'data/daily').as_posix()}/**/*.parquet', union_by_name=true) "
                    f"WHERE date < '{PRICE_START}' ORDER BY 1").df()["d"]
    sessions = pd.DatetimeIndex(pd.to_datetime(s))
    return PreCloses(q[["date", "cid", "close"]].reset_index(drop=True), sessions)


def pre_adjusted_closes(P: Panels, pre: PreCloses) -> np.ndarray:
    """(pre sessions x P.K) EQ/BE closes before the panel, NaN on a session
    without one, adjusted for splits and bonuses into P's basis: each close is
    multiplied by every factor of P's corporate-action frame whose ex-session
    is after it, i.e. those on later pre-panel sessions and on the panel's first
    session (ex-date after the last pre-panel session, up to the first panel
    session), and by P.C[0] for the rest."""
    fr = pre.frame[pre.frame["cid"].isin(P.cids)]
    Tp = len(pre.sessions)
    X = np.full((Tp, P.K), np.nan)
    ri = pre.sessions.get_indexer(fr["date"])
    ci = P.cids.get_indexer(fr["cid"])
    assert (ri >= 0).all() and (ci >= 0).all()
    X[ri, ci] = fr["close"].values.astype(float)
    Cl = np.ones((Tp, P.K))
    ca = P.ca_sessions
    if len(ca):
        cal = pre.sessions.append(P.sessions[:1])
        e = cal.searchsorted(pd.to_datetime(ca["ex_date"]))     # first session on/after the ex-date
        ki = P.cids.get_indexer(ca["cid"])
        ok = (e <= Tp) & (ki >= 0)
        for ei, k, f in zip(e[ok], ki[ok], ca["factor"].values[ok]):
            Cl[:ei, k] *= f
    return X * Cl * P.C[0]


def a1_indicators(P: Panels, pre: PreCloses | None = None
                  ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """A1's two conditions at every session's close, on corporate-action
    adjusted EQ/BE closes. Sessions are market sessions (v1 Clarification 1): a
    session with no trade carries the stock's last adjusted close (as the
    momentum closes of Clarification 2 do), so its change is zero.
      above[t] = close[t] / mean(close[t-49..t]) - 1       (needs 50 closes)
      rsi[t]   = Wilder's 14-session RSI: the first average gain and loss are
                 the simple means of the stock's first 14 changes from its
                 first EQ or BE close in the archive (with `pre`, the closes
                 before the panel; without it, the panel's first), then
                 avg = (13 * avg + today) / 14; RSI = 100 - 100 / (1 + gain/loss),
                 100 when the average loss is 0 and the gain is not, undefined
                 (NaN, so the condition fails) when both are 0.
      ok[t]    = close[t] <= 1.10 x its 50-session mean AND rsi[t] < 70.
    A1 buys at the open of t+1 when ok[t] holds. Every value at t uses closes
    up to t only; the adjustment basis cancels in both ratios. Returned arrays
    cover the panel's sessions only."""
    if pre is not None:
        xpre = pre_adjusted_closes(P, pre)
        off = len(xpre)
        xa = pd.DataFrame(np.vstack([xpre, P.adj])).ffill().values
        del xpre
    else:
        off = 0
        xa = pd.DataFrame(P.adj).ffill().values
    x = xa[off:]
    T, K = x.shape
    sma = np.full((T, K), np.nan)
    for t in range(T):
        ta = t + off
        if ta >= A1_SMA - 1:
            sma[t] = xa[ta - A1_SMA + 1:ta + 1].mean(axis=0)     # NaN unless all 50 exist
    with np.errstate(invalid="ignore", divide="ignore"):
        above = x / sma - 1.0
    rsi = np.full((T, K), np.nan)
    cnt = np.zeros(K, dtype=np.int64)
    sg, sl = np.zeros(K), np.zeros(K)
    ag, al = np.full(K, np.nan), np.full(K, np.nan)
    n = A1_RSI
    for ta in range(1, len(xa)):
        d = xa[ta] - xa[ta - 1]
        ok = np.isfinite(d)
        g = np.where(ok, np.maximum(d, 0.0), 0.0)
        lo = np.where(ok, np.maximum(-d, 0.0), 0.0)
        cnt = cnt + ok
        seed = ok & (cnt <= n)
        sg += np.where(seed, g, 0.0)
        sl += np.where(seed, lo, 0.0)
        first = ok & (cnt == n)
        ag = np.where(first, sg / n, ag)
        al = np.where(first, sl / n, al)
        upd = ok & (cnt > n)
        ag = np.where(upd, (ag * (n - 1) + g) / n, ag)
        al = np.where(upd, (al * (n - 1) + lo) / n, al)
        if ta < off:
            continue                                       # before the panel: state only
        with np.errstate(invalid="ignore", divide="ignore"):
            r = np.where(al > 0, 100.0 - 100.0 / (1.0 + ag / al), np.where(ag > 0, 100.0, np.nan))
        rsi[ta - off] = np.where(cnt >= n, r, np.nan)
    with np.errstate(invalid="ignore"):
        a1_ok = (x <= (1.0 + A1_MAX_ABOVE) * sma) & (rsi < A1_RSI_MAX)
    return x, above, rsi, a1_ok


def size_weights(sigma, cash_frac: float, n_slots: int = N) -> tuple[np.ndarray, np.ndarray, dict]:
    """Item 6 as the Clarification before the v2 build fixes it. Weight is
    proportional to 1/sigma, scaled to sum to (1 - cash); each weight is then
    held within 0.5 and 1.5 x (1 - cash)/12, the excess or shortfall
    redistributed pro rata (to current weight) over the names not at a bound,
    until all are within it. A name at a bound stays there. A name with no
    sigma (Clarification after the in-sample engine comparison): its weight
    before scaling is (1 - cash)/12, and the others are scaled around it, i.e.
    the names with a sigma share (1 - cash) less those equal weights in
    proportion to 1/sigma; the bounds then apply to every name alike.
    With fewer than 12 names the bounds still use (1 - cash)/12 and whatever
    the capped weights leave is cash."""
    sig = np.asarray(sigma, dtype=float)
    c = 1.0 - cash_frac
    e = c / n_slots
    lo, hi = W_LO * e, W_HI * e
    if len(sig) == 0:
        return np.zeros(0), np.zeros(0, dtype=object), {"no_sigma": 0, "iterations": 0, "left_as_cash": c}
    has = np.isfinite(sig) & (sig > 0)
    inv = np.where(has, 1.0 / np.where(has, sig, 1.0), 0.0)
    w = np.full(len(sig), e)
    if has.any():
        w[has] = inv[has] / inv[has].sum() * (c - e * int((~has).sum()))
    fixed = np.zeros(len(w), dtype=bool)
    bound = np.array([""] * len(w), dtype=object)
    it = 0
    for it in range(1, 10 * len(w) + 10):
        over = ~fixed & (w > hi)
        under = ~fixed & (w < lo)
        if not (over | under).any():
            break
        excess = float((w[over] - hi).sum() - (lo - w[under]).sum())
        w[over] = hi
        w[under] = lo
        bound[over] = "upper"
        bound[under] = "lower"
        fixed |= over | under
        free = ~fixed
        if not free.any():
            break
        w[free] += excess * w[free] / w[free].sum()
    return w, bound, {"no_sigma": int((~has).sum()), "iterations": it, "left_as_cash": float(c - w.sum())}


class BookV2:
    """The v2 strategy book: a share-count simulator in the v1 checker's style
    (v1 Clarifications 10, 11, 13, 15; the trade-for-trade rule of the
    Clarification to A2) with v2's rebalance and tranches.

    Orders are 'target' (a rupee value for the whole position: kept resizes and
    sales, v1 style) or 'add' (a rupee amount to buy: a tranche). At a session's
    open the orders that can fill are executed, sells first, then buys scaled
    down pro rata if cash net of costs cannot cover them (Clarification 11).
      target  fills on an EQ/BE session, or a sale/trim on a BZ session; waits
              up to 5 sessions after its scheduled session, then is cancelled;
      add (a) fills at its session's open on an EQ/BE session, waiting up to 5
              sessions (v1's buying rule), then is cancelled;
      add (b) from its scheduled session s, fills at the open of the first
              session u in s..s+29 with an EQ/BE trade whose previous close
              met A1's two conditions; from s+30 (the 31st session) it fills
              regardless, under v1's buying rule (up to 5 more sessions).
    At a decision date every pending order is cancelled and replaced; a
    position sold to zero (forced exit included) cancels its pending orders."""

    TOL = 1e-7                                     # rupees: smaller differences are not traded

    def __init__(self, P: Panels, cost: float, variant: str, a1_ok: np.ndarray,
                 exit_mult: float = 1.0, start: float = START_CASH):
        assert variant in ("a", "b")
        self.P, self.cost, self.variant, self.a1_ok, self.exit_mult = P, cost, variant, a1_ok, exit_mult
        self.sh = np.zeros(P.K)
        self.cash = float(start)
        self.orders: list[dict] = []
        self.fills: list[dict] = []
        self.trades: list[tuple] = []
        self.divs = 0.0
        self.costs = 0.0
        self.forced: list[tuple] = []
        self.positions: dict = {}
        self.t4t: list[tuple] = []
        self.nav_open_at: dict = {}
        self.min_cash = float(start)
        self.scaled_buys: list[tuple] = []
        self.pending_at_end: list[dict] = []

    # --- prices
    def _val(self, t) -> np.ndarray:
        """Value per share at the open of t: EQ/BE open, else BZ open, else the
        holding's last close (v1 checker, trade-for-trade rule on)."""
        P = self.P
        return np.where(P.traded[t], P.open[t], np.where(P.bz_traded[t], P.bz_open[t], P.hmark[t]))

    def _can_target(self, t, k, tgt) -> bool:
        P = self.P
        if P.traded[t, k]:
            return True
        return bool(P.bz_traded[t, k] and tgt < self.sh[k] * P.bz_open[t, k] - self.TOL)

    def _ticker(self, t, k):
        col = self.P.symbol_at
        for j in range(t, -1, -1):
            v = col[j, k]
            if isinstance(v, str):
                return v
        return None

    # --- orders
    def _order(self, k, kind, mode, value, t_sched, t_D, pos=None) -> dict:
        return {"k": int(k), "kind": kind, "mode": mode, "value": float(value),
                "t_sched": int(t_sched), "t_D": int(t_D), "pos": pos}

    def _status(self, o, t) -> tuple[str, str]:
        if t < o["t_sched"]:
            return "wait", ""
        P, k = self.P, o["k"]
        if o["mode"] == "target":
            if self._can_target(t, k, o["value"]):
                return "fill", ""
            return ("cancel", "no_trade_within_5_sessions") if t >= o["t_sched"] + EXEC_WAIT else ("wait", "")
        tr = bool(P.traded[t, k])
        if self.variant == "a":
            if tr:
                return "fill", ""
            return ("cancel", "no_trade_within_5_sessions") if t >= o["t_sched"] + EXEC_WAIT else ("wait", "")
        if t < o["t_sched"] + A1_WINDOW:                  # sessions s .. s+29: A1's rule
            return ("fill", "") if (tr and t >= 1 and bool(self.a1_ok[t - 1, k])) else ("wait", "")
        if tr:                                            # the 31st session on: regardless
            return "fill", ""
        if t >= o["t_sched"] + A1_WINDOW + EXEC_WAIT:
            return "cancel", "no_trade_within_5_sessions_of_the_31st"
        return "wait", ""

    def _row(self, t, k, o, side, units_raw, price_raw, value, cost, kind, **extra) -> dict:
        P = self.P
        C = P.C[t, k]
        return {"date": P.sessions[t].date(), "symbol": P.cids[k], "ticker": self._ticker(t, k),
                "side": side, "units_adjusted": units_raw / C if units_raw else 0.0,
                "price_adjusted": price_raw * C if np.isfinite(price_raw) else np.nan,
                "value": value, "cost": cost, "kind": kind,
                "scheduled_date": P.sessions[min(o["t_sched"], P.T - 1)].date(),
                "D": P.sessions[o["t_D"]].date(), **extra}

    def _cancel(self, t, o, reason) -> None:
        k = o["k"]
        if o["mode"] == "target":
            cur = self.sh[k] * self._val(t)[k]
            side = "sell" if o["value"] < cur else "buy"
            amt = abs(o["value"] - cur)
        else:
            side, amt = "buy", o["value"]
        self.fills.append(self._row(t, k, o, side, 0.0, np.nan, amt, 0.0, "cancelled",
                                    cancelled_kind=o["kind"], reason=reason))
        if o["pos"] is not None:
            self.positions[o["pos"]]["tranches"][o["kind"]] = {
                "status": "cancelled", "date": str(self.P.sessions[t].date()), "reason": reason}

    def _exec(self, t, k, price, new_units, o) -> None:
        P = self.P
        before = self.sh[k]
        d = new_units - before
        if d == 0:
            return
        val = d * price
        c = abs(val) * self.cost
        self.cash -= val + c
        self.costs += c
        self.sh[k] = new_units
        self.min_cash = min(self.min_cash, self.cash)
        self.trades.append((t, k, d, price, val))
        kind = o["kind"]
        self.fills.append(self._row(t, k, o, "buy" if d > 0 else "sell", abs(d), price, abs(val), c, kind,
                                    cancelled_kind="",
                                    reason="bz_open" if (d < 0 and not P.traded[t, k] and kind != "forced_exit")
                                    else ""))
        if o["pos"] is not None and d > 0:
            pos = self.positions[o["pos"]]
            pos["units_adj"] += d / P.C[t, k]
            pos["value"] += val
            pos["tranches"][kind] = {"status": "filled", "date": str(P.sessions[t].date()),
                                     "sessions_after_scheduled": int(t - o["t_sched"]),
                                     "price_adjusted": float(price * P.C[t, k]), "value": float(val)}
        if new_units <= 0:
            self.sh[k] = 0.0
            # a position that leaves the book cancels its pending orders
            rest = [x for x in self.orders if x["k"] == k]
            if rest:
                self.orders = [x for x in self.orders if x["k"] != k]
                for x in rest:
                    self._cancel(t, x, "position_left_book")

    def _fill(self, t, orders: list[dict]) -> None:
        P = self.P
        buys = []
        for o in orders:
            k = o["k"]
            if o["mode"] == "target":
                px = P.open[t, k] if P.traded[t, k] else P.bz_open[t, k]
                cur = self.sh[k] * px
                if o["value"] < cur - self.TOL:
                    if not P.traded[t, k]:
                        self.t4t.append((t, k, "sale_at_bz_open"))
                    self._exec(t, k, px, o["value"] / px, o)
                elif o["value"] > cur + self.TOL:
                    assert P.traded[t, k], "a buy must fill on an EQ/BE session"
                    buys.append((o, o["value"] - cur))
            else:
                assert P.traded[t, k], "a tranche must fill on an EQ/BE session"
                buys.append((o, o["value"]))
        need = sum(d for _, d in buys) * (1 + self.cost)
        scale = min(1.0, max(self.cash, 0.0) / need) if need > 0 else 0.0
        if buys and scale < 1.0:
            self.scaled_buys.append((str(P.sessions[t].date()), float(scale)))
        for o, d in buys:
            k = o["k"]
            if d * scale > 0:
                px = P.open[t, k]
                self._exec(t, k, px, self.sh[k] + d * scale / px, o)

    def _process(self, t) -> None:
        now, keep = [], []
        for o in self.orders:
            st, why = self._status(o, t)
            if st == "fill":
                now.append(o)
            elif st == "cancel":
                self._cancel(t, o, why)
            else:
                keep.append(o)
        self.orders = keep
        if now:
            self._fill(t, now)

    def rebalance(self, t, D, decide) -> None:
        P = self.P
        for o in self.orders:
            self._cancel(t, o, "next_decision_date")
        self.orders = []
        val = self._val(t)
        held_k = np.flatnonzero(self.sh > 0)
        V = self.cash + float(np.nansum(self.sh[held_k] * val[held_k]))
        self.nav_open_at[t] = V
        held = {P.cids[k] for k in held_k}
        sel, w, status = decide(D, held, V)
        ks = P.cids.get_indexer(sel)
        assert (ks >= 0).all()
        orders = []
        for c in sorted(held - set(sel)):
            orders.append(self._order(P.cids.get_loc(c), "rebalance", "target", 0.0, t, t))
        for c, k, wi in zip(sel, ks, w):
            if status[c] == "kept":
                orders.append(self._order(k, "rebalance", "target", wi * V, t, t))
            else:
                Vi = wi * V
                pos = (pd.Timestamp(D), c)
                self.positions[pos] = {"D": pd.Timestamp(D), "cid": c, "k": int(k), "V": Vi, "weight": wi,
                                       "units_adj": 0.0, "value": 0.0, "tranches": {}}
                for j, off in enumerate(TRANCHE_OFFSETS):
                    orders.append(self._order(k, f"tranche{j + 1}", "add", Vi / len(TRANCHE_OFFSETS),
                                              t + off, t, pos))
        self.orders = orders
        self._process(t)

    def run(self, plan: dict, decide, t0: int, t_end: int) -> pd.DataFrame:
        P = self.P
        out = [(P.sessions[t0], "open", self.cash)]
        t_last_D = t0
        for t in range(t0, t_end + 1):
            _guard(P.sessions[t])
            f = P.F[t]
            chg = (f != 1) & (self.sh > 0)
            if chg.any():
                self.sh[chg] /= f[chg]
            if t > t0:
                d = float(np.nansum(self.sh * P.dps[t]))
                self.cash += d
                self.divs += d
            if t == t_end:
                px = self._val(t)
                held = self.sh > 0
                for k in np.flatnonzero(held & P.bz_traded[t]):
                    self.t4t.append((t, k, "end_valued_at_bz_open"))
                out.append((P.sessions[t], "open", self.cash + float(np.nansum(self.sh[held] * px[held]))))
                break
            if t in plan:
                t_last_D = t
                self.rebalance(t, plan[t], decide)
            elif self.orders:
                self._process(t)
            # forced exit after 20 sessions without a trade, at the last close
            for k in np.flatnonzero((self.sh > 0) & (P.stale[t] >= FORCED_EXIT_SESSIONS)):
                o = self._order(k, "forced_exit", "target", 0.0, t, t_last_D)
                self._exec(t, k, P.hmark[t, k] * self.exit_mult, 0.0, o)
                self.forced.append((P.sessions[t], P.cids[k]))
            held = self.sh > 0
            for k in np.flatnonzero(held & P.bz_traded[t]):
                self.t4t.append((t, k, "valued_at_bz_close"))
            out.append((P.sessions[t], "close", self.cash + float(np.nansum(self.sh[held] * P.hmark[t][held]))))
        self.pending_at_end = list(self.orders)
        return pd.DataFrame(out, columns=["date", "mark", "nav"])

    def t4t_frame(self, name: str) -> pd.DataFrame:
        P = self.P
        return pd.DataFrame([(name, P.sessions[t].date(), P.cids[k], ev) for t, k, ev in self.t4t],
                            columns=["book", "date", "cid", "event"])


# ================================================================ metrics
def perf(nav: pd.Series, dates: pd.Series) -> dict:
    r = nav.pct_change().dropna()
    yrs = (dates.iloc[-1] - dates.iloc[0]).days / 365.25
    # growth from the first mark (the NAVs start at Rs 5 lakh, not at 1)
    cagr = (nav.iloc[-1] / nav.iloc[0]) ** (1 / yrs) - 1
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


def benchmark_columns(con, clock: pd.DataFrame, sessions: pd.DatetimeIndex) -> tuple[pd.DataFrame, dict]:
    """Total-return benchmark levels on the NAV clock (Clarification 16/17 and
    the v2-spec Clarification to A4), each normalised to 1 at the first open.

    Closes are NSE's gross TRI (`tri`). At the two opens,
        TRI_open(d) = TRI_close(d-1) x price_open(d) / price_close(d-1),
    with the broad indices' price levels from data/indices and the factor
    indices' from nifty_price_endpoints.csv. Where NSE printed no factor open,
    the parent index's overnight move stands in; the *_no_overnight column
    then uses TRI_close(d-1) and is otherwise identical. A missing TRI close,
    price level or endpoint row raises: the engine refuses, it never guesses."""
    d0, dE = pd.Timestamp(clock["date"].iloc[0]), pd.Timestamp(clock["date"].iloc[-1])
    assert clock["mark"].iloc[0] == "open" and clock["mark"].iloc[-1] == "open"
    ends = {d: sessions[sessions.get_loc(d) - 1] for d in (d0, dE)}
    # TRI closes: from the session before the first open to the session before
    # the last open. The END session's TRI close is never loaded (holdout lock).
    tri = con.execute(f"""SELECT CAST(date AS DATE) AS date, index_name, tri FROM read_parquet(
        '{(ROOT / 'data/external/nifty_tri.parquet').as_posix()}')
        WHERE CAST(date AS DATE) >= DATE '{ends[d0].date()}' AND CAST(date AS DATE) < DATE '{dE.date()}'
        """).df()
    tri["date"] = pd.to_datetime(tri["date"])
    _guard(tri["date"].max())
    assert not tri.duplicated(["index_name", "date"]).any()
    # price levels at the four endpoint sessions; the END session's close is not read
    pdates = sorted({d0, ends[d0], dE, ends[dE]})
    parents = sorted({p for _, p in BROAD_IDX.values()} | {p for _, p in FACTOR_IDX.values()})
    px = con.execute(f"""SELECT CAST(date AS DATE) AS date, index_name, open,
            CASE WHEN CAST(date AS DATE) < DATE '{dE.date()}' THEN close END AS close
        FROM read_parquet('{(ROOT / 'data/indices').as_posix()}/*.parquet', union_by_name=true)
        WHERE index_name IN ({', '.join(repr(x) for x in parents)})
          AND CAST(date AS DATE) IN ({', '.join(f"DATE '{d.date()}'" for d in pdates)})""").df()
    px["date"] = pd.to_datetime(px["date"])
    assert not px.duplicated(["index_name", "date"]).any()
    pxi = px.set_index(["index_name", "date"])
    ep = pd.read_csv(ROOT / "data/external/nifty_price_endpoints.csv", dtype=str)
    ep["Date"] = pd.to_datetime(ep["Date"], format="%d %b %Y")
    assert not ep.duplicated(["IndexName", "Date"]).any()
    epi = ep.set_index(["IndexName", "Date"])

    def level(tab, name, d, col):
        if (name, d) not in tab.index:
            raise SystemExit(f"refusing: no {col} for {name} on {d.date()} (fetch it first)")
        v = tab.at[(name, d), col]
        return v

    def price_ratio(name, d) -> float:
        o, c = level(pxi, name, d, "open"), level(pxi, name, ends[d], "close")
        if not (pd.notna(o) and pd.notna(c) and o > 0 and c > 0):
            raise SystemExit(f"refusing: {name} has no open on {d.date()} or close on {ends[d].date()}")
        return float(o) / float(c)

    closes = clock[clock["mark"] == "close"]["date"].map(pd.Timestamp)
    cols, info = {}, {}
    for col, (tri_name, parent) in [*BROAD_IDX.items(), *FACTOR_IDX.items()]:
        s = tri[tri["index_name"] == tri_name].set_index("date")["tri"]
        miss = [d for d in [ends[d0], *closes] if d not in s.index or not (s[d] > 0)]
        if miss:
            raise SystemExit(f"refusing: {tri_name} TRI missing on {len(miss)} clock sessions, "
                             f"first {miss[0].date()}")
        opens, opens_no, how = {}, {}, {}
        for d in (d0, dE):
            if col in BROAD_IDX:
                r, src = price_ratio(parent, d), "own open (data/indices)"
                r_no = r
            else:
                o = level(epi, tri_name, d, "Open")
                c = level(epi, tri_name, ends[d], "Close")
                c = float(c)
                if isinstance(o, str) and o.strip() not in ("-", ""):
                    r, src = float(o) / c, "own open (endpoints csv)"
                    r_no = r
                else:
                    r, src = price_ratio(parent, d), f"parent {parent} overnight move"
                    r_no = 1.0
            opens[d] = float(s[ends[d]]) * r
            opens_no[d] = float(s[ends[d]]) * r_no
            how[str(d.date())] = {"source": src, "overnight_ratio": r,
                                  "tri_prev_close": float(s[ends[d]])}
        lv = []
        lv_no = []
        for d, mk in zip(clock["date"].map(pd.Timestamp), clock["mark"]):
            if mk == "close":
                lv.append(float(s[d])); lv_no.append(float(s[d]))
            else:
                if d not in opens:
                    raise SystemExit(f"refusing: an open mark on {d.date()} is not a clock endpoint")
                lv.append(opens[d]); lv_no.append(opens_no[d])
        lv = np.array(lv); lv_no = np.array(lv_no)
        cols[col] = lv / lv[0]
        if col in FACTOR_IDX:
            cols[col + "_no_overnight"] = lv_no / lv_no[0]
        info[col] = {"tri_name": tri_name, "price_or_parent": parent, "endpoints": how}
    return pd.DataFrame(cols, index=clock.index), info


# ================================================================ main
def pick(u: pd.DataFrame, held: set) -> tuple[list, set]:
    """Item 1 and the Clarification before the v2 build: a holding is kept while
    in the universe and ranked within the top 24; vacancies up to 12 are filled
    best rank first under the cap of 3 per sector. Kept names count toward the
    cap and are never sold for it. An unlabelled company is its own sector."""
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


def sold_reason(c, df: pd.DataFrame, u: pd.DataFrame) -> str:
    """Why a holding at D is not in D's book."""
    if c not in df.index:
        return "left_universe:no_data"
    r = df.loc[c]
    fails = [x for x in ("r1", "r2", "r3", "r4", "r5", "r6", "r7") if not bool(r[x])]
    if fails:
        return "left_universe:" + ",".join(fails)
    hf = [n for n, col in (("mcap", "hf_mcap"), ("pe", "hf_pe"), ("roce", "hf_roce"), ("de", "hf_de"))
          if not bool(r[col])]
    if hf:
        nb = "" if bool(r["hf_bs"]) else f";no_balance_sheet:{r['bs_reason']}"
        return "hard_filter:" + ",".join(hf) + nb
    if c in u.index and int(u.at[c, "rank"]) > BUFFER:
        return f"rank_outside_{BUFFER}"
    return "not_selected"


def a1_metric(book: BookV2, P: Panels, adj_ff: np.ndarray) -> dict:
    """The Clarification before the v2 build, "A1's adoption test, made exact":
    for each new position, the unit-weighted average adjusted fill price over
    its tranches (sum of value / sum of adjusted units; costs not included),
    divided by the adjusted close on the session before its D (EQ/BE, the last
    close on or before that session, same adjustment basis); averaged over all
    new positions weighted by V, the target value set at D's open. A position
    with no filled tranche has no price and is counted, not averaged."""
    rows = []
    for (D, c), pos in sorted(book.positions.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        k, tD = pos["k"], P.idx(D)
        ref = float(adj_ff[tD - 1, k])
        n_f = sum(1 for v in pos["tranches"].values() if v["status"] == "filled")
        avg = pos["value"] / pos["units_adj"] if pos["units_adj"] > 0 else np.nan
        rows.append({"D": str(D.date()), "symbol": c, "V": pos["V"], "weight": pos["weight"],
                     "ref_close_adjusted": ref, "avg_fill_price_adjusted": avg,
                     "relative_price": avg / ref if np.isfinite(avg) else None,
                     "tranches_filled": n_f,
                     "tranches": {kk: pos["tranches"].get(kk, {"status": "pending_at_end"})
                                  for kk in ("tranche1", "tranche2", "tranche3")}})
    f = [r for r in rows if r["relative_price"] is not None]
    Vs = sum(r["V"] for r in f)
    return {"definition": a1_metric.__doc__.split("\n\n")[0] if a1_metric.__doc__ else "",
            "variant": book.variant,
            "v_weighted_relative_price": (sum(r["V"] * r["relative_price"] for r in f) / Vs) if Vs else None,
            "n_new_positions": len(rows), "n_with_a_fill": len(f),
            "n_with_no_fill": len(rows) - len(f),
            "n_all_three_filled": sum(1 for r in rows if r["tranches_filled"] == 3),
            "positions": rows}


def main(argv=None) -> int:
    global UNLOCK, END_DATE, FULL_END, OUT, ROCE_FROM, LATEST_NAMES
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", choices=["a", "b"], required=True,
                    help="a: staggered entry (item 5); b: staggered entry with A1's rule")
    ap.add_argument("--run-final-test", action="store_true",
                    help="required to compute anything after the in-sample end. The owner's one run only.")
    ap.add_argument("--no-leak-test", action="store_true")
    ap.add_argument("--end", default=None, help="final NAV mark (open) date; must be a session")
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    if a.end:
        if not a.run_final_test:
            raise SystemExit("--end after the in-sample end needs --run-final-test")
        FULL_END = pd.Timestamp(a.end)
    UNLOCK = bool(a.run_final_test)
    OUT = Path(a.out) if a.out else ROOT / "data" / "backtest" / (
        f"v2{a.variant}_final_check" if FULL_END is not None else f"v2{a.variant}_is_check")
    t_start = time.time()

    con = duck()
    sessions_all = all_sessions(con)
    Ds, END = decision_dates(sessions_all)
    END_DATE = END
    assert END in sessions_all, "END must be a session"
    ROCE_FROM = sessions_all[sessions_all.searchsorted(pd.Timestamp(*ROCE_ANCHOR))]
    sessions = sessions_all[sessions_all <= END]
    log(f"variant ({a.variant}); decision dates: {[d.date().isoformat() for d in Ds]}  end(open)={END.date()}; "
        f"ROCE from {ROCE_FROM.date()}")
    assert len(Ds) == 16 if FULL_END is None else len(Ds) > 16, len(Ds)

    p = load_prices(con, END)
    log(f"prices {len(p):,} rows")
    spells = build_ids(load_identity_keys(con), all_sessions(con, "1990-01-01"))
    ca, dv = load_corpactions(con, END, spells)
    reparsed = list(ca.attrs.get("reparsed", []))
    bz, bz_map = map_bz(load_bz(con, END), spells)
    P = Panels(p, spells, sessions, ca, bz)
    P.add_dividends(dv)
    log(f"BZ rows mapped {bz_map}; in panel {P.bz_info}; panels {P.T} x {P.K}; "
        f"split/bonus events {len(ca)}; dividends {len(dv)} (unparsed {dv.attrs['unparsed']})")
    # EQ/BE closes before the panel, only to start A1's RSI at each stock's first
    # close in the archive (Clarification after the in-sample engine comparison)
    pre = load_pre_closes(con, spells)
    log(f"pre-panel EQ/BE closes for the RSI seed: {len(pre.frame):,} rows on {len(pre.sessions)} sessions "
        f"from {pre.sessions[0].date()}")
    fin = load_financials(con, spells)
    log(f"quarter rule (Clarification 38) in the files: {QUARTER_RULE_INFO}")
    taxfill = taxonomy_fill(fin)
    _f = fin.dropna(subset=["company"]).sort_values("broadcast_dt")
    LATEST_NAMES = _f.groupby("cid")["company"].last().to_dict()
    fin = fin[fin["broadcast_dt"] < END]
    lab, names, flagged = load_labels(con, spells, P)
    nse_sector, nse_scheme, sec_info = load_nse_sectors(spells)
    log(f"financial rows {len(fin):,}; v1 labelled stocks {lab['cid'].nunique()}; taxonomy backfill "
        f"{len(taxfill)}; NSE sector labels {sec_info['stock_ids_labelled']} stock ids "
        f"(conflicts {len(sec_info['stock_ids_with_conflicting_labels'])}, "
        f"legacy scheme {len(sec_info['legacy_scheme_stock_ids'])})")
    # Nifty 500 TRI closes for the trend filter: never on or after END
    tri = con.execute(f"""SELECT CAST(date AS DATE) AS date, tri FROM read_parquet(
        '{(ROOT / 'data/external/nifty_tri.parquet').as_posix()}')
        WHERE index_name = '{TREND_INDEX}' AND CAST(date AS DATE) < DATE '{END.date()}' ORDER BY 1""").df()
    tri["date"] = pd.to_datetime(tri["date"])
    assert not tri["date"].duplicated().any()
    _guard(tri["date"].max())
    # "Session" is a market session in the archive's calendar (v1 Clarification
    # 1), so the trend windows count archive sessions and use the TRI's close on
    # each. NSE's special sessions that the archive does not hold (Muhurat and
    # Saturday budget sessions) are not sessions here; a missing TRI close on an
    # archive session is refused.
    tri_s = tri.set_index("date")["tri"].astype(float)
    cal = all_sessions(con, str(tri_s.index.min().date()))
    cal = cal[cal < END]
    tri500 = tri_s.reindex(cal)
    if tri500.isna().any():
        raise SystemExit(f"refusing: Nifty 500 TRI missing on archive sessions "
                         f"{[str(d.date()) for d in tri500.index[tri500.isna()]][:5]}")
    tri_extra = [str(d.date()) for d in tri_s.index.difference(cal)]
    log(f"Nifty 500 TRI on {len(tri500)} archive sessions; TRI-only special sessions ignored: {tri_extra}")

    OUT.mkdir(parents=True, exist_ok=True)
    frames, scored, diag, trend = {}, {}, {}, {}
    for D in Ds:
        dg: dict = {}
        df = universe_and_measures(D, P, fin, lab, names, taxfill, ca, nse_sector, dg)
        u = score(df)
        frames[D], scored[D], diag[str(D.date())] = df, u, dg
        trend[D] = trend_at(tri500, D, P.sessions[P.idx(D) - 1])
        v1u = df["v1_univ"]
        log(f"{D.date()}  v1 rules {int(v1u.sum()):4d}  universe {len(u):4d}  "
            f"(fail mcap {int((v1u & ~df.hf_mcap).sum())}, pe {int((v1u & ~df.hf_pe).sum())}, "
            f"roce {int((v1u & ~df.hf_roce).sum())}, de {int((v1u & ~df.hf_de).sum())}, "
            f"no bs {int((v1u & ~df.hf_bs).sum())})  quality={u['quality_basis'].iloc[0] if len(u) else '-'}  "
            f"trend {int(trend[D]['c1'])}{int(trend[D]['c2'])}{int(trend[D]['c3'])} "
            f"cash {trend[D]['cash_fraction']:.3f}")

    # ---------------- outputs: universe, ranks, balance-sheet diagnostics
    uni = pd.concat([pd.DataFrame({"D": u["D"].dt.date, "symbol": u.index, "ticker": u["symbol"].values})
                     for u in scored.values()])
    uni.to_csv(OUT / "universe.csv", index=False)
    rcols = ["D", "symbol", "ticker", "sector", "quality_basis", "consolidated", "mcap", "pe", "ttm_ebit",
             "bs_period_end", "equity", "debt", *MEASURES_ALL, *[m + "_pct" for m in MEASURES_ALL],
             *GROUP_NAMES, "composite", "rank"]
    rk = pd.concat([u.reset_index().rename(columns={"cid": "_cid", "symbol": "ticker"})
                    .rename(columns={"_cid": "symbol"})[rcols] for u in scored.values()])
    rk["D"] = pd.to_datetime(rk["D"]).dt.date
    rk.to_csv(OUT / "ranks.csv", index=False)
    bcols = ["D", "symbol", "ticker", "v1_univ", "in_univ", "consolidated", "ttm_ebit", "bs_period_end",
             "bs_broadcast_dt", "bs_url", "bs_has_is", "bs_age_days", "bs_reason", "bs_usable", "equity", "debt",
             "roce_value", "de_value", "mcap", "pe", "hf_mcap", "hf_pe", "hf_bs", "hf_roce", "hf_de"]
    bd = pd.concat([df[df["v1_univ"]].reset_index().rename(columns={"index": "_cid", "symbol": "ticker"})
                    .rename(columns={"_cid": "symbol"})[bcols] for df in frames.values()])
    bd["D"] = pd.to_datetime(bd["D"]).dt.date
    bd.to_csv(OUT / "bs_diag.csv", index=False)

    # ---------------- A1 indicators (both variants report them; only (b) trades on them)
    adj_ff, above, rsi, a1_ok = a1_indicators(P, pre)

    # ---------------- the strategy
    t_idx = {D: P.idx(D) for D in Ds}
    t0, t_end = t_idx[Ds[0]], P.idx(END)
    hold_rows, dec_rows, size_info = [], [], {}

    def decide(D, held: set, V: float):
        u, df, tr = scored[D], frames[D], trend[D]
        sel, keep = pick(u, held)
        cf = tr["cash_fraction"]
        sig = (-u.loc[sel, "low_vol"]).values.astype(float) if sel else np.zeros(0)
        w, bound, si = size_weights(sig, cf)
        size_info[str(D.date())] = si
        status = {c: ("kept" if c in keep else "new") for c in sel}
        for c, wi, s_, b in zip(sel, w, sig, bound):
            hold_rows.append({"D": D.date(), "symbol": c, "ticker": u.at[c, "symbol"],
                              "rank": int(u.at[c, "rank"]), "sector": u.at[c, "sector"], "sigma": s_,
                              "target_weight": wi, "status": status[c], "sold_reason": "",
                              "weight_bound": b, "composite": u.at[c, "composite"],
                              "quality_basis": u.at[c, "quality_basis"],
                              "sector_scheme": nse_scheme.get(c, None)})
        for c in sorted(held - set(sel)):
            in_u = c in u.index
            hold_rows.append({"D": D.date(), "symbol": c,
                              "ticker": df.at[c, "symbol"] if c in df.index else None,
                              "rank": int(u.at[c, "rank"]) if in_u else None,
                              "sector": nse_sector.get(c, None),
                              "sigma": float(-df.at[c, "low_vol"]) if c in df.index else None,
                              "target_weight": 0.0, "status": "sold", "sold_reason": sold_reason(c, df, u),
                              "weight_bound": "", "composite": u.at[c, "composite"] if in_u else None,
                              "quality_basis": u.at[c, "quality_basis"] if in_u else None,
                              "sector_scheme": nse_scheme.get(c, None)})
        v1u = df["v1_univ"]
        roce_on = bool(df["roce_on"].iloc[0])
        ranked_roce = (u["quality_basis"] == "roce") & u["roce"].notna()
        sel_sec = u.loc[sel, "sector"]
        dec_rows.append({
            "D": D.date(), "trend_c1": tr["c1"], "trend_c2": tr["c2"], "trend_c3": tr["c3"],
            "cash_fraction": cf, "n_universe": int(len(u)),
            "n_excluded_hard_filters": int((v1u & ~df["hard_ok"]).sum()),
            "n_excluded_no_balance_sheet": int((v1u & ~df["hf_bs"]).sum()) if roce_on else 0,
            "n_ranked_on_roce": int(ranked_roce.sum()),
            "n_held": len(sel), "n_held_unlabelled_sector": int(sel_sec.isna().sum()),
            # extra columns
            "nav_open": V, "n_kept": len(keep), "n_new": len(sel) - len(keep),
            "n_sold": len(held - set(sel)), "n_v1_rules": int(v1u.sum()),
            "n_fail_mcap": int((v1u & ~df["hf_mcap"]).sum()), "n_fail_pe": int((v1u & ~df["hf_pe"]).sum()),
            "n_fail_roce": int((v1u & ~df["hf_roce"]).sum()), "n_fail_de": int((v1u & ~df["hf_de"]).sum()),
            "n_held_ranked_on_roce": int(ranked_roce.reindex(sel).fillna(False).sum()),
            "n_held_legacy_sector_label": int(sum(1 for c in sel if nse_scheme.get(c) == "legacy")),
            "weight_sum": float(w.sum()), "n_weight_upper": int((bound == "upper").sum()),
            "n_weight_lower": int((bound == "lower").sum()),
            "tri_date": tr["tri_date"].date(), "tri_last": tr["tri_last"], "tri_sma200": tr["tri_sma200"],
            "tri_high252": tr["tri_high252"], "tri_below_sma_sessions_of_126": tr["tri_below_sma_sessions_of_126"]})
        return sel, w, status

    book = BookV2(P, COST, a.variant, a1_ok)
    strat = book.run({t_idx[D]: D for D in Ds}, decide, t0, t_end)
    hold = pd.DataFrame(hold_rows)
    hold["rank"] = hold["rank"].astype("Int64")
    hold.to_csv(OUT / "holdings.csv", index=False)
    dec = pd.DataFrame(dec_rows)
    dec.to_csv(OUT / "decisions.csv", index=False)
    fcols = ["date", "symbol", "ticker", "side", "units_adjusted", "price_adjusted", "value", "cost", "kind",
             "scheduled_date", "D", "cancelled_kind", "reason"]
    fills = pd.DataFrame(book.fills).reindex(columns=fcols)
    fills.to_csv(OUT / "fills.csv", index=False)
    a1 = a1_metric(book, P, adj_ff)
    (OUT / "a1.json").write_text(json.dumps(a1, indent=2, default=_js), encoding="utf-8")

    # ---------------- universe equal-weight benchmark (v1's rules on the v2 universe)
    ew = Book(P, COST, name="universe_ew")
    plan = {t_idx[D]: (P.cids.get_indexer(scored[D].index), len(scored[D])) for D in Ds}
    ew_nav = ew.run(plan, t0, t_end)

    nav = strat.rename(columns={"nav": "strategy"})
    nav["universe_ew"] = ew_nav["nav"].values
    assert (nav["date"].values == ew_nav["date"].values).all()
    bench, bench_info = benchmark_columns(con, nav[["date", "mark"]], sessions)
    nav = pd.concat([nav, bench], axis=1)[NAV_COLS]
    nav["date"] = pd.to_datetime(nav["date"]).dt.date
    nav.to_csv(OUT / "nav.csv", index=False)
    t4t = pd.concat([book.t4t_frame("strategy"), ew.t4t_frame()], ignore_index=True)
    t4t.to_csv(OUT / "t4t_events.csv", index=False)

    # ---------------- metrics and checks (reported; nothing here chooses anything)
    dates = pd.to_datetime(nav["date"])
    m: dict = {"variant": a.variant, "end_open": str(END.date()), "in_sample_only": FULL_END is None,
               "decision_dates": [str(d.date()) for d in Ds], "roce_from": str(ROCE_FROM.date())}
    m["strategy"] = perf(nav["strategy"], dates)
    m["universe_ew"] = perf(nav["universe_ew"], dates)
    m["benchmarks_how"] = bench_info
    tr = pd.DataFrame(book.trades, columns=["t", "k", "d", "px", "val"])
    yrs = m["strategy"]["years"]
    m["strategy"].update({
        "start_rupees": START_CASH, "end_rupees": float(nav["strategy"].iloc[-1]),
        "turnover_one_sided_annual_incl_initial": float(tr["val"].abs().sum() / 2 / nav["strategy"].mean() / yrs),
        "dividends_credited": book.divs, "costs_paid": book.costs, "min_cash": book.min_cash,
        "forced_exits": [(str(d.date()), c) for d, c in book.forced],
        "buys_scaled_down": book.scaled_buys,
        "orders_pending_at_end": [{"symbol": P.cids[o["k"]], "kind": o["kind"],
                                   "scheduled": str(P.sessions[min(o["t_sched"], P.T - 1)].date()),
                                   "D": str(P.sessions[o["t_D"]].date()), "value": o["value"]}
                                  for o in book.pending_at_end]})
    m["universe_ew"].update({"dividends_credited": ew.divs, "costs_paid": ew.costs,
                             "forced_exits": [(str(d.date()), c) for d, c in ew.forced]})
    fl = fills
    m["fills_by_kind"] = {k: int(v) for k, v in fl["kind"].value_counts().items()}
    m["cancelled_by_kind_and_reason"] = {f"{a_}|{b_}": int(v) for (a_, b_), v in
                                         fl[fl["kind"] == "cancelled"].groupby(["cancelled_kind", "reason"]).size().items()}
    m["cash_fraction_by_D"] = {str(r["D"]): r["cash_fraction"] for r in dec_rows}
    held_rows = hold[hold["status"] != "sold"]
    m["share_universe_rows_ranked_on_roce"] = float(dec["n_ranked_on_roce"].sum() / dec["n_universe"].sum())
    m["share_holdings_ranked_on_roce"] = float((held_rows["quality_basis"] == "roce").mean())
    m["share_held_slots_unlabelled_sector"] = float(held_rows["sector"].isna().mean())
    m["n_held_slots"] = int(len(held_rows))
    m["sizing"] = size_info
    m["nse_sectors"] = sec_info
    m["a1_summary"] = {k: v for k, v in a1.items() if k != "positions"}
    m["a1_ok_share_of_universe_rows_at_D_minus_1"] = {
        str(D.date()): float(np.mean(a1_ok[t_idx[D] - 1, P.cids.get_indexer(scored[D].index)]))
        for D in Ds}
    m["by_decision_date"] = diag
    m["trend_tri_special_sessions_ignored"] = tri_extra
    m["trend_by_D"] = {str(D.date()): {k: (v if not isinstance(v, pd.Timestamp) else str(v.date()))
                                       for k, v in trend[D].items()} for D in Ds}
    m["reparsed_null_factor_actions"] = reparsed
    m["bz_mapping"], m["bz_panel"] = bz_map, P.bz_info

    # invariants (asserted: a failure stops the run)
    assert book.min_cash >= -1e-6, f"cash went negative: {book.min_cash}"
    for D, g in held_rows.groupby("D"):
        assert len(g) <= N, (D, len(g))
        lab_counts = g["sector"].dropna().value_counts()
        assert (lab_counts <= SECTOR_CAP).all(), (D, lab_counts.to_dict())
        cf = trend[pd.Timestamp(D)]["cash_fraction"]
        e = (1 - cf) / N
        assert (g["target_weight"] >= W_LO * e - 1e-12).all() and (g["target_weight"] <= W_HI * e + 1e-12).all()
        assert g["target_weight"].sum() <= (1 - cf) + 1e-12
    m["invariants"] = "ok: cash never negative; <= 12 names; <= 3 per labelled sector; weights in bounds"

    try:
        _guard(END + pd.Timedelta(days=1))
        m["holdout_guard_selftest"] = ("not applicable: unlocked by --run-final-test" if UNLOCK
                                       else "FAILED: guard did not raise")
    except HoldoutViolation:
        m["holdout_guard_selftest"] = "ok: raises after " + str(END.date())

    if not a.no_leak_test:
        # 2022-11-15, the last in-sample date, where balance sheets exist; in the
        # full-period run also the first date a year into the ROCE rules
        leak_Ds = [Ds[15]]
        if FULL_END is not None:
            leak_Ds += [d for d in Ds if d >= ROCE_FROM + pd.DateOffset(years=1)][:1]
        m["leak_test"] = {}
        for Dl in leak_Ds:
            r = leak_test(con, Dl, p, bz, spells, sessions, fin, lab, names, taxfill, nse_sector,
                          tri500, frames[Dl], scored[Dl], trend[Dl], P, above, rsi, pre)
            m["leak_test"][str(Dl.date())] = r
            log(f"leak test {Dl.date()}: passed={r['passed']}  {json.dumps(r, default=_js)[:600]}")
        m["leak_test_passed"] = all(r["passed"] for r in m["leak_test"].values())

    m["readings"] = READINGS
    m["v1_readings_kept"] = AMBIGUITIES
    m["runtime_seconds"] = round(time.time() - t_start, 1)
    (OUT / "metrics.json").write_text(json.dumps(m, indent=2, default=_js), encoding="utf-8")
    log(f"wrote {OUT}: fills by kind {m['fills_by_kind']}; cash fractions {sorted(set(dec['cash_fraction']))}; "
        f"A1 V-weighted relative price {a1['v_weighted_relative_price']}")
    return 0


def _js(o):
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (pd.Timestamp,)):
        return str(o.date())
    return str(o)


def leak_test(con, D, p, bz, spells, sessions, fin, lab, names, taxfill, nse_sector, tri500,
              ref_df: pd.DataFrame, ref_u: pd.DataFrame, ref_trend: dict, P_full: Panels,
              above_full: np.ndarray, rsi_full: np.ndarray, pre: PreCloses | None = None) -> dict:
    """Rebuild everything from inputs cut to what was knowable at D (prices,
    trade-for-trade rows, corporate actions and dividends before D; filings
    broadcast before D; Nifty 500 TRI closes before D) and check that the v2
    universe (v1 rules and hard filters), every measure, the balance-sheet
    choice, ROCE and debt / equity, the composite, the trend conditions and
    A1's two indicators at the session before D are unchanged. The industry
    labels (v1's backfill, NSE's sector), the taxonomy backfill and names are
    static inputs held fixed, as in the v1 checker."""
    D = pd.Timestamp(D)
    p2 = p[p["date"] < D]
    s2 = sessions[sessions <= D]
    ca2, dv2 = load_corpactions(con, D - pd.Timedelta(days=1), spells)
    P2 = Panels(p2, spells, s2, ca2, bz[bz["date"] < D])
    P2.add_dividends(dv2)
    fin2 = fin[fin["broadcast_dt"] < D]
    df2 = universe_and_measures(D, P2, fin2, lab, names, taxfill, ca2, nse_sector)
    u2 = score(df2)
    res: dict = {"D": str(D.date()), "n_universe": int(len(ref_u)),
                 "same_universe": set(u2.index) == set(ref_u.index),
                 "same_v1_rules_set": set(df2.index[df2["v1_univ"]]) == set(ref_df.index[ref_df["v1_univ"]])}
    common = ref_u.index.intersection(u2.index)
    diffs = {}
    for c in [*MEASURES_ALL, "composite", "mcap", "pe", "ttm_ebit"]:
        x, y = ref_u.loc[common, c].astype(float), u2.loc[common, c].astype(float)
        both = x.notna() & y.notna()
        diffs[c] = float(np.nanmax(np.abs((x[both] - y[both]) / y[both].abs().clip(lower=1e-12)))) \
            if both.any() else 0.0
        if (x.isna() != y.isna()).any():
            diffs[c + "_nan_mismatch"] = int((x.isna() != y.isna()).sum())
    res["same_rank"] = bool((ref_u.loc[common, "rank"] == u2.loc[common, "rank"]).all())
    # balance-sheet choice and its figures, over every stock passing v1's rules
    v1c = ref_df.index[ref_df["v1_univ"]].intersection(df2.index)
    for c in ["roce_value", "de_value", "equity", "debt"]:
        x, y = ref_df.loc[v1c, c].astype(float), df2.loc[v1c, c].astype(float)
        both = x.notna() & y.notna()
        diffs["bs:" + c] = float(np.nanmax(np.abs((x[both] - y[both]) / y[both].abs().clip(lower=1e-12)))) \
            if both.any() else 0.0
        if (x.isna() != y.isna()).any():
            diffs["bs:" + c + "_nan_mismatch"] = int((x.isna() != y.isna()).sum())
    same_bs = (ref_df.loc[v1c, "bs_url"].fillna("") == df2.loc[v1c, "bs_url"].fillna("")).all() and \
        (ref_df.loc[v1c, "bs_reason"] == df2.loc[v1c, "bs_reason"]).all()
    res["same_balance_sheet_choice"] = bool(same_bs)
    res["n_with_usable_balance_sheet"] = int(ref_df.loc[v1c, "bs_usable"].fillna(False).astype(bool).sum())
    same_hf = all((ref_df.loc[v1c, h] == df2.loc[v1c, h]).all() for h in ("hf_mcap", "hf_pe", "hf_bs", "hf_roce", "hf_de"))
    res["same_hard_filter_flags"] = bool(same_hf)
    tr2 = trend_at(tri500[tri500.index < D], D, s2[-2])
    res["same_trend"] = all(tr2[k] == ref_trend[k] for k in ("c1", "c2", "c3", "cash_fraction", "tri_date",
                                                              "tri_below_sma_sessions_of_126")) and \
        all(abs(tr2[k] - ref_trend[k]) <= 1e-12 * abs(ref_trend[k]) for k in ("tri_last", "tri_sma200", "tri_high252"))
    # A1 indicators at the session before D, universe members
    _, ab2, rs2, _ = a1_indicators(P2, pre)          # pre-panel closes are all before D
    t1, t2 = P_full.idx(D) - 1, P2.idx(D) - 1
    k1, k2 = P_full.cids.get_indexer(common), P2.cids.get_indexer(common)
    a_1, a_2 = above_full[t1, k1], ab2[t2, k2]
    r_1, r_2 = rsi_full[t1, k1], rs2[t2, k2]
    diffs["a1:above_abs"] = float(np.nanmax(np.abs(a_1 - a_2))) if np.isfinite(a_1).any() else 0.0
    diffs["a1:rsi_abs"] = float(np.nanmax(np.abs(r_1 - r_2))) if np.isfinite(r_1).any() else 0.0
    if (np.isfinite(a_1) != np.isfinite(a_2)).any() or (np.isfinite(r_1) != np.isfinite(r_2)).any():
        diffs["a1_nan_mismatch"] = int((np.isfinite(a_1) != np.isfinite(a_2)).sum() +
                                       (np.isfinite(r_1) != np.isfinite(r_2)).sum())
    same_ok = bool(((a_1 <= A1_MAX_ABOVE) & (r_1 < A1_RSI_MAX) == (a_2 <= A1_MAX_ABOVE) & (r_2 < A1_RSI_MAX)).all())
    res["same_a1_decisions"] = same_ok
    num_ok = all(v < 1e-9 for k, v in diffs.items() if not k.endswith("mismatch") and not k.startswith("a1:")) \
        and diffs["a1:above_abs"] < 1e-9 and diffs["a1:rsi_abs"] < 1e-7
    res["max_diff"] = diffs
    res["passed"] = bool(res["same_universe"] and res["same_v1_rules_set"] and res["same_rank"] and same_bs
                         and same_hf and res["same_trend"] and same_ok and num_ok
                         and not any(k.endswith("mismatch") for k in diffs))
    return res


READINGS = [
    "Hard filters (item 3, Clarification before the v2 build) are universe rules ANDed with v1 rules 1-7 at "
    "every D: market cap (v1's definition, rupees) > Rs 100 crore = 1e9, strictly above; trailing P/E = market "
    "cap / TTM normalised profit (the chosen basis, the same TTM as earnings yield), 0 < P/E <= 70. From the "
    "first decision date on or after 15 Feb 2023: ROCE >= 10% and debt / equity < 1.5, equity > 0, from the "
    "balance sheet the Clarification to A5 picks; no usable balance sheet (none known in the basis, older than "
    "400 days, equity <= 0, or equity + debt <= 0) fails both. In-sample (Feb 2019 to Nov 2022) no D is on or after that date, so ROCE "
    "is neither a filter nor a measure in-sample; it is computed and written to bs_diag.csv for checking only.",
    "n_excluded_hard_filters = stocks passing v1 rules 1-7 that fail at least one hard filter. "
    "n_excluded_no_balance_sheet = stocks passing v1 rules 1-7 with no usable balance sheet at D (0 before the "
    "ROCE date), whether or not another hard filter also removes them. n_ranked_on_roce = universe rows whose "
    "quality group used ROCE.",
    "EBIT (Clarification to A5): per quarter, pbt_before_exceptional + finance_costs; where "
    "pbt_before_exceptional is missing, pbt - exceptional_items (missing exceptional = 0) + finance_costs. A "
    "quarter with no finance-cost line has no EBIT (missing, not 0: Clarification after the in-sample engine "
    "comparison), nor has one with neither profit line; either way the TTM EBIT and ROCE are missing and "
    "the stock fails the ROCE floor from Feb 2023. TTM = the same four consecutive quarters as v1's TTM (the chosen basis), each from the "
    "income-statement revision of Clarification 38/39.",
    "Balance sheet (Clarification to A5): a filing has one when it reports equity and total assets (equals the "
    "archive's has_balance_sheet flag, asserted). Candidates: filings broadcast before D with a balance sheet, "
    "excluding the filings whose income statement the income scale screen (30a) set aside at D, matched by "
    "(stock, basis, period end, broadcast time), not by URL. A balance-sheet period is its exact period end, "
    "not the calendar quarter (both: Clarification after the in-sample engine comparison). Per (stock, "
    "basis, period end) the latest revision (broadcast time, then document URL) is that period's balance sheet. "
    "The balance-sheet screen then drops a period whose total assets AND paid-up share capital "
    "(equity_capital) both differ 30x or more in the same direction from more than half of its up-to-8 "
    "nearest other balance sheets of the same stock and basis (nearest by days between period ends, ties to "
    "the earlier), "
    "counting only balance sheets with positive assets and capital; 30a's 'a basis's only quarter is compared "
    "with the other basis' provision is not part of 'the same band and majority rule' and is not applied. "
    "THE balance sheet is the latest remaining period in the stock's basis as chosen at D (v1 Clarification "
    "4). Age = D minus the balance sheet's period_end in days; > 400 is not used. Debt = debt_long + "
    "debt_short, each missing line 0. Usable only with equity > 0 AND equity + debt > 0 (reason "
    "capital_not_positive otherwise). Capital = equity + debt; ROCE = TTM EBIT / capital; D/E = debt / equity.",
    "Sector (item 7): the `sector` column of data/reference/industry_nse.parquet, every non-empty value "
    "including the 21 rows NSE labels in its old (legacy) scheme, joined to the stock id by (symbol, ISIN) "
    "spell, else a unique ISIN, else the symbol's latest spell. When two rows map to one id, a current-scheme "
    "row wins over a legacy one. Unlabelled = own sector. decisions.csv also reports held slots with a legacy "
    "label. The lender rule (v1 rule 4) still uses v1's announcement labels.",
    "Positions (item 1): kept = held at D's open (units > 0) AND in the v2 universe AND rank <= 24; vacancies to "
    "12 in rank order, cap 3 per sector counting kept names; held names not kept are sold at D's open (v1 "
    "rules: sells first, may wait 5 sessions, a sale may fill at the BZ open). A new position whose first "
    "tranche has not filled by the next D is not 'held' there.",
    "Trend (item 4): Nifty 500 gross TRI closes (nifty_tri.parquet `tri`) on the archive's market sessions "
    "before D (v1 Clarification 1: a session is a market session in the archive's calendar). The TRI file also "
    "has closes on NSE special sessions the archive lacks (2019-10-27, 2020-02-01 and 2020-11-14 in-sample; "
    "2023-11-12, 2024-01-20, 2024-03-02, 2024-05-18, 2025-02-01 and 2026-02-01 later); they are not sessions "
    "here and are skipped. The last close must be the archive session before D, else the run refuses. (1) last < mean(last 200); (2) last <= 0.9 x max(last "
    "252); (3) count over the last 126 sessions j of close_j < mean(close_{j-199..j}) >= 0.6 x 126 (i.e. >= "
    "76). Cash fraction by count: 0, 0.20, 0.275, 0.35 of NAV at D's open.",
    "Sizing (item 6): sigma = v1's low-vol standard deviation at D (sample sd of daily adjusted log returns "
    "between traded closes in the last 252 sessions). A name with no sigma gets (1-cash)/12 before scaling "
    "and the others are scaled around it: w_i = (1/sigma_i) / sum(1/sigma) x ((1-cash) - m x (1-cash)/12) over "
    "the names with a sigma, m the names without (Clarification after the in-sample engine comparison; never "
    "occurs, every universe member has a sigma); with every sigma known, w = (1/sigma) / sum(1/sigma) x "
    "(1-cash). Then repeat, over all names alike: "
    "every name not yet at a bound and above 1.5e or below 0.5e (e = (1-cash)/12) is set to that bound and "
    "fixed; the net excess (over-bound minus under-bound amounts) is added to the unfixed names pro rata to "
    "their current weights; stop when none is out of bounds or all are fixed (then the rest is cash).",
    "Targets: NAV = cash + holdings valued at D's open (EQ/BE open, else BZ open, else last close). Kept "
    "holding target = w x NAV, traded in full at D's open (pending tranche cancelled). New position: V = w x "
    "NAV; three 'add' orders of V/3 rupees scheduled at session indices t_D, t_D + 21 and t_D + 42 (market "
    "sessions). Each tranche's rupee amount is fixed at D; costs (0.20% of traded value) come out of cash. "
    "Buys in one session are scaled down pro rata only if cash after that session's sells cannot cover them "
    "plus costs (v1 Clarification 11 applied to every fill session).",
    "Variant (a): a tranche fills at the open of its scheduled session if the stock has an EQ/BE trade there, "
    "else at the first such session within the next 5, else it is cancelled (v1 Clarification 10). The other "
    "tranches are unaffected by one cancelled tranche.",
    "Variant (b), A1: from the scheduled session s, the tranche fills at the open of the first session u in "
    "s .. s+29 (the 30 sessions 'within 30 sessions of the scheduled session') on which the stock has an EQ/BE "
    "trade and, at the close of session u-1, adjusted close <= 1.10 x its 50-session simple mean and 14-session "
    "Wilder RSI < 70. If none, it fills at the open of s+30 (the 31st session) regardless; if the stock has no "
    "EQ/BE trade there, v1's buying rule lets it wait up to 5 more sessions, then it is cancelled. Indicators "
    "use EQ/BE adjusted closes carried forward over market sessions without a trade (C1: sessions are market "
    "sessions), SMA over the last 50 market sessions including u-1, RSI started at the stock's first EQ/BE "
    "close in the archive (from 1 Jan 2015; the closes before the 2017-06-01 panel are read for this only, "
    "adjusted into the panel's basis), seeded with the simple mean of its first 14 changes, then Wilder's "
    "recursion; where the average gain and loss are both 0 the RSI is undefined and the condition fails "
    "(Clarification after the in-sample engine comparison). The scheduled session is the first of the 30.",
    "Pending orders are all cancelled at the next D (logged in fills.csv as kind=cancelled, reason "
    "next_decision_date); a sale to zero, including a forced exit, cancels that stock's pending orders "
    "(reason position_left_book). Orders scheduled after the END open never execute and are listed in "
    "metrics.json, not fills.csv.",
    "Forced exit: v1 Clarification 15 with the trade-for-trade rule (BZ sessions count as trading), sold at "
    "the holding's last close with the 0.20% cost; kind=forced_exit, D = the latest decision date.",
    "fills.csv: symbol = stock id, ticker = the stock's last EQ/BE symbol on or before the fill date; units "
    "and prices are adjusted by the split/bonus factors with ex-session after the fill date up to the run's "
    "END (the share basis at END), so units_adjusted x price_adjusted = value (rupees, always positive; side "
    "gives the direction). Cancelled rows: units 0, price empty, value = the rupee amount not traded.",
    "A1 metric (a1.json): per new position, sum(value) / sum(units_adjusted) over its FILLED tranches (costs "
    "excluded), divided by the adjusted EQ/BE close of the session before its D (last close on or before, "
    "same basis); V-weighted mean over positions with at least one fill. A new position means a status=new "
    "row in holdings.csv (a stock re-entering after being sold is a new position).",
    "nav.csv: strategy and universe_ew in rupees from Rs 5,00,000 at the first open (Clarification after the "
    "in-sample engine comparison: the equal-weight benchmark starts at Rs 5 lakh, as in v1); the index columns "
    "from 1.0. The universe equal-weight benchmark holds every v2-universe name at D equally (V/|U|), with "
    "v1's rules: no trend cash, no tranches, same costs, dividends, 5-session waits, forced exit and BZ rule.",
    "Outputs name the stock id 'symbol' and the ticker traded at D 'ticker' in every file.",
    "Leak test: at the last in-sample date (2022-11-15), where balance sheets exist, everything is rebuilt from "
    "inputs cut to before D and compared (universe, measures, balance-sheet choice, ROCE, D/E, hard-filter "
    "flags, trend, A1 indicators).",
]


AMBIGUITIES = [
    "(v1 checker readings, kept for v2 where v2 does not change them; see jobs/crosscheck_v1.py for the full "
    "text.) Sessions = distinct bhavcopy dates; D = first session on/after the anchor; the final mark is END's "
    "OPEN. Stock identity by linked (symbol, ISIN) spells. Rule 1 on the last trade row before D; rule 2 "
    "median over 60 market sessions with untraded = 0; rule 3 trading days in [D-365d, D). Rule 4 by v1 "
    "labels, the latest-name net for unlabelled stocks, and the XBRL taxonomy vote with its static backfill. "
    "Basis: consolidated when consolidated income statements cover the 4 quarters ending at the latest known "
    "quarter. Clarifications 22, 30, 38 and 39 as in the v1 checker. Percentiles rank(pct, average), missing "
    "0.5, ties by the symbol traded at D then the id. Dividends and splits as Clarifications 13, 14, 24; "
    "trade-for-trade as the Clarification to A2; benchmarks as the Clarification to A4.",
]


if __name__ == "__main__":
    raise SystemExit(main())
