"""Strategy v1 backtest: the four checks the build must pass before any number is read.

(a) the composite is future-blind at three in-sample decision dates, by the
    archive-truncation test in zen/validation/leak.py, plus a stricter version
    that truncates at the day BEFORE D (the information set is "strictly
    before D", which the standard test, truncating at D inclusive, cannot
    distinguish from "through D");
(b) a split does not move adjusted returns, NAV or the dividend credited;
(c) the buffer and the sector cap select exactly what the spec says;
(d) the holdout guard raises, in the engine and in the CLI.
"""

from __future__ import annotations

import logging
import tempfile
from datetime import date, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

from zen.portfolio import engine, metrics
from zen.signals import composite
from zen.universe import pit
from zen.validation import leak

logging.basicConfig(level=logging.INFO)

LEAK_DATES = [date(2019, 6, 3), date(2020, 11, 17), date(2022, 2, 15)]


@pytest.fixture(scope="module")
def con():
    c = pit.connect(read_only=True)
    yield c
    c.close()


@pytest.fixture(scope="module")
def static(con):
    return pit.StaticLabels.load(con)


# ---------------------------------------------------------------- (a) leak
@pytest.mark.parametrize("asof", LEAK_DATES)
def test_composite_future_blind(con, static, asof):
    cal = pit.sessions(con)
    assert pd.Timestamp(asof) in pit.decision_dates(cal), "test dates must be decision dates"
    report = leak.future_blindness(composite.Composite(static), con, asof, raise_on_fail=False)
    assert report["signals_full"] > 100, report
    assert report["passed"], report


def test_composite_ignores_decision_day_itself(con, static):
    """Truncate at D-1: nothing dated D (prices, filings, labels, actions) may matter.

    Also covers the point-in-time lender taxonomy vote (Clarification 28) and
    the scale screen (Clarification 30): both are derived from the connection
    at D, so a later filing cannot change them."""
    D = LEAK_DATES[-1]
    full, _ = composite.compute(con, D, static)
    snap_full = pit.build_snapshot(con, D, static)
    with tempfile.TemporaryDirectory() as tmp:
        tcon = leak._truncated_copy(con, D - timedelta(days=1), Path(tmp) / "t.duckdb")
        try:
            trunc, _ = composite.compute(tcon, D, static)
            snap_trunc = pit.build_snapshot(tcon, D, static)
        finally:
            tcon.close()
    full.attrs, trunc.attrs = {}, {}
    pd.testing.assert_frame_equal(full, trunc)
    pd.testing.assert_frame_equal(snap_full.taxonomy.sort_index(), snap_trunc.taxonomy.sort_index())
    pd.testing.assert_frame_equal(snap_full.scale_dropped, snap_trunc.scale_dropped)


def test_known_leak_is_caught_by_the_same_harness(con):
    """The detector must still fail a cheat when pointed at this archive."""
    from tests.test_leak import Leaky
    with pytest.raises(leak.LeakDetected):
        leak.future_blindness(Leaky(), con, LEAK_DATES[0])


# ---------------------------------------------------------------- synthetic archive
def _synthetic(split_on: int = 6, dividend_on: int = 8):
    """Two stocks over 12 sessions. AAA's true value is flat at 100/share pre-split;
    it splits 10 -> 2 (factor 0.2) on session `split_on`, and pays Rs 1 per
    (post-split) share on session `dividend_on`. BBB never moves."""
    c = duckdb.connect(":memory:")
    days = pd.bdate_range("2021-01-04", periods=12)
    rows = []
    for i, d in enumerate(days):
        raw = 100.0 if i < split_on else 20.0
        rows.append((d.date(), "AAA", "EQ", "INE000A01011", raw, raw, raw, raw, None, 1000, 1e7, 10))
        rows.append((d.date(), "BBB", "EQ", "INE000B01011", 50.0, 50.0, 50.0, 50.0, None, 1000, 1e7, 10))
    c.execute(store_schema())
    c.executemany("INSERT INTO prices VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    c.execute("CREATE TABLE corpactions (ex_date DATE, symbol VARCHAR, isin VARCHAR, company VARCHAR, "
              "action VARCHAR, subject VARCHAR, factor DOUBLE)")
    c.execute("INSERT INTO corpactions VALUES (?, 'AAA', NULL, NULL, 'split', "
              "'Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share', 0.2)",
              [days[split_on].date()])
    c.execute("INSERT INTO corpactions VALUES (?, 'AAA', NULL, NULL, 'dividend', "
              "'Interim Dividend - Rs 1 Per Share', NULL)", [days[dividend_on].date()])
    return c, days


def store_schema() -> str:
    from zen.data import store
    return store.SCHEMA


def test_split_does_not_move_adjusted_returns():
    c, days = _synthetic()
    end = days[-1]
    panel = engine.build_panel(c, ["AAA", "BBB"], days[2], end, is_end=end)
    a = panel.col("AAA")
    closes = panel.close[:-1, a]                     # last session is the open-only end mark
    r = closes[1:] / closes[:-1] - 1
    assert np.allclose(r, 0.0), r                    # the raw series falls 80%; adjusted does not
    assert engine.daily_returns_check(panel).empty
    # Point-in-time adjustment inside the signal: factors with ex_date < D only.
    fac = pit.split_factors(c, before=days[-1])
    assert len(fac) == 1 and fac["factor"].iloc[0] == pytest.approx(0.2)


def test_split_leaves_nav_flat_and_dividend_in_post_split_shares():
    split_on, div_on = 6, 8
    c, days = _synthetic(split_on, div_on)
    end = days[-1]
    panel = engine.build_panel(c, ["AAA"], days[2], end, is_end=end)
    cfg = engine.Config(n=1, buffer_mult=1, sector_cap=None, cost=0.0, initial_capital=1000.0)
    res = engine.simulate(panel, [days[2]], lambda D, held: (["AAA"], {}), cfg, is_end=end)
    nav = res.nav.set_index(["date", "mark"])["nav"]
    before_div = nav[nav.index.get_level_values("date") < days[div_on]]
    assert np.allclose(before_div.to_numpy(), 1000.0), before_div   # split day included
    # 1000 rupees bought 10 shares at 100; after the 1:5 split that is 50 shares,
    # so Rs 1 per post-split share credits Rs 50 -- not Rs 10, not Rs 250.
    after = nav[nav.index.get_level_values("date") >= days[div_on]]
    assert np.allclose(after.to_numpy(), 1050.0), after


# ---------------------------------------------------------------- (c) buffer & cap
def _ranked():
    syms = [f"S{i:02d}" for i in range(1, 31)]
    sectors = (["Steel"] * 5 + ["IT"] * 2 + [None] * 3 + ["Pharma"] * 20)
    return pd.DataFrame({"rank": range(1, 31), "sector": sectors}, index=pd.Index(syms, name="symbol"))


def test_sector_cap_skips_fourth_name_in_a_sector():
    r = _ranked()
    got = engine.select_top(r, held=set(), cfg=engine.Config(n=10, buffer_mult=2, sector_cap=3))
    # Steel S01-S03 in, S04-S05 skipped; IT S06-S07; unlabelled S08-S10 are never capped;
    # then Pharma S11, S12 fill to ten.
    assert got == ["S01", "S02", "S03", "S06", "S07", "S08", "S09", "S10", "S11", "S12"]
    nocap = engine.select_top(r, held=set(), cfg=engine.Config(n=10, sector_cap=None))
    assert nocap == [f"S{i:02d}" for i in range(1, 11)]


def test_buffer_keeps_holdings_within_2n_and_counts_them_against_the_cap():
    r = _ranked()
    held = {"S15", "S20", "S25", "S04"}       # Pharma 15, 20; Pharma 25 (outside 2N); Steel 4
    got = engine.select_top(r, held=held, cfg=engine.Config(n=10, buffer_mult=2, sector_cap=3))
    assert "S15" in got and "S20" in got       # ranks <= 20 are kept
    assert "S25" not in got                    # rank 25 > 2N is sold
    assert "S04" in got                        # kept holding counts toward Steel's cap
    assert sum(r.loc[s, "sector"] == "Steel" for s in got) == 3
    # Kept (by rank) S04, S15, S20; fills S01, S02 (Steel reaches 3 with S04), S03 and
    # S05 skipped by the cap, IT S06-S07, unlabelled S08-S10: ten before any Pharma fill.
    assert got == ["S04", "S15", "S20", "S01", "S02", "S06", "S07", "S08", "S09", "S10"]
    nobuf = engine.select_top(r, held=held, cfg=engine.Config(n=10, buffer_mult=1, sector_cap=3))
    assert "S15" not in nobuf and "S20" not in nobuf


def test_buffer_drops_holdings_that_left_the_universe():
    r = _ranked()
    got = engine.select_top(r, held={"GONE"}, cfg=engine.Config(n=10, buffer_mult=3, sector_cap=None))
    assert "GONE" not in got


# ---------------------------------------------------------------- (d) holdout guard
def test_holdout_guard_raises(con):
    cal = pit.sessions(con)
    is_end = engine.in_sample_end(cal)
    assert is_end == pd.Timestamp("2023-02-15")
    engine.guard(is_end, is_end)                                     # the boundary itself is fine
    with pytest.raises(engine.HoldoutLocked):
        engine.guard(is_end + pd.Timedelta(days=1), is_end)
    later = cal[cal > is_end][5]
    with pytest.raises(engine.HoldoutLocked):
        engine.build_panel(con, ["TCS"], pd.Timestamp("2022-11-15"), later, is_end)
    with pytest.raises(engine.HoldoutLocked):
        engine.index_nav(con, "Nifty 500", pd.Timestamp("2022-11-15"), later, is_end)


def test_holdout_guard_raises_in_cli():
    from jobs import backtest_v1
    with pytest.raises(engine.HoldoutLocked):
        backtest_v1.main(["--config", "base", "--end", "2023-06-01", "--no-record"])


def test_simulate_refuses_a_panel_past_the_end():
    c, days = _synthetic()
    panel = engine.build_panel(c, ["AAA"], days[2], days[-1], is_end=days[-1])
    cfg = engine.Config(n=1, buffer_mult=1, sector_cap=None, cost=0.0)
    with pytest.raises(engine.HoldoutLocked):
        engine.simulate(panel, [days[2]], lambda D, h: (["AAA"], {}), cfg, is_end=days[-3])


# ---------------------------------------------------------------- helpers
@pytest.mark.parametrize("subject,amount", [
    ("Interim Dividend - Rs 3.50 Per Share", 3.5),
    ("Annual General Meeting/Dividend - Re 0.50 Per Share", 0.5),
    ("Final Dividend - Rs 8 Per Share And Special Dividend - Rs 10 Per Share", 18.0),
    ("Interim Dividend - Rs 5 Per Share / Special Dividend - Rs 40 Per Share", 45.0),
    ("Bonus 1:1/Dividend- Rs 7 Per Share", 7.0),
    ("Dividend Rs - 2.50 Per Share", 2.5),
    ("Annual General Meeting/Dividend - Rs.0125 Per Share", 0.125),
    # Clarification 24
    ("Interim Dividend - Rs 6 Per Share Special Interim Dividend - Rs 10 Per Share", 16.0),
    ("Dividend - Rs 106 Per Share Special Dividend 243 Per Share", 349.0),
    ("Annual General Meeting/ Dividend - 12.50 Per Share", 12.5),
    ("Dividend - 30%", None),
    ("Distribution - Rs 3 Per Unit Consisting Of Rs 1 Per Unit As Dividend", None),
    ("Interim Dividend", None),
    ("Buyback", None),
])
def test_parse_dividend(subject, amount):
    got = engine.parse_dividend(subject)
    assert got == (pytest.approx(amount) if amount is not None else None)


def test_bootstrap_ci_brackets_point():
    rng = np.random.default_rng(0)
    a, b = rng.normal(5e-4, 0.01, 800), rng.normal(2e-4, 0.01, 800)
    ci = metrics.block_bootstrap_ci(a, b, n_boot=500)
    assert ci["lo"] <= ci["point"] <= ci["hi"]


def test_quantile_and_universe_ew_paths_on_synthetic_panel():
    """Diagnostics and the benchmark run end to end without touching real returns."""
    c, days = _synthetic()
    end = days[-1]
    panel = engine.build_panel(c, ["AAA", "BBB"], days[2], end, is_end=end)
    ranks = pd.DataFrame({"D": [days[2]] * 2, "symbol": ["AAA", "BBB"], "rank": [1, 2],
                          "composite": [0.9, 0.1], "sector": [None, None],
                          "sector_source": ["none", "none"]})
    q1 = engine.run_quantile(panel, ranks, [days[2]], "composite", 1, is_end=end, n_q=2)
    q2 = engine.run_quantile(panel, ranks, [days[2]], "composite", 2, is_end=end, n_q=2)
    assert q1.nav["nav"].iloc[-1] == pytest.approx(q1.nav["nav"].iloc[0] * 1.05)  # AAA's dividend
    assert q2.nav["nav"].iloc[-1] == pytest.approx(q2.nav["nav"].iloc[0])
    ew = engine.run_universe_ew(panel, ranks, [days[2]], engine.Config(cost=0.002), is_end=end)
    # Buys are scaled so value + 0.2% cost fits the cash: 1/1.002 invested, half in each;
    # AAA's 5% dividend on its half adds 0.025/1.002.
    assert ew.nav["nav"].iloc[-1] / ew.nav["nav"].iloc[0] == pytest.approx(1.025 / 1.002, rel=1e-9)
    rep = metrics.full_report(q1.nav, {"universe_ew": ew.nav})
    assert "bootstrap_vs_universe_ew" in rep


# ---------------------------------------------------------------- identity (Clarification 23)
def test_identity_links_a_rename_and_not_a_reused_symbol():
    from zen.universe.identity import Identity, link
    cal = pd.bdate_range("2019-01-01", periods=400)
    keys = pd.DataFrame([
        # renamed on the same ISIN: OLDCO -> NEWCO
        ("OLDCO", "INE001A01011", cal[0], cal[99]),
        ("NEWCO", "INE001A01011", cal[100], cal[399]),
        # face-value split changes the ISIN, same symbol
        ("SPLT", "INE002A01011", cal[0], cal[49]),
        ("SPLT", "INE002A01029", cal[50], cal[399]),
        # a symbol reused by a different company 200 sessions later
        ("REUSE", "INE003A01011", cal[0], cal[99]),
        ("REUSE", "INE004A01011", cal[300], cal[399]),
    ], columns=["symbol", "isin", "first", "last"])
    ids = Identity(link(keys, cal))
    cid = ids.spells.set_index(["symbol", "isin"])["cid"]
    assert cid[("OLDCO", "INE001A01011")] == cid[("NEWCO", "INE001A01011")] == "NEWCO"
    assert cid[("SPLT", "INE002A01011")] == cid[("SPLT", "INE002A01029")]
    assert cid[("REUSE", "INE003A01011")] != cid[("REUSE", "INE004A01011")]
    # a filing stored under the later symbol, broadcast before it existed, maps back
    ev = pd.DataFrame({"symbol": ["NEWCO", "OLDCO", "REUSE", "REUSE", "NOSUCH"],
                       "d": [cal[10], cal[10], cal[10], cal[350], cal[10]]})
    got = list(ids.for_events(ev, "symbol", "d"))
    assert got[:2] == ["NEWCO", "NEWCO"]
    assert got[2] == cid[("REUSE", "INE003A01011")] and got[3] == cid[("REUSE", "INE004A01011")]
    assert got[4] == "NOSUCH"


def test_renamed_company_keeps_its_filings(con, static):
    """CADILAHC's filings are stored under ZYDUSLIFE (renamed Feb 2022); at the
    Feb 2021 decision date it must still be in the universe."""
    ranked, _ = composite.compute(con, pd.Timestamp("2021-02-15"), static)
    assert "ZYDUSLIFE" in ranked.index
    assert ranked.at["ZYDUSLIFE", "ticker"] == "CADILAHC"


# ---------------------------------------------------------------- rule 4 (Clarifications 28, 29)
def _snap_for_flags(symbols, taxonomy=None, pit_labels=None, bankfmt=()):
    idx = pd.Index(symbols, name="symbol")
    tax = taxonomy if taxonomy is not None else pd.DataFrame(
        {"n_new": 0, "n_lender": 0}, index=idx)
    return pit.Snapshot(D=pd.Timestamp("2021-02-15"), cal=pd.DatetimeIndex([]),
                        px=pd.DataFrame(), quarters=pd.DataFrame(), bankfmt=set(bankfmt),
                        industry_pit=pit_labels if pit_labels is not None else pd.Series(dtype=object),
                        factors=pd.DataFrame(), taxonomy=tax)


def test_todays_nse_list_does_not_decide_rule_4():
    """LA-1: a stock today's list flags (from its 2025 filing format) but whose
    filings known at D are industrial is NOT a lender; a stock whose own filings
    known at D vote NBFC is, whatever today's list says."""
    tax = pd.DataFrame({"n_new": [8, 8, 8], "n_lender": [0, 5, 2]},
                       index=pd.Index(["SUNCLAY", "NBFCCO", "ODDFILE"], name="symbol"))
    static = pit.StaticLabels(nse_lenders={"SUNCLAY"})
    lf = pit.lender_flags(_snap_for_flags(tax.index, tax), static, tax.index)
    assert not lf.at["SUNCLAY", "lender"]          # nse_list is diagnostics only
    assert lf.at["SUNCLAY", "nse_list"]
    assert lf.at["NBFCCO", "lender"] and lf.at["NBFCCO", "pit_taxonomy"]
    assert not lf.at["ODDFILE", "lender"]          # 2 of 8 filings mis-tagged: no flip


def test_taxonomy_backfill_applies_only_before_any_nbfc_era_filing():
    tax = pd.DataFrame({"n_new": [0, 4], "n_lender": [0, 0]},
                       index=pd.Index(["EARLY", "LATER"], name="symbol"))
    static = pit.StaticLabels(taxonomy_fill={"EARLY", "LATER"})
    lf = pit.lender_flags(_snap_for_flags(tax.index, tax), static, tax.index)
    assert lf.at["EARLY", "lender"] and lf.at["EARLY", "tax_fill"]
    assert not lf.at["LATER", "lender"]            # its own filings at D outvote the backfill


def test_taxonomy_votes_count_only_latest_four_quarters_after_the_nbfc_taxonomy():
    rows = []
    # an NBFC: pre-2020 filings in INDAS (no NBFC taxonomy yet), then NBFC_INDAS,
    # with one INDAS quarter in between (as Capri Global filed Sep-2020)
    for pe, bd, t in [("2019-06-30", "2019-08-10", "INDAS"), ("2019-09-30", "2019-11-10", "INDAS"),
                      ("2019-12-31", "2020-02-10", "NBFC_INDAS"), ("2020-03-31", "2020-06-10", "NBFC_INDAS"),
                      ("2020-06-30", "2020-08-10", "NBFC_INDAS"), ("2020-09-30", "2020-11-10", "INDAS")]:
        for cons in (False, True):
            rows.append(("NBFC", pd.Timestamp(pe), pd.Timestamp(bd), cons, t))
    f = pd.DataFrame(rows, columns=["symbol", "period_end", "broadcast_dt", "consolidated", "taxonomy"])
    f["q"] = f["period_end"].dt.year * 4 + (f["period_end"].dt.month - 1) // 3
    v = pit.taxonomy_votes(f)
    assert v.at["NBFC", "n_new"] == 8 and v.at["NBFC", "n_lender"] == 6
    early = pit.taxonomy_votes(f[f["broadcast_dt"] < "2020-01-01"])
    assert early.at["NBFC", "n_new"] == 0          # the backfill decides at such a D


def test_taxonomy_flag_on_real_filings(con, static):
    """Membership only, no prices after D and no returns: companies that were
    industrial at D are not lenders although today's list flags them, and
    genuine lenders are still out."""
    at = {}
    for D in ("2019-06-03", "2021-02-15"):
        snap = pit.build_snapshot(con, D, static)
        pit.universe(snap, static)
        at[D] = snap.lenders
    lf19, lf21 = at["2019-06-03"], at["2021-02-15"]
    # Gujarat Fluorochemicals (GUJFLUORO, now GFLLIMITED) and Sundaram-Clayton
    # (SUNCLAYLTD, now TVSHLTD) were manufacturers in 2019.
    for s in ("GFLLIMITED", "TVSHLTD"):
        assert s in lf19.index and not lf19.at[s, "lender"], s
    for s in ("CGCL", "ABCAPITAL", "SAMMAANCAP", "MUTHOOTCAP"):
        assert lf19.at[s, "lender"], s
    for s in ("SBICARD", "CGCL", "CREDITACC", "HUDCO"):
        assert lf21.at[s, "lender"], s


def test_name_net_skips_labelled_companies():
    """LA-2: Oracle Financial Services Software is labelled 'Computers - Software';
    the word 'Financial' in its name must not make it a lender. An unlabelled
    'X Finance Ltd' still is."""
    syms = pd.Index(["OFSS", "XFIN"], name="symbol")
    static = pit.StaticLabels(
        backfill_industry=pd.Series({"OFSS": "Computers - Software"}),
        names=pd.Series({"OFSS": "Oracle Financial Services Software Limited",
                         "XFIN": "X Finance Limited"}))
    lf = pit.lender_flags(_snap_for_flags(syms), static, syms)
    assert not lf.at["OFSS", "lender"]
    assert lf.at["XFIN", "lender"] and lf.at["XFIN", "name"]


# ---------------------------------------------------------------- data quality (Clarification 30)
def _quarters(sym, revs, shares, emp=None, cons=False, start="2020-03-31"):
    pe = pd.date_range(start, periods=len(revs), freq="QE")
    f = pd.DataFrame({"symbol": sym, "consolidated": cons, "period_end": pe,
                      "broadcast_dt": pe + pd.Timedelta(days=45),
                      "revenue": revs, "total_income": revs, "shares_implied": shares,
                      "employee_cost": emp if emp is not None else [r * 0.1 for r in revs]})
    f["q"] = f["period_end"].dt.year * 4 + (f["period_end"].dt.month - 1) // 3
    return f


def test_scale_screen_drops_a_unit_error_but_not_a_real_collapse():
    # LODHA-style: one quarter with every rupee line 100x low, EPS intact
    bad = _quarters("UNIT", [2e10, 2.1e10, 3e8, 2.2e10, 1.9e10], [4.8e8, 4.8e8, 4.8e6, 4.8e8, 4.8e8])
    # a lockdown quarter: revenue down 100x, employee cost and shares unchanged
    covid = _quarters("SHUT", [5e9, 5e9, 5e7, 4e9, 5e9], [1e8] * 5, emp=[5e8] * 5)
    # a mis-scaled FIRST filing (TCI Mar-2018) must not drag the later ones out
    first = _quarters("FIRST", [6e7, 6e9, 6.2e9, 6.4e9, 6.6e9, 6.1e9], [7.6e5] + [7.6e7] * 5)
    f = pd.concat([bad, covid, first], ignore_index=True)
    got = f.loc[pit.scale_errors(f), ["symbol", "period_end"]]
    assert list(got["symbol"]) == ["UNIT", "FIRST"]
    assert got.iloc[0]["period_end"] == bad["period_end"][2]
    assert got.iloc[1]["period_end"] == first["period_end"][0]


def test_scale_screen_on_real_filings(con, static):
    """At 2022-06-01 the mis-scaled Mar-2022 filings (LODHA standalone, UBL and
    IRCON both bases) are treated as not filed, and the share counts come out at
    the right scale. Membership and fundamentals only; no returns."""
    D = pd.Timestamp("2022-06-01")
    snap = pit.build_snapshot(con, D, static)
    dropped = snap.scale_dropped
    key = set(zip(dropped["symbol"], dropped["consolidated"], dropped["period_end"]))
    mar22 = pd.Timestamp("2022-03-31")
    assert ("LODHA", False, mar22) in key and ("LODHA", True, mar22) not in key
    assert ("UBL", False, mar22) in key and ("UBL", True, mar22) in key
    assert ("IRCON", False, mar22) in key
    ranked, _ = composite.compute(con, D, static)
    assert ranked.at["LODHA", "shares"] == pytest.approx(4.84e8, rel=0.05)
    assert ranked.at["UBL", "shares"] == pytest.approx(2.645e8, rel=0.05)
    # ARVIND's standalone Mar-2022 EPS is -373: the consolidated count is used
    assert ranked.at["ARVIND", "shares_fix"] == "other_basis"
    assert ranked.at["ARVIND", "shares"] == pytest.approx(2.6e8, rel=0.1)


def test_share_count_check_uses_other_basis_or_carries_the_reference():
    q = pd.concat([_quarters("A", [1e9] * 5, [1e8, 1e8, 1e8, 1e8, 1e6]),
                   _quarters("A", [1.1e9] * 5, [1.02e8] * 5, cons=True),
                   _quarters("B", [1e9] * 5, [1e8, 1e8, 1e8, 1e8, 1e6])], ignore_index=True)
    # a 1:1 bonus between the last two filings: the reference doubles
    fac = pd.DataFrame({"symbol": ["B"], "ex_date": [q["broadcast_dt"].max() - pd.Timedelta(days=30)],
                        "factor": [0.5]})
    lq = q[q["q"] == q.groupby("symbol")["q"].transform("max")]
    sa = lq[~lq["consolidated"]].set_index("symbol")
    co = lq[lq["consolidated"]].set_index("symbol")
    syms = pd.Index(["A", "B"])
    shares = sa["shares_implied"].reindex(syms)
    bdt = sa["broadcast_dt"].reindex(syms)
    sh, _, fix = pit._check_share_count(q, syms, shares, bdt, sa, co, fac)
    assert fix["A"] == "other_basis" and sh["A"] == pytest.approx(1.02e8)
    assert fix["B"] == "carried_reference" and sh["B"] == pytest.approx(2e8)


# ---------------------------------------------------------------- benchmarks (Clarification 17)
def test_tri_nav_uses_official_tri_and_refuses_the_holdout(tmp_path):
    c = duckdb.connect(":memory:")
    days = pd.bdate_range("2021-01-04", periods=6)
    c.execute("CREATE TABLE indices (index_name VARCHAR, date DATE, open DOUBLE, close DOUBLE)")
    # price index flat at 100 except the end date opens at 102
    rows = [("Nifty 500", d.date(), 100.0, 100.0) for d in days[:-1]] + \
           [("Nifty 500", days[-1].date(), 102.0, 999.0)]
    c.executemany("INSERT INTO indices VALUES (?,?,?,?)", rows)
    tri = pd.DataFrame({"index_name": "NIFTY 500", "date": days,
                        "tri": [1000, 1000, 1010, 1010, 1020, 5000.0], "net_tri": 0.0})
    p = tmp_path / "tri.parquet"
    tri.to_parquet(p)
    nav = engine.tri_nav(c, "NIFTY 500", "Nifty 500", days[1], days[-1], is_end=days[-1], path=p)
    # open of start: TRI(d0) x 100/100 = 1000; closes 1000, 1010, 1010, 1020;
    # end open: TRI(d4) 1020 x 102/100. The end date's TRI (5000) is never read.
    assert list(nav["mark"]) == ["open", "close", "close", "close", "close", "open"]
    assert nav["nav"].tolist() == pytest.approx([1.0, 1.0, 1.01, 1.01, 1.02, 1.02 * 1.02])
    with pytest.raises(engine.HoldoutLocked):
        engine.tri_nav(c, "NIFTY 500", "Nifty 500", days[1], days[-1], is_end=days[-2], path=p)
