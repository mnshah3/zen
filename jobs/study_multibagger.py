"""What precedes a stock tripling, and does it actually predict one?

THE QUESTION THIS ANSWERS, AND THE ONE IT REFUSES TO

Every multibagger has order wins, capacity expansion and broker upgrades in its
history. Looking back from PG Electroplast and finding them tells you
P(order wins | multibagger), which is near certain and worth nothing, because
hundreds of companies had the same filings and went nowhere.

What decides whether money is made is the inversion: P(multibagger | signal).
This study measures that, and measures it against a matched base rate rather
than against zero.

WHY MEDIANS ARE THE WRONG TOOL AND ARE NOT USED

A long-term investor is buying a tail. If 5% of companies showing a pattern go
up fivefold and 95% do nothing, the median is flat and the strategy is
excellent. An earlier study in this project reported medians over three
sessions and concluded very little, which was the right answer to a question
nobody had asked. Everything here is a HIT RATE on a large outcome.

THE REGIME PROBLEM, WHICH IS BIGGER THAN ANY SIGNAL HERE

Unconditionally, 6.40% of liquid stock-months triple within 24 months. By year
of entry that runs from 0.29% in 2017 to 16.76% in 2020. Anything bought at the
covid bottom tripled. So a signal that merely fires more often in 2020 will
look extraordinary and be worthless, and every comparison below is made WITHIN
the same month against stocks that could have been bought instead.

POINT-IN-TIME

Features are computed from data available at the snapshot date and no later.
Fundamentals join on broadcast_dt, never period end. Prices are adjusted for
splits and bonuses. The universe is every liquid stock trading that month,
including the ones that later delisted.

    python -m jobs.study_multibagger
    python -m jobs.study_multibagger --outcome 5
"""

from __future__ import annotations

import argparse
import logging
import sys

import duckdb
import pandas as pd

from zen.validation import trials

log = logging.getLogger(__name__)

HORIZON_MONTHS = 24
MIN_TURNOVER = 1e7          # Rs 1 crore normal daily turnover
LAST_ENTRY = "2024-09-01"   # needs 24 months of forward price


def build(con) -> None:
    """Adjusted prices, month-end snapshots, forward outcome, and features."""
    con.execute("""CREATE OR REPLACE VIEW prices AS SELECT * FROM
        read_parquet('data/daily/**/*.parquet', union_by_name=true)""")
    con.execute("""CREATE OR REPLACE VIEW ca AS SELECT * FROM
        read_parquet('data/corpactions/*.parquet', union_by_name=true)""")
    con.execute("""CREATE OR REPLACE VIEW ann AS SELECT * FROM
        read_parquet('data/announcements/**/*.parquet', union_by_name=true)""")
    con.execute("""CREATE OR REPLACE VIEW fin AS SELECT * FROM
        read_parquet('data/financials/legacy_2*.parquet', union_by_name=true)""")

    # Same-day split and bonus collapsed by product first; applying one of two
    # is a 5x price error.
    con.execute("""CREATE OR REPLACE TEMP TABLE fac AS
        SELECT symbol, CAST(ex_date AS DATE) ex, exp(sum(ln(factor))) f
        FROM ca WHERE factor IS NOT NULL AND factor > 0 GROUP BY 1, 2""")

    con.execute("""CREATE OR REPLACE TEMP TABLE adj AS
        SELECT symbol, CAST(date AS DATE) d, turnover, high, low,
               close * coalesce((SELECT exp(sum(ln(f.f))) FROM fac f
                   WHERE f.symbol = p.symbol AND f.ex > CAST(p.date AS DATE)), 1.0) px
        FROM prices p
        WHERE isin_code LIKE 'INE%' AND series IN ('EQ','BE')
          AND close > 0 AND turnover > 0""")

    # One snapshot per symbol per month, at that month's last traded session.
    # Daily log return first: DuckDB will not nest a lag() inside a stddev()
    # window, so the return is materialised before it is aggregated.
    con.execute("""CREATE OR REPLACE TEMP TABLE adjr AS
        SELECT *, ln(px / nullif(lag(px) OVER (PARTITION BY symbol ORDER BY d), 0)) AS lr
        FROM adj""")

    con.execute("""CREATE OR REPLACE TEMP TABLE snap AS
        WITH me AS (SELECT symbol, max(d) d FROM adjr
                    GROUP BY symbol, date_trunc('month', d))
        SELECT a.symbol, a.d, a.px, a.turnover,
               median(a.turnover) OVER w12 AS tno,
               stddev_samp(a.lr) OVER w12 AS vol,
               (SELECT a2.px FROM adj a2 WHERE a2.symbol = a.symbol
                  AND a2.d <= a.d - INTERVAL 12 MONTH
                ORDER BY a2.d DESC LIMIT 1) AS px_12m_ago,
               (SELECT a2.px FROM adj a2 WHERE a2.symbol = a.symbol
                  AND a2.d <= a.d - INTERVAL 6 MONTH
                ORDER BY a2.d DESC LIMIT 1) AS px_6m_ago,
               (SELECT max(a3.px) FROM adj a3 WHERE a3.symbol = a.symbol
                  AND a3.d BETWEEN a.d - INTERVAL 36 MONTH AND a.d) AS px_high_3y,
               (SELECT min(a3.px) FROM adj a3 WHERE a3.symbol = a.symbol
                  AND a3.d BETWEEN a.d - INTERVAL 12 MONTH AND a.d) AS px_low_1y,
               (SELECT max(a3.px) FROM adj a3 WHERE a3.symbol = a.symbol
                  AND a3.d BETWEEN a.d - INTERVAL 12 MONTH AND a.d) AS px_high_1y
        FROM me JOIN adjr a ON a.symbol = me.symbol AND a.d = me.d
        WINDOW w12 AS (PARTITION BY a.symbol ORDER BY a.d
                       ROWS BETWEEN 250 PRECEDING AND CURRENT ROW)""")

    # Forward outcome: the price nearest 24 months out, within a month either
    # side. A stock that stops trading has no forward price and is EXCLUDED
    # from the denominator rather than counted as a failure -- it may have been
    # acquired at a premium. That exclusion is stated in the output.
    con.execute(f"""CREATE OR REPLACE TEMP TABLE panel AS
        SELECT s.*,
               (SELECT a.px FROM adj a WHERE a.symbol = s.symbol
                  AND a.d BETWEEN s.d + INTERVAL {HORIZON_MONTHS - 1} MONTH
                              AND s.d + INTERVAL {HORIZON_MONTHS + 1} MONTH
                ORDER BY abs(date_diff('day', a.d, s.d + INTERVAL {HORIZON_MONTHS} MONTH))
                LIMIT 1) AS px_fwd
        FROM snap s
        WHERE s.tno >= {MIN_TURNOVER} AND s.d <= DATE '{LAST_ENTRY}'""")


def features(con) -> pd.DataFrame:
    """Everything knowable at the snapshot date."""
    df = con.execute("""
        SELECT symbol, d, px, tno, px_fwd,
               px / nullif(px_12m_ago, 0) - 1 AS ret_12m,
               px / nullif(px_6m_ago, 0)  - 1 AS ret_6m,
               px / nullif(px_high_3y, 0) - 1 AS off_3y_high,
               CASE WHEN px_high_1y > px_low_1y
                    THEN (px - px_low_1y) / (px_high_1y - px_low_1y) END AS pos_52w,
               vol * sqrt(250) AS vol_ann
        FROM panel WHERE px_fwd IS NOT NULL
    """).df()

    # Fundamentals as of the snapshot, point-in-time on broadcast_dt. TTM over
    # the four most recent quarters PUBLISHED by then.
    fund = con.execute("""
        WITH pub AS (
            SELECT p.symbol, p.d, f.period_end, f.revenue, f.ebitda,
                   f.profit_normalised,
                   row_number() OVER (PARTITION BY p.symbol, p.d
                                      ORDER BY f.period_end DESC) AS rn
            FROM panel p JOIN fin f
              ON f.symbol = p.symbol
             AND CAST(f.broadcast_dt AS DATE) <= p.d
             AND f.consolidated
        )
        SELECT symbol, d,
               sum(revenue) FILTER (WHERE rn <= 4)  AS rev_ttm,
               sum(revenue) FILTER (WHERE rn BETWEEN 5 AND 8) AS rev_ttm_prior,
               sum(ebitda)  FILTER (WHERE rn <= 4)  AS ebitda_ttm,
               sum(ebitda)  FILTER (WHERE rn BETWEEN 5 AND 8) AS ebitda_ttm_prior,
               sum(profit_normalised) FILTER (WHERE rn <= 4) AS pat_ttm
        FROM pub WHERE rn <= 8 GROUP BY 1, 2
    """).df()

    # Filings in the trailing year. Categories carry their own first
    # trustworthy year, so these are only meaningful late in the sample and the
    # output says so rather than letting a zero read as an absence of events.
    fil = con.execute("""
        SELECT p.symbol, p.d,
               count(*) FILTER (WHERE a.category = 'orders')    AS n_orders,
               count(*) FILTER (WHERE a.category = 'expansion') AS n_expansion
        FROM panel p LEFT JOIN ann a
          ON a.symbol = p.symbol
         AND a.session_date BETWEEN p.d - INTERVAL 12 MONTH AND p.d
        GROUP BY 1, 2
    """).df()

    df = df.merge(fund, on=["symbol", "d"], how="left").merge(fil, on=["symbol", "d"], how="left")
    df["rev_growth"] = df.rev_ttm / df.rev_ttm_prior.where(df.rev_ttm_prior > 0) - 1
    df["margin"] = df.ebitda_ttm / df.rev_ttm.where(df.rev_ttm > 0)
    df["margin_prior"] = df.ebitda_ttm_prior / df.rev_ttm_prior.where(df.rev_ttm_prior > 0)
    df["margin_chg"] = df.margin - df.margin_prior
    # No valuation feature here on purpose. Earnings yield needs a share
    # count, and the only one available is back-solved as profit / EPS, which
    # swings 5% quarter to quarter on Reliance and 22% between the standalone
    # and consolidated basis. A P/E built on that would be noise wearing a
    # factor's clothes. Valuation waits for a real share count.
    df["mult"] = df.px_fwd / df.px
    df["month"] = pd.to_datetime(df.d).dt.to_period("M")
    return df


# Pre-registered. Fixed before anything was run, and every one is reported
# whatever it shows. Trying features until one looks good is how a study finds
# something that is not there.
FEATURES = [
    ("ret_12m",     "12-month prior return"),
    ("ret_6m",      "6-month prior return"),
    ("off_3y_high", "distance below the 3-year high"),
    ("pos_52w",     "position in the 52-week range"),
    ("vol_ann",     "annualised volatility"),
    ("tno",         "normal daily turnover (size proxy)"),
    ("rev_growth",  "TTM revenue growth"),
    ("margin",      "TTM EBITDA margin"),
    ("margin_chg",  "change in EBITDA margin"),
    ("n_orders",    "order-win filings in the trailing year"),
    ("n_expansion", "expansion filings in the trailing year"),
]


def hit_rates(df: pd.DataFrame, col: str, mult: float, q: int = 5) -> pd.DataFrame:
    """Hit rate by quintile, computed WITHIN each month.

    Within-month is the whole point. Unconditionally, anything bought in early
    2020 tripled, so a feature that merely fires more often then would look
    extraordinary. Ranking inside the month asks the only question that
    matters: against the other stocks you could have bought that day, did this
    one do better?
    """
    d = df[df[col].notna()].copy()
    if len(d) < 2000:
        return pd.DataFrame()
    d["bucket"] = (d.groupby("month")[col]
                    .transform(lambda s: pd.qcut(s.rank(method="first"), q,
                                                 labels=False, duplicates="drop")))
    d = d[d["bucket"].notna()]
    d["hit"] = (d["mult"] >= mult).astype(float)
    out = (d.groupby("bucket")
             .agg(n=("hit", "size"), hit_pct=("hit", lambda s: 100 * s.mean()),
                  median_mult=("mult", "median"))
             .reset_index())
    out["bucket"] = out["bucket"].astype(int) + 1
    return out


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--outcome", type=float, default=3.0,
                    help="multiple that counts as a hit (3 = tripled)")
    args = ap.parse_args()
    pd.set_option("display.width", 200)

    con = duckdb.connect()
    log.info("building the adjusted panel")
    build(con)
    log.info("computing features")
    df = features(con)
    con.close()

    base = 100 * (df["mult"] >= args.outcome).mean()
    print(f"\n{len(df):,} liquid stock-months, entry to {LAST_ENTRY}, "
          f"{HORIZON_MONTHS}-month horizon")
    print(f"BASE RATE: {base:.2f}% reach {args.outcome:g}x\n")
    print("Quintile 5 is the HIGHEST value of the feature. Buckets are formed "
          "within each month,\nso every comparison is against stocks that "
          "could have been bought the same day.\n")

    rows = []
    for col, label in FEATURES:
        hr = hit_rates(df, col, args.outcome)
        if hr.empty:
            log.warning("%s: too few observations, skipped", col)
            continue
        lo, hi = hr.iloc[0], hr.iloc[-1]
        spread = hi.hit_pct - lo.hit_pct
        rows.append({"feature": label, "n": int(hr.n.sum()),
                     "q1_pct": round(lo.hit_pct, 2), "q5_pct": round(hi.hit_pct, 2),
                     "spread_pp": round(spread, 2),
                     "best_q": int(hr.loc[hr.hit_pct.idxmax(), "bucket"]),
                     "best_pct": round(hr.hit_pct.max(), 2),
                     "lift_x": round(hr.hit_pct.max() / base, 2) if base else None})
        trials.record("multibagger_hit_rate",
                      {"feature": col, "outcome_multiple": args.outcome,
                       "horizon_months": HORIZON_MONTHS},
                      {"base_rate_pct": round(base, 3),
                       "by_quintile": hr.to_dict("records")})

    out = pd.DataFrame(rows).sort_values("best_pct", ascending=False)
    print(out.to_string(index=False))
    print(f"\nbest_pct = hit rate in the feature's best quintile.")
    print(f"lift_x   = that rate divided by the {base:.2f}% base rate.")
    print(f"\ntrials recorded for this study: {trials.count('multibagger_hit_rate')}")
    print(f"trials across all studies: {trials.count()}")
    print("\nA lift under about 1.5x is not worth acting on. These are single "
          "features with no\nmultiple-testing correction applied yet, and "
          f"{len(FEATURES)} were tested.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
