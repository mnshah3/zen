"""Indian delivery-trade costs and capital gains tax, after the fact (v2 spec, A6).

A layer over a finished backtest. It never re-runs the engine: it reads the
trade log, the NAV, the dividends credited and the bonus issues, and works out
what the same portfolio would have left its owner after itemised costs and tax.

How the after-tax portfolio is modelled
---------------------------------------
The engine is scale-free: every rebalance sizes positions from NAV, units are
fractional and costs are proportional. A portfolio that starts with less money
(or loses some to tax) therefore holds exactly k times the pre-tax book, and
trades k times each pre-tax trade. The layer keeps that multiplier k:

  after-tax book(t) = k(t) x pre-tax NAV(t)

k starts at capital / starting NAV (1 by default) and moves only when money
leaves or enters the after-tax book but not the pre-tax one:

  cost difference   each trade costs the after-tax book its itemised charges
                    plus slippage instead of the flat rate the engine charged.
                    A trade day's total difference is raised (or returned) at
                    that day's close by selling (or buying) the same fraction
                    of every holding. Those slices go through the lot book, so
                    a sale is taxed like any other (reason 'cost_rebook'); the
                    slices carry no further costs of their own (second order,
                    their total is reported as `cost_rebook_abs`). The per-scrip
                    depository charge is a fixed rupee amount, so this is the
                    one place the after-tax book is not a pure rescaling; it is
                    itemised per trade at the after-tax scale.
  tax payment       advance tax (Params.tax_timing = 'advance', the default).
                    At the close of the last session whose sale settles by
                    15 Jun, 15 Sep, 15 Dec and 15 Mar (T+2 before 27 Jan 2023,
                    when SEBI's T+1 rollout finished, T+1 after; so the money
                    is in hand by the due date even when the 15th is a
                    holiday) the owner pays the tax on the
                    year's income realised BEFORE that session, less what is
                    already paid for the year, whenever that year-to-date tax
                    is Rs 10,000 or more (s.208; the test uses only income
                    already realised, so no later information is used). Paying
                    100% of the year-to-date tax at each instalment is more
                    than the 15/45/75% minimum and meets the s.234C proviso for
                    capital gains and dividends, which excuses a shortfall
                    caused by income that arose after an instalment date.
                    At the close of the first session of the next financial
                    year the year is trued up: the year's tax less the advance
                    tax paid is paid (self-assessment), with interest when the
                    year's tax is Rs 10,000 or more: s.234C 1% on the shortfall
                    at the 15 March instalment (income realised on or after
                    that session), and s.234B 1% a month or part month from
                    1 April on the same shortfall when the advance tax paid is
                    below 90% of the year's tax. An overpayment (a loss later
                    in the year) is refunded at the same session, without the
                    s.244A interest a real refund carries but months earlier
                    than a real refund arrives (after the return is
                    processed). Not conservative either way: the early credit
                    can favour the strategy (the independent check put it at
                    about Rs 3,000 on v1). Stated, not modelled.
                    Not charged, and stated: s.234C on the Jun/Sep/Dec
                    instalments of a year whose year-to-date tax was still
                    under Rs 10,000 at them but whose final tax is Rs 10,000 or
                    more (counted in the year rows as `instalments_skipped`),
                    and any reading under which income realised after the
                    15 March instalment also bears s.234C at the earlier
                    instalments (not resolved here).
                    The cash comes pro rata from cash and every holding, and
                    the units sold are matched FIFO and taxed in the year they
                    are sold, with their own costs. A sale made to pay an
                    instalment is realised on that session, so it falls into
                    the NEXT instalment (or the true-up), never into the one it
                    pays: no circularity.
                    'advance_minimum' (a labelled sensitivity): the same, but
                    each instalment pays only the cumulative statutory share
                    (15/45/75/100%) of the year-to-date tax, the least that
                    meets the proviso; the headline's 100% is conservative.
                    'april' (a labelled sensitivity, the earlier convention):
                    each year's whole tax at the first session of the next
                    year, no interest.

Lots are kept in after-tax units, FIFO per symbol (units in the demat account
are matched first-in-first-out, CBDT Circular 768 of 24 June 1998, stated,
not re-checked here). Units and prices are the engine's corporate-action
adjusted units, so a lot's cost basis per unit is in the same adjusted units.
A split is a pure rescaling in adjusted units: cost and date are inherited.

Bonus shares are not a split. On the bonus ex-date (a stated proxy for the
allotment date, which comes a few days later; the proxy starts the holding
clock slightly early, which is slightly taxpayer-favourable) every open lot
bought before it is split into the original shares (units x f, the full cost,
the original date) and a bonus lot (units x (1 - f), nil cost, dated the
ex-date), placed after every lot dated on or before the ex-date (FIFO), where
f is the bonus price factor (0.5 for 1:1). Section 55(2)(aa)(iiia): the cost
of bonus shares is nil; section 2(42A) Explanation 1(i)(f): their holding
period runs from allotment. With the ex-date also standing in for the record
date:
  s.94(8)  bonus stripping, sales from 1 April 2022 (Finance Act 2022 extended
           it from units to securities, AY 2023-24): shares bought within
           3 months before the record date and sold within 9 months after it
           at a loss, while some of the bonus shares they gave rise to are
           still held after the sale: the loss is ignored and added to the
           cost of those remaining bonus shares.
  s.94(7)  dividend stripping, for the exempt dividends before 1 April 2020:
           shares bought within 3 months before the record date and sold
           within 3 months after it: the loss is ignored up to the exempt
           dividend received on them.
(Params.stripping = False turns both off, for attribution.)

Costs
-----
`rates_on(d)` gives the charges in force on date d for a retail delivery
trade on NSE at a discount broker (Zerodha's published schedule, used as the
example). Every rate and its dates are in `COST_SOURCES`. Brokerage is zero
(an assumption: Zerodha charges none on delivery; a full-service broker would).
Slippage is a separate, stated assumption, default 0.10% per side.

Securities transaction tax is not deductible in computing capital gains
(proviso to section 48, inserted by the Finance (No. 2) Act, 2004); every
other charge is added to the cost of a buy or taken off the proceeds of a sale.
Slippage is simply a worse price.

Tax
---
Listed equity sold on the exchange, STT paid both ways:
  long term     held MORE than 12 months (section 2(42A): a listed share
                held "not more than twelve months" is short-term). The test
                is sale date > buy date + 12 calendar months, on trade dates.
  rates         sold before 23 July 2024: short 15% (s.111A), long 10% above
                Rs 1 lakh (s.112A). Sold on or after 23 July 2024: short 20%,
                long 12.5% above Rs 1.25 lakh (Finance (No. 2) Act, 2024).
  exemption     per financial year: Rs 1 lakh up to FY 2023-24 and Rs 1.25
                lakh from FY 2024-25, for the WHOLE of FY 2024-25 including
                sales before 23 July 2024 (CBDT FAQ, PIB 24 July 2024). It is
                applied to the highest-rate long-term gains first (a stated
                assumption: the one most favourable to the taxpayer).
  set-off       (1) this year's long-term losses against long-term gains;
                (2) this year's short-term losses against short-term gains,
                then against long-term gains; (3) brought-forward losses,
                oldest first: long-term against long-term, then short-term
                against short-term and then long-term. Within each step the
                highest-rate gains absorb losses first. Set-off comes before
                the exemption (the exemption is on gains "exceeding" the limit,
                computed after set-off). Unused losses carry forward eight
                financial years (section 74), assuming returns are filed on
                time.
  dividends     exempt in the shareholder's hands until 31 March 2020
                (section 10(34), dividend distribution tax paid by the
                company); taxable at the slab rate from 1 April 2020
                (Finance Act, 2020). Slab default 30%.
  cess          Health and Education Cess 4% on all income tax, capital
                gains included. Surcharge is ignored (it starts at Rs 50 lakh
                of income); so are the rebate under section 87A and any unused
                basic exemption (the owner is assumed to be in the 30% slab).
  rounding      total income to the nearest Rs 10 (s.288A) and the tax, each
                interest amount and each refund to the nearest Rs 10 (s.288B):
                paise ignored, a last digit of 5 or more rounds up. The
                owner's other income is not modelled, so the rounding is
                applied to this portfolio's income and tax alone; the income
                rounding lands on the income taxed at the normal (slab) rate,
                as in a return. At most a few rupees a year.
  Grandfathering (31 January 2018) is irrelevant: nothing here was held then.
  From 1 April 2026 the Income-tax Act, 2025 replaces the 1961 Act; the
  Finance Bill 2026 memorandum carries the same capital gains rates under
  the new sections 196-198 and the same 4% cess.

Like-for-like series
--------------------
Both NAV outputs carry, at every mark, `liquidation_value`: the book's value
minus the exit costs and the tax that would be due if everything were sold at
that mark (that year's realised pieces and dividends, its set-off, exemption
and brought-forward losses, less the advance tax already paid for the year),
and `held_value`: the book minus the tax already owed on income realised so
far. The strategy pays most of its tax as it goes and a growth fund defers
almost all of it, so only liquidation values compare like with like: every
after-tax statistic is computed from them.

The buy-and-hold index fund benchmark (`benchmark_after_tax`, default
'growth'): a growth-option Nifty 500 index fund held throughout and sold at
the end, as A6 asks, with its expense ratio accrued daily (Params.fund_ter,
the Direct-plan TER in FUND_SOURCES; 0 gives the gross TRI) and 0.005% stamp
duty on unit issues from 1 July 2020. 'distributed' is a labelled
sensitivity close to holding the index directly: the index's dividends are
paid out at every mark and reinvested; before 1 April 2020 each payout is a
fund distribution net of s.115R distribution tax (11.648% of the grossed-up
amount), exempt in the investor's hands; from 1 April 2020 it is taxed at the
slab rate plus cess, with the same advance-tax schedule as the strategy.
No Nifty 500 index fund is known to have been open to investors at the
clock's start: the Motilal Oswal Nifty 500 fund began on 6 September 2019 (an
earlier Goldman Sachs CNX 500 fund had closed; FUND_SOURCES), so before that
date the benchmark is a hypothetical fund. Redemption STT and exit
loads are ignored (stated), and so is s.94 for the fund's units.

Known simplifications, all stated: GST is charged on the SEBI fee throughout
(Zerodha's 2019 page shows GST on brokerage and transaction charges only; the
difference is Rs 1.8 per crore). Dividends before 1 April 2020 are exempt in
full (s.115BBDA's 10% on dividends over Rs 10 lakh a year is ignored at this
scale). TDS on dividends is ignored: it is credited against the same year's
tax, and only the date of payment would change. Demergers, amalgamations and
bonus debentures or preference shares are NOT reflected in the lot book (the
engine shows a demerger's price drop as a loss and force-sells an absorbed
company); the job counts the trades affected.
"""

from __future__ import annotations

import calendar
import math
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import date, datetime

import numpy as np
import pandas as pd

# ------------------------------------------------------------------ sources
COST_SOURCES = {
    "stt": "0.1% on buy and on sell for equity delivery. Zerodha charges page archived "
           "17 Nov 2019 (web.archive.org/web/20191117030325/https://zerodha.com/charges) "
           "and live page https://zerodha.com/charges (read 24 Sep 2026); unchanged by "
           "Budget 2024 (F&O only, Zerodha Z-Connect 'Revision in exchange transaction "
           "charges and STT from October 1, 2024') and Budget 2026 (F&O only).",
    "stamp": "From 1 Jul 2020: 0.015% on the buy side only, Indian Stamp Act as amended "
             "by the Finance Act 2019 (PwC news alert 3 Jul 2020; Zerodha charges page). "
             "Before 1 Jul 2020 stamp duty was set by the client's state of residence and "
             "charged on both sides: Zerodha's archived calculator (Nov 2019) lists 0.002% "
             "to 0.018% by state. ASSUMED 0.01% on both sides before 1 Jul 2020.",
    "exchange": "NSE cash market transaction charge, the top slab retail brokers passed on "
                "(Rs per lakh, each side): 3.25 to 31 Dec 2020 (NSE/FA/46730, 18 Dec 2020, "
                "'existing'; Zerodha archive Nov 2019 and Aug 2020 show 0.00325%); 3.45 from "
                "1 Jan 2021 (NSE/FA/46730); 3.25 from 1 Apr 2023 (NSE/FA/56129, 24 Mar 2023); "
                "3.22 from 1 Apr 2024 (NSE/FA/61137, 14 Mar 2024); 2.97 flat from 1 Oct 2024 "
                "(NSE/FA/64232, 27 Sep 2024); 3.0699 from 1 Mar 2026 (NSE/FA/73061, 27 Feb 2026). "
                "Zerodha's live page (read 24 Sep 2026) shows NSE 0.00307%, i.e. 306.99 + "
                "0.01 IPFT per crore. The rate before Feb 2019 was not checked.",
    "ipft": "NSE Investor Protection Fund Trust contribution, passed through by Zerodha: "
            "Rs 0.01 per crore before 1 Apr 2023, Rs 10 per crore from 1 Apr 2023 "
            "(NSE/FA/56129), Rs 0.01 per crore from 1 Mar 2026 (NSE/FA/73061).",
    "sebi": "SEBI turnover fee Rs 10 per crore (Zerodha archive Nov 2019 and May 2023; SEBI "
            "board memo Jul 2020); halved to Rs 5 per crore for June 2020 to March 2021 "
            "(SEBI press release 24/2020, 27 Apr 2020; Zerodha archive Aug 2020 shows Rs 5).",
    "gst": "18% on brokerage, exchange transaction charges, IPFT and the SEBI fee, and on "
           "DP charges (Zerodha charges page; GST in force since 1 Jul 2017).",
    "dp": "Depository charge per scrip per day on which it is sold, before GST: Rs 13.5 "
          "(Zerodha archive Nov 2019, Aug 2020, May 2023); Rs 13 from 1 Jun 2024 (Zerodha "
          "on tradingqna.com, 13 May 2024; the live charges page shows Rs 15.34 with GST, "
          "i.e. Rs 13 + 18%). Only these totals and dates are taken from the sources; how "
          "the charge divides between the depository and the broker is not.",
    "brokerage": "Zero on equity delivery at Zerodha (charges page, 2019 and 2026). ASSUMPTION: "
                 "the owner uses a zero-delivery-brokerage discount broker.",
    "slippage": "ASSUMPTION, not sourced: 0.10% of traded value per side for Rs 50,000 to "
                "Rs 5 lakh orders in liquid mid and small caps (half the quoted spread plus "
                "impact). Varied in the job's sensitivity table.",
}

TAX_SOURCES = {
    "rates_2024": "Memorandum explaining the Finance (No. 2) Bill, 2024: s.111A 15% -> 20%, "
                  "s.112A 10% -> 12.5%, exemption 'upto 1.25 lakh (aggregate)', effective "
                  "23 July 2024; holding period 12 months for all listed securities.",
    "exemption_fy2425": "CBDT FAQs, PIB press release PRID 2036604 (24 Jul 2024): 'This "
                        "increased exemption limit will apply for FY 2024-25 and subsequent "
                        "years.'",
    "stt_not_deductible": "Section 48 proviso (Finance (No. 2) Act, 2004): no deduction for "
                          "securities transaction tax in computing capital gains.",
    "set_off": "Sections 70 and 74 (incometaxindia.gov.in, 'Set off and carry forward of "
               "losses'): short-term capital loss against short- or long-term gains, "
               "long-term loss against long-term gains only, carried forward 8 years.",
    "dividends": "Finance Act, 2020: dividend distribution tax abolished and dividends "
                 "taxable in the shareholder's hands from 1 April 2020 (s.10(34) no longer "
                 "applies), KPMG 'Taxation of dividend' (Oct 2020).",
    "cess": "Health and Education Cess 4% on income tax including surcharge (Finance Act "
            "2018 onwards; Finance Bill 2026 memorandum repeats it for 2026-27).",
    "advance_tax": "Sections 208 (advance tax due when the year's tax is Rs 10,000 or more), "
                   "211 (individuals: 15% by 15 Jun, 45% by 15 Sep, 75% by 15 Dec, 100% by "
                   "15 Mar), 234C and its proviso (a shortfall caused by capital gains or "
                   "dividends is excused if the tax on them is paid in the remaining "
                   "instalments or, when none remain, by 31 March) and 234B (1% a month or "
                   "part month from 1 April when advance tax paid is below 90% of the assessed "
                   "tax), as read from Indian Kanoon and taxguru extracts by the A6 law "
                   "checker (A6 findings, 25 Sep 2026); not re-read here.",
    "bonus": "Section 55(2)(aa)(iiia): cost of bonus shares nil; section 2(42A) Explanation "
             "1(i)(f): holding period from the date of allotment (A6 law checker, as above).",
    "stripping": "Section 94(8) (bonus stripping; extended from units to securities by the "
                 "Finance Act 2022 from AY 2023-24) and section 94(7) (dividend stripping, "
                 "exempt dividends), as read by the A6 law and model checkers (A6 findings, "
                 "25 Sep 2026); not re-read here.",
    "rounding": "Sections 288A (total income) and 288B (tax, interest, refunds): nearest "
                "multiple of Rs 10.",
    "s115R": "From 1 Apr 2018 to 31 Mar 2020 an equity-oriented fund's distributions bore "
             "additional income tax under s.115R(2) at 10%, '11.648 percent including "
             "surcharge and cess' (cleartax.in/s/dividend-distribution-tax, read 25 Sep "
             "2026), payable 'on gross basis' (R. Sarda, 'Finance Bill 2018 - Deemed "
             "Dividend and Distribution Tax', itatonline.org, 6 Mar 2018, read 25 Sep 2026). "
             "Distributions were exempt in the unit-holder's hands until 31 Mar 2020.",
}

FUND_SOURCES = {
    "ter": "Motilal Oswal Nifty 500 Index Fund, Direct plan: total expense ratio 0.16%, "
           "data as on 31 Aug 2026 (AMC scheme page "
           "https://www.motilaloswalmf.com/mutual-funds/motilal-oswal-nifty-500-index-fund, "
           "read 25 Sep 2026). Today's TER is applied to the whole period; the scheme's "
           "earlier TERs were not checked. Accrued daily over calendar days.",
    "stamp": "Stamp duty of 0.005% on mutual fund unit issues since July 2020, 'applicable "
             "on purchase, SIP instalments, switch-ins, and IDCW reinvestments', 'not "
             "applicable on redemption' (Bajaj Finserv AMC, 'Stamp Duty on Mutual Funds', "
             "published 29 May 2025, https://www.bajajamc.com/knowledge-centre/"
             "stamp-duty-on-mutual-funds, read 25 Sep 2026). Applied from 1 Jul 2020.",
    "first_fund": "Motilal Oswal Nifty 500 Index Fund (launched as Motilal Oswal Nifty 500 "
                  "Fund): date of inception 6 Sep 2019, NFO from 18 Aug 2019 (the AMC page "
                  "above, read 25 Sep 2026). That it was the first Nifty 500 index fund is "
                  "the A6 law checker's statement (A6 findings, 25 Sep 2026), not confirmed "
                  "independently here. Before 6 Sep 2019 the benchmark is a hypothetical fund.",
}
FIRST_NIFTY500_FUND = date(2019, 9, 6)

CROR = 1e7
LAKH = 1e5
RATE_CHANGE = date(2024, 7, 23)
DIVIDENDS_TAXABLE_FROM = date(2020, 4, 1)
STAMP_UNIFORM_FROM = date(2020, 7, 1)
CARRY_YEARS = 8
EARLIEST = date(1900, 1, 1)
ADVANCE_TAX_MIN = 10_000.0                           # s.208
T1_SETTLEMENT_FROM = pd.Timestamp("2023-01-27")      # SEBI T+1 rollout complete; T+2 before
INSTALMENT_DATES = ((6, 15), (9, 15), (12, 15), (3, 15))   # s.211, individuals
INSTALMENT_SHARES = (0.15, 0.45, 0.75, 1.00)                # cumulative, s.211
INTEREST_A_MONTH = 0.01                              # s.234B, s.234C
S234B_PAID_SHARE = 0.90
S94_8_SHARES_FROM = date(2022, 4, 1)                 # sales from AY 2023-24
S115R_FROM = date(2018, 4, 1)
S115R_RATE = 0.10 * 1.12 * 1.04                      # 11.648% of the grossed-up amount
FUND_STAMP = 0.00005
FUND_STAMP_FROM = date(2020, 7, 1)
INTERNAL_REASONS = ("cost_rebook", "tax_funding", "advance_tax", "self_assessment",
                    "final_liquidation", "liquidation", "final_sale")


def _step(schedule, d):
    """Value of a step schedule [(from_date, value), ...] (ascending) on date d."""
    val = None
    for start, v in schedule:
        if d >= start:
            val = v
        else:
            break
    if val is None:
        raise ValueError(f"no rate before {schedule[0][0]} (asked for {d})")
    return val


EXCHANGE = [(EARLIEST, 3.25 / LAKH), (date(2021, 1, 1), 3.45 / LAKH),
            (date(2023, 4, 1), 3.25 / LAKH), (date(2024, 4, 1), 3.22 / LAKH),
            (date(2024, 10, 1), 2.97 / LAKH), (date(2026, 3, 1), 306.99 / CROR)]
IPFT = [(EARLIEST, 0.01 / CROR), (date(2023, 4, 1), 10 / CROR), (date(2026, 3, 1), 0.01 / CROR)]
SEBI = [(EARLIEST, 10 / CROR), (date(2020, 6, 1), 5 / CROR), (date(2021, 4, 1), 10 / CROR)]
STAMP_BUY = [(EARLIEST, 0.0001), (STAMP_UNIFORM_FROM, 0.00015)]
STAMP_SELL = [(EARLIEST, 0.0001), (STAMP_UNIFORM_FROM, 0.0)]
DP = [(EARLIEST, 13.5), (date(2024, 6, 1), 13.0)]
STT = 0.001
GST = 0.18


def _d(x) -> date:
    if type(x) is date:
        return x
    if isinstance(x, datetime):
        return x.date()
    return pd.Timestamp(x).date()


def _finite(x, what: str) -> float:
    v = float(x)
    if not math.isfinite(v):
        raise ValueError(f"non-finite {what}: {x!r}")
    return v


def _check_finite(df: pd.DataFrame, cols, what: str) -> None:
    """Raise if any of `cols` holds NaN or inf (a NaN would otherwise zero tax silently)."""
    for c in cols:
        v = pd.to_numeric(df[c], errors="coerce").to_numpy(dtype=float)
        bad = ~np.isfinite(v)
        if bad.any():
            i = int(np.flatnonzero(bad)[0])
            raise ValueError(f"{what}: non-finite {c} in {int(bad.sum())} rows, first "
                             f"{df.iloc[i].to_dict()}")


def plus_months(d, n: int) -> date:
    """d + n calendar months, clamped to the month's last day (pandas DateOffset rule)."""
    d = _d(d)
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def round_10(x: float) -> float:
    """Sections 288A/288B: paise ignored, then the nearest multiple of Rs 10 (5 rounds up)."""
    x = _finite(x, "amount to round")
    sign = -1.0 if x < 0 else 1.0
    r = math.floor(abs(x))
    q, last = divmod(r, 10)
    return sign * float(q * 10 + (10 if last >= 5 else 0))


@dataclass(frozen=True)
class Params:
    slippage: float = 0.001            # per side, fraction of traded value (assumption)
    brokerage: float = 0.0             # per side, fraction (zero-brokerage delivery assumed)
    slab: float = 0.30                 # dividend slab rate, before cess
    cess: float = 0.04                 # on all income tax
    stamp_pre_2020: float = 0.0001     # both sides, before 1 Jul 2020 (assumption)
    dp_gst: bool = True
    tax_timing: str = "advance"        # 'advance' (s.208-211, 234B/C); sensitivities
                                       # 'advance_minimum' (15/45/75/100%) and 'april'
    stripping: bool = True             # s.94(7) dividend and s.94(8) bonus stripping
    statutory_rounding: bool = True    # s.288A / s.288B
    fund_ter: float = 0.0016           # index fund expense ratio a year (FUND_SOURCES['ter'])
    fund_stamp: bool = True            # 0.005% on fund unit issues from 1 Jul 2020
    s115r: bool = True                 # distribution tax on fund payouts before 1 Apr 2020


def rates_on(d, p: Params = Params()) -> dict:
    d = _d(d)
    pre = d < STAMP_UNIFORM_FROM
    return {"stt": STT,
            "stamp_buy": p.stamp_pre_2020 if pre else _step(STAMP_BUY, d),
            "stamp_sell": p.stamp_pre_2020 if pre else _step(STAMP_SELL, d),
            "exchange": _step(EXCHANGE, d), "ipft": _step(IPFT, d), "sebi": _step(SEBI, d),
            "brokerage": p.brokerage, "gst": GST, "dp": _step(DP, d)}


def trade_costs(side: str, value: float, d, p: Params = Params(), dp_scrips: int = 1) -> dict:
    """Itemised charges in rupees on one delivery trade of `value` rupees.

    `dp_scrips` is the number of scrips whose depository charge falls on this
    trade (1 for the first sale of a symbol on a day, 0 for a buy or a repeat).
    Slippage is reported separately and is not in `charges`.
    """
    if side not in ("buy", "sell"):
        raise ValueError(side)
    r = rates_on(d, p)
    v = _finite(value, "trade value")
    out = {"stt": STT * v,
           "stamp": v * (r["stamp_buy"] if side == "buy" else r["stamp_sell"]),
           "exchange": v * r["exchange"], "ipft": v * r["ipft"], "sebi": v * r["sebi"],
           "brokerage": v * r["brokerage"]}
    out["gst"] = GST * (out["exchange"] + out["ipft"] + out["sebi"] + out["brokerage"])
    dp = r["dp"] * dp_scrips if side == "sell" else 0.0
    out["dp"] = dp * (1 + GST) if p.dp_gst else dp
    out["charges"] = sum(out[k] for k in ("stt", "stamp", "exchange", "ipft", "sebi",
                                           "brokerage", "gst", "dp"))
    out["deductible"] = out["charges"] - out["stt"]     # s.48 proviso: STT is not
    out["slippage"] = p.slippage * v
    out["total"] = out["charges"] + out["slippage"]
    return out


def cost_table(order_value: float = 50_000.0, flat: float = 0.002, p: Params = Params(),
               dates=None) -> pd.DataFrame:
    """Effective cost per side of one order in each rate period, against the flat rate."""
    if dates is None:
        dates = [date(2019, 2, 15), date(2020, 6, 15), date(2020, 7, 1), date(2021, 1, 1),
                 date(2021, 4, 1), date(2023, 4, 1), date(2024, 4, 1), date(2024, 6, 1),
                 date(2024, 10, 1), date(2026, 3, 1)]
    rows = []
    for d in dates:
        b = trade_costs("buy", order_value, d, p)
        s = trade_costs("sell", order_value, d, p)
        rows.append({"from": d, "buy_charges": b["charges"], "sell_charges": s["charges"],
                     "buy_pct": 100 * b["charges"] / order_value,
                     "sell_pct": 100 * s["charges"] / order_value,
                     "avg_side_pct": 50 * (b["charges"] + s["charges"]) / order_value,
                     "avg_side_with_slippage_pct":
                         50 * (b["total"] + s["total"]) / order_value,
                     "flat_backtest_pct": 100 * flat})
    return pd.DataFrame(rows)


# ------------------------------------------------------------------ holding period
def fy_of(d) -> int:
    """Financial year by its starting calendar year: 2024 means FY 2024-25."""
    d = _d(d)
    return d.year if d.month >= 4 else d.year - 1


def fy_label(fy: int) -> str:
    return f"FY{fy}-{(fy + 1) % 100:02d}"


def is_long_term(buy_date, sell_date) -> bool:
    """Held more than twelve months: sold after the same calendar day a year on."""
    return _d(sell_date) > plus_months(buy_date, 12)


def cg_rates(sell_date) -> tuple[float, float]:
    """(short-term, long-term) rate for listed equity sold on sell_date."""
    return (0.15, 0.10) if _d(sell_date) < RATE_CHANGE else (0.20, 0.125)


def ltcg_exemption(fy: int) -> float:
    if fy < 2018:
        raise ValueError("s.112A applies from FY 2018-19; earlier gains are out of scope")
    return 100_000.0 if fy < 2024 else 125_000.0


# ------------------------------------------------------------------ lots
@dataclass
class Piece:
    """One FIFO match of a sale against a lot."""
    symbol: str
    buy_date: date
    sell_date: date
    units: float
    cost: float          # basis of the units sold, deductible charges included
    proceeds: float      # net of deductible sale charges
    long_term: bool
    reason: str = "trade"
    ignored: float = 0.0     # loss ignored under s.94(7)/(8) (a positive number)
    note: str = ""

    @property
    def gain(self) -> float:
        return self.proceeds - self.cost

    @property
    def taxable_gain(self) -> float:
        return self.proceeds - self.cost + self.ignored

    @property
    def rate(self) -> float:
        st, lt = cg_rates(self.sell_date)
        return lt if self.long_term else st


@dataclass
class Lot:
    buy_date: date
    units: float
    basis: float                 # per unit, deductible charges included
    lot_id: int = 0
    bonus: bool = False          # bonus shares: nil cost, dated at allotment (ex-date proxy)
    strip8: tuple | None = None  # (record date, bonus lot id): bought within 3 months before
    strip7: tuple = ()           # ((record date, exempt dividend per unit), ...)
    lt_after: date | None = None  # buy_date + 12 months: sold after this is long-term

    def __post_init__(self):
        if self.lt_after is None:
            self.lt_after = plus_months(self.buy_date, 12)


class LotBook:
    """FIFO lots per symbol, with bonus lots and the s.94(7)/(8) stripping rules."""

    def __init__(self, stripping: bool = True):
        self.lots: dict[str, deque] = {}
        self.stripping = stripping
        self.s94_log: list[dict] = []
        self._next = 1

    def _new(self, d, units, basis, **kw) -> Lot:
        lot = Lot(_d(d), float(units), float(basis), self._next, **kw)
        self._next += 1
        return lot

    def buy(self, symbol, d, units: float, basis_per_unit: float) -> None:
        units = _finite(units, f"units bought of {symbol}")
        basis_per_unit = _finite(basis_per_unit, f"basis of {symbol}")
        if units <= 0:
            return
        self.lots.setdefault(symbol, deque()).append(self._new(d, units, basis_per_unit))

    def units(self, symbol) -> float:
        return float(sum(l.units for l in self.lots.get(symbol, ())))

    def symbols(self) -> list:
        return [s for s, q in self.lots.items() if q]

    def bonus(self, symbol, ex_date, f: float) -> float:
        """A bonus issue with price factor f (1:1 is 0.5) on ex_date. Returns bonus units.

        Every lot bought before ex_date keeps units x f at its full cost and its own
        date; a bonus lot of units x (1 - f), nil cost, dated ex_date follows every
        lot dated on or before ex_date (FIFO). An original lot bought within 3 months
        before ex_date is linked to its bonus lot for s.94(8).
        """
        f = _finite(f, f"bonus factor of {symbol}")
        if not 0 < f < 1:
            raise ValueError(f"{symbol}: bonus factor {f} is not in (0, 1)")
        q = self.lots.get(symbol)
        if not q:
            return 0.0
        ex = _d(ex_date)
        window = plus_months(ex, -3)
        new = []
        for lot in q:
            if lot.buy_date >= ex:
                continue
            b = self._new(ex, lot.units * (1 - f), 0.0, bonus=True)
            lot.units *= f
            lot.basis /= f
            lot.strip7 = tuple((r, v / f) for r, v in lot.strip7)
            if not lot.bonus and lot.buy_date >= window:
                lot.strip8 = (ex, b.lot_id)
            new.append(b)
        items = list(q)
        pos = next((i for i, l in enumerate(items) if l.buy_date > ex), len(items))
        items[pos:pos] = new
        self.lots[symbol] = deque(items)
        return float(sum(b.units for b in new))

    def tag_dividend(self, symbol, d, per_unit: float) -> None:
        """An exempt dividend on d: lots bought within 3 months before carry it (s.94(7))."""
        if not self.stripping or per_unit <= 0:
            return
        d = _d(d)
        window = plus_months(d, -3)
        for lot in self.lots.get(symbol, ()):
            if window <= lot.buy_date < d and not lot.bonus:
                lot.strip7 = lot.strip7 + ((d, float(per_unit)),)

    def _s94_7(self, lot: Lot, d: date, units: float, loss: float) -> float:
        """Loss ignored under s.94(7) on `units` of `lot` sold on d at a loss of `loss`."""
        ign = 0.0
        for r, dps in lot.strip7:
            if loss - ign <= 1e-12:
                break
            if r < d <= plus_months(r, 3):
                ign += min(loss - ign, dps * units)
        return ign

    def sell(self, symbol, d, units: float | None, proceeds_per_unit: float,
             reason: str = "trade", tol: float = 1e-6) -> list[Piece]:
        """Sell `units` (None = everything) FIFO. Returns the matched pieces."""
        proceeds_per_unit = _finite(proceeds_per_unit, f"proceeds of {symbol}")
        q = self.lots.get(symbol)
        held = self.units(symbol)
        if units is None:
            units = held
        units = _finite(units, f"units sold of {symbol}")
        if units > held * (1 + tol) + 1e-9:
            raise ValueError(f"{symbol}: selling {units} units, only {held} held on {d}")
        units = min(units, held)
        d = _d(d)
        takes, left = [], units
        for lot in (q or ()):
            if left <= 1e-12 * max(1.0, units):
                break
            take = min(lot.units, left)
            takes.append((lot, take))
            left -= take
        out = [Piece(symbol, lot.buy_date, d, take, take * lot.basis, take * proceeds_per_unit,
                     d > lot.lt_after, reason) for lot, take in takes]
        for lot, take in takes:
            lot.units -= take
        if self.stripping:
            for (lot, take), pc in zip(takes, out):
                loss = -pc.gain
                if loss <= 0:
                    continue
                if lot.strip7:
                    ign = self._s94_7(lot, d, take, loss)
                    if ign > 0:
                        pc.ignored += ign
                        pc.note = "s94(7)"
                        self.s94_log.append({"section": "94(7)", "symbol": symbol, "sold": d,
                                             "bought": lot.buy_date, "loss_ignored": ign})
                        loss -= ign
                if lot.strip8 and loss > 1e-12 and d >= S94_8_SHARES_FROM:
                    r, bid = lot.strip8
                    if r < d <= plus_months(r, 9):
                        b = next((l for l in q if l.lot_id == bid), None)
                        if b is not None and b.units > 1e-12:     # bonus shares still held
                            pc.ignored += loss
                            pc.note = (pc.note + " s94(8)").strip()
                            b.basis += loss / b.units
                            self.s94_log.append({"section": "94(8)", "symbol": symbol,
                                                 "sold": d, "bought": lot.buy_date,
                                                 "loss_ignored": loss})
        if q is not None:
            while q and q[0].units <= 1e-12 * max(1.0, units):
                q.popleft()
            if not q:
                self.lots.pop(symbol, None)
        return out

    def liquidation_pieces(self, symbol, d, proceeds_per_unit: float,
                           reason: str = "liquidation") -> list[Piece]:
        """The pieces a sale of everything at d would realise, without selling.

        Aggregated by (long term, sign of taxable gain), which year_tax treats
        exactly like the lot-by-lot pieces (they share one sale date and rate).
        s.94(7) applies; s.94(8) cannot, since no bonus share is held afterwards.
        """
        ppu = _finite(proceeds_per_unit, f"proceeds of {symbol}")
        d = _d(d)
        agg: dict = {}
        for lot in self.lots.get(symbol, ()):
            cost, proc = lot.units * lot.basis, lot.units * ppu
            ign = 0.0
            if self.stripping and lot.strip7 and proc < cost:
                ign = self._s94_7(lot, d, lot.units, cost - proc)
            lt = d > lot.lt_after
            a = agg.setdefault((lt, proc - cost + ign >= 0), [0.0, 0.0, 0.0, 0.0, lot.buy_date])
            a[0] += lot.units
            a[1] += cost
            a[2] += proc
            a[3] += ign
        return [Piece(symbol, a[4], d, a[0], a[1], a[2], lt, reason, ignored=a[3])
                for (lt, _), a in agg.items()]

    def scale(self, f: float) -> None:
        for q in self.lots.values():
            for lot in q:
                lot.units *= f


# ------------------------------------------------------------------ yearly tax
@dataclass
class Loss:
    fy: int            # year the loss arose
    kind: str          # "ST" or "LT"
    amount: float


@dataclass
class YearTax:
    fy: int
    st_gains: float = 0.0          # gross positive short-term gains
    st_losses: float = 0.0         # gross short-term losses (positive number)
    lt_gains: float = 0.0
    lt_losses: float = 0.0
    loss_ignored_s94: float = 0.0  # losses ignored under s.94(7)/(8)
    bf_used_st: float = 0.0        # brought-forward short-term losses used
    bf_used_lt: float = 0.0
    exemption: float = 0.0         # exemption available
    exemption_used: float = 0.0
    taxable: dict = field(default_factory=dict)     # "ST@0.15" -> rupees taxed
    cg_tax: float = 0.0            # before cess
    dividends: float = 0.0
    dividends_taxable: float = 0.0
    dividend_tax: float = 0.0      # before cess
    total_income: float = 0.0      # capital gains after set-off + taxable dividends
    total_income_rounded: float = 0.0
    rounding_tax: float = 0.0      # slab tax on the s.288A rounding (a few paise to Rs 1.5)
    cess: float = 0.0
    tax_rounding: float = 0.0      # s.288B rounding of the tax
    total_tax: float = 0.0
    cf_st: float = 0.0             # losses carried out of this year, all vintages
    cf_lt: float = 0.0
    expired: float = 0.0           # brought-forward losses that lapsed unused

    def row(self) -> dict:
        r = asdict(self)
        r["fy"] = fy_label(self.fy)
        tx = r.pop("taxable")
        for k, v in sorted(tx.items()):
            r[f"taxable_{k}"] = v
        return r


def year_tax(fy: int, pieces: list[Piece], dividends: list[tuple] | float,
             carried: list[Loss], p: Params = Params()) -> tuple[YearTax, list[Loss]]:
    """Tax for one financial year and the losses carried out of it.

    `dividends` is a list of (date, rupees) or a rupee total already known to
    be taxable. `carried` holds losses brought forward (any vintage; expired
    ones are dropped here). Returns (YearTax, losses to carry into fy + 1).
    A non-finite gain or dividend raises: a NaN would otherwise drop out of
    every comparison and silently zero the tax.
    """
    y = YearTax(fy=fy, exemption=ltcg_exemption(fy))
    gains: dict[tuple, float] = {}
    for pc in pieces:
        if fy_of(pc.sell_date) != fy:
            raise ValueError(f"piece sold {pc.sell_date} is not in {fy_label(fy)}")
        g = pc.taxable_gain
        if not math.isfinite(g):
            raise ValueError(f"non-finite gain on {pc.symbol} sold {pc.sell_date}: {pc}")
        y.loss_ignored_s94 += pc.ignored
        kind = "LT" if pc.long_term else "ST"
        if g >= 0:
            gains[(kind, pc.rate)] = gains.get((kind, pc.rate), 0.0) + g
            if kind == "ST":
                y.st_gains += g
            else:
                y.lt_gains += g
        elif kind == "ST":
            y.st_losses -= g
        else:
            y.lt_losses -= g

    def keys(kind):
        return sorted((k for k in gains if k[0] == kind), key=lambda k: -k[1])

    def absorb(loss: float, kinds: tuple) -> float:
        for kind in kinds:
            for k in keys(kind):
                use = min(loss, gains[k])
                gains[k] -= use
                loss -= use
        return loss

    # current-year set-off (s.70)
    lt_left = absorb(y.lt_losses, ("LT",))
    st_left = absorb(y.st_losses, ("ST", "LT"))

    # brought forward (s.74): drop expired, then oldest first; LT then ST
    live, expired = [], 0.0
    for l in carried:
        if not math.isfinite(l.amount):
            raise ValueError(f"non-finite loss brought forward: {l}")
        if l.amount <= 0:
            continue
        if fy - l.fy > CARRY_YEARS:
            expired += l.amount
        elif l.fy < fy:
            live.append(Loss(l.fy, l.kind, l.amount))
        else:
            raise ValueError(f"a loss from {fy_label(l.fy)} brought into {fy_label(fy)}")
    y.expired = expired
    live.sort(key=lambda l: l.fy)
    for l in live:
        if l.kind == "LT":
            before = l.amount
            l.amount = absorb(l.amount, ("LT",))
            y.bf_used_lt += before - l.amount
    for l in live:
        if l.kind == "ST":
            before = l.amount
            l.amount = absorb(l.amount, ("ST", "LT"))
            y.bf_used_st += before - l.amount
    cg_income = sum(v for v in gains.values() if v > 0)

    # annual exemption on long-term gains, highest rate first
    ex = y.exemption
    for k in keys("LT"):
        use = min(ex, gains[k])
        gains[k] -= use
        ex -= use
        y.exemption_used += use

    y.taxable = {f"{k[0]}@{k[1]:g}": v for k, v in gains.items() if v > 1e-9}
    y.cg_tax = sum(k[1] * v for k, v in gains.items() if v > 0)

    if isinstance(dividends, (int, float)):
        y.dividends = y.dividends_taxable = _finite(dividends, "dividends")
    else:
        for d, amt in dividends:
            amt = _finite(amt, f"dividend on {d}")
            y.dividends += amt
            if _d(d) >= DIVIDENDS_TAXABLE_FROM:
                y.dividends_taxable += amt
    y.dividend_tax = max(0.0, y.dividends_taxable) * p.slab
    y.total_income = cg_income + max(0.0, y.dividends_taxable)
    y.total_income_rounded = y.total_income
    if p.statutory_rounding:
        y.total_income_rounded = round_10(y.total_income)
        y.rounding_tax = (y.total_income_rounded - y.total_income) * p.slab
    base = y.cg_tax + y.dividend_tax + y.rounding_tax
    y.cess = p.cess * base
    unrounded = max(0.0, base + y.cess)
    y.total_tax = max(0.0, round_10(unrounded)) if p.statutory_rounding else unrounded
    y.tax_rounding = y.total_tax - unrounded

    out = [l for l in live if l.amount > 1e-9]
    if st_left > 1e-9:
        out.append(Loss(fy, "ST", st_left))
    if lt_left > 1e-9:
        out.append(Loss(fy, "LT", lt_left))
    y.cf_st = sum(l.amount for l in out if l.kind == "ST")
    y.cf_lt = sum(l.amount for l in out if l.kind == "LT")
    return y, out


# ------------------------------------------------------------------ paying the tax
class TaxAccount:
    """One taxpayer's income, advance tax, year-end true-up, interest and refunds.

    `sessions` are the dates on which money can move (the clock's closes).
    Params.tax_timing 'advance': instalments at the last session whose sale
    settles by 15 Jun, 15 Sep, 15 Dec and 15 Mar of each year (T+2 before
    27 Jan 2023, T+1 after), a true-up at the first
    session of the next year (see the module docstring); 'advance_minimum':
    the same dates, paying the cumulative 15/45/75/100% share of the
    year-to-date tax; 'april': only the true-up, with no interest.
    """

    def __init__(self, sessions, start, p: Params = Params()):
        if p.tax_timing not in ("advance", "advance_minimum", "april"):
            raise ValueError(f"tax_timing {p.tax_timing!r}")
        self.p = p
        self.pieces: dict[int, list] = {}
        self.divs: dict[int, list] = {}
        self.carried: list[Loss] = []            # into year settled_through + 1
        self.settled_through = fy_of(start) - 1
        self.advance: dict[int, float] = {}
        self.inst: dict[int, list] = {}          # fy -> [(date, ytd tax, paid)]
        self.rows: list[dict] = []
        self.payments: list[dict] = []
        self.tax_paid = self.interest_paid = self.refunded = 0.0
        self.events: dict = {}
        self.share: dict = {}                    # instalment session -> cumulative share
        sess = pd.DatetimeIndex(sorted(set(pd.to_datetime(list(sessions)))))
        start = pd.Timestamp(start)
        sess = sess[sess >= start.normalize()]
        if len(sess) == 0:
            return
        s_arr = sess.to_numpy()

        def first_on_or_after(d):
            i = int(np.searchsorted(s_arr, np.datetime64(pd.Timestamp(d)), side="left"))
            return sess[i] if i < len(sess) else None

        def pay_by(due):
            """The last session whose sale settles on or before `due`; None when the
            clock ends before the due date (that tax is owed at the end instead)."""
            due = pd.Timestamp(due)
            if due > sess[-1]:
                return None
            j = int(np.searchsorted(s_arr, np.datetime64(due), side="right")) - 1
            i = j - (2 if due < T1_SETTLEMENT_FROM else 1)
            return sess[i] if 0 <= i < len(sess) else None

        for f in range(fy_of(sess[0]), fy_of(sess[-1]) + 1):
            if f > fy_of(start):
                t = first_on_or_after(pd.Timestamp(f, 4, 1))
                if t is not None and fy_of(t) == f:
                    self.events.setdefault(t, []).append(("true_up", f - 1))
            if p.tax_timing != "april":
                for (m, dd), share in zip(INSTALMENT_DATES, INSTALMENT_SHARES):
                    due = pd.Timestamp(f + (m < 4), m, dd)
                    if due < start.normalize():          # due before the clock began
                        continue
                    t = pay_by(due)
                    if t is not None and fy_of(t) == f:
                        self.events.setdefault(t, []).append(("instalment", f))
                        self.share[t] = share if p.tax_timing == "advance_minimum" else 1.0
                        self.inst.setdefault(f, [])

    # income
    def add(self, pieces) -> None:
        for pc in pieces:
            self.pieces.setdefault(fy_of(pc.sell_date), []).append(pc)

    def add_dividend(self, d, amount: float) -> None:
        amount = _finite(amount, f"dividend on {d}")
        self.divs.setdefault(fy_of(d), []).append((pd.Timestamp(d), amount))

    def due(self, d) -> list:
        return self.events.get(pd.Timestamp(d), [])

    # the computations
    def _tax(self, fy, carried, before=None, extra=()):
        ps = self.pieces.get(fy, [])
        dv = self.divs.get(fy, [])
        if before is not None:
            b = _d(before)
            ps = [x for x in ps if x.sell_date < b]
            dv = [x for x in dv if _d(x[0]) < b]
        return year_tax(fy, list(ps) + list(extra), dv, carried, self.p)

    def _interest(self, fy, tax, advance, on) -> tuple[float, float]:
        """(s.234B, s.234C) interest on a year's shortfall paid on date `on`."""
        if self.p.tax_timing == "april" or tax < ADVANCE_TAX_MIN:
            return 0.0, 0.0
        short = max(0.0, tax - advance)
        if self.p.statutory_rounding:                   # Rule 119A(b): whole Rs 100, fraction ignored
            short = math.floor(short / 100.0) * 100.0
        if short <= 0:
            return 0.0, 0.0
        on = _d(on)
        c = INTEREST_A_MONTH * short                        # 15 March instalment, 1 month
        months = max(1, (on.year - (fy + 1)) * 12 + on.month - 4 + 1)
        b = INTEREST_A_MONTH * months * short if advance < S234B_PAID_SHARE * tax else 0.0
        if self.p.statutory_rounding:
            b, c = round_10(b), round_10(c)
        return b, c

    def instalment(self, d, fy) -> float:
        """Advance tax due at session d: year-to-date tax on income realised before d
        (times the cumulative share under 'advance_minimum'), less what is paid, when
        that year-to-date tax is Rs 10,000 or more."""
        if fy != self.settled_through + 1:
            raise RuntimeError(f"instalment for {fy_label(fy)} before earlier years settled")
        y, _ = self._tax(fy, self.carried, before=d)
        ytd = y.total_tax
        share = self.share.get(pd.Timestamp(d), 1.0)
        amt = (max(0.0, share * ytd - self.advance.get(fy, 0.0))
               if ytd >= ADVANCE_TAX_MIN else 0.0)
        if self.p.statutory_rounding and amt > 0:
            amt = max(0.0, round_10(amt))
        self.inst.setdefault(fy, []).append((pd.Timestamp(d), ytd, amt))
        return amt

    def paid_instalment(self, d, fy, amount: float) -> None:
        self.advance[fy] = self.advance.get(fy, 0.0) + amount
        self.tax_paid += amount
        self.payments.append({"date": pd.Timestamp(d).date(), "fy": fy_label(fy),
                              "kind": "advance_tax", "amount": amount})

    def true_up(self, d, fy) -> float:
        """Settle year fy at session d. Returns the cash due (negative: a refund)."""
        if fy != self.settled_through + 1:
            raise RuntimeError(f"true-up of {fy_label(fy)} out of order")
        y, self.carried = self._tax(fy, self.carried)
        self.settled_through = fy
        tax, adv = y.total_tax, self.advance.get(fy, 0.0)
        i234b, i234c = self._interest(fy, tax, adv, d)
        net = tax - adv
        insts = self.inst.get(fy, [])
        skipped = sum(1 for _, ytd, _ in insts if ytd < ADVANCE_TAX_MIN)
        self.rows.append({**y.row(), "advance_paid": adv, "instalments_paid":
                          sum(1 for *_, a in insts if a > 0),
                          "instalments_skipped": skipped,
                          "liable_advance_tax": bool(tax >= ADVANCE_TAX_MIN),
                          "self_assessment": max(0.0, net), "refund": max(0.0, -net),
                          "interest_234b": i234b, "interest_234c": i234c,
                          "paid_on": pd.Timestamp(d).date(), "paid": tax + i234b + i234c})
        self._book(d, fy, net, i234b + i234c)
        return net + i234b + i234c

    def _book(self, d, fy, net, interest):
        dd = pd.Timestamp(d).date()
        if net > 0:
            self.tax_paid += net
            self.payments.append({"date": dd, "fy": fy_label(fy), "kind": "self_assessment",
                                  "amount": net})
        elif net < 0:
            self.tax_paid += net
            self.refunded -= net
            self.payments.append({"date": dd, "fy": fy_label(fy), "kind": "refund",
                                  "amount": net})
        if interest:
            self.interest_paid += interest
            self.payments.append({"date": dd, "fy": fy_label(fy), "kind": "interest",
                                  "amount": interest})

    def owed(self, d, extra=()) -> float:
        """Tax (and interest on any past year) owed if every open year were closed at d,
        on income realised so far plus `extra` pieces, less the advance tax paid."""
        f_now = fy_of(d)
        carried, total = self.carried, 0.0
        for f in range(self.settled_through + 1, f_now + 1):
            y, carried = self._tax(f, carried, extra=extra if f == f_now else ())
            adv = self.advance.get(f, 0.0)
            total += y.total_tax - adv
            if f < f_now:
                total += sum(self._interest(f, y.total_tax, adv, d))
        return total

    def close_all(self, d, extra=()) -> float:
        """Settle every open year at d (the end of the clock), `extra` pieces included."""
        self.add(extra)
        f_now = fy_of(d)
        total = 0.0
        for f in range(self.settled_through + 1, f_now + 1):
            y, self.carried = self._tax(f, self.carried)
            self.settled_through = f
            tax, adv = y.total_tax, self.advance.get(f, 0.0)
            i234b, i234c = self._interest(f, tax, adv, d) if f < f_now else (0.0, 0.0)
            net = tax - adv
            insts = self.inst.get(f, [])
            self.rows.append({**y.row(), "advance_paid": adv, "instalments_paid":
                              sum(1 for *_, a in insts if a > 0),
                              "instalments_skipped": sum(1 for _, t, _ in insts
                                                         if t < ADVANCE_TAX_MIN),
                              "liable_advance_tax": bool(tax >= ADVANCE_TAX_MIN),
                              "self_assessment": max(0.0, net), "refund": max(0.0, -net),
                              "interest_234b": i234b, "interest_234c": i234c,
                              "paid_on": pd.Timestamp(d).date(), "paid": tax + i234b + i234c,
                              "open_at_end": f == f_now})
            total += net + i234b + i234c
        return total


# ------------------------------------------------------------------ strategy layer
@dataclass
class AfterTax:
    nav: pd.DataFrame            # date, mark, pre_tax, k, after_tax, ..., liquidation_value
    years: pd.DataFrame          # one row per financial year (YearTax.row + payment)
    pieces: pd.DataFrame         # every FIFO match
    costs: pd.DataFrame          # per trade: itemised charges vs flat
    final: dict                  # end values, liquidation, checks
    payments: pd.DataFrame = None    # every tax payment, refund and interest


def _units_path(trades: pd.DataFrame) -> pd.DataFrame:
    """Pre-tax units held at the close of each trade date (wide: date x symbol)."""
    t = trades[trades["side"].isin(["buy", "sell"])].copy()
    t["signed"] = np.where(t["side"] == "buy", t["units"], -t["units"])
    w = t.pivot_table(index="date", columns="symbol", values="signed", aggfunc="sum").fillna(0.0)
    return w.cumsum()


def reconstruct_dividends(trades: pd.DataFrame, dates: pd.DatetimeIndex, symbols,
                          div_unit: np.ndarray, tol: float = 1e-6) -> pd.DataFrame:
    """Dividends the engine credited: units held at the previous close x cash per unit.

    `div_unit` is engine.Panel.div_unit (dates x symbols, cash per adjusted unit
    credited on each date), for the same `dates` and `symbols`. This is the
    engine's own rule (simulate, step 1): cash += units(t-1) @ div_unit[t].
    Amounts under `tol` rupees (float residue of a closed position) are dropped.
    """
    u = _units_path(trades)
    u.index = pd.to_datetime(u.index)
    dates = pd.DatetimeIndex(dates)
    held = u.reindex(u.index.union(dates)).ffill().reindex(dates).fillna(0.0)
    held = held.reindex(columns=list(symbols), fill_value=0.0).to_numpy()
    prev = np.vstack([np.zeros((1, held.shape[1])), held[:-1]])
    cash = prev * div_unit
    ti, si = np.nonzero(np.abs(cash) > tol)
    return pd.DataFrame({"date": dates[ti], "symbol": np.asarray(symbols)[si],
                         "amount": cash[ti, si]})


def replay_check(trades: pd.DataFrame, nav: pd.DataFrame, dividends: pd.DataFrame,
                 close_px: pd.DataFrame, end_px: pd.Series, nav_col: str = "strategy",
                 bench: pd.DataFrame | None = None, bench_col: str | None = None) -> dict:
    """Rebuild the engine's NAV from the inputs this layer uses, before any tax.

    cash = start NAV - buys x (1 + c) + sells x (1 - c) + dividends; NAV at each
    close = cash + units x close_px, and at the end mark cash + units x end_px.
    If the trade log, prices and dividends are the ones the engine used, this
    matches nav[nav_col] to rounding. Also reconciles every trade's value with
    units x price (the layer takes cost basis and proceeds from the price
    column), and, given `bench`, the benchmark's clock, its TRI against
    nav[bench_col] and the price index's validity. NaN anywhere raises.
    Returns the worst relative errors and where.
    """
    nv = nav[["date", "mark", nav_col]].copy()
    nv["date"] = pd.to_datetime(nv["date"])
    _check_finite(nv, [nav_col], "nav")
    tr = trades[trades["side"].isin(["buy", "sell"])].copy()
    tr["date"] = pd.to_datetime(tr["date"])
    _check_finite(tr, ["units", "price", "value", "cost"], "trades")
    tv = (tr["value"] - tr["units"] * tr["price"]).abs() / np.maximum(1.0, tr["value"].abs())
    tv_worst = float(tv.max()) if len(tv) else 0.0
    tv_at = (f"{tr['date'].iloc[int(tv.to_numpy().argmax())].date()} "
             f"{tr['symbol'].iloc[int(tv.to_numpy().argmax())]}") if len(tv) else None
    tr["cash"] = np.where(tr["side"] == "buy", -(tr["value"] + tr["cost"]),
                          tr["value"] - tr["cost"])
    tr["du"] = np.where(tr["side"] == "buy", tr["units"], -tr["units"])
    dv = dividends.copy()
    dv["date"] = pd.to_datetime(dv["date"]) if not dv.empty else pd.to_datetime([])
    if not dv.empty:
        _check_finite(dv, ["amount"], "dividends")
    cash_by = tr.groupby("date")["cash"].sum().add(
        dv.groupby("date")["amount"].sum() if not dv.empty else pd.Series(dtype=float),
        fill_value=0.0)
    du_by = {d: g.groupby("symbol")["du"].sum() for d, g in tr.groupby("date")}
    cpx = close_px.copy()
    cpx.index = pd.to_datetime(cpx.index)
    cash = float(nv[nav_col].iloc[0])
    units = pd.Series(dtype=float)
    worst, where = 0.0, None
    body = nv.iloc[1:]
    for r in body.itertuples(index=False):
        d, mark, want = r.date, r.mark, float(getattr(r, nav_col))
        cash += float(cash_by.get(d, 0.0))
        if d in du_by:
            units = units.add(du_by[d], fill_value=0.0)
        live = units[units.abs() > 1e-9]
        px = cpx.loc[d, live.index] if mark == "close" else end_px.reindex(live.index)
        px = px.astype(float)
        if not np.isfinite(px.to_numpy()).all():
            raise ValueError(f"replay: no price on {d.date()} {mark} for "
                             f"{list(px.index[~np.isfinite(px.to_numpy())])}")
        got = cash + float((live * px).sum())
        if not math.isfinite(got):
            raise ValueError(f"replay: non-finite NAV on {d.date()} {mark}")
        err = abs(got - want) / max(1.0, abs(want))
        if err > worst:
            worst, where = err, f"{d.date()} {mark}"
    out = {"worst_rel_error": worst, "at": where, "marks": len(body),
           "trade_value_worst_rel": tv_worst, "trade_value_at": tv_at,
           "trades": int(len(tr))}
    if bench is not None:
        out["bench"] = bench_check(bench, nav, bench_col)
    return out


def bench_check(bench: pd.DataFrame, nav: pd.DataFrame, bench_col: str | None) -> dict:
    """The benchmark inputs: on the NAV's clock, TRI equal to nav[bench_col] where that
    column exists, both levels finite and positive, and the implied dividend yield."""
    b = bench.reset_index(drop=True).copy()
    b["date"] = pd.to_datetime(b["date"])
    _check_finite(b, ["tri", "price"], "benchmark")
    if (b["tri"] <= 0).any() or (b["price"] <= 0).any():
        raise ValueError("benchmark: a non-positive index level")
    a = nav.reset_index(drop=True)
    if len(a) != len(b) or not (pd.to_datetime(a["date"]).equals(b["date"])
                                and a["mark"].equals(b["mark"])):
        raise ValueError("benchmark inputs are not on the strategy's clock")
    tri_err = None
    if bench_col is not None and bench_col in nav.columns:
        t = a[bench_col].astype(float).to_numpy()
        tri_err = float(np.max(np.abs(b["tri"].to_numpy() - t) / np.abs(t)))
    tri, pr = b["tri"].to_numpy(float), b["price"].to_numpy(float)
    yld = tri[1:] / tri[:-1] - pr[1:] / pr[:-1]
    years = (b["date"].iloc[-1] - b["date"].iloc[0]).days / 365.25
    return {"tri_vs_nav_worst_rel": tri_err, "marks": int(len(b)),
            "implied_dividend_yield_a_year": float(yld[yld > 0].sum() / years) if years else None,
            "negative_residue_marks": int((yld < 0).sum()),
            "negative_residue_sum": float(-yld[yld < 0].sum())}


def _pro_rata(book: LotBook, u_pre: dict, k: float, f: float, d, px: dict, p: Params,
              reason: str, with_costs: bool) -> tuple[list, float]:
    """Sell (f > 0) or buy (f < 0) the fraction |f| of every after-tax holding at px.

    Returns (FIFO pieces sold, charges incl. slippage when with_costs).
    """
    pieces, charges = [], 0.0
    for s, u in u_pre.items():
        if u <= 0:
            continue
        units_at = abs(f) * k * u
        if units_at <= 0:
            continue
        v = units_at * px[s]
        if f > 0:
            if with_costs:
                c = trade_costs("sell", v, d, p)
                charges += c["total"]
                net = px[s] * (1 - p.slippage) - c["deductible"] / units_at
            else:
                net = px[s]
            pieces.extend(book.sell(s, d, units_at, net, reason=reason))
        else:
            book.buy(s, d, units_at, px[s])
    return pieces, charges


def _bonus_frame(bonuses) -> pd.DataFrame:
    if bonuses is None or len(bonuses) == 0:
        return pd.DataFrame({"symbol": [], "ex_date": pd.to_datetime([]), "factor": []})
    b = bonuses[["symbol", "ex_date", "factor"]].copy()
    b["ex_date"] = pd.to_datetime(b["ex_date"])
    _check_finite(b, ["factor"], "bonuses")
    if ((b["factor"] <= 0) | (b["factor"] >= 1)).any():
        raise ValueError("bonuses: a factor outside (0, 1)")
    if b.duplicated(["symbol", "ex_date"]).any():
        raise ValueError("bonuses: two rows for one (symbol, ex_date); multiply them first")
    return b.sort_values(["ex_date", "symbol"]).reset_index(drop=True)


def after_tax_strategy(trades: pd.DataFrame, nav: pd.DataFrame, dividends: pd.DataFrame,
                       close_px: pd.DataFrame, end_px: pd.Series, p: Params = Params(),
                       nav_col: str = "strategy", capital: float | None = None,
                       bonuses: pd.DataFrame | None = None, series: bool = True) -> AfterTax:
    """Apply itemised costs, slippage and tax to one backtest.

    trades     engine trade log (date, symbol, side, units, price, value, cost, reason);
               adjusted units and prices. 'cancel' rows are ignored.
    nav        date, mark, <nav_col>: open of the first date, closes, open of the end.
    dividends  date, symbol, amount (rupees at the pre-tax scale).
    close_px   adjusted closes, ffilled (dates x symbols), as engine.Panel.last:
               values the book on days money leaves it (tax, cost re-booking).
    end_px     adjusted price per symbol at the final mark (the engine's end mark).
    capital    starting rupees of the after-tax book; None = the backtest's own
               starting NAV. The tax exemption and the per-scrip depository
               charge are fixed rupee amounts, so the answer depends on it.
    bonuses    symbol, ex_date, factor: equity bonus issues (one row per symbol and
               ex-date, factors of one day multiplied). None: bonus shares are left
               averaged into the parent lot, as a split is (the pre-fix treatment).
    series     compute held_value and liquidation_value at every mark (off for
               sensitivity runs that need only the end values).

    The after-tax book is k(t) x the pre-tax book. k moves only when:
      * a trade day's itemised costs differ from the flat cost the engine charged:
        the difference is raised (or returned) at that day's close by selling
        (or buying) the same fraction of every holding, through the lot book, so
        these sales are taxed like any other (reason 'cost_rebook'; the re-booking
        trades themselves carry no further costs, a second-order omission);
      * tax is paid or refunded (module docstring, 'tax payment'): the cash is
        raised by selling the same fraction of cash and every holding, FIFO,
        taxed in the year of sale (reasons 'advance_tax', 'self_assessment', or
        'tax_funding' under the 'april' timing); a refund buys the same fraction
        of every holding at that close, without costs.
    Every after-tax statistic should be computed from nav['liquidation_value'];
    final['after_tax_end_held'] is the end book less the tax already owed on the
    open year's realised income, final['after_tax_end_liquidated'] also sells
    everything at the end mark.
    """
    nav = nav[["date", "mark", nav_col]].rename(columns={nav_col: "pre_tax"}).copy()
    nav["date"] = pd.to_datetime(nav["date"])
    _check_finite(nav, ["pre_tax"], "nav")
    closes = nav[nav["mark"] == "close"].set_index("date")["pre_tax"]
    if closes.index.duplicated().any():
        raise ValueError("nav has two closes on one date")
    start, end = nav["date"].iloc[0], nav["date"].iloc[-1]
    nav0 = float(nav["pre_tax"].iloc[0])
    end_nav = float(nav["pre_tax"].iloc[-1])
    tr = trades[trades["side"].isin(["buy", "sell"])].copy()
    tr["date"] = pd.to_datetime(tr["date"])
    _check_finite(tr, ["units", "price", "value", "cost"], "trades")
    dv = dividends.copy()
    if dv.empty:
        dv = pd.DataFrame({"date": pd.to_datetime([]), "symbol": [], "amount": []})
    dv["date"] = pd.to_datetime(dv["date"])
    if not dv.empty:
        _check_finite(dv, ["amount"], "dividends")
    close_px = close_px.copy()
    close_px.index = pd.to_datetime(close_px.index)
    bz = _bonus_frame(bonuses)
    bz = bz[(bz["ex_date"] > start) & (bz["ex_date"] <= end)].reset_index(drop=True)
    pending = list(bz.itertuples(index=False))

    acct = TaxAccount(closes.index, start, p)
    book = LotBook(stripping=p.stripping)
    u_pre: dict[str, float] = {}
    k = 1.0 if capital is None else float(capital) / nav0
    k0 = k
    cost_rows, k_at, bonus_log = [], {}, []
    rebook_abs = funding_charges = 0.0
    held_at, owed_at, liq_at, exit_at, owed_liq_at = {}, {}, {}, {}, {}

    tr_by_date = {d: g for d, g in tr.groupby("date", sort=True)}
    dv_by_date = {d: g for d, g in dv.groupby("date", sort=True)}
    event_dates = sorted(set(tr_by_date) | set(dv_by_date) | set(closes.index))

    def px_on(d):
        row = close_px.loc[d]
        out = {s: float(row[s]) for s in u_pre}
        bad = [s for s, v in out.items() if not math.isfinite(v)]
        if bad:
            raise ValueError(f"{d.date()}: no close price for held {bad}")
        return out

    def apply_bonuses(d):
        while pending and pending[0].ex_date <= d:
            b = pending.pop(0)
            if book.units(b.symbol) > 0:
                bu = book.bonus(b.symbol, b.ex_date, float(b.factor))
                bonus_log.append({"symbol": b.symbol, "ex_date": str(b.ex_date.date()),
                                  "factor": float(b.factor), "bonus_units": bu})

    def pay(amount, d, reason):
        """Raise `amount` rupees at d's close pro rata, costs of raising it included."""
        nonlocal k, funding_charges
        V = k * float(closes[d])
        if amount >= V:
            raise ValueError(f"{d.date()}: tax {amount:,.0f} exceeds the portfolio {V:,.0f}")
        pxd = px_on(d)
        held_val = sum(k * u * pxd[s] for s, u in u_pre.items() if u > 0)
        if held_val > V * (1 + 1e-6) + 1:
            raise ValueError(f"{d.date()}: holdings {held_val:,.0f} exceed the book "
                             f"{V:,.0f}; prices and units disagree")
        phi = amount / V
        for _ in range(8):          # phi = (amount + costs of raising it) / V
            cost = sum(trade_costs("sell", phi * k * u * pxd[s], d, p)["total"]
                       for s, u in u_pre.items() if u > 0)
            phi = (amount + cost) / V
        ps, charges = _pro_rata(book, u_pre, k, phi, d, pxd, p, reason, True)
        acct.add(ps)
        funding_charges += charges
        k *= (1 - phi)

    def refund(amount, d):
        nonlocal k
        f = -amount / (k * float(closes[d]))
        _pro_rata(book, u_pre, k, f, d, px_on(d), p, "refund", False)
        k *= (1 - f)

    def liquidation_at(d, prices):
        """(exit charges, pieces) of selling every lot at `prices` on d, without selling."""
        charges, pieces = 0.0, []
        for s in book.symbols():
            units_at = book.units(s)
            px = float(prices[s])
            if not math.isfinite(px):
                raise ValueError(f"{pd.Timestamp(d).date()}: no price for held {s}")
            c = trade_costs("sell", units_at * px, d, p)
            charges += c["total"]
            pieces.extend(book.liquidation_pieces(
                s, d, px * (1 - p.slippage) - c["deductible"] / units_at))
        return charges, pieces

    for d in event_dates:
        if d > end or d < start:
            raise ValueError(f"event on {d.date()} outside the clock {start.date()}..{end.date()}")
        apply_bonuses(d)
        # 1. dividends credited before the open (to units held at the previous close)
        if d in dv_by_date:
            for r in dv_by_date[d].itertuples(index=False):
                acct.add_dividend(d, k * float(r.amount))
                if p.stripping and d < pd.Timestamp(DIVIDENDS_TAXABLE_FROM) \
                        and u_pre.get(r.symbol, 0.0) > 0:
                    book.tag_dividend(r.symbol, d, float(r.amount) / u_pre[r.symbol])
        # 2. trades: rebalance fills at the open, forced exits at the close
        if d in tr_by_date:
            if d not in closes.index:
                raise ValueError(f"trade on {d.date()} but no close NAV that day")
            sold_today, day_delta = set(), 0.0
            g = tr_by_date[d]
            g = pd.concat([g[g["side"] == "sell"], g[g["side"] == "buy"]])   # engine order
            for r in g.itertuples(index=False):
                v_at, units_at = k * float(r.value), k * float(r.units)
                if units_at <= 0 or v_at <= 0:
                    continue
                first = r.side == "sell" and r.symbol not in sold_today
                c = trade_costs(r.side, v_at, d, p, dp_scrips=1 if first else 0)
                px = float(r.price)
                if r.side == "buy":
                    basis = px * (1 + p.slippage) + c["deductible"] / units_at
                    book.buy(r.symbol, d, units_at, basis)
                    u_pre[r.symbol] = u_pre.get(r.symbol, 0.0) + float(r.units)
                else:
                    sold_today.add(r.symbol)
                    if r.symbol not in u_pre:
                        raise ValueError(f"{d.date()}: sale of {r.symbol}, which is not held")
                    u_pre[r.symbol] -= float(r.units)
                    full = abs(u_pre[r.symbol]) <= 1e-9 * max(1.0, float(r.units))
                    if full:
                        u_pre.pop(r.symbol)
                    net = px * (1 - p.slippage) - c["deductible"] / units_at
                    acct.add(book.sell(r.symbol, d, None if full else units_at, net,
                                       reason=str(getattr(r, "reason", "trade"))))
                flat = k * float(r.cost)
                delta = c["total"] - flat
                day_delta += delta
                cost_rows.append({"date": d.date(), "symbol": r.symbol, "side": r.side,
                                  "value": v_at, **{f"c_{x}": c[x] for x in
                                  ("stt", "stamp", "exchange", "ipft", "sebi", "gst", "dp",
                                   "charges", "slippage", "total")},
                                  "flat": flat, "delta": delta, "k_before": k})
            # the day's cost difference, re-booked pro rata at the close
            if day_delta != 0.0:
                V = k * float(closes[d])
                f = day_delta / V
                ps, _ = _pro_rata(book, u_pre, k, f, d, px_on(d), p, "cost_rebook", False)
                acct.add(ps)
                rebook_abs += abs(day_delta)
                k *= (1 - f)
        # 3. tax: the true-up of last year, or an advance-tax instalment, at the close
        for kind, fy in acct.due(d):
            if kind == "true_up":
                amt = acct.true_up(d, fy)
                if amt > 0:
                    pay(amt, d, "tax_funding" if p.tax_timing == "april" else "self_assessment")
                elif amt < 0:
                    refund(-amt, d)
            else:
                amt = acct.instalment(d, fy)
                if amt > 0:
                    pay(amt, d, "advance_tax")
                    acct.paid_instalment(d, fy, amt)
        k_at[d] = k
        if series and d in closes.index:
            V = k * float(closes[d])
            owed = acct.owed(d)
            ch, hyp = liquidation_at(d, close_px.loc[d])
            owed_liq = acct.owed(d, hyp)
            held_at[d], owed_at[d] = V - owed, owed
            exit_at[d], owed_liq_at[d], liq_at[d] = ch, owed_liq, V - ch - owed_liq

    # lot book and pre-tax units must agree (units in the book are k x pre-tax units)
    worst = 0.0
    for s in set(book.symbols()) | set(u_pre):
        want, have = k * u_pre.get(s, 0.0), book.units(s)
        rel = abs(have - want) / max(1e-9, abs(want), abs(have))
        if not math.isfinite(rel):
            raise AssertionError(f"lot book check is not finite for {s}")
        worst = max(worst, rel)
    if worst > 1e-6:
        raise AssertionError(f"lot book disagrees with the trade log (worst rel. {worst:.2e})")

    # end: the final mark. Hold (tax owed so far), or liquidate and settle every open year.
    apply_bonuses(end)
    V_end = k * end_nav
    ep = {s: float(end_px[s]) for s in u_pre}
    bad = [s for s, v in ep.items() if not math.isfinite(v)]
    if bad:
        raise ValueError(f"no end price for held {bad}")
    pos_val = sum(k * u * ep[s] for s, u in u_pre.items())
    cash_end = V_end - pos_val
    owed_held = acct.owed(end)
    liq_charges, liq_pieces = 0.0, []
    for s in list(book.symbols()):
        units_at = book.units(s)
        px = ep[s]
        c = trade_costs("sell", units_at * px, end, p)
        liq_charges += c["total"]
        liq_pieces.extend(book.sell(s, end, None,
                                    px * (1 - p.slippage) - c["deductible"] / units_at,
                                    reason="final_liquidation"))
    owed_liq = acct.close_all(end, liq_pieces)
    liquidation = V_end - liq_charges - owed_liq
    held = V_end - owed_held

    nav["k"] = nav["date"].map(pd.Series(k_at)).ffill()
    nav.loc[nav.index[0], "k"] = k0          # the opening mark precedes any event
    nav["k"] = nav["k"].fillna(k0)
    nav["pre_tax"] = nav["pre_tax"] * k0     # the pre-tax book at the same capital
    nav["after_tax"] = nav["k"] / k0 * nav["pre_tax"]
    if series:
        is_close = nav["mark"] == "close"
        for col, src in (("tax_owed_realised", owed_at), ("held_value", held_at),
                         ("exit_charges", exit_at), ("tax_owed_if_liquidated", owed_liq_at),
                         ("liquidation_value", liq_at)):
            nav[col] = np.where(is_close, nav["date"].map(pd.Series(src, dtype=float)), np.nan)
        first, last = nav.index[0], nav.index[-1]
        nav.loc[first, ["tax_owed_realised", "exit_charges", "tax_owed_if_liquidated"]] = 0.0
        nav.loc[first, ["held_value", "liquidation_value"]] = nav.loc[first, "after_tax"]
        nav.loc[last, ["tax_owed_realised", "held_value", "exit_charges",
                       "tax_owed_if_liquidated", "liquidation_value"]] = [
            owed_held, held, liq_charges, owed_liq, liquidation]
        _check_finite(nav, ["liquidation_value", "held_value"], "after-tax series")
    allp = [asdict(x) | {"gain": x.gain, "taxable_gain": x.taxable_gain, "rate": x.rate}
            for f in sorted(acct.pieces) for x in acct.pieces[f]]
    costs = pd.DataFrame(cost_rows)
    turnover = float(costs["value"].sum()) if not costs.empty else 0.0
    s94 = pd.DataFrame(book.s94_log)
    final = {"start": str(start.date()), "end": str(end.date()), "capital": k0 * nav0,
             "tax_timing": p.tax_timing,
             "pre_tax_end": k0 * end_nav, "k_end_over_k0": k / k0,
             "after_tax_end_book": V_end,
             "after_tax_end_held": held, "after_tax_end_liquidated": liquidation,
             "liquidation_charges": liq_charges,
             "tax_owed_realised_at_end": owed_held, "tax_due_at_end": owed_liq,
             "tax_paid_during": acct.tax_paid, "interest_paid": acct.interest_paid,
             "refunds": acct.refunded, "advance_tax_paid": float(sum(acct.advance.values())),
             "tax_funding_charges": funding_charges,
             "cash_end_after_tax": cash_end,
             "itemised_costs_total": float(costs["c_total"].sum()) if not costs.empty else 0.0,
             "flat_costs_total": float(costs["flat"].sum()) if not costs.empty else 0.0,
             "cost_rebook_abs": rebook_abs,
             "cost_rebook_share_of_turnover": rebook_abs / turnover if turnover else 0.0,
             "lot_book_check_worst_rel": worst,
             "bonus_events_applied": bonus_log,
             "s94_loss_ignored": ({sec: float(g["loss_ignored"].sum())
                                   for sec, g in s94.groupby("section")} if not s94.empty else {}),
             "s94_pieces": int(len(s94)),
             "losses_carried_out": [asdict(l) for l in acct.carried]}
    return AfterTax(nav=nav, years=pd.DataFrame(acct.rows), pieces=pd.DataFrame(allp),
                    costs=costs, final=final, payments=pd.DataFrame(acct.payments))


# ------------------------------------------------------------------ index fund benchmark
def benchmark_after_tax(clock: pd.DataFrame, initial: float, p: Params = Params(),
                        mode: str = "growth", ter: float | None = None,
                        series: bool = True) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """A buy-and-hold Nifty 500 index fund on the strategy's clock, after tax.

    clock   rows in clock order with columns date, mark, tri, price: the
            total-return and price index levels at each mark (open of the first
            date, closes, open of the end). Dates may repeat (open and close).
    mode    'growth' (default, the headline, as A6 asks: held throughout and
            sold at the end): the fund keeps the index's dividends; its NAV per
            unit is the TRI less the expense ratio; the only tax is on the sale.
            'distributed' (a labelled sensitivity, close to holding the index
            directly): the index's dividends, estimated at each mark as units x
            previous NAV x (TRI return - price return) with negative residues
            clipped to zero and counted, are paid out and reinvested at that
            mark's NAV as a new lot. Before 1 April 2020 a payout is a fund
            distribution net of s.115R tax (S115R_RATE of the grossed-up amount,
            from 1 April 2018) and exempt in the investor's hands; from 1 April
            2020 it is taxed at the slab rate plus cess, paid by redeeming units
            FIFO on the same advance-tax schedule as the strategy.
    ter     expense ratio a year, accrued daily over calendar days; None takes
            Params.fund_ter. 0 gives the gross TRI.
    Stamp duty of 0.005% applies to every unit issue from 1 July 2020 (the
    initial purchase if the clock starts after it, reinvested payouts, refunds
    reinvested) when Params.fund_stamp; it is part of the units' cost. Everything
    is sold at the last mark and the remaining tax settled. Redemption STT, exit
    loads and s.94 on the fund's units are ignored (stated). Before 6 Sep 2019
    no Nifty 500 index fund existed: the benchmark is a hypothetical fund.
    Every after-tax statistic should be computed from the returned frame's
    liquidation_value column (the value less the tax if redeemed at that mark).
    """
    if mode not in ("growth", "distributed"):
        raise ValueError(mode)
    ter = p.fund_ter if ter is None else float(ter)
    c = clock.reset_index(drop=True).copy()
    c["date"] = pd.to_datetime(c["date"])
    _check_finite(c, ["tri", "price"], "benchmark")
    tri, pr = c["tri"].astype(float).to_numpy(), c["price"].astype(float).to_numpy()
    if (tri <= 0).any() or (pr <= 0).any():
        raise ValueError("benchmark: a non-positive index level")
    n = len(c)
    start, end = c["date"].iloc[0], c["date"].iloc[-1]
    days = (c["date"] - start).dt.days.to_numpy(dtype=float)
    fee = (1.0 - ter / 365.0) ** days
    level = tri if mode == "growth" else pr
    nav_u = level * fee                          # the fund's NAV per unit
    closes = c.loc[c["mark"] == "close", "date"]
    acct = TaxAccount(closes, start, p)
    book = LotBook(stripping=False)
    rows = []
    clipped = s115r_paid = stamp_paid = 0.0

    def stamp_on(d):
        return FUND_STAMP if (p.fund_stamp and _d(d) >= FUND_STAMP_FROM) else 0.0

    st0 = stamp_on(start)
    units = initial * (1 - st0) / nav_u[0]
    stamp_paid += initial * st0
    book.buy("FUND", start, units, initial / units)

    def mark_row(i, d):
        v = units * nav_u[i]
        r = {"date": d, "mark": c["mark"].iloc[i], "pre_tax": initial * tri[i] / tri[0],
             "after_tax": v}
        if series:
            owed = acct.owed(d)
            owed_liq = acct.owed(d, book.liquidation_pieces("FUND", d, nav_u[i]))
            r.update({"tax_owed_realised": owed, "held_value": v - owed,
                      "tax_owed_if_liquidated": owed_liq, "liquidation_value": v - owed_liq})
        return r

    rows.append(mark_row(0, start))
    for i in range(1, n):
        d = c["date"].iloc[i]
        if mode == "distributed":
            yld = tri[i] / tri[i - 1] - pr[i] / pr[i - 1]
            div = units * nav_u[i - 1] * (fee[i] / fee[i - 1]) * yld
            if div < 0:
                clipped += -div
            elif div > 0:
                net = div
                if p.s115r and S115R_FROM <= _d(d) < DIVIDENDS_TAXABLE_FROM:
                    s115r_paid += div * S115R_RATE
                    net = div * (1 - S115R_RATE)
                acct.add_dividend(d, net)
                st = stamp_on(d)
                add = net * (1 - st) / nav_u[i]
                stamp_paid += net * st
                book.buy("FUND", d, add, net / add)
                units += add
        if c["mark"].iloc[i] == "close":
            for kind, fy in acct.due(d):
                if kind == "true_up":
                    amt = acct.true_up(d, fy)
                else:
                    amt = acct.instalment(d, fy)
                    if amt > 0:
                        acct.paid_instalment(d, fy, amt)
                if amt > 0:
                    sell = amt / nav_u[i]
                    if sell >= units:
                        raise ValueError(f"{d.date()}: tax {amt:,.0f} exceeds the fund")
                    acct.add(book.sell("FUND", d, sell, nav_u[i],
                                       reason="advance_tax" if kind == "instalment"
                                       else "tax_funding"))
                    units -= sell
                elif amt < 0:
                    st = stamp_on(d)
                    add = -amt * (1 - st) / nav_u[i]
                    stamp_paid += -amt * st
                    book.buy("FUND", d, add, -amt / add)
                    units += add
        rows.append(mark_row(i, d))

    value_end = units * nav_u[-1]
    owed_held = acct.owed(end)
    sale = book.sell("FUND", end, None, nav_u[-1], reason="final_sale")
    unpaid = acct.close_all(end, sale)
    liquidation = value_end - unpaid
    nav = pd.DataFrame(rows)
    if series:
        nav.loc[nav.index[-1], ["tax_owed_realised", "held_value", "tax_owed_if_liquidated",
                                "liquidation_value"]] = [owed_held, value_end - owed_held,
                                                         unpaid, liquidation]
    final = {"mode": mode, "ter": ter, "stamp_duty_on_units": p.fund_stamp,
             "start": str(start.date()), "end": str(end.date()),
             "capital": initial, "pre_tax_end": float(initial * tri[-1] / tri[0]),
             "fund_value_end": value_end,
             "after_tax_end_held": value_end - owed_held, "after_tax_end_liquidated": liquidation,
             "tax_paid_during": acct.tax_paid, "interest_paid": acct.interest_paid,
             "refunds": acct.refunded, "tax_due_at_end": unpaid,
             "s115r_distribution_tax": s115r_paid, "stamp_duty_paid": stamp_paid,
             "negative_dividend_residue_clipped": clipped,
             "hypothetical_before": str(FIRST_NIFTY500_FUND),
             "losses_carried_out": [asdict(l) for l in acct.carried]}
    return nav, pd.DataFrame(acct.rows), final


# ------------------------------------------------------------------ disclosures
def near_long_term(pieces: pd.DataFrame, sessions, within: int = 5,
                   p: Params = Params()) -> dict:
    """Short-term gains on lots that would have turned long-term within `within` sessions.

    Reported, not changed. For each short-term piece with a positive taxable gain,
    n = sessions after the sale up to the first session on which the same sale
    would have been long-term. The tax figures are first order: gain x rate x
    (1 + cess), before set-off and the exemption, at the short-term rate paid and
    at the long-term rate that waiting would have brought.
    """
    sess = pd.DatetimeIndex(sorted(set(pd.to_datetime(list(sessions)))))
    s_arr = sess.to_numpy()
    empty = {"within_sessions": within, "pieces": 0, "gain": 0.0,
             "st_tax_first_order": 0.0, "lt_tax_first_order": 0.0, "by_sessions": {}}
    if pieces is None or pieces.empty:
        return empty
    x = pieces[(~pieces["long_term"].astype(bool)) & (pieces["taxable_gain"] > 0)].copy()
    rows = []
    for r in x.itertuples(index=False):
        lt_from = pd.Timestamp(plus_months(r.buy_date, 12)) + pd.Timedelta(days=1)
        sold = np.datetime64(pd.Timestamp(r.sell_date))
        i0 = int(np.searchsorted(s_arr, sold, side="right"))
        i1 = int(np.searchsorted(s_arr, np.datetime64(lt_from), side="left"))
        if i1 >= len(sess):
            continue
        nsess = i1 - i0 + 1
        if 1 <= nsess <= within:
            st, lt = cg_rates(r.sell_date)
            rows.append({"n": nsess, "gain": r.taxable_gain, "reason": r.reason,
                         "st_tax": r.taxable_gain * st * (1 + p.cess),
                         "lt_tax": r.taxable_gain * lt * (1 + p.cess)})
    if not rows:
        return empty
    t = pd.DataFrame(rows)
    return {"within_sessions": within, "pieces": int(len(t)), "gain": float(t["gain"].sum()),
            "st_tax_first_order": float(t["st_tax"].sum()),
            "lt_tax_first_order": float(t["lt_tax"].sum()),
            "by_sessions": {int(k): int(v) for k, v in t["n"].value_counts().sort_index().items()},
            "by_reason": {str(k): float(v) for k, v in t.groupby("reason")["st_tax"].sum().items()}}


def corporate_action_exposure(events: pd.DataFrame, pieces: pd.DataFrame) -> dict:
    """Sales of lots held across a corporate action the lot book does not model.

    events: symbol, ex_date, kind (e.g. 'demerger', 'amalgamation', 'bonus_non_equity').
    A piece is affected when its lot was bought before the ex-date and sold on or
    after it. Trades are counted as distinct (symbol, sale date) pairs among the
    engine's own sales; the layer's internal sales and the final liquidation are
    counted apart.
    """
    out = {"events_in_window": 0, "events_held_through": 0, "engine_sales_affected": 0,
           "internal_sale_pieces_affected": 0, "final_liquidation_positions_affected": 0,
           "by_kind": {}, "events": []}
    if events is None or events.empty or pieces is None or pieces.empty:
        if events is not None:
            out["events_in_window"] = int(len(events))
        return out
    ps = pieces.copy()
    ps["buy_date"] = pd.to_datetime(ps["buy_date"])
    ps["sell_date"] = pd.to_datetime(ps["sell_date"])
    engine, internal, final = set(), 0, set()
    out["events_in_window"] = int(len(events))
    for e in events.itertuples(index=False):
        ex = pd.Timestamp(e.ex_date)
        hit = ps[(ps["symbol"] == e.symbol) & (ps["buy_date"] < ex) & (ps["sell_date"] >= ex)]
        if hit.empty:
            continue
        out["events_held_through"] += 1
        out["by_kind"][e.kind] = out["by_kind"].get(e.kind, 0) + 1
        eng = hit[~hit["reason"].isin(INTERNAL_REASONS)]
        fin = hit[hit["reason"] == "final_liquidation"]
        engine |= set(zip(eng["symbol"], eng["sell_date"]))
        final |= set(fin["symbol"])
        internal += int(len(hit) - len(eng) - len(fin))
        out["events"].append({"symbol": e.symbol, "ex_date": str(ex.date()), "kind": e.kind,
                              "engine_sales": int(eng[["symbol", "sell_date"]]
                                                  .drop_duplicates().shape[0]),
                              "gain_on_affected_pieces": float(hit["gain"].sum())})
    out["engine_sales_affected"] = len(engine)
    out["internal_sale_pieces_affected"] = internal
    out["final_liquidation_positions_affected"] = len(final)
    return out
