"""The day's filings as NSE publishes them, for the owner's site's wire (29 Oct 2026, the owner:
"where is the breaking news part that gives me updates"): live/filings.json.

    python -m jobs.live_wire                 # today's filings so far (IST)
    python -m jobs.live_wire --days 2        # and yesterday's

Run every five minutes by .github/workflows/live_wire.yml, which publishes the file alone on the
`live` branch (one commit, replaced each run, so the repository's history does not grow). The site
reads it through its own /api/wire. Each filing: its time, the symbol, the company, zen's category
(zen.data.filing_types, as the archive), whether it is material for the day (announcements.MATERIAL,
less the call schedules, recordings and transcripts), the subject and NSE's link. Newest first;
nothing is summarised.

THE RESULTS' FIGURES (2 Nov 2026, the owner: "I didn't even know Reliance had results")

    python -m jobs.live_wire --results-out live/results.json --prev live/results.json

Reliance filed its September 2026 quarter on Friday 30 Oct: the PDF at 19:40 IST, the XBRL with the
figures at 21:32, after the evening's results job (20:15) had run, and nothing ran at the weekend, so
the site had no figures until Monday night. With --results-out the wire also reads NSE's financial
results feed (zen.data.financials.listing, the filings the archive keeps) for the last LIST_DAYS
days, parses each XBRL not read before (financials.parse_xbrl, as the archive does), and writes
live/results.json: the quarter's revenue (net of excise duty where filed), EBITDA, profit before tax,
profit as reported, EPS and exceptional items, in crore, beside the same quarter a year earlier and
the quarter before from the archive's parquet files (the latest revision of each, on the same
basis), for every filing of the last KEEP_DAYS days. A filing read once is not read again: --prev is
the file the last round wrote. Nothing here goes into the archive; the evening job still files
everything with its broadcast time.
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from zen.data import announcements, filing_types

OUT = Path("live/filings.json")
KEEP = 2000                     # filings kept, newest first (2 Nov 2026: 600 reached back only to 13:08 in results season)
LIST_DAYS = 2                   # days of NSE's results feed read each round
KEEP_DAYS = 4                   # days of results kept, so Friday evening's are still there on Monday
FIN_DIR = Path("data/financials")
RESULT_COLS = ["t", "s", "n", "pe", "c", "rev", "ebitda", "pbt", "np", "eps", "exc",
               "rev_py", "ebitda_py", "np_py", "eps_py", "pe_py", "rev_pq", "ebitda_pq", "np_pq", "eps_pq", "pe_pq", "u"]
CR = 1e7
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


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _cr(v):
    v = _num(v)
    return None if v is None else round(v / CR, 2)


def figures(f: dict) -> dict:
    """A parsed filing's headline figures in crore (EPS in rupees). Revenue is net of excise duty
    where the filing breaks it out, as the site's quarterly tables show it."""
    rev, exc = _num(f.get("revenue")), _num(f.get("excise_duty"))
    if rev is not None and exc:
        rev -= exc
    eps = _num(f.get("eps_basic"))
    return {"rev": None if rev is None else round(rev / CR, 2), "ebitda": _cr(f.get("ebitda")), "pbt": _cr(f.get("pbt")),
            "np": _cr(f.get("profit_reported")), "eps": None if eps is None else round(eps, 2), "exc": _cr(f.get("exceptional_items"))}


def _quarter(span) -> bool:
    """A quarter's figures, not a half-year's or a year's (financials.QUARTER_MAX_DAYS)."""
    span = _num(span)
    return span is None or span <= 100


def prior(archive: pd.DataFrame, symbol: str, consolidated: bool, period_end) -> dict | None:
    """The latest revision of a company's quarter in the archive, on the same basis."""
    if archive is None or archive.empty:
        return None
    m = archive[(archive["symbol"] == symbol) & (archive["consolidated"] == consolidated) &
                (pd.to_datetime(archive["period_end"]) == pd.Timestamp(period_end))]
    if "quarter_span_days" in m:
        m = m[[_quarter(x) for x in m["quarter_span_days"]]]
    if m.empty:
        return None
    return m.sort_values("broadcast_dt").iloc[-1].to_dict()


def _archive_for(periods, fin_dir: Path = FIN_DIR) -> pd.DataFrame:
    """The archive's parquet files for the quarters asked for (one file a quarter-end month)."""
    parts = []
    for ym in sorted({f"{pd.Timestamp(x):%Y-%m}" for x in periods}):
        p = fin_dir / f"{ym}.parquet"
        if p.exists():
            df = pd.read_parquet(p)
            parts.append(df[[c for c in ("symbol", "period_end", "broadcast_dt", "consolidated", "revenue", "excise_duty", "ebitda", "pbt",
                                         "profit_reported", "eps_basic", "exceptional_items", "quarter_span_days") if c in df]])
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def _back(period_end, months: int) -> pd.Timestamp:
    """The quarter-end `months` before, at its month's end (30 Sep less three months is 30 Jun)."""
    return (pd.Timestamp(period_end) - pd.DateOffset(months=months)) + pd.offsets.MonthEnd(0)


def result_row(f: dict, archive: pd.DataFrame) -> dict:
    """One row of live/results.json: the filing's figures, a year earlier's and the quarter before's."""
    pe = pd.Timestamp(f["period_end"])
    py, pq = _back(pe, 12), _back(pe, 3)
    a, b = prior(archive, f["symbol"], bool(f["consolidated"]), py), prior(archive, f["symbol"], bool(f["consolidated"]), pq)
    fa, fb = figures(a) if a else {}, figures(b) if b else {}
    return {"t": pd.Timestamp(f["broadcast_dt"]).strftime("%Y-%m-%d %H:%M:%S"), "s": f["symbol"], "n": f.get("company"),
            "pe": f"{pe:%Y-%m-%d}", "c": bool(f["consolidated"]), **figures(f),
            "rev_py": fa.get("rev"), "ebitda_py": fa.get("ebitda"), "np_py": fa.get("np"), "eps_py": fa.get("eps"),
            "pe_py": f"{py:%Y-%m-%d}" if a else None,
            "rev_pq": fb.get("rev"), "ebitda_pq": fb.get("ebitda"), "np_pq": fb.get("np"), "eps_pq": fb.get("eps"),
            "pe_pq": f"{pq:%Y-%m-%d}" if b else None, "u": f["xbrl_url"]}


def build_results(prev: dict | None, listing: pd.DataFrame, fetch_one, now: datetime, fin_dir: Path = FIN_DIR) -> dict:
    """live/results.json from the last round's file and NSE's feed. `fetch_one(meta)` returns the
    parsed filing (financials.parse_xbrl, then _derive) or None. Only filings not in `prev` are read;
    a later filing of a company's quarter on the same basis replaces the earlier one."""
    cutoff = (now - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    rows = {}
    if prev and prev.get("cols") == RESULT_COLS:
        for r in prev.get("rows") or []:
            d = dict(zip(RESULT_COLS, r))
            if d["t"] >= cutoff:
                rows[d["u"]] = d
    new = [] if listing is None or listing.empty else [m for m in listing.to_dict("records") if m["xbrl_url"] not in rows]
    # a filing with no figures read (FLAIR's of 2 Nov 2026 parsed to nothing) is left for the next round
    parsed = [(m, f) for m, f in ((m, fetch_one(m)) for m in new)
              if f and _quarter(f.get("quarter_span_days")) and any(v is not None for k, v in figures(f).items() if k != "exc")]
    if parsed:
        archive = _archive_for({_back(m["period_end"], k) for m, _ in parsed for k in (3, 12)}, fin_dir)
        for m, f in parsed:
            rows[m["xbrl_url"]] = result_row({**m, **f}, archive)
    latest = {}
    for d in sorted(rows.values(), key=lambda d: d["t"]):
        latest[(d["s"], d["pe"], d["c"])] = d
    out = sorted(latest.values(), key=lambda d: d["t"], reverse=True)
    return {"at": now.strftime("%Y-%m-%d %H:%M:%S"), "n": len(out), "read": len(parsed), "cols": RESULT_COLS,
            "rows": [[d[k] for k in RESULT_COLS] for d in out]}


def _fetch_results(prev_path: str | None, out_path: str) -> None:
    from zen.data import financials as fin
    from zen.data.bhavcopy import _session
    now = datetime.now(IST).replace(tzinfo=None)
    prev = None
    if prev_path and Path(prev_path).exists():
        try:
            prev = json.loads(Path(prev_path).read_text(encoding="utf-8"))
        except ValueError:
            prev = None
    s = _session()
    listing = fin.listing(start=now.date() - timedelta(days=LIST_DAYS - 1), end=now.date(), session=s)

    def one(meta):
        try:
            r = s.get(meta["xbrl_url"], timeout=60)
            r.raise_for_status()
            facts = fin.parse_xbrl(r.content)
        except Exception:                                            # noqa: BLE001
            return None
        return fin._derive({**meta, **facts}) if facts else None

    out = build_results(prev, listing, one, now)
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"results: {out['n']} kept, {out['read']} read this round, at {out['at']} IST")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--results-out", default=None)
    ap.add_argument("--prev", default=None)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    today = datetime.now(IST).date()
    df = announcements.fetch(today - timedelta(days=a.days - 1), today, strict=True)
    out = build(df)
    p = Path(a.out)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"wire: {out['n']} filings at {out['at']} IST")
    if a.results_out:
        # the figures are a second read: when NSE's results feed fails, the filings above still go out
        try:
            _fetch_results(a.prev, a.results_out)
        except Exception as e:                                       # noqa: BLE001
            print(f"results: not read this round ({e})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
