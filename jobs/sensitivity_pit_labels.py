"""Does knowing a company's later industry label change any v1 decision?

Sector labels come from NSE announcements, which start in January 2022, so the
specification lets a company with no label published before the decision date
borrow its first-ever label (Clarification 8). The same backfill identifies
lenders for rule 4, alongside a similar backfill of NBFC filing taxonomy
(Clarification 28). Both are classifications, not prices or fundamentals, but
both are information from after the decision date.

The second audit reported 854 names against 823 at 2023-06-01 with and without
them. That was measured on the archive before the March 2025 refill and with
the audit's own reconstruction of the labels. This run, on the completed
archive and through the engine itself, finds no universe row in the held-back
period that depends on them: a backfilled label is a company's FIRST label,
published by 2022, so from 2023 every labelled company already has one known
before the date. The effect is before 2022, where it is large (a multiple of
2.63 as specified against 2.39 with point-in-time labels only, over 2019-2023
in the independent review) and unavoidable, because no point-in-time label
exists at all.

This re-runs the selected v1 configuration with those two backfills switched
off, so a company is labelled only by what had been published before each
decision date and is otherwise its own sector. It is a sensitivity check. The
rules do not change.

Only the final test is compared. Before 2022 there are no point-in-time labels
at all, so switching the backfill off there lets lenders into the universe and
measures a different strategy rather than the same one without hindsight.

    python -m jobs.sensitivity_pit_labels
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import replace
from pathlib import Path

import pandas as pd

from zen.portfolio import engine
from zen.universe import pit

log = logging.getLogger(__name__)

END = pd.Timestamp("2026-09-18")
SPLIT = pd.Timestamp("2023-02-15")
OUT = Path("data/backtest/sensitivity_pit_labels")


def run(con, static, decisions, is_end, cfg):
    ranks, _ = engine.build_ranks(con, decisions, static)
    panel = engine.build_panel(con, sorted(ranks["symbol"].unique()), decisions[0], END,
                               is_end, run_final_test=True, ids=static.ids)
    res = engine.run_strategy(panel, ranks, decisions, cfg, is_end, run_final_test=True)
    ew = engine.run_universe_ew(panel, ranks, decisions, cfg, is_end, run_final_test=True)
    return ranks, res, ew


def final_test(res) -> float:
    nav = res.nav.set_index("date")["nav"]
    v0 = res.rebalance_open[SPLIT]
    yrs = (nav.index[-1] - SPLIT).days / 365.25
    return (nav.iloc[-1] / v0) ** (1 / yrs) - 1


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    OUT.mkdir(parents=True, exist_ok=True)
    con = pit.connect()
    cal = pit.sessions(con)
    is_end = engine.in_sample_end(cal)
    decisions = [d for d in pit.decision_dates(cal, last=(END.year, END.month)) if d < END]
    cfg = engine.Config(n=10, buffer_mult=2, rebalance="quarterly", sector_cap=3,
                        cost=0.002, initial_capital=500_000)

    base = pit.StaticLabels.load(con)
    strict = replace(base, backfill_industry=pd.Series(dtype=object), taxonomy_fill=set())

    out = {}
    books, uni, srcs = {}, {}, {}
    for name, static in (("as_specified", base), ("point_in_time_only", strict)):
        log.info("running %s", name)
        ranks, res, ew = run(con, static, decisions, is_end, cfg)
        ft = ranks[ranks["D"] >= SPLIT]
        out[name] = {"final_test_strategy_cagr_pct": round(final_test(res) * 100, 2),
                     "final_test_universe_ew_cagr_pct": round(final_test(ew) * 100, 2),
                     "final_test_mean_universe": round(float(ft.groupby("D").size().mean()), 1)}
        h = res.holdings
        h["D"] = pd.to_datetime(h["D"])
        # The holdings log also lists the names being SOLD at each date, so the
        # book is the rows that are kept or bought, not every row.
        h = h[h["action"] != "sell"]
        books[name] = {D: set(g["symbol"]) for D, g in h[h["D"] >= SPLIT].groupby("D")}
        uni[name] = set(zip(ft["D"], ft["symbol"]))
        srcs[name] = ft["sector_source"].value_counts().to_dict() if "sector_source" in ft else {}

    diffs = []
    for D in sorted(books["as_specified"]):
        a, b = books["as_specified"][D], books["point_in_time_only"].get(D, set())
        if a != b:
            diffs.append({"D": str(D.date()), "only_as_specified": sorted(a - b),
                          "only_point_in_time": sorted(b - a)})
    out["final_test_dates_with_a_different_book"] = len(diffs)
    # Proof the switch did something: universe rows that differ, and where each
    # run's sector labels came from. Identical inputs would make any "no
    # difference" result meaningless.
    out["final_test_universe_rows_that_differ"] = len(uni["as_specified"] ^ uni["point_in_time_only"])
    out["sector_label_sources"] = srcs
    out["differences"] = diffs
    (OUT / "result.json").write_text(json.dumps(out, indent=2))
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
