# Strategy v3: pre-registered specification

**Status: approved by the owner on 5 October 2026 and committed here before any v3 code
computed a return.** Written 29 September 2026 and revised 5 October 2026 after two independent
reviews: one for look-ahead and data problems, one for mechanics, read as if building the second
engine from this text alone. The owner approved the rules, every recommended default in section 19,
and the edge hypothesis in section 1 as drafted. No v3 code existed and no return of any v3 rule had
been computed when it was approved; the only data work behind it is archive structure and coverage
(section 21). From here, anything the build turns up goes into the Clarifications section at the
end, with a date and a reason, and nothing may be chosen by looking at a return.

v3 is one rule set. There are no variants, no grid and no selection step. It is
built twice from this text, run once, checked independently, and judged against
v1 over the same dates.

## How to read this

Every rule carries a tag saying where it comes from.

- **OWNER**: decided by the owner on 29 September 2026. This document only
  makes it exact.
- **PLUMBING**: identical to v1 and v2, so that the comparison with v1 is fair.
  Where a plumbing rule is cited by number (v1 "C" plus a number means v1-spec
  Clarification that number; "v2 Clarification ..." means the v2-spec section of
  that name), the cited text governs and is not repeated here.
- **WRITER**: a choice made in writing this spec, where the owner's words leave
  a case open. Each one is listed in section 19, as an open question with a
  default (19.1) or as a choice to confirm (19.2).
- **SOURCE**: a form or a value taken from a cited public source (section 20).

Units. The archive stores every rupee amount in rupees. Checked: TCS's
consolidated revenue for the March 2024 quarter is stored as 6.1237e11, which
is Rs 61,237 crore. One crore is 10^7 rupees, so Rs 25 crore is 2.5e8 in the
archive's units, Rs 12.5 crore is 1.25e8 and Rs 1 crore is 1e7.

Code names refer to the Zen repository at commit 7772f70. The code this spec
names (`pit`, `factors`, `financials`, `engine`, `engine_v2`, `metrics` and the
measurement jobs) is unchanged since commit 0b8319a, which the first draft
cited; the commits in between touched the filings pipeline and the daily brief
only.

---

## 1. Edge hypothesis: DRAFT, for the owner to rewrite in their own words

*The assistant wrote this draft. It is not the owner's thesis until the owner
has rewritten it. A good result with no reason behind it cannot be told apart
from luck, which is why this section comes first.*

**The bet.** Buy companies whose growth is speeding up and whose margins are
widening, at a price that is still reasonable for that growth. Ignore recent
price moves. Hold for years unless the business itself breaks.

**Why it might work in India.**

1. *People anchor on last year.* The usual way to forecast a quarter is to take
   the same quarter a year earlier and add a trend. When growth changes speed,
   that forecast is wrong in a predictable direction, and prices take months to
   adjust. He and Narayanamoorthy (2020) find this for earnings acceleration in
   US data. Akbas, Jiang and Koch (2017) find the same for the trend in
   profitability, and show that analysts' forecasts are slow to reflect it.
   Bernard and Thomas (1989) is the classic evidence that prices react slowly to
   earnings news. None of this evidence is Indian.
2. *Thin coverage (a hypothesis, not tested here).* Outside the largest few
   hundred Indian companies, fewer analysts and funds follow each name, so slow
   reactions could be larger and could last longer.
3. *Constraints on the other side.* Large domestic funds work inside SEBI's
   size categories (large cap is the top 100 companies by market value, mid cap
   the next 150, small cap the rest) and cannot take meaningful positions in
   small, illiquid companies. A Rs 5 lakh book can.
4. *Price discipline.* The P/E ceiling of 60, the PEG rank and the trim above a
   P/E of 85 mean the screen never pays any price for growth, which is where
   growth investing usually goes wrong.

**Who is on the other side.**

- Sellers who project a company's slower past into its future and sell just as
  growth turns.
- Investors and analysts whose forecasts adjust slowly to a change in the
  growth rate.
- Institutions that cannot own a company until it is large and liquid enough,
  and so buy after the re-rating from those who bought before it.
- On the way out, buyers who are willing to pay more than 85 times earnings.

**Why it might fail.**

- Quarterly profit is noisy. Acceleration measured over two quarters can come
  from one-off or seasonal items.
- Most of the published evidence is from the US.
- 2020 to 2026 was a strong period for Indian small and mid caps, which flatters
  any screen that leans towards them.
- The acceleration idea is published and well known, so any edge it had may
  already have been competed away.

---

## 2. Dates, window and information set

- **Review date D** (PLUMBING): the first NSE session on or after 15 Feb,
  1 Jun, 15 Aug and 15 Nov (`pit.decision_date`), the same dates as v1 and v2.
- **First review: 17 February 2020** (OWNER, date fixed by WRITER): the first
  session on or after 15 February 2020. The acceleration, margin-expansion and
  PEG measures and the growth-reversal test need eight consecutive quarters.
  - The archive's first XBRL filing was broadcast on 21 May 2018, so March 2018
    is its first quarter, and only for companies that reported it after that
    date: 193 standalone and 36 consolidated filers have June and September
    2018 but not March 2018 (section 21.2).
  - At every review in 2019 no member of v1's universe had eight quarters
    (`v3_spec/evidence_final/cov_final.csv`, row 2019-11-15).
  - At 17 February 2020, 314 of the 473 members (66%) have a complete
    eight-quarter window in their v3 basis (section 3.3a), 221 pass every v3
    filter, and a cap of 4 per sector could fill 75 slots, so 18 slots can be
    filled from the first review.
  - That coverage is not only the cost of needing eight quarters. Of the names
    that pass every other filter at 17 February 2020, 114 fail the history
    filter: 70 because their first quarter in the archive is later than March
    2018 (mostly June or September 2018: the archive's start, then NSE's
    June 2018 gap), and 44 because a quarter inside their eight is missing from
    the archive. Section 5 ("Archive gaps") and section 21 give the counts at
    every review.
- **Last review: 17 August 2026. End of the run: the open of 18 September
  2026** (PLUMBING): the end of `v1_final` (v2-spec, Clarification to A2).
  That makes 27 reviews.
- **Information set at D** (PLUMBING): filings with `broadcast_dt` strictly
  before D 00:00, prices up to and including the previous session's close, and
  split and bonus factors with ex-date before D (v1 "Dates", C2, C6).
  `period_end` is never used as a time filter; it is used only to measure a
  quarter's age (E5, the 400-day balance-sheet age). Every input is read from
  the frozen copies of section 15.2.
- **Execution** (PLUMBING, with order quantities made exact in section 9.4): at
  the open of D on corporate-action-adjusted prices. An order fills at the open
  of the first session, from D to the 5th market session after D, on which the
  stock trades; if there is none, it is cancelled after that 5th session
  (v1 C10 as `engine.simulate` implements it: `deadline` = D + 5 sessions;
  this is "C10's window" below). Sells go before buys, and buys are scaled
  down pro rata if cash is short (C11). The trade-for-trade rule applies (v2, Clarification to A2): a BZ
  session counts as a trading session for sales and for the 20-session rule,
  but nothing is bought in BZ.
- **NAV clock** (PLUMBING, C16): the open of the first review, every close,
  then the open of the end date. That day's close is never read. Starting
  capital is Rs 5,00,000.
- **Agreement period** (section 15): the reviews from 17 February 2020 to
  15 November 2022 (12 reviews), measured to the open of the 15 February 2023
  review. The code refuses to compute any return after that open unless the
  explicit unlock is passed (`engine.guard`, with the in-sample end set to the
  open of 15 February 2023).

---

## 3. Universe and data conventions

1. **Candidate pool** (PLUMBING). v1's universe at D, rules 1 to 7, exactly as
   `pit.universe` builds it:
   - ordinary equity in EQ or BE, traded in the last 5 sessions;
   - median 60-session turnover of at least Rs 20 lakh;
   - 200 sessions traded in the last 365 days;
   - not a bank, NBFC or insurer (C28, C29);
   - four consecutive quarters known, the latest within 200 days;
   - TTM profit and TTM EBITDA above zero;
   - market cap computable.

   All of v1's clarifications that these rules depend on apply: C1, C4 to C6,
   C22 and C23, C28 to C30, C33, C38 and C39. Lenders stay out because they
   need price-to-book, and the archive has neither NIM nor NPA figures.
2. **Company identity** (PLUMBING, C23, C33). A company keeps one stock id
   across renames. Every output carries both the stock id and the symbol traded
   at D.
3. **v1's basis** (PLUMBING, C4). A company is on its consolidated figures if
   consolidated figures known before D exist for all four of its TTM quarters;
   otherwise on standalone figures. This basis decides v1's universe (F0), the
   results-overdue exit E5 (which measures the latest quarter as v1 rule 5
   does) and nothing else in v3. The share count is C6's, standalone first.

   **3a. v3's basis for its own figures (WRITER, question 10).** Every v3
   figure of a stock at D (F1 to F6, the ranking measures, DOWN0 and DOWN1,
   exits E1 to E4 and the P/E of the valuation trim) uses one basis b*,
   chosen afresh at every review, for every stock, member or holding:
   - L* = the latest quarter index among the stock's known quarters
     (section 4.1) in either basis.
   - The eight-quarter window in a basis is **complete** when each of the
     quarters L* − 7 to L* is a known quarter in that basis that reports
     revenue, P and E (section 4.2).
   - b* = consolidated if its window is complete; otherwise standalone if its
     window is complete; otherwise v1's basis (item 3), with L = that basis's
     latest known quarter, and the stock then fails F5.
   - In the first two cases L = L*.

   Why: quarterly consolidated results became mandatory only with the June 2019
   quarter, so under C4 many companies were on a consolidated series with only
   four to seven quarters and failed the history filter although their
   standalone series had all eight, ending at the same quarter (ITC, HCLTECH,
   ONGC, POWERGRID, IOC and SHREECEM among them). Under C4, names passing F5
   were 270 at 17 August 2020; under b* they are 425. The draft's reason for
   keeping C4, not mixing bases inside one growth rate, does not apply: b*
   never mixes bases, because every figure at a review comes from one basis.
   A holding can switch basis between reviews, typically from standalone to
   consolidated when its consolidated series reaches eight quarters; nothing
   carries over between reviews except U0 (section 9.1), which is in units.
   Universe members whose b* differs from C4's basis: 17 to 171 per review, all
   moved from consolidated to standalone (section 21).
4. **Revisions** (PLUMBING, C38, C39). The income statement for a quarter comes
   from the latest filing broadcast before D that reports at least one
   income-statement figure, after the unit-scale screen (C30a). The EBIT lines
   come from that same filing (`factors.quarter_extras`). The balance sheet is
   chosen separately (section 4.6). The same rules apply to every stock,
   member or holding. How unevenly the archive holds revisions is disclosed in
   section 16.5.
5. **Market cap** (PLUMBING for universe members; WRITER where the plumbing
   leaves a case open). For every stock at D, member or holding:
   **market cap = C × N**.
   - **C, the close**: the stock's latest close strictly before D in the EQ, BE
     or BZ series (tables `prices` and `prices_other`), within the snapshot's
     420-day price window (`pit.PRICE_LOOKBACK_DAYS`). If two series report a
     close on that date, the EQ or BE close is used. C is put on D's share
     basis by multiplying it by `pit.cumulative_factor`: the product of the
     split and bonus factors with an ex-date after that close's date and
     before D.
   - **N, the share count**: `pit.fundamentals`' `shares` at D (C6 and C30b:
     the latest quarter's implied share count, standalone first, screened
     against the recent median, carried to D's share basis).
   - No close in the window, or N missing or not above zero: no market cap,
     so no P/E.

   For v1's universe members this is exactly `pit.universe`'s market cap at
   every review in the window: no member's latest close was a BZ close, and no
   split or bonus fell between a member's latest close and D (section 21). It
   adds a market cap for holdings that have left the universe, for which `pit`
   computes none. Universe rule 1 (traded in the last 5 sessions) and the
   20-session exit keep the close recent in practice. `eligibility.csv`
   records C, its date and series, N and N's source (`pit`'s `shares_fix`).
6. **Sector** (v2 item 7 and its clarifications; the grouping of unlabelled
   and legacy-labelled names WRITER, question 14).
   - The label is the `sector` column of `data/reference/industry_nse.parquet`,
     mapped to stock ids by `engine_v2.nse_sectors`. These are today's labels
     applied to the past, as in v2.
   - The **sector key** is the label compared without regard to case (Python
     `str.casefold`), so `FINANCIAL SERVICES` and `Financial Services`, both in
     NSE's current scheme, are one sector.
   - A stock with no label, or with a label from NSE's legacy scheme
     (`label_scheme` not equal to `current`, such as AUTOMOBILE, TEXTILES,
     METALS, CONSUMER GOODS or IT), has the key **`unclassified`**, which is one
     sector for the cap like any other.
   - Why: the label file was fetched on 23 September 2026. Every
     legacy-labelled eligible name (1 to 3 at 7 of the first 9 reviews, such as
     GSKCONS, MEGH and SRIPIPES) had stopped trading before the fetch, and so
     had some unlabelled ones. The label's scheme or its absence therefore
     encodes a later delisting, and under v2's convention each such name was a
     sector of its own and escaped the cap. Grouping them removes that link.
     It lowers the most slots the cap allows at one review (85 rather than 87
     at 15 November 2021) and never below 65.
7. **Costs, dividends and corporate actions** (PLUMBING).
   - Costs are 0.20% of traded value per side.
   - Cash dividends are credited on the ex-date to positions held at the
     previous close, and held as cash at zero interest (C13, C24).
   - Splits and bonuses are adjusted.
   - Demergers are not adjusted (C20, C27).
   - Share-swap mergers leave through the no-trade exit (C31).
8. **Forced exit** (PLUMBING, C15, C34, v2 Clarification to A2). A holding
   with no trade for 20 consecutive market sessions is sold at its last close,
   with the per-side cost. BZ trades count as trades.
9. **Static inputs** (PLUMBING, as v1 C8). The sector labels
   (`industry_nse.parquet`), `pit.StaticLabels` and today's lender flags
   (`nse_list.json`) are static inputs. A stock's sector key is fixed for the
   whole run, so a holding never changes sector. They are passed explicitly
   and are outside the leak test's scope (section 15.5).

---

## 4. Definitions

### 4.1 Quarters

For stock i at review D, with basis b* chosen as in section 3.3a:

- A **known quarter** is a (stock, basis, period_end) row of `financials`,
  chosen as in section 3.4, that survives the scale screen and reports an
  income statement (C39).
- The **quarter index** is q = 4 × year + (month − 1) div 3, taken from
  `period_end`.
- **L** is the latest quarter of section 3.3a. **Q_k** is quarter L − k in
  basis b*, for k = 0 to 8. Q_8 is used only by DOWN1 (section 4.9).
- **The run** is the unbroken run of known quarters ending at L in basis b*
  (the same count `consec_quarters` in `pit.fundamentals` makes for C4's
  basis, C5).

### 4.2 Figures

| Symbol | Meaning | Archive column (table `financials`) |
|---|---|---|
| R_k | Revenue from operations of Q_k | `revenue` (tag RevenueFromOperations) |
| P_k | **Profit before exceptional items**: net profit from continuing operations after tax (the reported net profit where a filing has no continuing-operations line), less exceptional items. The pre-tax exceptional amount is taken off with no tax adjustment. This is v1's "normalised profit". | `profit_normalised` |
| E_k | Operating EBITDA: profit before exceptional items and tax, plus finance costs and depreciation, less other income | `ebitda` |
| PBE_k | Profit before exceptional items and tax; where that line is missing, profit before tax less exceptional items (a missing exceptional item counts as 0) | `pbt_before_exceptional`, `pbt`, `exceptional_items` |
| FC_k | Finance costs | `finance_costs` |
| EBIT_k | PBE_k + FC_k. A quarter with no finance-costs line has no EBIT. This is `factors.ttm_ebit`'s per-quarter definition, applied in basis b*. | computed |
| Equity | Total equity on the chosen balance sheet | `equity` (tag Equity) |
| Debt | Current plus non-current borrowings; 0 when the balance sheet has no borrowings line | `debt_total` = `debt_long` + `debt_short` |

Every figure is in rupees. Lease liabilities are not borrowings in the archive,
so debt here leaves them out.

**What P contains for a consolidated filer** (PLUMBING, disclosed in
section 16.9; question 11). `profit_continuing` is the continuing-operations
line of NSE's format. It **includes** the profit attributable to minority
(non-controlling) interests and **excludes** the share of profit or loss of
associates and joint ventures, which the format reports after that line. So P
is not exactly the profit attributable to the company's shareholders, and the
P/E, the Rs 25 crore floor, PEG and the trim are measured on it as v1 and v2
measured their P/E. `eligibility.csv` also records each consolidated stock's
TTM profit and P/E on an estimate of owners' profit (section 13), as a
diagnostic that no rule uses.

**"Profit before exceptional items" is after tax.** Throughout v3 it means P
above: net profit with exceptional items taken out. It is the profit in the
P/E, and the one the owner's Rs 25 crore floor applies to. PBE is used only
inside EBIT. Question 9 asks the owner to confirm this reading.

### 4.3 Trailing twelve months

- **TTM_X** = X_0 + X_1 + X_2 + X_3, where X is one of the figures above.
- **TTM4_X** = X_4 + X_5 + X_6 + X_7: the same sum four quarters earlier.
- **TTM_P(L − j)** = P_j + P_(j+1) + P_(j+2) + P_(j+3): TTM profit as it stood
  at quarter L − j. So TTM_P(L) = TTM_P and TTM_P(L − 4) = TTM4_P.

A sum is missing unless every quarter in it is a known quarter in basis b* that
reports X. **TTM profit** is TTM_P.

### 4.4 P/E (OWNER)

**P/E = market cap / TTM_P**, with the market cap of section 3.5 (the close
before D). It is defined only when TTM_P is above zero and the market cap
exists. It is computed the same way for universe members and for holdings.

### 4.5 Interest coverage (OWNER; the write-back rule WRITER)

Over the four TTM quarters Q_0 to Q_3 in basis b*:

- **TTM_EBIT** = EBIT_0 + EBIT_1 + EBIT_2 + EBIT_3. Missing unless all four
  exist.
- **FC⁺** = max(FC_0, 0) + max(FC_1, 0) + max(FC_2, 0) + max(FC_3, 0): the
  positive finance costs. Missing if any FC_k is missing.
- **Write-back**: at least one of FC_0 to FC_3 is below −Rs 1 crore (−1e7).
- Four states, in this order:
  - *unknown*: TTM_EBIT or FC⁺ is missing;
  - *write-back*: a write-back, whatever FC⁺ is;
  - *no finance costs*: FC⁺ = 0;
  - otherwise **ICR = TTM_EBIT / FC⁺**.

Why. A negative finance cost is almost always interest written back on a debt
settlement, not the absence of debt. ISMTLTD at 15 February 2023 had TTM
finance costs of −Rs 170 crore against TTM profit of Rs 114 crore; JAYNECOIND
passed the draft's test at 16 August 2022 with an ICR of 2.52 only because one
quarter's finance costs were −Rs 273 crore. The write-back is not tagged as
exceptional, so it sits inside P, inflating F1 and lowering the P/E. EBIT is
not affected, because EBIT_k = PBE_k + FC_k takes it back out; only the
denominator was distorted, which the positive-only FC⁺ removes. A quarter
between −Rs 1 crore and 0 counts as zero: amounts that small are
reclassifications, not settlements. Counts: 2 to 11 universe members per
review have a write-back, and the positive-only denominator changed no other
F2 decision (section 21).

### 4.6 Debt to equity (OWNER)

- **The balance sheet**: the one `factors.latest_balance_sheets` returns at D in
  basis b* (v2, Clarifications to A5 and after the in-sample comparison). That
  is the latest period known before D in any filing, including a filing with no
  income statement, then that period's latest revision, with both unit-scale
  screens applied.
- **Usable**: its `period_end` is no more than 400 days before D.
- **D/E = Debt / Equity**.
- A usable balance sheet with equity of zero or less **fails** every D/E test
  (WRITER): negative equity is worse than any debt ratio. This differs from
  `factors.signals`, which treats such a balance sheet as unusable. So D/E is
  computed from `factors.latest_balance_sheets`' row directly, not from
  `signals`' capital figure.
- A company whose balance sheet exists only in the other basis has no usable
  balance sheet.

### 4.7 One growth function for every growth rate (WRITER, the cap is SOURCE)

Every growth rate in v3 is:

    G(new, old) = (new − old) / max(old, B)

where B = **Rs 12.5 crore for a two-quarter sum** and **Rs 25 crore for a
four-quarter sum**, for revenue and profit alike.

**Why.** When the old value is at least B, G is ordinary growth,
new / old − 1. A base below the owner's own size floor (Rs 25 crore a year, so
Rs 12.5 crore for half a year) is too small to give a meaningful growth rate:
Rs 1 crore rising to Rs 30 crore reads as 2,900%. The floor stops such tiny
bases from pushing a stock to the top of a rank. It also gives a finite,
correctly ordered value when the old figure is zero or negative, for example a
return to profit, so no growth rate is ever missing or has the wrong sign. The
floor ties to the owner's parameter. It is not fitted to anything.

**The cap, in PEG only (SOURCE).** In the PEG measure (section 4.8), growth is
capped at **50% a year**, for two reasons:

- Lynch (1989), as practitioners usually summarise him (for example Validea's
  write-up of his method), treated long-run earnings growth of about 20 to 25%
  a year as the sweet spot and growth above 50% as unsustainable.
- Chan, Karceski and Lakonishok (2003) find that high past growth persists no
  more than chance would predict, so past growth beyond such a level carries
  little information about future growth.

How often the cap binds among eligible names (section 21):

- 21% to 39% at most reviews (17% at the first);
- 49% to 63% from August 2021 to June 2022, when TTM profits were being
  compared with the pandemic year.

Those comparisons are exactly the kind of low-base growth that says little
about the future. Among capped names, PEG ranks by P/E alone.

There is no cap in the acceleration measure. There growth enters only through
its rank, and a cap would erase any difference in acceleration among companies
already growing faster than the cap.

### 4.8 Ranking measures (OWNER, made exact by WRITER)

For each eligible stock:

- **Revenue acceleration**
  A_R = G(R_0 + R_1, R_4 + R_5) − G(R_2 + R_3, R_6 + R_7):
  the growth of the latest two quarters over the same two a year earlier, minus
  the same measure for the two quarters before those.
- **Profit acceleration**
  A_P = G(P_0 + P_1, P_4 + P_5) − G(P_2 + P_3, P_6 + P_7).
- **Margin expansion**
  M = TTM_E / TTM_R − TTM4_E / TTM4_R, in margin points (0.02 means two
  percentage points). Operating margin is operating EBITDA over revenue.
- **PEG.** Let g = G(TTM_P, TTM4_P), the TTM profit growth, with the Rs 25
  crore floor.
  - If g > 0: PEG = P/E / (100 × min(g, 0.50)). Growth enters in percent,
    capped at 50.
  - If g ≤ 0: the stock has **non-positive growth** and ranks last on PEG.

### 4.9 Growth-reversal flags (OWNER's exit made exact; the quarter reading WRITER, question 5)

In basis b* at D:

- **DOWN0** = TTM_P(L) < TTM_P(L − 4), that is
  P_0 + P_1 + P_2 + P_3 < P_4 + P_5 + P_6 + P_7. Known only when P_0 to P_7 are
  all known.
- **DOWN1** = TTM_P(L − 1) < TTM_P(L − 5), that is
  P_1 + P_2 + P_3 + P_4 < P_5 + P_6 + P_7 + P_8. Known only when P_1 to P_8
  are all known (quarters L − 1 to L − 8 in basis b*).
- An unknown flag counts as false.
- Both are worked out at every review for every stock with the data, held or
  not, from what is known at D.

**Why on quarters.** The owner's rule is "TTM profit below its level four
quarters earlier at two consecutive reviews". The intent is two consecutive
quarterly results. When the latest quarter advances by one between reviews,
which is the norm, DOWN1 at D is the previous review's DOWN0, recomputed on the
latest revisions, so the two readings agree. When a review sees no new
quarter, counting reviews would count one result twice: at 1 June 2020 (after
SEBI relaxed the March 2020 results deadline) 284 of 445 members had the same
latest quarter as at 17 February 2020, and at 1 June 2021, 271 of 767 did. The
quarter reading also needs no state carried from earlier reviews.

At 17 February 2020 DOWN1 is unknown for every member, because it would need
the December 2017 quarter, before the archive starts. So F6 blocks nothing at
the first review.

### 4.10 Arithmetic (WRITER, so that two engines agree exactly)

- Every figure is an IEEE double (float64).
- Every sum of quarterly figures is evaluated from the left in increasing k:
  X_0 + X_1 + X_2 + X_3 means ((X_0 + X_1) + X_2) + X_3, and likewise for
  TTM4_X, TTM_P(L − j), FC⁺ and TTM_EBIT. Two-quarter sums are X_0 + X_1,
  X_2 + X_3, X_4 + X_5 and X_6 + X_7.
- Every formula is evaluated as written, left to right within each level of
  brackets.
- Thresholds and the ordering of measures compare those doubles exactly, with
  no rounding first.
- The ranking (section 6) sorts only on sums of whole and half ranks, which
  are exact.

---

## 5. Eligibility: which stocks may be bought at D

A stock is **eligible** at D when all of the following hold. Every figure is in
basis b* (section 3.3a).

| Filter | Rule | Tag |
|---|---|---|
| F0 | In v1's universe at D (rules 1 to 7) | PLUMBING |
| F1 | TTM_P of at least Rs 25 crore | OWNER |
| F2 | Interest coverage known, no write-back, and either no finance costs (FC⁺ = 0) or ICR ≥ 2.5 (section 4.5) | OWNER (write-back rule WRITER) |
| F3 | From the review of **15 November 2022** onward: if a usable balance sheet exists, equity above zero and D/E strictly below 1.2. A stock with no usable balance sheet passes. Not applied before that review. | OWNER (switch date and per-stock reading WRITER, question 2) |
| F4 | 0 < P/E ≤ 60 | OWNER |
| F5 | History: the eight-quarter window in basis b* is complete (so R, P and E are reported for each of Q_0 to Q_7), TTM_R > 0 and TTM4_R > 0 | OWNER (made exact by WRITER) |
| F6 | Not in growth reversal: not both DOWN0 and DOWN1 | WRITER (consistency, question 5) |

Notes on these filters.

- **F3.** The owner's rule is D/E below 1.2 "whenever a balance sheet is known
  point in time (from about Nov 2022), not applied before". Balance sheets
  start with the September 2022 half-year. At the 16 August 2022 review, 7 of
  v1's 859 members had a usable balance sheet; at 15 November 2022, 733 of 828
  (88.5%); from 15 February 2023 onward, all or all but one.
  - The default reads the owner's words literally: from 15 November 2022, D/E
    is tested wherever a usable balance sheet is known, and a stock without one
    passes F3.
  - The alternative (question 2) makes a missing balance sheet fail F3 from
    15 November 2022, as v2 did. The two differ only at 15 November 2022:
    364 eligible names under the default, 329 under the alternative.
  - Interest coverage (F2) is the debt test at every review.
- **Why F6 exists.** The screen does not buy what its own exit rules would sell
  at the same review. F0 to F4 are already at least as strict as exits E1 to
  E3, E5 (through v1's rule 5) and the valuation trim, so F6, for E4, is the
  only extra condition this principle adds. It blocks 0 to 164 names per
  review that pass everything else (section 21), most of them in 2020 and 2021
  when TTM profits fell with the pandemic.
- **Basis and F5.** Under v3's basis (section 3.3a) a stock fails F5 only when
  neither basis has eight complete quarters ending at its latest quarter. Under
  the draft's C4 basis, a further 99 to 115 names per review from August 2020
  to February 2021, and 72 at June 2021, failed F5 only because they were on a
  young consolidated series while their standalone series had all eight
  quarters.
- **Archive gaps.** The archive is missing whole quarters for many companies
  (section 21.2). The June 2018, June 2019 and June 2022 quarters are short at
  NSE's own source: on 23 September 2026 both NSE's date-range listing and its
  per-company query returned no filing for the missing companies
  (`jobs/validate_financials.py`, `research/strategy/v1-verification.md`).
  Smaller shortfalls remain in other quarters, to be refilled where NSE has
  the filings before the build (section 15.2). Because F5 needs an unbroken
  eight-quarter window, one missing quarter keeps a company out of the
  eligible list for up to seven reviews, against three for v1's four-quarter
  rule. Among names that pass every other filter, F5 fails because of a
  missing quarter for 3 to 132 per review: 44 to 62 from 17 February 2020 to
  1 June 2021, 47 to 51 at February and June 2023, and 99 to 132 from August
  2023 to June 2024, mostly the June 2022 gap. From November 2022 to June
  2023, 133 to 136 liquid non-lender stocks have no TTM figures at all because
  of that gap and drop out of v1's universe, as they did for v1. What a gap
  does to a holding is in section 12.
- **Holdings.** A holding can be eligible or not. Eligibility matters only for
  buying and for the diagnostics in section 14.

---

## 6. Ranking (OWNER; exact form WRITER; tie rules PLUMBING)

The owner's ranking has three equal parts, each a percentile rank within the
eligible list, and the acceleration part averages the revenue and profit
percentiles. This section computes exactly that order using whole and half
ranks only. The percentile form produces genuine ties: 31 to 177 eligible
names per review share a score under exact arithmetic. Floating-point
evaluation of the percentiles breaks those ties arbitrarily: writing the same
formula in a different order changed the order from position 5 down, and
changed one name in a fresh 18-name fill at 17 August 2020
(`v3_spec/review_mechanics/rank_check.csv`).

Let N be the number of eligible stocks at D.

1. **Rank from the worst.** For a measure X, r(X) is the stock's average rank
   counted from the worst value: the worst gets 1, the best N, and tied values
   share the average of their positions, a whole or half number. This is v1
   C9's average rank (`scipy.stats.rankdata(..., method="average")` on values
   oriented so that larger is better).
2. **Part 1, acceleration.** R1 = the average rank, from the worst, of the sum
   r(A_R) + r(A_P).
3. **Part 2, margin expansion.** R2 = r(M).
4. **Part 3, growth at a reasonable price.** Let n0 be the number of eligible
   stocks with non-positive growth (g ≤ 0). Each of them gets R3 = (n0 + 1) / 2:
   they tie at the bottom. Each stock with positive growth gets R3 = n0 + its
   average rank, from the worst, of PEG among the positive-growth stocks, where
   the highest PEG is the worst.
5. **Order.** Sort by T = R1 + R2 + R3, highest first. Ties go to the symbol
   traded at D in ascending order (Python's default string order, by code
   point), then to the stock id ascending (v1 C23, v2 `score_v2`). **Rank** 1
   is the first. Both engines sort on T, never on a percentile.
6. **Reported, never sorted on:** pct(X) = r(X) / N for each measure,
   S1 = R1 / N, S2 = R2 / N, S3 = R3 / N, and the owner's score
   S = T / (3N) = (S1 + S2 + S3) / 3.
7. **No missing values.** F4 and F5 guarantee that every measure exists for
   every eligible stock. If one is missing anyway, the engine stops with an
   error. It never fills in a value.

Why this is the owner's rule and not a new one: S1, the percentile of
(pct(A_R) + pct(A_P)) / 2, orders stocks exactly as r(A_R) + r(A_P) does, and
S orders them exactly as T does. Every r and R is a multiple of one half below
10,000, so every sum above is exact in float64.

The ranking decides only the order in which empty slots are filled. **A holding
is never sold because its rank fell** (OWNER).

---

## 7. Portfolio rules (OWNER unless tagged)

- **18 slots.** The owner's range is 15 to 18. In the backtest every empty slot
  is filled whenever enough stocks qualify. If fewer qualify, the unfilled slots
  stay in cash.
- **Size of a new buy.** 1/18 of the portfolio's value at the open of the
  review (NAV_open), or the cash left if that is less. The 0.20% cost is paid
  on top, as in v1.
  - **Minimum (WRITER, question 1):** a buy smaller than half a slot
    (NAV_open / 36) is not placed. Filling then stops for that review, so no
    slot is taken for years by a position too small to matter.
- **Sector cap.** At most 4 holdings per sector key (section 3.6), counting the
  holdings kept at the review and the buys accepted at it. `unclassified`
  counts like any other sector.
- **No resizing.** Weights drift between reviews. Holdings are sold down only
  by the valuation trim and the 10% cap (section 9), and sold in full only by
  the exits (section 8) and a valuation trim to zero.
- **Cash.** Cash from sales, trims and dividends stays in cash, earning
  nothing, until a review can spend it on an empty slot. It is never used to
  top up existing holdings (question 7).
- **Leaving v1's universe is not an exit.** A holding whose turnover falls
  below Rs 20 lakh, for example, is kept. v1 and v2 sold such holdings; v3
  holds unless one of its own exit rules fires. Its figures are computed by the
  same rules as a member's (sections 3.3a to 3.5).
- **Fractional units.** Positions are held in fractional adjusted units, as in
  v1 and v2. The live portfolio rounds to whole shares.
- **Edge cases, stated so both engines treat them alike.**
  - A holding given a full-sale order at D is not a kept holding: it does not
    count towards the 18 slots or the sector cap at D. If the order never fills
    (no trade within the window of section 2), the stock stays held, so until
    the next review the book can hold 19 or more names and a sector 5 or more.
    At the next review it is tested again like any holding.
  - A valuation trim can leave a small residual (at a P/E of 120, 17.6% of U0)
    that occupies a slot until the P/E reaches 127.5 or an exit fires
    (question 16).

---

## 8. Exits: checked only at reviews, all written in advance (OWNER, E5 WRITER)

At each review, each stock held at D's open (units above zero before any trade
at D) is tested against the rules below, in basis b*. If any applies, **the
whole holding is sold**. Every rule that applies is recorded; the first in this
order is the reported reason.

| Code | Exit | Exact condition at D |
|---|---|---|
| E1 | Loss | TTM_P ≤ 0 |
| E2 | Interest coverage | FC⁺ > 0 and TTM_EBIT / FC⁺ < 2.5, both known (section 4.5; a write-back does not by itself trigger E2) |
| E3 | Debt | From 15 Nov 2022: a usable balance sheet with equity ≤ 0 or D/E > 1.2 |
| E4 | Growth reversal | DOWN0 and DOWN1 are both true, **and** the stock was held at the open of the previous review |
| E5 | Results overdue | The latest known quarter in v1's basis, measured as v1 rule 5 measures it (`pit.fundamentals`' `latest_period_end`), has a `period_end` more than 200 days before D, or the stock has no known quarter at all |

- **Between reviews** (PLUMBING): the 20-session no-trade exit (section 3.8)
  applies every day. It covers delisting and suspension.
- **Why E4 needs the stock held at the previous review's open.** A stock bought
  at a review is not a holding when the exits are checked at that review, so
  E4 can first fire at the second review after the purchase. F6 means a stock
  is never bought with both flags true. A company bought at an inflexion, when
  its TTM profit may still be below last year's, therefore has two quarterly
  results to show the turn.
- **Why E5 (WRITER, question 3).** SEBI requires quarterly results within 45
  days (60 for the March quarter), so a latest quarter more than 200 days old
  means at least two missed deadlines. Without E5, a company that stopped
  reporting would be held indefinitely, because no exit test could be computed
  for it.
- **Where buy and sell limits differ** (OWNER).
  - Interest coverage uses one boundary: a purchase needs ICR ≥ 2.5, and a
    sale follows ICR < 2.5.
  - D/E does not: a purchase needs D/E < 1.2, but a sale follows only
    D/E > 1.2, so exactly 1.2 blocks a purchase without forcing a sale.
  - Valuation has a wide band on purpose: a purchase needs P/E ≤ 60, and
    selling starts only above 85.
  - A write-back blocks a purchase (F2) but is not an exit; E2 is then tested
    on the positive finance costs alone.
- **Missing inputs** (section 12). An exit whose inputs are unknown at D is
  not triggered. A missing figure is not evidence that the business broke, and
  E5 covers the case where a company stops reporting. Holdings with an exit
  left untested are listed at every review (section 13).
- **Re-buying** (WRITER). A stock sold at D cannot be bought back at D, because
  it was held at D's open. It may be bought at a later review if it is eligible
  and ranked high enough.

---

## 9. Valuation trim, the 10% cap and the orders they place

### 9.1 Valuation trim (OWNER, made exact by WRITER)

The owner's rule: buy only at P/E ≤ 60. If a holding's P/E goes above 85 at a
review, cut the holding by 2% for every 1% by which the P/E is above 85.

At every review, for every holding h (units above zero at D's open), **before
the exits are tested** (section 10, step 4):

1. P/E_h as in section 4.4. When P/E_h > 85, **excess_h = P/E_h / 85 − 1**.
2. **Reference units U0_h**, in adjusted units (which splits and bonuses do not
   change):
   - P/E_h known and above 85, and U0_h not set: **set** U0_h = the units held
     at D's open, before any trade at D;
   - P/E_h known and at or below 85: **clear** U0_h;
   - P/E_h unknown (no market cap, or TTM_P not above zero or missing): keep
     U0_h as it is, set or not;
   - U0_h belongs to the holding episode. It is cleared when the units reach
     zero by any route (an exit, a V sale, the 20-session exit), and a stock
     bought again later starts with none.
3. If h gets a full-sale order under section 8, nothing more in this section
   applies to it at D.
4. If U0_h is set and P/E_h is known and above 85, the **valuation target** is
   u_V = max(0, 1 − 2 × excess_h) × U0_h units.
   - If u_V = 0 (P/E_h ≥ 127.5), the whole holding is sold, with reason **V**,
     and its slot is freed.
   - Otherwise, if the units at D's open exceed u_V, the holding is sold down
     to u_V units.
   - **Nothing is ever bought back because the P/E fell.**
5. If P/E_h is unknown, there is no valuation trim at D.

**Why units, not weight.** The draft recorded W0 as a weight, so its target
moved with the rest of the book: a holding whose earnings and price both rose
20% at an unchanged P/E would have been sold again, and so would a flat holding
when the rest of the book fell. That is selling on price and weight, which the
purely fundamental rule forbids. In units, the holding after valuation trims is
(1 − 2 × the highest excess since the breach) × U0, whatever prices did, so the
trim acts again only if the P/E rises further. It matches the owner's example
exactly: at a P/E of 89.25 (5% above 85) the target is 0.90 × U0, so 10% of
the holding is sold; at 127.5 or above, everything is sold.

### 9.2 The 10% cap (OWNER)

After section 9.1, for every holding without a full-sale order:

- the **cap** is u_C = 0.10 × NAV_open / px_now units (px_now as in section 10,
  step 1), so that no holding is worth more than 10% of NAV_open;
- the **target** is u* = min(u_V, u_C) where u_V exists, else u_C;
- if the units at D's open exceed u*, the holding is sold down to u* units.

The cap is checked only at reviews, and it is the only rule based on weight.
There is no minimum trade size for a trim.

### 9.3 Worked example, matching the owner's

A holding bought earlier. Costs are 0.20% of the amount sold and are left out.

| Review | Units at open | Price (Rs) | Value (Rs) | NAV_open (Rs) | Weight | P/E | excess | U0 after the update | Valuation target (units) | Action |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 700 | 100 | 70,000 | 10,00,000 | 7.00% | 89.25 | 5% | set: 700 | 0.90 × 700 = 630 | sell 70 units (Rs 7,000): **10% of the holding**, the owner's example |
| 2 | 630 | 120 | 75,600 | 10,20,000 | 7.41% | 89.25 | 5% | 700 | 630 | none: earnings and price both rose 20%, the P/E did not change |
| 3 | 630 | 140 | 88,200 | 11,00,000 | 8.02% | 102.0 | 20% | 700 | 0.60 × 700 = 420 | sell 210 units, a third of the holding |
| 4 | 420 | 110 | 46,200 | 11,00,000 | 4.20% | 80.0 | none | cleared | none | none: never bought back |
| 5 | 420 | 130 | 54,600 | 11,20,000 | 4.88% | 93.5 | 10% | set: 420 | 0.80 × 420 = 336 | sell 84 units, 20% of the holding |
| 6 | 336 | any | any | any | any | ≥ 127.5 | ≥ 50% | 420 | 0 | sell everything; slot freed (reason V) |

In every row the 10% cap is above the valuation target (at review 3 it is
0.10 × 11,00,000 / 140 = 785.7 units), so the valuation target binds.

If a holding is also above 10%: at a 12% weight and a P/E of 89.25 with no U0
yet, U0 is set to the units held; the valuation target 0.90 × U0 is worth 10.8%
of NAV_open and the cap 10%, so the holding is sold down to the cap, 0.833 × U0
units. At the next review with the same P/E it holds fewer units than the
valuation target, so only the cap can sell again, and only if the weight is
back above 10%.

### 9.4 Orders (PLUMBING execution; the trim quantities WRITER)

v1 C10 says a delayed order keeps the rupee target set at D, and
`engine.simulate` refills that target at the fill session's open. For a trim
that can turn into a buy: a holding worth Rs 70,000 at its last close with a
target of Rs 63,000, which next opens 12% lower, would be worth Rs 61,600 at
the open, and the engine would buy Rs 1,400. v3 therefore fixes each order's
quantity at D:

- **Full sale** (E1 to E5, V): sell every unit held, at the open of the first
  session in C10's window on which the stock trades (EQ, BE or BZ).
- **Trim** (valuation or cap): a sell-only order for n = units at D's open − u*
  adjusted units, fixed at D. At the first session in the window on which the
  stock trades (EQ, BE or BZ), sell min(n, units held then) at that open. It is
  never a buy.
- **Buy**: a rupee amount V fixed at D (section 10, step 10), filled at the
  open of the first session in the window on which the stock trades in EQ or
  BE; units bought = V / that open. If cash at that session is short, buys
  filling there are scaled down pro rata (C11), and a scaled-down buy is final:
  it is never topped up.
- An order not filled within the window is cancelled and recorded. A cancelled
  buy leaves its slot empty until the next review; a cancelled sale leaves the
  stock held.

---

## 10. The review, step by step (exact order)

At each review date D:

0. **Dividends.** Credit dividends with ex-date D to positions held at the
   previous close (PLUMBING; the engine's first step each session).
1. **Value the book.** For every holding, px_now = its adjusted open if it
   trades at D (EQ, BE or BZ), else its last adjusted close.
   NAV_open = cash + Σ units × px_now. Record NAV_open (`rebalance_open.csv`).
2. **Cancel pending orders.** Every order still pending is cancelled and
   recorded. This is a safeguard and cannot fire: orders expire within 6
   sessions, and reviews are more than 50 sessions apart.
3. **Information set.** Build the point-in-time snapshot at D. Compute v1's
   universe; b*, L and every figure for every universe member and every
   holding; F0 to F6 for every member; and DOWN0 and DOWN1 for every stock with
   the data.
4. **Reference units.** Update U0 for every holding (section 9.1, step 2).
5. **Exits.** Test every holding against E1 to E5 (section 8). Each holding
   that fails gets a full-sale order.
6. **Valuation trim.** For every holding without a full-sale order, compute
   u_V (section 9.1, step 4). A holding with u_V = 0 gets a full-sale order
   (reason V).
7. **10% cap.** For every holding still without a full-sale order, compute u_C
   and u* and place a trim order for any holding with more units than u*
   (section 9.2).
8. **Empty slots.** The kept holdings are those held at D's open that did not
   get a full-sale order at D. Empty slots = max(0, 18 − kept holdings).
9. **Candidates.** The eligible list at D in rank order, leaving out every
   stock held at D's open, whether kept, trimmed or being sold.
10. **Budget.** B = cash after step 0, plus the expected proceeds of every sell
    order placed at D: units to sell × px_now × (1 − 0.002).
11. **Fill.** Go down the candidate list from rank 1 until the empty slots are
    filled or the list runs out.
    - Skip a candidate if its sector key already has 4 names among the kept
      holdings and the buys accepted so far.
    - For each accepted candidate, V = min(NAV_open / 18, B_left / 1.002). If
      V is below NAV_open / 36, stop filling. Otherwise place a buy order of V
      rupees and set B_left = B_left − V × 1.002.
    - Every later candidate would get the same V, which is why filling stops
      at the first V below half a slot.
    - At the first review costs come out of the same Rs 5 lakh, so the 18th
      buy is Rs 26,780, slightly under 1/18 and above the half-slot minimum of
      Rs 13,889.
12. **Execute** (section 9.4): at each session, sales fill first at the open,
    then buys.

What is recorded at each review is listed in section 13.

## 11. Between reviews (PLUMBING)

Every session:

1. credit dividends;
2. fill pending orders (section 9.4);
3. apply the 20-session no-trade exit;
4. mark the book at the close.

A slot freed by a forced exit stays empty until the next review. At the open
of the end date the book is marked, nothing trades, and any pending orders are
listed in `metrics.json`.

---

## 12. What happens when an input is missing at a review

| Missing input at D | For a candidate (buying) | For a holding (exit or trim) |
|---|---|---|
| TTM profit (fewer than four consecutive known quarters, or a missing profit figure) | Not eligible (F0 rule 6, F1) | E1 not tested, and no P/E, so no valuation trim. E5 fires if the latest quarter is more than 200 days old |
| The eight-quarter window or any of its inputs | Not eligible (F5) | DOWN0 or DOWN1 unknown, so it counts as false and E4 cannot fire |
| TTM EBIT or a finance-costs line | Not eligible (F2) | E2 not tested |
| A usable balance sheet (from 15 Nov 2022) | Passes F3 (default; question 2) | E3 not tested |
| Market cap (no close in the window, or no usable share count) | Not eligible (F0 rule 7, F4) | No valuation trim; U0 kept; the 10% cap still applies, since it uses value, not P/E |
| A whole quarter missing from the archive (section 5, "Archive gaps") | Not eligible while the quarter is inside Q_0 to Q_7: up to seven reviews | Inside Q_0 to Q_3, TTM figures are unknown, so E1, E2 and the valuation trim are not tested, for up to three reviews; DOWN0 and DOWN1 stay unknown for up to eight. E5 does not fire, because the latest quarter is recent. Every such holding is listed in `holdings.csv` and counted in `decisions.csv` |
| Sector label | Sector key `unclassified` | Same |
| Price at D's open | The order waits within the window | Valued at the last close; the order waits within the window |

No missing input is ever filled with a guess, a median or a neutral score.

---

## 13. Records each engine writes (the agreed format)

In the v2 required-outputs format, so that `jobs/compare_runs.py`, extended for
the files below, can compare the two engines:

| File | Key | Contents |
|---|---|---|
| `universe.csv` | (D, symbol) | v1's universe members |
| `eligibility.csv` | (D, symbol) | Every v1-universe member and every holding: C4 basis, b*, L, the run, missing quarters among Q_0 to Q_8 in b* (as YYYY-MM), TTM_P, the close C with its date and series, N with its source (`shares_fix`), market cap, P/E, TTM_EBIT, FC⁺, write-back flag, ICR state and value, balance-sheet period, D/E, DOWN0, DOWN1, each filter's pass flag F0 to F6, eligible; and two diagnostics no rule uses: TTM owners' profit and P/E on it (consolidated basis only: P_k × EPS_k × N_k / `profit_continuing`_k per quarter, N_k the quarter's standalone implied share count, the factor used only within (0, 1.5], else 1), and the share count at C30b's reference (the median of the previous four quarters' implied counts, carried through splits) with its ratio to N |
| `ranks.csv` | (D, symbol) | Every eligible stock: A_R, A_P, M, g, PEG, r(A_R), r(A_P), R1, R2, R3, T, rank, the reported percentiles S1, S2, S3 and S, sector key, ticker |
| `decisions.csv` | (D) | NAV_open, cash after step 0, budget B, B_left after filling, eligible count, the v1 funnel, the v3 funnel (F1 to F6 in turn), holdings at open, exits by code, V sales, trims by kind, empty slots, buys placed, slots left empty and the reasons, holdings with an untested exit or trim |
| `holdings.csv` | (D, symbol) | Every stock held at D's open or bought at D: status, exit codes, units at open, px_now, weight at open, P/E, excess, U0 after the update, u_V, u_C, units to sell, DOWN0, DOWN1, `untested` (the tests that could not be computed), rank if eligible, sector key |
| `fills.csv` | (D, symbol, kind, side, n) | Every fill and cancellation: kind is exit, valuation, cap, buy or forced_exit |
| `nav.csv` | (date, mark) | The 17 columns of v1 and v2: strategy, universe_ew (see 14.6), nifty500, midcap150, smallcap250, the five factor indices and their no-overnight versions |

Also written, as in v1 and v2: `trades.csv` (v1-compatible), `episodes.csv`,
`rebalance_open.csv`, `in_sample_end_open.csv` (the open of 15 February 2023),
`metrics.json` (including this spec's commit hash and the input hashes of
section 15.2) and `trade_for_trade.csv`.

### 13.1 Record dictionary (WRITER)

So that the first engine comparison finds rule differences, not label
differences:

- **fills.csv `kind`.** `exit`: a full sale under E1 to E5. `valuation`: a V
  full sale, or a trim where u_V exists and u_V ≤ u_C. `cap`: a trim where u_C
  is strictly below u_V, or u_V does not exist. `buy`. `forced_exit`: the
  20-session rule. A cancellation row carries the kind of the order cancelled.
- **holdings.csv `status`.** `new` for a buy placed at D. Otherwise, in this
  order: `exited` (a full-sale order, under E1 to E5 or V); `trimmed_valuation`
  or `trimmed_cap` (a trim order, named by the same rule as `kind`); `kept`.
- **`exit codes`.** Every E rule that applies, in the order E1 to E5, or `V`
  when no E rule applies and u_V = 0. The first is the reported reason.
- **u_V** is empty when U0 is not set or P/E ≤ 85 or unknown; u_C is empty for
  a holding with a full-sale order; units to sell is 0 when nothing is sold.
- **`untested`.** Any of `E1`, `E2`, `E3`, `E4`, `trim` whose inputs are
  unknown at D, in that order. `E3` appears only from 15 November 2022 and only
  when no usable balance sheet exists. `E4` appears when DOWN0 or DOWN1 is
  unknown and the stock was held at the previous review's open.
- **Reasons for empty slots**, written only if slots are left empty, as a
  semicolon-separated list in this order, each only if it applies:
  `none_eligible` (the candidate list ran out), `sector_cap` (at least one
  candidate was skipped for the sector cap), `half_slot_minimum` (filling
  stopped because V fell below NAV_open / 36).
- **Flags for holdings outside v1's universe**: F0 false; F1 to F6 computed
  with the same rules and the market cap of section 3.5.
- **Formats**: booleans as `true` or `false`, missing values as empty fields,
  numbers in Python's shortest round-trip form (`repr`), dates as ISO
  YYYY-MM-DD.

---

## 14. Measurement

### 14.1 The pre-registered primary hypothesis

- **H0:** v3's alpha ≤ 0. **H1:** alpha > 0, one-sided, at the 5% level.
- **Alpha** is measured after IIMA's four Indian factors (market, size, value,
  momentum) plus the A5 quality factor, version `margin`
  (`data/study/quality_factor.parquet`, complete months only).
- **Returns.** Monthly: the last NAV mark of each calendar month over the last
  mark of the month before, minus IIMA's risk-free rate, built exactly as
  `jobs/libcheck_v1.py` and `jobs/attribution.py` build them.
- **Months: March 2020 to December 2025, 70 months.** The IIMA file in the
  repository
  (`2025-12_FourFactors_and_Market_Returns_Monthly_SurvivorshipBiasAdjusted.csv`)
  ends in December 2025. A later IIMA file is not used for this test even if
  one is published before the run.
- **Estimation.** statsmodels OLS with HAC (Newey-West) errors, 3 lags, no
  small-sample correction: the settings of the Clarification to A2's
  measurement. Annual alpha = (1 + monthly alpha)^12 − 1.
- **Decision.** Reject H0 if the alpha's t-statistic is above 1.645
  (statsmodels' HAC statistic is normal-based), that is, if the one-sided
  p-value is below 0.05.
- This is the only primary test. Everything else in section 14 is secondary or
  descriptive.

**Power statement.** What 70 months can detect depends on how much of v3's
return the factors do not explain. The only estimate available is v1's, from
its own published regression (alpha 5.04% a year at t 1.11 over 82 months with
the quality factor, `data/backtest/v1_final/attribution.json`). That implies
residual volatility of about 3.35% a month (11.6% a year).

| Residual volatility assumed | Alpha detected half the time (50% power) | Alpha detected 80% of the time |
|---|---|---|
| v1's, 11.6% a year | about 8.2% a year | about 12.6% a year |
| v1's scaled for 18 names instead of 10, about 8.6% a year (optimistic: it assumes the whole residual is stock-specific) | about 6.1% a year | about 9.3% a year |

So the test can only detect a large alpha. Failing to reject H0 is **not**
evidence that v3 has no edge; it means the sample is too short to tell. This
is stated in the verdict whatever the result.

### 14.2 Against v1 on the same window: A2's five bars

**v1's comparator** (WRITER, question 13). v2-spec A2 compares against "v1
re-run on the same final archive with the same code". v3 does the same:

- **Replay.** After the inputs are frozen (section 15.2), v1 is re-run with its
  own unchanged code and configuration (`jobs/backtest_v1.py`, as for
  `v1_final`) on the frozen inputs, from its own first review (15 February
  2019) to the open of 18 September 2026, into `data/backtest/v1_replay_v3/`.
  It changes no v1 rule and is a reproduction, not a new trial: it is not
  counted in the deflated Sharpe.
- **Reproduction check.** The largest relative difference between the
  replay's NAV and `v1_final`'s at every common mark is reported in the
  verdict. If it is within 1e-9, the replay is `v1_final`. If not (a refill of
  the archive would cause that), the A2 bars use the replay, so that v1 and v3
  stand on the same data, and `v1_final`'s window figures are reported beside
  them with the difference.
- **v1's published full-period figures stay as published.**
- **Window view.** A new job, `jobs/window_view.py`, writes
  `data/backtest/v1_window_2020/` from the replay folder:
  - `nav.csv` starts at a new first mark, (17 Feb 2020, open), holding v1's
    value from `rebalance_open.csv` and each index's value from
    `engine.tri_value_at_open` (or its factor-index form), then every mark
    after it unchanged;
  - `trades.csv` keeps only trades dated in the window;
  - `episodes.csv` keeps the episodes whose entry is on or after 17 February
    2020; the number of episodes already open at the window's start is
    reported beside it;
  - the other files are filtered by date in the same way.

  The measurement jobs then run on it unchanged. The monkey test is not re-run
  for the window view (`--monkeys 0`), because it would need a replay starting
  from v1's 2020 book; v1's published monkey test stands.

**The bars.** v2-spec A2 and the Clarification to A2's measurement, with v1
measured over v3's window: **the open of 17 February 2020 to the open of
18 September 2026**. v3 **beats v1 on A2** only if all five hold:

1. v3's maximum drawdown over the window is at least 5 percentage points
   smaller than v1's over the window.
2. Sortino and Calmar (quantstats) are both higher than v1's over the window.
3. Annual alpha after IIMA's four factors (the months of 14.1, without the
   quality factor) is not lower than v1's over the same months, and its
   t-statistic is not lower than v1's.
4. Annual one-way turnover is not higher than v1's (WRITER, question 15):

       turnover = Σ value of the fills counted / 2
                  / mean of all NAV marks in the window
                  / (2,405 / 365.25)

   where 2,405 is the number of calendar days from 17 February 2020 to
   18 September 2026, and the fills counted are every buy and sell row of
   `trades.csv` dated in the window (trims, exits and forced exits included),
   except, for v3, the fills of the buy orders placed at 17 February 2020. For
   v1's window view every fill dated in the window counts, including those of
   v1's own rebalance at 17 February 2020. A2's text says "including the
   initial build"; v3 leaves out its own because v1 did not start from cash in
   2020 and the window view holds none of v1's 2019 build. It is computed by a
   function `turnover_a2` in `jobs/v3_verdict.py`, written from this text, and
   reported beside v3's figure with its initial build included and beside
   `metrics.turnover` and `verify_v1.turnover`.
5. Capacity is not worse: the 95th percentile, over all fills in the window, of
   trade value as a share of the stock's 60-session median turnover before the
   trade date.
   - v3 starts at Rs 5 lakh.
   - v1's trade values are multiplied by 5,00,000 / v1's NAV at the open of
     17 February 2020 (from `rebalance_open.csv`), so both are measured at
     Rs 5 lakh at the window's start. Capacity scales in proportion to trade
     size, so this is exact.

### 14.3 The owner's own measures

- **Nifty 500 plus 15 points.** v3's CAGR over the window minus the Nifty 500
  total-return CAGR on the same clock, both annualised over calendar days as
  `metrics.json` does. "Met" if it is at least 15.00 percentage points.
  Reported beside v1's figure for the same window.
- **3, 6, 12 and 24-month windows.**
  - Each window starts at the close of the last session of a calendar month,
    from February 2020, and ends at the close of the last session of the month
    m months later (m = 3, 6, 12, 24).
  - Only windows ending on or before 31 August 2026 count.
  - For each m, report for v3, v1 (window view), the Nifty 500 TRI and v3's
    eligible equal-weight benchmark: the number of windows; the median, 10th
    and 90th percentile of (v3 − Nifty 500) in percentage points, not
    annualised (`numpy.percentile`, method `linear`); the share of windows in
    which v3 beat the Nifty 500; and the share in which v3 beat v1. "Beat"
    means strictly greater.
  - For m = 12 and 24 only, also report the share of windows in which v3 beat
    the Nifty 500 by at least 15 points a year:
    (1 + r_v3)^(12/m) − (1 + r_N500)^(12/m) ≥ 0.15.
  - The same figures for v1.
- **Multibaggers.** The number of holding episodes in which an adjusted close
  reached twice the entry price ("doubled while held", v1 C18), with the share
  of episodes.

### 14.4 Does the score itself carry signal? (all eligible stocks)

- **Rank information coefficient (IC).**
  - *What it is:* at each of the first 26 reviews, the Spearman correlation
    (`scipy.stats.spearmanr`, average ranks for ties), across all eligible
    stocks, between T (equivalently S) and the forward return. The 27th
    review's period, from 17 August 2026 to the end, is shorter than a review
    period; its IC is reported but left out of the test.
  - *Forward return:* (V(D′) + dividends) / V(D) − 1, where D′ is the next
    review and V(d) is the engine panel's price at d: the adjusted open if the
    stock trades at d (the EQ or BE open, else the BZ open), else its last
    adjusted close before d. Dividends are the cash per adjusted unit with
    ex-dates in (D, D′]. A stock that stops trading keeps its last close.
  - *Reported:* the mean IC; t = mean / (standard deviation with ddof = 1 /
    √26), a Fama-MacBeth-style test; and the share of positive ICs. The same
    for R1, R2 and R3 separately.
- **Top-minus-bottom quintile spread.**
  - At each review, the eligible list in the order of section 6 is cut into
    five groups whose sizes differ by at most one (v1 C19).
  - Each group is held equal-weighted from the open of D to the next review,
    through `engine.run_quantile`'s machinery: v1's execution rules, no costs,
    dividends as cash, the 20-session exit. `score_col` is minus the section-6
    rank, so `run_quantile`'s sort (score descending, then stock id) is exactly
    the section-6 order.
  - Reported: the CAGR of the top group minus that of the bottom group, and
    the information ratio of the top group against the bottom one
    (`metrics.active`, as v1's diagnostics did).

These diagnostics do not use the portfolio rules. They show whether the score
orders stocks well, as distinct from whether the 18-slot book made money.

### 14.5 How much came from earnings growth, how much from re-rating

- **Intervals.** Take the reviews D_1 to D_27 and set D_28 = the end date,
  18 September 2026. For each interval k from D_k to D_(k+1), let H_k be the
  stocks held at the close of the session before D_(k+1), with the adjusted
  units u_i held then.
- **For each i in H_k, at each end t of the interval**, using the close of the
  session before D_k and the close of the session before D_(k+1):
  - value v_i(t) = u_i × the stock's last adjusted close on or before that
    session (the engine's `Panel.last`);
  - look-through earnings e_i(t) = v_i(t) × TTM_P_i / market cap_i, with TTM_P
    and market cap as known at that review date (sections 4.3 and 3.5). For
    D_28 the snapshot is built at 18 September 2026 like any review's
    (`broadcast_dt` before that date). Negative TTM profit enters as negative
    earnings. A stock without TTM_P or a market cap at either end is left out
    of both ends, and the share of value left out is reported.
- **Sums.** V_k,start and V_k,end are the sums of v_i; E_k,start and E_k,end
  are the sums of e_i.
- **Split of each interval's price growth:**

      V_end / V_start = (E_end / E_start) × [(V_end / E_end) / (V_start / E_start)]
                      =  earnings growth    ×   change in the book's P/E (re-rating)

  This is exact whenever both E values are above zero. An interval where
  either is not is reported as undefined and left out of the chain.
- **Chain.** G_E = Π earnings factors and G_PE = Π re-rating factors. Report
  each annualised over calendar days, from the close of 14 Feb 2020 to the
  close of 17 Sep 2026, and the share ln G_E / (ln G_E + ln G_PE).
- **The remainder** = NAV at the close of 17 September 2026 / NAV at the open
  of 17 February 2020 (Rs 5,00,000 for v3) / (G_E × G_PE). It is dividends,
  cash drag, costs, fill prices differing from the previous close, stocks sold
  within an interval, undefined intervals and the one-session difference
  between the chain's clock and the book's. It is reported, not hidden.
- **Also reported:** the same split for v1 (window view). For context, the
  Nifty 500, using NSE's published P/E (`indices.pe`, a different earnings
  definition, so it is not comparable in level).

### 14.6 Also reported (the same jobs as v1 and v2)

- **`jobs/libcheck_v1.py`**, generalised for a v3 run as it was for v2: CAGR,
  Sortino, Calmar, drawdown, the IIMA regression and the stationary bootstrap
  interval for the margin over v1 (the window view) and over the eligible
  equal-weight benchmark.
- **`jobs/verify_v1.py`**:
  - the monkey test, 500 draws in each of the persistent and fresh modes,
    through `engine_v3.Replay`: only the fill order is randomised, and every
    other v3 rule is kept;
  - attribution against the equal-weight benchmark;
  - the sub-periods before and after the open of 15 February 2023;
  - calendar years;
  - capacity;
  - the deflated Sharpe, counting every trial in `state/trials.jsonl`, which
    then includes v3's one run.
- **`jobs/stats_v1.py`:** drawdowns with duration, up and down capture,
  per-position outcomes, sector weights.
- **`jobs/attribution.py`:**
  - the four-factor regression with and without the quality factor, and the
    ROCE version from March 2023 as a labelled secondary line;
  - excess CAGR and information ratio against the Nifty 500, Midcap 150,
    Smallcap 250 and the five factor indices (A4), over the whole window and
    over each factor index's live part, with the no-overnight sensitivity.
- **`jobs/after_tax.py` (A6):** v3, v1 (window view) and a Nifty 500 index
  fund, after itemised costs and capital-gains tax by lot.
- **Equal-weight benchmark.** The `universe_ew` column is **v3's eligible list**
  equal-weighted, rebalanced at every review, with the same costs, dividends
  and forced exit (`engine.run_universe_ew` on the eligible list). It asks
  whether picking beat buying everything eligible. v1's universe equal-weight
  over the window is reported beside it.
- **v3-specific figures:**
  - cash as a share of NAV at every review, measured at D's open after the
    planned trades (B_left / NAV_open), mean and maximum;
  - holdings at every review;
  - exits by code;
  - valuation trims and cap trims (count and rupees);
  - empty slots and why they were left empty;
  - the number of holdings outside v1's universe at a review;
  - the number of holdings with an exit or trim left untested at a review, by
    test, and how many of those are due to a quarter missing from the archive;
  - the share of held slots with the sector key `unclassified`.

### 14.7 What the verdict will say, fixed now

`jobs/v3_verdict.py` writes `data/backtest/v3_verdict.md` and `.json`. Every
figure comes with its file and key, as `jobs/v2_verdict.py` does. The verdict
states:

1. the primary test's result and the power caveat;
2. whether v3 beat v1 on all five A2 bars on the same window, bar by bar, and
   the v1 reproduction check;
3. whether the owner's "Nifty 500 plus 15" was met, for v3 and for v1;
4. the IC and quintile spread;
5. the earnings versus re-rating split.

What each result means for real money is fixed before the run: see question 6.

---

## 15. Evaluation process

1. **Approval and commit.** The owner approves this text and settles
   section 19. It is committed as `research/strategy/v3-spec.md` before any v3
   code computes a return, and its commit hash is written into every v3
   `metrics.json`.
2. **Data prerequisites** (structure only, no returns), in this order:
   1. **Factor-index endpoints.** NSE's printed open, high, low and close for
      the five factor indices on 14 and 17 February 2020 are added to
      `data/external/nifty_price_endpoints.csv`. The engine refuses to run
      without them (Clarification to A4; the file now has only 2019, 2023 and
      September 2026 rows).
   2. **Quarter coverage gate and targeted refill** (question 12).
      - For each quarter and basis, the *hole share* is the number of
        companies with a stored filing for the quarter before and the quarter
        after (same basis) but none for this quarter, over the number with
        both neighbours (`v3_spec/evidence_final/quarters_doc.py`). It is
        tested for standalone figures from June 2018 and consolidated figures
        from September 2019 (quarterly consolidated results were not required
        before the June 2019 quarter), up to the last quarter any review uses.
      - Every quarter with a hole share above 3% gets a targeted refill first:
        `jobs/backfill_quarter.py --period <quarter end>` (week-wide listing
        windows, retries, documents already held skipped by URL), plus a retry
        of the documents listed in `data/financials/legacy_missing.parquet`.
        On today's archive that is June 2018, September 2018, March 2019,
        June 2019, September 2019, March 2021, December 2021, June 2022,
        September 2022 and December 2022 (section 21.2).
      - After the refill, a quarter still above 3% passes the gate only if NSE
        is confirmed not to carry the missing filings: both its date-range
        listing and a per-company query return none, as recorded for June
        2018, June 2019 and June 2022 on 23 September 2026. Its missing
        companies are written into the run folder and disclosed. Otherwise the
        build waits.
      - No second source fills a gap.
   3. **Revision spot check.** 50 company-quarters are drawn with
      `numpy.random.default_rng(20261005)` from the 2019 to 2023 quarters
      whose first stored `broadcast_dt` is more than 60 days after
      `period_end`. For each, NSE's listing must confirm that the stored
      `broadcast_dt` and the stored figures come from the same document. A
      mismatch stops the build until a dated Clarification explains it.
   4. **Freeze.** Every input is copied and its SHA-256 hash written into every
      `metrics.json`: `data/zen.duckdb`; every financials statement file
      (`zen.data.financials.statement_files()`, which the independent checker
      reads); `data/reference/industry_nse.parquet`; `nse_list.json`;
      `data/external/nifty_tri.parquet`;
      `data/external/nifty_price_endpoints.csv`; the IIMA CSV; and
      `data/study/quality_factor.parquet`, rebuilt from the frozen archive if
      the archive changed. Both engines, the v1 replay and the verifier read
      only the frozen copies. If any input has to change after the freeze,
      everything from step 4 is re-run.
   5. **Coverage on the frozen archive.** Section 21's checks
      (`evidence_final/cov_final.py`, `quarters.py`, `quarters_doc.py`) are
      re-run on the frozen copy and their counts recorded as a dated
      Clarification before any v3 return is computed.
   6. **v1 replay and reproduction check** (section 14.2).
3. **Two engines, built from this text.**
   - *Production* is `zen/portfolio/engine_v3.py` and `jobs/backtest_v3.py`. It
     calls the plumbing code named here: `pit`, `factors`, `engine`,
     `engine_v2.nse_sectors`. Where this spec departs from what that code does
     (v3's basis, the write-back rule, the market cap for holdings, the trim
     orders), production implements the text.
   - *The independent checker* is `jobs/crosscheck_v3.py`. A separate builder
     writes it from this document without reading `engine_v3.py`, and it
     imports nothing from `zen` except what the v2 checker imported
     (`zen.data.financials.statement_files`).
4. **Agreement before any return beyond 2022.**
   - Both engines write `universe.csv`, and `eligibility.csv` and `ranks.csv`
     restricted to v1-universe members, for **all 27 reviews**. These use only
     data before each review and need no simulation, so they involve no
     return. They must agree exactly.
   - Both engines then run the agreement period: reviews to 15 November 2022,
     measured to the open of 15 February 2023.
   - `jobs/compare_runs.py`, extended for v3's files, must find every file and
     every NAV column identical (relative tolerance 1e-9 for numbers, exact
     for flags, keys, labels and order).
   - Each difference is traced to a stock and a date and resolved by a dated
     Clarification, written from this text and never from a return. Both
     engines are then re-run.
   - No trial is recorded for these runs.
5. **Leak test.** At all 27 reviews, eligibility, measures and ranks computed
   from a database cut off at D must equal those computed from the full
   database (`jobs/leak_v1_all_dates.py`, generalised). The static inputs of
   section 3.9 (`industry_nse.parquet`, `pit.StaticLabels`, `nse_list.json`)
   are outside its scope, as in v1 C8, and are passed explicitly.
6. **One full run.**
   - Production runs the full window once with the explicit unlock, and
     records one trial in `state/trials.jsonl`. The checker runs the same
     window.
   - The two must agree exactly.
   - If they do not, the difference is resolved as in step 4, both engines are
     re-run, and the event is disclosed, as v1 disclosure 40 and v2's crash
     disclosure were.
7. **Measurement.** The jobs in section 14 run on the v3 run folder and on
   `v1_window_2020`. `jobs/v3_verdict.py` writes the verdict.
8. **Independent verification.** A separate verifier recomputes every figure in
   the verdict from the run folders with its own code. It also:
   - re-derives eligibility and ranks directly from the frozen database at
     three reviews: `numpy.random.default_rng(20260929).choice(27, size=3,
     replace=False)`, read as 0-based positions in the 27 reviews sorted by
     date;
   - checks every exit, trim and fill against sections 8 to 10.

   Code defects are fixed and disclosed. No rule is changed.
9. **Publication.** Only verified work is pushed. Section 16's disclosures go
   into the README beside the result.

---

## 16. Disclosures

1. **This is not a blind design.**
   - The owner and the assistant had both seen v1's and v2's full results
     before v3 was written. So had this document's writer, who read
     `v1_final`'s recorded values at review opens while fixing the end date.
   - The owner set v3's parameters (Rs 25 crore, 18 slots, 4 per sector, P/E 60
     and 85 with the 2%-per-1% trim, ICR 2.5, D/E 1.2) by principle. The
     assistant proposed, and the owner accepted, the start date, the forms of
     the three ranks, no selling on rank and the 10% cap. The writer chose
     everything tagged WRITER.
   - No return of any candidate rule was computed for any of these choices,
     including the revision of 5 October 2026: the two reviews and the reviser
     ran archive structure and coverage checks only (section 21), reading
     fundamentals and prices before each review.
   - A reader should still treat v3 as designed by people who knew how 2019 to
     2026 went.
2. **The window includes a boom in Indian small and mid caps.** Over v1's
   period, the Nifty Smallcap 250 total-return index ended at about 4.0 times
   its February 2019 level, against 2.8 times for the Nifty 500
   (`data/backtest/v1_final/nav.csv`). Two features of v3 lean towards smaller
   companies: the Rs 25 crore floor and equal-sized buys. The Midcap 150 and
   Smallcap 250 comparisons and the size factor in the regression are there to
   separate that tilt from stock selection.
3. **No balance sheets before the September 2022 half-year.** D/E is tested
   only from 15 November 2022, so for 11 of the 27 reviews, interest coverage
   is the only debt test.
   - **Why not Screener's older balance sheets?**
     - Screener's terms allow personal, non-commercial, transitory viewing only,
       and forbid copying and public display. Building a published backtest on
       its data would break them.
     - Its history is restated and has no broadcast dates, so it is not
       point-in-time.

     The owner may still use it for manual checks in live use (section 18).
4. **Today's sector labels are applied to the past** (v2 item 7). Companies
   that stopped trading before the labels were fetched on 23 September 2026
   have none or carry NSE's legacy scheme. Both are grouped as `unclassified`,
   capped at 4 like any sector (section 3.6), so neither the label's absence
   nor its scheme, which encode a later delisting, exempts a stock from the
   cap. The label file is a static input outside the leak test (section 15.5).
5. **The archive's limits carry over unchanged** from v1 and v2:
   - demergers are not adjusted;
   - share-swap mergers leave through the no-trade exit;
   - the March 2025 refill and later revisions are used only after their
     broadcast dates (C36);
   - lease liabilities are not in debt;
   - **revisions are held unevenly.** Until 23 September 2026 the updater
     skipped revisions and the parquet writer kept only the last version
     (C36). So 0.1% to 0.7% of company-quarters for periods in 2018 to 2023
     have more than one stored version, against 9.6% to 11.2% for 2025 and
     2026. For most of the window "the latest revision before D" is the only
     stored version, which may be a later restatement stamped with its own
     broadcast date. No look-ahead path was found; the spot check of
     section 15.2 tests the pairing of dates and figures;
   - Clarification 38 refuses March 2025 filings tagged only as half-years or
     years, which leaves 25 names without that quarter at the 2026 reviews.
6. **What an active fund manager can do that a screen cannot.** v3 takes
   inspiration only from ValueQuest's publicly described "growth at inflexion
   points" approach, on its website (section 20). The owner's four ValueQuest
   documents are marked not for public circulation and are not quoted or used
   here. Zen has no affiliation with ValueQuest. A fund manager working this
   way can do what this screen cannot:
   - meet management and visit plants;
   - judge promoters, governance and capital allocation;
   - read industry structure and multi-year demand;
   - act on channel checks before results are published.

   v3 sees only quarterly numbers, and sees them late. It tests whether the
   visible part of such an approach works on its own, not whether any fund's
   approach works.
7. **The evidence cited for the edge is mostly from the US.** Its size in India
   is exactly what this test is for.
8. **Whole quarters are missing for many companies** (section 5, "Archive
   gaps", section 21.2). June 2018, June 2019 and June 2022 are short at NSE's
   own source, and other quarters are short by 3% to 10% before the refill of
   section 15.2. Because F5 needs eight unbroken quarters, a gap keeps a
   company out of the eligible list for up to seven reviews, and for a holding
   it leaves E1, E2 and the trim untested for up to three reviews and E4 for
   up to eight. v1 ran on the same archive, where a gap costs up to three
   reviews. Every affected holding is listed in the records.
9. **P is not exactly owners' profit** (section 4.2). For consolidated filers
   it includes minority interests and leaves out associates and joint
   ventures. Examples: VOLTAS's September 2023 quarter shows continuing
   profit of Rs 68.8 crore against reported profit of Rs 35.7 crore
   (joint-venture losses); GODREJPROP's P/E at 15 November 2022 is 59.0 on P
   against about 117 on owners' profit. On an estimate of owners' profit
   (EPS × shares), the owners' share is below 90% for 5 to 36 eligible
   consolidated names per review and below 75% for 0 to 12; using it would
   change F4 for 0 to 9 names and F1 for 0 to 4 per review. Fifteen per cent
   of consolidated quarters since 2023 have reported profit more than 5% away
   from continuing profit. Question 11.
10. **The share count is noisy for a few names.** It is profit divided by EPS
    for the latest quarter (C6), and EPS rounding or a weighted-average count
    can move it well inside C30b's 30-times screen. For stocks with no split or
    bonus in the window, 1 to 10 eligible names per review have a latest count
    more than 25% away from the median of the previous four quarters (SKFINDIA
    at 17 August 2020: P/E 17.8 against 35.2 at the reference count). It
    changed F4 twice (BUTTERFLY at 15 November 2022: 52.5 against 80.1;
    WEBELSOLAR at 18 November 2024: 58.7 against 87.8). For a holding it can
    start a trim that is not reversed. The count used, its source and the
    reference count are recorded (section 13). Question 11.

---

## 17. v3 against v1 and v2

### 17.1 The rules that drive returns differ

| Element | v1 | v2 | v3 |
|---|---|---|---|
| Filters beyond the plumbing | none | mcap > Rs 100 cr; 0 < P/E ≤ 70; from Feb 2023: ROCE ≥ 10%, D/E < 1.5 | TTM profit ≥ Rs 25 cr; ICR ≥ 2.5 throughout, finance-cost write-backs fail; from Nov 2022: D/E < 1.2 where a balance sheet is known; 0 < P/E ≤ 60; 8 complete quarters; not in growth reversal |
| Basis of the figures | C4 | C4 | C4 for the universe; v3's own figures on the basis with eight complete quarters (3.3a) |
| What is ranked | 8 measures in 5 equal groups: margin level and stability; one-quarter revenue growth and profit change; earnings and sales yield; 12-1 month momentum; low volatility | v1's, with ROCE for margin from Feb 2023 | 3 equal parts: two-quarter growth acceleration (revenue and profit); the one-year change in operating margin; PEG with growth capped at 50% |
| Uses price other than valuation | yes (momentum, volatility) | yes (momentum, volatility, trend cash, volatility sizing, entry tranches) | no |
| Positions | 10 | 12 | 18 |
| Sector cap | 3, NSE industry labels (C8) | 3, NSE sector | 4, NSE sector, case-insensitive; unlabelled and legacy-labelled names form one group |
| Selling on rank | yes: outside the top 20 | yes: outside the top 24 | never |
| Sizing | equal weight, every holding resized each review | 1/σ, 0.5 to 1.5 times equal weight | 1/18 at purchase, then drift, cut to a 10% cap |
| Cash | none by rule | 0 to 35% by the trend filter | only unfilled slots and cash waiting for a slot |
| Entry | at the open of D | three tranches over 42 sessions | at the open of D |
| Exits | leaving the universe; rank | leaving the universe; rank; failing a hard filter | loss; ICR < 2.5; D/E > 1.2; growth reversal in two consecutive quarterly results; results overdue; valuation (P/E ≥ 127.5) |
| Valuation selling | none | P/E above 70 means leaving the universe, so a full sale | above P/E 85: 2% of the holding per 1% of excess, in units from the first breach (U0) |
| First review | 15 Feb 2019 | 15 Feb 2019 | 17 Feb 2020 |

**What overlaps, stated honestly.**

- All three use price relative to earnings: v1 through earnings yield, v2
  through a P/E ceiling of 70, v3 through a ceiling of 60 and PEG, which
  divides the P/E by growth.
- v1 ranks the margin **level**; v3 ranks its **change**.
- v1 ranks one-quarter year-on-year growth; v3 ranks the **change** in
  two-quarter growth.
- v2 caps D/E at 1.5 from February 2023; v3 caps it at 1.2 from November 2022,
  with interest coverage before that.

No v3 threshold or ranking form is copied from v1 or v2. The building blocks
are shared plumbing: the archive's definitions of normalised profit, EBITDA
margin, market cap, P/E and D/E. So a number means the same thing in all three
versions.

### 17.2 The plumbing is identical

| Element | Same as |
|---|---|
| Review dates | first session on or after 15 Feb, 1 Jun, 15 Aug, 15 Nov (v1) |
| Point in time | broadcast_dt < D; prices to the previous close; C38 and C39 on quarters |
| Universe pool | v1 rules 1 to 7, lenders excluded, with all their clarifications |
| Revisions, scale screens, share count, identity | C22, C23, C30, C33, C38, C39 |
| Market cap for universe members | the raw close before D times C6's share count (identical at every review in the window) |
| Execution | open of D, the window of C10, sells first, pro rata (C11); trims fixed in units (9.4) |
| Costs | 0.20% per side |
| Dividends, splits, bonuses | C13, C14, C24 |
| Delisting and suspension | 20-session no-trade exit at the last close (C15), with trade-for-trade BZ handling (v2, Clarification to A2) |
| NAV clock, starting capital | C16, Rs 5 lakh |
| Benchmarks | Nifty 500, Midcap 150, Smallcap 250 TRI; the five factor indices with the missing-open rule (A4 and its Clarification) |
| Tax | A6 |
| Measurement jobs | libcheck, verify, stats, attribution, after_tax, baseline_report; the A2 bars as the Clarification to A2's measurement fixes them, against v1 re-run on the same archive |
| End of run | the open of 18 Sep 2026 |

---

## 18. Live use

From the first live review, **16 November 2026** (the first session on or after
15 November 2026):

0. **A completeness gate before every live review.** The backtest sees every
   filing broadcast before D, because the archive was built later with
   retries; a live review sees only what the daily updater fetched by the
   night before D. That can fail unnoticed: the filings refresh failed every
   day from 10 September to 4 October 2026 before it was repaired (commit
   022d369), and the March 2025 listing failure (C36) went unnoticed for
   months. 16 November 2026 is the first session after the 14 November results
   deadline. So, after the last update before D:
   - `jobs/validate_financials.py`'s quarter check and `jobs/data_health.py`
     pass;
   - the number of companies with a filing for the latest quarter whose
     deadline has passed is at least 97% of the number for the same quarter a
     year earlier, in each basis;
   - if a check fails, the screen waits until it passes, and the review's
     trades move to the first session after that, with the delay recorded.

   The database fingerprint and the gate's result are written into the dated
   output file.
1. **The screen's output is recorded as-is, before anything else.** At each
   review the screen runs on the live archive with the same code and rules.
   Everything goes into a dated file: its exits, trims, fills in rank order,
   and the next 20 eligible names below the last one bought. The **screen-only
   portfolio** follows that output exactly, on the same simulation rules as
   the backtest.
2. **The owner's judgement, through a decision journal.** For each buy, the
   owner may:
   - **veto** a pick; or
   - **choose a different name** from the 30 best-ranked eligible names not
     already held, within the sector cap.

   Each such decision is written in the journal **before the trade**, with:
   - the date;
   - the name taken and the name passed over;
   - the reason;
   - what would prove the decision wrong.

   The owner may also leave a slot empty, with a reason.
3. **Exits stay mechanical.** E1 to E5, the valuation trim, the 10% cap and the
   20-session exit are carried out as written. The owner does not override
   them, because pre-committed exits are the point of the rules (the owner's
   own finding: impatient, emotional exits destroyed returns before).
4. **Three portfolios are tracked from 16 November 2026, on the same clock and
   costs:**
   - v1's screen;
   - v3's screen-only portfolio;
   - the owner's **actual** portfolio, at the actual fill prices.

   The value of the owner's judgement is the actual portfolio's return minus
   the screen-only portfolio's, reported with the same windows as section 14.3
   and reviewed after 12 months with the journal beside it.
5. **Judgement never enters a backtest.** A change to v3's rules after
   16 November 2026 makes a v4, pre-registered separately. v3's live record
   continues unchanged beside it.
6. **Manual checks.** The owner may read any source for their own judgement,
   Screener included, within its terms of personal use. Nothing read that way
   feeds the screen.

The dashboard that shows v1, v3's screen and the owner's portfolio side by side
is specified later, when the owner asks for it.

---

## 19. Open questions and choices to confirm

Each of these must be settled before the build starts. The default applies if
the owner does not change it.

### 19.1 Open questions, each with a recommended default

1. **The half-slot minimum for a new buy (section 7).** A buy smaller than
   NAV_open / 36 is not placed.
   *Default: keep it.* Without it the last buy at a review can be a few hundred
   rupees and then occupy a slot for years. It is an addition to the owner's
   rule, which is why it is asked.
2. **D/E where no balance sheet is known (F3).**
   - *Default:* from the review of 15 November 2022, D/E is tested wherever a
     usable balance sheet is known; a stock with none passes F3, and a holding
     with none is not sold. This is the owner's wording read literally
     ("whenever a balance sheet is known point in time").
   - *Alternative:* from 15 November 2022, a stock with no usable balance
     sheet is not eligible, as in v2 and the first draft.
   - They differ only at 15 November 2022: 364 eligible names under the
     default, 329 under the alternative. From 15 February 2023 the lists are
     identical.
3. **The results-overdue exit, E5.** *Default: keep it.* Without it a holding
   that stops reporting cannot be tested by any exit.
4. **Growth: the Rs 12.5 / 25 crore base floor, and the 50% cap in PEG
   (section 4.7).**
   - *Default: both as written.*
   - *Alternative:* a 100% cap, which binds for 6% to 42% of eligible names
     per review, against 17% to 63% for the 50% cap.
   - The floor matters most in 2021 and 2022, when many two-quarter profit
     bases were at or below zero; without it those names would have no growth
     rate.
5. **Growth reversal (E4 and F6).**
   - *Default:* the pair is counted on quarterly results (DOWN0 and DOWN1,
     section 4.9); E4 counts only after the stock was held at the previous
     review's open; a stock with both flags true is not bought (F6). F6 blocks
     0 to 164 names per review that pass everything else.
   - *Alternative:* count reviews, as the first draft did, but count the
     previous review only if the latest quarter has advanced since it
     (otherwise the previous review's state stands). Without that condition
     one result counted twice: in the draft's reading, 130 of 2,465 F6 blocks
     and 981 of 9,443 review pairs rested on the same quarter, including 50 of
     61 blocks at 1 June 2020.
6. **What the verdict decides for real money from 16 November 2026.** To be
   fixed before the run, not after it.
   - *Recommended default:* real money follows v3, with the owner's journal,
     only if v3 beats v1 on all five A2 bars on the same window. Otherwise
     real money follows v1, and v3's screen-only and journal portfolios are
     tracked on paper beside it, with the question revisited after 12 months
     of live data.
   - Whatever the owner chooses, it is written here before the run.
7. **Cash from trims waits for an empty slot (section 7).** This is the owner's
   rule as given. In a strong market the 10% cap and the valuation trim can
   build up cash while all 18 slots are full.
   - *Default: keep the rule as given*, and report cash at every review so the
     effect can be seen.
   - *Alternative, to choose now if wanted:* spread cash above one slot's worth
     equally over the kept holdings that are eligible at the review, each up
     to the 10% cap.
8. **Holdings that leave v1's universe are kept (section 7).** *Default: keep
   them.* The owner's rule is that holdings are sold only by the exits; the
   20-session exit covers a stock that stops trading.
9. **The Rs 25 crore floor is on profit after tax** (section 4.2), the same
   profit as in the P/E. *Default: after tax, as written.* A pre-tax floor
   would admit somewhat smaller companies.
10. **v3's basis for its own figures (section 3.3a).**
    - *Default:* consolidated when its eight quarters are complete, otherwise
      standalone when its eight are complete, otherwise v1's basis (and the
      stock fails F5). v1's universe is unchanged.
    - *Alternative:* v1's C4 basis for everything, as in the first draft.
      Under it, 99 to 115 names per review from August 2020 to February 2021,
      and 72 at June 2021, fail F5 only because they sit on a young
      consolidated series while their standalone series is complete, and the
      skew towards companies without subsidiaries persists in a book that
      never sells on rank.
11. **How the P/E is measured (sections 4.2, 16.9 and 16.10).**
    - *Default:* keep v1's and v2's definitions, so a P/E means the same in all
      three versions: P as `profit_normalised` (including minority interests,
      excluding associates and joint ventures) and C6's share count. Owners'
      profit and C30b's reference count are recorded as diagnostics.
    - *Alternative:* for F1, F4, PEG, the trim and the DOWN flags, use owners'
      profit, P_k × EPS_k × N_k / `profit_continuing`_k (the factor used only
      within (0, 1.5], else 1), and C30b's reference count whenever the latest
      count is more than 25% away from it. That changes F4 for 0 to 9 names
      and F1 for 0 to 4 per review, but the EPS-based estimate has its own
      artefacts (ARVIND, RKFORGE).
12. **Archive gaps (sections 5, 15.2 and 16.8).**
    - *Default:* before the build, refill every quarter with a hole share above
      3% from NSE with the existing backfill job; accept as gaps only quarters
      NSE is confirmed not to carry (June 2018, 2019 and 2022 already are);
      disclose them and list every affected holding.
    - *Alternative:* fill NSE's confirmed gaps from BSE's XBRL filings. Not
      recommended now: it is a new source that would need its own
      point-in-time, identity and scale checks, and v1 never used it.
13. **v1's comparator (section 14.2).**
    - *Default:* v1 re-run with its own code on the frozen archive, window view
      cut from that replay; `v1_final`'s published figures unchanged and
      reported beside it. This is what A2 itself required for v2.
    - *Alternative:* the window view cut from `v1_final` as published. Only
      equivalent if the replay reproduces it exactly, which a refill rules out.
14. **Sector grouping (section 3.6).**
    - *Default:* labels compared without case; unlabelled and legacy-labelled
      stocks form one `unclassified` sector, capped at 4.
    - *Alternative:* v2's convention exactly: each unlabelled stock, and each
      distinct label string, is a sector of its own. Under it the label's
      absence or scheme, which encode a later delisting, exempt a stock from
      the cap.
15. **The turnover bar leaves out v3's initial build (section 14.2, bar 4).**
    *Default: as written,* with the figure including the build reported
    beside it. A2's words include it, but the window view holds none of v1's
    2019 build, so counting v3's would compare a build against no build.
16. **Residual slots after valuation trims (section 7).** A valuation trim can
    leave a small residual, 17.6% of U0 at a P/E of 120, that occupies a slot
    until the P/E reaches 127.5 or an exit fires.
    - *Default: no rule change;* residuals are visible in `holdings.csv`.
    - *Alternative, if wanted:* sell the residual in full when the valuation
      target is worth less than half a slot (NAV_open / 36), matching the
      half-slot minimum for buys.

### 19.2 Other WRITER choices, listed for confirmation

These make the owner's rules exact without changing them. Each stands unless
the owner says otherwise.

- The first review is 17 February 2020, the first session on or after
  15 February 2020 (section 2).
- A usable balance sheet with equity of zero or less fails every D/E test
  (section 4.6).
- A finance-cost write-back below −Rs 1 crore in any TTM quarter fails F2, and
  ICR is computed on positive finance costs (section 4.5).
- The market cap of a holding outside v1's universe uses its latest EQ, BE or
  BZ close before D on D's share basis (section 3.5).
- The ranking sorts on sums of whole and half ranks, which gives the owner's
  percentile order exactly (section 6); floating-point arithmetic is fixed
  (section 4.10).
- The valuation trim's reference is in units (U0), and trim orders are
  sell-only orders for a fixed number of units (sections 9.1 and 9.4). The
  owner's example holds exactly.
- A stock sold at a review cannot be bought back at that review (section 8).
- The record formats and labels of section 13.1.
- The completeness gate before every live review (section 18).

---

## 20. Sources

- Akbas, F., Jiang, C. and Koch, P.D. (2017) 'The trend in firm profitability
  and the cross-section of stock returns', *The Accounting Review*, 92(5),
  pp. 1-32.
- Bernard, V.L. and Thomas, J.K. (1989) 'Post-earnings-announcement drift:
  delayed price response or risk premium?', *Journal of Accounting Research*,
  27 (Supplement), pp. 1-36.
- Chan, L.K.C., Karceski, J. and Lakonishok, J. (2003) 'The level and
  persistence of growth rates', *Journal of Finance*, 58(2), pp. 643-684.
- Damodaran, A. 'Ratings, interest coverage ratios and default spread',
  Damodaran Online (pages.stern.nyu.edu/~adamodar/New_Home_Page/datafile/ratings.html,
  accessed 29 September 2026). An interest coverage of 2.5 is the lower edge
  of the BBB band for large non-financial firms. This is context for the
  owner's 2.5, which was set by the owner.
- Damodaran, A. 'PEG ratios' (pages.stern.nyu.edu/~adamodar/New_Home_Page/lectures/peg.htm,
  accessed 29 September 2026). PEG does not remove the effect of growth; a low
  PEG can reflect risk.
- He, S. and Narayanamoorthy, G. (2020) 'Earnings acceleration and stock
  returns', *Journal of Accounting and Economics*, 69(1). v3's two-quarter
  percentage growth follows the owner's definition, not the paper's scaling.
- Lynch, P. with Rothchild, J. (1989) *One Up on Wall Street*. New York: Simon
  & Schuster. The PEG idea, and the view that growth above about 50% a year
  rarely lasts.
- Validea, 'The investment strategy of Peter Lynch'
  (blog.validea.com/the-investment-strategy-of-peter-lynch, accessed
  29 September 2026). A practitioner's summary that treats growth over 50% as
  unsustainable.
- ValueQuest Investment Advisors, 'Inflexion Fund'
  (www.valuequest.in/inflexion-fund, accessed 29 September 2026 and rechecked
  on 1 October 2026). The public description of the "growth at inflexion
  points" style: 15 to 25 stocks, a 3 to 5-year rolling view, at most 10% of
  assets in one stock, and exits for mistakes, a completed thesis, valuation,
  better opportunities or governance. Inspiration only; no affiliation.
  Nothing in this document comes from the owner's private ValueQuest
  documents.
- SEBI (2015) Securities and Exchange Board of India (Listing Obligations and
  Disclosure Requirements) Regulations, regulation 33: quarterly results within
  45 days of the quarter's end, and annual audited results within 60 days.
- SEBI (2017) circular SEBI/HO/IMD/DF3/CIR/P/2017/114, categorisation of mutual
  fund schemes: the large, mid and small-cap definitions.
- Zen repository: `research/strategy/v1-spec.md`, `v2-spec.md`,
  `research/strategy/v1-verification.md` and `jobs/validate_financials.py` (the
  quarters confirmed short at NSE's source on 23 September 2026), and the code
  named in each rule.

---

## 21. Evidence: archive structure and coverage (no returns)

**Method.** `v3_spec/evidence_final/cov_final.py`, read-only on
`data/zen.duckdb` (as of commit 7772f70), at every review from 15 November 2019
to 17 August 2026, on v1's universe. It reads fundamentals broadcast before D
and closes before D, used only for the market cap in the P/E. It computes no
return. Its full output is `cov_final.csv`. `quarters.py` and
`quarters_doc.py` count companies per quarter in the archive as it stands.

**Reconciliation.** The script also applies the first draft's rules and
reproduces the draft's 27 eligible counts exactly (210 to 771, column "Draft"
below), so every difference comes from the rule changes: v3's basis, the
write-back rule, F3 per stock, F6 now counted (the draft's table left it out)
and F6 on quarters.

### 21.1 Per review

Columns:

- **N**: v1's universe.
- **Pass**: members that pass every filter except F5 and F6.
- **F5 gap**: of those, failing F5 because a quarter inside the eight-quarter
  window is missing. **F5 start**: failing F5 because the stock's first quarter
  in basis b* is later than L − 7 (at the first two reviews mostly the
  archive's start; later mostly new listings). A further 0 to 5 per review fail
  F5 on a missing input inside a present quarter.
- **F6**: of the rest, blocked by F6.
- **Elig**: eligible under the default (F3 per stock). **Strict F3**: under
  question 2's alternative. **Draft**: the first draft's count.
- **Fill**: the most slots a cap of 4 per sector key allows.
- **Uncl.**: eligible names with the sector key `unclassified`.
- **g > 50%** and **g ≤ 0**: eligible names with TTM profit growth (with the
  Rs 25 crore floor) above 50%, and at or below zero.
- **P/E > 85**: members above 85. **WB**: members with a finance-cost
  write-back.

| D | N | Pass | F5 gap | F5 start | F6 | Elig | Strict F3 | Draft | Fill | Uncl. | g > 50% | g ≤ 0 | P/E > 85 | WB |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2020-02-17 | 473 | 335 | 44 | 70 | 0 | 221 | 221 | 210 | 75 | 2 | 38 | 74 | 21 | 4 |
| 2020-06-01 | 445 | 334 | 53 | 51 | 11 | 219 | 219 | 217 | 75 | 1 | 45 | 74 | 23 | 2 |
| 2020-08-17 | 596 | 390 | 57 | 58 | 57 | 218 | 218 | 173 | 75 | 2 | 43 | 64 | 48 | 2 |
| 2020-11-17 | 623 | 408 | 56 | 70 | 99 | 180 | 180 | 175 | 65 | 1 | 45 | 37 | 57 | 5 |
| 2021-02-15 | 728 | 442 | 58 | 73 | 104 | 202 | 202 | 197 | 67 | 0 | 61 | 26 | 71 | 8 |
| 2021-06-01 | 767 | 473 | 62 | 57 | 80 | 270 | 270 | 284 | 76 | 2 | 96 | 25 | 80 | 11 |
| 2021-08-16 | 887 | 564 | 12 | 22 | 42 | 485 | 485 | 517 | 85 | 4 | 287 | 23 | 71 | 11 |
| 2021-11-15 | 887 | 586 | 6 | 22 | 33 | 524 | 524 | 551 | 85 | 6 | 330 | 24 | 79 | 11 |
| 2022-02-15 | 938 | 596 | 7 | 29 | 43 | 517 | 517 | 557 | 82 | 3 | 292 | 32 | 74 | 8 |
| 2022-06-01 | 910 | 608 | 16 | 39 | 52 | 501 | 501 | 533 | 86 | 3 | 243 | 53 | 68 | 4 |
| 2022-08-16 | 859 | 608 | 20 | 38 | 73 | 477 | 477 | 538 | 86 | 4 | 188 | 63 | 70 | 5 |
| 2022-11-15 | 828 | 523 | 23 | 38 | 98 | 364 | 329 | 409 | 86 | 3 | 128 | 53 | 64 | 9 |
| 2023-02-15 | 862 | 573 | 51 | 41 | 116 | 364 | 364 | 471 | 83 | 1 | 126 | 51 | 48 | 8 |
| 2023-06-01 | 854 | 569 | 47 | 38 | 116 | 367 | 367 | 474 | 81 | 1 | 135 | 49 | 56 | 8 |
| 2023-08-16 | 1,032 | 639 | 115 | 44 | 124 | 354 | 354 | 432 | 78 | 0 | 122 | 47 | 86 | 9 |
| 2023-11-15 | 1,081 | 669 | 132 | 39 | 130 | 366 | 366 | 453 | 79 | 0 | 124 | 27 | 100 | 6 |
| 2024-02-15 | 1,174 | 671 | 118 | 33 | 114 | 406 | 406 | 471 | 83 | 1 | 132 | 45 | 128 | 6 |
| 2024-06-03 | 1,181 | 679 | 99 | 40 | 101 | 439 | 439 | 493 | 83 | 0 | 125 | 44 | 142 | 3 |
| 2024-08-16 | 1,239 | 675 | 28 | 41 | 96 | 510 | 510 | 594 | 85 | 0 | 140 | 76 | 153 | 3 |
| 2024-11-18 | 1,265 | 706 | 24 | 42 | 134 | 505 | 505 | 621 | 83 | 0 | 135 | 61 | 153 | 5 |
| 2025-02-17 | 1,255 | 760 | 8 | 57 | 148 | 547 | 547 | 683 | 86 | 1 | 122 | 75 | 106 | 9 |
| 2025-06-02 | 1,238 | 728 | 9 | 49 | 152 | 518 | 518 | 654 | 85 | 1 | 115 | 61 | 140 | 8 |
| 2025-08-18 | 1,195 | 751 | 8 | 52 | 154 | 537 | 537 | 676 | 83 | 0 | 113 | 65 | 111 | 5 |
| 2025-11-17 | 1,205 | 765 | 3 | 57 | 162 | 543 | 543 | 689 | 84 | 0 | 119 | 69 | 126 | 5 |
| 2026-02-16 | 1,181 | 805 | 3 | 61 | 164 | 576 | 576 | 726 | 80 | 0 | 145 | 45 | 89 | 7 |
| 2026-06-01 | 1,295 | 853 | 28 | 65 | 127 | 630 | 630 | 746 | 82 | 0 | 155 | 55 | 130 | 6 |
| 2026-08-17 | 1,316 | 883 | 25 | 75 | 134 | 646 | 646 | 771 | 84 | 0 | 175 | 40 | 131 | 6 |

At 15 November 2019 the universe had 458 members and none had eight quarters,
so nothing was eligible.

### 21.2 Companies per quarter in the archive

A **hole** is a company with an income statement for the quarter before and
the quarter after, in the same basis, but none for this quarter; the
percentage is of the companies with both neighbours. The last column counts a
company as present if it has any stored filing for the quarter, including one
whose figures Clarification 38 refused; that is the measure the gate of
section 15.2 uses. Consolidated holes before September 2019 are not
necessarily gaps, because quarterly consolidated results were not required
before the June 2019 quarter.

| Quarter | Standalone companies | Holes | Consolidated companies | Holes | Holes, any stored filing (standalone / consolidated) |
|---|---|---|---|---|---|
| 2018-03 | 1,336 |  | 445 |  |  |
| 2018-06 (NSE source gap) | 1,296 | 248 (19.3%) | 214 | 222 (56.5%) | 19.3% / 56.5% |
| 2018-09 | 1,542 | 44 (3.5%) | 502 | 3 (1.4%) | 3.5% / 1.4% |
| 2018-12 | 1,585 | 14 (1.1%) | 508 | 2 (0.6%) | 1.1% / 0.6% |
| 2019-03 | 1,323 | 121 (10.1%) | 404 | 41 (11.9%) | 10.1% / 11.9% |
| 2019-06 (NSE source gap) | 1,250 | 161 (13.3%) | 752 | 51 (13.4%) | 13.3% / 13.4% |
| 2019-09 | 1,478 | 52 (4.3%) | 1,061 | 30 (4.1%) | 4.3% / 4.1% |
| 2019-12 | 1,600 | 6 (0.4%) | 1,162 | 7 (0.7%) | 0.4% / 0.7% |
| 2020-03 | 1,596 | 22 (1.4%) | 1,170 | 11 (1.0%) | 1.4% / 1.0% |
| 2020-06 | 1,597 | 21 (1.3%) | 1,168 | 12 (1.1%) | 1.3% / 1.1% |
| 2020-09 | 1,631 | 15 (0.9%) | 1,190 | 12 (1.0%) | 0.9% / 1.0% |
| 2020-12 | 1,663 | 8 (0.5%) | 1,215 | 7 (0.6%) | 0.5% / 0.6% |
| 2021-03 | 1,605 | 74 (4.7%) | 1,185 | 51 (4.4%) | 4.7% / 4.4% |
| 2021-06 | 1,661 | 13 (0.8%) | 1,232 | 10 (0.9%) | 0.8% / 0.9% |
| 2021-09 | 1,686 | 15 (1.0%) | 1,255 | 8 (0.7%) | 1.0% / 0.7% |
| 2021-12 | 1,663 | 80 (4.9%) | 1,235 | 57 (4.7%) | 4.9% / 4.7% |
| 2022-03 | 1,773 | 26 (2.0%) | 1,316 | 19 (1.9%) | 2.0% / 1.9% |
| 2022-06 (NSE source gap) | 1,470 | 319 (18.9%) | 1,085 | 238 (18.9%) | 18.9% / 18.9% |
| 2022-09 | 1,776 | 43 (3.1%) | 1,319 | 20 (1.9%) | 3.1% / 1.9% |
| 2022-12 | 1,789 | 60 (3.5%) | 1,325 | 39 (3.0%) | 3.5% / 3.0% |
| 2023-03 | 1,868 | 9 (0.5%) | 1,373 | 7 (0.5%) | 0.5% / 0.5% |
| 2023-06 | 1,901 | 15 (0.8%) | 1,396 | 13 (1.0%) | 0.8% / 1.0% |
| 2023-09 | 1,947 | 10 (0.5%) | 1,438 | 7 (0.5%) | 0.5% / 0.5% |
| 2023-12 | 1,985 | 7 (0.4%) | 1,475 | 1 (0.1%) | 0.4% / 0.1% |
| 2024-03 | 1,995 | 17 (0.9%) | 1,482 | 13 (0.9%) | 0.9% / 0.9% |
| 2024-06 | 2,036 | 3 (0.1%) | 1,521 | 3 (0.2%) | 0.1% / 0.2% |
| 2024-09 | 2,069 | 1 (0.1%) | 1,548 | 0 (0.0%) | 0.1% / 0.0% |
| 2024-12 | 2,100 | 5 (0.2%) | 1,575 | 7 (0.5%) | 0.2% / 0.5% |
| 2025-03 | 2,069 | 81 (3.9%) | 1,557 | 60 (3.9%) | 0.4% / 0.7% |
| 2025-06 | 2,207 | 5 (0.2%) | 1,659 | 4 (0.3%) | 0.7% / 0.9% |
| 2025-09 | 2,195 | 45 (2.0%) | 1,650 | 33 (2.0%) | 0.2% / 0.1% |
| 2025-12 | 2,282 | 3 (0.1%) | 1,725 | 0 (0.0%) | 0.2% / 0.2% |
| 2026-03 | 2,291 | 14 (0.6%) | 1,739 | 6 (0.4%) | 0.3% / 0.1% |
| 2026-06 | 2,335 |  | 1,770 |  |  |

The archive's first filing was broadcast on 21 May 2018. 193 standalone and 36
consolidated filers have June and September 2018 but not March 2018, most of
them because their March 2018 results were broadcast before that date. Large companies are
among the gaps: TECHM, AUROPHARMA, SRF, HCLTECH and TITAN lack June 2018;
MARUTI and NTPC lack June 2019; ASIANPAINT, BAJAJ-AUTO, CIPLA, SUNPHARMA,
ULTRACEMCO and WIPRO lack June 2022. TCS, INFY and WIPRO start at September
2018.

### 21.3 In short

- **Enough names at every review.** The eligible list never falls below 180
  names (17 November 2020). A cap of 4 per sector key never allows fewer than
  65 slots, across 20 to 22 labelled sectors, so 18 slots can be filled at
  every review. Eligible names with the key `unclassified` number 0 to 6.
- **Gaps and the archive's start cost more than the eight-quarter rule
  itself.** Of the names that pass every other filter, F5 fails for 3 to 132
  per review because a quarter is missing (99 to 132 from August 2023 to June
  2024, mostly June 2022) and for 22 to 75 because the stock's series starts
  too late. The refill of section 15.2 will change these counts; they are
  recomputed on the frozen archive before the build.
- **v3's basis.** 17 to 171 members per review are on a different basis than
  under C4, all of them standalone instead of consolidated. Names passing F5
  rise most in 2020 and early 2021 (270 to 425 at 17 August 2020).
- **Growth reversal.** F6 blocks 0 to 164 names per review that pass every
  other filter; none at the first review, where DOWN1 cannot be known.
- **The pandemic distorts growth bases, and the floor and cap handle it.** The
  50% cap binds for 17% to 63% of eligible names per review, highest from
  August 2021 to June 2022 (49% to 63%); a 100% cap would bind for 6% to 42%.
  Non-positive TTM growth (ranked last on PEG): 5% to 34% of eligible names.
- **D/E.** At 15 November 2022, 733 of 828 members (88.5%) had a usable
  balance sheet; 364 names are eligible under the default reading of F3 and
  329 under the strict one. From 15 February 2023 the two readings give
  identical lists. Among members with a usable balance sheet, 58 to 91 fail
  D/E < 1.2 at each review from 15 November 2022.
- **Interest coverage.** It is known for every member at every review. 5 to 13
  members per review have no finance costs; 2 to 11 have a write-back, which
  blocks 1 to 8 names per review that pass F1, F3, F4 and F5. The
  positive-only denominator changed no other F2 decision.
- **Market cap.** The close rule of section 3.5 gives exactly `pit`'s market
  cap for every member at every review: no member's latest close was a BZ
  close, and no split or bonus fell between a member's last close and D.
- **Owners' profit (diagnostic only).** Among eligible consolidated names, the
  owners' share of P is below 90% for 5 to 36 per review and below 75% for 0
  to 12; on owners' profit, F4 would change for 0 to 9 names and F1 for 0 to 4
  per review (question 11).
- **Where the valuation trim can bite.** Among members, 21 to 153 have a P/E
  above 85 and 10 to 87 one at or above 127.5 at each review.

---

## Clarifications

_None yet._ Anything the build turns up goes here, with its date and its
reason, before any return that could be affected by it is computed.
