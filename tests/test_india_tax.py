"""A6: Indian delivery costs and capital gains tax (zen/portfolio/india_tax.py).

Every expected figure below is worked by hand in the comment beside it. The
ledgers are synthetic; no backtest output is read and no strategy return is
computed anywhere here.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date

import numpy as np
import pandas as pd
import pytest

from zen.portfolio import india_tax as it

CESS = 1.04
APRIL = it.Params(tax_timing="april")          # the pre-fix payment convention


def piece(buy, sell, gain, units=1.0, symbol="A"):
    """A FIFO piece with a given gain: cost 1,000,000, proceeds 1,000,000 + gain."""
    b, s = pd.Timestamp(buy).date(), pd.Timestamp(sell).date()
    return it.Piece(symbol, b, s, units, 1_000_000.0, 1_000_000.0 + gain, it.is_long_term(b, s))


# ---------------------------------------------------------------- holding period
def test_twelve_month_boundary():
    # s.2(42A): "not more than twelve months" is short term
    assert not it.is_long_term("2021-06-15", "2022-06-15")      # exactly 12 months
    assert it.is_long_term("2021-06-15", "2022-06-16")          # one day more
    assert not it.is_long_term("2021-06-15", "2021-12-31")
    # leap day: 29 Feb 2020 + 12 months is 28 Feb 2021 (the pandas DateOffset rule)
    assert not it.is_long_term("2020-02-29", "2021-02-28")
    assert it.is_long_term("2020-02-29", "2021-03-01")
    assert it.plus_months("2024-01-31", 1) == date(2024, 2, 29)
    assert it.plus_months("2024-05-31", -3) == date(2024, 2, 29)


def test_boundary_changes_the_tax():
    # gain 50,000 sold on the anniversary: short term, 15% = 7,500, x1.04 = 7,800
    y, _ = it.year_tax(2022, [piece("2021-06-15", "2022-06-15", 50_000)], [], [])
    assert y.total_tax == pytest.approx(7_800.0)
    # one day later: long term, under the Rs 1 lakh exemption, nil
    y, _ = it.year_tax(2022, [piece("2021-06-15", "2022-06-16", 50_000)], [], [])
    assert y.total_tax == pytest.approx(0.0)
    assert y.exemption_used == pytest.approx(50_000)


def test_financial_year():
    assert it.fy_of("2024-03-31") == 2023 and it.fy_of("2024-04-01") == 2024
    assert it.fy_label(2024) == "FY2024-25"


# ---------------------------------------------------------------- 23 July 2024
def test_rates_change_on_23_july_2024():
    assert it.cg_rates("2024-07-22") == (0.15, 0.10)
    assert it.cg_rates("2024-07-23") == (0.20, 0.125)


def test_short_term_either_side_of_the_change_in_one_year():
    # FY2024-25: 100,000 ST on 22 Jul at 15% = 15,000; 100,000 ST on 23 Jul at 20% = 20,000
    # 35,000 x 1.04 = 36,400
    ps = [piece("2024-01-10", "2024-07-22", 100_000), piece("2024-01-10", "2024-07-23", 100_000)]
    y, _ = it.year_tax(2024, ps, [], [])
    assert y.cg_tax == pytest.approx(35_000)
    assert y.total_tax == pytest.approx(36_400)


def test_fy2024_25_exemption_is_125k_for_the_whole_year():
    # CBDT FAQ (PIB 2036604): 1.25 lakh applies to FY 2024-25, sales before 23 July included.
    # LT 200,000 sold 22 Jul 2024 at 10%: (200,000 - 125,000) x 10% = 7,500; x1.04 = 7,800
    y, _ = it.year_tax(2024, [piece("2023-01-10", "2024-07-22", 200_000)], [], [])
    assert y.exemption == 125_000
    assert y.total_tax == pytest.approx(7_800)


def test_exemption_goes_to_the_higher_rate_first_in_the_split_year():
    # LT 100,000 at 10% (before) and 100,000 at 12.5% (after). Exemption 125,000
    # takes all the 12.5% gains and 25,000 of the 10%: 75,000 x 10% = 7,500.
    ps = [piece("2023-01-10", "2024-07-22", 100_000), piece("2023-01-10", "2024-08-01", 100_000)]
    y, _ = it.year_tax(2024, ps, [], [])
    assert y.cg_tax == pytest.approx(7_500)
    assert y.taxable == {"LT@0.1": pytest.approx(75_000)}


def test_old_and_new_long_term_years():
    # FY2023-24: (200,000 - 100,000) x 10% = 10,000
    y, _ = it.year_tax(2023, [piece("2022-01-10", "2023-06-01", 200_000)], [], [])
    assert y.cg_tax == pytest.approx(10_000)
    # FY2025-26: (200,000 - 125,000) x 12.5% = 9,375
    y, _ = it.year_tax(2025, [piece("2024-01-10", "2025-06-01", 200_000)], [], [])
    assert y.cg_tax == pytest.approx(9_375)


def test_annual_exemption_edge():
    y, _ = it.year_tax(2023, [piece("2022-01-10", "2023-06-01", 100_000)], [], [])
    assert y.cg_tax == 0.0
    y, _ = it.year_tax(2023, [piece("2022-01-10", "2023-06-01", 100_001)], [], [])
    assert y.cg_tax == pytest.approx(0.10)                   # 1 rupee x 10%
    assert y.total_tax == 0.0                                # s.288B: Rs 0.104 rounds to nil


# ---------------------------------------------------------------- set-off
def test_current_year_set_off_order():
    # FY2025-26: ST loss 50k, LT loss 30k, ST gain 40k, LT gain 200k.
    # LT loss vs LT gain -> LT 170k. ST loss vs ST gain 40k -> 0, the other 10k vs LT -> 160k.
    # Exemption 125k -> 35k x 12.5% = 4,375; x1.04 = 4,550. Nothing carried.
    ps = [piece("2025-05-01", "2025-06-01", -50_000), piece("2024-01-01", "2025-06-01", -30_000),
          piece("2025-05-01", "2025-07-01", 40_000), piece("2024-01-01", "2025-07-01", 200_000)]
    y, out = it.year_tax(2025, ps, [], [])
    assert y.total_tax == pytest.approx(4_550)
    assert out == []


def test_long_term_loss_never_touches_short_term_gains():
    # LT loss 50k, ST gain 100k: ST taxed in full, 20% = 20,000; LT loss carried
    ps = [piece("2024-01-01", "2025-06-01", -50_000), piece("2025-05-01", "2025-07-01", 100_000)]
    y, out = it.year_tax(2025, ps, [], [])
    assert y.cg_tax == pytest.approx(20_000)
    assert [(l.fy, l.kind, l.amount) for l in out] == [(2025, "LT", pytest.approx(50_000))]


def test_short_term_loss_offsets_long_term_gain():
    # ST loss 100k, LT gain 300k (FY2025): LT 200k - 125k = 75k x 12.5% = 9,375
    ps = [piece("2025-05-01", "2025-06-01", -100_000), piece("2024-01-01", "2025-07-01", 300_000)]
    y, _ = it.year_tax(2025, ps, [], [])
    assert y.cg_tax == pytest.approx(9_375)


def test_brought_forward_losses_keep_their_character():
    # bf LT loss 50k cannot touch a ST gain of 50k: 20% = 10,000; LT loss carried on
    y, out = it.year_tax(2025, [piece("2025-05-01", "2025-07-01", 50_000)], [],
                         [it.Loss(2023, "LT", 50_000)])
    assert y.cg_tax == pytest.approx(10_000)
    assert [(l.fy, l.kind, l.amount) for l in out] == [(2023, "LT", 50_000)]
    # bf ST loss 30k does: (50k - 30k) x 20% = 4,000
    y, out = it.year_tax(2025, [piece("2025-05-01", "2025-07-01", 50_000)], [],
                         [it.Loss(2023, "ST", 30_000)])
    assert y.cg_tax == pytest.approx(4_000) and out == []


def test_brought_forward_is_set_off_before_the_exemption():
    # LT gain 150k, bf LT loss 50k: 100k left, under 125k: nil, and the loss is used up
    y, out = it.year_tax(2025, [piece("2024-01-01", "2025-07-01", 150_000)], [],
                         [it.Loss(2022, "LT", 50_000)])
    assert y.cg_tax == 0.0 and y.bf_used_lt == pytest.approx(50_000) and out == []


def test_carry_forward_eight_years_oldest_first():
    # ST losses 100k from FY2019-20 and 100k from FY2022-23 brought into FY2027-28
    # (8 years after 2019: still alive). ST gain 60k uses the 2019 loss first.
    carried = [it.Loss(2022, "ST", 100_000), it.Loss(2019, "ST", 100_000)]
    y, out = it.year_tax(2027, [piece("2027-05-01", "2027-07-01", 60_000)], [], carried)
    assert y.cg_tax == 0.0 and y.expired == 0.0
    assert sorted((l.fy, l.amount) for l in out) == [(2019, pytest.approx(40_000)),
                                                     (2022, 100_000)]
    # into FY2028-29 the 2019 loss (9 years on) has lapsed: 40k expired, 2022 loss usable
    y, out = it.year_tax(2028, [piece("2028-05-01", "2028-07-01", 150_000)], [], out)
    assert y.expired == pytest.approx(40_000)
    assert y.cg_tax == pytest.approx(0.20 * 50_000)          # 150k - 100k at 20%
    assert out == []


def test_loss_years_carry_forward_through_a_chain():
    carried = []
    y, carried = it.year_tax(2021, [piece("2021-05-01", "2021-07-01", -80_000)], [], carried)
    assert y.total_tax == 0 and y.cf_st == pytest.approx(80_000)
    y, carried = it.year_tax(2022, [], [], carried)
    assert y.cf_st == pytest.approx(80_000)
    # FY2023: ST gain 100k less 80k bf = 20k x 15% = 3,000
    y, carried = it.year_tax(2023, [piece("2023-05-01", "2023-07-01", 100_000)], [], carried)
    assert y.cg_tax == pytest.approx(3_000) and carried == []


# ---------------------------------------------------------------- rounding (s.288A/288B)
def test_round_to_ten_rupees():
    # paise ignored, then a last digit of 5 or more rounds up
    assert it.round_10(4.99) == 0 and it.round_10(5.0) == 10 and it.round_10(14.99) == 10
    assert it.round_10(15) == 20 and it.round_10(405.6) == 410 and it.round_10(-15.2) == -20


def test_year_tax_rounds_income_and_tax():
    # ST gain 33,333.33 (FY2021-22): total income 33,333.33 -> 33,330 (s.288A), the -3.33
    # landing on slab income: tax = (33,333.33 x 15% - 3.33 x 30%) x 1.04 = 5,198.96
    # -> 5,200 (s.288B). Off: 5,200.00 exactly unrounded is 5,199.99.
    y, _ = it.year_tax(2021, [piece("2021-05-01", "2021-07-01", 33_333.33)], [], [])
    assert y.total_income_rounded == 33_330 and y.total_tax == 5_200
    assert y.rounding_tax == pytest.approx(-3.33 * 0.30)
    y, _ = it.year_tax(2021, [piece("2021-05-01", "2021-07-01", 33_333.33)], [], [],
                       it.Params(statutory_rounding=False))
    assert y.total_tax == pytest.approx(33_333.33 * 0.15 * CESS)


# ---------------------------------------------------------------- dividends
def test_dividends_exempt_before_april_2020_then_slab():
    divs = [(pd.Timestamp("2020-03-31"), 10_000.0)]
    y, _ = it.year_tax(2019, [], divs, [])
    assert y.total_tax == 0.0 and y.dividends == 10_000
    # 10,000 x 30% = 3,000; x1.04 = 3,120
    y, _ = it.year_tax(2020, [], [(pd.Timestamp("2020-04-01"), 10_000.0)], [])
    assert y.total_tax == pytest.approx(3_120)
    y, _ = it.year_tax(2020, [], [(pd.Timestamp("2020-04-01"), 10_000.0)], [],
                       it.Params(slab=0.20))
    assert y.total_tax == pytest.approx(2_080)


def test_capital_losses_do_not_touch_dividends():
    y, out = it.year_tax(2021, [piece("2021-05-01", "2021-07-01", -50_000)],
                         [(pd.Timestamp("2021-08-01"), 10_000.0)], [])
    assert y.total_tax == pytest.approx(3_120) and out[0].amount == pytest.approx(50_000)


# ---------------------------------------------------------------- NaN guards
def test_nan_gain_or_dividend_raises_instead_of_zeroing_tax():
    bad = piece("2021-05-01", "2021-07-01", 0.0)
    bad.proceeds = float("nan")
    with pytest.raises(ValueError, match="non-finite gain"):
        it.year_tax(2021, [bad, piece("2021-05-01", "2021-07-01", 100_000)], [], [])
    with pytest.raises(ValueError, match="non-finite"):
        it.year_tax(2021, [], [(pd.Timestamp("2021-08-01"), float("nan"))], [])
    b = it.LotBook()
    with pytest.raises(ValueError, match="non-finite"):
        b.buy("A", "2021-01-01", float("nan"), 10.0)
    b.buy("A", "2021-01-01", 10, 10.0)
    with pytest.raises(ValueError, match="non-finite"):
        b.sell("A", "2021-02-01", 5, float("nan"))


# ---------------------------------------------------------------- FIFO lots
def test_fifo_matching_across_lots():
    b = it.LotBook()
    b.buy("A", "2021-01-01", 100, 10.0)
    b.buy("A", "2021-06-01", 100, 20.0)
    ps = b.sell("A", "2022-03-01", 150, 30.0)
    assert [(p.buy_date, p.units, p.long_term) for p in ps] == [
        (date(2021, 1, 1), 100, True), (date(2021, 6, 1), 50, False)]
    assert [p.gain for p in ps] == [pytest.approx(2_000), pytest.approx(500)]
    assert b.units("A") == pytest.approx(50)
    with pytest.raises(ValueError):
        b.sell("A", "2022-03-02", 60, 30.0)


# ---------------------------------------------------------------- bonus shares
def test_bonus_splits_lots_into_originals_and_a_nil_cost_lot():
    # 200 adjusted units at 100 (raw: 100 shares at 200) bought 10 Jan 2022; 1:1 bonus
    # ex 1 Jun 2022 (factor 0.5): originals 100 units at 200 dated 10 Jan 2022, bonus
    # 100 units at nil dated 1 Jun 2022. A lot bought on the ex-date is not entitled.
    b = it.LotBook()
    b.buy("A", "2022-01-10", 200, 100.0)
    assert b.bonus("A", "2022-06-01", 0.5) == pytest.approx(100)
    b.buy("A", "2022-06-01", 10, 120.0)
    lots = [(l.buy_date, l.units, l.basis, l.bonus) for l in b.lots["A"]]
    assert lots == [(date(2022, 1, 10), 100, 200.0, False), (date(2022, 6, 1), 100, 0.0, True),
                    (date(2022, 6, 1), 10, 120.0, False)]
    assert b.units("A") == pytest.approx(210)       # adjusted units are unchanged
    # a second bonus splits the bonus lot too (bonus on bonus shares), still at nil cost
    b.bonus("A", "2022-09-01", 0.5)
    assert [(l.buy_date, l.units, l.basis) for l in b.lots["A"]][-3:] == [
        (date(2022, 9, 1), 50, 0.0), (date(2022, 9, 1), 50, 0.0), (date(2022, 9, 1), 5, 0.0)]


def test_bonus_sale_between_twelve_months_of_purchase_and_of_allotment():
    # Sold 1 Mar 2023 at 150: originals (bought 10 Jan 2022) are LONG term at a loss of
    # 100 x (150 - 200) = -5,000; bonus shares (allotted 1 Jun 2022) are SHORT term with a
    # gain of 100 x 150 = 15,000. Averaged units would show one LT gain of 200 x 50 = 10,000.
    b = it.LotBook(stripping=False)
    b.buy("A", "2022-01-10", 200, 100.0)
    b.bonus("A", "2022-06-01", 0.5)
    ps = b.sell("A", "2023-03-01", None, 150.0)
    assert [(p.long_term, p.gain) for p in ps] == [(True, pytest.approx(-5_000)),
                                                   (False, pytest.approx(15_000))]
    # FY2022-23: ST 15,000 x 15% x 1.04 = 2,340; the LT loss cannot touch it and is carried
    y, out = it.year_tax(2022, ps, [], [])
    assert y.total_tax == pytest.approx(2_340)
    assert [(l.kind, l.amount) for l in out] == [("LT", pytest.approx(5_000))]


def test_s94_8_bonus_stripping():
    # Bought 2 Jan 2023 (within 3 months before the 15 Feb 2023 record date), originals
    # sold 15 May 2023 (within 9 months after) at 150: loss 100 x (150 - 200) = -5,000 is
    # ignored while the 100 bonus shares are still held, and becomes their cost (50 each).
    b = it.LotBook()
    b.buy("A", "2023-01-02", 200, 100.0)
    b.bonus("A", "2023-02-15", 0.5)
    ps = b.sell("A", "2023-05-15", 100, 150.0)
    assert ps[0].gain == pytest.approx(-5_000) and ps[0].taxable_gain == pytest.approx(0)
    assert b.lots["A"][0].basis == pytest.approx(50.0)
    later = b.sell("A", "2024-03-01", None, 150.0)
    assert later[0].taxable_gain == pytest.approx(100 * (150 - 50))
    assert b.s94_log[0]["section"] == "94(8)"


@pytest.mark.parametrize("buy,sell,units,why", [
    ("2023-01-02", "2023-12-01", 100, "sold more than 9 months after the record date"),
    ("2022-10-01", "2023-05-15", 100, "bought more than 3 months before"),
    ("2023-01-02", "2023-05-15", None, "the bonus shares are sold too"),
    ("2021-12-01", "2022-03-15", 100, "before 1 Apr 2022 s.94(8) covered units only")])
def test_s94_8_does_not_apply(buy, sell, units, why):
    ex = "2022-01-15" if buy.startswith("2021") else "2023-02-15"
    b = it.LotBook()
    b.buy("A", buy, 200, 100.0)
    b.bonus("A", ex, 0.5)
    ps = b.sell("A", sell, units, 150.0)
    assert ps[0].gain == pytest.approx(-5_000) and ps[0].ignored == 0.0, why


def test_s94_7_dividend_stripping_before_april_2020():
    # Bought 3 Jun 2019, exempt dividend Rs 5 a unit with record (ex) date 15 Jul 2019,
    # sold 16 Sep 2019 at 90 (within 3 months): loss 1,000, ignored up to the 500 dividend.
    b = it.LotBook()
    b.buy("A", "2019-06-03", 100, 100.0)
    b.tag_dividend("A", "2019-07-15", 5.0)
    ps = b.sell("A", "2019-09-16", 100, 90.0)
    assert ps[0].gain == pytest.approx(-1_000) and ps[0].taxable_gain == pytest.approx(-500)
    # sold after three months: the whole loss counts
    b.buy("A", "2019-06-03", 100, 100.0)
    b.tag_dividend("A", "2019-07-15", 5.0)
    assert b.sell("A", "2019-10-16", 100, 90.0)[0].ignored == 0.0
    # bought more than three months before the record date: no tag
    b.buy("A", "2019-04-01", 100, 100.0)
    b.tag_dividend("A", "2019-07-15", 5.0)
    assert b.sell("A", "2019-09-16", 100, 90.0)[0].ignored == 0.0
    # the aggregated liquidation pieces apply it too
    b.buy("A", "2019-06-03", 100, 100.0)
    b.tag_dividend("A", "2019-07-15", 5.0)
    lp = b.liquidation_pieces("A", "2019-09-16", 90.0)
    assert len(lp) == 1 and lp[0].taxable_gain == pytest.approx(-500)


# ---------------------------------------------------------------- costs
def test_costs_at_50k_after_october_2024():
    # Buy Rs 50,000 on 15 Jan 2025: STT 50, stamp 0.015% 7.50, NSE 2.97/lakh 1.485,
    # IPFT 10/crore 0.05, SEBI 10/crore 0.05, GST 18% x 1.585 = 0.2853. Total 59.3703.
    b = it.trade_costs("buy", 50_000, "2025-01-15", it.Params(slippage=0))
    assert b["charges"] == pytest.approx(59.3703)
    assert b["dp"] == 0 and b["deductible"] == pytest.approx(9.3703)   # STT not deductible
    # Sell: STT 50, no stamp, 1.485 + 0.05 + 0.05 + 0.2853, DP 13 x 1.18 = 15.34. 67.2103
    s = it.trade_costs("sell", 50_000, "2025-01-15", it.Params(slippage=0))
    assert s["charges"] == pytest.approx(67.2103)
    assert it.trade_costs("sell", 50_000, "2025-01-15", dp_scrips=0)["dp"] == 0.0


def test_costs_in_2021_and_before_the_stamp_act_change():
    # 4 Jan 2021: NSE 3.45/lakh 1.725, IPFT 0.01/crore, SEBI 5/crore 0.025,
    # GST 18% x 1.75005 = 0.315009; buy: + STT 50 + stamp 7.5 = 59.565059
    b = it.trade_costs("buy", 50_000, "2021-01-04", it.Params(slippage=0))
    assert b["charges"] == pytest.approx(59.565059)
    # 15 Feb 2020: stamp both sides (assumed 0.01%), NSE 3.25/lakh, SEBI 10/crore
    r = it.rates_on("2020-02-15")
    assert r["stamp_buy"] == r["stamp_sell"] == 0.0001 and r["exchange"] == 3.25 / 1e5
    assert it.rates_on("2026-03-01")["exchange"] == pytest.approx(306.99 / 1e7)
    assert it.rates_on("2024-06-01")["dp"] == 13.0 and it.rates_on("2024-05-31")["dp"] == 13.5


def test_dp_note_states_only_what_its_sources_support():
    note = it.COST_SOURCES["dp"]
    assert "13.5" in note and "Rs 13 from 1 Jun 2024" in note
    assert "CDSL cut" not in note and "3.5 CDSL" not in note


def test_cost_table_against_the_flat_rate():
    t = it.cost_table(50_000, flat=0.002, p=it.Params(slippage=0.001))
    row = t[t["from"] == date(2024, 10, 1)].iloc[0]
    assert row["avg_side_pct"] == pytest.approx(50 * (59.3703 + 67.2103) / 50_000)   # 0.12658
    assert row["avg_side_with_slippage_pct"] == pytest.approx(row["avg_side_pct"] + 0.1)
    assert (t["flat_backtest_pct"] == 0.2).all()


# ---------------------------------------------------------------- synthetic engine
def mini_engine(start, end, prices, trades, divs=(), cost=0.0, capital=500_000.0):
    """The engine's cash rule on a tiny book. prices: {sym: [(from, px), ...]};
    trades: [(date, sym, side, units)] priced at that day's price; divs: [(date, sym, per_unit)]."""
    dates = pd.bdate_range(start, end)
    px = pd.DataFrame(index=dates)
    for s, steps in prices.items():
        col = pd.Series(np.nan, index=dates)
        for d0, v in steps:
            col[col.index >= pd.Timestamp(d0)] = v
        px[s] = col
    tr = pd.DataFrame(trades, columns=["date", "symbol", "side", "units"])
    tr["date"] = pd.to_datetime(tr["date"])
    tr["price"] = [px.at[d, s] for d, s in zip(tr["date"], tr["symbol"])]
    tr["value"] = tr["units"] * tr["price"]
    tr["cost"] = tr["value"] * cost
    tr["reason"] = "rebalance"
    dv = pd.DataFrame(list(divs), columns=["date", "symbol", "dps"])
    dv["date"] = pd.to_datetime(dv["date"])
    cash, units, rows, credited = capital, {}, [(dates[0], "open", capital)], []
    for d in dates:
        for r in dv[dv["date"] == d].itertuples():
            amt = units.get(r.symbol, 0.0) * r.dps
            if amt:
                cash += amt
                credited.append((d, r.symbol, amt))
        for r in tr[tr["date"] == d].itertuples():
            sgn = 1 if r.side == "buy" else -1
            units[r.symbol] = units.get(r.symbol, 0.0) + sgn * r.units
            cash += -r.value - r.cost if r.side == "buy" else r.value - r.cost
        val = cash + sum(u * px.at[d, s] for s, u in units.items())
        rows.append((d, "open" if d == dates[-1] else "close", val))
    nav = pd.DataFrame(rows, columns=["date", "mark", "strategy"])
    end_px = px.iloc[-1]
    return (tr, nav, pd.DataFrame(credited, columns=["date", "symbol", "amount"]),
            px, end_px)


@pytest.fixture
def no_costs(monkeypatch):
    zero = {k: 0.0 for k in ("stt", "stamp", "exchange", "ipft", "sebi", "brokerage", "gst",
                             "dp", "charges", "deductible", "slippage", "total")}
    monkeypatch.setattr(it, "trade_costs", lambda *a, **k: dict(zero))


def payments(r):
    return [(p.date, p.kind, p.amount) for p in r.payments.itertuples()]


def test_strategy_dividends_and_year_end_tax(no_costs):
    # 'april' timing. Buy 5,000 @100 on 4 Jan 2021; Rs 10 dividend on 1 Jun 2021 (50,000);
    # sell all @150 on 1 Feb 2022 (LT, 250,000 gain). FY2021-22 tax:
    #   LT (250,000 - 100,000) x 10% = 15,000; dividends 50,000 x 30% = 15,000;
    #   cess 4% of 30,000 = 1,200. Total 31,200, paid at the close of 1 Apr 2022
    #   from 800,000 of cash: 768,800 left, nothing more to pay at the end.
    tr, nav, dv, px, end_px = mini_engine(
        "2021-01-04", "2022-06-01", {"A": [("2021-01-04", 100.0), ("2022-02-01", 150.0)]},
        [("2021-01-04", "A", "buy", 5000.0), ("2022-02-01", "A", "sell", 5000.0)],
        divs=[("2021-06-01", "A", 10.0)])
    assert it.replay_check(tr, nav, dv, px, end_px)["worst_rel_error"] < 1e-12
    r = it.after_tax_strategy(tr, nav, dv, px, end_px, it.Params(slippage=0, tax_timing="april"))
    y = r.years.set_index("fy").loc["FY2021-22"]
    assert y["total_tax"] == pytest.approx(31_200) and y["paid_on"] == date(2022, 4, 1)
    assert y["interest_234b"] == 0 and y["interest_234c"] == 0
    assert r.final["after_tax_end_liquidated"] == pytest.approx(768_800)
    after = r.nav.set_index(["date", "mark"])["after_tax"]
    assert after[(pd.Timestamp("2022-03-31"), "close")] == pytest.approx(800_000)
    assert after[(pd.Timestamp("2022-04-01"), "close")] == pytest.approx(768_800)


def test_tax_is_raised_by_selling_fifo_and_compounds(no_costs):
    # 'april' timing. Buy 5,000 @100 (4 Jan 2021); sell 2,000 @120 (1 Jun 2021: ST gain
    # 40,000); buy 1,000 @110 (1 Sep 2021). FY2021-22 tax: 40,000 x 15% x 1.04 = 6,240.
    # 1 Apr 2022 close, price 130: book = 130,000 cash + 4,000 x 130 = 650,000;
    # phi = 6,240 / 650,000 = 0.0096: 38.4 units sold, FIFO from the Jan 2021 lot
    # (LT gain 38.4 x 30 = 1,152). End 1 Jun 2022 @130: the rest of the Jan lot is LT
    # (2,961.6 x 30 = 88,848; LT total 90,000 < 1 lakh: nil) and the Sep lot ST
    # (1,000 x 20 = 20,000 x 15% x 1.04 = 3,120). Held 643,760; liquidated 640,640.
    tr, nav, dv, px, end_px = mini_engine(
        "2021-01-04", "2022-06-01",
        {"A": [("2021-01-04", 100.0), ("2021-06-01", 120.0), ("2021-09-01", 110.0),
               ("2022-04-01", 130.0)]},
        [("2021-01-04", "A", "buy", 5000.0), ("2021-06-01", "A", "sell", 2000.0),
         ("2021-09-01", "A", "buy", 1000.0)])
    r = it.after_tax_strategy(tr, nav, dv, px, end_px, it.Params(slippage=0, tax_timing="april"))
    fund = r.pieces[r.pieces["reason"] == "tax_funding"]
    assert len(fund) == 1 and fund["units"].iloc[0] == pytest.approx(38.4)
    assert fund["buy_date"].iloc[0] == date(2021, 1, 4) and fund["long_term"].iloc[0]
    assert fund["gain"].iloc[0] == pytest.approx(1_152)
    assert r.final["after_tax_end_held"] == pytest.approx(643_760)
    assert r.final["tax_due_at_end"] == pytest.approx(3_120)
    assert r.final["after_tax_end_liquidated"] == pytest.approx(640_640)
    assert r.final["lot_book_check_worst_rel"] < 1e-9
    # the same book under advance tax: 6,240 is under Rs 10,000 at every instalment and
    # for the year, so it is paid on 1 Apr 2022 as before, with no interest
    a = it.after_tax_strategy(tr, nav, dv, px, end_px, it.Params(slippage=0))
    assert a.final["after_tax_end_liquidated"] == pytest.approx(640_640)
    assert payments(a) == [(date(2022, 4, 1), "self_assessment", pytest.approx(6_240))]


def test_capital_scales_the_book_but_not_the_exemption(no_costs):
    # Same as the dividend test at Rs 10 lakh: LT gain 500,000 - 100,000 = 400,000 x 10%
    # = 40,000; dividends 100,000 x 30% = 30,000; x1.04 = 72,800. 1,600,000 - 72,800.
    tr, nav, dv, px, end_px = mini_engine(
        "2021-01-04", "2022-06-01", {"A": [("2021-01-04", 100.0), ("2022-02-01", 150.0)]},
        [("2021-01-04", "A", "buy", 5000.0), ("2022-02-01", "A", "sell", 5000.0)],
        divs=[("2021-06-01", "A", 10.0)])
    r = it.after_tax_strategy(tr, nav, dv, px, end_px, it.Params(slippage=0, tax_timing="april"),
                              capital=1e6)
    assert r.final["pre_tax_end"] == pytest.approx(1_600_000)
    assert r.final["after_tax_end_liquidated"] == pytest.approx(1_527_200)


# ---------------------------------------------------------------- advance tax
def test_advance_tax_instalment_on_income_before_the_session(no_costs):
    # Buy 5,000 @100 on 1 Apr 2021, flat price; Rs 10 dividend on 1 Jun 2021 (50,000).
    # Tax 50,000 x 30% x 1.04 = 15,600 >= 10,000: paid at the close of 11 Jun 2021 (the
    # last session whose T+2 sale settles by Tue 15 Jun), not in April 2022. The units sold to pay it were
    # bought at 100 and sold at 100: no gain, so nothing more falls due. End: 534,400.
    tr, nav, dv, px, end_px = mini_engine(
        "2021-04-01", "2022-06-01", {"A": [("2021-04-01", 100.0)]},
        [("2021-04-01", "A", "buy", 5000.0)], divs=[("2021-06-01", "A", 10.0)])
    r = it.after_tax_strategy(tr, nav, dv, px, end_px, it.Params(slippage=0))
    assert payments(r) == [(date(2021, 6, 11), "advance_tax", pytest.approx(15_600))]
    assert (r.pieces["reason"] == "advance_tax").any()
    y = r.years.set_index("fy").loc["FY2021-22"]
    assert y["advance_paid"] == pytest.approx(15_600) and y["self_assessment"] == 0
    assert y["instalments_paid"] == 1 and y["interest_234b"] == 0 == y["interest_234c"]
    assert r.final["after_tax_end_liquidated"] == pytest.approx(534_400)
    k = r.nav.set_index(["date", "mark"])["k"]
    assert k[(pd.Timestamp("2021-06-10"), "close")] == 1.0
    assert k[(pd.Timestamp("2021-06-11"), "close")] == pytest.approx(1 - 15_600 / 550_000)


def test_income_on_the_instalment_session_falls_into_the_next_one(no_costs):
    # The dividend is credited on 11 Jun 2021, the June instalment session itself: it is
    # not income realised before that session, so it is paid at the next instalment,
    # 13 Sep 2021 (the last session whose T+2 sale settles by Wed 15 Sep).
    tr, nav, dv, px, end_px = mini_engine(
        "2021-04-01", "2022-06-01", {"A": [("2021-04-01", 100.0)]},
        [("2021-04-01", "A", "buy", 5000.0)], divs=[("2021-06-11", "A", 10.0)])
    r = it.after_tax_strategy(tr, nav, dv, px, end_px, it.Params(slippage=0))
    assert payments(r) == [(date(2021, 9, 13), "advance_tax", pytest.approx(15_600))]


def test_no_advance_tax_under_ten_thousand(no_costs):
    # Dividend 20,000: tax 6,240 < 10,000 all year: paid 1 Apr 2022, no interest (s.208).
    tr, nav, dv, px, end_px = mini_engine(
        "2021-04-01", "2022-06-01", {"A": [("2021-04-01", 100.0)]},
        [("2021-04-01", "A", "buy", 5000.0)], divs=[("2021-06-01", "A", 4.0)])
    r = it.after_tax_strategy(tr, nav, dv, px, end_px, it.Params(slippage=0))
    assert payments(r) == [(date(2022, 4, 1), "self_assessment", pytest.approx(6_240))]
    y = r.years.set_index("fy").loc["FY2021-22"]
    assert not y["liable_advance_tax"] and y["instalments_skipped"] == 4


def test_income_after_the_march_instalment_bears_234b_and_234c(no_costs):
    # Buy 4,000 @100 on 1 Apr 2021; Rs 10 dividend 1 Jun 2021 (40,000): tax 12,480, paid
    # 11 Jun 2021 from a book of 540,000, so k = 1 - 12,480 / 540,000 = 0.9768889.
    # 21 Mar 2022 (after the March instalment, paid 11 Mar) the book sells k x 2,000 units at 150:
    # ST gain k x 100,000 = 97,688.89. Year's tax (15% x 97,688.89 + 30% x 40,000) x 1.04
    # = 27,719.5 -> 27,720. Advance 12,480, shortfall 15,240. s.234C: 1% = 152.4 -> 150.
    # s.234B: 12,480 < 90% of 27,720, one month (paid 1 Apr): 152.4 -> 150.
    tr, nav, dv, px, end_px = mini_engine(
        "2021-04-01", "2022-06-01", {"A": [("2021-04-01", 100.0), ("2022-03-21", 150.0)]},
        [("2021-04-01", "A", "buy", 4000.0), ("2022-03-21", "A", "sell", 2000.0)],
        divs=[("2021-06-01", "A", 10.0)])
    r = it.after_tax_strategy(tr, nav, dv, px, end_px, it.Params(slippage=0))
    y = r.years.set_index("fy").loc["FY2021-22"]
    assert y["total_tax"] == 27_720 and y["advance_paid"] == pytest.approx(12_480)
    assert y["self_assessment"] == pytest.approx(15_240)
    assert y["interest_234c"] == 150 and y["interest_234b"] == 150
    assert payments(r) == [(date(2021, 6, 11), "advance_tax", pytest.approx(12_480)),
                           (date(2022, 4, 1), "self_assessment", pytest.approx(15_240)),
                           (date(2022, 4, 1), "interest", 300)]
    assert r.final["interest_paid"] == 300
    # the 'april' sensitivity pays the whole year then, with no interest
    a = it.after_tax_strategy(tr, nav, dv, px, end_px, it.Params(slippage=0, tax_timing="april"))
    assert a.final["interest_paid"] == 0 and len(a.payments) == 1


def test_sale_on_31_march_and_on_1_april(no_costs):
    # FY boundary through the strategy layer. 5,000 bought 1 Apr 2021 @100, sold @150:
    # on 31 Mar 2022 the ST gain 250,000 is FY2021-22 income after the last instalment:
    # tax 39,000 paid 1 Apr 2022 with 1% s.234C (390) and 1% s.234B (390).
    # On 1 Apr 2022 it is FY2022-23 income (still short term: not MORE than 12 months),
    # paid at the June 2022 instalment (13 Jun, T+2 before Wed 15 Jun) with no interest.
    for sold, fy, when, interest in (("2022-03-31", "FY2021-22", date(2022, 4, 1), 780),
                                     ("2022-04-01", "FY2022-23", date(2022, 6, 13), 0)):
        tr, nav, dv, px, end_px = mini_engine(
            "2021-04-01", "2022-08-01", {"A": [("2021-04-01", 100.0), (sold, 150.0)]},
            [("2021-04-01", "A", "buy", 5000.0), (sold, "A", "sell", 5000.0)])
        r = it.after_tax_strategy(tr, nav, dv, px, end_px, it.Params(slippage=0))
        y = r.years.set_index("fy").loc[fy]
        assert y["total_tax"] == pytest.approx(39_000) and y["st_gains"] == pytest.approx(250_000)
        assert r.final["interest_paid"] == interest
        assert payments(r)[0][:2] == (when, "advance_tax" if interest == 0 else "self_assessment")
        assert r.final["after_tax_end_liquidated"] == pytest.approx(750_000 - 39_000 - interest)


def test_overpaid_advance_tax_is_refunded_without_interest(no_costs):
    # Sell 5,000 A @120 on 1 Jun 2021 (bought @100): ST gain 100,000, tax 15,600, paid
    # 11 Jun 2021 by selling 130 of the 5,000 B units bought 2 Jun @120 (no gain), so
    # k = 0.974. B falls to 100 and the book sells 4,870 on 1 Feb 2022: ST loss 97,400.
    # Year: net ST 2,600 x 15% x 1.04 = 405.6 -> 410, under Rs 10,000: no interest.
    # Refund 15,600 - 410 = 15,190 on 1 Apr 2022, without interest. End 487,000 + 15,190.
    tr, nav, dv, px, end_px = mini_engine(
        "2021-04-01", "2022-06-01",
        {"A": [("2021-04-01", 100.0), ("2021-06-01", 120.0)],
         "B": [("2021-06-02", 120.0), ("2022-02-01", 100.0)]},
        [("2021-04-01", "A", "buy", 5000.0), ("2021-06-01", "A", "sell", 5000.0),
         ("2021-06-02", "B", "buy", 5000.0), ("2022-02-01", "B", "sell", 5000.0)])
    r = it.after_tax_strategy(tr, nav, dv, px, end_px, it.Params(slippage=0))
    assert payments(r) == [(date(2021, 6, 11), "advance_tax", pytest.approx(15_600)),
                           (date(2022, 4, 1), "refund", pytest.approx(-15_190))]
    y = r.years.set_index("fy").loc["FY2021-22"]
    assert y["total_tax"] == 410 and y["refund"] == pytest.approx(15_190)
    assert r.final["after_tax_end_liquidated"] == pytest.approx(502_190)
    assert r.final["refunds"] == pytest.approx(15_190)


def test_advance_minimum_pays_the_statutory_share(no_costs):
    # Sensitivity. The 50,000 dividend of 1 Jun 2021 (tax 15,600): 15% = 2,340 at 15 Jun,
    # 45% = 7,020 by 15 Sep (4,680 more), 75% = 11,700 by 15 Dec (4,680), 100% by 15 Mar
    # (3,900). Nothing is short at the true-up, so no interest.
    tr, nav, dv, px, end_px = mini_engine(
        "2021-04-01", "2022-06-01", {"A": [("2021-04-01", 100.0)]},
        [("2021-04-01", "A", "buy", 5000.0)], divs=[("2021-06-01", "A", 10.0)])
    r = it.after_tax_strategy(tr, nav, dv, px, end_px,
                              it.Params(slippage=0, tax_timing="advance_minimum"))
    assert payments(r) == [(date(2021, 6, 11), "advance_tax", 2_340),
                           (date(2021, 9, 13), "advance_tax", 4_680),
                           (date(2021, 12, 13), "advance_tax", 4_680),
                           (date(2022, 3, 11), "advance_tax", 3_900)]
    assert r.final["interest_paid"] == 0
    assert r.final["after_tax_end_liquidated"] == pytest.approx(534_400)


def test_instalment_is_paid_on_the_last_session_whose_sale_settles_by_the_due_date():
    # T+1 (from 27 Jan 2023). 15 Jun 2024 was a Saturday, 15 Sep and 15 Dec 2024
    # Sundays, 15 Mar 2025 a Saturday: the last session on or before each is a Friday,
    # so the sale is on the Thursday and settles on the Friday.
    acct = it.TaxAccount(pd.bdate_range("2024-04-01", "2025-04-30"), pd.Timestamp("2024-04-01"))
    ev = sorted((d.date(), kind) for d, v in acct.events.items() for kind, _ in v)
    assert ev == [(date(2024, 6, 13), "instalment"), (date(2024, 9, 12), "instalment"),
                  (date(2024, 12, 12), "instalment"), (date(2025, 3, 13), "instalment"),
                  (date(2025, 4, 1), "true_up")]
    # T+2 before 27 Jan 2023: Tue 15 Jun 2021 -> sell Fri 11 Jun; Wed 15 Dec 2021 -> Mon 13 Dec
    acct = it.TaxAccount(pd.bdate_range("2021-04-01", "2022-04-30"), pd.Timestamp("2021-04-01"))
    inst = sorted(d.date() for d, v in acct.events.items() if ("instalment", 2021) in v)
    assert inst == [date(2021, 6, 11), date(2021, 9, 13), date(2021, 12, 13), date(2022, 3, 11)]
    # no instalment before the clock starts (a clock starting 15 Feb 2019; Fri 15 Mar
    # 2019 under T+2 -> sell Wed 13 Mar)
    acct = it.TaxAccount(pd.bdate_range("2019-02-15", "2019-04-30"), pd.Timestamp("2019-02-15"))
    assert sorted(d.date() for d in acct.events) == [date(2019, 3, 13), date(2019, 4, 1)]
    assert it.TaxAccount(pd.bdate_range("2024-04-01", "2025-04-30"), pd.Timestamp("2024-04-01"),
                         it.Params(tax_timing="april")).events == {
        pd.Timestamp("2025-04-01"): [("true_up", 2024)]}


def test_234b_counts_every_month_or_part_month():
    acct = it.TaxAccount([], pd.Timestamp("2024-04-01"))
    # tax 100,000, advance 50,000 (< 90%): shortfall 50,000; paid 2 May 2025: 2 months
    assert acct._interest(2024, 100_000, 50_000, date(2025, 5, 2)) == (1_000, 500)
    # advance 95% of the tax: no s.234B, s.234C 1% of the 5,000 shortfall
    assert acct._interest(2024, 100_000, 95_000, date(2025, 4, 1)) == (0, 50)
    assert acct._interest(2024, 9_990, 0, date(2025, 4, 1)) == (0, 0)     # not liable
    # Rule 119A(b): the base is rounded down to a whole Rs 100 before the rate. Shortfall
    # 100,190 -> 100,100: s.234B three months (Apr-Jun) = 3,003 -> 3,000, s.234C 1,001 ->
    # 1,000. On the unrounded base s.234B would be 3,005.7 -> 3,010.
    assert acct._interest(2024, 100_190, 0, date(2025, 6, 15)) == (3_000, 1_000)


# ---------------------------------------------------------------- bonus through the layer
def test_bonus_lots_in_the_strategy_layer(no_costs):
    # 4,000 adjusted units bought 3 Jan 2022 @100 (raw 2,000 @200); 1:1 bonus ex 1 Jun 2022;
    # all sold 1 Mar 2023 @150: originals LT loss 2,000 x (150 - 200) = -100,000; bonus
    # shares ST gain 2,000 x 150 = 300,000. FY2022-23 tax 300,000 x 15% x 1.04 = 46,800, paid
    # at the March 2023 instalment (14 Mar: T+1 before Wed 15 Mar); the LT loss is carried.
    # Book 100,000 + 600,000 = 700,000.
    # Averaged into the parent lot (bonuses=None) it would be one LT gain of 200,000:
    # (200,000 - 100,000) x 10% x 1.04 = 10,400.
    tr, nav, dv, px, end_px = mini_engine(
        "2022-01-03", "2023-06-01", {"A": [("2022-01-03", 100.0), ("2023-03-01", 150.0)]},
        [("2022-01-03", "A", "buy", 4000.0), ("2023-03-01", "A", "sell", 4000.0)])
    bz = pd.DataFrame({"symbol": ["A", "A"], "ex_date": ["2022-06-01", "2024-01-01"],
                       "factor": [0.5, 0.5]})          # the second is after the clock
    r = it.after_tax_strategy(tr, nav, dv, px, end_px, it.Params(slippage=0), bonuses=bz)
    y = r.years.set_index("fy").loc["FY2022-23"]
    assert y["st_gains"] == pytest.approx(300_000) and y["lt_losses"] == pytest.approx(100_000)
    assert y["total_tax"] == pytest.approx(46_800)
    assert payments(r) == [(date(2023, 3, 14), "advance_tax", pytest.approx(46_800))]
    assert r.final["after_tax_end_liquidated"] == pytest.approx(653_200)
    assert r.final["losses_carried_out"] == [{"fy": 2022, "kind": "LT",
                                              "amount": pytest.approx(100_000)}]
    assert r.final["bonus_events_applied"][0]["bonus_units"] == pytest.approx(2_000)
    plain = it.after_tax_strategy(tr, nav, dv, px, end_px, it.Params(slippage=0))
    assert plain.final["after_tax_end_liquidated"] == pytest.approx(700_000 - 10_400)


def test_dividend_stripping_in_the_strategy_layer(no_costs):
    # Bought 3 Jun 2019 5,000 @100; exempt Rs 2 dividend ex 15 Jul 2019 (10,000); sold
    # 16 Sep 2019 @90: ST loss 50,000, of which 10,000 is ignored (s.94(7)): 40,000 carried.
    tr, nav, dv, px, end_px = mini_engine(
        "2019-06-03", "2020-02-14", {"A": [("2019-06-03", 100.0), ("2019-09-16", 90.0)]},
        [("2019-06-03", "A", "buy", 5000.0), ("2019-09-16", "A", "sell", 5000.0)],
        divs=[("2019-07-15", "A", 2.0)])
    r = it.after_tax_strategy(tr, nav, dv, px, end_px, it.Params(slippage=0))
    assert r.final["s94_loss_ignored"] == {"94(7)": pytest.approx(10_000)}
    assert r.final["losses_carried_out"] == [{"fy": 2019, "kind": "ST",
                                              "amount": pytest.approx(40_000)}]
    off = it.after_tax_strategy(tr, nav, dv, px, end_px, it.Params(slippage=0, stripping=False))
    assert off.final["losses_carried_out"][0]["amount"] == pytest.approx(50_000)


# ---------------------------------------------------------------- like-for-like series
def test_liquidation_value_at_every_mark(no_costs):
    # 5,000 bought 4 Jan 2021 @100, 150 from 1 Jun 2021, held to 1 Jun 2022.
    # 1 Jun 2021 close: 750,000, ST gain 250,000 if sold: 39,000 tax -> 711,000.
    # 1 Feb 2022 close: long term: (250,000 - 100,000) x 10% x 1.04 = 15,600 -> 734,400.
    # End (FY2022-23): the same 15,600 -> 734,400 = the liquidated end value.
    tr, nav, dv, px, end_px = mini_engine(
        "2021-01-04", "2022-06-01", {"A": [("2021-01-04", 100.0), ("2021-06-01", 150.0)]},
        [("2021-01-04", "A", "buy", 5000.0)])
    r = it.after_tax_strategy(tr, nav, dv, px, end_px, it.Params(slippage=0))
    n = r.nav.set_index(["date", "mark"])
    assert n.loc[(pd.Timestamp("2021-06-01"), "close"), "liquidation_value"] == pytest.approx(711_000)
    assert n.loc[(pd.Timestamp("2022-02-01"), "close"), "liquidation_value"] == pytest.approx(734_400)
    assert n["liquidation_value"].iloc[0] == 500_000
    assert r.nav["liquidation_value"].iloc[-1] == pytest.approx(r.final["after_tax_end_liquidated"])
    assert r.final["after_tax_end_liquidated"] == pytest.approx(734_400)
    assert (r.nav["held_value"] == r.nav["after_tax"]).all()       # nothing realised
    assert r.nav["liquidation_value"].notna().all()


def test_held_end_value_deducts_tax_already_owed(no_costs):
    # A 50,000 dividend on 2 May 2022 (FY2022-23, before its first instalment): the end
    # book of 550,000 owes 15,600 on it, so held = 534,400; nothing else to realise.
    tr, nav, dv, px, end_px = mini_engine(
        "2022-01-03", "2022-06-01", {"A": [("2022-01-03", 100.0)]},
        [("2022-01-03", "A", "buy", 5000.0)], divs=[("2022-05-02", "A", 10.0)])
    r = it.after_tax_strategy(tr, nav, dv, px, end_px, it.Params(slippage=0))
    assert r.final["after_tax_end_book"] == pytest.approx(550_000)
    assert r.final["after_tax_end_held"] == pytest.approx(534_400)
    assert r.final["after_tax_end_liquidated"] == pytest.approx(534_400)


# ---------------------------------------------------------------- itemised costs
def synthetic_with_costs(cost=0.002):
    return mini_engine(
        "2023-12-01", "2025-09-01",
        {"A": [("2023-12-01", 100.0), ("2024-06-03", 125.0), ("2025-02-03", 90.0)],
         "B": [("2023-12-01", 50.0), ("2024-09-02", 70.0)]},
        [("2023-12-01", "A", "buy", 2400.0), ("2023-12-01", "B", "buy", 4800.0),
         ("2024-06-03", "A", "sell", 1000.0), ("2024-09-02", "B", "sell", 4800.0),
         ("2024-09-02", "A", "buy", 1500.0), ("2025-02-03", "A", "sell", 800.0)],
        divs=[("2024-08-01", "A", 4.0), ("2025-06-02", "A", 3.0)], cost=cost)


def test_itemised_costs_flow_through_the_lot_book():
    tr, nav, dv, px, end_px = synthetic_with_costs()
    assert it.replay_check(tr, nav, dv, px, end_px)["worst_rel_error"] < 1e-12
    p = it.Params()
    r = it.after_tax_strategy(tr, nav, dv, px, end_px, p)
    first = r.costs.iloc[0]
    want = it.trade_costs(first["side"], first["value"], first["date"], p)
    assert first["c_total"] == pytest.approx(want["total"])
    # DP is charged once per scrip sold per day
    sells = r.costs[r.costs["side"] == "sell"]
    assert (sells["c_dp"] > 0).all() and (r.costs.loc[r.costs["side"] == "buy", "c_dp"] == 0).all()
    assert (r.pieces["reason"] == "cost_rebook").any()
    assert r.final["lot_book_check_worst_rel"] < 1e-6
    # every financial year from FY2023-24 to FY2025-26 is settled exactly once
    assert list(r.years["fy"]) == ["FY2023-24", "FY2024-25", "FY2025-26"]
    assert r.final["after_tax_end_liquidated"] < r.final["after_tax_end_held"]


def test_negative_cost_rebook_when_the_flat_rate_is_dearer():
    # At a flat 0.4% the engine overcharged every trade: the difference comes back as
    # buys pro rata. The after-tax book barely depends on the flat rate the engine used.
    tr4, nav4, dv4, px4, e4 = synthetic_with_costs(0.004)
    tr2, nav2, dv2, px2, e2 = synthetic_with_costs(0.002)
    r4 = it.after_tax_strategy(tr4, nav4, dv4, px4, e4)
    r2 = it.after_tax_strategy(tr2, nav2, dv2, px2, e2)
    assert (r4.costs["delta"] < 0).all()
    assert r4.final["lot_book_check_worst_rel"] < 1e-9
    assert r4.final["after_tax_end_liquidated"] == pytest.approx(
        r2.final["after_tax_end_liquidated"], rel=1e-3)


def test_reconstruct_dividends_uses_the_previous_close():
    dates = pd.bdate_range("2024-01-01", periods=6)
    tr = pd.DataFrame({"date": [dates[1], dates[4]], "symbol": ["A", "A"],
                       "side": ["buy", "sell"], "units": [100.0, 100.0]})
    du = np.zeros((6, 1))
    du[1, 0], du[3, 0], du[5, 0] = 5.0, 2.0, 7.0      # on the buy day, while held, after sale
    out = it.reconstruct_dividends(tr, dates, ["A"], du)
    assert list(out["date"]) == [dates[3]] and out["amount"].iloc[0] == pytest.approx(200)


# ---------------------------------------------------------------- replay and NaN
def test_replay_check_reconciles_trade_values_with_units_times_price():
    tr, nav, dv, px, end_px = synthetic_with_costs()
    assert it.replay_check(tr, nav, dv, px, end_px)["trade_value_worst_rel"] < 1e-12
    bad = tr.copy()
    bad.loc[0, "price"] *= 1.01                      # a wrong price column, value unchanged
    chk = it.replay_check(bad, nav, dv, px, end_px)
    assert chk["worst_rel_error"] < 1e-12            # the NAV alone would not notice
    assert chk["trade_value_worst_rel"] == pytest.approx(0.01, rel=1e-6)   # |v - 1.01 v| / v


def test_nan_prices_or_units_raise():
    tr, nav, dv, px, end_px = synthetic_with_costs()
    holey = px.copy()
    holey.loc[pd.Timestamp("2024-06-17"), "A"] = np.nan        # A held that day
    with pytest.raises(ValueError, match="price"):
        it.replay_check(tr, nav, dv, holey, end_px)
    with pytest.raises(ValueError, match="price"):
        it.after_tax_strategy(tr, nav, dv, holey, end_px)
    bad = tr.copy()
    bad.loc[2, "units"] = np.nan
    with pytest.raises(ValueError, match="non-finite units"):
        it.after_tax_strategy(bad, nav, dv, px, end_px)
    e = end_px.copy()
    e["A"] = np.nan
    with pytest.raises(ValueError, match="end price"):
        it.after_tax_strategy(tr, nav, dv, px, e)


# ---------------------------------------------------------------- index fund
def clock(rows):
    return pd.DataFrame(rows, columns=["date", "mark", "tri", "price"])


def test_index_fund_dividends_taxed_yearly():
    # 'distributed', TER 0, no stamp duty. Price flat at 100; a 2% dividend (TRI 1.00 ->
    # 1.02) in June 2021 on Rs 5 lakh: 10,000, reinvested; FY2021-22 tax 10,000 x 30% x
    # 1.04 = 3,120 (< 10,000: no advance tax), paid 1 Apr 2022 by redeeming 31.2 units at
    # 100 (zero gain). End: 510,000 - 3,120 = 506,880.
    c = clock([("2021-01-04", "open", 1.0, 100.0), ("2021-01-04", "close", 1.0, 100.0),
               ("2021-06-01", "close", 1.02, 100.0), ("2022-04-01", "close", 1.02, 100.0),
               ("2022-06-01", "open", 1.02, 100.0)])
    p = it.Params(fund_stamp=False)
    nav, years, f = it.benchmark_after_tax(c, 500_000.0, p, mode="distributed", ter=0.0)
    y = years.set_index("fy")
    assert y.loc["FY2021-22", "total_tax"] == pytest.approx(3_120)
    assert y.loc["FY2021-22", "paid_on"] == date(2022, 4, 1)
    assert f["after_tax_end_liquidated"] == pytest.approx(506_880)
    assert f["pre_tax_end"] == pytest.approx(510_000)
    # growth option: the 10,000 stays in, taxed only on the final sale as LT, under 1 lakh
    _, _, g = it.benchmark_after_tax(c, 500_000.0, p, mode="growth", ter=0.0)
    assert g["after_tax_end_liquidated"] == pytest.approx(510_000)
    # stamp duty 0.005% on unit issues after 1 Jul 2020: the initial 500,000 (Rs 25, so
    # 4,999.75 units) and the reinvested payout, now 4,999.75 x 100 x 2% = 9,999.50
    # (Rs 0.50). Tax 9,999.50 x 30% x 1.04 -> 3,120 (s.288A/B). The units redeemed carry
    # the stamp duty in their cost, so they are sold at a small loss: no tax on them.
    _, _, s = it.benchmark_after_tax(c, 500_000.0, it.Params(), mode="distributed", ter=0.0)
    assert s["stamp_duty_paid"] == pytest.approx(25 + 9_999.5 * 0.00005)
    assert s["after_tax_end_liquidated"] == pytest.approx(
        500_000 * 0.99995 + 9_999.5 * 0.99995 - 3_120)


def test_growth_is_the_default_benchmark_with_its_expense_ratio():
    # TRI flat for 365 days: the fund loses its TER, accrued daily: 500,000 x
    # (1 - 0.0016 / 365) ^ 365; the loss on the final sale is short term (not MORE than
    # 12 months), no tax. TER 0 gives the gross TRI.
    c = clock([("2021-01-04", "open", 1.0, 100.0), ("2021-01-04", "close", 1.0, 100.0),
               ("2022-01-04", "open", 1.0, 100.0)])
    nav, _, f = it.benchmark_after_tax(c, 500_000.0, it.Params(fund_stamp=False))
    assert f["mode"] == "growth" and f["ter"] == it.Params().fund_ter == 0.0016
    assert f["after_tax_end_liquidated"] == pytest.approx(500_000 * (1 - 0.0016 / 365) ** 365)
    assert nav["liquidation_value"].iloc[-1] == pytest.approx(f["after_tax_end_liquidated"])
    _, _, g = it.benchmark_after_tax(c, 500_000.0, it.Params(fund_stamp=False), ter=0.0)
    assert g["after_tax_end_liquidated"] == pytest.approx(500_000)
    # the default also pays 0.005% stamp duty on the initial units (bought after 1 Jul 2020)
    _, _, d = it.benchmark_after_tax(c, 500_000.0)
    assert d["stamp_duty_paid"] == pytest.approx(25)
    assert d["after_tax_end_liquidated"] == pytest.approx(
        500_000 * 0.99995 * (1 - 0.0016 / 365) ** 365)


def test_index_fund_final_sale_long_term():
    # TER 0, no stamp duty: 500,000 doubles, sold Jun 2025:
    # (500,000 - 125,000) x 12.5% x 1.04 = 48,750
    c = clock([("2023-01-02", "open", 1.0, 100.0), ("2023-01-02", "close", 1.0, 100.0),
               ("2025-06-02", "open", 2.0, 200.0)])
    nav, _, f = it.benchmark_after_tax(c, 500_000.0, it.Params(fund_stamp=False), ter=0.0)
    assert f["tax_due_at_end"] == pytest.approx(48_750)
    assert f["after_tax_end_liquidated"] == pytest.approx(1_000_000 - 48_750)
    # the like-for-like series: at the first close nothing is owed; at the end the tax
    assert nav["liquidation_value"].iloc[1] == pytest.approx(500_000)
    assert nav["liquidation_value"].iloc[-1] == pytest.approx(1_000_000 - 48_750)
    assert nav["held_value"].iloc[-1] == pytest.approx(1_000_000)


def test_distributed_fund_bears_s115r_before_april_2020():
    # TER 0. June 2019 payout 10,000 (TRI 1.00 -> 1.02, price flat): the fund pays
    # 11.648% s.115R tax on the grossed-up amount and distributes 8,835.20, exempt in the
    # investor's hands; redeemed at cost at the end: 508,835.20.
    c = clock([("2019-02-15", "open", 1.0, 100.0), ("2019-02-15", "close", 1.0, 100.0),
               ("2019-06-03", "close", 1.02, 100.0), ("2020-02-14", "open", 1.02, 100.0)])
    _, years, f = it.benchmark_after_tax(c, 500_000.0, mode="distributed", ter=0.0)
    assert it.S115R_RATE == pytest.approx(0.11648)
    assert f["s115r_distribution_tax"] == pytest.approx(1_164.8)
    assert f["after_tax_end_liquidated"] == pytest.approx(508_835.2)
    assert years["total_tax"].sum() == 0
    _, _, off = it.benchmark_after_tax(c, 500_000.0, it.Params(s115r=False), mode="distributed",
                                       ter=0.0)
    assert off["after_tax_end_liquidated"] == pytest.approx(510_000)
    assert f["hypothetical_before"] == "2019-09-06"


def test_bench_check_reconciles_the_benchmark_inputs():
    c = clock([("2021-01-04", "open", 1.0, 100.0), ("2021-01-04", "close", 1.01, 101.0),
               ("2022-01-04", "open", 1.05, 102.0)])
    nav = c[["date", "mark"]].assign(nifty500=c["tri"])
    assert it.bench_check(c, nav, "nifty500")["tri_vs_nav_worst_rel"] == 0.0
    nav2 = nav.assign(nifty500=nav["nifty500"] * 1.001)
    assert it.bench_check(c, nav2, "nifty500")["tri_vs_nav_worst_rel"] == pytest.approx(
        0.001 / 1.001)
    with pytest.raises(ValueError, match="non-finite"):
        it.bench_check(c.assign(price=[100.0, np.nan, 102.0]), nav, "nifty500")
    with pytest.raises(ValueError, match="clock"):
        it.bench_check(c.iloc[:2], nav, "nifty500")


# ---------------------------------------------------------------- disclosures
def test_short_term_sales_near_the_anniversary_are_reported():
    # Bought 15 Jun 2021, sold 14 Jun 2022: long term from 16 Jun 2022, 2 sessions later.
    # First order: 10,000 x 15% x 1.04 = 1,560 paid; 10,000 x 10% x 1.04 = 1,040 if waited.
    ps = pd.DataFrame([
        {"buy_date": date(2021, 6, 15), "sell_date": date(2022, 6, 14), "long_term": False,
         "taxable_gain": 10_000.0, "reason": "rebalance"},
        {"buy_date": date(2021, 6, 15), "sell_date": date(2022, 6, 1), "long_term": False,
         "taxable_gain": 10_000.0, "reason": "rebalance"},        # 11 sessions away
        {"buy_date": date(2021, 6, 15), "sell_date": date(2022, 6, 14), "long_term": False,
         "taxable_gain": -5_000.0, "reason": "rebalance"}])       # a loss: not tax paid
    out = it.near_long_term(ps, pd.bdate_range("2022-05-02", "2022-07-29"))
    assert out["pieces"] == 1 and out["by_sessions"] == {2: 1}
    assert out["st_tax_first_order"] == pytest.approx(1_560)
    assert out["lt_tax_first_order"] == pytest.approx(1_040)


def test_corporate_action_exposure_counts_the_trades_held_through():
    ev = pd.DataFrame({"symbol": ["A", "Z"], "ex_date": pd.to_datetime(["2022-06-01", "2022-06-01"]),
                       "kind": ["demerger", "amalgamation"]})
    rows = [("2022-01-03", "2022-07-01", "rebalance", 500.0),     # held through: counted
            ("2022-01-03", "2022-07-01", "rebalance", 20.0),      # same sale, second lot
            ("2022-06-02", "2022-07-01", "rebalance", 10.0),      # bought after
            ("2022-01-03", "2022-05-02", "rebalance", 10.0),      # sold before
            ("2022-01-03", "2022-06-15", "cost_rebook", 1.0),
            ("2022-01-03", "2022-09-01", "final_liquidation", 50.0)]
    ps = pd.DataFrame([{"symbol": "A", "buy_date": b, "sell_date": s, "reason": r, "gain": g}
                       for b, s, r, g in rows])
    out = it.corporate_action_exposure(ev, ps)
    assert out["events_in_window"] == 2 and out["events_held_through"] == 1
    assert out["engine_sales_affected"] == 1 and out["internal_sale_pieces_affected"] == 1
    assert out["final_liquidation_positions_affected"] == 1
    assert out["by_kind"] == {"demerger": 1}


def test_corp_events_reads_bonuses_like_split_factors():
    import duckdb
    from jobs import after_tax as job
    con = duckdb.connect()
    con.execute("CREATE TABLE corpactions (symbol VARCHAR, ex_date DATE, action VARCHAR, "
                "subject VARCHAR, factor DOUBLE)")
    rows = [("A", "2022-06-01", "bonus", "Bonus 1:1", 0.5),
            ("A", "2022-06-01", "bonus", "Bonus 1:1", 0.5),           # re-announced
            ("A", "2022-06-01", "split", "Face Value Split", 0.2),    # a split: not a bonus
            ("B", "2022-07-01", "bonus", "Bonus- 1:2", None),         # lenient parse: 2/3
            ("C", "2022-08-01", "bonus", "Scheme Of Arrangement - Bonus Debentures 1:1", None),
            ("D", "2022-09-01", "other", "Demerger", None),
            ("E", "2022-09-02", "other", "Scheme Of Amalgamation", None),
            ("F", "2022-09-03", "other", "Annual General Meeting", None),
            ("A", "2018-01-01", "bonus", "Bonus 1:1", 0.5),           # before the clock
            ("Q", "2022-06-01", "bonus", "Bonus 1:1", 0.5)]           # not traded
    con.executemany("INSERT INTO corpactions VALUES (?, ?, ?, ?, ?)", rows)
    b, u = job.corp_events(con, list("ABCDEF"), "2022-01-03", "2022-12-30")
    assert [(r.symbol, r.ex_date.date(), round(r.factor, 6)) for r in b.itertuples()] == [
        ("A", date(2022, 6, 1), 0.5), ("B", date(2022, 7, 1), round(2 / 3, 6))]
    assert [(r.symbol, r.kind) for r in u.itertuples()] == [
        ("C", "bonus_non_equity"), ("D", "demerger"), ("E", "amalgamation")]


# ---------------------------------------------------------------- the job
def _write_folder(folder, with_derived=True, derived_to=None):
    tr, nav, dv, px, end_px = synthetic_with_costs()
    tri = np.linspace(1.0, 1.3, len(nav))
    nav["nifty500"] = tri
    folder.mkdir(parents=True, exist_ok=True)
    tr.assign(date=tr["date"].dt.date).to_csv(folder / "trades.csv", index=False)
    nav.to_csv(folder / "nav.csv", index=False)
    d = derived_to or folder
    if with_derived:
        d.mkdir(parents=True, exist_ok=True)
        dv.to_csv(d / "dividends.csv", index=False)
        px.to_csv(d / "close_px.csv")
        end_px.rename("price").rename_axis("symbol").reset_index().to_csv(
            d / "end_px.csv", index=False)
        pd.DataFrame({"date": nav["date"], "mark": nav["mark"], "tri": tri,
                      "price": tri * 0.99}).to_csv(d / "bench.csv", index=False)
        pd.DataFrame({"symbol": ["A"], "ex_date": ["2024-12-02"], "factor": [0.5]}).to_csv(
            d / "bonuses.csv", index=False)
        pd.DataFrame({"symbol": ["B"], "ex_date": ["2024-03-01"], "kind": ["demerger"],
                      "subject": ["Demerger"]}).to_csv(d / "corp_unmodelled.csv", index=False)
    return tr, nav


def test_job_on_a_synthetic_folder(tmp_path):
    from jobs import after_tax as job
    _write_folder(tmp_path)
    out = tmp_path / "out"
    assert job.main(["--in", str(tmp_path), "--out", str(out)]) == 0
    for f in ("after_tax_nav.csv", "tax_by_year.csv", "tax_payments.csv", "lots_realised.csv",
              "costs_by_trade.csv", "cost_per_side.csv", "bench_after_tax_nav.csv",
              "bench_tax_by_year.csv", "bench_gross_tri_after_tax_nav.csv",
              "bench_distributed_after_tax_nav.csv", "slippage_sensitivity.csv", "summary.json"):
        assert (out / f).exists(), f
    s = json.loads((out / "summary.json").read_text())
    assert s["replay_check"]["worst_rel_error"] < 1e-9
    assert s["replay_check"]["trade_value_worst_rel"] < 1e-9
    assert s["replay_check"]["bench"]["tri_vs_nav_worst_rel"] == 0.0
    assert set(s["input_provenance"].values()) == {"folder"}
    assert "stt" in s["cost_sources"] and "exemption_fy2425" in s["tax_sources"]
    # every input file is fingerprinted; the database is recorded as unused
    for k in ("trades", "nav", "dividends", "close_px", "end_px", "bench", "bonuses"):
        f = s["input_fingerprint"][k]
        assert f["sha256"] == hashlib.sha256(open(f["path"], "rb").read()).hexdigest()
    assert s["database"]["used_for_rebuild"] is False
    # the headline benchmark is the growth fund, in the unsuffixed files
    assert s["benchmark"]["headline"] == "growth" and s["benchmark"]["growth"]["mode"] == "growth"
    bn = pd.read_csv(out / "bench_after_tax_nav.csv")
    assert bn["liquidation_value"].iloc[-1] == pytest.approx(
        s["benchmark"]["growth"]["after_tax_end_liquidated"])
    assert s["benchmark"]["notes"]["hypothetical_before"] == "2019-09-06"
    assert "6 Sep 2019" in s["benchmark"]["notes"]["first_nifty500_index_fund"]
    # statistics come from liquidation values
    nv = pd.read_csv(out / "after_tax_nav.csv")
    assert s["after_tax_stats"]["strategy"]["final_multiple"] == pytest.approx(
        nv["liquidation_value"].iloc[-1] / nv["liquidation_value"].iloc[0])
    assert s["after_tax_stats"]["basis"].startswith("liquidation_value")
    assert [x["tax_timing"] for x in s["sensitivities"]["tax_timing"]] == [
        "advance_minimum", "april"]
    assert "corporate_actions_not_modelled" in s and "limitation" in s[
        "corporate_actions_not_modelled"]
    assert s["corporate_actions_not_modelled"]["events_held_through"] == 1     # B, Mar 2024


def test_job_reads_rebuilt_inputs_from_the_output_folder(tmp_path, monkeypatch):
    from jobs import after_tax as job
    out = tmp_path / "out"
    _write_folder(tmp_path / "in", derived_to=out)

    def no_db(*a, **k):
        raise AssertionError("the database must not be touched")
    monkeypatch.setattr(job, "rebuild_inputs", no_db)
    x, prov, files = job.load_inputs(tmp_path / "in", "", "strategy", "nifty500", out=out)
    assert set(prov.values()) == {"output folder"}
    assert files["bonuses"] == out / "bonuses.csv"
    assert job.main(["--in", str(tmp_path / "in"), "--out", str(out)]) == 0


def test_job_refuses_inputs_that_do_not_reproduce_the_nav(tmp_path):
    from jobs import after_tax as job
    tr, nav, dv, px, end_px = synthetic_with_costs()
    tr.to_csv(tmp_path / "trades.csv", index=False)
    nav.to_csv(tmp_path / "nav.csv", index=False)
    dv.iloc[:0].to_csv(tmp_path / "dividends.csv", index=False)        # dividends missing
    px.to_csv(tmp_path / "close_px.csv")
    end_px.rename("price").rename_axis("symbol").reset_index().to_csv(
        tmp_path / "end_px.csv", index=False)
    pd.DataFrame(columns=["symbol", "ex_date", "factor"]).to_csv(tmp_path / "bonuses.csv",
                                                                 index=False)
    pd.DataFrame(columns=["symbol", "ex_date", "kind", "subject"]).to_csv(
        tmp_path / "corp_unmodelled.csv", index=False)
    with pytest.raises(RuntimeError, match="reproduce"):
        job.run(tmp_path, tmp_path / "out", bench_col="none")


def test_job_refuses_a_wrong_price_column_or_benchmark(tmp_path):
    from jobs import after_tax as job
    tr, _ = _write_folder(tmp_path)
    bad = tr.assign(date=tr["date"].dt.date)
    bad.loc[0, "price"] *= 1.01
    bad.to_csv(tmp_path / "trades.csv", index=False)
    with pytest.raises(RuntimeError, match="reproduce"):
        job.run(tmp_path, tmp_path / "out")
    _write_folder(tmp_path)                            # restore, then break the benchmark
    b = pd.read_csv(tmp_path / "bench.csv")
    b["tri"] *= 1.01
    b.to_csv(tmp_path / "bench.csv", index=False)
    with pytest.raises(RuntimeError, match="benchmark TRI"):
        job.run(tmp_path, tmp_path / "out")


def test_advance_tax_convention_text_follows_the_timing():
    from jobs import after_tax as job
    assert set(job.ADVANCE_TAX_LABELS) == {"advance", "advance_minimum", "april"}
    assert "15/45/75/100%" in job.ADVANCE_TAX_LABELS["advance_minimum"]
    assert "no advance tax" in job.ADVANCE_TAX_LABELS["april"]


def test_rebuilt_inputs_keep_their_database_record_when_reused(tmp_path, monkeypatch):
    """The first run rebuilds the derived inputs (here a stand-in for the database)
    and records which database built them in inputs_provenance.json; a second run
    that reuses them from the output folder carries that record into summary.json."""
    import json
    from jobs import after_tax as job
    src, out = tmp_path / "src", tmp_path / "out"
    _write_folder(tmp_path / "in", derived_to=src)
    rebuilt = {k: job._read(k, src / f"{k}.csv") for k in job.DERIVED}
    monkeypatch.setattr(job, "rebuild_inputs", lambda *a, **k: dict(rebuilt))
    assert job.main(["--in", str(tmp_path / "in"), "--out", str(out)]) == 0
    side = json.loads((out / "inputs_provenance.json").read_text(encoding="utf-8"))
    assert side["database"]["used_for_rebuild"] is True
    assert set(side["files"]) == set(job.DERIVED)

    def no_db(*a, **k):
        raise AssertionError("the database must not be touched")
    monkeypatch.setattr(job, "rebuild_inputs", no_db)
    assert job.main(["--in", str(tmp_path / "in"), "--out", str(out)]) == 0
    summ = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert {summ["input_provenance"][k] for k in job.DERIVED} == {"output folder"}
    assert summ["rebuilt_inputs_provenance"]["database"] == side["database"]
    assert summ["rebuilt_inputs_provenance"]["hashes_match_this_run"] is True
