"""Re-parse the 2025-26 filings with the corrected parser, and show the diff first.

WHY THIS RUNS AT ALL

The stored rows are old-parser output by construction: the parquets were written
on 8 September and the dimension-skip fix landed on 10 September. The only
question was whether the old output happened to coincide with the new. Mostly it
does, and three independent checks found roughly 110-200 rows of 22,522 where it
does not.

TWO KINDS OF DIFFERENCE, RUNNING IN OPPOSITE DIRECTIONS

  Stored is WRONG. Every life insurer tags OtherIncome only inside segment
  contexts, so the old parser banked segment one as the company. LICI's stored
  figure is byte-identical to ReportableSegmentsMember1.

  Stored is RIGHT and a naive rebuild would destroy it. Some small caps tag
  Assets and Liabilities only under AuditedOrAdjustedAxis, which is SEBI's
  audit-qualification disclosure rather than a business segment. The parser now
  falls back to AuditedMember when no undimensioned fact exists, so those
  survive -- but that fix has to be in place BEFORE this job runs.

NOTHING IS OVERWRITTEN UNSEEN

The job writes to a staging directory, diffs every field against what is
stored, prints the result, and only replaces the live files when run with
--apply. A rebuild that silently changed twenty thousand rows would be worse
than the bug it fixes.

    python -m jobs.rebuild_2025_financials              # fetch and diff only
    python -m jobs.rebuild_2025_financials --apply      # and replace the files
"""

from __future__ import annotations

import argparse
import glob
import logging
import shutil
import sys
import time
from collections import Counter
from pathlib import Path

import pandas as pd

from zen.data.financials import _derive, parse_xbrl
from zen.data.financials_legacy import REWARM_SECONDS, document_session

log = logging.getLogger(__name__)

STAGE = Path("data/financials/.rebuild")

# Every numeric field, not a chosen subset. The earlier drift checks compared
# ten fields and omitted other_income and liabilities -- which is where 55 of
# the 56 known differences turned out to live. A diff that picks its own fields
# can only find what it already suspects.
SKIP = {"symbol", "company", "period_end", "broadcast_dt", "consolidated",
        "audited", "xbrl_url", "has_balance_sheet"}


def differs(old, new, tol: float = 0.005) -> bool:
    if pd.isna(old) and pd.isna(new):
        return False
    if pd.isna(old) or pd.isna(new):
        return True
    try:
        o, n = float(old), float(new)
    except (TypeError, ValueError):
        return old != new
    if o == n:
        return False
    base = max(abs(o), abs(n))
    return base > 0 and abs(o - n) / base > tol


def fetch_all(stored: pd.DataFrame, workers: int, rate: float) -> pd.DataFrame:
    """Re-fetch and re-parse every document, concurrently."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    local = threading.local()
    gate = threading.Lock()
    slot = [time.monotonic()]
    interval = 1.0 / rate if rate > 0 else 0.0

    def sess():
        born = getattr(local, "born", 0)
        if not getattr(local, "s", None) or time.monotonic() - born > REWARM_SECONDS:
            if getattr(local, "s", None):
                local.s.close()
            local.s = document_session()
            local.born = time.monotonic()
        return local.s

    def one(meta):
        with gate:
            now = time.monotonic()
            wait = max(0.0, slot[0] - now)
            slot[0] = max(now, slot[0]) + interval
        if wait:
            time.sleep(wait)

        for attempt in range(3):
            try:
                r = sess().get(meta["xbrl_url"], timeout=25)
                if r.status_code == 404:
                    return {"xbrl_url": meta["xbrl_url"], "_outcome": "missing"}
                if r.status_code == 200:
                    facts = parse_xbrl(r.content)
                    if not facts:
                        # NOT a fetch failure. The fixed parser returning
                        # nothing for a document that produced a stored row is
                        # the single worst outcome here, and the earlier checks
                        # booked it as a network problem and dropped it from
                        # the denominator.
                        return {"xbrl_url": meta["xbrl_url"], "_outcome": "unparsed"}
                    row = {"symbol": meta["symbol"], "company": meta.get("company"),
                           "period_end": meta["period_end"],
                           "broadcast_dt": meta["broadcast_dt"],
                           "consolidated": bool(meta["consolidated"]),
                           "audited": bool(meta.get("audited", False)),
                           "xbrl_url": meta["xbrl_url"], **facts}
                    out = _derive(row)
                    out["_outcome"] = "ok"
                    return out
            except Exception:                                    # noqa: BLE001
                pass
            if getattr(local, "s", None):
                local.s.close()
                local.s = None
            time.sleep(1.0 * (attempt + 1))
        return {"xbrl_url": meta["xbrl_url"], "_outcome": "failed"}

    rows, done = [], 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for row in pool.map(one, (m for _, m in stored.iterrows())):
            rows.append(row)
            done += 1
            if done % 500 == 0:
                log.info("  %d/%d", done, len(stored))
    return pd.DataFrame(rows)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="replace the live files")
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--rate", type=float, default=8.0)
    args = ap.parse_args()

    files = sorted(f for f in glob.glob("data/financials/*.parquet")
                   if "legacy_" not in f and ".rebuild" not in f)
    stored = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    stored = stored[stored["xbrl_url"].notna()].reset_index(drop=True)
    log.info("re-parsing %d stored rows from %d files", len(stored), len(files))

    STAGE.mkdir(parents=True, exist_ok=True)
    cache = STAGE / "reparsed.parquet"
    if cache.exists():
        fresh = pd.read_parquet(cache)
        log.info("resuming from %s (%d rows already fetched)", cache.name, len(fresh))
        todo = stored[~stored["xbrl_url"].isin(fresh["xbrl_url"])]
        if len(todo):
            log.info("fetching the remaining %d", len(todo))
            fresh = pd.concat([fresh, fetch_all(todo, args.workers, args.rate)],
                              ignore_index=True)
            fresh.to_parquet(cache, index=False)
    else:
        fresh = fetch_all(stored, args.workers, args.rate)
        fresh.to_parquet(cache, index=False)

    counts = Counter(fresh["_outcome"])
    log.info("outcomes: %s", dict(counts))

    ok = fresh[fresh["_outcome"] == "ok"].set_index("xbrl_url")
    old = stored.set_index("xbrl_url")
    both = old.index.intersection(ok.index)
    fields = [c for c in old.columns if c not in SKIP]

    changes = []
    for url in both:
        o, n = old.loc[url], ok.loc[url]
        for f in fields:
            if f in n.index and differs(o.get(f), n.get(f)):
                changes.append({"symbol": o["symbol"],
                                "period": str(o["period_end"])[:10],
                                "consolidated": bool(o["consolidated"]),
                                "field": f, "stored": o.get(f), "reparsed": n.get(f)})

    pd.set_option("display.width", 220)
    print(f"\nre-parsed {len(ok):,} of {len(stored):,} documents")
    for k, v in sorted(counts.items()):
        print(f"  {k:10} {v:,}")

    if not changes:
        print("\nNo field changed. The stored rows already match the corrected parser.")
        return 0

    ch = pd.DataFrame(changes)
    rows_changed = ch[["symbol", "period", "consolidated"]].drop_duplicates()
    print(f"\n{len(ch):,} field changes across {len(rows_changed):,} rows "
          f"({100*len(rows_changed)/len(both):.2f}% of documents)")

    print("\nby field:")
    for field, n in Counter(ch["field"]).most_common():
        gained = int(ch[(ch.field == field) & ch.stored.isna()].shape[0])
        lost = int(ch[(ch.field == field) & ch.reparsed.isna()].shape[0])
        print(f"  {field:24} {n:5}   gained {gained:4}   lost {lost:4}   "
              f"changed {n - gained - lost:4}")

    print("\nmost affected companies:")
    for sym, n in Counter(ch["symbol"]).most_common(12):
        print(f"  {sym:14} {n}")

    print("\nsample of changes:")
    print(ch.head(15).to_string(index=False))

    ch.to_csv(STAGE / "changes.csv", index=False)
    print(f"\nfull diff written to {STAGE / 'changes.csv'}")

    if not args.apply:
        print("\nDry run. Re-run with --apply to replace the live files.")
        return 0

    written = 0
    for f in files:
        live = pd.read_parquet(f)
        keep = live[~live["xbrl_url"].isin(ok.index)]
        take = ok.loc[ok.index.intersection(live["xbrl_url"])].reset_index()
        take = take.drop(columns=["_outcome"], errors="ignore")
        merged = pd.concat([keep, take[live.columns.intersection(take.columns)]],
                           ignore_index=True)
        merged.to_parquet(f, index=False, compression="zstd")
        written += len(merged)
        log.info("rewrote %s (%d rows)", Path(f).name, len(merged))
    print(f"\napplied: {written:,} rows rewritten across {len(files)} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
