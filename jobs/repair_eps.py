"""Put right the stored EPS of the filings that state it on another line (zen/data/financials.py, EPS ON
ANOTHER LINE), in place in data/financials/*.parquet, and list every change in data/reference/eps_repairs.csv.

    python -m jobs.repair_eps                                   # what would change, written to the list only
    python -m jobs.repair_eps --apply                           # and the parquet files
    python -m jobs.repair_eps --apply --cache ../zen/data/raw_xbrl   # another checkout's XBRL copy, read first

WHICH FILINGS. Those stored with an EPS of 0 beside a profit, or of the opposite sign to it: 2,489 of the
111,267 on 20 Nov 2026. Each is read again from its own document (zen's local XBRL copy, else NSE's
archive), with the parser that decides from the document's own lines, and only its EPS (basic and
diluted) and the share count derived from it (financials._derive) are changed; every other column stays
as stored. A document that cannot be read leaves its row as it is, and the list says so.

Why it matters: an EPS of 0 where the quarter earned something makes the trailing EPS too small and the
P/E history jump (Shaily Engineering: 74 to 390, 2026), which is what a five-year P/E chart then shows.
"""

from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

import numpy as np
import pandas as pd

from zen.data import financials as fin, financials_legacy as leg, xbrl_cache

log = logging.getLogger("repair_eps")
LIST = Path("data/reference/eps_repairs.csv")


def suspects(df: pd.DataFrame) -> pd.Series:
    """Rows whose EPS is 0 beside a profit, or of the opposite sign to it (|EPS| at least 0.2)."""
    profit = df["profit_continuing"].where(df["profit_continuing"].notna(), df["profit_reported"])
    eps = df["eps_basic"]
    zero = eps.eq(0) & profit.notna() & profit.ne(0)
    sign = eps.abs().ge(0.2) & profit.notna() & profit.ne(0) & (np.sign(eps) != np.sign(profit))
    return zero | sign


def document(url: str, session, caches=()) -> bytes | None:
    """The filing's XBRL: zen's local copy (the URL as stored, or on NSE's other host name), else downloaded."""
    alt = url.replace("nseindia.in/", "nseindia.com/") if "nseindia.in/" in url else url.replace("nseindia.com/", "nseindia.in/")
    for root in (None, *caches):
        for u in (url, alt):
            b = xbrl_cache.get(u, root=root) if root is not None else xbrl_cache.get(u)
            if b is not None:
                return b
    for k in range(3):
        try:
            r = session.get(url, timeout=30)
            if r.status_code == 200 and r.content.lstrip()[:1] == b"<":
                xbrl_cache.put(url, r.content)
                return r.content
            if r.status_code == 404:
                return None
        except Exception:                                            # noqa: BLE001
            time.sleep(2 * (k + 1))
    return None


def repaired(row: dict, content: bytes) -> dict | None:
    """{eps_basic, eps_diluted, shares_implied, fix} for a stored row from its document, or None when the
    document leaves its EPS as stored."""
    det: dict = {}
    f = fin.parse_xbrl(content, detail=det)
    if not det.get("eps_fix"):
        return None
    new = {"eps_basic": f.get("eps_basic"), "eps_diluted": f.get("eps_diluted")}
    same = lambda a, b: (a is None or pd.isna(a)) and (b is None or pd.isna(b)) or (a is not None and b is not None and not pd.isna(a) and not pd.isna(b) and float(a) == float(b))  # noqa: E731
    if same(new["eps_basic"], row.get("eps_basic")) and same(new["eps_diluted"], row.get("eps_diluted")):
        return None
    d = fin._derive({**row, **new})
    return {**new, "shares_implied": d["shares_implied"], "fix": det["eps_fix"]}


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--cache", action="append", default=[], help="another XBRL copy to read (read-only)")
    a = ap.parse_args(argv)
    caches = [Path(p) for p in a.cache if Path(p).exists()]
    s = leg.document_session()
    changes, unread = [], []
    for path in fin.statement_files():
        df = pd.read_parquet(path)
        cols = list(df.columns)
        if not {"eps_basic", "profit_reported", "xbrl_url"} <= set(cols):
            continue
        if "profit_continuing" not in df:
            df["profit_continuing"] = np.nan
        hit = df.index[suspects(df)]
        if not len(hit):
            continue
        touched = False
        for i in hit:
            row = df.loc[i].to_dict()
            content = document(row["xbrl_url"], s, caches)
            if content is None:
                unread.append({"file": path.name, "symbol": row["symbol"], "period_end": row["period_end"], "xbrl_url": row["xbrl_url"]})
                continue
            r = repaired(row, content)
            if r is None:
                continue
            changes.append({"file": path.name, "symbol": row["symbol"], "period_end": pd.Timestamp(row["period_end"]).date(),
                            "consolidated": bool(row["consolidated"]), "broadcast_dt": row["broadcast_dt"],
                            "profit": row.get("profit_continuing") if pd.notna(row.get("profit_continuing")) else row.get("profit_reported"),
                            "eps_was": row.get("eps_basic"), "eps_now": r["eps_basic"], "fix": r["fix"], "xbrl_url": row["xbrl_url"]})
            for c in ("eps_basic", "eps_diluted", "shares_implied"):
                df.at[i, c] = np.nan if r[c] is None else r[c]
            touched = True
        if touched and a.apply:
            df[cols].to_parquet(path, index=False, compression="zstd")
            log.info("%s: %d rows put right", path.name, sum(1 for c in changes if c["file"] == path.name))
    out = pd.DataFrame(changes)
    LIST.parent.mkdir(parents=True, exist_ok=True)
    # the list is the record of every repair applied: a later run (a newly filed quarter, a file rewritten
    # by the daily job) adds to it rather than replacing it
    whole = out
    if a.apply and LIST.exists():
        old = pd.read_csv(LIST)
        whole = pd.concat([old[~old["xbrl_url"].isin(set(out["xbrl_url"]) if len(out) else set())], out], ignore_index=True)
    whole.to_csv(LIST, index=False)
    log.info("%d EPS put right (%s), %d documents not read%s", len(out),
             out["fix"].value_counts().to_dict() if len(out) else {}, len(unread), "" if a.apply else "; nothing written but the list")
    if unread:
        pd.DataFrame(unread).to_csv(LIST.with_name("eps_repairs_unread.csv"), index=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
