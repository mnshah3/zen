"""Can order wins be rescued from the vague buckets without reintroducing the bug?

THE PROBLEM THIS IS TRYING TO SOLVE

NSE labels a filing's type only from 2024-09-23. Before that, and for many
companies after it, an order win is filed under "Updates" or "Press Release"
with the substance in the free text. An audit found 2,299 such filings with no
labelled twin -- against a labelled bucket of 3,220. So roughly 40% of
identifiable order wins are invisible to a subtype-only mapping.

THE REASON THIS IS DANGEROUS

Recovering them means matching free text, which is precisely what produced the
previous categoriser's `orders` bucket: 14,955 filings of which a quarter were
regulatory penalties with the opposite sign. Doing that again, but calling it a
rescue, would be the same mistake with better intentions.

WHAT MAKES IT TESTABLE THIS TIME

From 2024-09-23 the exchange's own label and the free text both exist. That is
ground truth. A candidate pattern can be measured against it:

  RECALL    of filings NSE itself labelled an order win, what share does the
            pattern catch? A pattern that misses most of them will not rescue
            the unlabelled years either.

  PRECISION of filings the pattern flags inside VAGUE buckets, what share are
            genuinely order wins? This needs judgement on real text, so this
            job writes a sample out for adjudication rather than scoring it
            itself. A script marking its own homework is how the last bucket
            passed.

  ANTI-TEST what share of REGULATORY filings does the pattern also catch? The
            old regex matched "orders passed" against companies, which is the
            opposite sign. Any pattern that touches regulatory at all is
            disqualified rather than tuned.

    python -m jobs.calibrate_rescue
"""

from __future__ import annotations

import logging
import re
import sys
from datetime import date

import duckdb
import pandas as pd

from zen.data.filing_types import categorise, subtype

log = logging.getLogger(__name__)

# The exchange began labelling filing types on this date. Before it, absence of
# a label means nothing; after it, absence is informative.
LABELS_FROM = date(2024, 9, 23)

# Candidate patterns, narrowest first. Each is tested, none is assumed. They
# describe the EVENT (a company receiving work) rather than the word "order",
# which is what the previous attempt got wrong -- "in order to ensure" appears
# in 39% of every filing containing the word.
CANDIDATES = {
    "strict": r"(receipt|received|receiving|secur(ed|ing)|bag(ged|ging)|"
              r"award(ed|ing)?)\s+(of\s+)?(a\s+|an\s+|the\s+|new\s+|"
              r"fresh\s+|major\s+|large\s+)?"
              r"(work\s+)?(order|contract|letter\s+of\s+(award|intent)|loa\b|loi\b)",

    "moderate": r"(receipt|received|receiving|secur(ed|ing)|bag(ged|ging)|"
                r"award(ed|ing)?|win(s|ning)?|won)\s+"
                r"(\w+\s+){0,3}?"
                r"(work\s+order|purchase\s+order|order|contract|"
                r"letter\s+of\s+(award|intent)|\bloa\b|\bloi\b|tender)",

    "loose":  r"\b(order|contract|letter\s+of\s+award|\bloa\b|tender)\b",
}


def body(subject: str) -> str:
    _, sep, rest = (subject or "").partition(": ")
    return (rest if sep else subject or "").lower()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    pd.set_option("display.width", 210)

    con = duckdb.connect()
    con.execute("""CREATE VIEW a AS SELECT * FROM
                   read_parquet('data/announcements/**/*.parquet', union_by_name=true)""")
    df = con.execute("SELECT symbol, trade_date, subject FROM a").df()
    df["cat"] = df["subject"].map(categorise)
    df["sub"] = df["subject"].map(subtype)
    df["body"] = df["subject"].map(body)
    df["labelled_era"] = pd.to_datetime(df["trade_date"]).dt.date >= LABELS_FROM

    era = df[df["labelled_era"]]
    truth = era[era["cat"] == "orders"]
    vague = era[era["cat"] == "other"]
    regul = era[era["cat"] == "regulatory"]

    print(f"\nlabelled era, from {LABELS_FROM}: {len(era):,} filings")
    print(f"  NSE-labelled order wins : {len(truth):,}")
    print(f"  vague bucket            : {len(vague):,}")
    print(f"  regulatory              : {len(regul):,}")

    print("\n" + "=" * 78)
    print(f"{'pattern':10} {'recall':>8} {'reg hits':>10} {'vague flagged':>15} {'implied':>9}")
    print("=" * 78)

    results = {}
    for name, pat in CANDIDATES.items():
        rx = re.compile(pat, re.I)
        rec = truth["body"].str.contains(rx, regex=True, na=False)
        reg = regul["body"].str.contains(rx, regex=True, na=False)
        vag = vague["body"].str.contains(rx, regex=True, na=False)
        results[name] = {"recall": rec.mean(), "reg_rate": reg.mean(),
                         "vague_n": int(vag.sum()), "mask": vag}
        print(f"{name:10} {100*rec.mean():7.1f}% {100*reg.mean():9.2f}% "
              f"{int(vag.sum()):15,} {int(vag.sum())/max(1,len(truth)):8.2f}x")

    print("\nrecall   = share of NSE-labelled order wins the pattern catches")
    print("reg hits = share of REGULATORY filings it also catches. Anything")
    print("           meaningfully above zero is disqualified, not tuned.")
    print("implied  = flagged vague filings per labelled order win. A pattern")
    print("           flagging several times the labelled population is")
    print("           almost certainly matching something else.")

    # What the strict pattern MISSES among known order wins, which is the
    # honest way to see where a rescue would fall short.
    rx = re.compile(CANDIDATES["strict"], re.I)
    missed = truth[~truth["body"].str.contains(rx, regex=True, na=False)]
    print(f"\n--- {len(missed):,} labelled order wins the strict pattern misses ---")
    for s in missed["subject"].head(8):
        print("   ", s[:112])

    # A sample for adjudication. Not scored here on purpose.
    out = pd.DataFrame()
    for name in ("strict", "moderate"):
        m = results[name]["mask"]
        s = vague[m].sample(min(120, int(m.sum())), random_state=5).copy()
        s["pattern"] = name
        out = pd.concat([out, s], ignore_index=True)
    out = out[["pattern", "symbol", "trade_date", "sub", "subject"]]
    out.to_csv("data/study/rescue_sample.csv", index=False)
    print(f"\nwrote {len(out)} flagged vague filings to data/study/rescue_sample.csv")
    print("Precision is NOT scored here. It needs judgement on the real text,")
    print("and a script that scores its own pattern will always pass itself.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
