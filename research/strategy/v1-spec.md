# Strategy v1: pre-registered specification

Written on 2026-09-21, before any backtest of this design was run. Everything
below is fixed in advance. If the build turns up a genuine ambiguity, the
resolution goes in the Clarifications section at the bottom with the date and
the reason, and it must not be chosen by looking at returns.

## Why this should work

The ranks are factors with published, out-of-sample evidence in many markets:
profitability and quality (Novy-Marx 2013, Asness, Frazzini and Pedersen 2019),
value (Fama and French 1992), momentum (Jegadeesh and Titman 1993) and low
volatility (Frazzini and Pedersen 2014). The question here is not whether they
exist but whether a small, long-only, cost-paying portfolio of Indian stocks
built on them beats simply holding the market. Nothing in this design was
discovered on this archive.

## Dates

**Rebalance decision date D:** the first NSE trading session on or after
15 Feb, 1 Jun, 15 Aug and 15 Nov each year. These fall just after the SEBI
results deadlines (45 days after quarter end, 60 for the March quarter), so
each rank uses a fresh quarter.

**Information set at D:** filings with `broadcast_dt` strictly before D
(00:00), and prices up to and including the close of the previous session.
`period_end` is never used as a time filter.

**Execution:** at the open of D, on corporate-action adjusted prices. If a
stock does not trade on D, the order waits for the next session it trades,
up to 5 sessions, and is otherwise cancelled.

**In-sample:** decision dates from Feb 2019 to Nov 2022 (16 rebalances), with
returns measured up to the open of the Feb 2023 decision date.

**Holdout:** everything after that. Code must refuse to compute any return
after the in-sample end unless an explicit unlock flag is passed, and the
holdout is run once, after the in-sample work is finished and reviewed.

Feb 2019 is the first date with enough history. Tagged results start in
early 2018 and year-on-year growth needs five quarters.

## Universe at D

1. Ordinary equity: ISIN starting `INE`, series `EQ` or `BE`, traded in at
   least one of the last 5 sessions before D.
2. Median daily turnover over the previous 60 sessions of at least Rs 20 lakh.
3. At least 200 trading sessions in the previous 365 calendar days.
4. Not a lender or insurer. Banks, NBFCs and insurers are excluded because
   operating margin and sales yield mean nothing for them and the archive has
   no NIM or NPA data. This is standard in the literature.
5. At least four consecutive quarters known at D, with the latest quarter's
   `period_end` no more than 200 days before D.
6. Trailing twelve-month (TTM) normalised profit above zero and TTM EBITDA above zero.
7. Market capitalisation computable.

For each quarter, consolidated figures are used where the company filed them,
otherwise standalone. The choice is made per company and held consistent
across its quarters at D. Where a quarter was revised, the latest version
known before D is used.

## Measures

Each measure becomes a cross-sectional percentile rank within the universe on
D, where 1 is best. A missing measure scores 0.5.

| Group | Measure | Definition |
|---|---|---|
| Quality | Margin | TTM EBITDA / TTM revenue |
| Quality | Stability | minus the standard deviation of quarterly EBITDA margin over the latest known quarters, up to 8, needing at least 4 |
| Growth | Revenue growth | latest quarter's revenue / same quarter a year earlier, minus 1 |
| Growth | Profit growth | (latest quarter's normalised profit minus the same quarter a year earlier) / TTM revenue |
| Value | Earnings yield | TTM normalised profit / market cap |
| Value | Sales yield | TTM revenue / market cap |
| Momentum | 12-1 month | adjusted close 21 sessions before D, divided by adjusted close 252 sessions before D, minus 1 |
| Risk | Low volatility | minus the standard deviation of daily adjusted log returns over the last 252 sessions |

**Group score** is the mean of its measures. The **composite** is the mean of
the five group scores, with equal weights that are not tuned. Ties go by symbol.

**Market cap** is the raw close of the session before D times the share count.
The share count is `shares_implied` from the latest known quarter, adjusted for
any split or bonus with an ex-date after that filing's `broadcast_dt`.

EBITDA is the archive's `ebitda` column, which is operating EBITDA with other
income stripped. Normalised profit is `profit_normalised`, which excludes
exceptional items.

## Portfolio (the investable configuration)

Built to the owner's own rules: Rs 5 lakh, at most 10 positions, at most
3 per sector.

- **N = 10**, equal-weighted at each rebalance.
- **Sector cap of 3**, using the company's NSE industry classification.
- **Buffer:** a holding is kept while it stays in the universe and ranks within
  the top 2N. Vacancies are filled with the best-ranked stocks not already held,
  respecting the sector cap.
- **Costs:** 0.20% of traded value per side. Sensitivity run at 0.40%.
- **Dividends:** cash dividends are credited on the ex-date and held as cash at
  zero interest until the next rebalance.
- **Stocks that stop trading:** a holding with no trade for 20 consecutive
  sessions is sold at its last close. A harsher sensitivity sells it at half
  its last close.

## Diagnostics (not investable, measuring whether the ranks carry signal)

- Composite quintile portfolios across the whole universe, equal-weighted,
  rebalanced on the same dates with no buffer and no costs. The top-minus-bottom
  quintile spread is the main measure of the signal.
- The same quintile spread for each of the five groups on its own.
- Leave-one-group-out composites (five of them).

Every diagnostic is logged as a trial.

## Benchmarks

1. **Universe equal-weight:** every stock in the universe at D, equal-weighted,
   rebalanced on the same dates, with the same costs. This asks whether picking
   beat not picking.
2. **Nifty 500**, with dividends if a total-return series or a dividend yield
   can be obtained, and stated as price-only if not.
3. **Nifty Midcap 150 and Smallcap 250** as context, to separate stock selection
   from a size effect.

## The grid (in-sample only)

Only portfolio construction varies. Nothing in the grid changes which stocks
rank highest.

| Parameter | Values |
|---|---|
| Positions N | 10, 15, 25 |
| Buffer | none (N), 2N, 3N |
| Rebalance | quarterly, half-yearly (Jun and Nov dates) |
| Sector cap | 3 per sector, none |

That's 36 variants, and every one is written to `state/trials.jsonl`.

**Selection rule:** for each variant, take the median in-sample information
ratio against the universe equal-weight benchmark across the variant itself and
every variant that differs from it in exactly one parameter. Choose the variant
with the highest neighbourhood median. This picks a plateau, not a spike. The
deflated Sharpe ratio is reported using the project's lifetime trial count.

## What counts as passing

Measured once on the holdout, for the selected variant, after costs:

1. Annualised return above the universe equal-weight benchmark, reported with
   a block-bootstrap 90% confidence interval.
2. Annualised return above the Nifty 500.
3. The composite top-minus-bottom quintile spread is positive.

All three are reported whatever they show. The limits are stated up front.
Four years in-sample and about three and a half out means wide intervals. This
test can show the strategy clearly works, clearly fails or is uncertain. It
cannot establish a small edge.

## Reported metrics

CAGR, volatility, Sharpe ratio, information ratio and tracking error against
each benchmark, maximum drawdown, returns by calendar year, turnover, average
holding period, and the number of holdings that doubled while held.

## Clarifications

_None yet._

### 2026-09-21: resolutions made while building the engine

Each of these was fixed while writing the code, before the first in-sample run,
and none was chosen or revised after seeing a return. Each is the most literal
reading available, or the only workable one where the archive forces a choice.

1. **Sessions.** "Session" means an NSE market session in the archive's
   calendar, not the stock's own trading day. Rule 1's "last 5 sessions", rule
   2's "previous 60 sessions" and the momentum and volatility windows all count
   market sessions. For rule 2, a session in which the stock did not trade
   counts as zero turnover in the median.
2. **Momentum closes.** The close "21 sessions before D" is the stock's last
   adjusted close on or before the 21st market session before D (the session
   before D is number 1). The same applies to 252. Adjustment uses split and
   bonus factors with ex-date strictly before D.
3. **Low volatility.** Log returns between consecutive traded closes whose
   dates fall within the last 252 market sessions, with sample standard
   deviation (ddof = 1).
4. **Consolidated or standalone.** A company counts as having "filed
   consolidated" at D if consolidated figures known before D exist for each
   of the four TTM quarters ending at its latest known quarter (either
   basis). If so, every figure uses consolidated; otherwise every figure uses
   standalone.
5. **Consecutive quarters and stability.** Rule 5 counts the unbroken run of
   calendar quarters ending at the latest known quarter in the chosen basis.
   Stability uses the EBITDA margins in that run, latest up to 8, skipping
   quarters with revenue of zero or less. It needs at least 4 and uses sample
   sd. A TTM sum is missing if any of its four quarters lacks the figure, so
   rule 6 then fails.
6. **Share count.** The spec names the latest known quarter's
   `shares_implied` but not a basis. It is taken from that quarter's
   **standalone** filing where one was filed, else consolidated, because
   consolidated profit / EPS is biased by minority interest. For example,
   RELIANCE Dec 2020 implies 7.29bn shares consolidated against 6.44bn
   standalone. Splits and bonuses are applied when the ex-date falls after
   the filing's broadcast date **and before D**. The spec says only "after the
   filing's broadcast_dt", but the upper bound is needed so the share count
   matches the raw close of the session before D. A count of zero or less
   means the market cap is not computable (rule 7).
7. **Lenders and insurers (rule 4).** A symbol is excluded if any one of
   these holds:
   - an NSE industry label of Banks, Finance, Finance - Housing, Financial
     Institution or Insurance, published before D;
   - otherwise, the first such label the archive ever records for the symbol
     (see 8);
   - `bank` of B or F in nse_list.json (today's list, a supplement only);
   - its filings known before D use the bank or insurer XBRL taxonomy (no
     RevenueFromOperations tag), which catches delisted banks and insurers
     directly, and they would fail rule 6 anyway;
   - its company name in filings known before D matches bank, finance,
     financial, financier, fincorp, finserv, finvest, insurance, assurance or
     leasing, which is the net for delisted NBFCs that never carried a label.
8. **Sector labels before 2022.** The announcements archive starts in Jan
   2022, so no label is known before D for any decision date in 2019-2021.
   Where no label had been published before D, the symbol's first label in
   the archive is used. No symbol's label changes anywhere in 2022-2026, so
   the label behaves as a static classification. This is the one input
   derived from data after D. It feeds only the sector cap and the lender
   flag, never a measure or a return, and it is passed to the composite as
   an explicit static input (`pit.StaticLabels`) so the leak test's scope is
   stated rather than implied. A company with no label at all (about 32% of
   universe rows in-sample) is its own sector, so the cap cannot bind on it.
9. **Percentiles.** Average-rank percentile among the names with a value
   (rank / count). Missing is 0.5, as specified.
10. **Execution window.** An order placed at D can fill at the open of D or
    of any of the next 5 market sessions in which the stock trades. After
    that it is cancelled and the cash stays uninvested until the next
    rebalance. A delayed order keeps the rupee target set at D.
11. **Equal weight at each rebalance** applies to every selected name,
    including kept holdings: each is resized to NAV(open of D) / N and only
    the difference is traded and costed. Sells execute before buys. If costs
    leave too little cash for every buy, buys are scaled down pro rata.
12. **Half-yearly rebalancing** builds the portfolio on the first decision
    date (Feb 2019), then trades only on the Jun and Nov decision dates. Every
    variant therefore covers the same in-sample window. Its universe
    equal-weight benchmark uses the same dates.
13. **Dividends.** A dividend is credited to positions held at the close
    before the ex-date. The Rs amount is summed across every "dividend" clause
    in the subject, and is taken as per share outstanding on the ex-date,
    after any split on the same day. It is then converted through later
    splits and bonuses by working in adjusted units. Percent-of-face-value
    and amount-less subjects are skipped (about 1.4% of dividend rows), as
    are REIT and InvIT distributions. An amount above half the prior raw close
    is treated as a parse error and dropped; none triggered in-sample.
14. **Unparsed splits and bonuses.** 25 split and bonus rows have a null
    factor in the archive (for example "Face Value Split Rs 10 To Rs 1" and
    "Bonus- 1:2"). They are re-parsed with a looser pattern. Bonus debentures,
    NCRPS and preference bonuses are not equity bonuses and stay unadjusted.
    Before the same-day product, rows repeating the same (symbol, ex-date,
    action, factor) are removed, so a re-announced action is not applied
    twice.
15. **No-trade exit.** Counted in market sessions. At the close of the 20th
    consecutive session without a trade, the holding is sold at its last
    close times the haircut (1.0 base, 0.5 sensitivity), with the per-side
    cost.
16. **NAV clock.** The first point is the open of the first decision date.
    After that come daily closes, then the open of the in-sample end date (the
    first session on or after 15 Feb 2023). That date's close is never loaded.
    Index benchmarks use the same clock.
17. **Benchmarks** (rewritten 2026-09-22; see 32). The Nifty 500, Nifty
    Midcap 150 and Nifty Smallcap 250 are NSE's own Total Returns Index, the
    gross `tri` column of `data/external/nifty_tri.parquet` (exported from
    niftyindices.com; its price-only series equals the stored Nifty 500 close
    on every row). The TRI is published at the close only, so the two opens
    on the NAV clock (16) are the previous session's TRI times the price
    index's open over its previous close. No price-only, derived or
    approximate series is used for any Nifty benchmark, and the Nifty 50,
    which has no TRI in the file, is no longer reported. The universe
    equal-weight benchmark includes dividends and the same costs and haircut
    as the variant it is compared with.
18. **Metrics.** Sharpe uses a zero risk-free rate, because the archive has
    no T-bill series. Volatility, Sharpe, tracking error and information
    ratio annualise with 252 periods; CAGR uses calendar days. Turnover is
    annual one-way: (buys + sells) / 2 / mean NAV / years, excluding the
    initial build (also reported including it). "Doubled while held" means an
    adjusted close during the holding episode reached twice the entry price.
    The block bootstrap is circular, with 21-session blocks and 5,000
    resamples of paired daily returns, and gives a 90% interval for the
    difference in annualised return.
19. **Quintiles.** Names are sorted by the score (descending, ties by symbol)
    and split into five groups whose sizes differ by at most one.
20. **Demergers are not adjusted.** The corporate-action feed carries no
    demerger ratio. In the in-sample panel, one close-to-close adjusted move
    exceeds 60%: SUVEN on 2020-01-21 (-94.7%), which is the Suven
    Pharmaceuticals demerger. SUVEN is never held by the base configuration.
21. **Feb 2019 universe is small, as a consequence of the rules, not a
    choice.** Rule 5 needs four consecutive quarters. The archive's first
    tagged quarter is Mar 2018, and only about 670 companies' Dec 2018 XBRL
    had been broadcast before 15 Feb 2019 (most followed from 19 to 28 Feb).
    The universe on the first decision date is therefore 171 names, against
    405 to 881 on the other fifteen. It is reported, not patched.

### 2026-09-21: resolutions from reconciling the two implementations (round 1)

The production engine and the independent cross-check were diffed level by
level (decision dates, universe, measures, holdings, NAV). Each difference
below was traced to a specific symbol and date and resolved by the most
literal reading of the text above. [Corrected 2026-09-23: the base
in-sample result had already been computed when these were applied. See 35.] Where one implementation simply departed from an existing
clarification (the cross-check took shares from the chosen basis against 6,
used 253 closes for volatility against 3, took costs out of the target
against 11 and measured "doubled" at exit against 18), that implementation
was corrected and nothing new was needed here.

22. **A filing stored twice.** 813 XBRL documents appear twice in the
    financials archive with identical figures but two period ends, almost
    always one year apart (ECEIND's Mar-2018 results, broadcast 26 May 2018,
    are also stored as 2017-03-31, one second later). They are one filing
    whose period is the later of the two period ends. Keeping the copy with
    the later broadcast time, as both builds first did, chose the mislabel in
    685 of the 813 in the database build and was arbitrary in the 150 exact
    ties (VGUARD, DLF, VISAKAIND at Feb 2019), so the same company could
    have three or four consecutive quarters depending on the build. The archive rebuild
    (`zen/data/financials.rebuild_from_parquet`) and the cross-check now both
    keep the latest period end per document. Because most Mar-2018 quarters
    now count, the Feb 2019 universe is 234 names, not the 171 stated in 21;
    the reasoning in 21 is unchanged.
23. **One company across renames.** The financials archive stores every
    filing under the symbol current when it was downloaded: all of Cadila
    Healthcare's filings from 2018 sit under ZYDUSLIFE, a name it took in Feb
    2022, and Amara Raja's sit under ARE&M. Keyed on the symbol traded at D,
    40 to 60 companies per decision date (AMARAJABAT, CADILAHC, MCDOWELL-N,
    TATAMOTORS, ...) lost their filings and left the universe because of a
    rename that happened after D, which is itself a look-ahead. Rule 3 had
    the same fault in the other direction: PRSMJOHNSN traded as PRISMCEM
    until 2018, so its 200 sessions were only visible under both names. A
    company is therefore one stock id across its (symbol, ISIN) spells in the
    bhavcopy archive: two spells are linked when they share a symbol or an
    ISIN and the later starts within 10 market sessions of the earlier one's
    end, so a symbol reused years later by another company is not merged.
    Filings, corporate actions, dividends and announcements are attributed by
    (symbol, date) to the spell of that symbol that had most recently started,
    else to its earliest spell. Prices of all spells form one continuous
    series, so a holding that is renamed stays held. The link uses only
    (symbol, ISIN, first and last date) keys over the whole archive, never a
    price, and is passed to the point-in-time code as a static input next to
    the industry backfill in 8. Output tables carry the stock id (the latest
    symbol) and the symbol traded at D; ties in the composite go by the
    symbol traded at D, then the id.
24. **Dividend amounts.** Clarification 13's "every dividend clause" includes
    a clause without "Rs" ("Special Dividend 243 Per Share", SANOFI
    2020-06-29; "Dividend - 12.50 Per Share", BLUEDART 2019-07-22) and two
    clauses with no separator between them ("Interim Dividend - Rs 6 Per Share
    Special Interim Dividend - Rs 10 Per Share", HCLTECH 2021-04-29, is Rs 16).
    A leading-zero amount such as "Rs.0125" is Rs 0.125 (GENESYS 2019-09-18).
    The same amount announced twice for one ex-date under differently worded
    subjects is one dividend (PIONEEREMB 2022-07-04 is Rs 0.30, not 0.60).
25. **Momentum after a long halt.** Clarification 2's "last adjusted close on
    or before" the 252nd session has no age limit: at 2021-08-16 SECURKLOUD
    (8KMILES until Jan 2021) had not traded between Sep 2019 and Jan 2021,
    and its close at session 252 is its Sep 2019 close.
26. **Initial build in turnover.** Clarification 18 excludes the initial
    build. That is every fill of the first decision date's orders, which by
    10 may come up to 5 market sessions after it, so trades on or before the
    5th session after the first decision date are excluded (the engine
    previously used 6 calendar days).
27. **What 20 and 13 now catch.** With the renamed companies back in the
    universe (23), four adjusted daily moves in the panel exceed 60%, not
    one. Three are demergers left unadjusted by 20: SUVEN 2020-01-21, HSIL
    (now AGI) 2019-08-19, when Somany Home Innovation was demerged, held by
    the universe equal-weight benchmark at 1/482, and NXTDIGITAL (now
    NDLVENTURE) 2022-11-22, not held that day. The fourth is MAJESCO (now
    AURUM) 2020-12-23, a genuine Rs 974 special dividend that 13's
    half-the-price rule drops as a parse error. MAJESCO left the universe at
    the Nov 2020 decision date, so nothing held it and the drop changes no
    in-sample number. Neither the demergers nor the drop touch the base
    portfolio, and the rules stay as written.

### 2026-09-22: corrections from the audit (made after in-sample returns were seen)

In-sample returns had already been seen when these were written. Each one is
therefore justified only by the point-in-time rule, by accounting, or by the
spec's own text, with the evidence stated. None was checked against a return,
and none changes a measure definition, a weight, the grid or a portfolio rule.
The production engine and the independent cross-check (`jobs/crosscheck_v1.py`)
were both changed. After the change they agree exactly on the universe and on
every member's share count at all 16 decision dates (compared without
computing any return).

28. **Lenders (rule 4) are identified from the company's own filings known
    before D, not from today's list.** This replaces the third bullet of 7.
    `nse_list.json` holds 3,816 filing rows from 2025-26, not a company list,
    and the flag fired if any single row was in bank or NBFC format. So a
    company's filing format in 2025 decided whether it was in the universe
    years earlier. That is information from after D, and it removed firms
    that were industrial at D:
    - Gujarat Fluorochemicals (GUJFLUORO, now GFLLIMITED), before its demerger;
    - Sundaram-Clayton (SUNCLAYLTD, now TVSHLTD), an auto-components maker;
    - Piramal Enterprises (PEL) before its 2022 demerger;
    - Borosil Renewables (BORORENEW), because of one standalone filing in Feb 2025;
    - Maharashtra Scooters (MAHSCOOTER) and Westlife.

    Today's list is still loaded, for diagnostics only, and no longer decides
    rule 4. The replacement signal is the XBRL taxonomy named in each filing's
    document URL (INDAS, NBFC_INDAS, BANKING, GI, LI, NONINDAS), which is known
    at broadcast_dt. A stock is a lender when at least half of the filings
    for its latest four known quarters (both bases, latest revision) are in
    NBFC_INDAS, BANKING, GI or LI.

    Two details were forced by the archive.
    - **A vote, not the latest filing alone.** Single quarters are mis-tagged
      in both directions. India Nippon Electricals, which makes auto
      electricals, filed Mar-2022 in the NBFC taxonomy, and Sunflag Iron did
      the same for Mar-2021. Capri Global, Aditya Birla Capital and Muthoot
      Capital filed some 2020-21 quarters as INDAS. Under a latest-filing-only
      rule those NBFCs would have re-entered the universe at up to three
      dates.
    - **What counts as a vote.** NSE's NBFC taxonomy first appears with the
      Dec-2019 quarter (the first NBFC_INDAS document was broadcast in Jan
      2020, and there are none before). Before then every NBFC filed as INDAS
      or NONINDAS, so only filings broadcast from 1 Jan 2020 vote.

    When none of a stock's latest-quarter filings known at D is that recent
    (decision dates up to early 2020), the vote over its first four quarters
    under the new taxonomy is used instead. That backfill is static, like the
    label backfill in 8. It is passed to the code in `pit.StaticLabels`. It
    reads filings from 2020, a few months to a year after D, instead of 2025.
    Without it, Capri Global, Aditya Birla Capital, Muthoot Capital, Indiabulls
    Housing (now SAMMAANCAP), HUDCO, Paisalo, Dhani and Centrum would all be
    in the 2019 universe.

    Checked on membership only, among the names past rule 3 at the 16
    in-sample dates:
    - SBICARD, CGCL, CREDITACC, HUDCO, ABCAPITAL, MUTHOOTCAP, PAISALO,
      SAMMAANCAP, ISEC, UTIAMC, SMCGLOBAL, UGROCAP, DHANI, CENTRUM, and the
      name-only lenders in 29, are excluded at every date.
    - Today's list alone would still exclude 88 stock-dates across 9 names:
      BORORENEW, GFLLIMITED (until mid-2021), MAHSCOOTER, PEL (until the Aug
      2022 date), TVSHLTD, RTNINDIA, SHAREINDIA, PILANIINVS (2 dates) and
      IWEL (1 date). Those names are now in the universe where the other
      rules admit them.
    - The taxonomy also excludes financial companies that the old flags
      missed: EQUITAS (the holding company of a bank), SATIN (a microfinance
      NBFC), CAPTRUST, 360ONE, SMARTLINK, and the asset managers NAM-INDIA,
      ABSLAMC and HDFCAMC. An asset manager or broker that files in the NBFC
      taxonomy is treated as a lender. This is a consequence of using the
      filing format as the point-in-time definition, not a separate choice.

29. **Company names are a static input, and the name net applies only to
    unlabelled companies.** This replaces the last bullet of 7. The
    financials archive stores the name current at download, not the name at
    filing:
    - all 66 ZYDUSLIFE filings from 2018 read "Zydus Lifesciences Limited",
      a name Cadila Healthcare took in 2022;
    - all 60 ARE&M filings read "Amara Raja Energy & Mobility Limited";
    - Tata Motors' 2018 filings read "Tata Motors Passenger Vehicles Limited";
    - 2,528 stocks carry exactly one name.

    So the name is declared in `pit.StaticLabels` (the latest name per stock
    id) and is outside the leak test's point-in-time scope. As 7 already
    described it, the name is only the net for delisted NBFCs that never
    carried a label, so it is now applied only to stocks with neither a
    label published before D nor a backfill label. Oracle Financial Services
    Software, labelled "Computers - Software", had been excluded at every
    date by the word "Financial". The genuine name-only lenders (BHARATFIN,
    DHFL, RHFL, UJJIVAN, CORALFINAC, NBIFIN) have no label and are still
    excluded at every date. Indiabulls Housing Finance shows why the name is
    weak evidence: its filings are stored as "Sammaan Capital Limited", so it
    is caught by 28, not by its name.

30. **XBRL unit errors are screened out, point-in-time.** Some filings are
    tagged on the wrong unit scale, so every rupee line is 100x (or 1,000x)
    off while EPS, a per-share figure, is unaffected:
    - LODHA standalone, UBL and IRCON (both bases) Mar-2022: 100x low;
    - SFL for three quarters in 2020: 100x low;
    - GAEL Sep-2019 and GRAPHITE Mar-2022: 100x high.

    The share count implied by profit / EPS then moves by the same factor.
    Before this screen, 25 consecutive-date share changes in the in-sample
    ranks were 30x or more (LODHA ranked 18 at 2022-06-01 on a share count
    100x too low). Taken literally, the spec's "latest quarter's
    `shares_implied`" is not wrong, but a market cap 100x off is a data
    defect, not a choice the spec made. Both screens use only filings
    broadcast before D. The rule was set from the structure of the errors,
    never from a return.

    (a) **Mis-scaled filing.** A filing is treated as not filed when it
    breaks scale with more than half of its up-to-8 nearest other known
    quarters of the same company and basis. "Breaks scale" means revenue
    (total income if revenue is missing), implied share count and, where
    both filings report it, employee cost all differ by 30x or more in the
    same direction. Only neighbours with positive revenue and share count
    are counted. A basis's only quarter is compared with the other basis's
    filing for the same quarter.
    - Requiring the share count and employee cost to move too keeps real
      collapses, where the share count stays put: cinemas and parks in
      Jun-2020 (INOXLEISUR, WONDERLA) and GFLLIMITED after its demerger.
    - A majority rather than a median of earlier levels stops one mis-scaled
      first filing (TCI standalone Mar-2018) from pushing every later filing
      out of line.
    - Nothing is rescaled by a guessed power of ten.

    The basis rule (4) and rules 5 and 6 then run as usual. A company whose
    run of four consecutive quarters is broken fails rule 5, as with any
    missing quarter:
    - LODHA, UBL, IRCON, GRAPHITE and SHANTIGEAR at the Aug and Nov 2022 dates;
    - DLF at Nov 2019 and Feb 2020;
    - GAEL from Feb to Aug 2020;
    - CAPLIPOINT from Aug 2020 to Jun 2021;
    - SFL from Nov 2020 to Jun 2022;
    - HCC at Feb and Jun 2021;
    - SILGO at Feb 2022.

    In all, 58 distinct filings are screened out somewhere in-sample.

    (b) **Share count.** The count for the latest quarter (chosen as in 6)
    is compared with the median of the counts for up to four earlier known
    quarters, standalone first. Each earlier count is carried to the latest
    filing's broadcast date through the splits and bonuses in between. If the
    count differs from that median by 30x or more, the other basis's count
    for the same quarter is used when it is within 1.5x of the median, and
    otherwise the median is carried forward. A count of zero or less still
    fails rule 7 (6). In-sample this changes three member counts:
    - ARVIND at 2022-06-01: its standalone EPS is -373, so the consolidated
      count is used;
    - GOACARBON at 2021-11-15 and 2022-02-15: its Sep and Dec 2021 EPS are
      both recorded as 915.11, about 100x its profit per share, so the
      earlier count is carried forward.

    After both screens no consecutive-date share change in the ranks is 30x
    or more. `data/backtest/v1/data_quality.csv` lists every screened filing
    and replaced count per decision date. The truncation leak tests cover
    both screens.

    Because of 28-30, the Feb 2019 universe is 236 names, not the 234 in 22.

31. **Share-swap mergers keep the no-trade exit.** The audit asked for a
    stock that stops trading because of a share-swap merger to be valued at
    the acquirer's price times the swap ratio, where the ratio can be parsed
    from the corporate-action or announcement text. No in-sample ratio can
    be parsed from the archive:
    - the corporate-action feed has no rows for these schemes;
    - announcement subjects are cut at 500 characters and give only the
      record date: MINDTREE, JMCPROJECT, EQUITAS, JSLHISAR, GALLISPAT,
      SORILINFRA, whose Jul-2022 text is cut at "will get sh";
    - the one announcement that mentions a "share exchange ratio" in the
      archive, SHRIRAMFIN on 2022-11-16, does not state it;
    - the announcements archive starts in Jan 2022, so Tata Steel BSL's Nov
      2021 merger into Tata Steel is not covered. The base portfolio held
      TATASTLBSL and sold it by the no-trade rule on 2021-12-13.

    The spec's no-trade exit (15) therefore stays as written for every
    merger. This is a limit of the archive, recorded here so the exits are
    read as such.

32. **Benchmarks use NSE's official Total Returns Index.** Clarification 17
    is rewritten. `data/external/nifty_tri.parquet` (NSE's TRI, exported from
    niftyindices.com and validated: its price-only series equals the stored
    Nifty 500 close on every row) covers the Nifty 500 from 2015, the Midcap
    150 from 2016-09 and the Smallcap 250 from 2018-09, with no in-sample
    session missing. Its gross `tri` column replaces the price-only Nifty 500
    and the Midcap 100 and Smallcap 100 stand-ins. The spec names Midcap 150
    and Smallcap 250 and asks for the Nifty 500 with dividends, so this
    follows the spec text. The file is not stored under `data/indices/`.
    Rows on or after the in-sample end date are never loaded.

### 2026-09-23: disclosures from the second audit

An independent audit of the finished result, run after the final test, found
that this document did not describe everything the code does, and that one
heading above overstated how early some rules were fixed. Nothing below
changes a rule. It records what was true and when.

33. **Linking a company across renames.** Clarification 23 says a symbol
    change is linked when the old and new keys are within 10 sessions. The
    code has used 60 since the 22 September fix round, and adds a third pass
    that joins two components of the same ISIN issuer (the first seven
    characters) when a company changes its symbol and its ISIN on the same
    day, under four guards documented in `zen/universe/identity.py`. This was
    written into the code and never into this document. Known limit: prices
    run straight across a capital reduction at such a seam, so RUCHISOYA at
    3.35 becomes PATANJALI at 17.0 as if it were a 407% return, and momentum
    reads it that way. No seam stock was held in the final test. Over the
    full period the likely effect is under 0.1 points a year, through one
    SPLPETRO holding from August to November 2021.

34. **Stocks moved to trade-for-trade are still force-sold.** The code
    comments and the 22 September fix round describe keeping every non-EQ/BE
    series in a separate `prices_other` store so that the 20-session no-trade
    exit would not sell a stock NSE had moved to its BZ segment while it was
    still trading. That fix was only half built: the store was never
    backfilled for past dates, and the engine never reads it. The exit rule
    therefore behaves exactly as Clarification 15 alone describes. The
    strategy is unaffected, because its only forced exit in the whole run
    was Tata Steel BSL's merger (31). The equal-weight benchmark holds every
    name and may be slightly affected. Deferred to the v2 build, where it
    will be implemented in both engines rather than patched in one.

35. **When Clarifications 22 to 27 were written.** The heading of that
    section says they were resolved "before either implementation's returns
    were compared". That is not accurate. The base configuration's in-sample
    result had already been computed and logged twice (`state/trials.jsonl`
    rows 130 and 131: 25.76% a year, information ratio 0.166) before those
    clarifications were applied, and the next logged run (row 132) was 28.70%
    with an information ratio of 0.337. Each change was justified by a
    specific accounting or data error rather than by its effect, and the
    final test was run afterwards on the fixed code. But the rules were not
    all fixed before any return had been seen, and a reader deciding how far
    to trust the pre-registration should know that.

36. **The March 2025 quarter was incomplete.** The financials archive held
    1,516 companies for March 2025 against about 2,100 before and 2,200 after.
    The listing code stopped at the first page that failed to load and
    returned the rest as if it were complete. Rule 5 then removed about 430
    names at the 2025-08-18, 2025-11-17 and 2026-02-16 rebalances. The
    listing now retries and then raises instead, the quarter has been
    refilled from NSE (2,166 companies, 14 documents confirmed absent by NSE
    itself). The same pass added about 2,200 documents to the later 2025 and
    2026 quarters. Almost all are REVISIONS of filings already held, not
    missing companies: the regular updater skipped anything whose company,
    quarter and basis it already had, so it never fetched a revision. The
    engine uses each version only after its broadcast date, so they are
    legitimate, and they move the held-back result from about 33.8% (March
    refill alone) to 33.0%. From 2026-09-23 the updater skips by document
    instead, so live and backfilled quarters follow the same revision policy,
    and the parquet writer keeps every version rather than the last. The one-shot final test stands on record
    as it was run. A re-run on the completed archive is published beside it.

37. **The clock for sub-periods.** The final test is measured from the value
    at the open of 15 February 2023 to the open of the end date, as 16
    requires. The first published figure used the close of 15 February
    instead, because the open value was not recorded mid-run. The engine now
    records the value at every rebalance open.
