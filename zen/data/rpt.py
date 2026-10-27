"""Related-party transactions, from the results' own XBRL (27 Oct 2026).

WHAT A FILING HOLDS

SEBI's LODR Regulation 23(9) has every listed company disclose its related-party transactions each
half year. Until the half year to Sep 2024 they were a filing of their own (NSE's "Related Party
Transactions" page); from 2025 they sit inside the Integrated Filing (Financial) for the second and
fourth quarters, in the standalone results' XBRL. Each transaction is a member of
in-capmkt:RelatedPartyTransactionAxis (a typed member, RelatedPartyTransactionDomain1, ...), and its
facts carry: the listed entity or subsidiary that entered into it, the counterparty's name, the
counterparty's relationship (Subsidiary, Associate, Key Management Personnel, ...), the type (Sale of
goods or services, Purchase of goods or services, Loan, Remuneration, Any other transaction, ...), the
details where the type is "any other", the amount during the reporting period and the balances
outstanding at its start and end, in rupees. ITC's March 2026 standalone filing: 340 transactions.

THE PERIOD. The contexts in the integrated filing carry the quarter's dates (1 Jan to 31 Mar 2026),
but the amounts are for the half year the regulation asks for: matched by counterparty, type and
details against the same companies' old half-year filings (six months to Sep 2024, contexts dated
Apr to Sep), the Sep 2025 amounts run at a median of 1.0 times the year-earlier ones across 25
companies checked on 27 Oct 2026; quarter figures would run near 0.5. So an amount here is for the
six months to `period_end`, whatever its context's start date.

Nothing is derived or filled: a transaction without an amount for the period is kept with none.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

import pandas as pd

AXIS = "RelatedPartyTransactionAxis"
TEXT = {"NameOfListedEntityOrSubsidiaryEnteringIntoTheTransaction": "entity",
        "NameOfCounterParty": "counterparty",
        "RelationshipOfTheCounterpartyWithTheListedEntityOrItsSubsidiary": "relationship",
        "TypeOfRelatedPartyTransaction": "type",
        "DetailsOfOtherRelatedPartyTransaction": "details"}
AMOUNT = "AmountOfRelatedPartyTransactionDuringTheReportingPeriod"
BALANCE = "AmountOfRelatedPartyTransaction"
APPROVED = "ValueOfTheRelatedPartyTransactionAsApprovedByTheAuditCommittee"


def _num(text) -> float | None:
    try:
        return float(re.sub(r"[,\s]", "", text or ""))
    except ValueError:
        return None


def _clean(text) -> str | None:
    t = re.sub(r"\s+", " ", text or "").strip()
    return t or None


def parse_rpt(content: bytes) -> dict | None:
    """{"period_end", "items": [{"item", "entity", "counterparty", "relationship", "type", "details",
    "amount", "closing", "approved"}]} for the related-party transactions a results filing discloses,
    or None when it discloses none. `amount` is for the half year to period_end, `closing` the balance
    outstanding at period_end, `approved` the value the audit committee approved; rupees, as filed."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return None
    ctx: dict[str, tuple[str, date | None, date]] = {}          # id -> (transaction, start, end or instant)
    for c in root.iter():
        if not (c.tag.endswith("}context") or c.tag == "context"):
            continue
        cid, item, start, end = c.get("id"), None, None, None
        for node in c.iter():
            tag = node.tag.split("}")[-1]
            if tag == "typedMember" and (node.get("dimension") or "").split(":")[-1] == AXIS:
                item = "".join(node.itertext()).strip()
            elif tag == "startDate":
                start = (node.text or "").strip()
            elif tag in ("endDate", "instant"):
                end = (node.text or "").strip()
        if not (cid and item and end):
            continue
        try:
            ctx[cid] = (item, date.fromisoformat(start) if start else None, date.fromisoformat(end))
        except ValueError:
            continue
    if not ctx:
        return None
    period_end = max(v[2] for v in ctx.values() if v[1] is not None) if any(v[1] for v in ctx.values()) else None
    if period_end is None:
        return None
    items: dict[str, dict] = {}
    for node in root.iter():
        ref = node.get("contextRef")
        if ref not in ctx:
            continue
        item, start, end = ctx[ref]
        tag = node.tag.split("}")[-1]
        it = items.setdefault(item, {"item": item, "entity": None, "counterparty": None, "relationship": None,
                                     "type": None, "details": None, "amount": None, "closing": None, "approved": None})
        if tag in TEXT and it[TEXT[tag]] is None:
            it[TEXT[tag]] = _clean(node.text)
        elif tag == AMOUNT and start is not None and end == period_end and it["amount"] is None:
            it["amount"] = _num(node.text)
        elif tag == BALANCE and start is None and end == period_end and it["closing"] is None:
            it["closing"] = _num(node.text)
        elif tag == APPROVED and it["approved"] is None:
            it["approved"] = _num(node.text)
    out = [v for v in items.values() if v["counterparty"] or v["amount"] is not None]
    if not out:
        return None
    out.sort(key=lambda v: int(re.sub(r"\D", "", v["item"]) or 0))
    return {"period_end": period_end.isoformat(), "items": out}


# ---- storage, as the segments' (zen.data.segments): one parquet per half year under data/rpt,
# every revision kept (de-duplicated on the document and the transaction), rebuilt into the `rpt` table.

PARQUET_DIR = Path("data/rpt")
COLUMNS = ["symbol", "consolidated", "period_end", "broadcast_dt", "xbrl_url", "item", "entity", "counterparty",
           "relationship", "type", "details", "amount", "closing", "approved"]


def rows_for(meta: dict, parsed: dict | None) -> list[dict]:
    """The rows of one filing: `meta` is its results row (symbol, consolidated, period_end,
    broadcast_dt, xbrl_url), `parsed` what parse_rpt read from it. None, or transactions dated to a
    different quarter end from the filing's, give none."""
    if not parsed or not parsed.get("items"):
        return []
    pe = pd.Timestamp(meta.get("period_end")) if meta.get("period_end") is not None else None
    if pe is not None and pd.notna(pe) and pe.strftime("%Y-%m-%d") != parsed["period_end"]:
        return []
    return [{"symbol": meta["symbol"], "consolidated": bool(meta.get("consolidated")), "period_end": parsed["period_end"],
             "broadcast_dt": meta.get("broadcast_dt"), "xbrl_url": meta["xbrl_url"], **it} for it in parsed["items"]]


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
        chunk = chunk.drop_duplicates(subset=["xbrl_url", "item"], keep="first").reindex(columns=COLUMNS)
        chunk.to_parquet(p, index=False, compression="zstd")
        written.append(p)
    return written


def rebuild(con, out_dir: Path = PARQUET_DIR) -> int:
    files = sorted(out_dir.glob("*.parquet"))
    con.execute("DROP TABLE IF EXISTS rpt")
    if not files:
        return 0
    con.execute("CREATE TABLE rpt AS SELECT * FROM read_parquet(?, union_by_name = true)", [[str(f) for f in files]])
    return con.execute("SELECT count(*) FROM rpt").fetchone()[0]
