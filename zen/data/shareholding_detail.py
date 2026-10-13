"""Who holds the public float: foreign and domestic institutions, mutual funds, insurers,
small shareholders. Read from each shareholding filing's own document.

WHY A SEPARATE TABLE

`shareholding` comes from NSE's listing of filings and has three numbers a quarter: promoter,
public, employee trusts. The split of the public holding is only in the filing's XBRL
document, one download each. Those figures go to `shareholding_detail`, keyed by the same
`record_id`, so the listing table and everything that reads it are unchanged. No strategy
reads either.

WHAT IS READ

Each document tags "shareholding as a percentage of total number of shares" once for every
category of shareholder (the CategoryOfShareholdersAxis). The categories kept are the ones
SEBI's format totals itself, so nothing is added up here:

  promoter_pct, public_pct       as the document states them, to check against the listing
  fii_pct                        Institutions (Foreign): foreign portfolio investors and the rest
  dii_pct                        Institutions (Domestic): the total of the lines below and others
  mf_pct, insurance_pct, pension_pct, aif_pct
                                 mutual funds, insurers, provident and pension funds, AIFs
  retail_pct                     resident individuals holding up to Rs 2 lakh of nominal capital
  non_institution_pct            everyone else in the public holding
  shareholders                   the number of shareholders in all

TWO THINGS THE DOCUMENTS DO DIFFERENTLY

Units. Filings up to early 2023 tag percentages (72.3); later ones tag fractions (0.723).
Every document also tags the whole (100, or 1), and that decides. Without it the largest
figure decides: nothing above 1 means fractions. Stored here in percent, like `shareholding`.

Names. The category names changed spelling between taxonomies (MutualFundsOrUti and
MutualFundsOrUTI, "Catergory"). Names are matched without regard to case.

`agrees` is true when the document's promoter and public figures match the listing's to
0.05 of a point. A row that does not agree is kept and marked, never corrected.

NOT KEPT ON DISK

The documents run from 150 KB to over 1 MB and there are tens of thousands. They are read
and discarded, not put in xbrl_cache: the figures kept are a dozen numbers a filing, and a
re-read means downloading again.
"""

from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path
from xml.etree import ElementTree as ET

import pandas as pd

from zen.data import financials_legacy, shareholding, xbrl_cache

log = logging.getLogger(__name__)

OUT_DIR = Path("data/shareholding_detail")
AXIS = "CategoryOfShareholdersAxis"
SHARE_TAG = "ShareholdingAsAPercentageOfTotalNumberOfShares"
COUNT_TAG = "NumberOfShareholders"
WHOLE = "shareholdingpatternmember"

# category (lower case) -> column
MEMBERS = {
    "shareholdingofpromoterandpromotergroupmember": "promoter_pct",
    "publicshareholdingmember": "public_pct",
    "institutionsforeignmember": "fii_pct",
    "institutionsdomesticmember": "dii_pct",
    "mutualfundsorutimember": "mf_pct",
    "insurancecompaniesmember": "insurance_pct",
    "providentfundsorpensionfundsmember": "pension_pct",
    "alternativeinvestmentfundsmember": "aif_pct",
    "residentindividualshareholdersholdingnominalsharecapitaluptorstwolakhmember": "retail_pct",
    "noninstitutionsmember": "non_institution_pct",
}
FIGURES = [*dict.fromkeys(MEMBERS.values()), "shareholders"]
COLUMNS = ["record_id", "symbol", "period_end", "broadcast_dt", *FIGURES, "agrees", "has_split", "xbrl_url"]
AGREE_WITHIN = 0.05                 # percentage points between the document and the listing

SCHEMA = """
CREATE TABLE IF NOT EXISTS shareholding_detail (
    record_id VARCHAR PRIMARY KEY, symbol VARCHAR, period_end DATE, broadcast_dt TIMESTAMP,
    promoter_pct DOUBLE, public_pct DOUBLE, fii_pct DOUBLE, dii_pct DOUBLE, mf_pct DOUBLE,
    insurance_pct DOUBLE, pension_pct DOUBLE, aif_pct DOUBLE, retail_pct DOUBLE,
    non_institution_pct DOUBLE, shareholders DOUBLE, agrees BOOLEAN, has_split BOOLEAN, xbrl_url VARCHAR
)
"""


def ensure_schema(con) -> None:
    con.execute(SCHEMA)


def _number(text) -> float | None:
    try:
        return float(re.sub(r"[,\s]", "", text or ""))
    except ValueError:
        return None


def parse(content: bytes) -> dict:
    """The holding of each category kept, in percent, and the number of shareholders. Empty where
    the document is unreadable or tags no category at all."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        log.debug("unparseable XBRL: %s", e)
        return {}
    category: dict[str, str] = {}              # context -> its one category, lower case
    for ctx in root.iter():
        if not (ctx.tag.endswith("}context") or ctx.tag == "context"):
            continue
        members = [((n.get("dimension") or "").split(":")[-1], (n.text or "").strip().split(":")[-1])
                   for n in ctx.iter() if n.tag.split("}")[-1] in ("explicitMember", "typedMember")]
        # one category and nothing else: a context that also names a shareholder is one holder's line
        if len(members) == 1 and members[0][0] == AXIS and ctx.get("id"):
            category[ctx.get("id")] = members[0][1].lower()
    share: dict[str, float] = {}
    count: dict[str, float] = {}
    for node in root.iter():
        cat = category.get(node.get("contextRef"))
        if cat is None:
            continue
        tag = node.tag.split("}")[-1]
        if tag == SHARE_TAG or tag == COUNT_TAG:
            v = _number(node.text)
            if v is not None:
                (share if tag == SHARE_TAG else count).setdefault(cat, v)
    if not share:
        return {}
    whole = share.get(WHOLE)
    if whole and whole > 0:
        scale = 100.0 / whole                  # the document's own total: 100, or 1
    else:
        scale = 100.0 if max(abs(v) for v in share.values()) <= 1.0 else 1.0
    out = {col: round(share[m] * scale, 4) for m, col in MEMBERS.items() if m in share}
    if WHOLE in count:
        out["shareholders"] = count[WHOLE]
    return out


def _agrees(doc: dict, listed_promoter, listed_public) -> bool | None:
    """Do the document's promoter and public holdings match the listing's? None where neither can
    be compared. A company with no promoter tags no promoter line: that is zero, not a gap."""
    checks = []
    for col, listed in (("promoter_pct", listed_promoter), ("public_pct", listed_public)):
        if listed is None or pd.isna(listed):
            continue
        got = doc.get(col)
        if got is None and col == "promoter_pct" and float(listed) == 0.0:
            got = 0.0
        if got is None:
            continue
        checks.append(abs(float(got) - float(listed)) <= AGREE_WITHIN)
    return all(checks) if checks else None


def documents(sh_dir: Path = shareholding.SH_DIR) -> pd.DataFrame:
    """The shareholding filings that have a document, read from the committed files (no archive
    needed, so this runs on a fresh checkout), oldest broadcast first."""
    files = sorted(Path(sh_dir).glob("*.parquet")) if Path(sh_dir).exists() else []
    cols = ["record_id", "symbol", "period_end", "broadcast_dt", "promoter_pct", "public_pct", "xbrl_url"]
    if not files:
        return pd.DataFrame(columns=cols)
    df = pd.concat([pd.read_parquet(f, columns=cols) for f in files], ignore_index=True)
    df = df[df["xbrl_url"].notna() & (df["xbrl_url"].astype(str).str.startswith("http"))]
    return df.drop_duplicates("record_id", keep="last").sort_values("broadcast_dt").reset_index(drop=True)


def stored_ids(out_dir: Path = OUT_DIR) -> set[str]:
    files = sorted(Path(out_dir).glob("*.parquet")) if Path(out_dir).exists() else []
    if not files:
        return set()
    return set(pd.concat([pd.read_parquet(f, columns=["record_id"]) for f in files])["record_id"].astype(str))


def _download(session, url: str) -> bytes:
    cached = xbrl_cache.get(url)               # only ever a document someone cached by hand
    if cached is not None:
        return cached
    r = session.get(url, timeout=30)
    r.raise_for_status()
    if not xbrl_cache._well_formed(r.content):
        raise ValueError("not an XBRL document")      # an error page served with a 200
    return r.content


_now = time.monotonic                              # the clock sessions are aged by


def fetch(todo: pd.DataFrame, session=None, pause: float = 0.3, give_up_after: int = 25,
          make_session=financials_legacy.document_session) -> tuple[pd.DataFrame, int]:
    """Read every document in `todo` (rows of documents()). Returns (rows, failed downloads).

    A failed download is left out, so the next run tries it again, and `give_up_after` failures
    in a row stop the run. Sessions are replaced on a timer and after every failure, as in
    zen.data.statements: NSE's archive host wedges a long-lived connection."""
    rows, failed, streak = [], 0, 0
    own, born = session is None, 0.0
    for i, meta in enumerate(todo.to_dict("records"), 1):
        if own and (session is None or _now() - born > financials_legacy.REWARM_SECONDS):
            if session is not None:
                session.close()
            session, born = make_session(), _now()
        try:
            content = _download(session, meta["xbrl_url"])
            streak = 0
        except Exception as e:                                       # noqa: BLE001
            failed += 1
            streak += 1
            log.debug("shareholding detail: %s not fetched (%s)", meta["xbrl_url"], e)
            if own and session is not None:
                session.close()
                session = None
            if streak >= give_up_after:
                log.warning("shareholding detail: %d downloads failed in a row, stopping at %d of %d",
                            streak, i, len(todo))
                break
            time.sleep(pause * min(streak, 10))
            continue
        doc = parse(content)
        row = {"record_id": str(meta["record_id"]), "symbol": meta["symbol"], "period_end": meta["period_end"],
               "broadcast_dt": meta["broadcast_dt"], "xbrl_url": meta["xbrl_url"],
               **{k: doc.get(k) for k in FIGURES},
               "agrees": _agrees(doc, meta.get("promoter_pct"), meta.get("public_pct")),
               "has_split": doc.get("fii_pct") is not None or doc.get("dii_pct") is not None}
        rows.append(row)
        time.sleep(pause)
        if i % 500 == 0:
            log.info("shareholding detail: %d of %d documents read", i, len(todo))
    return pd.DataFrame(rows, columns=COLUMNS), failed


def write(df: pd.DataFrame, out_dir: Path = OUT_DIR) -> list[Path]:
    if df.empty:
        return []
    df = df.copy()
    df["period_end"] = pd.to_datetime(df["period_end"])
    df["broadcast_dt"] = pd.to_datetime(df["broadcast_dt"])
    for c in ("agrees", "has_split"):
        df[c] = df[c].astype("boolean")
    written = []
    years = df["period_end"].dt.year
    for year, chunk in df.groupby(years):
        p = Path(out_dir) / f"{int(year)}.parquet"
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            chunk = pd.concat([pd.read_parquet(p), chunk], ignore_index=True)
        chunk = chunk.reindex(columns=COLUMNS).drop_duplicates(subset=["record_id"], keep="last")
        chunk = chunk.sort_values(["symbol", "period_end", "record_id"])
        tmp = p.with_name(p.name + ".tmp")         # whole or not at all, as in zen.data.statements
        chunk.to_parquet(tmp, index=False, compression="zstd")
        os.replace(tmp, p)
        written.append(p)
    return written


def rebuild(con, out_dir: Path = OUT_DIR) -> int:
    cols = [r[0] for r in con.execute("SELECT column_name FROM information_schema.columns "
                                      "WHERE table_name = 'shareholding_detail'").fetchall()]
    if cols and set(cols) != set(COLUMNS):
        con.execute("DROP TABLE shareholding_detail")
    ensure_schema(con)
    if not Path(out_dir).exists() or not any(Path(out_dir).glob("*.parquet")):
        return 0
    con.execute("DELETE FROM shareholding_detail")
    cols = ", ".join(COLUMNS)
    con.execute(f"INSERT OR IGNORE INTO shareholding_detail ({cols}) "
                f"SELECT {cols} FROM read_parquet('{Path(out_dir).as_posix()}/*.parquet', union_by_name=true)")
    return con.execute("SELECT count(*) FROM shareholding_detail").fetchone()[0]
