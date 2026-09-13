"""Which 2025-26 filings are actually exposed to the segment-context bug?

Random sampling is the wrong test. The bug is deterministic: it only bites a
document that declares DIMENSIONED contexts covering the same period as the
company total, because the old parser selected on dates alone and a tie went to
whichever context appeared first. A document with no segment contexts cannot be
affected no matter how many times it is sampled, so a clean random draw says
very little.

This finds the exposed population first and tests that, which is a far stronger
statement than "48 random documents matched".

    python -m jobs.check_segment_risk --sample 120
"""

from __future__ import annotations

import argparse
import glob
import logging
import re
import sys
from xml.etree import ElementTree as ET

import pandas as pd

from zen.data.financials import _derive, parse_xbrl
from zen.data.financials_legacy import document_session

log = logging.getLogger(__name__)

CHECKED = ["revenue", "total_income", "ebitda", "pbt", "profit_reported",
           "profit_normalised", "eps_basic", "equity", "assets", "debt_total"]


def context_profile(content: bytes) -> dict:
    """How this document declares its contexts.

    `dimensioned` counts contexts carrying an explicit or typed member, which
    is what marks a segment rather than the company. `undeclared_refs` counts
    facts pointing at a context the document never declares, which is the
    2018-2024 pattern.
    """
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return {}

    declared, dimensioned = set(), set()
    for ctx in root.iter():
        if not (ctx.tag.endswith("}context") or ctx.tag == "context"):
            continue
        cid = ctx.get("id")
        if not cid:
            continue
        declared.add(cid)
        for node in ctx.iter():
            if node.tag.split("}")[-1] in ("explicitMember", "typedMember"):
                dimensioned.add(cid)
                break

    refs = set()
    for node in root.iter():
        ref = node.get("contextRef")
        if ref:
            refs.add(ref)

    return {"declared": len(declared),
            "dimensioned": len(dimensioned),
            "undimensioned": len(declared - dimensioned),
            "undeclared_refs": len(refs - declared),
            "exposed": len(dimensioned) > 0}


def material(old, new, tol: float = 0.01) -> bool:
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
    ap.add_argument("--sample", type=int, default=120)
    args = ap.parse_args()

    files = [f for f in glob.glob("data/financials/*.parquet") if "legacy_" not in f]
    stored = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    stored = stored[stored["xbrl_url"].notna()].reset_index(drop=True)

    # Spread across periods so a change in NSE's template partway through the
    # period cannot hide.
    per = max(1, args.sample // stored["period_end"].nunique())
    sample = (stored.groupby(stored["period_end"].astype(str), group_keys=False)
                    .apply(lambda g: g.sample(min(per, len(g)), random_state=11)))
    log.info("profiling %d documents from %d stored rows", len(sample), len(stored))

    s = document_session()
    profiles, drift, failed = [], [], 0

    for n, (_, old) in enumerate(sample.iterrows(), 1):
        try:
            r = s.get(old["xbrl_url"], timeout=25)
            content = r.content if r.status_code == 200 else b""
        except Exception:                                        # noqa: BLE001
            content = b""
        if not content:
            failed += 1
            continue

        prof = context_profile(content)
        if not prof:
            failed += 1
            continue
        prof["period"] = str(old["period_end"])[:10]
        profiles.append(prof)

        if prof["exposed"]:
            facts = parse_xbrl(content)
            if facts:
                new = _derive(dict(facts))
                diffs = [c for c in CHECKED if material(old.get(c), new.get(c))]
                drift.append({"symbol": old["symbol"], "period": prof["period"],
                              "dimensioned": prof["dimensioned"],
                              "n_diff": len(diffs), "fields": ",".join(diffs)})
        if n % 30 == 0:
            log.info("  %d/%d", n, len(sample))
    s.close()

    if not profiles:
        print("\nNothing could be profiled.")
        return 1

    pf = pd.DataFrame(profiles)
    pd.set_option("display.width", 200)

    print(f"\nprofiled {len(pf)} documents ({failed} unreadable)\n")
    print("context structure:")
    print(f"  documents WITH segment contexts (exposed) : "
          f"{int(pf.exposed.sum())}  ({100*pf.exposed.mean():.1f}%)")
    print(f"  documents with undeclared context refs     : "
          f"{int((pf.undeclared_refs > 0).sum())}")
    print(f"  median declared contexts                   : {pf.declared.median():.0f}")
    print(f"  median dimensioned contexts                : {pf.dimensioned.median():.0f}")

    print("\nby period:")
    print(pf.groupby("period").agg(docs=("exposed", "size"),
                                   exposed=("exposed", "sum"),
                                   undeclared=("undeclared_refs",
                                               lambda x: int((x > 0).sum()))
                                   ).to_string())

    if not drift:
        print("\nNo exposed documents found in this sample, so nothing to compare.")
        print("VERDICT: the 2025-26 scheme does not carry segment contexts. "
              "No rebuild needed.")
        return 0

    df = pd.DataFrame(drift)
    bad = df[df.n_diff > 0]
    print(f"\nexposed documents re-parsed: {len(df)}")
    print(f"of those, differing from stored: {len(bad)}")
    if len(bad):
        print(bad.head(10).to_string(index=False))
        print("\nVERDICT: exposed documents DO drift. Rebuild 2025-26.")
        return 2

    print("\nVERDICT: even the exposed documents match what is stored. "
          "No rebuild needed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
