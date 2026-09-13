"""Does the segment-context fix change the 2025 filings, and by how much?

The 2018-2024 filings were definitely affected: they declare only dimensioned
segment contexts and reference company totals through an undeclared one, so the
old parser read a division's revenue as the company's. The 2025 scheme declares
its contexts properly, so it may or may not have the same problem.

Re-fetching 22,000 documents on the assumption that it does would be an hour
spent on a guess. This samples the stored rows, re-parses the same documents
with the current parser, and reports what actually differs.

    python -m jobs.check_parser_drift --sample 40
"""

from __future__ import annotations

import argparse
import glob
import logging
import random
import sys

import pandas as pd

from zen.data.financials import _derive, parse_xbrl
from zen.data.financials_legacy import document_session

log = logging.getLogger(__name__)

# Every field a difference could hide in, not a chosen ten. The first version
# of this list omitted other_income and liabilities, and 55 of the 56 real
# differences turned out to sit in exactly those two. A comparison that picks
# its own fields can only find what its author already suspected.
CHECKED = ["revenue", "other_income", "total_income", "materials",
           "employee_cost", "finance_costs", "depreciation", "other_expenses",
           "total_expenses", "pbt_before_exceptional", "exceptional_items",
           "pbt", "tax", "profit_continuing", "profit_reported", "eps_basic",
           "eps_diluted", "debt_long", "debt_short", "equity", "equity_capital",
           "other_equity", "assets", "liabilities", "current_assets",
           "current_liabilities", "noncurrent_assets", "noncurrent_liabilities",
           "ebitda", "profit_normalised", "debt_total"]


def material(old, new, tol: float = 0.01) -> bool:
    """Different enough to matter.

    A tolerance rather than equality because floats reread from parquet can
    differ in the last bits. One per cent is far below any real parsing error,
    which substitutes one number for a completely different one.
    """
    if pd.isna(old) and pd.isna(new):
        return False
    if pd.isna(old) or pd.isna(new):
        return True
    if old == 0 and new == 0:
        return False
    base = max(abs(float(old)), abs(float(new)))
    return abs(float(old) - float(new)) / base > tol


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=40)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    files = [f for f in glob.glob("data/financials/*.parquet") if "legacy_" not in f]
    stored = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    stored = stored[stored["xbrl_url"].notna()]
    log.info("stored new-scheme rows: %d across %d files", len(stored), len(files))

    # Sampled evenly across periods, not at random, so a drift confined to one
    # quarter cannot hide behind a lucky draw.
    rng = random.Random(args.seed)
    picks = []
    for _, grp in stored.groupby(stored["period_end"].astype(str)):
        n = max(1, args.sample // stored["period_end"].nunique())
        picks.append(grp.sample(min(n, len(grp)), random_state=rng.randrange(10**6)))
    sample = pd.concat(picks, ignore_index=True)
    log.info("checking %d documents", len(sample))

    s = document_session()
    rows, fetch_fail, unparsed = [], 0, 0

    for n, (_, old) in enumerate(sample.iterrows(), 1):
        content = b""
        try:
            r = s.get(old["xbrl_url"], timeout=25)
            content = r.content if r.status_code == 200 else b""
        except Exception:                                        # noqa: BLE001
            pass
        if not content:
            fetch_fail += 1
            continue
        facts = parse_xbrl(content)
        if not facts:
            # The document arrived and the CURRENT parser got nothing from it,
            # while the old parser produced a stored row. That is the worst
            # outcome available and the first version of this script booked it
            # as a network failure and dropped it from the denominator.
            unparsed += 1
            rows.append({"symbol": old["symbol"], "period": str(old["period_end"])[:10],
                         "consolidated": bool(old["consolidated"]),
                         "n_diff": len(CHECKED), "fields": "ALL (parser returned nothing)",
                         "old_revenue": old.get("revenue"), "new_revenue": None})
            continue

        new = _derive(dict(facts))
        diffs = [c for c in CHECKED if material(old.get(c), new.get(c))]
        rows.append({"symbol": old["symbol"], "period": str(old["period_end"])[:10],
                     "consolidated": bool(old["consolidated"]),
                     "n_diff": len(diffs), "fields": ",".join(diffs),
                     "old_revenue": old.get("revenue"), "new_revenue": new.get("revenue")})
        if n % 20 == 0:
            log.info("  %d/%d", n, len(sample))
    s.close()

    if not rows:
        print("\nNo documents could be re-parsed. Cannot conclude anything.")
        return 1

    out = pd.DataFrame(rows)
    changed = out[out.n_diff > 0]
    pd.set_option("display.width", 200)

    print(f"\nre-parsed      {len(out)} of {len(sample)} sampled "
          f"({fetch_fail} could not be fetched)")
    print(f"rows differing {len(changed)}  ({100*len(changed)/len(out):.1f}%)")

    if len(changed):
        print("\nfields affected:")
        from collections import Counter
        c = Counter(f for fs in changed.fields for f in fs.split(",") if f)
        for field, n in c.most_common():
            print(f"  {field:20} {n}")
        print("\nexamples:")
        print(changed.head(8).to_string(index=False))
        print("\nVERDICT: the 2025-26 files need rebuilding.")
        return 2

    print("\nVERDICT: no material drift. The stored 2025-26 rows already match "
          "what the current parser produces, so a rebuild would change nothing.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
