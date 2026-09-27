"""Invariants of the committed v1 baseline (data/backtest/v1_final).

Everything in the first part reads committed files only and needs no
database. The book is rebuilt from the engine's own records:

    trades.csv                every fill (adjusted units, adjusted price, value, cost)
    after_tax/dividends.csv   cash the engine credited (units at the previous close
                              x cash per unit), written from the engine's panel
    after_tax/close_px.csv    the engine's marked price at each close (Panel.last,
                              adjusted and carried forward)
    after_tax/end_px.csv      the engine's price at the final open mark
    holdings.csv              the target book at each decision date
    rebalance_open.csv        the value at the open of each decision date

and compared with nav.csv and with the independent checker's committed run
(data/backtest/v1_final_check), which shares no code with the engine and keeps
raw shares rather than adjusted units, so it is compared on rupee values.

The universe and ranks used to re-derive the buffer rule come from the
checker's committed ranks.csv: ranks.parquet is not committed (.gitignore). Where
ranks.parquet exists locally it is checked against the checker's.

The last part (skipped without data/zen.duckdb) checks what the files alone
cannot: that dividends were credited on the ex-dates in the corporate-action
feed, and that every fill was on a session the stock traded, at its adjusted
open.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "data" / "backtest" / "v1_final"
CHECK = ROOT / "data" / "backtest" / "v1_final_check"
DB = ROOT / "data" / "zen.duckdb"

# Units left by a full sale are zero in the engine; replaying the CSV leaves a
# float residue of about 1e-13 units. A position counts as held when it is
# worth more than a millionth of a rupee.
HELD_RUPEES = 1e-6
# Buys are scaled so that value plus cost equals the cash available when cash
# binds (Clarification 11). Floating-point rounding of that scaling leaves cash
# a residue below zero after the buys of 15 decision dates in this run, at worst
# -4.1e-10 rupees (16 Aug 2023), so the floor is a billionth of a rupee.
CASH_FLOOR = -1e-9


# ---------------------------------------------------------------- loading and replay
def _load():
    m = json.loads((RUN / "metrics.json").read_text())
    nav = pd.read_csv(RUN / "nav.csv", parse_dates=["date"])
    trades = pd.read_csv(RUN / "trades.csv", parse_dates=["date"])
    hold = pd.read_csv(RUN / "holdings.csv", parse_dates=["D"])
    divs = pd.read_csv(RUN / "after_tax" / "dividends.csv", parse_dates=["date"])
    close_px = pd.read_csv(RUN / "after_tax" / "close_px.csv", parse_dates=["date"]).set_index("date")
    end_px = pd.read_csv(RUN / "after_tax" / "end_px.csv").set_index("symbol")["price"]
    ro = pd.read_csv(RUN / "rebalance_open.csv", parse_dates=["date"]).set_index("date")
    return {"metrics": m, "cfg": m["config"], "nav": nav, "trades": trades, "hold": hold,
            "divs": divs, "close_px": close_px, "end_px": end_px, "rebalance_open": ro,
            "decisions": [pd.Timestamp(d) for d in m["decision_dates"]]}


def _replay(b: dict) -> dict:
    """Walk the clock as the engine does: dividends, then sells, then buys, then
    the close. Returns cash and value at every mark, the cash after every step,
    the book before each session's fills and the positions at every close."""
    nav, cp, ep = b["nav"], b["close_px"], b["end_px"]
    fills = b["trades"][b["trades"]["side"].isin(["buy", "sell"])]
    by_fill = {d: g for d, g in fills.groupby("date")}
    by_div = {d: g for d, g in b["divs"].groupby("date")}
    cash = float(nav["strategy"].iloc[0])
    units: dict[str, float] = {}
    rows, steps, before, positions, div_units = [], [], {}, {}, []

    def held(d):
        return {s for s, u in units.items() if abs(u * cp.at[d, s]) > HELD_RUPEES}

    prev_close = None
    for i, (d, mark) in enumerate(zip(nav["date"], nav["mark"])):
        if i == 0:
            rows.append((d, mark, cash, cash))
            continue
        # dividends first, to the units held at the previous close; the engine
        # also credits an ex-date on the end date before its opening mark
        for r in by_div.get(d, pd.DataFrame()).itertuples():
            u = units.get(r.symbol, 0.0)
            div_units.append((d, r.symbol, u, r.amount,
                              u * cp.at[prev_close, r.symbol] if prev_close is not None else np.nan))
            cash += r.amount
        steps.append((d, "dividends", cash))
        if mark == "open":                           # the final mark: no trading
            val = cash + sum(u * ep[s] for s, u in units.items() if u != 0)
            rows.append((d, mark, cash, val))
            continue
        if d in by_fill:
            before[d] = dict(units)
            g = by_fill[d]
            for r in g[g["side"] == "sell"].itertuples():
                cash += r.value - r.cost
                units[r.symbol] = units.get(r.symbol, 0.0) - r.units
            steps.append((d, "sells", cash))
            for r in g[g["side"] == "buy"].itertuples():
                cash -= r.value + r.cost
                units[r.symbol] = units.get(r.symbol, 0.0) + r.units
            steps.append((d, "buys", cash))
        val = cash + sum(u * cp.at[d, s] for s, u in units.items() if u != 0)
        rows.append((d, mark, cash, val))
        positions[d] = held(d)
        prev_close = d
    marks = pd.DataFrame(rows, columns=["date", "mark", "cash", "value"])
    marks["nav"] = nav["strategy"].to_numpy()
    return {"marks": marks, "steps": pd.DataFrame(steps, columns=["date", "step", "cash"]),
            "before": before, "positions": positions,
            "div_units": pd.DataFrame(div_units, columns=["date", "symbol", "units_prev",
                                                         "amount", "value_prev"])}


@pytest.fixture(scope="module")
def book():
    b = _load()
    b["replay"] = _replay(b)
    b["sessions"] = pd.DatetimeIndex(b["nav"].loc[b["nav"]["mark"] == "close", "date"])
    return b


@pytest.fixture(scope="module")
def checker_ranks():
    r = pd.read_csv(CHECK / "ranks.csv", parse_dates=["D"])
    return {D: g.set_index("cid") for D, g in r.groupby("D")}


def _held_at_open(b, D) -> set:
    """Positions at the open of D, before any fill on D: the last close before D."""
    pos = b["replay"]["positions"]
    prev = b["sessions"][b["sessions"] < D]
    return set(pos[prev[-1]]) if len(prev) else set()


def _target(b, D) -> pd.DataFrame:
    h = b["hold"]
    return h[(h["D"] == D) & (h["action"] != "sell")]


# ---------------------------------------------------------------- NAV and cash
def test_nav_is_cash_plus_units_times_marked_price(book):
    m = book["replay"]["marks"]
    rel = (m["value"] / m["nav"] - 1).abs()
    assert len(m) == len(book["nav"]) == 1873
    assert rel.max() < 1e-12, m.loc[rel.idxmax()]


def test_cash_is_never_negative(book):
    steps = book["replay"]["steps"]
    marks = book["replay"]["marks"]
    worst = min(steps["cash"].min(), marks["cash"].min())
    assert worst >= CASH_FLOOR, steps.loc[steps["cash"].idxmin()]


# ---------------------------------------------------------------- the book at each decision date
def test_at_most_ten_names_and_three_per_sector(book):
    n = book["cfg"]["n"]
    cap = book["cfg"]["sector_cap"]
    assert (n, cap) == (10, 3)
    for D in book["decisions"]:
        tgt = _target(book, D)
        assert 0 < len(tgt) <= n, (D, len(tgt))
        sectors = Counter(s for s in tgt["sector"] if pd.notna(s))
        assert not sectors or max(sectors.values()) <= cap, (D, sectors.most_common(1))
        # held at the close of D, after the day's fills
        assert len(book["replay"]["positions"][D]) <= n, D
    # In this run no close ever shows more than ten positions (a sale waiting
    # for its stock to trade while buys fill could briefly make eleven).
    assert max(len(p) for p in book["replay"]["positions"].values()) == n


def _buffer_rule(ranked: pd.DataFrame, held: set, n: int, keep_within: int, cap: int) -> list:
    """The spec's Portfolio section, written out again from the text: keep a
    holding still in the universe and ranked within the top 2N (best first, at
    most N); fill vacancies in rank order, skipping a sector already at the cap
    (kept names count); a company with no label is its own sector."""
    rank = ranked["rank"]
    keep = sorted((s for s in held if s in rank.index and rank[s] <= keep_within),
                  key=lambda s: rank[s])[:n]
    count = Counter(ranked.at[s, "sector"] for s in keep if pd.notna(ranked.at[s, "sector"]))
    out = list(keep)
    for s in rank.sort_values(kind="stable").index:
        if len(out) >= n:
            break
        if s in out:
            continue
        sec = ranked.at[s, "sector"]
        if pd.notna(sec):
            if count[sec] >= cap:
                continue
            count[sec] += 1
        out.append(s)
    return out


def test_every_book_is_what_the_buffer_rule_allows(book, checker_ranks):
    cfg = book["cfg"]
    for D in book["decisions"]:
        r = checker_ranks[D]
        held = _held_at_open(book, D)
        want = _buffer_rule(r, held, cfg["n"], cfg["buffer_mult"] * cfg["n"], cfg["sector_cap"])
        h = book["hold"][book["hold"]["D"] == D].set_index("symbol")
        tgt = h[h["action"] != "sell"]
        assert set(tgt.index) == set(want), (D, sorted(set(tgt.index) ^ set(want)))
        # every name bought is in that date's universe, at the rank recorded
        assert set(tgt.index) <= set(r.index), D
        assert (tgt["rank"].to_numpy() == r.loc[tgt.index, "rank"].to_numpy()).all(), D
        # buy means not held at the open; keep means held; every other holding is sold
        assert set(tgt.index[tgt["action"] == "buy"]) == set(want) - held, D
        assert set(tgt.index[tgt["action"] == "keep"]) == set(want) & held, D
        assert set(h.index[h["action"] == "sell"]) == held - set(want), D


def test_local_ranks_parquet_agrees_with_the_checker(checker_ranks):
    p = RUN / "ranks.parquet"
    if not p.exists():
        pytest.skip("ranks.parquet is not committed; nothing local to compare")
    rp = pd.read_parquet(p)
    rp["D"] = pd.to_datetime(rp["D"])
    for D, g in rp.groupby("D"):
        c = checker_ranks[pd.Timestamp(D)]
        g = g.set_index("symbol")
        assert set(g.index) == set(c.index), D
        c = c.loc[g.index]
        assert (g["rank"].to_numpy() == c["rank"].to_numpy()).all(), D
        a, b = g["sector"].astype(object), c["sector"].astype(object)
        assert ((a.isna() & b.isna()) | (a == b)).all(), D


def test_every_fill_carries_out_an_order_of_the_latest_decision_date(book):
    """Buys only of the target book, sales only of what was held, within the
    5-session window; a name leaving the book is sold in full, a kept name is
    resized to exactly NAV(open of D) / N, and the buys on one session share a
    single pro-rata scale no greater than one (Clarifications 10 and 11)."""
    cfg, sess, ro = book["cfg"], book["sessions"], book["rebalance_open"]
    decisions = pd.DatetimeIndex(book["decisions"])
    fills = book["trades"][(book["trades"]["side"].isin(["buy", "sell"])) &
                           (book["trades"]["reason"] == "rebalance")]
    for d, g in fills.groupby("date"):
        D = decisions[decisions <= d][-1]
        assert sess.get_loc(d) - sess.get_loc(D) <= cfg["wait_sessions"], (d, D)
        tgt = _target(book, D)
        per = ro.at[D, "strategy_open"] / len(tgt)
        held = _held_at_open(book, D)
        before = book["replay"]["before"][d]
        scales = []
        for r in g.itertuples():
            u0 = before.get(r.symbol, 0.0)
            if r.side == "buy":
                assert r.symbol in set(tgt["symbol"]), (d, r.symbol)
                scales.append(r.value / (per - u0 * r.price))
            else:
                assert r.symbol in held, (d, r.symbol)
                left = u0 - r.units
                if r.symbol in set(tgt["symbol"]):
                    assert abs(left * r.price / per - 1) < 1e-9, (d, r.symbol)
                else:
                    assert abs(left) <= 1e-9 * u0, (d, r.symbol)
        if scales:
            assert max(scales) <= 1 + 1e-12, (d, max(scales))
            assert max(scales) - min(scales) < 1e-9, (d, scales)


def test_the_only_forced_exit_is_the_tata_steel_bsl_merger(book):
    t = book["trades"]
    other = t[t["reason"] != "rebalance"]
    assert list(zip(other["date"].dt.date.astype(str), other["symbol"], other["side"])) == [
        ("2021-11-23", "TATASTLBSL", "cancel"), ("2021-12-13", "TATASTLBSL", "sell")]
    forced = other[other["side"] == "sell"].iloc[0]
    # sold at the last close x haircut 1.0 (Clarification 15)
    assert forced["price"] == pytest.approx(book["close_px"].at[forced["date"], "TATASTLBSL"],
                                            rel=1e-12)


# ---------------------------------------------------------------- costs, sessions, dividends
def test_every_fill_costs_twenty_basis_points_of_its_value(book):
    t = book["trades"]
    f = t[t["side"].isin(["buy", "sell"])]
    assert book["cfg"]["cost"] == 0.002
    assert len(f) == 452 and (f["value"] > 0).all()
    assert ((f["cost"] / f["value"] - 0.002).abs() < 1e-15).all()
    assert (f["value"] - f["units"] * f["price"]).abs().max() < 1e-6
    c = t[t["side"] == "cancel"]
    assert ((c["value"] == 0) & (c["cost"] == 0)).all()


def test_no_fill_on_a_non_session(book):
    sess = book["sessions"]
    t = book["trades"]
    assert t["date"].isin(sess).all(), sorted(set(t["date"]) - set(sess))
    end = book["nav"]["date"].iloc[-1]
    assert (t["date"] >= book["decisions"][0]).all() and (t["date"] < end).all()
    # the clock itself: open of the first decision date, every session's close,
    # open of the end date
    nav = book["nav"]
    assert list(nav["mark"].iloc[[0, -1]]) == ["open", "open"]
    assert (nav["mark"].iloc[1:-1] == "close").all() and sess.is_unique


def test_dividends_are_credited_only_to_held_units(book):
    dv = book["replay"]["div_units"]
    assert len(dv) == len(book["divs"]) == 125
    sess = book["sessions"].append(pd.DatetimeIndex([book["nav"]["date"].iloc[-1]]))
    assert dv["date"].isin(sess).all() and (dv["date"] > sess[0]).all()
    assert (dv["amount"] > 0).all()
    # held at the previous close: units worth more than a rupee, and the credit
    # is a plausible cash yield on them (under half the position's value, the
    # engine's own implausibility screen, Clarification 13)
    assert (dv["value_prev"] > 1.0).all(), dv[dv["value_prev"] <= 1.0]
    assert (dv["amount"] < 0.5 * dv["value_prev"]).all()


# ---------------------------------------------------------------- against the checker
def test_strategy_nav_equals_the_checker_at_every_mark(book):
    ck = pd.read_csv(CHECK / "nav.csv", parse_dates=["date"])
    nav = book["nav"]
    assert (nav[["date", "mark"]].to_numpy() == ck[["date", "mark"]].to_numpy()).all()
    cap = book["cfg"]["initial_capital"]
    for col in nav.columns[2:]:
        scale = cap if col in ("strategy", "universe_ew") else 1.0
        rel = (nav[col] / scale / ck[col] - 1).abs()
        assert rel.max() < 1e-9, (col, float(rel.max()))


def test_books_and_fills_equal_the_checker(book):
    ck_h = pd.read_csv(CHECK / "holdings.csv", parse_dates=["D"])
    h = book["hold"][book["hold"]["action"] != "sell"]
    a = set(zip(h["D"], h["symbol"], h["action"]))
    b = set(zip(ck_h["D"], ck_h["cid"], ck_h["status"].map({"new": "buy", "kept": "keep"})))
    assert a == b
    ck_t = pd.read_csv(CHECK / "trades.csv", parse_dates=["date"])
    f = book["trades"][book["trades"]["side"].isin(["buy", "sell"])].copy()
    f["sign"] = np.where(f["side"] == "buy", 1.0, -1.0)
    ck_t["sign"] = np.sign(ck_t["d"])
    m = f.merge(ck_t, left_on=["date", "symbol", "sign"], right_on=["date", "cid", "sign"],
                how="outer", indicator=True)
    assert (m["_merge"] == "both").all() and len(m) == 452
    cap = book["cfg"]["initial_capital"]
    assert ((m["value"] / cap / m["val"].abs() - 1).abs() < 1e-9).all()


# ---------------------------------------------------------------- with the database
needs_db = pytest.mark.skipif(not DB.exists(), reason="data/zen.duckdb not present")


@pytest.fixture(scope="module")
def db():
    from zen.universe import pit
    from zen.universe.identity import Identity
    con = pit.connect(read_only=True)
    ids = Identity.build(con)
    yield con, ids
    con.close()


def _units_prev(book, d, sym) -> float:
    """Units held at the close before session d, from the replay."""
    sess = book["sessions"]
    prev = sess[sess < d]
    if not len(prev):
        return 0.0
    p = book["replay"]["positions"][prev[-1]]
    if sym not in p:
        return 0.0
    fills = book["trades"][(book["trades"]["symbol"] == sym) &
                           (book["trades"]["side"].isin(["buy", "sell"])) &
                           (book["trades"]["date"] <= prev[-1])]
    return float((np.where(fills["side"] == "buy", 1, -1) * fills["units"]).sum())


@needs_db
def test_dividends_are_credited_on_ex_dates_and_none_is_missed(book, db):
    """Both ways: every credit is a cash dividend in the corporate-action feed
    whose ex-date maps to that session (the next session if the ex-date is not
    one), paid on the units held at the previous close in adjusted units; and
    every such dividend of a held stock was credited, unless the engine's
    implausibility screen dropped it (Clarification 13)."""
    from zen.portfolio import engine
    from zen.universe import pit
    con, ids = db
    sess = book["sessions"]
    start, end = sess[0], book["nav"]["date"].iloc[-1]
    dv = engine.dividends(con, start, end, ids)
    fac = pit.split_factors(con, through=end, ids=ids)
    dv["cum"] = pit.cumulative_factor(dv.rename(columns={"ex_date": "date"}), fac)
    cal = pit.sessions(con)
    dv["session"] = cal[cal.searchsorted(dv["ex_date"], side="left")]
    dv = dv[(dv["session"] > start) & (dv["session"] <= end)]
    traded = set(book["trades"]["symbol"])
    dv = dv[dv["symbol"].isin(traded)]
    dv["units_prev"] = [_units_prev(book, d, s) for d, s in zip(dv["session"], dv["symbol"])]
    due = dv[dv["units_prev"] > 0].copy()
    # the implausibility screen: more than half the last raw close before the ex-date
    prev_adj = [book["close_px"].loc[book["close_px"].index < d, s].iloc[-1]
                for d, s in zip(due["session"], due["symbol"])]
    due["screened"] = due["dps"] > 0.5 * np.array(prev_adj) / due["cum"]
    due["expected"] = due["units_prev"] * due["dps"] * due["cum"]
    due = due[~due["screened"]].groupby(["session", "symbol"], as_index=False)["expected"].sum()
    got = book["divs"].rename(columns={"date": "session"})
    m = due.merge(got, on=["session", "symbol"], how="outer", indicator=True)
    assert (m["_merge"] == "both").all(), m[m["_merge"] != "both"]
    assert ((m["amount"] / m["expected"] - 1).abs() < 1e-9).all()


@needs_db
def test_every_fill_is_on_a_session_the_stock_traded_at_its_adjusted_open(book, db):
    """A buy needs an EQ/BE trade that session; a sale an EQ/BE trade or, on a
    trade-for-trade session, a BZ trade (v2-spec, Clarification to A2). The
    price is that row's open (its close if the open is missing), adjusted by
    every split and bonus after the session up to the end date."""
    from zen.universe import pit
    con, ids = db
    end = book["nav"]["date"].iloc[-1]
    f = book["trades"][(book["trades"]["side"].isin(["buy", "sell"])) &
                       (book["trades"]["reason"] == "rebalance")].copy()
    tickers = sorted(set(ids.spells.loc[ids.spells["cid"].isin(set(f["symbol"])), "symbol"])
                     | set(f["symbol"]))
    days = sorted({d.date() for d in f["date"]})
    con.register("_t", pd.DataFrame({"symbol": tickers}))
    con.register("_d", pd.DataFrame({"date": pd.to_datetime(days)}))
    q = ("SELECT p.symbol, p.isin_code, p.date, p.open, p.close, p.turnover, {fb} AS fb "
         "FROM {tab} p JOIN _t USING (symbol) JOIN _d ON p.date = CAST(_d.date AS DATE) "
         "WHERE p.close > 0{extra}")
    px = pd.concat([con.execute(q.format(fb=0, tab="prices", extra="")).df(),
                    con.execute(q.format(fb=1, tab="prices_other",
                                         extra=" AND p.series = 'BZ'")).df()],
                   ignore_index=True)
    con.unregister("_t")
    con.unregister("_d")
    px["date"] = pd.to_datetime(px["date"])
    px["cid"] = ids.for_prices(px)
    px = (px.sort_values(["fb", "turnover", "isin_code"], ascending=[True, False, True])
            .drop_duplicates(["cid", "date"]))
    fac = pit.split_factors(con, through=end, ids=ids)
    px["cum"] = pit.cumulative_factor(px.rename(columns={"cid": "symbol", "symbol": "ticker"}),
                                      fac)
    px["raw"] = px["open"].where(px["open"] > 0, px["close"])
    j = f.merge(px, left_on=["symbol", "date"], right_on=["cid", "date"], how="left")
    assert j["cid"].notna().all(), j.loc[j["cid"].isna(), ["date", "symbol_x", "side"]]
    assert (j.loc[j["side"] == "buy", "fb"] == 0).all()             # never bought in BZ
    assert ((j["price"] / (j["raw"] * j["cum"]) - 1).abs() < 1e-9).all()
