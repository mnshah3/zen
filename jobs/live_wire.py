"""The day's filings as NSE publishes them, for the owner's site's wire (29 Oct 2026, the owner:
"where is the breaking news part that gives me updates"): live/filings.json.

    python -m jobs.live_wire                 # today's filings so far (IST)
    python -m jobs.live_wire --days 2        # and yesterday's

Run every ten minutes in market hours by .github/workflows/live_wire.yml, which publishes the file
alone on the `live` branch (one commit, replaced each run, so the repository's history does not
grow). The site reads it through its own /api/wire. Each filing: its time, the symbol, the company,
zen's category (zen.data.filing_types, as the archive), whether it is material for the day
(announcements.MATERIAL, less the call schedules, recordings and transcripts), the subject and NSE's link. Newest first; nothing is summarised.
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from zen.data import announcements, filing_types

OUT = Path("live/filings.json")
KEEP = 600                      # filings kept, newest first
IST = timezone(timedelta(hours=5, minutes=30))
# NSE subtypes the wire does not count as news though their category is material: the schedule of an
# analysts' call, its recording and its transcript (on 29 Oct 2026 most of the wire's "material"
# filings in the evening were "Link of Recording" and "Schedule of meet")
ROUTINE = {filing_types._norm(x) for x in (
    "Analysts/Institutional Investor Meet/Con. Call Updates",
    "Recording of Analysts/Institutional Investor Meet/Con. Call",
    "Transcript of Analysts/Institutional Investor Meet/Con. Call")}


def material(category: str, subject) -> bool:
    """Material for the wire: a material category (announcements.MATERIAL) and not a ROUTINE subtype."""
    return category in announcements.MATERIAL and filing_types._norm(filing_types.subtype(subject)) not in ROUTINE


def build(df) -> dict:
    df = df.sort_values("an_dt", ascending=False).head(KEEP)
    rows = [[r.an_dt.strftime("%Y-%m-%d %H:%M:%S"), r.symbol, r.company, r.category, material(r.category, r.subject),
             filing_types.gist(r.subject, 220), r.url] for r in df.itertuples()]
    return {"at": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"), "n": len(rows),
            "cols": ["t", "s", "n", "cat", "material", "gist", "url"], "rows": rows}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--out", default=str(OUT))
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    today = datetime.now(IST).date()
    df = announcements.fetch(today - timedelta(days=a.days - 1), today, strict=True)
    out = build(df)
    p = Path(a.out)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"wire: {out['n']} filings at {out['at']} IST")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
