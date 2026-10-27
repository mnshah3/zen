"""Revenue and profit by business segment, from the results' own XBRL (27 Oct 2026).

WHAT A FILING HOLDS

A company with more than one reportable segment (Ind AS 108) tags each segment's figures under
in-capmkt:ReportableSegmentsAxis, one member per segment (ReportableSegments1Member, ...), in a
context of its own ("OneReportable1D" for the quarter). The same context carries the segment's name
as text (DescriptionOfReportableSegment). Segment revenue (SegmentRevenue) is before inter-segment
sales are taken out: the company total under the undimensioned context (SegmentRevenue) equals the
segments' sum, and InterSegmentRevenue is what the company sold to itself. Segment profit before
finance costs and tax (SegmentProfitLossBeforeTaxAndFinanceCosts) sits under a parallel axis,
ReportableSegmentsFinanceCostsAxis, with the same members. ITC's June 2026 quarter: five segments
summing to Rs 35,112 crore, inter-segment Rs 5,702 crore, revenue from operations Rs 29,410 crore.

THE RULES, as for the company totals (zen.data.financials.parse_xbrl)

* The quarter: a segment context counts only if its span is at most QUARTER_MAX_DAYS and it ends on
  the latest end date among the segment contexts; where several qualify, the "One..." contexts win,
  since a "Four..." context can hold the year to date under the quarter's dates.
* A segment is a member of the axis; its name is its own DescriptionOfReportableSegment, trimmed.
  Revenue and profit are matched to a segment by member, never by position.
* The check: the segments' revenue must sum to the company's own total segment revenue (the
  undimensioned SegmentRevenue for the same quarter) within RECONCILE_TOL, else the filing's segments
  are returned with `reconciles` False and must not be shown as a breakdown.

Nothing is derived: no half-year less a quarter, no segment filled from another filing.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import date

from zen.data.financials import QUARTER_MAX_DAYS

RECONCILE_TOL = 0.01
SEG_AXIS = "ReportableSegmentsAxis"
PROFIT_AXIS = "ReportableSegmentsFinanceCostsAxis"


def _num(text) -> float | None:
    try:
        return float(re.sub(r"[,\s]", "", text or ""))
    except ValueError:
        return None


def parse_segments(content: bytes) -> dict | None:
    """{"period_start", "period_end", "segments": [{"member", "name", "revenue", "pbit"}], "total",
    "inter", "reconciles"} for the quarter a results filing reports, or None when it reports no
    segments for a quarter. Amounts in rupees, as filed."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return None
    seg_ctx: dict[str, tuple[str, str, date, date]] = {}      # id -> (axis, member, start, end)
    plain: dict[str, tuple[date, date]] = {}                   # undimensioned spans
    for ctx in root.iter():
        if not (ctx.tag.endswith("}context") or ctx.tag == "context"):
            continue
        cid = ctx.get("id")
        start = end = None
        mem = []
        for node in ctx.iter():
            tag = node.tag.split("}")[-1]
            if tag == "startDate":
                start = (node.text or "").strip()
            elif tag == "endDate":
                end = (node.text or "").strip()
            elif tag in ("explicitMember", "typedMember"):
                mem.append(((node.get("dimension") or "").split(":")[-1], (node.text or "").strip().split(":")[-1]))
        if not (cid and start and end):
            continue
        try:
            a, b = date.fromisoformat(start), date.fromisoformat(end)
        except ValueError:
            continue
        if not mem:
            plain[cid] = (a, b)
        elif len(mem) == 1 and mem[0][0] in (SEG_AXIS, PROFIT_AXIS):
            seg_ctx[cid] = (mem[0][0], mem[0][1], a, b)
    rev_ctx = {c: v for c, v in seg_ctx.items() if v[0] == SEG_AXIS and (v[3] - v[2]).days <= QUARTER_MAX_DAYS}
    if not rev_ctx:
        return None
    end = max(v[3] for v in rev_ctx.values())
    span = min((v[3] - v[2]).days for v in rev_ctx.values() if v[3] == end)
    pick = {c for c, v in rev_ctx.items() if v[3] == end and (v[3] - v[2]).days == span}
    if any(c.startswith("One") for c in pick):
        pick = {c for c in pick if c.startswith("One")}
    start = next(v[2] for c, v in rev_ctx.items() if c in pick)
    prof = {c for c, v in seg_ctx.items() if v[0] == PROFIT_AXIS and v[2] == start and v[3] == end}
    if any(c.startswith("One") for c in prof):
        prof = {c for c in prof if c.startswith("One")}
    # the company's own total for the same quarter: an undimensioned context with these dates, or the
    # undeclared OneD the legacy filings use for the company
    same = {c for c, (a, b) in plain.items() if a == start and b == end}
    referenced = {n.get("contextRef") for n in root.iter()} - {None}
    if "OneD" in referenced and "OneD" not in plain:
        same.add("OneD")
    segs: dict[str, dict] = {}
    total = inter = None
    for node in root.iter():
        tag, ref = node.tag.split("}")[-1], node.get("contextRef")
        if ref in pick:
            m = seg_ctx[ref][1]
            s = segs.setdefault(m, {"member": m, "name": None, "revenue": None, "pbit": None})
            if tag == "DescriptionOfReportableSegment" and s["name"] is None and (node.text or "").strip():
                s["name"] = re.sub(r"\s+", " ", node.text).strip()
            elif tag == "SegmentRevenue" and s["revenue"] is None:
                s["revenue"] = _num(node.text)
        elif ref in prof:
            m = seg_ctx[ref][1]
            s = segs.setdefault(m, {"member": m, "name": None, "revenue": None, "pbit": None})
            if tag == "DescriptionOfReportableSegment" and s["name"] is None and (node.text or "").strip():
                s["name"] = re.sub(r"\s+", " ", node.text).strip()
            elif tag == "SegmentProfitLossBeforeTaxAndFinanceCosts" and s["pbit"] is None:
                s["pbit"] = _num(node.text)
        elif ref in same:
            if tag == "SegmentRevenue" and total is None:
                total = _num(node.text)
            elif tag == "InterSegmentRevenue" and inter is None:
                inter = _num(node.text)
    out = [s for s in segs.values() if s["revenue"] is not None or s["pbit"] is not None]
    if not out:
        return None
    out.sort(key=lambda s: int(re.sub(r"\D", "", s["member"]) or 0))
    rsum = sum(s["revenue"] or 0.0 for s in out)
    ok = bool(total) and all(s["revenue"] is not None for s in out) and abs(rsum - total) <= RECONCILE_TOL * abs(total)
    return {"period_start": start.isoformat(), "period_end": end.isoformat(), "segments": out,
            "total": total, "inter": inter, "reconciles": ok}


# ---- storage, as the results' (zen.data.financials): one parquet per quarter under data/segments,
# every revision kept (de-duplicated on the document and the segment), rebuilt into the `segments`
# table. One row per segment; the filing's total and inter-segment sales repeat on each.
from pathlib import Path  # noqa: E402

import pandas as pd  # noqa: E402

PARQUET_DIR = Path("data/segments")
COLUMNS = ["symbol", "consolidated", "period_end", "broadcast_dt", "xbrl_url", "member", "name",
           "revenue", "pbit", "total", "inter", "reconciles"]


def rows_for(meta: dict, parsed: dict | None) -> list[dict]:
    """The rows of one filing: `meta` is its results row (symbol, consolidated, broadcast_dt,
    xbrl_url), `parsed` what parse_segments read from it. None or a different quarter gives none."""
    if not parsed or not parsed.get("segments"):
        return []
    pe = pd.Timestamp(meta.get("period_end")) if meta.get("period_end") is not None else None
    if pe is not None and pd.notna(pe) and pe.strftime("%Y-%m-%d") != parsed["period_end"]:
        return []
    return [{"symbol": meta["symbol"], "consolidated": bool(meta.get("consolidated")), "period_end": parsed["period_end"],
             "broadcast_dt": meta.get("broadcast_dt"), "xbrl_url": meta["xbrl_url"], "member": s["member"], "name": s["name"],
             "revenue": s["revenue"], "pbit": s["pbit"], "total": parsed["total"], "inter": parsed["inter"],
             "reconciles": bool(parsed["reconciles"])} for s in parsed["segments"]]


def write_parquet(df: pd.DataFrame, out_dir: Path = PARQUET_DIR) -> list[Path]:
    if df.empty:
        return []
    df = df.copy()
    df["period_end"] = pd.to_datetime(df["period_end"])
    df["broadcast_dt"] = pd.to_datetime(df["broadcast_dt"])
    written = []
    for q, chunk in df.groupby(df["period_end"].map(lambda d: f"{d:%Y-%m}")):
        p = out_dir / f"{q}.parquet"
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            old = pd.read_parquet(p)
            old["period_end"] = pd.to_datetime(old["period_end"])
            old["broadcast_dt"] = pd.to_datetime(old["broadcast_dt"])
            chunk = pd.concat([old, chunk], ignore_index=True)
        chunk = chunk.drop_duplicates(subset=["xbrl_url", "member"], keep="first").reindex(columns=COLUMNS)
        chunk.to_parquet(p, index=False, compression="zstd")
        written.append(p)
    return written


def rebuild(con, out_dir: Path = PARQUET_DIR) -> int:
    files = sorted(out_dir.glob("*.parquet"))
    con.execute("DROP TABLE IF EXISTS segments")
    if not files:
        return 0
    con.execute("CREATE TABLE segments AS SELECT * FROM read_parquet(?, union_by_name = true)", [[str(f) for f in files]])
    return con.execute("SELECT count(*) FROM segments").fetchone()[0]
