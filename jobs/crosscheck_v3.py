"""Independent cross-check of strategy v3, written from the spec text alone.

Source: research/strategy/v3-spec.md (committed ce91bdd; sections 2 to 13 and 15),
with the v1 and v2 plumbing it cites (research/strategy/v1-spec.md, Clarifications
1-40; v2-spec's Clarification to A2 and A4/A5), exactly as jobs/crosscheck_v2.py
read them.

This is the CHECKER, not the production engine. It was written without opening
zen/portfolio/engine_v3.py, jobs/backtest_v3.py or tests/test_engine_v3.py. Its
data loaders, identity links, scale screens, trade-for-trade handling and benchmark
clock are the v2 checker's (themselves independent of zen/pit, zen/factors and
zen/portfolio); every v3 rule (basis b*, the figures, the filters F0-F6, the
ranking, the exits, the valuation trim, the 10% cap, the orders and the review
sequence) is written here from the v3 text. The only zen import is
zen.data.financials.statement_files.

    .venv/Scripts/python.exe -m jobs.crosscheck_v3 --out data/backtest/v3_dev_check            # 27 reviews, no returns
    .venv/Scripts/python.exe -m jobs.crosscheck_v3 --out data/backtest/v3_dev_check --agreement  # + the agreement run
    .venv/Scripts/python.exe -m jobs.crosscheck_v3 --frozen data/frozen/v3 --out <dir> ...     # frozen inputs (15.2.4)

Outputs (--out): universe.csv, eligibility.csv, ranks.csv for all 27 reviews
(no returns; eligibility rows are the v1-universe members). With --agreement
(the reviews to 15 November 2022, measured to the open of 15 February 2023),
the folder <out>/agreement/ holds the full section 13 set for those 12 reviews:
universe, eligibility (members AND holdings), ranks, decisions, holdings, fills,
nav, plus trades, episodes, rebalance_open, in_sample_end_open, trade_for_trade,
t4t_events and metrics.json.

HOLDOUT LOCK (spec section 2 and 15.4): every price or return the simulator reads
passes through _guard(), which raises after the open of 15 February 2023. The
simulation panel is built from prices truncated at that open (its close and
turnover are never loaded). The all-27-reviews stage reads closes only up to the
session before each review (it needs them for the market cap) and runs no
simulation. Without --run-final-test this checker cannot compute a v3 return after
the 15 February 2023 open. --run-final-test is the one full run of section 15.6 (all
27 reviews, measured to the open of 18 September 2026, into <out>/full), used once,
after the agreement period agrees; the guard then stands at that open. It never
writes state/trials.jsonl (the production run records the one trial).

--frozen DIR: every input is read from DIR (the freeze mirrors the repo's relative
paths); without it the live repo paths are read (development only). The raw inputs
this checker reads are: data/daily/**, data/daily_other/**, data/corpactions/*,
data/announcements/**, data/indices/*, data/financials/* (via statement_files),
data/reference/industry_nse.parquet, data/external/nifty_tri.parquet,
data/external/nifty_price_endpoints.csv, nse_list.json. It does not open
data/zen.duckdb (it reads the parquet the database is rebuilt from).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
import sys
import time
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from zen.data.financials import statement_files  # noqa: E402  (the only zen import)

DATA_ROOT: Path = ROOT                               # --frozen DIR replaces it
DUCK_TMP = ROOT / "data" / "cache" / "duck"          # git-ignored spill directory

# ---------------------------------------------------------------- spec constants
ANCHORS = [(2, 15), (6, 1), (8, 15), (11, 15)]
FIRST_ANCHOR = pd.Timestamp(2020, 2, 15)             # section 2: first review = first session on/after
LAST_ANCHOR = pd.Timestamp(2026, 8, 15)              # last review 17 Aug 2026 (27 reviews)
AGREE_LAST_ANCHOR = pd.Timestamp(2022, 11, 15)       # last review of the agreement period (12 reviews)
END_ANCHOR = pd.Timestamp(2023, 2, 15)               # agreement period measured to this review's OPEN
FULL_END = pd.Timestamp(2026, 9, 18)                 # the one full run is measured to this session's OPEN
N_REVIEWS, N_AGREE = 27, 12
PRICE_START = "2017-06-01"

# v1 universe rules (v1 spec, rules 1-7 and their clarifications)
MIN_TURNOVER = 20e5                                  # Rs 20 lakh
TURNOVER_WINDOW = 60
MIN_SESSIONS_365 = 200
TRADED_LAST = 5
MAX_STALE_DAYS = 200                                 # rule 5, and E5

# v3 portfolio (sections 7 to 11)
SLOTS = 18
SECTOR_CAP = 4
CAP_WEIGHT = 0.10                                    # section 9.2
HALF_SLOT_DIV = 36                                   # a buy below NAV_open / 36 is not placed
COST = 0.0020
BUY_FACTOR = 1.002                                   # section 10, step 11: B_left / 1.002
SELL_FACTOR = 0.998                                  # step 10: units x px_now x (1 - 0.002)
EXEC_WAIT = 5                                        # C10's window: D to the 5th session after D
FORCED_EXIT_SESSIONS = 20
START_CASH = 500_000.0

# v3 filters and definitions (sections 4 and 5)
TTM_P_FLOOR = 25e7                                   # Rs 25 crore = 2.5e8 rupees (the spec's units note)
ICR_MIN = 2.5
WB_LIMIT = -1e7                                      # write-back: a finance cost below -Rs 1 crore
DE_BUY = 1.2                                         # purchase needs D/E < 1.2
DE_SELL = 1.2                                        # sale follows D/E > 1.2 (E3)
DE_FROM = pd.Timestamp(2022, 11, 15)                 # F3 and E3 apply from this review
PE_BUY = 60.0
PE_TRIM = 85.0
BS_MAX_AGE_DAYS = 400
PRICE_LOOKBACK_DAYS = 420                            # pit.PRICE_LOOKBACK_DAYS (section 3.5)
G_BASE_2Q = 12.5e7                                   # Rs 12.5 crore, a two-quarter sum
G_BASE_4Q = 25e7                                     # Rs 25 crore, a four-quarter sum
PEG_CAP = 0.50

LENDER_LABEL = re.compile(r"bank|financ|insurance|nbfc", re.I)
LENDER_NAME = re.compile(
    r"\bbank\b|insurance|assurance|\bfinance\b|financial\b|fincorp|finserv|finvest|"
    r"\bcredit\b|leasing|housing fin", re.I)

# Rule 4 point-in-time lender flag (v1 Clarification 28)
TAXONOMY_RX = r"/xbrl/(?:INTEGRATED_FILING_)?([A-Z_]+?)_\d"
LENDER_TAX = ("NBFC_INDAS", "BANKING", "GI", "LI")
NBFC_TAX_START = pd.Timestamp("2020-01-01")

# Data-quality screens (v1 Clarification 30)
LOG_BAND = math.log10(30.0)          # 30x on a log scale
SCALE_NEIGH = 8
SH_REF_Q = 4
SH_ALT_TOL = 1.5

# Clarification 38/39: the income-statement figures that make a filing "report an income
# statement", and the per-quarter fields taken from that filing.
IS_FIGURES = ("revenue", "total_income", "other_income", "employee_cost", "ebitda",
              "profit_normalised")
IS_CARRY = ["revenue", "total_income", "employee_cost", "ebitda", "profit_normalised",
            "shares_implied",
            "pbt_before_exceptional", "finance_costs", "pbt", "exceptional_items",
            # v3 diagnostics (owners' profit estimate, section 13)
            "eps_basic", "profit_continuing"]
BS_FIELDS = ["equity", "equity_capital", "assets", "debt_long", "debt_short"]
QUARTER_MAX_DAYS = 100

# Benchmarks (v1 Clarification 17/32 and the v2-spec Clarification to A4)
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

LATEST_NAMES: dict = {}
QUARTER_RULE_INFO: dict = {}
END_DATE: pd.Timestamp | None = None                 # the holdout lock (simulation only)


class HoldoutViolation(RuntimeError):
    pass


def _guard(d) -> None:
    """Refuse any price/return read after the agreement period's end."""
    if END_DATE is None:
        raise HoldoutViolation("in-sample end not set")
    if pd.Timestamp(d) > END_DATE:
        raise HoldoutViolation(f"{pd.Timestamp(d).date()} is after the agreement period's end "
                               f"{END_DATE.date()}; this checker has no unlock")


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

# ================================================================ loading (the v2 checker's loaders)
def duck() -> duckdb.DuckDBPyConnection:
    DUCK_TMP.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute(f"SET temp_directory='{DUCK_TMP.as_posix()}'")
    con.execute("SET memory_limit='8GB'")
    return con


def all_sessions(con, start: str = PRICE_START) -> pd.DatetimeIndex:
    s = con.execute(f"SELECT DISTINCT CAST(date AS DATE) d FROM read_parquet("
                    f"'{(DATA_ROOT / 'data/daily').as_posix()}/**/*.parquet', union_by_name=true) "
                    f"WHERE date >= '{start}' ORDER BY 1").df()["d"]
    return pd.DatetimeIndex(pd.to_datetime(s))


def load_prices(con, end: pd.Timestamp) -> pd.DataFrame:
    # NOTE: prev_close is never selected (it is unadjusted and wrong on ex-dates).
    p = con.execute(f"""
        SELECT CAST(date AS DATE) AS date, symbol, series, isin_code AS isin,
               open,
               -- the END session is marked at its OPEN only: its close and
               -- turnover are never loaded (holdout lock)
               CASE WHEN CAST(date AS DATE) < DATE '{end.date()}' THEN close END AS close,
               CASE WHEN CAST(date AS DATE) < DATE '{end.date()}' THEN turnover END AS turnover
        FROM read_parquet('{(DATA_ROOT / 'data/daily').as_posix()}/**/*.parquet', union_by_name=true)
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
        FROM read_parquet('{(DATA_ROOT / 'data/daily_other').as_posix()}/**/*.parquet', union_by_name=true)
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
        FROM read_parquet('{(DATA_ROOT / 'data/daily').as_posix()}/**/*.parquet', union_by_name=true)
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



def review_dates(sessions: pd.DatetimeIndex) -> list[pd.Timestamp]:
    """Section 2: the first NSE session on or after 15 Feb, 1 Jun, 15 Aug and 15 Nov, from the
    first review (the first session on or after 15 Feb 2020) to 17 Aug 2026: 27 reviews."""
    out = []
    for y in range(FIRST_ANCHOR.year, LAST_ANCHOR.year + 1):
        for m, d in ANCHORS:
            a = pd.Timestamp(y, m, d)
            if a < FIRST_ANCHOR or a > LAST_ANCHOR:
                continue
            i = sessions.searchsorted(a)
            assert i < len(sessions), f"no session on or after {a.date()}"
            out.append(sessions[i])
    return out


class Panels:
    """Wide (session x stock) arrays. Raw prices; adjustment factors separate.

    light=True builds only what the review-stage structure needs (closes, symbols,
    traded flags, turnover, the BZ close); the simulation uses a full panel."""

    def __init__(self, p: pd.DataFrame, spells: pd.DataFrame, sessions: pd.DatetimeIndex,
                 ca: pd.DataFrame, bz: pd.DataFrame | None = None, light: bool = False):
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
        self.series_at = mat(p["series"].values, None, object)
        self.light = light

        # --- trade-for-trade (v2-spec Clarification to A2). A BZ session is a session with a BZ
        # trade and NO EQ/BE trade for the stock id. It is not a no-trade session, a holding is
        # valued at its BZ close, and a sale may be made at its BZ open. Buying never uses it.
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
        # the latest close of a stock in EQ, BE or BZ, EQ/BE winning on a day with both (section 3.5)
        self.hclose = hclose
        if light:
            return

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
        self.mark = adj_ff / self.C
        del adj_ff
        # holding mark: last close in EQ/BE or, on BZ sessions, BZ  (raw-equivalent, correct
        # across a split with no trade)
        self.hadj = hclose * self.C
        self.hmark = pd.DataFrame(self.hadj).ffill().values / self.C

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
        for t, k, a in zip(ti[ok], ki[ok], dv["dps"].values[ok]):
            # Clarification 13: more than half the prior close (in ex-date share units) is a parse
            # error and is dropped. The prior close is the holding's last close.
            prior = self.hmark[t - 1, k] * self.F[t, k] if t > 0 else np.nan
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
        FROM read_parquet('{(DATA_ROOT / 'data/corpactions').as_posix()}/*.parquet', union_by_name=true)
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
    files = ", ".join(f"'{p.as_posix()}'" for p in statement_files(DATA_ROOT / "data" / "financials"))
    f = con.execute(f"""
        SELECT * EXCLUDE (rn) FROM (
          SELECT symbol, company, period_end, broadcast_dt, consolidated, revenue, ebitda,
                 profit_normalised, shares_implied, xbrl_url, total_income, employee_cost,
                 other_income, quarter_span_days,
                 -- v2 (Clarification to A5): EBIT's inputs and the balance sheet
                 pbt_before_exceptional, finance_costs, pbt, exceptional_items, eps_basic, profit_continuing,
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


def load_labels(con, spells) -> tuple[pd.DataFrame, dict, set]:
    a = con.execute(f"""
        SELECT an_dt, symbol, company, industry
        FROM read_parquet('{(DATA_ROOT / 'data/announcements').as_posix()}/**/*.parquet', union_by_name=true)
        """).df()
    a["an_dt"] = pd.to_datetime(a["an_dt"])
    a["cid"] = map_symbol_dates(a, "symbol", "an_dt", spells)
    names = a.dropna(subset=["company"]).groupby("cid")["company"].agg(lambda s: set(s)).to_dict()
    lab = a[a["industry"].notna() & (a["industry"].str.strip() != "-")][["cid", "an_dt", "industry"]]
    lab = lab.sort_values(["an_dt", "industry"])
    # Today's nse_list is 2025-26 filing rows: how a company files now, not what
    # it was at D. Kept for the log only; it does not decide rule 4 (Clarification 28).
    L = pd.DataFrame(json.loads((DATA_ROOT / "nse_list.json").read_text(encoding="utf-8")))
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
    r = pd.read_parquet(DATA_ROOT / "data/reference/industry_nse.parquet",
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


def sector_at(lab: pd.DataFrame, D) -> pd.Series:
    """Latest label known before D; if none known before D, the earliest label ever
    recorded for the stock (the archive's labels start Jan 2022)."""
    before = lab[lab["an_dt"] < D].groupby("cid")["industry"].last()
    earliest = lab.groupby("cid")["industry"].first()
    return before.combine_first(earliest)


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



# ================================================================ the information set at D
def balance_sheets_both(fa: pd.DataFrame, D, set_aside: set) -> dict:
    """The latest balance sheet at D in each basis (v2-spec Clarification to A5, "Which balance
    sheet", as section 4.6 takes it: factors.latest_balance_sheets in basis b*). Candidates are
    filings broadcast before D that carry a balance sheet (equity and total assets), including
    filings with no income statement, less the filings the income scale screen set aside (matched
    on stock, basis, period end and broadcast time). The latest revision of each (stock, basis,
    period end) is that period's balance sheet; a balance sheet off scale against most of its
    neighbours (assets and paid-up capital 30x out, same direction) is dropped; the latest
    remaining period in each basis is returned. Which basis a stock reads is chosen by the caller
    (b*), so a stock whose balance sheet exists only in the other basis has none (section 4.6).
    Returns {(cid, consolidated): {period_end, equity, debt, age}}."""
    key = ["cid", "consolidated", "period_end"]
    b = fa[fa["has_bs"]]
    fkey = [(c, bool(k), pd.Timestamp(pe), pd.Timestamp(bd)) for c, k, pe, bd in
            zip(b["cid"], b["consolidated"], b["period_end"], b["broadcast_dt"])]
    aside = np.array([x in set_aside for x in fkey], dtype=bool)
    b = b[~aside]
    b = b.drop_duplicates(key, keep="last")
    b = b[~_bs_scale_breaks(b)]
    b = b.sort_values(["cid", "consolidated", "period_end"]).drop_duplicates(["cid", "consolidated"], keep="last")
    debt = b["debt_long"].astype(float).fillna(0.0) + b["debt_short"].astype(float).fillna(0.0)
    out = {}
    for cid, cons, pe, eq, dt in zip(b["cid"], b["consolidated"], b["period_end"],
                                     b["equity"].astype(float), debt):
        out[(cid, bool(cons))] = {"period_end": pd.Timestamp(pe), "equity": float(eq), "debt": float(dt),
                                  "age": int((pd.Timestamp(D) - pd.Timestamp(pe)).days)}
    return out


def fundamentals_core(fin: pd.DataFrame, D, ca: pd.DataFrame) -> dict:
    """Everything the filings known before D give, before any basis is chosen.

    The quarters (Clarifications 38, 39 and 30a, as v1 and the v2 checker applied them): the
    income statement for each (stock, basis, quarter) is the latest filing broadcast before D that
    reports one; a quarter no filing reports an income statement for is not known; a filing the
    unit-scale screen sets aside counts as not filed. v1's basis (C4), v1's TTM rows and the
    share count (C6, C30b) are built here too, exactly as the v2 checker built them."""
    fa = fin[fin["broadcast_dt"] < D].sort_values(["broadcast_dt", "xbrl_url"])
    key = ["cid", "consolidated", "qn"]
    f = fa.drop_duplicates(key, keep="last")
    fi = fa[fa["has_is"]].drop_duplicates(key, keep="last")
    fi = fi[key + IS_CARRY + ["broadcast_dt", "xbrl_url", "period_end"]].rename(
        columns={"broadcast_dt": "is_bdt", "xbrl_url": "is_url", "period_end": "is_pe"})
    f = f.drop(columns=IS_CARRY).merge(fi, on=key, how="left")
    lqn = f.groupby("cid")["qn"].transform("max")
    w = f[(f["qn"] > lqn - 12) & (f["broadcast_dt"] >= NBFC_TAX_START)]
    votes = w.assign(l=w["taxonomy"].isin(LENDER_TAX)).groupby("cid")["l"].agg(["sum", "count"])
    f = f[f["is_url"].notna()]
    brk = _scale_breaks(f)
    sa_rows = f.loc[brk & f["is_url"].notna()]
    set_aside = {(c, bool(k), pd.Timestamp(pe), pd.Timestamp(bd)) for c, k, pe, bd in
                 zip(sa_rows["cid"], sa_rows["consolidated"], sa_rows["is_pe"], sa_rows["is_bdt"])}
    f = f[~brk]
    f_both = f
    # v1's basis (C4): consolidated when consolidated filings cover the four consecutive quarters
    # ending at the latest known quarter (either basis)
    latest = f.groupby("cid")["qn"].max()
    cons = f[f["consolidated"]][["cid", "qn"]]
    need = pd.DataFrame({"cid": np.repeat(latest.index.values, 4),
                         "qn": np.concatenate([[q, q - 3, q - 6, q - 9] for q in latest.values])
                         if len(latest) else []})
    cov = need.merge(cons, on=["cid", "qn"], how="inner").groupby("cid").size()
    use_cons = cov.reindex(latest.index).fillna(0) >= 4
    # Share count (C6): the latest known quarter's shares_implied, standalone first
    lq = f[f["qn"] == f["cid"].map(latest)]
    sa = lq[~lq["consolidated"] & lq["shares_implied"].notna()].set_index("cid")
    co = lq[lq["consolidated"] & lq["shares_implied"].notna()].set_index("cid")
    sh = sa["shares_implied"].combine_first(co["shares_implied"])
    sh_bdt = sa["is_bdt"].combine_first(co["is_bdt"])
    sh_fix = pd.Series("", index=sh.index, dtype=object)
    sh_ref = pd.Series(np.nan, index=sh.index, dtype=float)       # C30b's reference count
    sh_ref_bdt = pd.Series(pd.NaT, index=sh.index, dtype="datetime64[ns]")
    # C30b: a count 30x or more from the median of up to four earlier quarters' counts (standalone
    # first, carried through splits and bonuses between the two broadcasts) is replaced by the other
    # basis if that is within 1.5x of the reference, else by the reference.
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
        sh_ref[c], sh_ref_bdt[c] = ref, bl
        if abs(math.log10(sh[c] / ref)) < LOG_BAND:
            continue
        alt = co if c in sa.index else sa
        if c in alt.index and alt.at[c, "shares_implied"] > 0 and \
                1 / SH_ALT_TOL <= alt.at[c, "shares_implied"] / ref <= SH_ALT_TOL:
            sh[c], sh_bdt[c], sh_fix[c] = alt.at[c, "shares_implied"], alt.at[c, "is_bdt"], "other_basis"
        else:
            sh[c], sh_fix[c] = ref, "carried_reference"
    # v1's TTM rows, in v1's basis
    f4 = f[f["consolidated"] == f["cid"].map(use_cons)]
    rows = []
    for cid, g in f4.sort_values(["qn", "consolidated"], ascending=False).groupby("cid", sort=True):
        q = g["qn"].values
        run = 1
        while run < len(q) and q[run - 1] - q[run] == 3:
            run += 1
        g0 = g.iloc[0]
        r = {"cid": cid, "consolidated": bool(g0["consolidated"]), "run": run,
             "latest_pe": g0["period_end"]}
        if run >= 4:
            top = g.iloc[:4]
            r["ttm_ebitda"] = top["ebitda"].sum(min_count=4)
            r["ttm_pn"] = top["profit_normalised"].sum(min_count=4)
        rows.append(r)
    cols = ["consolidated", "run", "latest_pe", "ttm_ebitda", "ttm_pn"]
    v1rows = pd.DataFrame(rows).set_index("cid").reindex(columns=cols)
    # the per-basis quarter tables for v3's own figures (section 4.1): known quarters only
    qd = f_both[["cid", "consolidated", "qn", "revenue", "profit_normalised", "ebitda",
                 "finance_costs", "eps_basic", "profit_continuing", "shares_implied"]].copy()
    pre = f_both["pbt_before_exceptional"].astype(float)
    alt_pbe = f_both["pbt"].astype(float) - f_both["exceptional_items"].astype(float).fillna(0.0)
    qd["pbe"] = pre.where(pre.notna(), alt_pbe)                       # PBE_k (section 4.2)
    qd["ebit"] = qd["pbe"] + qd["finance_costs"].astype(float)       # EBIT_k = PBE_k + FC_k
    qmap: dict = {}
    for r in qd.itertuples(index=False):
        qmap.setdefault((r.cid, bool(r.consolidated)), {})[int(r.qn) // 3 - 1] = (
            r.revenue, r.profit_normalised, r.ebitda, r.pbe, r.finance_costs, r.ebit,
            r.eps_basic, r.profit_continuing, r.shares_implied)
    return {"fa": fa, "f": f_both, "set_aside": set_aside, "use_cons": use_cons, "v1": v1rows,
            "sh": sh, "sh_bdt": sh_bdt, "sh_fix": sh_fix, "sh_ref": sh_ref, "sh_ref_bdt": sh_ref_bdt,
            "votes": votes, "qmap": qmap}


def sector_key(cid, nse_sector: pd.Series, nse_scheme: pd.Series) -> str:
    """Section 3.6: the label compared without regard to case (casefold); no label, or a label from
    NSE's legacy scheme, is the one sector 'unclassified'."""
    lab = nse_sector.get(cid)
    if lab is None or (isinstance(lab, float) and math.isnan(lab)) or str(lab).strip() == "":
        return "unclassified"
    if nse_scheme.get(cid) != "current":
        return "unclassified"
    return str(lab).casefold()


def review_frame(D, P: Panels, core: dict, lab: pd.DataFrame, taxfill: set, ca: pd.DataFrame) -> pd.DataFrame:
    """v1's universe at D (rules 1 to 7, section 3.1) for every stock id in the price panel, with the
    market cap of section 3.5 for every stock (member or not): market cap = C x N, C the latest
    close strictly before D in EQ, BE or BZ within 420 days (EQ/BE winning on a day with both),
    carried to D's share basis by the split and bonus factors with ex-date after that close's date
    and before D; N the share count at D (C6, C30b, carried to D's share basis)."""
    t = P.idx(D)
    D = pd.Timestamp(D)
    win5 = P.traded[t - TRADED_LAST:t].any(axis=0)
    last_isin = pd.DataFrame(P.isin_at[:t]).ffill().values[-1]
    last_sym = pd.DataFrame(P.symbol_at[:t]).ffill().values[-1]
    ine = np.array([isinstance(x, str) and x.startswith("INE") for x in last_isin])
    med_turn = np.median(P.turn[t - TURNOVER_WINDOW:t], axis=0)
    lo365 = P.sessions.searchsorted(D - pd.Timedelta(days=365))
    n365 = P.traded[lo365:t].sum(axis=0)
    df = pd.DataFrame({"ticker": last_sym, "r1": win5 & ine, "med_turn": med_turn, "n365": n365}, index=P.cids)
    df["r2"] = df["med_turn"] >= MIN_TURNOVER
    df["r3"] = df["n365"] >= MIN_SESSIONS_365
    # rule 4: lenders / insurers (v1 unchanged)
    sec = sector_at(lab, D)
    df["v1_label"] = sec.reindex(df.index)
    lab_lender = df["v1_label"].fillna("").str.contains(LENDER_LABEL)
    name_lender = pd.Series([bool(LENDER_NAME.search(LATEST_NAMES.get(c, "") or "")) for c in df.index],
                            index=df.index)
    name_lender &= df["v1_label"].isna()
    fu = core["v1"]
    votes = core["votes"]
    df = df.join(fu, how="left")
    tn = votes["count"].reindex(df.index).fillna(0)
    tl = votes["sum"].reindex(df.index).fillna(0)
    lender = (lab_lender | name_lender | ((tn > 0) & (2 * tl >= tn)) | ((tn == 0) & df.index.isin(list(taxfill))))
    df["r4"] = ~lender
    fresh = (D - df["latest_pe"]).dt.days <= MAX_STALE_DAYS
    df["r5"] = (df["run"] >= 4) & fresh
    df["r6"] = (df["ttm_pn"] > 0) & (df["ttm_ebitda"] > 0)
    # ---- section 3.5: the close C
    lo = P.sessions.searchsorted(D - pd.Timedelta(days=PRICE_LOOKBACK_DAYS))
    sub = P.hclose[lo:t]
    nrow = sub.shape[0]
    kk = np.arange(P.K)
    if nrow:
        idx = np.where(np.isfinite(sub), np.arange(nrow)[:, None], -1).max(axis=0)
    else:
        idx = np.full(P.K, -1)
    has = idx >= 0
    sidx = np.where(has, idx + lo, 0)
    close = np.where(has, P.hclose[sidx, kk], np.nan)
    cdate = pd.Series(P.sessions[sidx], index=df.index).where(pd.Series(has, index=df.index))
    cser = pd.Series(np.where(has, np.where(P.bz_traded[sidx, kk], "BZ", P.series_at[sidx, kk]), None),
                     index=df.index, dtype=object)
    df["close"], df["close_date"], df["close_series"] = close, cdate, cser
    # cumulative split/bonus factor: ex-date after the close's date and before D
    cum = np.ones(P.K)
    ca_pre = ca[ca["ex_date"] < D]
    kidx = {c: i for i, c in enumerate(P.cids)}
    for c, g in ca_pre.groupby("cid"):
        i = kidx.get(c)
        if i is None or not has[i]:
            continue
        m = g["ex_date"] > cdate.iat[i]
        if m.any():
            cum[i] = g.loc[m, "factor"].prod()
    # ---- N: the share count at D in pit.fundamentals (a stock with a row in v1's basis), carried
    # to D's share basis by the factors with ex-date after the count's filing's broadcast and before D
    N = df.index.map(core["sh"]).astype(float).values if len(core["sh"]) else np.full(P.K, np.nan)
    N = pd.Series(N, index=df.index)
    N = N.where(df["run"].notna())                     # pit.fundamentals has a row only in v1's basis
    ref = pd.Series(df.index.map(core["sh_ref"]).astype(float), index=df.index).where(df["run"].notna())
    bdt = pd.Series(pd.to_datetime(df.index.map(core["sh_bdt"])), index=df.index)
    rbdt = pd.Series(pd.to_datetime(df.index.map(core["sh_ref_bdt"])), index=df.index)
    Nadj, refadj = N.copy(), ref.copy()
    for c, g in ca_pre.groupby("cid"):
        if c not in df.index:
            continue
        b0 = bdt[c]
        if pd.notna(b0) and pd.notna(N[c]):
            after = g[g["ex_date"].dt.normalize() > pd.Timestamp(b0).normalize()]
            if len(after):
                Nadj[c] = N[c] / after["factor"].prod()
        r0 = rbdt[c]
        if pd.notna(r0) and pd.notna(ref[c]):
            after = g[g["ex_date"].dt.normalize() > pd.Timestamp(r0).normalize()]
            if len(after):
                refadj[c] = ref[c] / after["factor"].prod()
    df["N"] = Nadj
    df["N_src"] = pd.Series(df.index.map(core["sh_fix"]), index=df.index).where(df["run"].notna())
    df["N_ref"] = refadj
    df["N_ref_over_N"] = refadj / Nadj
    df["mcap"] = (df["close"] * cum) * df["N"]          # market cap = C (on D's basis) x N
    ok = np.isfinite(df["mcap"]) & (df["N"] > 0) & (df["mcap"] > 0)
    df.loc[~ok, "mcap"] = np.nan
    df["r7"] = ok
    df["v1_univ"] = df[["r1", "r2", "r3", "r4", "r5", "r6", "r7"]].all(axis=1)
    df["D"] = D
    return df


# ================================================================ v3's own figures
_NAN = float("nan")


def _isn(x) -> bool:
    return x != x


def _tsum(xs):
    """Left-to-right sum of doubles (section 4.10); NaN if any term is NaN."""
    s = xs[0]
    for x in xs[1:]:
        s = s + x
    return s


def _G(new, old, B):
    """Section 4.7: G(new, old) = (new - old) / max(old, B)."""
    if _isn(new) or _isn(old):
        return _NAN
    return (new - old) / max(old, B)


def _q2s(q: int) -> str:
    y, r = divmod(int(q), 4)
    return f"{y:04d}-{3 * (r + 1):02d}"


def v3_figures(cid, core: dict, bsd: dict, D) -> dict | None:
    """Section 3.3a (the basis b*), 4.1 to 4.9: every v3 figure of one stock at D, from the quarters
    known before D. Returns None for a stock with no known quarter at all."""
    qmap = core["qmap"]
    D = pd.Timestamp(D)
    qs = {True: qmap.get((cid, True), {}), False: qmap.get((cid, False), {})}
    allq = set(qs[True]) | set(qs[False])
    if not allq:
        return None
    uc = core["use_cons"]
    c4 = bool(uc[cid]) if cid in uc.index else False
    Ls = max(allq)

    def complete(b: bool) -> bool:
        d = qs[b]
        for q in range(Ls - 7, Ls + 1):
            r = d.get(q)
            if r is None or _isn(r[0]) or _isn(r[1]) or _isn(r[2]):
                return False
        return True

    if complete(True):
        b, case, L = True, "consolidated", Ls
    elif complete(False):
        b, case, L = False, "standalone", Ls
    else:
        b, case = c4, "v1_basis"
        L = max(qs[b]) if qs[b] else None
    out: dict = {"c4_cons": c4, "b_star": "consolidated" if b else "standalone", "b_case": case,
                 "window_complete": case != "v1_basis", "L_star": Ls}
    if L is None:
        out["L"] = None
        out["has_figures"] = False
        return out
    d = qs[b]

    def X(i, k):
        r = d.get(L - k)
        return _NAN if r is None else float(r[i])

    R = [X(0, k) for k in range(9)]
    P_ = [X(1, k) for k in range(9)]
    E = [X(2, k) for k in range(9)]
    FC = [X(4, k) for k in range(4)]
    EBIT = [X(5, k) for k in range(4)]
    run = 0
    while (L - run) in d:
        run += 1
    miss = [_q2s(L - k) for k in range(9) if (L - k) not in d]
    out.update({"L": L, "L_str": _q2s(L), "has_figures": True, "run": run,
                "missing_quarters": ";".join(miss)})
    ttm_r, ttm_p, ttm_e = _tsum(R[0:4]), _tsum(P_[0:4]), _tsum(E[0:4])
    ttm4_r, ttm4_p, ttm4_e = _tsum(R[4:8]), _tsum(P_[4:8]), _tsum(E[4:8])
    ttm_ebit = _tsum(EBIT)
    fcp = _NAN if any(_isn(x) for x in FC) else _tsum([max(x, 0.0) for x in FC])
    wb = any((not _isn(x)) and x < WB_LIMIT for x in FC)
    out.update({"ttm_r": ttm_r, "ttm_p": ttm_p, "ttm_e": ttm_e, "ttm4_r": ttm4_r, "ttm4_p": ttm4_p,
                "ttm4_e": ttm4_e, "ttm_ebit": ttm_ebit, "fc_plus": fcp, "write_back": wb})
    # interest coverage (section 4.5): four states in this order
    if _isn(ttm_ebit) or _isn(fcp):
        state = "unknown"
    elif wb:
        state = "write_back"
    elif fcp == 0:
        state = "no_finance_costs"
    else:
        state = "icr"
    icr = ttm_ebit / fcp if (not _isn(ttm_ebit) and not _isn(fcp) and fcp > 0) else _NAN
    out.update({"icr_state": state, "icr": icr})
    # growth-reversal flags (section 4.9); an unknown flag is None
    def known(xs):
        return not any(_isn(x) for x in xs)
    down0 = (_tsum(P_[0:4]) < _tsum(P_[4:8])) if known(P_[0:8]) else None
    down1 = (_tsum(P_[1:5]) < _tsum(P_[5:9])) if known(P_[1:9]) else None
    out.update({"down0": down0, "down1": down1})
    # ranking measures (section 4.8) -- computed whenever the inputs exist
    if known(R[0:8]) and known(P_[0:8]):
        a_r = _G(R[0] + R[1], R[4] + R[5], G_BASE_2Q) - _G(R[2] + R[3], R[6] + R[7], G_BASE_2Q)
        a_p = _G(P_[0] + P_[1], P_[4] + P_[5], G_BASE_2Q) - _G(P_[2] + P_[3], P_[6] + P_[7], G_BASE_2Q)
    else:
        a_r = a_p = _NAN
    if (not _isn(ttm_r)) and (not _isn(ttm4_r)) and ttm_r > 0 and ttm4_r > 0 and \
            not _isn(ttm_e) and not _isn(ttm4_e):
        m = ttm_e / ttm_r - ttm4_e / ttm4_r
    else:
        m = _NAN
    g = _G(ttm_p, ttm4_p, G_BASE_4Q)
    out.update({"A_R": a_r, "A_P": a_p, "M": m, "g": g})
    # balance sheet in basis b* (section 4.6)
    bs = bsd.get((cid, b))
    if bs is None:
        out.update({"bs_period_end": None, "bs_equity": _NAN, "bs_debt": _NAN, "bs_usable": False, "de": _NAN})
    else:
        usable = bs["age"] <= BS_MAX_AGE_DAYS
        de = bs["debt"] / bs["equity"] if bs["equity"] > 0 else _NAN
        out.update({"bs_period_end": bs["period_end"], "bs_equity": bs["equity"], "bs_debt": bs["debt"],
                    "bs_usable": bool(usable), "de": de})
    # diagnostic (section 13): TTM owners' profit, consolidated basis only
    own = _NAN
    if b:
        terms = []
        for k in range(4):
            r = d.get(L - k)
            if r is None:
                terms.append(_NAN)
                continue
            sa = qs[False].get(L - k)
            p_, eps, pc = float(r[1]), float(r[6]), float(r[7])
            nk = float(sa[8]) if sa is not None else _NAN
            fac = (eps * nk) / pc if (not _isn(eps) and not _isn(nk) and not _isn(pc) and pc != 0) else _NAN
            if _isn(fac) or not (0 < fac <= 1.5):
                fac = 1.0
            terms.append(p_ * fac)
        own = _tsum(terms)
    out["owners_ttm_p"] = own
    return out


def eligibility_row(D, cid, fg: dict | None, mc: dict, member: bool) -> dict:
    """The section 13 eligibility record, with filters F0 to F6 (section 5). `mc` carries the close,
    N and the market cap of section 3.5 from review_frame."""
    D = pd.Timestamp(D)
    row = {"D": D.date(), "symbol": cid, "ticker": mc["ticker"], "member": bool(member)}
    mcap = mc["mcap"]
    row.update({"close": mc["close"], "close_date": (None if pd.isna(mc["close_date"]) else mc["close_date"].date()),
                "close_series": mc["close_series"], "N": mc["N"], "N_src": mc["N_src"], "mcap": mcap,
                "N_ref": mc["N_ref"], "N_ref_over_N": mc["N_ref_over_N"]})
    if fg is None:
        fg = {"has_figures": False}
    hf = fg.get("has_figures", False)
    g_ = lambda k, default=_NAN: fg.get(k, default) if hf else default  # noqa: E731
    ttm_p = g_("ttm_p")
    pe = mcap / ttm_p if (not _isn(mcap) and not _isn(ttm_p) and ttm_p > 0 and mcap > 0) else _NAN
    row.update({"c4_basis": ("consolidated" if fg.get("c4_cons") else "standalone") if "c4_cons" in fg else None,
                "b_star": fg.get("b_star"), "L": fg.get("L_str") if hf else None, "run": g_("run", None),
                "missing_quarters": g_("missing_quarters", None), "ttm_p": ttm_p, "pe": pe,
                "ttm_ebit": g_("ttm_ebit"), "fc_plus": g_("fc_plus"), "write_back": g_("write_back", None),
                "icr_state": g_("icr_state", None), "icr": g_("icr"),
                "bs_period_end": (fg["bs_period_end"].date() if hf and fg.get("bs_period_end") is not None else None),
                "de": g_("de"), "down0": g_("down0", None), "down1": g_("down1", None),
                "window_complete": fg.get("window_complete"), "ttm_r": g_("ttm_r"), "ttm4_r": g_("ttm4_r"),
                "ttm4_p": g_("ttm4_p"), "bs_equity": g_("bs_equity"), "bs_debt": g_("bs_debt"),
                "bs_usable": g_("bs_usable", None)})
    own = g_("owners_ttm_p")
    row["owners_ttm_p"] = own
    row["owners_pe"] = mcap / own if (not _isn(mcap) and not _isn(own) and own > 0 and mcap > 0) else _NAN
    # filters
    f1 = bool(hf and not _isn(ttm_p) and ttm_p >= TTM_P_FLOOR)
    st = g_("icr_state", None)
    icr = g_("icr")
    f2 = bool(hf and (st == "no_finance_costs" or (st == "icr" and not _isn(icr) and icr >= ICR_MIN)))
    if D < DE_FROM:
        f3 = True
    elif hf and fg.get("bs_usable"):
        eq = fg["bs_equity"]
        de = fg["de"]
        f3 = bool(eq > 0 and not _isn(de) and de < DE_BUY)
    else:
        f3 = True
    f4 = bool(not _isn(pe) and 0 < pe <= PE_BUY)
    ttm_r, ttm4_r = g_("ttm_r"), g_("ttm4_r")
    f5 = bool(hf and fg.get("window_complete") and not _isn(ttm_r) and ttm_r > 0 and not _isn(ttm4_r) and ttm4_r > 0)
    f6 = not (g_("down0", None) is True and g_("down1", None) is True)
    row.update({"F0": bool(member), "F1": f1, "F2": f2, "F3": f3, "F4": f4, "F5": f5, "F6": f6})
    row["eligible"] = bool(member and f1 and f2 and f3 and f4 and f5 and f6)
    for k in ("A_R", "A_P", "M", "g"):
        row[k] = g_(k)
    g = row["g"]
    row["PEG"] = pe / (100 * min(g, PEG_CAP)) if (not _isn(g) and g > 0 and not _isn(pe)) else _NAN
    return row


def rank_eligible(elig: pd.DataFrame, sector_of) -> pd.DataFrame:
    """Section 6: whole and half ranks only. r(X) is the average rank from the worst (larger is
    better); R1 the average rank of r(A_R) + r(A_P); R2 = r(M); R3 = (n0 + 1) / 2 for the n0 stocks with
    g <= 0, else n0 + the average rank from the worst of PEG among positive-growth stocks (the
    highest PEG is the worst). T = R1 + R2 + R3; ties by the symbol traded at D, then the stock id."""
    df = elig.copy()
    n = len(df)
    if n == 0:
        return df.assign(r_A_R=[], r_A_P=[], r_M=[], R1=[], R2=[], R3=[], T=[], rank=[])
    for c in ("A_R", "A_P", "M", "g"):
        if df[c].isna().any():
            raise SystemExit(f"a ranking measure {c} is missing for {df.loc[df[c].isna(), 'symbol'].tolist()[:5]}")
    pos = df["g"] > 0
    if df.loc[pos, "PEG"].isna().any():
        raise SystemExit("PEG missing for a positive-growth stock")
    df["r_A_R"] = df["A_R"].rank(method="average")
    df["r_A_P"] = df["A_P"].rank(method="average")
    df["R1"] = (df["r_A_R"] + df["r_A_P"]).rank(method="average")
    df["r_M"] = df["M"].rank(method="average")
    df["R2"] = df["r_M"]
    n0 = int((~pos).sum())
    r3 = pd.Series((n0 + 1) / 2, index=df.index, dtype=float)
    r3[pos] = n0 + (-df.loc[pos, "PEG"]).rank(method="average")
    df["R3"] = r3
    df["T"] = df["R1"] + df["R2"] + df["R3"]
    order = sorted(range(n), key=lambda i: (-df["T"].iat[i], str(df["ticker"].iat[i]), str(df["symbol"].iat[i])))
    rk = np.empty(n, dtype=int)
    for pos_, i in enumerate(order):
        rk[i] = pos_ + 1
    df["rank"] = rk
    df["pct_A_R"], df["pct_A_P"], df["pct_M"] = df["r_A_R"] / n, df["r_A_P"] / n, df["r_M"] / n
    df["S1"], df["S2"], df["S3"] = df["R1"] / n, df["R2"] / n, df["R3"] / n
    df["S"] = df["T"] / (3 * n)
    df["sector"] = [sector_of(c) for c in df["symbol"]]
    return df.sort_values("rank")


# ================================================================ v1's share-count simulator (the universe equal-weight benchmark)
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


class BookEW(Book):
    """v1's engine for the universe equal-weight benchmark, recording NAV at each review's open."""

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.nav_open_at: dict = {}

    def rebalance(self, t, targets, slots):
        ref = self._val(t)
        held = np.flatnonzero(self.sh > 0)
        self.nav_open_at[t] = float(self.cash + np.nansum(self.sh[held] * ref[held]))
        super().rebalance(t, targets, slots)



# ================================================================ the v3 book (sections 7 to 11)
class BookV3:
    """Share-count simulator in adjusted units (units on the share basis of the last date loaded, so
    splits and bonuses never change a unit count; section 9.1's "adjusted units"). Orders are fixed
    at the review (section 9.4):
      sell_all  every unit held, filled at the open of the first session in C10's window (D to the
                5th session after D) on which the stock trades (EQ, BE or BZ);
      sell_n    a sell-only order for n adjusted units fixed at D (a trim), the same window, sells
                min(n, units held then);
      buy       a rupee amount V fixed at D, filled at the open of the first session in the window
                on which the stock trades in EQ or BE; units = V / that open; scaled down pro rata
                (and final) if cash at that session is short.
    At each session sells fill first, then buys. A window that passes with no fill cancels the order
    (recorded). Between reviews: dividends, pending orders, the 20-session no-trade exit, the close."""

    def __init__(self, P: Panels, start: float = START_CASH, cost: float = COST):
        self.P, self.cost = P, cost
        self.ua = np.zeros(P.K)
        self.cash = float(start)
        self.orders: list[dict] = []
        self.fills: list[dict] = []
        self.trades: list[tuple] = []
        self.divs = 0.0
        self.costs = 0.0
        self.forced: list[tuple] = []
        self.U0: dict[int, float] = {}
        self.prev_open_held: set = set()
        self.open_spells: dict[int, tuple] = {}
        self.closed_spells: list[tuple] = []
        self.t4t: list[tuple] = []
        self.nav_open_at: dict = {}
        self.min_cash = float(start)
        self.scaled_buys: list[tuple] = []
        self.pending_at_end: list[dict] = []
        self.hold_rows: list[dict] = []
        self.dec_rows: list[dict] = []
        self.hold_elig_rows: list[dict] = []
        self.counts = {"cancelled_pending_at_review": 0}

    # ---- prices (adjusted: raw x C)
    def _pxnow(self, t) -> np.ndarray:
        """px_now: the adjusted open if the stock trades at t (EQ, BE or BZ), else its last adjusted close."""
        P = self.P
        raw = np.where(P.traded[t], P.open[t], np.where(P.bz_traded[t], P.bz_open[t], P.hmark[t]))
        return raw * P.C[t]

    def _ticker(self, t, k):
        col = self.P.symbol_at
        for j in range(t, -1, -1):
            v = col[j, k]
            if isinstance(v, str):
                return v
        return None

    def _order(self, k, kind, mode, qty, t_D, code="") -> dict:
        return {"k": int(k), "kind": kind, "mode": mode, "qty": float(qty), "t_D": int(t_D),
                "t_sched": int(t_D), "code": code}

    def _row(self, t, k, o, side, units, price, value, cost, status, reason="") -> dict:
        P = self.P
        return {"date": P.sessions[t].date(), "symbol": P.cids[k], "ticker": self._ticker(t, k),
                "side": side, "units_adjusted": units, "price_adjusted": price, "value": value,
                "cost": cost, "kind": o["kind"], "scheduled_date": P.sessions[o["t_sched"]].date(),
                "D": P.sessions[o["t_D"]].date(), "status": status, "reason": reason,
                "exit_code": o.get("code", "")}

    def _can(self, o, t) -> bool:
        P, k = self.P, o["k"]
        if o["mode"] == "buy":
            return bool(P.traded[t, k])                   # buying never uses a BZ session
        return bool(P.traded[t, k] or P.bz_traded[t, k])  # a sale may fill on a BZ session

    def _status(self, o, t) -> tuple[str, str]:
        if t < o["t_sched"]:
            return "wait", ""
        if self._can(o, t):
            return "fill", ""
        if t >= o["t_D"] + EXEC_WAIT:
            return "cancel", "no_trade_within_5_sessions"
        return "wait", ""

    def _cancel(self, t, o, reason) -> None:
        side = "buy" if o["mode"] == "buy" else "sell"
        amt = o["qty"] if o["mode"] == "buy" else np.nan
        self.fills.append(self._row(t, o["k"], o, side, 0.0, np.nan, amt, 0.0, "cancelled", reason))

    def _left_book(self, t, k) -> None:
        """Units reached zero by any route: U0 cleared, the episode closed, pending orders cancelled."""
        self.U0.pop(k, None)
        rest = [x for x in self.orders if x["k"] == k]
        if rest:
            self.orders = [x for x in self.orders if x["k"] != k]
            for x in rest:
                self._cancel(t, x, "position_left_book")

    def _sell(self, t, k, n, price_adj, o, reason="") -> None:
        P = self.P
        full = o["mode"] == "sell_all" or o["kind"] == "forced_exit"
        n = float(self.ua[k]) if full else min(float(n), float(self.ua[k]))
        if n <= 0:
            return
        val = n * price_adj
        c = val * self.cost
        self.cash += val - c
        self.costs += c
        new = 0.0 if full else self.ua[k] - n
        if new <= 0:
            new = 0.0
        self.ua[k] = new
        C = P.C[t, k]
        self.trades.append((t, k, -n * C, price_adj / C, val, c, o["kind"]))
        self.fills.append(self._row(t, k, o, "sell", n, price_adj, val, c, "filled", reason))
        if new == 0.0:
            t0, e = self.open_spells.pop(k)
            self.closed_spells.append((P.cids[k], t0, t, e, price_adj, o["kind"], o.get("code", "")))
            self._left_book(t, k)

    def _buy(self, t, k, V, o) -> None:
        P = self.P
        C = P.C[t, k]
        px_adj = P.open[t, k] * C
        units = V / px_adj                                  # = (V / open) / C
        c = V * self.cost
        self.cash -= V + c
        self.costs += c
        self.min_cash = min(self.min_cash, self.cash)
        before = self.ua[k]
        self.ua[k] = before + units
        self.trades.append((t, k, units * C, P.open[t, k], V, c, "buy"))
        self.fills.append(self._row(t, k, o, "buy", units, px_adj, V, c, "filled", ""))
        if before <= 0:
            self.open_spells[k] = (t, px_adj)

    def _fill(self, t, now: list) -> None:
        P = self.P
        buys = []
        for o in now:
            k = o["k"]
            if o["mode"] == "buy":
                buys.append(o)
                continue
            on_bz = not P.traded[t, k]
            px = (P.bz_open[t, k] if on_bz else P.open[t, k]) * P.C[t, k]
            if on_bz:
                self.t4t.append((t, k, "sale_at_bz_open"))
            self._sell(t, k, o["qty"], px, o, "bz_open" if on_bz else "")
        need = sum(o["qty"] for o in buys) * (1 + self.cost)
        scale = min(1.0, max(self.cash, 0.0) / need) if need > 0 else 0.0
        if buys and scale < 1.0:
            self.scaled_buys.append((str(P.sessions[t].date()), float(scale)))
        for o in buys:
            V = o["qty"] * scale
            if V > 0:
                self._buy(t, o["k"], V, o)

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

    # ---- the review, step by step (section 10)
    def review(self, t, D, ctx, review_no: int) -> None:
        P = self.P
        D = pd.Timestamp(D)
        # 1. value the book (step 0, dividends, was credited at the top of the session)
        px = self._pxnow(t)
        held_k = np.flatnonzero(self.ua > 0)
        assert np.isfinite(px[held_k]).all(), "a holding has no price at its review"
        nav_open = self.cash + float(np.sum(self.ua[held_k] * px[held_k]))
        self.nav_open_at[t] = nav_open
        cash0 = self.cash
        # 2. cancel pending orders (a safeguard that cannot fire)
        for o in self.orders:
            self._cancel(t, o, "next_review")
            self.counts["cancelled_pending_at_review"] += 1
        self.orders = []
        # 3. the information set: figures for every holding
        held_cids = [P.cids[k] for k in held_k]
        rows = {c: ctx.row(D, c) for c in held_cids}
        # 4. reference units; 5. exits; 6. valuation trim; 7. the 10% cap
        info = {}
        for k in held_k:
            cid = P.cids[k]
            r = rows[cid]
            units = float(self.ua[k])
            pe = r["pe"]
            pe_known = not _isn(pe)
            if pe_known:
                if pe > PE_TRIM and k not in self.U0:
                    self.U0[k] = units                      # set at the first breach
                elif pe <= PE_TRIM:
                    self.U0.pop(k, None)                    # cleared
            codes = []
            ttm_p, ebit, fcp = r["ttm_p"], r["ttm_ebit"], r["fc_plus"]
            if not _isn(ttm_p) and ttm_p <= 0:
                codes.append("E1")
            if not _isn(ebit) and not _isn(fcp) and fcp > 0 and ebit / fcp < ICR_MIN:
                codes.append("E2")
            e3_on = D >= DE_FROM
            if e3_on and r["bs_usable"] is True and (r["bs_equity"] <= 0 or r["de"] > DE_SELL):
                codes.append("E3")
            was_held_prev = cid in self.prev_open_held
            if r["down0"] is True and r["down1"] is True and was_held_prev:
                codes.append("E4")
            if ctx.e5(D, cid):
                codes.append("E5")
            untested = []
            if _isn(ttm_p):
                untested.append("E1")
            if _isn(ebit) or _isn(fcp):
                untested.append("E2")
            if e3_on and r["bs_usable"] is not True:
                untested.append("E3")
            if (r["down0"] is None or r["down1"] is None) and was_held_prev:
                untested.append("E4")
            if _isn(r["mcap"]) or _isn(ttm_p):
                untested.append("trim")
            full = bool(codes)
            if full:
                # Clarification C8 (12 October 2026): for a holding with a full-sale order
                # under section 8 the trim is neither computed nor left untested
                untested = [u for u in untested if u != "trim"]
            uV = uC = None
            excess = None
            n_sell = 0.0
            kind = ""
            u_set = self.U0.get(k)
            if pe_known and pe > PE_TRIM:
                excess = pe / PE_TRIM - 1
            if not full:
                if u_set is not None and pe_known and pe > PE_TRIM:
                    uV = max(0.0, 1 - 2 * excess) * u_set
                    if uV == 0:
                        full = True
                        codes = ["V"]
                if not full:
                    uC = CAP_WEIGHT * nav_open / px[k]
                    ustar = min(uV, uC) if uV is not None else uC
                    if units > ustar:
                        n_sell = units - ustar
                        kind = "valuation" if (uV is not None and uV <= uC) else "cap"
            info[k] = {"cid": cid, "r": r, "units": units, "codes": codes, "full": full, "uV": uV, "uC": uC,
                       "excess": excess, "n_sell": units if full else n_sell, "trim_kind": kind,
                       "untested": untested, "U0": self.U0.get(k)}
        # 8. empty slots
        kept = [k for k in held_k if not info[k]["full"]]
        empty = max(0, SLOTS - len(kept))
        # orders for the sales and trims
        exp_proceeds = 0.0
        for k in held_k:
            i = info[k]
            if i["full"]:
                kind = "valuation" if i["codes"] == ["V"] else "exit"
                self.orders.append(self._order(k, kind, "sell_all", 0.0, t, i["codes"][0]))
                exp_proceeds += i["units"] * px[k] * SELL_FACTOR
            elif i["n_sell"] > 0:
                self.orders.append(self._order(k, i["trim_kind"], "sell_n", i["n_sell"], t, ""))
                exp_proceeds += i["n_sell"] * px[k] * SELL_FACTOR
        # 9. candidates: the eligible list in rank order, minus every stock held at D's open
        held_set = set(held_cids)
        cands = [c for c in ctx.eligible_in_rank_order(D) if c not in held_set]
        # 10. budget; 11. fill
        B_left = cash0 + exp_proceeds
        B = B_left
        count: dict = {}
        for k in kept:
            s = ctx.sector(P.cids[k])
            count[s] = count.get(s, 0) + 1
        placed, skipped_cap, half_stop = [], False, False
        for c in cands:
            if len(placed) >= empty:
                break
            s = ctx.sector(c)
            if count.get(s, 0) >= SECTOR_CAP:
                skipped_cap = True
                continue
            V = min(nav_open / SLOTS, B_left / BUY_FACTOR)
            if V < nav_open / HALF_SLOT_DIV:
                half_stop = True
                break
            k = P.cids.get_loc(c)
            self.orders.append(self._order(k, "buy", "buy", V, t))
            B_left = B_left - V * BUY_FACTOR
            count[s] = count.get(s, 0) + 1
            placed.append(c)
        left_empty = empty - len(placed)
        reasons = []
        if left_empty > 0:
            if not half_stop:
                # the candidate list ran out (the loop ended without a half-slot stop)
                reasons.append("none_eligible")
            if skipped_cap:
                reasons.append("sector_cap")
            if half_stop:
                reasons.append("half_slot_minimum")
        # records ------------------------------------------------------------------
        D_date = D.date()
        rank_of = ctx.rank_of(D)
        for k in held_k:
            i = info[k]
            r = i["r"]
            if i["full"]:
                status = "exited"
            elif i["n_sell"] > 0:
                status = "trimmed_" + i["trim_kind"]
            else:
                status = "kept"
            self.hold_rows.append({
                "D": D_date, "symbol": i["cid"], "ticker": r["ticker"], "status": status,
                "exit_codes": ";".join(i["codes"]), "units_open": i["units"], "px_now": px[k],
                "weight_open": i["units"] * px[k] / nav_open, "pe": r["pe"], "excess": i["excess"],
                "U0": i["U0"], "u_V": i["uV"], "u_C": i["uC"], "units_to_sell": i["n_sell"] if (i["full"] or i["n_sell"] > 0) else 0.0,
                "down0": r["down0"], "down1": r["down1"], "untested": ";".join(i["untested"]),
                "rank": rank_of.get(i["cid"]), "sector": ctx.sector(i["cid"]),
                "in_v1_universe": bool(r["member"])})
            self.hold_elig_rows.append(r)
        for c in placed:
            k = P.cids.get_loc(c)
            r = ctx.row(D, c)
            self.hold_rows.append({
                "D": D_date, "symbol": c, "ticker": r["ticker"], "status": "new", "exit_codes": "",
                # px_now is defined for the holdings at D's open (section 10, step 1); a stock
                # bought at D has none (Clarification of 12 October 2026)
                "units_open": 0.0, "px_now": None, "weight_open": 0.0, "pe": r["pe"], "excess": None,
                "U0": None, "u_V": None, "u_C": None, "units_to_sell": 0.0, "down0": r["down0"],
                "down1": r["down1"], "untested": "", "rank": rank_of.get(c), "sector": ctx.sector(c),
                "in_v1_universe": True})
        n_by_code = {c: 0 for c in ("E1", "E2", "E3", "E4", "E5")}
        for k in held_k:
            i = info[k]
            if i["full"] and i["codes"][0] in n_by_code:
                n_by_code[i["codes"][0]] += 1
        trims_val = sum(1 for k in held_k if (not info[k]["full"]) and info[k]["n_sell"] > 0 and info[k]["trim_kind"] == "valuation")
        trims_cap = sum(1 for k in held_k if (not info[k]["full"]) and info[k]["n_sell"] > 0 and info[k]["trim_kind"] == "cap")
        unt = [info[k]["untested"] for k in held_k]
        dec = {"D": D_date, "nav_open": nav_open, "cash_after_step0": cash0, "budget_B": B, "B_left": B_left,
               "n_eligible": len(ctx.eligible_in_rank_order(D)), **ctx.funnel(D),
               "holdings_at_open": len(held_k), **{f"exits_{c}": v for c, v in n_by_code.items()},
               "v_sales": sum(1 for k in held_k if info[k]["full"] and info[k]["codes"] == ["V"]),
               "trims_valuation": trims_val, "trims_cap": trims_cap, "kept": len(kept),
               "empty_slots": empty, "buys_placed": len(placed), "slots_left_empty": left_empty,
               "empty_reasons": ";".join(reasons),
               "untested_holdings": sum(1 for u in unt if u),
               **{f"untested_{x}": sum(1 for u in unt if x in u) for x in ("E1", "E2", "E3", "E4", "trim")},
               "holdings_outside_v1_universe": sum(1 for k in held_k if not info[k]["r"]["member"]),
               "cash_share_after_plan": B_left / nav_open}
        self.dec_rows.append(dec)
        self.prev_open_held = set(held_cids)
        # 12. execute at D's open
        self._process(t)

    # ---- the clock
    def run(self, plan: dict, ctx, t0: int, t_end: int) -> pd.DataFrame:
        P = self.P
        out = [(P.sessions[t0], "open", self.cash)]
        n = 0
        for t in range(t0, t_end + 1):
            _guard(P.sessions[t])
            if t > t0:                                    # cash dividends, on units held at the previous close
                d = float(np.nansum(self.ua * P.C[t] * P.dps[t]))
                self.cash += d
                self.divs += d
            if t == t_end:
                px = self._pxnow(t)
                held = self.ua > 0
                for k in np.flatnonzero(held & P.bz_traded[t] & ~P.traded[t]):
                    self.t4t.append((t, k, "end_valued_at_bz_open"))
                out.append((P.sessions[t], "open", self.cash + float(np.sum(self.ua[held] * px[held]))))
                break
            if t in plan:
                n += 1
                self.review(t, plan[t], ctx, n)
            elif self.orders:
                self._process(t)
            # 20 consecutive sessions without a trade (BZ counts as a trade): sold at the last close
            for k in np.flatnonzero((self.ua > 0) & (P.stale[t] >= FORCED_EXIT_SESSIONS)):
                o = self._order(k, "forced_exit", "sell_all", 0.0, self._last_review_t(t, plan), "")
                self._sell(t, k, 0.0, P.hmark[t, k] * P.C[t, k], o, "")
                self.forced.append((P.sessions[t], P.cids[k]))
            held = self.ua > 0
            for k in np.flatnonzero(held & P.bz_traded[t] & ~P.traded[t]):
                self.t4t.append((t, k, "valued_at_bz_close"))
            out.append((P.sessions[t], "close",
                        self.cash + float(np.sum(self.ua[held] * (P.hmark[t] * P.C[t])[held]))))
        self.pending_at_end = list(self.orders)
        return pd.DataFrame(out, columns=["date", "mark", "nav"])

    @staticmethod
    def _last_review_t(t, plan) -> int:
        return max((x for x in plan if x <= t), default=t)

    def spells(self, t_end: int) -> pd.DataFrame:
        P = self.P
        rows = list(self.closed_spells)
        px = self._pxnow(t_end)
        for k, (t0, e) in self.open_spells.items():
            rows.append((P.cids[k], t0, t_end, e, px[k], "open_at_end", ""))
        sp = pd.DataFrame(rows, columns=["cid", "t0", "t1", "entry_adj", "exit_adj", "exit_kind", "exit_code"])
        mx = []
        for r in sp.itertuples():
            k = P.cids.get_loc(r.cid)
            seg = P.hadj[r.t0:r.t1 + 1, k]            # adjusted closes (BZ close on a BZ session)
            mx.append(np.nanmax(seg) if np.isfinite(seg).any() else np.nan)
        sp["max_adj_close"] = mx
        return sp

    def t4t_frame(self, name: str) -> pd.DataFrame:
        P = self.P
        return pd.DataFrame([(name, P.sessions[t].date(), P.cids[k], ev) for t, k, ev in self.t4t],
                            columns=["book", "date", "cid", "event"])


# ================================================================ benchmarks
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
        '{(DATA_ROOT / 'data/external/nifty_tri.parquet').as_posix()}')
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
        FROM read_parquet('{(DATA_ROOT / 'data/indices').as_posix()}/*.parquet', union_by_name=true)
        WHERE index_name IN ({', '.join(repr(x) for x in parents)})
          AND CAST(date AS DATE) IN ({', '.join(f"DATE '{d.date()}'" for d in pdates)})""").df()
    px["date"] = pd.to_datetime(px["date"])
    assert not px.duplicated(["index_name", "date"]).any()
    pxi = px.set_index(["index_name", "date"])
    ep = pd.read_csv(DATA_ROOT / "data/external/nifty_price_endpoints.csv", dtype=str)
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



# ================================================================ records and the review context
def _fmt(v) -> str:
    """Section 13.1 formats: booleans true/false, missing empty, numbers by repr, dates ISO."""
    if v is None or v is pd.NaT or v is pd.NA:
        return ""
    if isinstance(v, (bool, np.bool_)):
        return "true" if v else "false"
    if isinstance(v, (float, np.floating)):
        return "" if math.isnan(v) else repr(float(v))
    if isinstance(v, (int, np.integer)):
        return str(int(v))
    if isinstance(v, pd.Timestamp):
        return v.date().isoformat()
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)


def write_csv(rows, cols: list, path: Path) -> int:
    import csv
    if isinstance(rows, pd.DataFrame):
        rows = rows.to_dict("records")
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(cols)
        n = 0
        for r in rows:
            w.writerow([_fmt(r.get(c)) for c in cols])
            n += 1
    return n


def elig_records(rows) -> list:
    """eligibility.csv's rows as recorded (Clarification of 12 October 2026): `icr` is written
    only in the fourth state of section 4.5 (state 'icr'), and `de` only for a usable balance
    sheet (section 4.6). The values the rules use (E2 on TTM_EBIT and FC+, E3 on a usable
    balance sheet) are untouched: this changes what is written, not what is computed."""
    if isinstance(rows, pd.DataFrame):
        rows = rows.to_dict("records")
    out = []
    for r in rows:
        r = dict(r)
        if r.get("icr_state") != "icr":
            r["icr"] = None
        u = r.get("bs_usable")
        if not (u is True or (isinstance(u, np.bool_) and bool(u))):
            r["de"] = None
        out.append(r)
    return out


ELIG_COLS = ["D", "symbol", "ticker", "c4_basis", "b_star", "L", "run", "missing_quarters", "ttm_p",
             "close", "close_date", "close_series", "N", "N_src", "mcap", "pe", "ttm_ebit", "fc_plus",
             "write_back", "icr_state", "icr", "bs_period_end", "de", "down0", "down1",
             "F0", "F1", "F2", "F3", "F4", "F5", "F6", "eligible",
             "owners_ttm_p", "owners_pe", "N_ref", "N_ref_over_N",
             "window_complete", "ttm_r", "ttm4_r", "ttm4_p", "bs_equity", "bs_debt", "bs_usable"]
RANK_COLS = ["D", "symbol", "ticker", "sector", "A_R", "A_P", "M", "g", "PEG", "r_A_R", "r_A_P", "r_M",
             "R1", "R2", "R3", "T", "rank", "pct_A_R", "pct_A_P", "pct_M", "S1", "S2", "S3", "S"]
HOLD_COLS = ["D", "symbol", "ticker", "status", "exit_codes", "units_open", "px_now", "weight_open", "pe",
             "excess", "U0", "u_V", "u_C", "units_to_sell", "down0", "down1", "untested", "rank", "sector",
             "in_v1_universe"]
FILL_COLS = ["date", "symbol", "ticker", "side", "units_adjusted", "price_adjusted", "value", "cost", "kind",
             "scheduled_date", "D", "status", "reason", "exit_code"]


class Ctx:
    """The information set of every review, as the simulator reads it."""

    def __init__(self, nse_sector, nse_scheme):
        self.nse_sector, self.nse_scheme = nse_sector, nse_scheme
        self.frames: dict = {}
        self._sk: dict = {}

    def sector(self, cid) -> str:
        v = self._sk.get(cid)
        if v is None:
            v = self._sk[cid] = sector_key(cid, self.nse_sector, self.nse_scheme)
        return v

    def row(self, D, cid) -> dict:
        fr = self.frames[pd.Timestamp(D)]
        r = fr["rows"].get(cid)
        if r is not None:
            return r
        r = fr["extra"].get(cid)
        if r is None:
            fg = v3_figures(cid, fr["core"], fr["bsd"], D)
            r = fr["extra"][cid] = eligibility_row(D, cid, fg, fr["mc"][cid], False)
            r["member"] = False
        return r

    def e5(self, D, cid) -> bool:
        """E5 (section 8): the latest known quarter in v1's basis has a period_end more than 200 days
        before D, or the stock has no known quarter in v1's basis (v1 rule 5 would fail it)."""
        pe = self.frames[pd.Timestamp(D)]["latest_pe"].get(cid)
        if pe is None or pd.isna(pe):
            return True
        return (pd.Timestamp(D) - pd.Timestamp(pe)).days > MAX_STALE_DAYS

    def eligible_in_rank_order(self, D) -> list:
        return self.frames[pd.Timestamp(D)]["ranks"]["symbol"].tolist()

    def rank_of(self, D) -> dict:
        r = self.frames[pd.Timestamp(D)]["ranks"]
        return dict(zip(r["symbol"], r["rank"]))

    def funnel(self, D) -> dict:
        return self.frames[pd.Timestamp(D)]["funnel"]


def review_stage(D, P: Panels, fin, lab, taxfill, ca, nse_sector, nse_scheme, keep_frame: bool):
    """One review's whole information set: v1's universe, every member's v3 figures and filters,
    the ranks. Returns (frame dict, member rows list, ranks DataFrame)."""
    core = fundamentals_core(fin, D, ca)
    df = review_frame(D, P, core, lab, taxfill, ca)
    bsd = balance_sheets_both(core["fa"], D, core["set_aside"])
    mc = df[["ticker", "close", "close_date", "close_series", "N", "N_src", "mcap", "N_ref",
             "N_ref_over_N"]].to_dict("index")
    members = [c for c in df.index[df["v1_univ"]]]
    rows = {}
    for cid in members:
        fg = v3_figures(cid, core, bsd, D)
        r = eligibility_row(D, cid, fg, mc[cid], True)
        r["member"] = True
        rows[cid] = r
    el = pd.DataFrame([r for r in rows.values() if r["eligible"]])
    sector_of = lambda c: sector_key(c, nse_sector, nse_scheme)  # noqa: E731
    if len(el):
        ranks = rank_eligible(el, sector_of)
    else:
        ranks = pd.DataFrame(columns=["symbol"] + RANK_COLS)
    # the funnels (section 13, decisions.csv): v1's rules in turn, then F1 to F6 in turn among members
    fun = {}
    m = pd.Series(True, index=df.index)
    for r_ in ("r1", "r2", "r3", "r4", "r5", "r6", "r7"):
        m = m & df[r_]
        fun[f"v1_after_{r_}"] = int(m.sum())
    mem = pd.DataFrame(list(rows.values()))
    fun["v1_universe"] = int(len(members))
    mm = pd.Series(True, index=mem.index) if len(mem) else pd.Series(dtype=bool)
    for f_ in ("F1", "F2", "F3", "F4", "F5", "F6"):
        mm = mm & mem[f_] if len(mem) else mm
        fun[f"v3_after_{f_}"] = int(mm.sum()) if len(mem) else 0
    frame = {"funnel": fun, "n_members": len(members), "rows": rows,
             "latest_pe": df["latest_pe"].to_dict(), "ranks": ranks,
             "mcap_member_bz": int(sum(1 for c in members if mc[c]["close_series"] == "BZ")),
             "mcap_member_missing": int(sum(1 for c in members if _isn(mc[c]["mcap"])))}
    if keep_frame:
        frame.update({"core": core, "bsd": bsd, "mc": mc, "extra": {}})
    return frame, rows, ranks


# ================================================================ metrics helpers
def perf(nav: pd.Series, dates: pd.Series) -> dict:
    r = nav.pct_change().dropna()
    yrs = (dates.iloc[-1] - dates.iloc[0]).days / 365.25
    cagr = (nav.iloc[-1] / nav.iloc[0]) ** (1 / yrs) - 1
    vol = r.std(ddof=1) * math.sqrt(252)
    dd = (nav / nav.cummax() - 1).min()
    return {"cagr": cagr, "vol": vol, "max_dd": dd, "total_return": nav.iloc[-1] / nav.iloc[0] - 1, "years": yrs}


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


def _git_hash() -> str | None:
    try:
        r = subprocess.run(["git", "log", "-1", "--format=%H", "--", "research/strategy/v3-spec.md"],
                           cwd=ROOT, capture_output=True, text=True, timeout=30)
        return r.stdout.strip() or None
    except Exception:  # noqa: BLE001
        return None


def main(argv=None) -> int:
    global DATA_ROOT, END_DATE, LATEST_NAMES
    ap = argparse.ArgumentParser()
    ap.add_argument("--frozen", default=None, help="read every input from this directory (15.2.4)")
    ap.add_argument("--out", default=str(ROOT / "data" / "backtest" / "v3_dev_check"))
    ap.add_argument("--agreement", action="store_true",
                    help="also run the agreement period (reviews to 15 Nov 2022, measured to the "
                         "open of 15 Feb 2023) into <out>/agreement")
    ap.add_argument("--run-final-test", action="store_true",
                    help="the one full run (spec 15.6): all 27 reviews, measured to the open of "
                         "18 September 2026, into <out>/full")
    ap.add_argument("--max-reviews", type=int, default=None, help="debug: stop the structure stage early")
    a = ap.parse_args(argv)
    t_start = time.time()
    if a.frozen:
        DATA_ROOT = Path(a.frozen).resolve()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    con = duck()
    sessions_all = all_sessions(con)
    Ds = review_dates(sessions_all)
    assert len(Ds) == N_REVIEWS and Ds[0] == pd.Timestamp(2020, 2, 17) and Ds[-1] == pd.Timestamp(2026, 8, 17), \
        [d.date() for d in Ds]
    END = sessions_all[sessions_all.searchsorted(END_ANCHOR)]
    Ds_ag = [d for d in Ds if d <= AGREE_LAST_ANCHOR + pd.Timedelta(days=5) and d < END]
    assert len(Ds_ag) == N_AGREE, len(Ds_ag)
    IS_END = END
    if a.run_final_test:
        if FULL_END not in set(sessions_all):
            raise RuntimeError(f"{FULL_END.date()} is not a session in the inputs")
        END, Ds_ag = FULL_END, list(Ds)
    log(f"{len(Ds)} reviews {Ds[0].date()} .. {Ds[-1].date()}; agreement reviews {len(Ds_ag)} to "
        f"{Ds_ag[-1].date()}, end (open) {END.date()}; inputs from {DATA_ROOT}")

    # ---- shared loading
    spells = build_ids(load_identity_keys(con), all_sessions(con, "1990-01-01"))
    D_last = Ds[-1]
    p_all = load_prices(con, D_last)
    ca_all, _dv = load_corpactions(con, D_last, spells)
    bz_all, _m = map_bz(load_bz(con, D_last), spells)
    sess_all = sessions_all[sessions_all <= D_last]
    P_all = Panels(p_all, spells, sess_all, ca_all, bz_all, light=True)
    log(f"structure panel {P_all.T} x {P_all.K} (prices to the session before {D_last.date()})")
    del p_all
    fin = load_financials(con, spells)
    log(f"quarter rule (Clarification 38) in the files: {QUARTER_RULE_INFO}")
    taxfill = taxonomy_fill(fin)
    _f = fin.dropna(subset=["company"]).sort_values("broadcast_dt")
    LATEST_NAMES = _f.groupby("cid")["company"].last().to_dict()
    lab, _names, _flagged = load_labels(con, spells)
    nse_sector, nse_scheme, sec_info = load_nse_sectors(spells)
    ctx = Ctx(nse_sector, nse_scheme)
    log(f"financial rows {len(fin):,}; v1 labelled stocks {lab['cid'].nunique()}; taxonomy backfill "
        f"{len(taxfill)}; NSE sector labels {sec_info['stock_ids_labelled']} stock ids "
        f"(conflicts {len(sec_info['stock_ids_with_conflicting_labels'])}, "
        f"legacy scheme {len(sec_info['legacy_scheme_stock_ids'])})")

    # ---- stage 1: the 27 reviews' structure (no returns)
    uni_rows, elig_rows, rank_frames, stage_info = [], [], [], {}
    for i, D in enumerate(Ds):
        if a.max_reviews and i >= a.max_reviews:
            break
        t0_ = time.time()
        keep = D in Ds_ag
        frame, rows, ranks = review_stage(D, P_all, fin, lab, taxfill, ca_all, nse_sector, nse_scheme, keep)
        if keep:
            ctx.frames[D] = frame
        else:
            ctx.frames[D] = {k: frame[k] for k in ("funnel", "ranks", "rows")}
        for cid, r in rows.items():
            uni_rows.append({"D": D.date(), "symbol": cid, "ticker": r["ticker"]})
            elig_rows.append(r)
        if len(ranks):
            ranks = ranks.copy()
            ranks["D"] = D.date()
            rank_frames.append(ranks)
        stage_info[str(D.date())] = {"members": len(rows), "eligible": int(len(ranks)),
                                     "members_latest_close_BZ": frame["mcap_member_bz"],
                                     "members_without_mcap": frame["mcap_member_missing"],
                                     **frame["funnel"]}
        fn = frame["funnel"]
        log(f"{D.date()}  members {len(rows):4d}  eligible {len(ranks):4d}  "
            f"F1-F6 funnel {fn['v3_after_F1']}/{fn['v3_after_F2']}/{fn['v3_after_F3']}/{fn['v3_after_F4']}/"
            f"{fn['v3_after_F5']}/{fn['v3_after_F6']}  ({time.time() - t0_:.0f}s)")
    rk_all = pd.concat(rank_frames, ignore_index=True) if rank_frames else pd.DataFrame(columns=RANK_COLS)
    n_u = write_csv(uni_rows, ["D", "symbol", "ticker"], out / "universe.csv")
    n_e = write_csv(elig_records(elig_rows), ELIG_COLS, out / "eligibility.csv")
    n_r = write_csv(rk_all, RANK_COLS, out / "ranks.csv")
    log(f"wrote {out}: universe.csv {n_u} rows, eligibility.csv {n_e} rows, ranks.csv {n_r} rows")
    summary = {"stage1": {"universe_rows": n_u, "eligibility_rows": n_e, "ranks_rows": n_r,
                          "reviews": [str(d.date()) for d in Ds]}}
    meta = {"spec_commit": _git_hash(), "inputs": _inputs_info(a.frozen)}

    if not (a.agreement or a.run_final_test):
        (out / "stage1_info.json").write_text(json.dumps({**meta, **summary, "by_review": stage_info},
                                                         indent=2, default=_js), encoding="utf-8")
        log(f"done in {time.time() - t_start:.0f}s")
        return 0
    return agreement_stage(a, con, out, Ds, Ds_ag, END, spells, fin, ctx, elig_rows, rk_all, uni_rows,
                           stage_info, summary, meta, sessions_all, t_start, IS_END)


def _inputs_info(frozen) -> dict:
    if not frozen:
        return {"mode": "live repo paths (development); not frozen"}
    fj = Path(frozen) / "freeze.json"
    if fj.exists():
        return {"mode": "frozen", "freeze_json_sha256": hashlib.sha256(fj.read_bytes()).hexdigest(),
                "paths": json.loads(fj.read_text(encoding="utf-8"))}
    return {"mode": "frozen directory without freeze.json", "dir": str(frozen)}


def agreement_stage(a, con, out: Path, Ds, Ds_ag, END, spells, fin, ctx, elig_rows, rk_all, uni_rows,
                    stage_info, summary, meta, sessions_all, t_start, IS_END=None) -> int:
    global END_DATE
    END_DATE = END
    ag = out / ("full" if a.run_final_test else "agreement")
    ag.mkdir(parents=True, exist_ok=True)
    sessions = sessions_all[sessions_all <= END]
    # the simulation panel: prices truncated at the open of END (its close and turnover never loaded)
    p = load_prices(con, END)
    ca, dv = load_corpactions(con, END, spells)
    bz, bz_map = map_bz(load_bz(con, END), spells)
    P = Panels(p, spells, sessions, ca, bz)
    P.add_dividends(dv)
    log(f"simulation panel {P.T} x {P.K}; BZ rows mapped {bz_map}; in panel {P.bz_info}; "
        f"split/bonus events {len(ca)}; dividends {len(dv)} (unparsed {dv.attrs['unparsed']})")
    t_idx = {D: P.idx(D) for D in Ds_ag}
    t0, t_end = t_idx[Ds_ag[0]], P.idx(END)
    book = BookV3(P)
    strat = book.run({t_idx[D]: D for D in Ds_ag}, ctx, t0, t_end)
    log(f"strategy simulated; fills {len(book.fills)}; forced exits {len(book.forced)}")

    # ---- universe equal-weight benchmark: v3's eligible list, equal-weighted at every review
    ew = BookEW(P, COST, name="universe_ew")
    plan = {}
    for D in Ds_ag:
        cids = ctx.eligible_in_rank_order(D)
        plan[t_idx[D]] = (P.cids.get_indexer(cids), len(cids))
    ew_nav = ew.run(plan, t0, t_end)
    nav = strat.rename(columns={"nav": "strategy"})
    nav["universe_ew"] = ew_nav["nav"].values
    assert (nav["date"].values == ew_nav["date"].values).all()
    bench, bench_info = benchmark_columns(con, nav[["date", "mark"]], sessions)
    nav = pd.concat([nav, bench], axis=1)[NAV_COLS]
    nav["date"] = pd.to_datetime(nav["date"]).dt.date

    # ---- records
    hold = pd.DataFrame(book.hold_rows)
    n_h = write_csv(hold, HOLD_COLS, ag / "holdings.csv")
    dec_cols = list(book.dec_rows[0].keys())
    n_d = write_csv(book.dec_rows, dec_cols, ag / "decisions.csv")
    n_f = write_csv(book.fills, FILL_COLS, ag / "fills.csv")
    n_n = write_csv(nav, NAV_COLS, ag / "nav.csv")
    ag_u = [r for r in uni_rows if pd.Timestamp(r["D"]) in set(Ds_ag)]
    ag_e = [r for r in elig_rows if pd.Timestamp(r["D"]) in set(Ds_ag)]
    seen = {(r["D"], r["symbol"]) for r in ag_e}
    ag_e += [r for r in book.hold_elig_rows if (r["D"], r["symbol"]) not in seen]
    ag_e.sort(key=lambda r: (r["D"], r["symbol"]))
    n_ue = write_csv(ag_u, ["D", "symbol", "ticker"], ag / "universe.csv")
    n_ee = write_csv(elig_records(ag_e), ELIG_COLS, ag / "eligibility.csv")
    rk_ag = rk_all[pd.to_datetime(rk_all["D"]).isin(Ds_ag)]
    n_rr = write_csv(rk_ag, RANK_COLS, ag / "ranks.csv")
    # v1-compatible companions
    tr = pd.DataFrame(book.trades, columns=["t", "k", "units", "price", "value", "cost", "reason"])
    trd = pd.DataFrame({"date": [P.sessions[t].date() for t in tr["t"]],
                        "symbol": [P.cids[k] for k in tr["k"]],
                        "side": np.where(tr["units"] > 0, "buy", "sell"), "units": tr["units"].abs(),
                        "price": tr["price"], "value": tr["value"].abs(), "cost": tr["cost"],
                        "reason": tr["reason"]})
    write_csv(trd, ["date", "symbol", "side", "units", "price", "value", "cost", "reason"], ag / "trades.csv")
    sp = book.spells(t_end)
    epi = pd.DataFrame({"symbol": sp["cid"], "entry": [P.sessions[t].date() for t in sp["t0"]],
                        "exit": [P.sessions[t].date() for t in sp["t1"]], "entry_px": sp["entry_adj"],
                        "exit_reason": [(f"{k}:{c}" if c else k) for k, c in zip(sp["exit_kind"], sp["exit_code"])],
                        "max_close": sp["max_adj_close"],
                        "doubled": sp["max_adj_close"] >= 2 * sp["entry_adj"],
                        "days_held": [(P.sessions[b] - P.sessions[a_]).days for a_, b in zip(sp["t0"], sp["t1"])]})
    write_csv(epi, list(epi.columns), ag / "episodes.csv")
    ro = [{"date": D.date(), "strategy_open": book.nav_open_at[t_idx[D]],
           "universe_ew_open": ew.nav_open_at[t_idx[D]]}
          for D in Ds_ag]
    write_csv(ro, ["date", "strategy_open", "universe_ew_open"], ag / "rebalance_open.csv")
    if a.run_final_test:
        # the full run passes the in-sample end as a review: the books' values at its open
        ise = [r for r in ro if r["date"] == IS_END.date()]
        write_csv([{"date": IS_END.date(), "strategy": ise[0]["strategy_open"],
                    "universe_ew": ise[0]["universe_ew_open"]}],
                  ["date", "strategy", "universe_ew"], ag / "in_sample_end_open.csv")
    else:
        write_csv([nav.iloc[-1].to_dict()], [c for c in NAV_COLS if c != "mark"],
                  ag / "in_sample_end_open.csv")
    t4t = pd.concat([book.t4t_frame("strategy"), ew.t4t_frame()], ignore_index=True)
    write_csv(t4t, list(t4t.columns), ag / "t4t_events.csv")
    tft = []
    for name, spl, bk in (("strategy", sp.assign(entry=sp["t0"], exit=sp["t1"]), book),):
        for r in spl.itertuples():
            k = P.cids.get_loc(r.cid)
            seg = slice(r.t0, r.t1 + 1)
            bzs = np.flatnonzero(P.bz_traded[seg, k] & ~P.traded[seg, k])
            if len(bzs):
                tft.append({"portfolio": name, "symbol": r.cid, "entry": P.sessions[r.t0].date(),
                            "exit": P.sessions[r.t1].date(), "exit_reason": r.exit_kind, "bz_sessions": len(bzs),
                            "first_bz": P.sessions[r.t0 + bzs[0]].date(), "last_bz": P.sessions[r.t0 + bzs[-1]].date(),
                            "exit_fill_in_bz": bool((not P.traded[r.t1, k]) and P.bz_traded[r.t1, k])})
    write_csv(tft, ["portfolio", "symbol", "entry", "exit", "exit_reason", "bz_sessions", "first_bz", "last_bz",
                    "exit_fill_in_bz"], ag / "trade_for_trade.csv")

    # ---- metrics and invariants
    dates = pd.to_datetime(nav["date"])
    m: dict = {**meta, "agreement_reviews": [str(d.date()) for d in Ds_ag], "end_open": str(END.date()),
               "stage1": summary["stage1"], "by_review": stage_info}
    m["strategy"] = perf(nav["strategy"], dates)
    m["universe_ew"] = perf(nav["universe_ew"], dates)
    m["strategy"].update({"start_rupees": START_CASH, "end_rupees": float(nav["strategy"].iloc[-1]),
                          "dividends_credited": book.divs, "costs_paid": book.costs, "min_cash": book.min_cash,
                          "forced_exits": [(str(d.date()), c) for d, c in book.forced],
                          "buys_scaled_down": book.scaled_buys,
                          "cancelled_pending_at_review": book.counts["cancelled_pending_at_review"],
                          "pending_orders_at_end": [{"symbol": P.cids[o["k"]], "kind": o["kind"], "mode": o["mode"],
                                                    "D": str(P.sessions[o["t_D"]].date())}
                                                   for o in book.pending_at_end]})
    fl = pd.DataFrame(book.fills)
    m["fills"] = {"filled_by_kind": {k: int(v) for k, v in fl[fl["status"] == "filled"]["kind"].value_counts().items()},
                  "cancelled_by_kind_and_reason": {f"{a_}|{b_}": int(v) for (a_, b_), v in
                                                   fl[fl["status"] == "cancelled"].groupby(["kind", "reason"]).size().items()}}
    m["holdings_by_review"] = {str(r["D"]): int(r["holdings_at_open"]) for r in book.dec_rows}
    m["bz_mapping"], m["bz_panel"] = bz_map, P.bz_info
    m["dropped_dividends"] = P.dropped_dividends
    assert book.min_cash >= -1e-6, f"cash went negative: {book.min_cash}"
    for D, g in hold[hold["status"].isin(["kept", "trimmed_valuation", "trimmed_cap", "new"])].groupby("D"):
        c = g["sector"].value_counts()
        assert (c <= SECTOR_CAP).all(), (D, c.to_dict())
    m["invariants"] = "ok: cash never negative; at most 4 per sector key among kept holdings and buys at each review"
    try:
        _guard(END + pd.Timedelta(days=1))
        m["holdout_guard_selftest"] = "FAILED: guard did not raise"
    except HoldoutViolation:
        m["holdout_guard_selftest"] = "ok: raises after " + str(END.date())
    m["ambiguities"] = AMBIGUITIES
    m["runtime_seconds"] = round(time.time() - t_start, 1)
    (ag / "metrics.json").write_text(json.dumps(m, indent=2, default=_js), encoding="utf-8")
    log(f"wrote {ag}: holdings {n_h}, decisions {n_d}, fills {n_f}, nav {n_n}, universe {n_ue}, "
        f"eligibility {n_ee}, ranks {n_rr} rows; filled by kind {m['fills']['filled_by_kind']}")
    return 0


AMBIGUITIES = [
    # --- the market cap (3.5) and share count
    "3.5 N: 'pit.fundamentals' shares at D' read as existing only for a stock that has a row in v1's basis "
    "(a known quarter in C4's basis). A stock with quarters only in the other basis has no N, so no market cap.",
    "3.5 'ex-date before D' and 'after the close's date' are exact date comparisons on the corporate-action "
    "ex_date (strict < D; strict > the close's date or, for N, the share count's broadcast date), not the "
    "session-mapped factor matrix.",
    "3.5 'within the 420-day window': the close's session date >= D - 420 calendar days (inclusive).",
    "13 'share count at C30b's reference ... with its ratio to N': reference = median of the previous four "
    "quarters' implied counts carried to the share count's filing, then to D's basis; ratio = reference / N; "
    "empty when the stock has no earlier quarter.",
    # --- basis, quarters, figures
    "3.3a case 3 (neither basis complete): b* = v1's basis, L = that basis's latest known quarter, and the stock "
    "fails F5 whether or not a window ending at that L would be complete. Figures (TTM_P, EBIT, balance sheet, "
    "DOWN flags) are still computed in that basis.",
    "3.3a: when v1's basis has no known quarter at all the stock has no figures (eligibility fields empty) and "
    "no basis-b* balance sheet.",
    "4.2 PBE: pbt_before_exceptional where present, else pbt - exceptional_items (a missing exceptional = 0); "
    "EBIT_k = PBE_k + FC_k and is missing when FC_k or both profit lines are missing.",
    "4.5: FC+ and the write-back flag are stored whenever the four FC lines exist (write-back: any FC_k < -1e7, "
    "strict). icr_state is one of unknown / write_back / no_finance_costs / icr, in the spec's order. The ICR "
    "value TTM_EBIT / FC+ is stored whenever both are known and FC+ > 0, including in the write_back state "
    "(E2 uses it there).",
    "4.6 usable balance sheet = its period_end is at most 400 days before D (no other condition); equity <= 0 "
    "then fails F3 and fires E3. D/E = (debt_long + debt_short, each missing line 0) / equity, empty if equity <= 0. "
    "The balance sheet is the latest period remaining AFTER the unit-scale screens (a screened latest period "
    "falls back to the previous one, as the v2 checker read it).",
    "4.8 M is defined only when TTM_R > 0 and TTM4_R > 0; A_R and A_P need R_0..R_7 and P_0..P_7.",
    "4.9 an unknown DOWN flag counts as false in F6 and E4 but is written as an EMPTY field (missing), not "
    "'false'; holdings.csv `untested` records the E4 case.",
    "13 eligibility: `L` is the period (YYYY-MM) of Q_0; `missing_quarters` lists Q_0..Q_8 absent in b* as "
    "YYYY-MM joined by ';' in increasing k (latest first); c4_basis / b_star are the words consolidated / "
    "standalone; `N_src` is shares_fix ('' when unscreened).",
    "13 owners' profit diagnostic: P_k x (EPS_k x N_k / profit_continuing_k) with EPS the basic EPS (eps_basic, "
    "the column shares_implied divides by), N_k the same quarter's standalone shares_implied; the factor is used "
    "only inside (0, 1.5] (missing inputs or profit_continuing = 0 give factor 1); the P/E on it is empty "
    "unless the owners' TTM profit is positive; consolidated basis only.",
    # --- ranking and records
    "6 tie-break symbol: the symbol of the stock's latest session before D (v1's ticker convention), not "
    "looked up at D itself; ties then by stock id.",
    "6 `sector` in ranks.csv / holdings.csv is the sector KEY (casefolded label or 'unclassified').",
    # --- the book
    "8 E5: a stock with no known quarter in v1's basis counts as 'no known quarter at all' and fires E5.",
    "12/13.1 `untested`: E1 when TTM_P is missing; E2 when TTM_EBIT or FC+ is missing; E3 from 15 Nov 2022 when "
    "no usable balance sheet exists; E4 when DOWN0 or DOWN1 is unknown and the stock was held at the previous "
    "review's open; `trim` when the market cap or TTM_P is missing (a known TTM_P <= 0 is E1, not untested), "
    "whatever the exits say.",
    "9.1 U0 is kept when a full-sale order does not fill (the units have not reached zero); it is cleared "
    "only when units reach zero (any route) or the P/E is known and <= 85.",
    "9.1/13.1 for a V sale u_V is recorded as 0.0 and u_C is empty; units_to_sell for a full sale is the units "
    "held at open; exit_codes for a V sale is 'V'.",
    "9.4 adjusted units are on the share basis of the last date the run loads (here the open of 15 Feb 2023), "
    "so `units_adjusted` and `price_adjusted` in fills.csv change basis in a longer run; values do not.",
    "13 fills.csv: columns follow the v2 file (date, symbol, ticker, side, units_adjusted, price_adjusted, value, "
    "cost, kind, scheduled_date, D) plus `status` (filled/cancelled), `reason` (cancellation reason, or bz_open "
    "for a sale filled at a BZ open) and `exit_code`. A cancellation row carries the cancelled order's kind and is "
    "dated the session that ended its window (D + 5 sessions); pending orders cancelled at a review (cannot "
    "fire) are dated that review.",
    "10 step 10: expected proceeds are units to sell x px_now x 0.998 (a literal), and 11's B_left / 1.002 uses "
    "the literal 1.002; filling stops at the first V < NAV_open / 36 (strict).",
    "13 decisions.csv columns are this checker's names (v1_after_r1..r7 cumulative, v3_after_F1..F6 cumulative "
    "among members, exits_E1..E5 by the reported reason, v_sales, trims_valuation, trims_cap, kept, "
    "empty_slots, buys_placed, slots_left_empty, empty_reasons, untested_*, cash_share_after_plan = B_left / "
    "NAV_open); the spec names the contents, not the columns.",
    "14.6 universe_ew: v3's eligible list equal-weighted through the v1 engine (rupee targets NAV/N, sells "
    "first, 5-session window, forced exit), rebalanced at every review.",
]


if __name__ == "__main__":
    raise SystemExit(main())
