"""What a company does, from its Business Responsibility and Sustainability Report (28 Oct 2026).

SEBI's LODR Regulation 34(2)(f) has the 1,000 largest listed companies by market cap (and others
that choose to) file a BRSR each financial year, in XBRL; NSE lists them at
/api/corporate-bussiness-sustainabilitiy (1,255 for FY2025-26 by 28 Oct 2026). Section A, the
general disclosures, is the company's own account of itself, and this module reads only that:

  activities   the business activities making up 90% of turnover: the main activity, the
               business activity and its share of turnover (Tata Steel, FY2025-26: Manufacturing,
               metal and metal products)
  products     the products or services making up 90% of turnover, their NIC code and share
               (Sale of Steel Products, NIC 2410, 93%)
  locations    plants and offices in India and abroad (78 and 46 in India, 41 and 25 abroad)
  markets      the states and countries it serves and exports as a share of turnover (28, 66, 8%)
  people       permanent employees and permanent workers (71,037 and 41,225)
  identity     date of incorporation, website, the reporting boundary (standalone or
               consolidated), and its brief on its types of customers, in its own words

Facts are matched by their dimensions in each context (typed or explicit members), never by the
context's name. Shares are as filed (fractions of one). Nothing is derived.
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import pandas as pd

LIST_URL = "https://www.nseindia.com/api/corporate-bussiness-sustainabilitiy?index=equities"
PAGE = "https://www.nseindia.com/companies-listing/corporate-filings-bussiness-sustainabilitiy-reports"
ACT_AXIS = "DetailsOfBusinessActivitiesAccountingForNinetyPercentOfTheTurnoverAxis"
PROD_AXIS = "ProductsOrServicesSoldByTheEntityAccountingForNinetyPercentOfTheTurnoverAxis"
CUSTOMERS_MAX = 700          # characters of the company's brief on its customers


def _txt(v) -> str | None:
    t = re.sub(r"\s+", " ", v or "").strip()
    return t or None


def _num(v) -> float | None:
    try:
        return float(re.sub(r"[,\s]", "", v or ""))
    except ValueError:
        return None


def parse_brsr(content: bytes) -> dict | None:
    """Section A of a BRSR filing as a dict (see the module's docstring), or None when the
    document is not one."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return None
    dims: dict[str, dict] = {}
    for c in root.iter():
        if not (c.tag.endswith("}context") or c.tag == "context"):
            continue
        d = {}
        for n in c.iter():
            tag = n.tag.split("}")[-1]
            if tag in ("explicitMember", "typedMember"):
                v = (n.text or "").strip() if tag == "explicitMember" else "".join(n.itertext()).strip()
                d[(n.get("dimension") or "").split(":")[-1]] = v.split(":")[-1]
        dims[c.get("id")] = d
    facts: list[tuple[str, dict, str]] = []
    for n in root.iter():
        ref = n.get("contextRef")
        if ref is not None and ref in dims:
            facts.append((n.tag.split("}")[-1], dims[ref], "".join(n.itertext())))
    if not any(t == "NameOfTheCompany" for t, _, _ in facts):
        return None

    def plain(tag):                                   # an undimensioned fact
        for t, d, v in facts:
            if t == tag and not d:
                return v
        return None

    def rows(axis, fields):
        out: dict[str, dict] = {}
        for t, d, v in facts:
            if axis in d and len(d) == 1 and t in fields:
                out.setdefault(d[axis], {})[fields[t]] = v
        return [out[k] for k in sorted(out, key=lambda k: int(re.sub(r"\D", "", k) or 0))]

    acts = [{"main": _txt(r.get("main")), "business": _txt(r.get("business")), "share": _num(r.get("share"))}
            for r in rows(ACT_AXIS, {"DescriptionOfMainActivity": "main", "DescriptionOfBusinessActivity": "business",
                                     "PercentageOfTurnoverOfTheEntity": "share", "PercentageOfTotalTurnoverForBusinessActivities": "share"})]
    prods = [{"name": _txt(r.get("name")), "nic": _txt(r.get("nic")), "share": _num(r.get("share"))}
             for r in rows(PROD_AXIS, {"ProductOrServiceSoldByTheEntity": "name", "NICCodeOfProductOrServiceSoldByTheEntity": "nic",
                                       "PercentageOfTotalTurnoverForProductOrServiceSold": "share"})]
    loc: dict[str, float | None] = {}
    for t, d, v in facts:
        if t == "NumberOfLocations" and "LocationAxis" in d:
            where = next((x for k, x in d.items() if k != "LocationAxis"), "")
            kind = d["LocationAxis"].replace("Member", "").lower()
            loc[kind + "_" + ("abroad" if "International" in where else "india" if "National" in where else where.lower())] = _num(v)
    people: dict[str, float | None] = {}
    for t, d, v in facts:
        if t == "NumberOfEmployeesOrWorkersIncludingDifferentlyAbled" and d.get("GenderAxis") == "GenderMember":
            who = d.get("DetailsOfEmployeesAndWorkersIncludingDifferentlyAbledAxis", "")
            if who in ("PermanentEmployeesMember", "PermanentWorkersMember"):
                people[who] = _num(v)
    inc = _txt(plain("DateOfIncorporation"))
    cust = _txt(re.sub(r"<[^>]+>", " ", plain("ABriefOnTypesOfCustomersExplanatoryTextBlock") or ""))
    return {"name": _txt(plain("NameOfTheCompany")), "incorporated": inc, "website": _txt(plain("WebsiteOfCompany")),
            "boundary": _txt(plain("ReportingBoundary")),
            "activities": [a for a in acts if a["main"] or a["business"]], "products": [p for p in prods if p["name"]],
            "plants_india": loc.get("plant_india"), "offices_india": loc.get("office_india"),
            "plants_abroad": loc.get("plant_abroad"), "offices_abroad": loc.get("office_abroad"),
            "states": _num(plain("NumberOfStatesWhereMarketServedByTheEntity")), "countries": _num(plain("NumberOfCountriesWhereMarketServedByTheEntity")),
            "exports": _num(plain("PercentageOfContributionOfExportsInTheTotalTurnoverOfTheEntity")),
            "employees": people.get("PermanentEmployeesMember"), "workers": people.get("PermanentWorkersMember"),
            "customers": _brief(cust)}


def _brief(text: str | None) -> str | None:
    """The first sentences of the company's brief, up to CUSTOMERS_MAX characters, ending where a
    sentence ends (else at a word, marked with an ellipsis)."""
    if not text or len(text) <= CUSTOMERS_MAX:
        return text
    cut = text[:CUSTOMERS_MAX]
    ends = [m.end() - 1 for m in re.finditer(r"[a-z)”]\.(?=\s)", cut)]          # a sentence's end, not a list's "1."
    return cut[:ends[-1] + 1] if ends and ends[-1] >= 120 else cut.rsplit(" ", 1)[0] + "…"


def listing(payload) -> pd.DataFrame:
    """NSE's list of BRSR filings as rows: symbol, company, fy_from, fy_to, filed, revised, xbrl_url."""
    rows = payload.get("data") if isinstance(payload, dict) else payload
    out = []
    for r in rows or []:
        url = (r.get("xbrlFile") or "").strip()
        if not url.lower().endswith(".xml"):
            continue
        def dt(v):
            try:
                return datetime.strptime((v or "").strip(), "%d-%b-%Y")
            except ValueError:
                return None
        out.append({"symbol": (r.get("symbol") or "").strip(), "company": (r.get("companyName") or "").strip(),
                    "fy_from": r.get("fyFrom"), "fy_to": r.get("fyTo"), "filed": dt(r.get("submissionDate")),
                    "revised": dt(r.get("revisionDate")), "xbrl_url": url})
    return pd.DataFrame(out, columns=["symbol", "company", "fy_from", "fy_to", "filed", "revised", "xbrl_url"])


# ---- storage: one parquet, one row a filing (the lists as JSON), rebuilt into the `brsr` table
PARQUET = Path("data/brsr/brsr.parquet")
COLUMNS = ["symbol", "company", "fy_from", "fy_to", "filed", "revised", "xbrl_url", "name", "incorporated", "website", "boundary",
           "activities", "products", "plants_india", "offices_india", "plants_abroad", "offices_abroad", "states", "countries",
           "exports", "employees", "workers", "customers"]


def row_for(meta: dict, parsed: dict) -> dict:
    return {**{k: meta.get(k) for k in ("symbol", "company", "fy_from", "fy_to", "filed", "revised", "xbrl_url")},
            **{k: (json.dumps(parsed[k], ensure_ascii=False) if k in ("activities", "products") else parsed[k])
               for k in COLUMNS if k in parsed}}


def write_parquet(rows: list[dict], path: Path = PARQUET) -> int:
    if not rows:
        return 0
    df = pd.DataFrame(rows).reindex(columns=COLUMNS)
    if path.exists():
        df = pd.concat([pd.read_parquet(path), df], ignore_index=True)
    df = df.drop_duplicates(subset=["xbrl_url"], keep="last").sort_values(["symbol", "filed"])
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False, compression="zstd")
    return len(df)


def rebuild(con, path: Path = PARQUET) -> int:
    con.execute("DROP TABLE IF EXISTS brsr")
    if not path.exists():
        return 0
    con.execute("CREATE TABLE brsr AS SELECT * FROM read_parquet(?)", [str(path)])
    return con.execute("SELECT count(*) FROM brsr").fetchone()[0]
