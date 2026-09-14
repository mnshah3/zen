"""Do the category signs match what prices actually did?

SIGN in zen/data/filing_types.py asserts that order wins and expansion move a
price up, that contraction and regulatory move it down, and that the rest are
directionless. Nothing has ever tested that. An asserted sign that the data
contradicts is worse than no sign, because it licenses pooling events that move
in opposite directions.

WHAT THIS MEASURES, AND WHAT IT DELIBERATELY DOES NOT

It measures DIRECTION over a short window, day 0 to day +3. That is long enough
for a filing to be absorbed and short enough that the result is about the
filing rather than about the following quarter.

It is not looking for an edge and does not scan horizons. Scanning horizons
until something looks good is how the previous order-win result happened, and
the trial log exists because of it. One window, fixed in advance, reported
whatever it shows.

THREE THINGS THAT WOULD OTHERWISE MAKE THIS WRONG

  Corporate actions. `prev_close` in the bhavcopy is unadjusted; the median
  implied return on an ex-date using it is -52.8%. Returns here are computed
  from an adjusted close series, never from prev_close.

  Holidays. Events key on `session_date`, the first session the symbol actually
  traded, not `trade_date`. Joining on trade_date silently discards 10.7% of
  filings, skewed towards those filed before long weekends.

  The taxonomy change. Each category is restricted to USABLE_FROM. Before that
  year, absence of a label means the exchange was not applying it, not that the
  event did not happen.

THE CONTROL IS THE POINT

A category's median return means nothing on its own -- in a rising market every
bucket is positive. Each event is matched to a control stock on the SAME
session with similar normal turnover, because an unmatched baseline once turned
a size effect into a signal in this project and the finding survived for weeks.

    python -m jobs.study_category_signs
"""

from __future__ import annotations

import argparse
import logging
import sys

import duckdb
import pandas as pd

from zen.data.filing_types import SIGN, USABLE_FROM

log = logging.getLogger(__name__)

HOLD_SESSIONS = 3          # day 0 close to day +3 close
MIN_TURNOVER = 1e7         # Rs 1 crore of normal daily turnover
MIN_EVENTS = 100


def open_archive() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("""CREATE VIEW prices AS SELECT * FROM
        read_parquet('data/daily/**/*.parquet', union_by_name=true)""")
    con.execute("""CREATE VIEW ann AS SELECT * FROM
        read_parquet('data/announcements/**/*.parquet', union_by_name=true)""")
    con.execute("""CREATE VIEW ca AS SELECT * FROM
        read_parquet('data/corpactions/*.parquet', union_by_name=true)""")
    return con


def build_panel(con) -> None:
    """Adjusted closes, forward returns and normal turnover, once."""
    # Factors collapsed by (symbol, ex_date) with a product first: a split and
    # a bonus routinely share an ex-date in India and applying only one of them
    # is a 5x price error.
    con.execute("""
        CREATE OR REPLACE TEMP TABLE factors AS
        SELECT symbol, CAST(ex_date AS DATE) AS ex_date,
               exp(sum(ln(factor))) AS f
        FROM ca WHERE factor IS NOT NULL AND factor > 0
        GROUP BY 1, 2
    """)
    # cum_factor at date d is the product of every factor going ex AFTER d, so
    # a pre-split price is expressed in post-split terms.
    con.execute("""
        CREATE OR REPLACE TEMP TABLE panel AS
        WITH base AS (
            SELECT p.symbol, CAST(p.date AS DATE) AS d, p.close, p.turnover
            FROM prices p
            WHERE p.isin_code LIKE 'INE%' AND p.series IN ('EQ','BE')
              AND p.close > 0 AND p.turnover > 0
        ),
        adj AS (
            SELECT b.symbol, b.d, b.turnover,
                   b.close * coalesce(
                       (SELECT exp(sum(ln(f.f))) FROM factors f
                        WHERE f.symbol = b.symbol AND f.ex_date > b.d), 1.0
                   ) AS px
            FROM base b
        )
        SELECT symbol, d, px, turnover,
               lag(px, 1) OVER (PARTITION BY symbol ORDER BY d) AS px_prev,
               lead(px, ?) OVER (PARTITION BY symbol ORDER BY d) AS px_fwd,
               median(turnover) OVER (PARTITION BY symbol ORDER BY d
                   ROWS BETWEEN 60 PRECEDING AND 1 PRECEDING) AS normal_turnover
        FROM adj
    """, [HOLD_SESSIONS])


def measure(con, category: str) -> dict | None:
    from_year = USABLE_FROM.get(category, 2022)
    rows = con.execute("""
        WITH ev AS (
            SELECT DISTINCT a.symbol, CAST(a.session_date AS DATE) AS d
            FROM ann a
            WHERE a.category = ?
              AND a.session_date IS NOT NULL
              AND year(a.session_date) >= ?
        ),
        scored AS (
            SELECT p.symbol, p.d,
                   p.px / p.px_prev - 1 AS ret0,
                   p.px_fwd / p.px - 1  AS ret,
                   p.normal_turnover
            FROM ev JOIN panel p ON p.symbol = ev.symbol AND p.d = ev.d
            WHERE p.px_fwd IS NOT NULL AND p.px_prev IS NOT NULL
              AND p.normal_turnover >= ?
        ),
        -- One control per event: same session, nearest normal turnover, not
        -- the event stock itself and not itself an event that day.
        ctrl AS (
            SELECT s.symbol AS ev_sym, s.d,
                   (SELECT c.px / c.px_prev - 1
                    FROM panel c
                    WHERE c.d = s.d AND c.symbol <> s.symbol
                      AND c.px_fwd IS NOT NULL AND c.px_prev IS NOT NULL
                      AND c.normal_turnover >= ?
                      AND c.symbol NOT IN (SELECT symbol FROM ev WHERE ev.d = s.d)
                    ORDER BY abs(ln(c.normal_turnover) - ln(s.normal_turnover))
                    LIMIT 1) AS ret0,
                   (SELECT c.px_fwd / c.px - 1
                    FROM panel c
                    WHERE c.d = s.d AND c.symbol <> s.symbol
                      AND c.px_fwd IS NOT NULL
                      AND c.normal_turnover >= ?
                      AND c.symbol NOT IN (SELECT symbol FROM ev WHERE ev.d = s.d)
                    ORDER BY abs(ln(c.normal_turnover) - ln(s.normal_turnover))
                    LIMIT 1) AS ret
            FROM scored s
        )
        SELECT (SELECT count(*) FROM scored)                       AS n,
               (SELECT median(ret0) FROM scored) * 100             AS ev_day0,
               (SELECT median(ret0) FROM ctrl WHERE ret0 IS NOT NULL) * 100 AS ct_day0,
               (SELECT median(ret) FROM scored) * 100              AS ev_med,
               (SELECT median(ret) FROM ctrl WHERE ret IS NOT NULL) * 100 AS ct_med,
               (SELECT avg(CASE WHEN ret > 0 THEN 1.0 ELSE 0 END) FROM scored) * 100 AS ev_up,
               (SELECT avg(CASE WHEN ret > 0 THEN 1.0 ELSE 0 END)
                  FROM ctrl WHERE ret IS NOT NULL) * 100           AS ct_up,
               (SELECT median(normal_turnover) FROM scored) / 1e7  AS liq_cr
    """, [category, from_year, MIN_TURNOVER, MIN_TURNOVER, MIN_TURNOVER]).df()
    r = rows.iloc[0]
    if pd.isna(r["n"]) or int(r["n"]) < MIN_EVENTS:
        return None
    return {"category": category, "from": from_year, "n": int(r["n"]),
            "liq_cr": round(float(r["liq_cr"]), 1),
            "day0_pp": round(float(r["ev_day0"] - r["ct_day0"]), 3),
            "event_pct": round(float(r["ev_med"]), 3),
            "control_pct": round(float(r["ct_med"]), 3),
            "gap_pp": round(float(r["ev_med"] - r["ct_med"]), 3),
            "event_up_pct": round(float(r["ev_up"]), 1),
            "control_up_pct": round(float(r["ct_up"]), 1)}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--categories", nargs="*", default=None)
    args = ap.parse_args()
    pd.set_option("display.width", 220)

    con = open_archive()
    log.info("building the adjusted price panel")
    build_panel(con)

    cats = args.categories or [c for c in SIGN if c not in
                               ("routine", "other", "governance", "corp_action")]
    out = []
    for c in cats:
        log.info("measuring %s", c)
        r = measure(con, c)
        if r:
            r["asserted_sign"] = SIGN[c]
            out.append(r)
        else:
            log.warning("  %s: too few events after filters, skipped", c)
    con.close()

    if not out:
        print("nothing measurable")
        return 1

    df = pd.DataFrame(out).sort_values("day0_pp", ascending=False)

    def verdict(row):
        # The sign is a claim about the REACTION to the filing, which is day 0.
        # The forward window measures what happens after the market has already
        # absorbed it, and those are different questions. Judging the sign on
        # the forward window alone would call a filing bearish purely because
        # its pop faded.
        d0, sign = row["day0_pp"], row["asserted_sign"]
        if abs(d0) < 0.15:
            return "flat" if sign == 0 else "NOT SUPPORTED"
        if sign == 0:
            return "moves, sign says 0"
        return "supported" if (d0 > 0) == (sign > 0) else "CONTRADICTED"

    df["verdict"] = df.apply(verdict, axis=1)
    print("\n" + df.to_string(index=False))
    print(f"\nreturn from the close of the filing session to {HOLD_SESSIONS} "
          f"sessions later, adjusted for corporate actions.")
    print("control = one stock per event, same session, nearest normal turnover.")
    print("gap_pp  = event median minus control median, in percentage points.")
    print("\nA gap under 0.15pp is treated as flat. These are medians over a "
          "3-session window,\nso a real effect should be visible without "
          "hunting for a horizon that flatters it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
