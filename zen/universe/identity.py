"""Stock identity across renames (strategy v1, spec Clarification 23).

The archive stores each dataset under the symbol NSE used when it was
fetched, and those do not agree over time:

  bhavcopy       the symbol on the day (CADILAHC until Feb 2022, then ZYDUSLIFE)
  financials     the symbol at download time, so every Cadila filing back to
                 2018 sits under ZYDUSLIFE
  corpactions,   the symbol on the ex-date / broadcast date
  announcements

Keyed on the raw symbol, a company that was renamed AFTER D loses its filings
at D and silently leaves the universe (AMARAJABAT, CADILAHC, MCDOWELL-N,
TATAMOTORS, ...: 40-60 names on every decision date). That exclusion depends
on an event after D, which is itself a look-ahead.

A stock is therefore identified by a component of (symbol, ISIN) spells from
the bhavcopy: two spells are the same stock when they share a symbol (an ISIN
change on a face-value split) or an ISIN (a symbol change on a rename) and the
later one starts within 60 market sessions of the earlier one ending. A symbol
reused years later by a different company is not merged. The component's
label (its `cid`) is the most recent symbol it traded under, with '#k' for a
later component that reuses a label.

Those two passes share a blind spot: a company that changes its symbol AND its
ISIN on the same day shares nothing with itself, so it is silently treated as
one company delisting and another listing (SUBEX -> SUBEXLTD, SUPPETRO ->
SPLPETRO). A third pass therefore links two components of the same ISIN
*issuer* (the first 7 characters, INE754A), under four guards -- see
`link()` and spec Clarification 33.

This reads only (symbol, ISIN, first date, last date) keys over the whole
archive -- identity, never a price or a return -- and is passed to the
point-in-time code as a static input (pit.StaticLabels), like the industry
backfill, so the leak test's scope is stated rather than implied.

Events keyed by (symbol, date) are mapped to the spell of that symbol which
had most recently started by the date, else to the symbol's earliest spell
(a filing stored under a symbol the stock only adopted later).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

LINK_GAP_SESSIONS = 60          # Clarification 33 (was 10)
ISSUER_GAP_SESSIONS = 60        # third pass: same ISIN issuer, both keys changed


def issuer_linkable(isin) -> bool:
    """True for an ordinary equity ISIN, the only kind the third pass may join.

    An Indian ISIN is INE + 4 issuer characters + 2 security-type digits + 3.
    '01' is ordinary equity; '20' is a rights entitlement, which shares the
    issuer code, trades in series BE and would otherwise be merged into its
    parent (RIL-RE INE002A20018 into RELIANCE INE002A01018).
    """
    s = str(isin)
    return len(s) == 12 and s.startswith("INE") and s[7:9] == "01"


@dataclass
class Identity:
    spells: pd.DataFrame          # symbol, isin, first, last, cid

    # ------------------------------------------------------------ building
    @classmethod
    def build(cls, con) -> "Identity":
        keys = con.execute(
            "SELECT symbol, isin_code AS isin, min(date) AS first, max(date) AS last "
            "FROM prices WHERE series IN ('EQ','BE') GROUP BY 1, 2").df()
        cal = pd.DatetimeIndex(pd.to_datetime(con.execute(
            "SELECT DISTINCT date FROM prices ORDER BY date").df()["date"]))
        return cls(link(keys, cal))

    @classmethod
    def trivial(cls) -> "Identity":
        return cls(pd.DataFrame(columns=["symbol", "isin", "first", "last", "cid"]))

    @property
    def empty(self) -> bool:
        return self.spells.empty

    # ------------------------------------------------------------ mapping
    def for_prices(self, df: pd.DataFrame, sym="symbol", isin="isin_code") -> np.ndarray:
        """cid of each (symbol, ISIN) price row; the symbol itself if unknown."""
        if self.empty or df.empty:
            return df[sym].to_numpy(dtype=object)
        key = self.spells.drop_duplicates(["symbol", "isin"]).set_index(["symbol", "isin"])["cid"]
        idx = pd.MultiIndex.from_arrays([df[sym].to_numpy(), df[isin].to_numpy()])
        out = key.reindex(idx).to_numpy(dtype=object)
        miss = pd.isna(out)
        if miss.any():
            out[miss] = self.for_events(df.loc[miss, [sym]].assign(_d=pd.Timestamp.max.normalize()),
                                        sym, "_d")
        return out

    def for_events(self, df: pd.DataFrame, sym="symbol", date_col="date") -> np.ndarray:
        """cid of each (symbol, date) event: the spell of that symbol that had most
        recently started by the date, else the symbol's earliest spell; the symbol
        itself if the bhavcopy never saw it."""
        if df.empty:
            return np.array([], dtype=object)
        if self.empty:
            return df[sym].to_numpy(dtype=object)
        sp = (self.spells.groupby(["symbol", "cid"], as_index=False)["first"].min()
              .sort_values("first"))
        sp["first"] = pd.to_datetime(sp["first"]).astype("datetime64[ns]")
        left = pd.DataFrame({"symbol": df[sym].to_numpy(),
                             "_d": pd.to_datetime(df[date_col]).astype("datetime64[ns]").to_numpy(),
                             "_i": np.arange(len(df))}).sort_values("_d")
        m = pd.merge_asof(left, sp, left_on="_d", right_on="first", by="symbol",
                          direction="backward")
        first = sp.groupby("symbol")["cid"].first()
        m["cid"] = m["cid"].fillna(m["symbol"].map(first)).fillna(m["symbol"])
        return m.sort_values("_i")["cid"].to_numpy(dtype=object)

    def for_today(self, symbols) -> set:
        """cids of symbols from a present-day list (their latest spell)."""
        if self.empty:
            return set(symbols)
        last = (self.spells.sort_values("last").drop_duplicates("symbol", keep="last")
                .set_index("symbol")["cid"])
        return {last.get(s, s) for s in symbols}


def _link_by_issuer(sp: pd.DataFrame, a, b, parent, find) -> None:
    """Third pass: join two components of one ISIN issuer (spec Clarification 33).

    A company that changes symbol and ISIN on the same day shares no key with
    itself, so the first two passes leave it as two stocks -- a surviving
    company treated as a delisting, which is the very look-ahead 23 exists to
    remove. Four guards keep this from merging anything else:

      1. both ISINs are ordinary equity ('01', see `issuer_linkable`), which
         excludes rights entitlements, that share the issuer code and trade;
      2. the two components share neither a whole symbol nor a whole ISIN --
         if they did, the passes above already decided about them;
      3. they do not overlap AT COMPONENT LEVEL and the later starts within
         ISSUER_GAP_SESSIONS of the earlier one's last session. Component
         level, not spell level, is what keeps GATECHDVR (INE224E01036) out
         of GATECH/STAMPEDE (INE224E01028): the DVR line trades alongside
         GATECH, so the components overlap;
      4. the issuer is unambiguous -- exactly one earlier and one later
         component qualify. Two competing candidates link nothing.

    On this archive the pass makes 17 joins and no false one.
    """
    elig = sp["isin"].map(issuer_linkable).to_numpy()
    if not elig.any():
        return
    roots = np.array([find(i) for i in range(len(sp))])
    comp_a, comp_b, comp_sym, comp_isin = {}, {}, {}, {}
    for i, r in enumerate(roots):
        comp_a[r] = min(comp_a.get(r, a[i]), a[i])
        comp_b[r] = max(comp_b.get(r, b[i]), b[i])
        comp_sym.setdefault(r, set()).add(sp["symbol"].iat[i])
        comp_isin.setdefault(r, set()).add(sp["isin"].iat[i])

    issuers: dict[str, set] = {}
    for i in np.flatnonzero(elig):
        issuers.setdefault(str(sp["isin"].iat[i])[:7], set()).add(roots[i])

    for _, rs in issuers.items():
        if len(rs) < 2:
            continue
        pairs = []
        for r1 in rs:
            for r2 in rs:
                if r1 == r2:
                    continue
                if comp_sym[r1] & comp_sym[r2] or comp_isin[r1] & comp_isin[r2]:
                    continue                                    # guard 2
                gap = comp_a[r2] - comp_b[r1]
                if 0 <= gap <= ISSUER_GAP_SESSIONS:             # guard 3
                    pairs.append((r1, r2))
        if len(pairs) != 1:                                     # guard 4
            continue
        r1, r2 = pairs[0]
        x, y = find(r1), find(r2)
        if x != y:
            parent[y] = x


def link(keys: pd.DataFrame, cal: pd.DatetimeIndex) -> pd.DataFrame:
    """Union (symbol, ISIN) spells into stocks; see the module docstring."""
    sidx = pd.Series(np.arange(len(cal)), index=cal)
    sp = keys.copy()
    sp["first"] = pd.to_datetime(sp["first"])
    sp["last"] = pd.to_datetime(sp["last"])
    sp = sp.sort_values(["first", "symbol", "isin"]).reset_index(drop=True)
    a = sidx.reindex(sp["first"]).to_numpy()
    b = sidx.reindex(sp["last"]).to_numpy()
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
            ix = g.index[np.argsort(a[g.index], kind="stable")]
            for i in range(len(ix)):
                for j in range(i + 1, len(ix)):
                    if a[ix[j]] - b[ix[i]] <= LINK_GAP_SESSIONS:
                        r1, r2 = find(ix[i]), find(ix[j])
                        if r1 != r2:
                            parent[r2] = r1

    _link_by_issuer(sp, a, b, parent, find)

    sp["root"] = [find(i) for i in range(len(sp))]
    sp["_a"], sp["_b"] = a, b
    label = sp.sort_values(["_b", "_a", "symbol"]).groupby("root")["symbol"].last()
    sp["cid"] = sp["root"].map(label)
    dup = sp.groupby("cid")["root"].nunique()
    for c in dup[dup > 1].index:
        order = sp[sp["cid"] == c].groupby("root")["_a"].min().sort_values()
        for k, r in enumerate(order.index):
            if k:
                sp.loc[sp["root"] == r, "cid"] = f"{c}#{k}"
    return sp[["symbol", "isin", "first", "last", "cid"]].reset_index(drop=True)
