"""Credit rating actions, read from the rating letters companies file with NSE (SEBI LODR
Regulation 30: a listed company discloses every rating and revision of its debt), by fixed rules.

WHERE: zen's announcements in the category "ratings" (zen.data.filing_categories: NSE's subjects
"Credit Rating", "Credit Rating- Revision" and the like, 9,037 filings from 2022 to 22 Oct 2026). The
subject rarely says what happened ("has informed the Exchange about Credit Rating"); the attached
PDF does, usually the agency's own letter or press release, sometimes behind a cover letter.

HOW, AND WHY NOT AI: the PDF's own text layer (pdfminer.six), its first MAX_PAGES pages, matched
against fixed patterns; a page with no text layer (a scan) is not read and says so. The owner ruled
out reading PDFs with a language model, as for the balance sheets (zen.data.pdf_balance_sheet): a
model that misreads gives a plausible wrong answer with nothing to say so. The rules here only ever
name words the letter itself prints:

  downgrade        "downgraded", "downgradation", or a rating "revised from" (or "migrated from") a
                   higher grade
  upgrade          "upgraded", "upgradation", or "revised from" (or "migrated from") a lower grade
  not cooperating  "issuer not cooperating", "non-cooperation" (the agency could not get information)
  watch negative   "watch with negative implications", "rating watch negative" (and positive, developing);
                   a rating that "continues on" a watch counts, the watch being where it stands
  outlook          "outlook revised to negative" (or positive), "outlook ... from stable to negative"
  withdrawn        "withdrawn"; assigned: "assigned" in a rating action, a new rating; reaffirmed:
                   "reaffirmed", "affirmed", "reaffirmation"

A company sometimes files its subsidiary's rating letter, and nothing in a fixed place says so: such
an action counts as the filing company's here. A letter often names several (a long-term rating
upgraded, the short-term one reaffirmed), so every
action found is kept, and the headline is the most severe by PRIORITY. Disclaimers in every letter
("reserves the right to withdraw or revise the ratings assigned") use other forms of the words and
are not matched: "withdraw", "revise" and "ratings assigned" are not actions; nor is a word in a
hedged or conditional clause ("may be withdrawn", "if the sovereign rating is downgraded", "could
lead to a downgrade"), SEBI's required sentence that a rating "shall be accompanied by 'ISSUER NOT
COOPERATING'", anything in the rating history table a press release ends with, or CRISIL's and
India Ratings' line on another agency's non-cooperation ("Status of Non-Cooperation").

CHECKED, 25 OCT 2026: the 106 rating filings of 9 to 22 Oct 2026, 104 with a text layer. Against
MarketPing's own classification of the same filings (an independent reading, its headlines): 11 of 11
agree (four downgrades, three upgrades, one with a withdrawal on redemption, a negative watch, two
reaffirmations, and an NSE ESG score filed as a rating, rightly no action); on a twelfth MarketPing
says a new rating and zen a new rating on watch with developing implications, as the letter says. The first version called
TVS Motor and Equitas Bank "not cooperating" (SEBI's required sentence on the suffix), and YES Bank
and HUDCO downgraded and upgraded at once (Moody's "if the sovereign rating is downgraded"); the
hedges above came from those four. 10 of the 104 read named no action: cover letters with the rating
alone, and ESG scores.

THE YEAR TO 22 OCT 2026: 2,589 filings of 1,193 companies, 2,582 with a text layer. A check of the
year's watch and non-cooperation calls found two more misreadings, both of a state left behind: a
rating "Removed from 'Rating Watch with Developing Implications'" (KRISHANA, 8 Oct) read as a watch,
and one "Migrated from 'Crisil BB/Stable ISSUER NOT COOPERATING'" to Crisil A (CONTROLPR, 2 Sep)
read as not cooperating. Fixed (_LEFT_INC, _LEFT_WATCH, "migrated from" compared like "revised
from"), such filings read again (225, then 192 after the second fix): 36 calls changed, 7 of them
from not cooperating to an upgrade out of it.
MarketPing confirms CHEMBOND's (1 Oct: "downgrades ... over non-cooperation")."""

from __future__ import annotations

import io
import logging
import re
from datetime import datetime
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

DIR = Path("data/ratings")
MAX_PAGES = 4
MIN_CHARS = 200                   # less text than this on the pages read: a scan, not read
COLUMNS = ["an_dt", "symbol", "url", "action", "actions", "agency", "read", "chars", "observed_dt"]
SCHEMA = """CREATE TABLE IF NOT EXISTS ratings (an_dt TIMESTAMP, symbol VARCHAR, url VARCHAR, action VARCHAR,
    actions VARCHAR, agency VARCHAR, read BOOLEAN, chars INTEGER, observed_dt TIMESTAMP);"""

PRIORITY = ["downgrade", "not cooperating", "watch negative", "upgrade", "outlook negative", "watch developing",
            "outlook positive", "watch positive", "withdrawn", "assigned", "reaffirmed"]
NEGATIVE = {"downgrade", "not cooperating", "watch negative", "outlook negative"}
POSITIVE = {"upgrade", "outlook positive", "watch positive"}

AGENCIES = [("CRISIL", r"\bcrisil\b"), ("ICRA", r"\bicra\b"), ("CARE", r"\bcare\s?(?:ratings|edge|\s[a-d])|\bcareedge\b"),
            ("India Ratings", r"india ratings|\bind-?ra\b|\bind\s(?:aaa|aa|a|bbb|bb|b|c|d)\b"), ("Acuite", r"\bacuit[eé]\b"),
            ("Brickwork", r"brickwork|\bbwr\b"), ("Infomerics", r"infomerics|\bivr\b"), ("Moody's", r"\bmoody'?s\b"),
            ("S&P", r"s&p global|\bstandard (?:&|and) poor'?s\b"), ("Fitch", r"\bfitch ratings\b")]

_LONG = ["AAA", "AA+", "AA", "AA-", "A+", "A", "A-", "BBB+", "BBB", "BBB-", "BB+", "BB", "BB-", "B+", "B", "B-",
         "C+", "C", "C-", "D"]
_SHORT = ["A1+", "A1", "A2+", "A2", "A3+", "A3", "A4+", "A4", "D"]
_PREFIX = r"(?:crisil|\[icra\]|icra|care|ind|acuite|bwr|ivr|pr|\[ind\])"
_TOKEN = re.compile(_PREFIX + r"\s?\]?\s?(a1\+|a1|a2\+|a2|a3\+|a3|a4\+|a4|aaa|aa\+|aa-|aa|a\+|a-|a|bbb\+|bbb-|bbb|bb\+|bb-|bb|b\+|b-|b|c\+|c-|c|d)"
                    r"(?![a-z0-9])", re.I)

_RULES = [
    ("downgrade", re.compile(r"\bdown\s?-?\s?grad(?:ed|ation)\b|rating action\W{0,5}downgrade\b", re.I)),
    ("upgrade", re.compile(r"\bup\s?-?\s?grad(?:ed|ation)\b|rating action\W{0,5}upgrade\b", re.I)),
    ("not cooperating", re.compile(r"issuer not co-?operating|non[- ]?co-?operat(?:ion|ing)|not co-?operating with", re.I)),
    ("watch negative", re.compile(r"watch with negative implications|rating watch (?:with )?negative|credit watch (?:with )?negative", re.I)),
    ("watch positive", re.compile(r"watch with positive implications|rating watch (?:with )?positive|credit watch (?:with )?positive", re.I)),
    ("watch developing", re.compile(r"watch with developing implications|rating watch (?:with )?developing", re.I)),
    ("outlook negative", re.compile(r"outlook\W{0,3}(?:has been |is |was )?revised (?:from \w+ )?to negative|outlook[^.]{0,40}from stable to negative|"
                                    r"outlook[^.]{0,40}from positive to (?:stable|negative)", re.I)),
    ("outlook positive", re.compile(r"outlook\W{0,3}(?:has been |is |was )?revised (?:from \w+ )?to positive|outlook[^.]{0,40}from stable to positive|"
                                    r"outlook[^.]{0,40}from negative to (?:stable|positive)", re.I)),
    ("withdrawn", re.compile(r"\bwithdrawn\b", re.I)),
    ("assigned", re.compile(r"rating action\W{0,5}assigned|\bnewly assigned\b|\bhas assigned\b|\bassigned (?:a|the|its|their) (?:long|short|credit|rating)|"
                            r"\(\s?assigned\s?\)|\bfresh rating\b|\bnew rating\b", re.I)),
    ("reaffirmed", re.compile(r"\b(?:re-?)?affirm(?:ed|ation)\b", re.I)),
]
_HEDGED = re.compile(
    r"\b(?:may|might|could|can|would|will|shall|liable to)\b(?:\W+\w+){0,2}\W*$"    # "may be withdrawn"
    r"|\bif\b(?:\W+\w+){0,6}\W*$"                                                  # "if the sovereign rating is downgraded"
    r"|\b(?:lead|leads|leading) to\b(?:\W+\w+){0,3}\W*$"                           # "lead to negative rating action/downgrade"
    r"|(?:up|down)\s?-?\s?grad\w*\s?(?:or|/)\s?$"                                  # "upgraded or downgraded"
    r"|(?:accompanied by|along with the comment|status of)\W*$"                    # SEBI's definition of the suffix; a table heading
    r"|\b(?:removed|migrated|moved) from\b(?:\W+[\w+-]+){0,4}\W*$",                # the state left: "Removed from 'Rating Watch ...'"
    re.I)
_LEFT_INC = re.compile(r"(?:migrated|moved|removed) from[^)]{0,80}not co-?operating", re.I)
_LEFT_WATCH = re.compile(r"removed from[^)]{0,30}(?:rating|credit) watch", re.I)
_HISTORY = re.compile(r"rating history|history of (?:the )?ratings?", re.I)   # the past three years' actions, at the end
_PREVIOUS_CRA = re.compile(r"status of non[- ]?co-?operation with (?:the )?previous (?:cra|credit rating agenc\w*)[^.]{0,120}", re.I)


def _grade(tok: str):
    t = tok.upper()
    if t in _SHORT[:-1]:
        return ("short", _SHORT.index(t))
    return ("long", _LONG.index(t)) if t in _LONG else None


def _revisions(text: str) -> set:
    """Direction of each "revised from X" against the rating printed just before it: the agencies
    write the new rating first, the old after it ("CARE A+; Stable (Revised from CARE A; Stable)")."""
    out = set()
    toks = [(m.start(), m.end(), m.group(1)) for m in _TOKEN.finditer(text)]
    for m in re.finditer(r"(?:revised|migrated) from", text, re.I):
        after = [t for t in toks if t[0] >= m.end() and t[0] - m.end() <= 25]
        before = [t for t in toks if t[1] <= m.start() and m.start() - t[1] <= 80]
        if not after or not before:
            continue
        new, old = _grade(before[-1][2]), _grade(after[0][2])
        if new and old and new[0] == old[0] and new[1] != old[1]:
            out.add("upgrade" if new[1] < old[1] else "downgrade")
    return out


def _hit(rx, text: str) -> bool:
    return any(not _HEDGED.search(text[max(0, m.start() - 40):m.start()]) for m in rx.finditer(text))


def classify(text: str) -> tuple[str | None, list[str]]:
    """(headline action, every action found) from a letter's text: up to its rating history table,
    without CRISIL's line on another agency's non-cooperation, an action never in a hedged clause."""
    t = " ".join((text or "").split())
    h = _HISTORY.search(t)
    t = _PREVIOUS_CRA.sub(" ", t[:h.start()] if h else t)
    found = {name for name, rx in _RULES if _hit(rx, t)} | _revisions(t)
    if _LEFT_INC.search(t):                             # migrated away from "issuer not cooperating": the old state
        found.discard("not cooperating")
    if _LEFT_WATCH.search(t):                           # removed from a watch: no longer on it
        found -= {"watch negative", "watch positive", "watch developing"}
    acts = [a for a in PRIORITY if a in found]
    return (acts[0] if acts else None), acts


def agency_of(text: str) -> str | None:
    t = " ".join((text or "").split())
    counts = {name: len(re.findall(rx, t, re.I)) for name, rx in AGENCIES}
    best = max(counts.items(), key=lambda kv: kv[1])
    return best[0] if best[1] else None


def pdf_text(content: bytes, pages: int = MAX_PAGES) -> str:
    from pdfminer.high_level import extract_text   # imported here: only this reader needs it
    return extract_text(io.BytesIO(content), maxpages=pages) or ""


def read(content: bytes) -> dict:
    """One filing's PDF: its headline action, every action, the agency, and whether it could be read.
    Without pdfminer.six this raises, so nothing is stored as unreadable for want of the reader."""
    import pdfminer.high_level  # noqa: F401
    try:
        text = pdf_text(content)
    except Exception as e:                              # noqa: BLE001 - a broken PDF is recorded as unread
        log.warning("ratings: unreadable PDF: %s", e)
        text = ""
    chars = len("".join(text.split()))
    if chars < MIN_CHARS:
        return {"action": None, "actions": "", "agency": None, "read": False, "chars": chars}
    head, acts = classify(text)
    return {"action": head, "actions": ",".join(acts), "agency": agency_of(text), "read": True, "chars": chars}


# ---------------------------------------------------------------- the filings, and storage
def filings(con, since, until=None) -> pd.DataFrame:
    """NSE's rating filings (category "ratings") broadcast from `since`, with a PDF."""
    q = ("SELECT an_dt, symbol, url FROM announcements WHERE category = 'ratings' AND an_dt >= ? "
         + ("AND an_dt < ? " if until is not None else "") + "AND lower(url) LIKE '%.pdf' ORDER BY an_dt")
    return con.execute(q, [since] + ([until] if until is not None else [])).df()


def stored(out_dir: Path | None = None) -> pd.DataFrame:
    d = out_dir or DIR
    files = sorted(d.glob("*.parquet")) if d.exists() else []
    return pd.concat([pd.read_parquet(f) for f in files], ignore_index=True) if files else pd.DataFrame(columns=COLUMNS)


def write(df: pd.DataFrame, out_dir: Path | None = None) -> list[Path]:
    """One file a year by the filing's time; a filing already stored keeps its first reading."""
    if df.empty:
        return []
    d = out_dir or DIR
    written = []
    for y, chunk in df.groupby(pd.to_datetime(df["an_dt"]).dt.year):
        p = d / f"{int(y)}.parquet"
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            chunk = pd.concat([pd.read_parquet(p), chunk], ignore_index=True)
        chunk.drop_duplicates("url", keep="first").sort_values("an_dt")[COLUMNS].to_parquet(p, index=False)
        written.append(p)
    return written


def update(todo: pd.DataFrame, session, out_dir: Path | None = None, pause: float = 0.35,
           now: datetime | None = None, flush: int = 100) -> int:
    """Read every filing in `todo` (filings(), read before: the archive need not stay open while the
    letters download) not yet stored. Returns the filings read."""
    import time
    have = set(stored(out_dir)["url"])
    todo = todo[~todo["url"].isin(have)]
    rows, n = [], 0
    for i, f in enumerate(todo.itertuples(), 1):
        try:
            r = session.get(f.url, timeout=30)
            r.raise_for_status()
            rows.append({"an_dt": f.an_dt, "symbol": f.symbol, "url": f.url, **read(r.content),
                         "observed_dt": (now or datetime.now()).replace(microsecond=0)})
        except Exception as e:                          # noqa: BLE001 - tried again on the next run
            log.warning("ratings: %s: %s", f.url, e)
        if len(rows) >= flush or i == len(todo):
            write(pd.DataFrame(rows, columns=COLUMNS), out_dir)
            n += len(rows)
            rows = []
            log.info("ratings: %d of %d filings read", n, len(todo))
        time.sleep(pause)
    return n


def rebuild(con) -> int:
    for stmt in SCHEMA.split(";"):
        if stmt.strip():
            con.execute(stmt)
    con.execute("DELETE FROM ratings")
    if DIR.exists() and any(DIR.glob("*.parquet")):
        con.execute(f"INSERT INTO ratings SELECT {', '.join(COLUMNS)} FROM read_parquet('{(DIR / '*.parquet').as_posix()}', union_by_name=true)")
    return con.execute("SELECT count(*) FROM ratings").fetchone()[0]
