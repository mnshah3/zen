# Screen Specification — Value & Quality, Live Only

**As-of date: 2026-09-08. Every number in this document was re-derived today against the parquet archive, not carried over from an earlier note.**

This is a **live screen**, not a strategy with a measured return. It has never been backtested and, on this data, it cannot be — see §7. If any number resembling a CAGR, a Sharpe ratio or a hit rate is ever attached to this design, it was invented. That is exactly how the 30% CAGR happened last time.

**Do not run this screen for money until the four blocking fixes in §8 (A–D) are in place.** A, B and C corrupt the price side of the screen itself; D silently inverts one of the investor's two hard limits.

---

## 0. Corrections to the premises this spec inherited

The designs this spec is built from were prototyped against a mid-backfill snapshot and each declared the screen unrunnable on data-availability grounds. **Those claims are false as of today.** Re-verified against `data/financials/*.parquet` this morning:

| Inherited claim | Verified today |
|---|---|
| "financials stop at 2025-09-30, ~10 months blind, disqualifying" | 22,522 rows, 2,365 symbols, **six period_ends 2025-03-31 → 2026-06-30**, max `broadcast_dt` **2026-09-07 18:44** — current to the day prices end |
| "NO TTM FOR ANYONE, max 3 quarters" | **2,134 symbols have all four quarters 2025-09 → 2026-06** with revenue, EBITDA, depreciation and normalised profit all populated. TTM is available for essentially the whole investable universe |
| "two balance-sheet dates, D/E up to 18 months old" | **Three**: 2025-03-31 (2,477 rows), 2025-09-30 (3,804), **2026-03-31 (3,921)**. The newest balance sheet is ~5 months old |
| "equity fills 48.6% of rows" | That divides by June/December filings which structurally never carry a balance sheet. At 2026-03-31, equity is populated on 3,921 of 4,039 rows |
| "D/E ≤ 1.5 mechanically excludes banks and NBFCs" | **Wrong mechanism.** Lenders drop out through a NULL `debt_to_equity` (missing borrowings tags), not a high one. They must be excluded explicitly — see F1 |
| "`bank` flag in nse_list.json is Y/N" | Values are **`N` (3,539), `F` (212), `B` (65)**. `bank IN ('B','F')` is the usable lender flag; it covers 165 of the names reaching stage 0 |

Two consequences. First, the ugliest compromise in the base design — annualising a single quarter, which structurally buys cyclical tops — is **unnecessary and is dropped**; every earnings figure below is a true four-quarter TTM. Second, the six-month working-capital "accrual proxy" adopted to route around the same absence is demoted from a hard filter to a tie-break, because a real TTM does more of that work.

**The lesson worth keeping:** a design whose distinguishing virtue is candour about its data limits, and which did not re-query those limits before asserting them, has failed at the thing it is best at. Re-run §0 before every quarterly screen and correct this table.

---

## 1. Hard filters

All filters are evaluated on a single as-of date. Nothing reads a row whose `broadcast_dt` is after the as-of timestamp; **`period_end` is never used as a time filter** (a December quarter is published mid-January, so filtering on period end grants six weeks of foresight).

### 1.0 Panel construction (must precede every filter)

```sql
-- Corporate-action factors, COLLAPSED to one row per (symbol, ex_date).
-- Without the GROUP BY, a same-day split+bonus (33 such pairs exist today)
-- applies only one of the two factors. See §8 fix A.
CREATE TABLE ca AS
SELECT symbol, ex_date, exp(sum(ln(factor))) AS factor
FROM corpactions
WHERE factor IS NOT NULL AND factor > 0 AND factor < 1
GROUP BY 1, 2;                                   -- 798 collapsed rows today

-- Adjusted price panel. Cumulative factor = product of all factors with ex_date > bar date.
CREATE TABLE px AS
WITH r AS (
  SELECT * FROM prices
  WHERE date BETWEEN (:asof - INTERVAL 400 DAY) AND :asof
    AND series = 'EQ' AND close > 0 AND isin_code LIKE 'INE%'),
     f AS (
  SELECT r.symbol, r.date, exp(sum(ln(ca.factor))) AS cf
  FROM r JOIN ca ON ca.symbol = r.symbol AND ca.ex_date > r.date
  GROUP BY 1, 2)
SELECT r.*, r.close * coalesce(f.cf, 1.0) AS adj_close,
            r.high  * coalesce(f.cf, 1.0) AS adj_high
FROM r LEFT JOIN f ON f.symbol = r.symbol AND f.date = r.date;
```

**Never read `prices.prev_close`.** It is not corporate-action adjusted: on the adjustable ex-dates its implied one-day return has a median of about −53%. Derive the previous close as `lag(adj_close) OVER (PARTITION BY symbol ORDER BY date)`.

**Never rank or filter on raw `close`.** On raw bhavcopy a bonus issue is arithmetically indistinguishable from a crash, so any "has fallen" or "has not rallied" test run unadjusted silently inverts into a momentum screen — the one thing the investor explicitly refuses. Nine of thirty-nine names in one prototype run of a fallen-stock screen were this artefact alone (TATAINVEST read −95% against a true −8%; SILVERTUC read −76% against a true +140%). `zen/validation/leak.py` cannot catch this: it truncates the archive and compares two runs, and both runs would be equally wrong.

```sql
-- Point-in-time fundamentals: one row per (symbol, period_end), consolidated preferred.
CREATE TABLE fin AS
WITH pit AS (SELECT * FROM financials WHERE broadcast_dt <= :asof_ts),
     d AS (SELECT *, row_number() OVER (
             PARTITION BY symbol, period_end
             ORDER BY consolidated DESC, broadcast_dt DESC) rn FROM pit)
SELECT * FROM d WHERE rn = 1;
```

TTM aggregates are the sum over the **four most recent consecutive `period_end`s** (today: 2025-09-30, 2025-12-31, 2026-03-31, 2026-06-30), and are required to be complete — four non-null observations of every field summed, no gap-filling:

```
ttm_revenue, ttm_ebitda, ttm_depreciation, ttm_profit_normalised, ttm_finance_costs
```

`ebitda` in this archive is `pbt_before_exceptional + finance_costs + depreciation − other_income` (verified: matches `revenue − (total_expenses − depreciation − finance_costs)` on 21,927 of 21,954 rows). It is therefore **operating** EBITDA with treasury income already stripped, which is what the investor asked for. `ttm_ebit = ttm_ebitda − ttm_depreciation`.

Balance-sheet fields come from the **latest row with `has_balance_sheet = true`** (2,225 of 2,280 symbols resolve to 2026-03-31).

### 1.1 The filters

| # | Filter | Computable expression | Rationale |
|---|---|---|---|
| **F0** | Data sufficiency | four complete TTM quarters **and** a balance sheet with non-null `equity` | Not a judgement — a name without these cannot be scored at all |
| **F1** | Not a lender | `symbol NOT IN (SELECT symbol FROM nse_list WHERE bank IN ('B','F'))` | EBITDA is meaningless for banks and NBFCs, D/E ≤ 1.5 is structurally unsatisfiable for them, and the archive holds no NIM, GNPA, provisions or tier-1, so P/Adjusted-BV cannot be computed. Better to own no lender than to own one on a fabricated number. The investor's P/B branch for financials is **moot in this screen** and needs a separate one this data cannot support |
| **F2** | Liquidity | `median(turnover) over last 60 sessions >= 2_000_000` (Rs 20 lakh) | `turnover` is in **rupees** (INFY on 2026-09-07 = 8.39e9 = Rs 839 crore). Rs 50,000 is 2.5% of a day at the floor. Deliberately low: thin names are the edge, not the problem |
| **F3** | Solvent equity | `equity > 0` | **Must precede F4.** 159 companies have negative equity at 2026-03-31; a negative denominator makes `debt_total/equity` negative, which silently passes any `<= 1.5` test. This one line is the difference between enforcing the investor's hard limit and inverting it |
| **F4** | Leverage | `coalesce(debt_total, 0) / equity <= 1.5` | Mandate constant, not tunable. **Honest disclosure:** after F3 this removes only 52 of 1,518 names (3.4%); survivor median D/E is 0.07 and the 25th percentile is 0.00. It is a tail-cutter, not a screen — but it is the tail that ends portfolios |
| **F5** | Return on capital | `(ttm_ebitda - ttm_depreciation) / (equity + coalesce(debt_total,0)) > 0.12` | Top of the stated 10–12% Indian WACC band. **This is the screen**: 1,466 → 748. Most listed Indian companies do not clear their cost of capital, and that is the correct behaviour |
| **F6** | Real profit | `ttm_profit_normalised > 0 AND ttm_ebitda > 0` | `profit_normalised` strips `exceptional_items`, so a land sale cannot buy a place on the list. The EBITDA clause stops "improved from deeply negative to less negative" from reading as quality |
| **F7** | Earnings-quality veto | drop if `abs(sum(exceptional_items)) / abs(sum(pbt)) > 0.25` over the four TTM quarters, **or** if the interim rows disagree materially with the audited annual row | Veto, not a score. A company whose reported and normalised profit diverge wildly is not a cheap company, it is an unreadable one |
| **F8** | Working-capital direction (tie-break, **not** a gate) | `(current_assets - current_liabilities)/ttm_revenue` at 2026-03-31 vs 2025-03-31 | **This is a weak substitute for cash conversion, not cash conversion.** There is no cash flow statement in this archive at all — no CFO, no capex, no FCF. Two balance-sheet dates twelve months apart will miss any receivables-financed revenue game slower than a year. Demoted from the base design's hard filter because a true TTM now does more of the work |
| **F9** | Promoter pledge — **manual, and a veto** | not computable | The `pledge` announcement category holds 4,187 filings, but they are reclassification notices and encumbrance letters; **only 34 rows in the entire announcements table contain any percentage figure**. One of the investor's two stated HARD LIMITS cannot be enforced in code. It is a per-finalist manual check on the NSE/NSDL shareholding pattern before any capital moves, and it is a veto. Any claim that this screen enforces his pledge limit is false |

### 1.2 The funnel, actually run (as of 2026-09-07)

| Stage | Names |
|---|---|
| EQ + adjusted price history + 4-quarter TTM + balance sheet (F0) | 2,079 |
| − banks/NBFCs (F1) | 1,959 |
| − liquidity < Rs 20 lakh/day (F2) | 1,541 |
| − negative equity (F3) | 1,518 |
| − D/E > 1.5 (F4) | 1,466 |
| − ROCE ≤ 12% (F5) | **748** |
| − TTM normalised profit or EBITDA ≤ 0 (F6) | 745 |
| − 6-month return at or above universe median (entry gate, §3) | **368** |

368 candidates for 8 slots. Survivor median 60-session turnover is **Rs 8.88 crore/day**, so a Rs 50,000 position is 0.06% of a day's trade — impact cost is genuinely negligible at his size, which is the one premise of this whole exercise that survives verification untouched.

---

## 2. Ranking factors

**Equal weights. Four ranks, averaged over whichever are computable.** This is a refusal to fit, not a judgement: there is no return series on this data to fit weights against (§7), so any weighting scheme would be four free parameters chosen from nothing. The base design's 40/30/15/15 is dropped for exactly that reason.

### 2.1 Which multiple — depreciation intensity, not the sector label

`industry` is populated for **1,041 of 2,537 symbols** in the announcements feed, and for only **164 of the 368 survivors**. A screen that routes the choice of multiple through that field is undefined for 55% of its own candidate list.

So the multiple is chosen from **depreciation intensity**, a field that is 100% populated on the survivor set:

```
dep_intensity = ttm_depreciation / ttm_revenue
split at the median of the qualified universe (0.0293 today)
  above  -> capital-intensive  -> rank on EV/EBITDA
  below  -> asset-light/stable -> rank on P/E
```

This reproduces the intent of the investor's rule — EV/EBITDA where depreciation policy distorts earnings (telecom, infra, steel, cement, power, hotels, hospitals), P/E where it does not (IT services, mature consumer, services) — from data we have rather than from a label we mostly do not. Today it splits the 368 survivors into 179 capital-intensive and 189 asset-light. It will occasionally misclassify an asset-heavy consumer name. That error is acceptable **and visible**: `dep_intensity` is printed next to every candidate, so a wrong bucket can be overridden by hand on a 30-name shortlist.

The median split costs **zero free parameters** — a median is not a threshold you can tune.

```
EV     = market_cap + debt_total            (GROSS debt; no cash line exists — see §7)
P/E    = market_cap / ttm_profit_normalised
market_cap = last adj_close * shares_implied
```

### 2.2 The four ranks (all computed **within** the depreciation bucket)

| Rank | Definition | Direction |
|---|---|---|
| **R1 Cheapness** | percentile of EV/EBITDA (capital-intensive) or P/E (asset-light) on TTM figures, within bucket | cheaper = better |
| **R2 Quality** | percentile of ROCE `(ttm_ebit / (equity + debt_total))`, within bucket | higher = better |
| **R3 Margin stability** | percentile of `−stdev(quarterly ebitda/revenue)` over the **six** available quarters | steadier = better |
| **R4 Balance-sheet headroom** | percentile of `−(debt_total/equity)`, within bucket | less levered = better |

`composite = mean(available ranks)`. R3 is now computed over six quarters rather than the three the base design had, which is the single largest quality improvement the corrected data availability buys. R4 deliberately double-counts leverage (already F4), because leverage is the mechanism by which a value position becomes a permanent loss rather than a long wait.

**Tie-break, not scored:** `current_assets/current_liabilities > 1` and F8 comfortably passed.

**Percentile ranks, never absolute cut-offs — and here is why that matters.** `shares_implied` is back-solved as `profit / eps_basic`, and it is unstable. Verified on RELIANCE consolidated across six consecutive quarters: 15.64bn, 15.38bn, 16.50bn, 16.09bn, 16.44bn, 14.86bn — a ±5% swing quarter to quarter. The standalone series gives 13.52–13.54bn, so **the consolidation basis alone moves market cap by 22%**. Every market-cap-derived number in R1 inherits this. Ranking within a bucket stops the error from being the whole decision; it does not repair it. **Every multiple on the final 8–10 names must be recomputed by hand from a filing before any money moves.**

---

## 3. Entry rule — "before a rally, not after"

### 3.1 The gate

```
ret_6m = adj_close(last) / adj_close(126 sessions earlier) - 1
require: ret_6m < median(ret_6m) across all names surviving F0–F7
```

**Stated as a percentile, so the threshold costs nothing.** In a bull tape "below median" is still a positive number, which is correct — demanding an absolute decline would only ever buy in crashes, and a fixed "≥15% below the 52-week high" would bind differently in every regime. Today the qualified universe's median 6-month return is **+17.0%**, and 85.3% of the names this gate admits happen also to sit ≥15% below their 52-week high, so the base design's separate "≤85% of the 12-month high" filter is **redundant today and is dropped** (one parameter removed). That redundancy is regime-specific: re-check it each quarter rather than assuming it.

**Say the honest thing out loud.** "Entry before a rally" is not an observable condition. Nobody can filter on a rally that has not happened. What is implementable is *entry without a preceding rally*, and those are different things. This gate guarantees he is not chasing; it guarantees nothing about what follows. And in this tape "below median" means "up less than 17% in six months", not "fallen" — the gate is much softer than its name suggests.

**The one piece of contrary evidence he should see before agreeing to this.** On the order-win filing study (2,386 events, 2022-23), stocks sorted by trailing return *before* the event ran monotonically the wrong way for this rule: the coldest quartile beat the benchmark 51.8% of the time with +2.2% median excess, the hottest 64.7% with +18.7%; on trailing 12-month return, 54.9% vs 74.4%. That is a different signal, a different horizon and one bull regime — but it is the only direct measurement we have of the constraint he imposed, and it points against it. **The anti-rally gate is in this design because he asked for it, not because the evidence supports it.** He should decide that knowingly.

### 3.2 Sizing and staging

- This screen governs the **long-term sleeve only: Rs 4,00,000 (80% of Rs 5 lakh)**. The 20% trading sleeve is out of scope and is not funded from these positions. Eight positions of Rs 50,000. (The base design's ten × Rs 50,000 quietly consumed 100% of capital and left no trading sleeve at all.)
- **Tranche 1: Rs 25,000** on qualification.
- **Tranche 2: Rs 25,000** after the name's **next quarterly results filing** lands (`announcements.category = 'results'`, on `trade_date`), and only if TTM revenue and TTM normalised profit have not deteriorated versus the entry card. If they have, the position stays at half size permanently and every exit rule below runs against the half.

The second tranche is timed by the **reporting calendar, not by price** — which keeps it from becoming a momentum rule through the back door, costs zero free parameters (the base design's "60 days" was one), gives a scheduled fundamental re-check, and does real behavioural work: during the wait he has a specific dated external event to look forward to rather than a price to stare at. That waiting period is exactly when his stated failure mode operates.

**Never average down beyond these two pre-committed tranches.** Averaging down is the same impatience reflex as panic-selling, wearing the opposite mask.

---

## 4. Exit rules — written at entry, before the buy order

### E0 — The entry card (this is the load-bearing part of the whole spec)

No order is placed until a card exists at `state/positions/<SYMBOL>.json` containing, as **numbers, not intentions**:

```
entry_date, tranche1_price, multiple_paid (and which multiple),
bucket_median_multiple_AT_ENTRY   <- frozen; this is the sell number
roce_at_entry, de_at_entry, ttm_revenue_at_entry
stop_price_in_rupees              <- computed, not "25% down, roughly"
time_stop_date                    <- entry + 18 months, an actual date
thesis: one sentence naming what would have to become false
```

His failure mode is deciding in the moment. The moment must therefore arrive with the decision already made, in rupees, in a file he wrote when he was calm. `jobs/daily_brief.py` evaluates every card daily and **reports breaches as facts, never as questions**. The system must never present an exit as a decision, because presenting it as a decision reopens the door the whole design exists to close.

### E1 — Valuation exit (the winner's exit; the half he historically got wrong)

When the position's multiple reaches `bucket_median_multiple_AT_ENTRY` — the frozen number on the card, never a recomputed one that can be rationalised upward — **sell half**. The remaining half continues under all rules below with its stop raised to breakeven. Selling in two pieces is a concession to psychology: full exits feel irreversible and get postponed; half exits get taken.

### E2 — Leverage break

`debt_total/equity > 1.5` at any half-yearly balance sheet → **full exit**, no grace period. Mandate-level. The entry condition failing is an exit condition.

### E3 — Quality break (sell for the reason you bought)

TTM ROCE below 12% for **two consecutive readings** → **full exit**. Two rather than one because a single quarter's TTM roll is noisy; two is also the maximum depth this data permits, so it is a data-forced number, not a tuned one.

### E4 — Hard stop

**−25% from average cost on a weekly close**, on adjusted prices. Weekly rather than intraday so a single wick in a thin small cap cannot eject a position the thesis still supports, and because a weekly close cannot be argued with in the moment. One stopped position costs 2.5% of total capital — survivable arithmetic, which is precisely why it must be honoured while it is cheap.

### E5 — Time stop

**18 months** from tranche 1 with no thesis progress, where progress is defined *only* as TTM revenue growth or ROCE improvement versus the entry card. A cheap stock that stays cheap while the business does not improve is dead capital, and this rule is what stops the long-term sleeve becoming the place where mistakes go to be forgotten.

### E6 — Governance thesis-break (exit within 2 sessions, no tranching)

From the announcements feed: a pledge filing that management has not explained, an auditor resignation or qualification, a promoter-group stake sale, or a credit-rating downgrade. These are the four ways an Indian small cap goes to zero, each detectable within a day, and none of them is a price event.

### E7 — Re-entry lockout

No re-entry into a stopped-out name for **90 days**. Aimed squarely at the panic-sell-then-revenge-buy loop he described.

### E8 — No profit target beyond E1

The remaining half runs until E2–E6 fires, or E5. Cutting winners short is the mirror image of his failure mode and costs just as much.

---

## 5. Position and sector limits

- **8 positions of Rs 50,000** in the long-term sleeve (Rs 4 lakh). The portfolio-wide cap of **10 positions** and **3 per sector** applies across both sleeves, so the trading sleeve's names count against the sector cap.
- **Max 3 per sector. The fourth slot does not open.** A concession granted in advance is a limit that has already failed.
- If a sector reaches its cap, the **lowest-composite-ranked** name in that sector is the one sold — chosen by the ranking, never by which one he likes least.
- **Sector is assigned by hand on the top ~30 ranked candidates, not automated.** `industry` exists for 164 of 368 survivors, it is announcement metadata rather than a taxonomy, and its 75 buckets are inconsistent ("Textiles - Cotton" / "Textile Products" / "Textiles - Synthetic" are one sector for concentration purposes). Thirty lookups is an hour a quarter, kept in a small CSV in the repo. Note this is needed *only* for the cap — the ranking never touches the label, because it buckets on depreciation intensity.
- **What the sector cap does not do:** it controls stock-specific blow-ups, and it barely touches factor risk. Eight small caps across eight sectors still share one enormous common factor — the Indian small-cap tape. Size the expected drawdown off the small-cap index, not off eight independent bets.

---

## 6. Parameter count, stated plainly

The base design claimed "7". That was not true of it and would not be true here. The honest count, with every number classified:

**MANDATE (given by the investor; not free, not mine to tune) — 6**
1. D/E ≤ 1.5
2. ROCE hurdle 12% (top of his stated 10–12% WACC band)
3. Max 10 positions portfolio-wide
4. Max 3 per sector
5. Rs 50,000 per position
6. 80/20 long-term/trading sleeve split

**DATA-FORCED (only one value the archive permits) — 4**

7. TTM = 4 quarters (the archive has exactly six; four is the definition of TTM)
8. Balance sheet = latest `has_balance_sheet` row (2026-03-31 for 2,225 of 2,280 symbols)
9. "Two consecutive readings" in E3 (two is the maximum depth available)
10. Depreciation-intensity split at the **median** (a median is not a tunable threshold)

**FREE — numbers I chose, that nothing in this data can validate — 9**

11. Liquidity floor Rs 20 lakh/day — chosen at 40× position size; the survivor median is 44× higher, so it binds on 418 names but on no plausible finalist
12. Liquidity window 60 sessions — conventional; untested
13. Anti-rally lookback 6 months — the *threshold* is the universe median and costs nothing, but the **window** is a free choice
14. Earnings-quality veto at 25% exceptional/PBT (F7)
15. Shortlist depth ~30 before the sector cap
16. Hard stop −25% weekly close (E4)
17. Time stop 18 months (E5)
18. Re-entry lockout 90 days (E7)
19. Governance-break reaction window 2 sessions (E6)

**Total 19, of which 9 are genuinely free.** Against the base design this spec **removes** five free parameters (four ranking weights → equal weighting; the 85%-of-12-month-high gate → redundant with the entry gate; the 60-day second tranche → the results calendar) and **adds** one (F7's 25% veto).

None of the nine free parameters was fitted to a return series, because no return series exists to fit them to. **That makes them unvalidated, not safe.** The correct posture is: freeze all nine, log the design in `state/trials.jsonl` **before** the first run, and do not touch them for four quarters. Tuning them until the candidate list looks good is the exact behaviour that produced the 30% CAGR.

---

## 7. What this design CANNOT be validated on

Read this section before quoting any number from this screen to anyone, including yourself.

1. **There is no fundamental backtest and there cannot be one.** The archive holds three half-yearly balance sheets (2025-03, 2025-09, 2026-03) and six income-statement quarters. Nothing here — not the leverage cap, not the ROCE hurdle, not cheapness, not margin stability, not the composite — has ever been tested against forward returns, and none of it can be until several more years of filings accumulate. **No event study, no trial count, no deflated Sharpe applies to the fundamental side of this design.** Every threshold in §1 and §2 is economic reasoning, frozen.
2. **The ranking is the part that actually picks the portfolio, and it is the least validated part.** 368 candidates become 8 positions, so the composite does nearly all the real selection, and there is zero evidence that within-bucket cheapness predicts anything in this universe.
3. **Only the price-side gate is measurable at all**, and only after the §8 fixes: the below-median-6m-return gate could in principle be event-studied — but only against a **liquidity-matched, date-matched** control (fix E), because the current baseline sits at Rs 6.81 crore median trailing turnover against Rs 1.06 crore for the event arm, and composition alone is worth ~3.2pp of hit rate. That mismatched baseline systematically rejects microcap screens and blesses large-cap ones — i.e. it rejects exactly what this investor's edge is.
4. **The universe is survivorship-biased even though prices are not.** `prices` includes delisted names; `financials` only covers companies currently filing. Any future attempt to test this screen historically inherits that bias.
5. **No cash flow statement exists.** No CFO, no capex, no FCF, no CFO/EBITDA. F8 is a twelve-month working-capital delta and will miss any slow receivables game.
6. **No cash balance tag**, so EV uses gross debt. EV/EBITDA is overstated for every net-cash company — and net-cash, under-covered small caps are precisely the profile this screen hunts. The bias runs against the strategy's own thesis.
7. **`shares_implied` is derived and unstable** (RELIANCE: ±5% quarter to quarter, 22% between consolidation bases), worst for low-EPS names, which are the ones this screen targets. Recompute every finalist's market cap by hand.
8. **No shareholding pattern of any kind**: no promoter holding %, no pledge %, no FII/DII, no free float. One of two stated hard limits is outside the system permanently (F9).
9. **No sector taxonomy** for 55% of survivors; the 3-per-sector cap is manual and will therefore be applied imperfectly.
10. **No goodwill/intangibles breakout**, so capital employed cannot be adjusted for acquisitions and ROCE is flattered for serial acquirers — the profile that most often blows up in Indian small caps.
11. **Consolidated/standalone mixing.** The PIT dedupe prefers consolidated, but names that only file standalone are compared on a different basis, and standalone hides subsidiary debt and subsidiary earnings both.
12. **One regime.** Everything observable in this archive comes from a bull tape — the qualified universe's median 6-month return today is +17.0%. How any of this behaves in a flat or falling market is untested and, on this data, untestable.
13. **The known base rate is hostile and has not been repealed.** Only 42.6% of randomly chosen liquid Indian stocks beat the Nifty 500 over 12 months and the median stock lags it. Note also that this constant is quoted with far too much precision: it was drawn from **40 entry dates**, so its date-clustered standard error is ~2.1pp, not ~1.0pp, and its true 95% interval is roughly **38.5%–46.7%**. Any future screen result that clears 42.6% by two or three points has cleared nothing.
14. **Correction to a stored prior this screen must not inherit wrongly.** The recorded conclusion "volume spikes beat the benchmark LESS often than random picks" is roughly half to entirely a size artefact. Against a liquidity-matched control the gap is −0.2pp at 12m and +0.1pp at 24m — inside noise. The honest statement is *"volume spikes are indistinguishable from same-size stocks at 6 months and beyond, with a small real 3-month drag."* `research/studies/volume_anomaly.md` must be corrected. It remains true that **nothing in this design should be built on volume**, which is why volume appears nowhere in §1–§4.

**The defensible claim this screen makes is narrow:** cheap *and* demonstrably earning above its cost of capital *and* not levered *and* not already re-rated is a better-than-random starting pool for human judgement on thirty names. It is not a claim of measured alpha. The apparatus's real contribution is not the ranking — it is the pre-committed exits in §4, which address the failure mode he actually named.

---

## 8. Audit findings that must be fixed first, in priority order

### Blocking — the screen is wrong until these land

**A. Collapse same-day corporate actions before the cumulative product.** `zen/validation/eventstudy.py` `adjusted_prices` (merge_asof, ~lines 108–118). When a symbol has two adjustable actions on one ex_date — a bonus plus a face-value split, the standard Indian pattern — merge_asof matches only one and applies only that factor. **33 symbol/ex_date pairs are affected today**, including BAJFINANCE, BAJAJFINSV, 360ONE, CUPID, NAZARA. CUPID's true combined factor is 0.05 and 0.10 is applied, making every pre-2024 price 10× too high; 360ONE fabricates a −50.3% session. Brute-force recomputation disagrees for 17 of the 33, and the applied factor is always *larger* than the truth, so windows spanning the action understate returns by 50–90% — on the compounders that split and bonus repeatedly, i.e. exactly the names any screen exists to find. **Fix:** `SELECT symbol, ex_date, exp(sum(ln(factor))) FROM corpactions WHERE factor>0 AND factor<1 GROUP BY 1,2` before the reverse cumprod (as in §1.0), plus a regression test asserting the result matches a brute-force product-of-factors-after-date for every multi-action symbol.

**B. Recover the unparsed factors, and stop dropping them silently.** `zen/data/corpactions.py`: `_SPLIT` demands the literal "per share"; `_BONUS` demands whitespace after "bonus". **25 real splits and bonuses carry a NULL factor and are never adjusted** — TRENT 2016-09-12 records −89.7% where the truth is +3.0%; also GRASIM, VGUARD, KAJARIACER, GRANULES, UNOMINDA, MOTHERSON, AJANTPHARM ("Bonus- 1:2", hyphen), CORPBANK ("Face Valus Split", an NSE typo). The `factor < 1` clause additionally drops every consolidation, so reverse splits are never adjusted in either direction (KAUSHALYA shows +9,933% in one session). **Fix:** make "per share" optional and allow hyphens in both regexes; parse face-value changes regardless of the `action` label (NSE files some under `other`); allow `factor >= 1`. Then add the control that matters more than the regex — **a standing data gate that fails loudly on any adjusted single-session move outside about −45% to +120% with no adjustable action on file.** That single query catches the entire class, including the next unparsed format.

**C. The screen must read adjusted prices everywhere, and never `prev_close`.** Enforced in §1.0. `prev_close` is unadjusted (median implied return −52.8% on ex-dates), and any gap, overnight or mean-reversion follow-up built on it will "discover" a spectacular bounce that is pure share-count arithmetic. Add the regression assertion: median `|close/prev_close − 1|` on known ex-dates must be under a few percent.

**D. `equity > 0` before every leverage test** (F3). 159 companies have negative equity at 2026-03-31 and pass `debt_to_equity <= 1.5` by sign flip. One line; it is the difference between enforcing his hard limit and inverting it.

### Required before any *measurement* is quoted from this system

**E. Liquidity-matched control in `baseline()`** (`eventstudy.py` ~276–299). Draw the control from names trading the same date whose **trailing-60-day median turnover** (measured before the event, never event-day turnover) is within ±60% of the event stock's — a band that matched 2,999 of 3,000 events. Keep the flat-turnover baseline as a second row labelled "unmatched — different liquidity", and print median trailing turnover for both arms in the output so a mismatch is visible on the face of the table.

**F. Align the benchmark window to the stock's realised window.** `bm_ret` must be `level(exit_date)/level(entry_date) − 1` using the **actual** exit date, and the entry leg must use the index **open** (the `indices` table has one) because the stock is bought at the open. Today 257 of 10,750 outcomes have windows differing by more than 5 days — delisted rows by a median of 201 days and up to 728 — and the open/close asymmetry shifts every excess return by roughly +0.09pp systematically. Add an assertion that |benchmark window − stock window| ≤ a few days, and surface violations in `summarise`.

**G. Fix the exit and delisting classification.** Reject an exit further than ~30 trading days past the target (a suspended-then-relisted name currently books a 1,250-day KOVAI hold as a "12-month" outcome at +201.8pp excess). Stop reusing one truncated exit price as a complete 3-, 6-, 12- *and* 24-month result; add `realised_days` and a `truncated` flag, and count distinct **events**, not outcome rows, in standard errors. Resolve renames by ISIN before calling anything a delisting — 187 of 982 "delisted" symbols are still trading under another ticker (TATAMOTORS, MINDAIND→UNOMINDA).

### Process — the reason the last failure was not caught

**H. `trials.deflated_sharpe` is wrong and must be rewritten or deleted.** It raises `ValueError` for `n_trials` of 1 or 2, omits `sqrt(V)`, and substitutes `sqrt(2 ln N)` for the paper's two inverse-normal quantiles (overstating E[max] by 9–17%). In practice it returns 0.00 for every realistic input and flips to 1.00 above a Sharpe of ~2.2 — it will rubber-stamp precisely the overfit backtest it exists to reject, with Bailey & López de Prado's authority attached. Also: `LOG_PATH` is relative, so `trials.count()` returns 15 from the repo root and 0 from anywhere else — make it absolute, resolved from the package location.

**I. `leak.py` cannot see the tables most likely to leak.** `_truncated_copy` executes `store.SCHEMA`, which defines only `prices`; announcements, financials, indices and corpactions **do not exist in the truncated database**, so a filings strategy raises `CatalogException` rather than being tested. Truncate every point-in-time table on its own key — `prices/indices/corpactions` on `date <= asof`, `announcements` on `an_dt`, `financials` on `broadcast_dt` (never `period_end`) — and add a deliberately leaky announcements strategy to `tests/test_leak.py` that must raise `LeakDetected`. Then wrap this screen and `find_events` in the strategy interface and run the truncation protocol at several as-of dates, including one immediately after a split ex-date.

**J. Correct `research/studies/volume_anomaly.md`** to the liquidity-matched conclusion (§7.14), and add `n_dates`, `n_symbols` and date-clustered standard errors to `summarise()`, refusing to print a rate when `n_dates` is small. The published 42.6% baseline came from 40 dates; its honest interval is 38.5%–46.7%.

**K. Fix the silent attrition paths** (currently dormant, load-bearing later): the `entry['adj_open'] or entry['adj_close']` fallback does not fire for NaN (NaN is truthy and all NaN comparisons are False), and events dropped for missing price history, missing benchmark or no next session vanish from `scored` with no counter. Report `events_in` vs `events_measured` plus three explicit drop columns.

**L. Volume-study hygiene** (only if volume work ever resumes, which §7.14 says it should not): compute the spike ratio on split-adjusted volume or on turnover, which is share-count invariant — corporate actions are currently over-represented among detected events by ~4.7× — and replace `ev.iloc[::found//max_events].head(n)` with a real even stride or a seeded sample, since the integer division collapses to a pure `head()` truncation whenever `max_events < found < 2*max_events`.

---

## 9. Grafts taken and refused

**Taken** (each costs zero or negative free parameters):
- Mandatory split/bonus adjustment on every price feature, and the explicit statement that `leak.py` cannot catch this class — from the falling-knife design, whose author found it by disbelieving his own survivor list.
- `equity > 0` before the D/E test; `broadcast_dt` never `period_end`; explicit bank/NBFC exclusion rather than trusting D/E to do it.
- The physical entry card with rupee numbers written before the order, the frozen-at-entry sell multiple, the half-exit on the winner, and breaches reported by `daily_brief.py` as facts rather than questions — from the neglect design.
- The percentile anti-rally gate (replacing "≤85% of 12-month high") and the results-calendar second tranche (replacing "60 days") — both from the neglect design, both strictly parameter-reducing.
- Equal ranking weights as a refusal to fit — from the catalyst design.
- The governance thesis-break exit read off the announcements feed — from the catalyst and neglect designs.
- The measured evidence *against* the anti-rally instruction, surfaced in §3.1 rather than buried — from the catalyst design.

**Refused, and why:**
- **Interest cover ≥ 3** (catalyst design). Genuinely good — the balance sheet is half-yearly and cover is quarterly, so it is the only current leverage read — but it adds a free parameter to a design that cannot validate one. It is instead **printed next to every finalist as a diagnostic** (`ttm_ebitda / ttm_finance_costs`) with no threshold attached.
- **The "neglect" residual as the primary ranking factor** (neglect design). It is a residual of two proxies — trade count and filing count — against size, and this archive has no analyst coverage, no float, no index membership and no shareholding pattern, so nothing can ever establish whether it measures neglect rather than a wide spread or a quiet promoter. A construct that cannot be checked should not carry 40% of a rank and simultaneously serve as the exit trigger.
- **The order-win catalyst as a trigger** (catalyst design). Its cleaning finding is real and valuable — the repo's `orders` regex sweeps SEBI enforcement actions and GST demand orders into a bullish bucket — but NSE's canonical subject line only exists from 2024, so the clean trigger has zero measured history and the 63.2% beat rate belongs to a different, dirtier population. A good candidate for a **separate, logged study**, not a filter in this screen.
- **A second target at 1.25× the median multiple** (neglect design): one more free number for very little discrimination.

---

## 10. Operating cadence

Run once a quarter, on the first weekend after the results window closes (mid-February, mid-May, mid-August, mid-November) — the only dates on which anything in §1 or §2 can change. Before each run: re-execute §0 and correct that table; re-run the §8-A/B continuity gate over the adjusted panel and refuse to produce a shortlist if it fails; log the run in `state/trials.jsonl` **before** looking at the output.

Then paper-track the eight names for four quarters against the Nifty 500 before adding a rupee. Not because that is validation — it is not, n=8 over one regime — but because it is the only honest thing available, and because the first four quarters are when it will become obvious whether the exits in §4 get obeyed. The screen is the easy part. The card with four numbers already written on it is the part that actually has to work.
---

## Editor's note, added after the spec was written

**On §7 point 14.** The spec inherits the audit's re-measurement of the volume
signal (gap −0.2pp at 12 months, "indistinguishable from same-size stocks").
That audit ran before the event sampler was fixed. The old sampler silently
truncated to the earliest events whenever the count sat between one and two
times the cap, concentrating its sample in 2016-2018. Re-run with the fix and
sampling evenly across 2016-2026, the matched-control gap is **−4.6pp at 12
months for a 10x threshold, significant at every horizon and dose-responsive
across 3x / 5x / 10x**. See `volume_anomaly.md`, which records the
disagreement rather than resolving it by preference.

The practical instruction is unchanged and the spec is right to state it:
**nothing here should be built on volume.** The two readings differ on whether
volume is neutral or actively harmful, not on whether it is useful.

**On the funnel.** Verified independently: 2,079 names clear panel
construction, 748 survive the ROCE hurdle, 368 remain after the anti-rally
gate. 368 candidates for 8 slots.

**On what this document is.** It is a specification, not a validated strategy.
§7 is the most important section in it and should be read before §1.
