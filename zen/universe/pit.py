"""Point-in-time universe for strategy v1 (research/strategy/v1-spec.md).

Everything in this module answers one question: what could an investor have
known at the open of decision date D? The rules, restated so they can be
audited against the code:

  Information set at D
    fundamentals   broadcast_dt strictly before D 00:00. period_end is never
                   used to decide what was known.
    prices         every session strictly before D, i.e. up to and including
                   the close of the previous session.
    corp actions   split/bonus factors with ex_date strictly before D. A price
                   series is adjusted relative to the last session before D,
                   so no future factor ever enters a ratio.

  prices.prev_close is never read. It is unadjusted for splits and bonuses
  (median implied return on an ex-date is -52.8%), and a ratio built on it is
  a fiction.

  Survivorship: the universe is built only from what actually traded in the
  bhavcopy archive. nse_list.json is TODAY's list (3,816 filing rows from
  2025-26). It is loaded for diagnostics only and no longer decides rule 4:
  a company's 2025 filing format is information from after D (spec
  Clarification 28). Lenders are caught point-in-time by the XBRL taxonomy of
  their own filings known before D (see `lender_flags`).

  Data-quality screens (Clarification 30), point-in-time, on filings known
  before D only: a filing whose rupee lines are on the wrong unit scale is
  treated as not filed (`scale_errors`), and a share count 30x away from the
  company's recent counts is replaced (`fundamentals`).

The module is split in two layers:

  Snapshot    raw point-in-time inputs at D (price window, adjustment factors,
              fundamentals by quarter, labels), loaded with a handful of
              queries and no correlated subqueries.
  universe()  the seven spec rules applied in order, with a funnel counting
              how many names each rule removed.

The composite (zen/signals/composite.py) builds its measures from the same
Snapshot, so the universe and the ranks can never disagree about what was
known.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from zen.universe.identity import Identity

log = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parents[2]
DB_PATH = REPO / "data" / "zen.duckdb"
NSE_LIST = REPO / "nse_list.json"

SCRATCH_DUCK = Path(
    r"C:\Users\mnsha\AppData\Local\Temp\claude\C--Users-mnsha-OneDrive-Desktop-Gostack"
    r"\196360f9-eded-41ee-bd9a-762d776d6462\scratchpad\duck")

# ---------------------------------------------------------------- spec constants
TURNOVER_MIN = 20e5            # Rs 20 lakh median daily turnover
TURNOVER_WINDOW = 60           # sessions
RECENT_TRADE_SESSIONS = 5      # traded in at least one of the last 5 sessions
MIN_SESSIONS_365D = 200        # sessions traded in the previous 365 calendar days
MIN_CONSEC_QUARTERS = 4
MAX_STALENESS_DAYS = 200       # latest quarter's period_end vs D
MOM_SKIP = 21
MOM_LOOKBACK = 252
VOL_WINDOW = 252
STAB_MAX_QUARTERS = 8
STAB_MIN_QUARTERS = 4
PRICE_LOOKBACK_DAYS = 420      # calendar days loaded; must cover 252 sessions + 365 days

# NSE industry labels (announcements.industry) that denote a lender or insurer.
LENDER_LABELS = {"Banks", "Finance", "Finance - Housing", "Financial Institution",
                 "Insurance"}
# Company-name fallback for lenders the other sources cannot see (delisted before
# 2022, never carried a label). Deliberately narrow: "capital" and "investment"
# are left out because they also name industrial holding companies. Applied
# only to companies with no industry label at all (Clarification 29).
LENDER_NAME = re.compile(
    r"\b(bank|banking|finance|financial|financiers?|fincorp|finserv|finvest|"
    r"insurance|assurance|leasing)\b", re.I)

# XBRL taxonomy of a filing, encoded in its document name
# (.../xbrl/INDAS_..., .../xbrl/INTEGRATED_FILING_NBFC_INDAS_..., BANKING_, GI_, LI_).
# Known at broadcast_dt, so it is a point-in-time lender flag (Clarification 28).
TAXONOMY_RX = r"/xbrl/(?:INTEGRATED_FILING_)?([A-Z_]+?)_\d"
LENDER_TAXONOMIES = {"NBFC_INDAS", "BANKING", "GI", "LI"}
# NSE's NBFC taxonomy first appears with the Dec-2019 quarter's results
# (first NBFC_INDAS document broadcast Jan 2020; none before in the archive).
# Before it existed an NBFC filed as INDAS or NONINDAS, so those filings say
# nothing either way about lender status.
NBFC_TAXONOMY_START = pd.Timestamp("2020-01-01")
TAXONOMY_QUARTERS = 4        # latest known quarters whose filings vote

# Data-quality screens (Clarification 30).
SCALE_BAND = 30.0            # a unit error moves every rupee line 100x or more
SCALE_NEIGHBOURS = 8         # nearest other known quarters a filing is compared with
SHARES_REF_QUARTERS = 4      # earlier known quarters behind the share-count reference
SHARES_OTHER_BASIS_TOL = 1.5 # the other basis is used when it is this close to the reference


# ---------------------------------------------------------------- connections
def connect(path: Path = DB_PATH, read_only: bool = True) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(str(path), read_only=read_only)
    configure(con)
    return con


def configure(con) -> None:
    """Scratch spill directory and memory cap, per the build rules."""
    SCRATCH_DUCK.mkdir(parents=True, exist_ok=True)
    try:
        con.execute(f"SET temp_directory='{SCRATCH_DUCK.as_posix()}'")
        con.execute("SET memory_limit='8GB'")
    except Exception as e:          # a read-only or in-memory copy may refuse; not fatal
        log.debug("duckdb config skipped: %s", e)


# ---------------------------------------------------------------- calendar
def sessions(con) -> pd.DatetimeIndex:
    """Every NSE session in the archive (dates only; no prices are read)."""
    d = con.execute("SELECT DISTINCT date FROM prices ORDER BY date").df()["date"]
    return pd.DatetimeIndex(pd.to_datetime(d))


ANCHORS = [(2, 15), (6, 1), (8, 15), (11, 15)]


def decision_date(cal: pd.DatetimeIndex, year: int, month: int, day: int) -> pd.Timestamp:
    """First session on or after the anchor date."""
    anchor = pd.Timestamp(year, month, day)
    i = cal.searchsorted(anchor, side="left")
    if i >= len(cal):
        raise ValueError(f"no session on or after {anchor.date()} in the archive")
    return cal[i]


def decision_dates(cal: pd.DatetimeIndex, first=(2019, 2), last=(2022, 11)) -> list[pd.Timestamp]:
    out = []
    for y in range(first[0], last[0] + 1):
        for m, d in ANCHORS:
            if (y, m) < first or (y, m) > last:
                continue
            out.append(decision_date(cal, y, m, d))
    return out


# ---------------------------------------------------------------- corp actions
_SPLIT_LENIENT = re.compile(
    r"(?:from\s+)?r[se]\.?\s*(\d+(?:\.\d+)?)\s*/?-?\s*(?:per\s*(?:share)?)?\s*to\s+"
    r"r[se]\.?\s*(\d+(?:\.\d+)?)", re.I)
_BONUS_LENIENT = re.compile(r"bonus\s*-?\s*(\d+)\s*:\s*(\d+)", re.I)
_NOT_EQUITY_BONUS = re.compile(r"debenture|ncrps|preference|\bncd", re.I)


def _lenient_factor(action: str, subject: str) -> float | None:
    """Second-chance parse for split/bonus rows the ingest parser left null.

    Handles 'Face Value Split Rs 10 To Rs 1' and 'Bonus- 1:2'. Bonus debentures
    and bonus preference shares are not equity bonuses and stay unadjusted.
    """
    s = subject or ""
    if action == "split":
        m = _SPLIT_LENIENT.search(s)
        if m:
            old, new = float(m.group(1)), float(m.group(2))
            if 0 < new < old:
                return new / old
    if action == "bonus" and not _NOT_EQUITY_BONUS.search(s):
        m = _BONUS_LENIENT.search(s)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a > 0 and b > 0:
                return b / (a + b)
    return None


def split_factors(con, before: date | pd.Timestamp | None = None,
                  through: date | pd.Timestamp | None = None,
                  ids: Identity | None = None) -> pd.DataFrame:
    """Price multipliers per (symbol, ex_date), collapsed by product.

    `before` keeps ex_date < before (the point-in-time cut for a signal at D);
    `through` keeps ex_date <= through (the engine's cut at the in-sample end).
    Same-day split + bonus pairs exist, so rows are multiplied together per
    (symbol, ex_date) before any cumulative product is taken.
    """
    q = ("SELECT symbol, ex_date, action, subject, factor FROM corpactions "
         "WHERE action IN ('split','bonus')")
    params: list = []
    if before is not None:
        q += " AND ex_date < ?"
        params.append(pd.Timestamp(before).date())
    if through is not None:
        q += " AND ex_date <= ?"
        params.append(pd.Timestamp(through).date())
    ca = con.execute(q, params).df()
    if ca.empty:
        return pd.DataFrame(columns=["symbol", "ex_date", "factor"])
    miss = ca["factor"].isna()
    if miss.any():
        ca.loc[miss, "factor"] = np.array(
            [_lenient_factor(a, s) for a, s in
             zip(ca.loc[miss, "action"], ca.loc[miss, "subject"])], dtype=float)
    ca = ca[ca["factor"].notna() & (ca["factor"] > 0)].copy()
    if ids is not None and not ca.empty:
        ca["symbol"] = ids.for_events(ca, "symbol", "ex_date")     # stock id (Clarification 23)
    # Revised-purpose duplicates carry the same subject stem and factor; a
    # genuine same-day pair (split + bonus) differs in action. Deduplicate on
    # (symbol, ex_date, action, factor) so a re-announced bonus is not applied
    # twice, then multiply what remains.
    ca = ca.drop_duplicates(subset=["symbol", "ex_date", "action", "factor"])
    out = (ca.groupby(["symbol", "ex_date"], as_index=False)["factor"].prod())
    out["ex_date"] = pd.to_datetime(out["ex_date"])
    return out.sort_values(["symbol", "ex_date"]).reset_index(drop=True)


def cumulative_factor(frame: pd.DataFrame, factors: pd.DataFrame,
                      date_col: str = "date") -> np.ndarray:
    """cum(d) = product of factors with ex_date > d, per symbol.

    Computed with one merge_asof (forward, strict), never a correlated query.
    Multiplying a raw price at d by cum(d) expresses it in the share units of
    the latest ex-date in `factors`.
    """
    if frame.empty:
        return np.ones(0)
    if factors.empty:
        return np.ones(len(frame))
    f = factors.sort_values(["symbol", "ex_date"]).copy()
    # reverse cumulative product within symbol: prod of factors at or after this event
    f["cum"] = (f.iloc[::-1].groupby("symbol")["factor"].cumprod()).iloc[::-1]
    left = frame[["symbol", date_col]].copy()
    left["_row"] = np.arange(len(left))
    left[date_col] = pd.to_datetime(left[date_col])
    left = left.sort_values(date_col)
    right = f[["symbol", "ex_date", "cum"]].sort_values("ex_date")
    m = pd.merge_asof(left, right, left_on=date_col, right_on="ex_date", by="symbol",
                      direction="forward", allow_exact_matches=False)
    out = np.ones(len(frame))
    out[m["_row"].to_numpy()] = m["cum"].fillna(1.0).to_numpy()
    return out


# ---------------------------------------------------------------- labels
def industry_labels(con, before: date | pd.Timestamp | None = None,
                    ids: Identity | None = None) -> pd.Series:
    """Latest NSE industry label per symbol from filings broadcast before `before`.

    With before=None it returns the FIRST label the archive ever records for
    each symbol: the static backfill used for decision dates earlier than the
    announcements archive (which starts Jan 2022). No symbol's label changes
    anywhere in 2022-2026, so the label behaves as a static classification.
    """
    if ids is not None and not ids.empty:
        q = ("SELECT symbol, an_dt, industry FROM announcements "
             "WHERE industry IS NOT NULL AND industry <> '-'")
        params = []
        if before is not None:
            q += " AND an_dt < ?"
            params.append(pd.Timestamp(before).to_pydatetime())
        a = con.execute(q, params).df()
        if a.empty:
            return pd.Series(dtype=object)
        a["symbol"] = ids.for_events(a, "symbol", "an_dt")
        a = a.sort_values(["an_dt", "industry"])
        g = a.groupby("symbol")["industry"]
        return g.first() if before is None else g.last()
    if before is None:
        q = ("SELECT symbol, arg_min(industry, an_dt) AS industry FROM announcements "
             "WHERE industry IS NOT NULL AND industry <> '-' GROUP BY symbol")
        df = con.execute(q).df()
    else:
        q = ("SELECT symbol, arg_max(industry, an_dt) AS industry FROM announcements "
             "WHERE industry IS NOT NULL AND industry <> '-' AND an_dt < ? GROUP BY symbol")
        df = con.execute(q, [pd.Timestamp(before).to_pydatetime()]).df()
    return df.set_index("symbol")["industry"] if not df.empty else pd.Series(dtype=object)


def nse_list_lenders(path: Path = NSE_LIST) -> set[str]:
    """Symbols TODAY's NSE list flags as bank ('B') or financial ('F') format filers.

    Diagnostics only (Clarification 28): the list is 2025-26 filing rows, so it
    says how a company files today, not what it was at D."""
    if not path.exists():
        return set()
    rows = json.loads(path.read_text(encoding="utf-8"))
    return {r["symbol"] for r in rows if r.get("bank") in ("B", "F") and r.get("symbol")}


def company_names(con, ids: Identity | None = None) -> pd.Series:
    """Company name per stock id: the latest `company` in the financials archive.

    The archive stores the name current when it was downloaded (every
    ZYDUSLIFE filing from 2018 reads 'Zydus Lifesciences Limited', a name
    Cadila Healthcare took in 2022), so the name is a static input, not a
    point-in-time one (Clarification 29)."""
    f = con.execute("SELECT symbol, broadcast_dt, company FROM financials "
                    "WHERE company IS NOT NULL").df()
    if f.empty:
        return pd.Series(dtype=object)
    if ids is not None and not ids.empty:
        f["symbol"] = ids.for_events(f, "symbol", "broadcast_dt")
    return (f.sort_values(["broadcast_dt", "company"])
             .drop_duplicates("symbol", keep="last").set_index("symbol")["company"])


@dataclass
class StaticLabels:
    """Static classification inputs, declared explicitly.

    These are the only inputs not re-derived from the connection at D, so the
    leak test cannot see them and they are listed here instead:

      backfill_industry  first-ever NSE industry label per symbol, used only
                         where no label had been published before D.
      taxonomy_fill      stocks whose first quarters filed under NSE's NBFC
                         taxonomy (from Jan 2020) were mostly in a lender
                         taxonomy; used only at a D before any such filing
                         of the stock was known (Clarification 28).
      names              company name per stock id, as stored at download
                         time (Clarification 29); feeds only the lender-name
                         net for companies with no label at all.
      nse_lenders        today's bank/financial flags from nse_list.json.
                         Diagnostics only: it no longer decides rule 4
                         (Clarification 28).
      ids                stock identity across renames (identity.py): which
                         (symbol, ISIN) spells are one company. Keys only,
                         never a price.

    All are classifications, not returns or fundamentals.
    """
    backfill_industry: pd.Series = field(default_factory=lambda: pd.Series(dtype=object))
    nse_lenders: set = field(default_factory=set)
    ids: Identity = field(default_factory=Identity.trivial)
    names: pd.Series = field(default_factory=lambda: pd.Series(dtype=object))
    taxonomy_fill: set = field(default_factory=set)

    @classmethod
    def load(cls, con) -> "StaticLabels":
        ids = Identity.build(con)
        return cls(backfill_industry=industry_labels(con, None, ids),
                   nse_lenders=ids.for_today(nse_list_lenders()), ids=ids,
                   names=company_names(con, ids),
                   taxonomy_fill=taxonomy_backfill(con, ids))


# ---------------------------------------------------------------- snapshot
@dataclass
class Snapshot:
    D: pd.Timestamp
    cal: pd.DatetimeIndex          # sessions strictly before D, ascending
    px: pd.DataFrame               # symbol (= stock id), ticker, date, isin_code, series, open, close, turnover, adj_close
    quarters: pd.DataFrame         # latest revision per (symbol, consolidated, period_end),
                                   # mis-scaled filings removed (Clarification 30)
    bankfmt: set                   # symbols whose latest known filing has no revenue tag
    industry_pit: pd.Series
    factors: pd.DataFrame          # split/bonus factors with ex_date < D
    px_prior: pd.DataFrame = field(default_factory=pd.DataFrame)
                                   # per stock, the last row BEFORE the loaded window
                                   # (symbol, date, adj_close): Clarification 2 takes the
                                   # last close on or before session 252 however old it is
    taxonomy: pd.DataFrame = field(default_factory=pd.DataFrame)
                                   # per stock: n_new (filings for its latest 4 known
                                   # quarters broadcast after NBFC_TAXONOMY_START) and
                                   # n_lender (those in a lender taxonomy)
    scale_dropped: pd.DataFrame = field(default_factory=pd.DataFrame)
                                   # filings treated as not filed (Clarification 30a)
    lenders: pd.DataFrame = field(default_factory=pd.DataFrame)
                                   # set by universe(): rule-4 flags for the rule-3 survivors


def build_snapshot(con, D, static: "StaticLabels | None" = None) -> Snapshot:
    """Point-in-time inputs at D. Every frame is keyed by stock id (Clarification
    23): `symbol` below is the id, and `ticker` in px is the symbol traded that day."""
    D = pd.Timestamp(D).normalize()
    ids = static.ids if static is not None else Identity.trivial()
    lo = (D - timedelta(days=PRICE_LOOKBACK_DAYS)).date()
    cal = pd.DatetimeIndex(pd.to_datetime(con.execute(
        "SELECT DISTINCT date FROM prices WHERE date >= ? AND date < ? ORDER BY date",
        [lo, D.date()]).df()["date"]))
    px = con.execute(
        "SELECT symbol, date, isin_code, series, open, close, turnover FROM prices "
        "WHERE date >= ? AND date < ? AND close > 0", [lo, D.date()]).df()
    px["date"] = pd.to_datetime(px["date"])
    px["ticker"] = px["symbol"]
    px["symbol"] = ids.for_prices(px)
    # two symbols of one stock on one day (a rename overlap): keep the traded one
    px = (px.sort_values(["turnover", "ticker"], ascending=[False, True])
            .drop_duplicates(["symbol", "date"]))
    factors = split_factors(con, before=D, ids=ids)
    px["cum"] = cumulative_factor(px, factors)
    px["adj_close"] = px["close"] * px["cum"]
    px = px.sort_values(["symbol", "date"]).reset_index(drop=True)

    # Last close before the window, per (symbol, ISIN): one GROUP BY, no
    # correlated subquery. Used only for the momentum close at session 252
    # when the stock had not traded since before the window (e.g. 8KMILES,
    # halted Sep 2019 to Jan 2021, at D = Aug 2021).
    prior = con.execute(
        "SELECT symbol, isin_code, max(date) AS date, arg_max(close, date) AS close "
        "FROM prices WHERE date < ? AND close > 0 GROUP BY 1, 2", [lo]).df()
    if not prior.empty:
        prior["date"] = pd.to_datetime(prior["date"])
        prior["symbol"] = ids.for_prices(prior)
        prior = prior.sort_values("date").drop_duplicates("symbol", keep="last")
        prior["adj_close"] = prior["close"] * cumulative_factor(prior, factors)
        prior = prior[["symbol", "date", "adj_close"]].reset_index(drop=True)

    cutoff = D.to_pydatetime()      # D 00:00; broadcast_dt must be strictly earlier
    fin = con.execute(
        "SELECT symbol, period_end, broadcast_dt, consolidated, revenue, total_income, "
        "employee_cost, ebitda, profit_normalised, shares_implied, other_income, "
        "regexp_extract(xbrl_url, ?, 1) AS taxonomy FROM financials "
        "WHERE broadcast_dt < ?", [TAXONOMY_RX, cutoff]).df()
    fin["period_end"] = pd.to_datetime(fin["period_end"])
    fin["broadcast_dt"] = pd.to_datetime(fin["broadcast_dt"])
    fin["symbol"] = ids.for_events(fin, "symbol", "broadcast_dt")
    # A filing that reports no income statement at all is not a revision of
    # the quarter's income statement. Since the parser fix of 2026-09-24 this
    # is mostly a filing that tagged only a half-year or a year, whose figures
    # are refused (spec Clarification 38). If such a filing was broadcast after
    # a valid quarterly one, taking "the latest" would replace real figures
    # with nothing, which happened for 13 company-quarters. So these rows are
    # set aside before the latest revision is chosen. Banks keep other_income,
    # so they are unaffected.
    income = ["revenue", "total_income", "other_income", "employee_cost",
              "ebitda", "profit_normalised"]
    fin = fin[fin[income].notna().any(axis=1)]
    # Latest revision known before D per (symbol, basis, quarter).
    fin = (fin.sort_values(["symbol", "consolidated", "period_end", "broadcast_dt"])
              .drop_duplicates(["symbol", "consolidated", "period_end"], keep="last"))
    fin["q"] = fin["period_end"].dt.year * 4 + (fin["period_end"].dt.month - 1) // 3
    # Bank/insurer taxonomy: no RevenueFromOperations tag but figures present.
    latest_any = fin.sort_values("broadcast_dt").drop_duplicates("symbol", keep="last")
    bankfmt = set(latest_any.loc[latest_any["revenue"].isna() &
                                 latest_any["other_income"].notna(), "symbol"])
    taxonomy = taxonomy_votes(fin)
    # Mis-scaled filings are treated as not filed (Clarification 30a).
    bad = scale_errors(fin)
    dropped = fin[bad].copy()
    fin = fin[~bad]
    return Snapshot(D=D, cal=cal, px=px, quarters=fin.reset_index(drop=True),
                    bankfmt=bankfmt,
                    industry_pit=industry_labels(con, D, ids), factors=factors,
                    px_prior=prior, taxonomy=taxonomy,
                    scale_dropped=dropped.reset_index(drop=True))


def taxonomy_votes(fin: pd.DataFrame) -> pd.DataFrame:
    """Lender-taxonomy vote per stock at D (Clarification 28).

    The filings (both bases, latest revision) for the stock's latest
    TAXONOMY_QUARTERS known quarters vote, counting only those broadcast after
    the NBFC taxonomy existed. A vote rather than one filing, because the
    archive has single mis-tagged quarters both ways: India Nippon Electricals
    (auto electricals) filed Mar-2022 in the NBFC taxonomy, and Capri Global
    Capital (an NBFC) filed Sep-2020 as INDAS.
    """
    if fin.empty:
        return pd.DataFrame(columns=["n_new", "n_lender"])
    lq = fin.groupby("symbol")["q"].transform("max")
    w = fin[(fin["q"] > lq - TAXONOMY_QUARTERS) &
            (fin["broadcast_dt"] >= NBFC_TAXONOMY_START)]
    out = pd.DataFrame(index=pd.Index(fin["symbol"].unique(), name="symbol"))
    g = w.assign(_l=w["taxonomy"].isin(LENDER_TAXONOMIES)).groupby("symbol")["_l"]
    out["n_new"] = g.size().reindex(out.index).fillna(0).astype(int)
    out["n_lender"] = g.sum().reindex(out.index).fillna(0).astype(int)
    return out


def taxonomy_backfill(con, ids: Identity | None = None) -> set:
    """Stocks whose first TAXONOMY_QUARTERS quarters filed after the NBFC
    taxonomy existed were at least half in a lender taxonomy.

    Static, like the industry backfill of Clarification 8, and used the same
    way: only at a D where none of the stock's filings for its latest known
    quarters was broadcast after NBFC_TAXONOMY_START, i.e. decision dates in
    2019 and early 2020, before any NBFC could file as one (Clarification 28).
    """
    f = con.execute(
        "SELECT symbol, period_end, broadcast_dt, consolidated, "
        "regexp_extract(xbrl_url, ?, 1) AS taxonomy FROM financials "
        "WHERE broadcast_dt >= ?", [TAXONOMY_RX, NBFC_TAXONOMY_START.to_pydatetime()]).df()
    if f.empty:
        return set()
    f["broadcast_dt"] = pd.to_datetime(f["broadcast_dt"])
    if ids is not None and not ids.empty:
        f["symbol"] = ids.for_events(f, "symbol", "broadcast_dt")
    f["period_end"] = pd.to_datetime(f["period_end"])
    # the first version filed of each (stock, basis, quarter)
    f = (f.sort_values("broadcast_dt")
          .drop_duplicates(["symbol", "consolidated", "period_end"], keep="first"))
    first_q = (f[["symbol", "period_end"]].drop_duplicates()
                .sort_values(["symbol", "period_end"])
                .groupby("symbol").head(TAXONOMY_QUARTERS))
    f = f.merge(first_q, on=["symbol", "period_end"])
    v = f.assign(_l=f["taxonomy"].isin(LENDER_TAXONOMIES)).groupby("symbol")["_l"].agg(["sum", "size"])
    return set(v.index[(2 * v["sum"] >= v["size"]).to_numpy()])


# ---------------------------------------------------------------- data quality
def _band(x) -> np.ndarray:
    """True where a positive ratio is SCALE_BAND x or more away from 1."""
    x = np.asarray(x, dtype=float)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.isfinite(x) & (x > 0) & ((x >= SCALE_BAND) | (x <= 1 / SCALE_BAND))


def _pairwise_scale_break(rev, shares, emp) -> np.ndarray:
    """m[i, j] True when filings i and j are on different unit scales.

    A unit error (a filing in lakhs tagged as rupees, and so on) multiplies
    every rupee line by the same power of ten but leaves EPS alone, so the
    implied share count (profit / EPS) moves with it. A genuine collapse in
    revenue (a lockdown quarter, a demerger) leaves the share count where it
    was. So all three must break together, in the same direction: revenue,
    implied shares, and employee cost where both filings report it.
    """
    pos = lambda v: np.where(np.isfinite(v) & (v > 0), v, np.nan)
    rev, shares, emp = pos(rev), pos(shares), pos(emp)
    with np.errstate(invalid="ignore", divide="ignore"):
        rr = rev[:, None] / rev[None, :]
        sr = shares[:, None] / shares[None, :]
        er = emp[:, None] / emp[None, :]
    up = rr > 1
    same = lambda x: _band(x) & ((x > 1) == up)
    emp_ok = np.isnan(er) | same(er)
    return _band(rr) & same(sr) & emp_ok


def scale_errors(fin: pd.DataFrame) -> np.ndarray:
    """Boolean mask over `fin` (one row per symbol, basis, quarter, all known
    before D): the filing is on a different unit scale from most of its
    neighbours (Clarification 30a).

    Each filing is compared with up to SCALE_NEIGHBOURS nearest other known
    quarters of the same company and basis, earlier or later (all of them were
    broadcast before D, so nothing after D is read), among those with positive
    revenue and implied shares, so the test can be decided. It is flagged when
    it breaks scale with more than half of them. A majority, not a median of
    levels, so a mis-scaled first filing (TCI Mar-2018 standalone) cannot drag
    every later filing out of line. With no other quarter in its basis, the
    other basis's filing for the same quarter is the one neighbour.
    """
    n = len(fin)
    out = np.zeros(n, dtype=bool)
    if n == 0:
        return out
    f = fin.reset_index(drop=True)
    rev = f["revenue"].where(f["revenue"].notna(), f["total_income"]).to_numpy(dtype=float)
    sh = f["shares_implied"].to_numpy(dtype=float)
    emp = f["employee_cost"].to_numpy(dtype=float)
    q = f["q"].to_numpy()
    pos = np.arange(n)
    groups = f.groupby(["symbol", "consolidated"], sort=False).indices
    for (sym, cons), ix in groups.items():
        if len(ix) < 2:
            continue
        ix = ix[np.argsort(q[ix], kind="stable")]
        brk = _pairwise_scale_break(rev[ix], sh[ix], emp[ix])
        # only neighbours the test can decide on: positive revenue and shares
        cmp_ok = (np.isfinite(rev[ix]) & (rev[ix] > 0) &
                  np.isfinite(sh[ix]) & (sh[ix] > 0))
        qq = q[ix]
        for a in range(len(ix)):
            if not cmp_ok[a]:
                continue
            d = np.abs(qq - qq[a]).astype(float)
            d[a] = np.inf
            d[~cmp_ok] = np.inf
            # nearest first; an earlier quarter wins a tie
            order = np.lexsort((qq, d))[:SCALE_NEIGHBOURS]
            order = order[np.isfinite(d[order])]
            if len(order) and brk[a, order].sum() * 2 > len(order):
                out[ix[a]] = True
    # A filing that is its basis's only quarter: compare with the other basis.
    lone = f.groupby(["symbol", "consolidated"])["q"].transform("size").to_numpy() == 1
    if lone.any():
        key = f[["symbol", "consolidated", "q"]].assign(_i=pos)
        other = key.assign(consolidated=~key["consolidated"])
        m = key[lone].merge(other, on=["symbol", "consolidated", "q"], suffixes=("", "_o"))
        if not m.empty:
            i, j = m["_i"].to_numpy(), m["_i_o"].to_numpy()
            with np.errstate(invalid="ignore", divide="ignore"):
                rr = rev[i] / rev[j]
                sr = sh[i] / sh[j]
                er = emp[i] / emp[j]
            up = rr > 1
            same = lambda x: _band(x) & ((x > 1) == up)
            hit = _band(rr) & same(sr) & (~np.isfinite(er) | (er <= 0) | same(er))
            out[i[hit]] = True
    return out


# ---------------------------------------------------------------- fundamentals at D
def fundamentals(snap: Snapshot) -> pd.DataFrame:
    """Per symbol: chosen basis, latest quarter, TTM sums, and the quarter series.

    Basis rule (spec: consolidated where the company filed them, held
    consistent across its quarters at D): consolidated if consolidated figures
    were filed, and known before D, for each of the four TTM quarters ending at
    the company's latest known quarter; otherwise standalone. The whole
    calculation for that company then uses that basis only.
    """
    f = snap.quarters
    if f.empty:
        return pd.DataFrame()
    latest_q = f.groupby("symbol")["q"].max().rename("Lq")
    f = f.join(latest_q, on="symbol")
    cons = f[f["consolidated"]]
    ttm_cons = (cons[(cons["q"] <= cons["Lq"]) & (cons["q"] > cons["Lq"] - 4)]
                .groupby("symbol")["q"].nunique())
    use_cons = set(ttm_cons[ttm_cons == 4].index)
    f = f[(f["consolidated"] & f["symbol"].isin(use_cons)) |
          (~f["consolidated"] & ~f["symbol"].isin(use_cons))].copy()
    f = f.sort_values(["symbol", "q"])

    rows = []
    for sym, g in f.groupby("symbol", sort=False):
        qs = g["q"].to_numpy()
        L = qs[-1]
        # consecutive run ending at the latest quarter in this basis
        run = 1
        while run < len(qs) and qs[-run - 1] == L - run:
            run += 1
        gi = g.set_index("q")
        last = gi.loc[L]
        ttm_idx = [L - k for k in range(4)]
        have4 = all(k in gi.index for k in ttm_idx)

        def ttm(col):
            if not have4:
                return np.nan
            v = gi.loc[ttm_idx, col].to_numpy(dtype=float)
            return float(v.sum()) if np.isfinite(v).all() else np.nan

        yago = gi.loc[L - 4] if (L - 4) in gi.index else None
        # stability: margins over the consecutive run, latest up to 8
        run_q = [L - k for k in range(min(run, STAB_MAX_QUARTERS))]
        rq = gi.loc[run_q]
        with np.errstate(divide="ignore", invalid="ignore"):
            m = (rq["ebitda"] / rq["revenue"]).where(rq["revenue"] > 0)
        m = m[np.isfinite(m)]
        rows.append({
            "symbol": sym,
            "consolidated": bool(last["consolidated"]),
            "latest_period_end": last["period_end"],
            "latest_broadcast": last["broadcast_dt"],
            "consec_quarters": run,
            "ttm_revenue": ttm("revenue"),
            "ttm_ebitda": ttm("ebitda"),
            "ttm_profit": ttm("profit_normalised"),
            "rev_latest": last["revenue"],
            "rev_yago": yago["revenue"] if yago is not None else np.nan,
            "profit_latest": last["profit_normalised"],
            "profit_yago": yago["profit_normalised"] if yago is not None else np.nan,
            "margin_sd": float(m.std(ddof=1)) if len(m) >= STAB_MIN_QUARTERS else np.nan,
            "margin_n": int(len(m)),
            "shares_basis": last["shares_implied"],
        })
    out = pd.DataFrame(rows).set_index("symbol")

    # Share count: standalone filing of the latest quarter where one exists
    # (the listed entity's own EPS denominator; consolidated EPS excludes
    # minority interest from the numerator only, which biases profit/EPS), else
    # the chosen basis. Adjusted for splits/bonus with ex_date after that
    # filing's broadcast date and before D.
    q_all = snap.quarters
    lq = q_all.groupby("symbol")["q"].transform("max")
    latest_rows = q_all[q_all["q"] == lq]
    sa = latest_rows[~latest_rows["consolidated"]].set_index("symbol")
    co = latest_rows[latest_rows["consolidated"]].set_index("symbol")
    shares = sa["shares_implied"].reindex(out.index)
    bdt = sa["broadcast_dt"].reindex(out.index)
    fill = shares.isna()
    shares[fill] = co["shares_implied"].reindex(out.index)[fill]
    bdt[fill] = co["broadcast_dt"].reindex(out.index)[fill]
    out["shares_filed"] = shares.copy()
    out["shares_fix"] = ""
    shares, bdt, fix = _check_share_count(q_all, out.index, shares, bdt, sa, co, snap.factors)
    out["shares_raw"] = shares
    out["shares_broadcast"] = bdt
    out.loc[fix.index, "shares_fix"] = fix
    fac = snap.factors
    adj = pd.Series(1.0, index=out.index)
    if not fac.empty:
        ff = fac[fac["symbol"].isin(out.index)]
        for r in ff.itertuples(index=False):
            b = out.at[r.symbol, "shares_broadcast"]
            if pd.notna(b) and r.ex_date.date() > pd.Timestamp(b).date():
                adj[r.symbol] /= r.factor
    out["shares"] = out["shares_raw"] * adj
    return out


def _check_share_count(q_all: pd.DataFrame, syms: pd.Index, shares: pd.Series,
                       bdt: pd.Series, sa: pd.DataFrame, co: pd.DataFrame,
                       factors: pd.DataFrame):
    """Clarification 30b: replace a latest-quarter share count that is 30x or
    more away from the company's recent counts.

    Reference: the median of the implied share counts of up to
    SHARES_REF_QUARTERS earlier known quarters (standalone first, as in
    Clarification 6), each carried to the latest filing's broadcast date
    through the splits and bonuses in between. Everything here was broadcast
    before D. If the latest count is 30x or more away from the reference, the
    other basis's count for the same quarter is used when it is within 1.5x of
    the reference; otherwise the reference itself is carried forward (ARVIND
    Mar-2022 standalone, EPS -373). A count of zero or less is left alone and
    fails rule 7 as before (Clarification 6).

    Returns (shares, broadcast, fix) where fix names what was done per symbol.
    """
    shares, bdt = shares.copy(), bdt.copy()
    fix = pd.Series(dtype=object)
    lq = q_all.groupby("symbol")["q"].max()
    per_q = q_all[q_all["shares_implied"] > 0]
    per_q = (per_q.sort_values(["symbol", "q", "consolidated"])
                  .drop_duplicates(["symbol", "q"])            # standalone first
                  [["symbol", "q", "shares_implied", "broadcast_dt"]])
    per_q = per_q[per_q["q"] < per_q["symbol"].map(lq)]
    per_q = per_q.sort_values(["symbol", "q"]).groupby("symbol").tail(SHARES_REF_QUARTERS)
    per_q = per_q[per_q["symbol"].isin(syms)]
    if per_q.empty:
        return shares, bdt, fix
    fac = factors[factors["symbol"].isin(per_q["symbol"].unique())] if not factors.empty else factors
    # C(d) = product of factors with ex_date > d; shares at b_j in units at b_L
    # are shares_j * C(b_L) / C(b_j).
    per_q = per_q.assign(date=per_q["broadcast_dt"].dt.normalize())
    per_q["C"] = cumulative_factor(per_q, fac)
    tgt = pd.DataFrame({"symbol": syms, "date": pd.to_datetime(bdt.reindex(syms).to_numpy())})
    ok = tgt["date"].notna().to_numpy()
    CL = np.ones(len(tgt))
    if ok.any():
        CL[ok] = cumulative_factor(tgt[ok].assign(date=tgt.loc[ok, "date"].dt.normalize()), fac)
    CL = pd.Series(CL, index=syms)
    per_q["ref_units"] = per_q["shares_implied"] * per_q["symbol"].map(CL) / per_q["C"]
    ref = per_q.groupby("symbol")["ref_units"].median()

    cur = shares.reindex(ref.index)
    with np.errstate(invalid="ignore", divide="ignore"):
        bad = pd.Series(_band((cur / ref).to_numpy()), index=ref.index)
    for s in bad.index[bad.to_numpy()]:
        r = ref[s]
        # the other basis for the same quarter (the one not already chosen)
        chosen_sa = s in sa.index and pd.notna(sa.at[s, "shares_implied"])
        alt = co if chosen_sa else sa
        alt_sh = alt.at[s, "shares_implied"] if s in alt.index else np.nan
        if (pd.notna(alt_sh) and alt_sh > 0 and
                1 / SHARES_OTHER_BASIS_TOL <= alt_sh / r <= SHARES_OTHER_BASIS_TOL):
            shares[s] = alt_sh
            bdt[s] = alt.at[s, "broadcast_dt"]
            fix[s] = "other_basis"
        else:
            shares[s] = r
            fix[s] = "carried_reference"
    return shares, bdt, fix


# ---------------------------------------------------------------- universe
FUNNEL_STEPS = [
    "archive_symbols",        # any row in the price window
    "r1_ordinary_equity_recent_trade",
    "r2_turnover_20L",
    "r3_200_sessions_365d",
    "r4_not_lender",
    "r5_four_quarters_fresh",
    "r6_ttm_profit_ebitda_positive",
    "r7_mcap_computable",
]


def lender_flags(snap: Snapshot, static: StaticLabels, symbols) -> pd.DataFrame:
    """Why each symbol is treated as a lender or insurer.

    Sources, because no single one covers delisted names:
      label_pit     NSE industry label published before D (2022 onward)
      label_fill    the symbol's first-ever label (static; covers 2019-2021)
      pit_taxonomy  at least half the filings for its latest 4 quarters known
                    before D (those broadcast once NSE's NBFC taxonomy existed)
                    are in the NBFC, banking or insurer XBRL taxonomy
      tax_fill      no such filing known before D yet (decision dates to early
                    2020): the stock's first filings under the NBFC taxonomy
                    (static, like label_fill) (Clarification 28)
      bank_xbrl     its latest filing known before D has no
                    RevenueFromOperations tag -- catches delisted banks and
                    insurers directly, and they fail rule 6 regardless
      name          company name matches a lender pattern, only for companies
                    with no industry label at all -- the net for delisted
                    NBFCs that never carried one (Clarification 29)
    `nse_list` (today's list) is reported for diagnostics and is NOT part of
    `lender`: a 2025 filing format is information from after D (Clarification 28).
    """
    idx = pd.Index(symbols, name="symbol")
    pit = snap.industry_pit.reindex(idx)
    fill = static.backfill_industry.reindex(idx)
    names = static.names.reindex(idx).fillna("") if len(static.names) else \
        pd.Series("", index=idx)
    unlabelled = pit.isna() & fill.isna()
    tv = snap.taxonomy.reindex(idx).fillna(0) if len(snap.taxonomy) else         pd.DataFrame({"n_new": 0, "n_lender": 0}, index=idx)
    df = pd.DataFrame({
        "label_pit": pit.isin(LENDER_LABELS),
        "label_fill": pit.isna() & fill.isin(LENDER_LABELS),
        "pit_taxonomy": (tv["n_new"] > 0) & (2 * tv["n_lender"] >= tv["n_new"]),
        "tax_fill": (tv["n_new"] == 0) & idx.isin(list(static.taxonomy_fill)),
        "bank_xbrl": idx.isin(list(snap.bankfmt)),
        "name": unlabelled & names.map(lambda s: bool(LENDER_NAME.search(s))).astype(bool),
    }, index=idx)
    df["lender"] = df.any(axis=1)
    df["nse_list"] = idx.isin(list(static.nse_lenders))     # diagnostics only
    return df


def sector_of(snap: Snapshot, static: StaticLabels, symbols) -> pd.DataFrame:
    idx = pd.Index(symbols, name="symbol")
    pit = snap.industry_pit.reindex(idx)
    fill = static.backfill_industry.reindex(idx)
    sector = pit.where(pit.notna(), fill)
    src = np.where(pit.notna(), "pit", np.where(fill.notna(), "backfill", "none"))
    return pd.DataFrame({"sector": sector, "sector_source": src}, index=idx)


def universe(snap: Snapshot, static: StaticLabels | None = None
             ) -> tuple[pd.DataFrame, dict, pd.DataFrame]:
    """Apply the seven rules in order.

    Returns (members, funnel, fund) where members is indexed by symbol with
    the price-derived fields the composite needs, funnel maps each step to the
    count remaining, and fund is the fundamentals table for all symbols.
    """
    static = static or StaticLabels()
    px, cal, D = snap.px, snap.cal, snap.D
    if len(cal) < MOM_LOOKBACK:
        raise ValueError(f"{D.date()}: only {len(cal)} sessions of history loaded")
    funnel: dict[str, int] = {}
    funnel["archive_symbols"] = px["symbol"].nunique()

    # Rule 1: ordinary equity, EQ/BE, traded in one of the last 5 sessions.
    last = px.groupby("symbol").tail(1).set_index("symbol")
    recent = cal[-RECENT_TRADE_SESSIONS]
    ok = last[(last["isin_code"].fillna("").str.startswith("INE")) &
              (last["series"].isin(["EQ", "BE"])) & (last["date"] >= recent)]
    syms = ok.index
    funnel["r1_ordinary_equity_recent_trade"] = len(syms)

    # Rule 2: median turnover over the previous 60 sessions, untraded sessions as 0.
    w60 = px[(px["date"] >= cal[-TURNOVER_WINDOW]) & px["symbol"].isin(syms)]
    tsum = w60.groupby("symbol")["turnover"].apply(
        lambda s: np.median(np.concatenate([s.fillna(0).to_numpy(),
                                            np.zeros(TURNOVER_WINDOW - len(s))])))
    med_turn = tsum.reindex(syms).fillna(0.0)
    syms = syms[med_turn.reindex(syms).to_numpy() >= TURNOVER_MIN]
    funnel["r2_turnover_20L"] = len(syms)

    # Rule 3: >= 200 sessions traded in the previous 365 calendar days.
    y1 = px[(px["date"] >= D - timedelta(days=365)) & px["symbol"].isin(syms)]
    n365 = y1.groupby("symbol").size().reindex(syms).fillna(0)
    syms = syms[n365.to_numpy() >= MIN_SESSIONS_365D]
    funnel["r3_200_sessions_365d"] = len(syms)

    # Rule 4: not a lender or insurer.
    lf = lender_flags(snap, static, syms)
    snap.lenders = lf
    syms = syms[~lf["lender"].to_numpy()]
    funnel["r4_not_lender"] = len(syms)

    # Rule 5: >= 4 consecutive quarters known, latest period_end within 200 days.
    fund = fundamentals(snap)
    fu = fund.reindex(syms)
    stale = (D - fu["latest_period_end"]).dt.days
    r5 = (fu["consec_quarters"] >= MIN_CONSEC_QUARTERS) & (stale <= MAX_STALENESS_DAYS)
    syms = syms[r5.fillna(False).to_numpy()]
    funnel["r5_four_quarters_fresh"] = len(syms)

    # Rule 6: TTM normalised profit > 0 and TTM EBITDA > 0.
    fu = fund.reindex(syms)
    r6 = (fu["ttm_profit"] > 0) & (fu["ttm_ebitda"] > 0)
    syms = syms[r6.fillna(False).to_numpy()]
    funnel["r6_ttm_profit_ebitda_positive"] = len(syms)

    # Rule 7: market cap computable = raw last close before D x adjusted shares > 0.
    fu = fund.reindex(syms)
    raw_close = last["close"].reindex(syms)
    mcap = raw_close * fu["shares"]
    r7 = mcap.notna() & (mcap > 0) & np.isfinite(mcap)
    syms = syms[r7.to_numpy()]
    funnel["r7_mcap_computable"] = len(syms)

    members = pd.DataFrame(index=pd.Index(syms, name="symbol"))
    members["ticker"] = last["ticker"].reindex(syms) if "ticker" in last else syms
    members["last_date"] = last["date"].reindex(syms)
    members["raw_close"] = raw_close.reindex(syms)
    members["mcap"] = mcap.reindex(syms)
    members["median_turnover_60"] = med_turn.reindex(syms)
    members["sessions_365d"] = n365.reindex(syms)
    members = members.join(sector_of(snap, static, syms))
    return members, funnel, fund


def funnel_frame(D, funnel: dict) -> pd.DataFrame:
    """One row per D: counts remaining and removed by each rule."""
    row = {"D": pd.Timestamp(D).date()}
    prev = None
    for k in FUNNEL_STEPS:
        row[k] = funnel.get(k)
        if prev is not None and funnel.get(k) is not None:
            row[f"removed_{k.split('_')[0]}"] = funnel[prev] - funnel[k]
        prev = k
    return pd.DataFrame([row])
