"""v3 spec, final revision: archive structure and coverage checks only.

    python research/strategy/v3_evidence/cov_final.py --frozen data/frozen/v3 --out DIR [YYYY-MM-DD ...]

(section 21; re-run on the frozen copy for section 15.2.5. The logic is the one
that produced section 21's tables; only the paths changed: the database, the
NSE industry labels and nse_list.json are read from --frozen, and the output
goes to --out.)

No returns, no forward prices, no performance of any candidate rule. The DB is
opened read-only. Every price read is a close strictly before D (pit's snapshot
window and prices_other's BZ rows before D), used only for the market cap in
the P/E. Fundamentals are cut by broadcast_dt < D exactly as pit does.

Per review date D, on v1's universe (pit.universe members), the eligible list is
computed twice:
  old = the draft spec's rules (Clarification 4 basis, TTM_FC <= 0 passes F2,
        pit's market cap, F5 in the C4 basis, F3 strict from 15 Nov 2022, no F6)
        -- must reproduce the draft's section 21 counts (210 ... 771);
  new = the final spec's rules (v3 basis rule 3.3a, write-back rule in F2,
        v3 close rule 3.5 for the P/E, F5 in the v3 basis, F3 per stock from
        15 Nov 2022 (default) and strict (alternative), F6 on quarters).
Also: basis switches, why F5 fails (archive gap / too young / missing input),
write-back counts, F6 blocks, sector fill with the 'unclassified' group,
growth-cap and P/E>85 counts, and the owners'-profit diagnostic (EPS proxy).
"""
import os, sys, time, warnings
warnings.filterwarnings("ignore")
sys.dont_write_bytecode = True
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[3]))
import duckdb, numpy as np, pandas as pd
from zen.universe import pit
from zen.validation import factors as F
from zen.portfolio import engine_v2 as E2

import argparse
from pathlib import Path
_ap = argparse.ArgumentParser()
_ap.add_argument("--frozen", required=True, help="the frozen directory (data/frozen/v3)")
_ap.add_argument("--out", required=True, help="folder for cov_final.csv and DuckDB's spill files")
_ap.add_argument("dates", nargs="*", help="only these review dates")
_a = _ap.parse_args()
FROZEN = Path(_a.frozen)
HERE = os.path.abspath(_a.out)
os.makedirs(HERE, exist_ok=True)
OUT = os.path.join(HERE, "cov_final.csv")
con = duckdb.connect(str(FROZEN / "data" / "zen.duckdb"), read_only=True)
os.makedirs(os.path.join(HERE, "duck"), exist_ok=True)
con.execute(f"SET temp_directory='{os.path.join(HERE, 'duck').replace(os.sep, '/')}'")
con.execute("SET memory_limit='6GB'")
static = pit.StaticLabels.load(con, FROZEN / "nse_list.json")
ids = static.ids
lab = E2.nse_sectors(E2.load_nse_labels(FROZEN / "data" / "reference" / "industry_nse.parquet"), ids)
sec_raw = lab["sector"]
scheme = lab["label_scheme"]
# v3 sector key: current-scheme label compared case-insensitively; a legacy-scheme
# label or no label at all -> one group 'unclassified', capped at 4 like any sector.
sec_v3 = pd.Series(np.where((scheme == "current") & sec_raw.notna(),
                            sec_raw.astype(str).str.casefold(), "unclassified"),
                   index=lab.index)
cal = pit.sessions(con)
dates = pit.decision_dates(cal, first=(2019, 11), last=(2026, 8))
if _a.dates:
    dates = [d for d in dates if str(d.date()) in _a.dates]
CR = 1e7
DE_SWITCH = pd.Timestamp("2022-11-15")
rows = []


def fill_v2(mask, sec):
    """Most slots a cap of 4 allows when an unlabelled name is its own sector."""
    s = sec[mask]
    labd = s.dropna().value_counts()
    return int(np.minimum(labd, 4).sum() + s.isna().sum())


def fill_v3(mask, sec):
    s = sec.reindex(mask.index).fillna("unclassified")[mask]
    return int(np.minimum(s.value_counts(), 4).sum())


def wide(q, col, L, kmax=9):
    t = q.copy()
    t["k"] = t["symbol"].map(L) - t["q"]
    t = t[(t["k"] >= 0) & (t["k"] < kmax)]
    return t.pivot_table(index="symbol", columns="k", values=col, aggfunc="first",
                         dropna=False).reindex(columns=range(kmax))


EXTRA = ["eps_basic", "profit_continuing"]


def owner_extras(snap):
    """eps_basic and profit_continuing of exactly the revision the snapshot kept."""
    x = con.execute("SELECT symbol, period_end, broadcast_dt, consolidated, xbrl_url, "
                    + ", ".join(EXTRA) + " FROM financials WHERE broadcast_dt < ?",
                    [snap.D.to_pydatetime()]).df()
    x["period_end"] = pd.to_datetime(x["period_end"])
    x["broadcast_dt"] = pd.to_datetime(x["broadcast_dt"])
    x["symbol"] = ids.for_events(x, "symbol", "broadcast_dt")
    x = (x.sort_values(F.BS_KEY + ["xbrl_url"]).drop_duplicates(F.BS_KEY, keep="last")
          .drop(columns="xbrl_url"))
    return x


for D in dates:
    t0 = time.time()
    snap = pit.build_snapshot(con, D, static)
    members, funnel, fund = pit.universe(snap, static)
    U = members.index
    N = len(U)
    fu = fund.reindex(U)
    c4 = fu["consolidated"].astype(bool)
    lpe = pd.to_datetime(fu["latest_period_end"])
    L4 = lpe.dt.year * 4 + (lpe.dt.month - 1) // 3
    qx = F.quarter_extras(con, snap, ids)
    qx = qx[qx["symbol"].isin(U)].copy()
    qx["consolidated"] = qx["consolidated"].astype(bool)
    ox = owner_extras(snap)
    qx = qx.merge(ox, on=F.BS_KEY, how="left")
    pbe = qx["pbt_before_exceptional"].where(qx["pbt_before_exceptional"].notna(),
                                             qx["pbt"] - qx["exceptional_items"].fillna(0.0))
    qx["ebit"] = pbe + qx["finance_costs"]
    qx["ok"] = qx["revenue"].notna() & qx["profit_normalised"].notna() & qx["ebitda"].notna()
    # owners'-profit factor (diagnostic only): eps x standalone implied shares of the
    # same quarter / profit from continuing operations; 1 where undefined or outside (0, 1.5]
    sa_sh = (qx[~qx["consolidated"]].set_index(["symbol", "q"])["shares_implied"])
    key = pd.MultiIndex.from_arrays([qx["symbol"], qx["q"]])
    nk = pd.Series(sa_sh.reindex(key).to_numpy(), index=qx.index)
    fac = qx["eps_basic"] * nk / qx["profit_continuing"]
    good = qx["consolidated"] & (qx["profit_continuing"] > 0) & (nk > 0) & (fac > 0) & (fac <= 1.5)
    qx["own_fac"] = np.where(good, fac, 1.0)
    qx["P_own"] = qx["profit_normalised"] * qx["own_fac"]

    Lstar = qx.groupby("symbol")["q"].max().reindex(U)

    def ok8(cons):
        sub = qx[qx["consolidated"] == cons]
        w = wide(sub, "ok", Lstar, 8).reindex(U)
        return w.fillna(False).astype(bool).all(axis=1)
    okc, oks = ok8(True), ok8(False)
    b3 = pd.Series(np.where(okc, True, np.where(oks, False, c4)), index=U).astype(bool)
    L3 = pd.Series(np.where(okc | oks, Lstar, L4), index=U)

    def series(basis, L, col):
        sub = qx[qx["consolidated"].to_numpy() == basis.reindex(qx["symbol"]).to_numpy()]
        return wide(sub, col, L).reindex(U)

    out = {}
    for tag, basis, L in (("o", c4, L4), ("n", b3, L3)):
        R, P, Eb = series(basis, L, "revenue"), series(basis, L, "profit_normalised"), series(basis, L, "ebitda")
        FC, EB = series(basis, L, "finance_costs"), series(basis, L, "ebit")
        PO = series(basis, L, "P_own")
        present = series(basis, L, "q").notna()
        ttmP = P[[0, 1, 2, 3]].sum(axis=1, min_count=4)
        ttm4P = P[[4, 5, 6, 7]].sum(axis=1, min_count=4)
        ttmR = R[[0, 1, 2, 3]].sum(axis=1, min_count=4)
        ttm4R = R[[4, 5, 6, 7]].sum(axis=1, min_count=4)
        hist = R[list(range(8))].notna().all(axis=1) & P[list(range(8))].notna().all(axis=1) & \
            Eb[list(range(8))].notna().all(axis=1)
        fc4 = FC[[0, 1, 2, 3]]
        eb4 = EB[[0, 1, 2, 3]]
        known = fc4.notna().all(axis=1) & eb4.notna().all(axis=1)
        out[tag] = dict(R=R, P=P, ttmP=ttmP, ttm4P=ttm4P, ttmR=ttmR, ttm4R=ttm4R, hist=hist,
                        known=known, ttmFC=fc4.sum(axis=1, min_count=4),
                        ttmEBIT=eb4.sum(axis=1, min_count=4), wb=(fc4 < -1e7).any(axis=1),
                        fcp=fc4.clip(lower=0).sum(axis=1, min_count=4), basis=basis,
                        ttmPO=PO[[0, 1, 2, 3]].sum(axis=1, min_count=4), present=present, L=L)

    # first known quarter per (symbol, basis), for 'too young' vs 'archive gap'
    firstq = qx.groupby(["symbol", "consolidated"])["q"].min()

    # ---- market cap: pit's (old) and the v3 close rule (new: latest EQ/BE/BZ close
    # before D within pit's 420-day window, on D's share basis, x pit's share count)
    mcap_old = members["mcap"]
    px = snap.px
    last_eb = px.sort_values("date").groupby("symbol").tail(1).set_index("symbol")
    lo = (D - pd.Timedelta(days=pit.PRICE_LOOKBACK_DAYS)).date()
    bz = con.execute("SELECT symbol, isin_code, date, close FROM prices_other WHERE series='BZ' "
                     "AND date >= ? AND date < ? AND close > 0", [lo, D.date()]).df()
    if len(bz):
        bz["date"] = pd.to_datetime(bz["date"])
        bz["symbol"] = ids.for_prices(bz)
        bz["adj_close"] = bz["close"] * pit.cumulative_factor(bz, snap.factors)
        last_bz = bz.sort_values("date").groupby("symbol").tail(1).set_index("symbol")
        d_bz = pd.to_datetime(last_bz["date"]).reindex(U)
        bz_close = last_bz["adj_close"].reindex(U)
    else:
        d_bz = pd.Series(pd.NaT, index=U)
        bz_close = pd.Series(np.nan, index=U)
    d_eb = last_eb["date"].reindex(U)
    use_bz = d_bz.notna() & (d_eb.isna() | (d_bz > d_eb))
    close_v3 = last_eb["adj_close"].reindex(U).where(~use_bz, bz_close)
    mcap_new = close_v3 * fu["shares"]
    split_between = (last_eb["adj_close"].reindex(U) != last_eb["close"].reindex(U))

    o, n = out["o"], out["n"]
    f1o = o["ttmP"] >= 25 * CR
    f1n = n["ttmP"] >= 25 * CR
    f2o = o["known"] & ((o["ttmFC"] <= 0) | ((o["ttmEBIT"] / o["ttmFC"]) >= 2.5))
    icr_n = (n["ttmEBIT"] / n["fcp"]).where(n["fcp"] > 0)
    f2n = n["known"] & ~n["wb"] & ((n["fcp"] == 0) | (icr_n >= 2.5))
    bs = F.latest_balance_sheets(con, snap, ids)
    bs = bs[bs["symbol"].isin(U)].copy()
    bs["consolidated"] = bs["consolidated"].astype(bool)

    def f3(basis):
        b = bs[bs["consolidated"].to_numpy() == basis.reindex(bs["symbol"]).to_numpy()].set_index("symbol")
        age = (D - pd.to_datetime(b["period_end"])).dt.days.reindex(U)
        usable = (age <= 400).fillna(False)
        eq = b["equity"].astype(float).reindex(U)
        debt = b["debt_total"].astype(float).fillna(0.0).reindex(U)
        okk = usable & (eq > 0) & (debt / eq < 1.2)
        on = D >= DE_SWITCH
        strict = okk if on else pd.Series(True, index=U)
        per = (~usable | okk) if on else pd.Series(True, index=U)
        return strict, per, usable, okk
    f3o_strict, _, us_o, _ = f3(o["basis"])
    f3n_strict, f3n_per, us_n, okk_n = f3(n["basis"])
    pe_o = mcap_old / o["ttmP"]
    pe_n = mcap_new / n["ttmP"]
    f4o = (pe_o > 0) & (pe_o <= 60)
    f4n = (pe_n > 0) & (pe_n <= 60)
    f5o = o["hist"]
    f5n = n["hist"] & (n["ttmR"] > 0) & (n["ttm4R"] > 0)
    Pn = n["P"]
    RL = (n["ttmP"] < n["ttm4P"]) & Pn[list(range(8))].notna().all(axis=1)
    p1 = Pn[[1, 2, 3, 4]].sum(axis=1, min_count=4)
    p5 = Pn[[5, 6, 7, 8]].sum(axis=1, min_count=4)
    RL1 = (p1 < p5) & Pn[list(range(1, 9))].notna().all(axis=1)
    f6n = ~(RL & RL1)

    eo = f1o & f2o & f3o_strict & f4o & f5o
    nonhist = f1n & f2n & f3n_per & f4n           # every filter but F5 and F6 (default F3)
    pre6 = nonhist & f5n
    en = pre6 & f6n                                # eligible, final rules, default F3
    en_strict = f1n & f2n & f3n_strict & f4n & f5n & f6n

    # why F5 fails among names passing every other filter
    fail5 = nonhist & ~f5n
    first_b = pd.Series([firstq.get((s, bool(b3[s])), np.nan) for s in U], index=U)
    young = fail5 & (first_b > (n["L"] - 7))
    miss_row = ~n["present"][list(range(8))].all(axis=1)
    gap = fail5 & ~young & miss_row
    inputs = fail5 & ~young & ~miss_row

    sec = sec_v3.reindex(U).fillna("unclassified")
    sec_v2 = sec_raw.reindex(U)
    gn = (n["ttmP"] - n["ttm4P"]) / np.maximum(n["ttm4P"], 25 * CR)
    ge = gn[en]
    # owners'-profit diagnostic on eligible (or F4-borderline) consolidated names
    share = (n["ttmPO"] / n["ttmP"]).where(b3 & (n["ttmP"] > 0))
    pe_own = mcap_new / n["ttmPO"]
    f4_own = (pe_own > 0) & (pe_own <= 60)
    f1_own = n["ttmPO"] >= 25 * CR
    base_other = f2n & f3n_per & f5n & f6n
    row = dict(
        D=str(D.date()), N=N, elig_old=int(eo.sum()),
        elig_new=int(en.sum()), elig_new_strict=int(en_strict.sum()),
        pass_nonhist=int(nonhist.sum()), f5_fail_gap=int(gap.sum()),
        f5_fail_young=int(young.sum()), f5_fail_inputs=int(inputs.sum()),
        basis_switch=int((b3 != c4).sum()), basis_to_sa=int((~b3 & c4).sum()),
        basis_to_cons=int((b3 & ~c4).sum()), cons_now=int(b3.sum()),
        f5_old=int(f5o.sum()), f5_new=int(f5n.sum()),
        f1_new=int(f1n.sum()), f2_new=int(f2n.sum()), f4_new=int(f4n.sum()),
        bs_usable_new=int(us_n.sum()), bs_usable_old=int(us_o.sum()),
        de_fail_known=int((us_n & ~okk_n).sum()),
        wb_univ=int((n["known"] & n["wb"]).sum()),
        wb_block=int((f1n & f3n_per & f4n & f5n & n["known"] & n["wb"]).sum()),
        f2_icr_flip=int((f2o & ~f2n & ~n["wb"] & (b3 == c4)).sum()),
        no_fc=int((n["known"] & ~n["wb"] & (n["fcp"] == 0)).sum()),
        f6_block=int((pre6 & ~f6n).sum()),
        f4_diff_mcap=int(((f4o != f4n) & (b3 == c4)).sum()),
        bz_close=int(use_bz.sum()), split_between=int(split_between.fillna(False).sum()),
        fill_v3=fill_v3(en, sec), fill_v2rule=fill_v2(en, sec_v2),
        unclassified=int((sec[en] == "unclassified").sum()),
        nsec=int(sec[en][sec[en] != "unclassified"].nunique()),
        g_gt50=int((ge > 0.5).sum()), g_le0=int((ge <= 0).sum()), g_gt100=int((ge > 1.0).sum()),
        pe85_univ=int((pe_n > 85).sum()), pe1275_univ=int((pe_n >= 127.5).sum()),
        own_tested=int((en & share.notna()).sum()),
        own_lt90=int((en & (share < 0.9)).sum()), own_lt75=int((en & (share < 0.75)).sum()),
        own_f4_flip=int(((f4n != f4_own) & f1n & base_other & b3).sum()),
        own_f1_flip=int(((f1n != f1_own) & f4n & base_other & b3).sum()),
        secs=round(time.time() - t0, 1))
    rows.append(row)
    print(row, flush=True)
    pd.DataFrame(rows).to_csv(OUT, index=False)
print(pd.DataFrame(rows).to_string())
