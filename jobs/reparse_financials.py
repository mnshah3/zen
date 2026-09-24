"""Re-download and re-parse every stored filing into a STAGING copy of the archive.

WHY

The parser used to take income-statement figures from "the shortest span ending
latest". A filing that tags no quarter-length context then had its half-year
(or year) stored as the quarter: DELTACORP's first Sep-2025 filing is stored
with 2,619.5m of revenue that is really April to September. The fixed parser
(zen.data.financials.parse_xbrl) accepts duration figures only from a span of
at most QUARTER_MAX_DAYS and otherwise leaves them empty. The documents are not
cached, so fixing the archive means fetching every one of them again.

WHAT IT WRITES -- AND WHAT IT NEVER TOUCHES

    data/financials_staging/<same file name>.parquet    one per statement file
    data/financials_staging/_reports/<stem>.json        per-file counts
    data/financials_staging/_reports/<stem>.rows.csv    every row that changed
                                                        or could not be re-parsed
    data/financials_staging/_reports/summary.csv        one line per file
    data/financials_staging/_progress/<stem>/part-*.parquet   resume state

data/financials/ is only ever read. Swapping staging in is a separate, reviewed
step.

Each staged file has exactly the rows of its source, in the same order. The
metadata columns (symbol, company, period_end, broadcast_dt, consolidated,
audited, xbrl_url) are copied from the stored row untouched; the fact columns,
quarter_span_days and the derived columns come from the fresh parse. Columns
are written in financials.COLUMNS order, so legacy files that lacked the
balance-sheet columns gain them as nulls.

A ROW IS NEVER DROPPED

A document that cannot be re-parsed keeps its stored values and is listed in
the report with the reason:

    failed        network error or non-404 HTTP error after retries
    missing       404 -- NSE no longer serves it
    unparseable   200 but not an XBRL document
    unexplained   parses to nothing, and not because the quarter rule refused
                  anything -- a document that USED to produce figures and now
                  produces none without the rule explaining why is not trusted

Such rows get quarter_span_days = NULL, which there means "not re-parsed", not
"no quarter"; the report is the record of which is which.

RESUMING

Progress is saved every --chunk documents, keyed by xbrl_url. A re-run skips
any file already complete in staging (same source bytes, same parser), and
picks a partial file up where it stopped. Transient failures get one more pass
before a file is written; --retry-failed re-opens finished files to try their
failed, missing and unparseable documents again. If the parser changes, saved
progress from the old parser is ignored and those documents are fetched again.

NETWORK

Downloads go through financials_legacy.fetch_documents, which rate-limits all
workers against one shared ceiling (--rate, default 8 requests a second, the
project's cap for nsearchives). Nothing here talks to www.nseindia.com.

    python -m jobs.reparse_financials                          # whole archive
    python -m jobs.reparse_financials --files 2025-09.parquet  # one file
    python -m jobs.reparse_financials --retry-failed
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import logging
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from xml.etree import ElementTree as ET

import numpy as np
import pandas as pd

from zen.data import financials as fin
from zen.data import financials_legacy as fl

log = logging.getLogger(__name__)

SOURCE = fin.PARQUET_DIR
STAGE = Path("data/financials_staging")

META = ["symbol", "company", "period_end", "broadcast_dt", "consolidated",
        "audited", "xbrl_url"]
FACTS = list(dict.fromkeys(fin.TAGS.values()))
VALUE_COLS = FACTS + fin.DERIVED + ["quarter_span_days"]

# Outcomes whose fresh parse is used. Everything else keeps the stored values.
USED = ("ok", "nofacts")
KEPT = ("failed", "missing", "unparseable", "unexplained", "not_checked")


def parser_hash() -> str:
    """Identifies the parser that produced saved progress."""
    src = inspect.getsource(fin.parse_xbrl) + inspect.getsource(fin._derive)
    src += f"QUARTER_MAX_DAYS={fin.QUARTER_MAX_DAYS}"
    return hashlib.sha256(src.encode()).hexdigest()[:16]


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _is_xbrl(content: bytes) -> bool:
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return False
    return any(n.tag.split("}")[-1] == "context" or n.get("contextRef")
               for n in root.iter())


def _parse(content: bytes) -> dict:
    """Parse one document into a progress record (without the url)."""
    detail: dict = {}
    facts = fin.parse_xbrl(content, detail)
    dropped = detail.get("dropped_columns") or []
    if facts:
        outcome = "ok"
    elif not _is_xbrl(content):
        outcome = "unparseable"
    elif dropped:
        # A real filing whose only figures were a half-year or a year and
        # which has no balance sheet: nothing left to store, and correctly so.
        outcome = "nofacts"
    else:
        outcome = "unexplained"
    return {"outcome": outcome,
            "facts_json": json.dumps(facts, sort_keys=True),
            "duration_source": detail.get("duration_source"),
            "rejected_span_days": detail.get("rejected_span_days"),
            "dropped_columns": ",".join(dropped)}


@contextmanager
def _parse_in_workers():
    """Make fetch_documents hand back our parse record instead of its own.

    fetch_documents calls the module-level name `parse_xbrl` in
    financials_legacy on each downloaded body, inside its worker threads. It is
    pointed at a wrapper for the duration of the call so the rate limiter,
    retries and connection handling are reused as they are, while the job gets
    the parse detail and a way to tell "parsed to nothing" from "not XBRL".
    The wrapper never returns an empty dict, so every 200 counts as fetched.
    """
    original = fl.parse_xbrl
    fl.parse_xbrl = lambda content: {"_record": _parse(content)}
    try:
        yield
    finally:
        fl.parse_xbrl = original


def fetch(urls: pd.DataFrame, workers: int, rate: float) -> list[dict]:
    """Download and parse; one record per url, whatever happened to it."""
    if urls.empty:
        return []
    index = urls.copy()
    index["has_xbrl"] = True
    # fetch_documents silently skips rows without a period_end; every url here
    # must come back with an outcome, so none may be skipped.
    index["period_end"] = index["period_end"].fillna(pd.Timestamp("1900-01-01"))
    with _parse_in_workers():
        rows, bad = fl.fetch_documents(None, index, workers=workers, max_rate=rate)
    out = []
    for r in (rows.to_dict("records") if not rows.empty else []):
        out.append({"xbrl_url": r["xbrl_url"], **r["_record"]})
    for r in (bad.to_dict("records") if not bad.empty else []):
        # "empty" cannot happen with the wrapper; anything unexpected is a
        # failure, so it is retried rather than trusted.
        out.append({"xbrl_url": r["xbrl_url"],
                    "outcome": "missing" if r["outcome"] == "missing" else "failed",
                    "facts_json": "{}", "duration_source": None,
                    "rejected_span_days": None, "dropped_columns": ""})
    seen = {o["xbrl_url"] for o in out}
    for u in set(urls["xbrl_url"]) - seen:
        out.append({"xbrl_url": u, "outcome": "failed", "facts_json": "{}",
                    "duration_source": None, "rejected_span_days": None,
                    "dropped_columns": ""})
    return out


class Progress:
    """Append-only per-file record of parsed documents, keyed by xbrl_url."""

    def __init__(self, stem: str, phash: str):
        self.dir = STAGE / "_progress" / stem
        self.phash = phash

    def load(self) -> pd.DataFrame:
        parts = sorted(self.dir.glob("part-*.parquet"))
        if not parts:
            return pd.DataFrame(columns=["xbrl_url", "outcome"])
        df = pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)
        df = df[df["parser"] == self.phash]
        # Later parts are later attempts; the last word on a url wins.
        return df.drop_duplicates("xbrl_url", keep="last").reset_index(drop=True)

    def save(self, records: list[dict]) -> None:
        if not records:
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        # One past the highest existing part, never a count: a count reuses a
        # name if any part is missing and would overwrite a later one.
        taken = [int(p.stem.split("-")[1]) for p in self.dir.glob("part-*.parquet")]
        n = max(taken, default=-1) + 1
        df = pd.DataFrame(records)
        df["parser"] = self.phash
        df["rejected_span_days"] = df["rejected_span_days"].astype("Int64")
        tmp = self.dir / f".part-{n:05d}.tmp"
        df.to_parquet(tmp, index=False)
        os.replace(tmp, self.dir / f"part-{n:05d}.parquet")


def _values_differ(old: pd.Series, new: pd.Series) -> pd.Series:
    """Elementwise, null-aware; floats compared to 1e-9 relative."""
    o_na, n_na = old.isna().to_numpy(), new.isna().to_numpy()
    both = ~o_na & ~n_na
    diff = o_na != n_na
    o = pd.to_numeric(old, errors="coerce").to_numpy(dtype=float, na_value=np.nan)
    n = pd.to_numeric(new, errors="coerce").to_numpy(dtype=float, na_value=np.nan)
    close = np.isclose(o, n, rtol=1e-9, atol=0.0, equal_nan=True)
    diff |= both & ~close
    return pd.Series(diff, index=old.index)


def build(old: pd.DataFrame, prog: pd.DataFrame,
          only: set | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """The staged frame, and a per-row account of what happened."""
    rec = prog.set_index("xbrl_url")
    values: list[dict] = []
    account: list[dict] = []
    for i, row in enumerate(old.to_dict("records")):
        url = row["xbrl_url"]
        r = rec.loc[url] if url in rec.index else None
        if r is not None:
            outcome = r["outcome"]
        elif only is not None and url not in only:
            # Targeted mode: this document was deliberately not re-fetched.
            # It keeps its stored values and is labelled as unchecked, never
            # as a failure.
            outcome = "not_checked"
        else:
            outcome = "failed"
        if outcome in USED:
            facts = json.loads(r["facts_json"])
            fresh = fin._derive({**{k: row[k] for k in META}, **facts})
            v = {c: fresh.get(c) for c in VALUE_COLS}
            v["has_balance_sheet"] = bool(fresh["has_balance_sheet"])
        else:
            v = {c: row.get(c) for c in VALUE_COLS}
            v["quarter_span_days"] = None
        values.append(v)
        account.append({"row": i, "xbrl_url": url, "symbol": row["symbol"],
                        "outcome": outcome,
                        "dropped_columns": (r["dropped_columns"] if r is not None
                                            and outcome in USED else ""),
                        "rejected_span_days": (r["rejected_span_days"]
                                               if r is not None else None)})

    vals = pd.DataFrame(values, index=old.index)
    out = pd.DataFrame(index=old.index)
    for c in fin.COLUMNS:
        if c in META:
            out[c] = old[c]
        elif c == "has_balance_sheet":
            out[c] = vals[c].fillna(False).astype(bool)
        elif c == "quarter_span_days":
            out[c] = pd.array(vals[c].tolist(), dtype="Int64")
        else:
            out[c] = pd.to_numeric(vals[c], errors="coerce").astype("float64")

    acc = pd.DataFrame(account, index=old.index)
    compare = [c for c in FACTS + fin.DERIVED if c != "has_balance_sheet"]
    changed_cols = [[] for _ in range(len(old))]
    for c in compare:
        before = old[c] if c in old.columns else pd.Series(np.nan, index=old.index)
        d = _values_differ(before, out[c])
        for i in np.flatnonzero(d.to_numpy()):
            changed_cols[i].append(c)
    hb_old = (old["has_balance_sheet"].fillna(False).astype(bool)
              if "has_balance_sheet" in old.columns else pd.Series(False, index=old.index))
    for i in np.flatnonzero((hb_old != out["has_balance_sheet"]).to_numpy()):
        changed_cols[i].append("has_balance_sheet")
    acc["changed_columns"] = [",".join(x) for x in changed_cols]
    acc["changed"] = acc["changed_columns"] != ""
    acc["revenue_changed"] = [("revenue" in x) for x in changed_cols]
    acc["duration_dropped"] = acc["dropped_columns"].fillna("") != ""
    acc["revenue_before"] = old["revenue"] if "revenue" in old.columns else np.nan
    acc["revenue_after"] = out["revenue"]
    acc["quarter_span_days"] = out["quarter_span_days"]
    return out, acc


def _check(old: pd.DataFrame, out: pd.DataFrame) -> None:
    """Refuse to write anything that is not the same rows with the same keys."""
    if len(out) != len(old):
        raise RuntimeError(f"row count changed: {len(old)} -> {len(out)}")
    for c in META:
        if c not in old.columns:
            raise RuntimeError(f"stored file lacks metadata column {c}")
        pd.testing.assert_series_equal(old[c], out[c], check_names=True)
    if list(out.columns) != fin.COLUMNS:
        raise RuntimeError("staged columns are not financials.COLUMNS")


def process(src: Path, workers: int, rate: float, chunk: int,
            retry_failed: bool, phash: str, only: set | None = None) -> dict | None:
    stem = src.stem
    staged = STAGE / src.name
    report_path = STAGE / "_reports" / f"{stem}.json"
    src_hash = file_hash(src)

    if staged.exists() and report_path.exists():
        rep = json.loads(report_path.read_text())
        same = (rep.get("complete") and rep.get("source_sha256") == src_hash
                and rep.get("parser") == phash
                and rep.get("mode", "full") == ("targeted" if only is not None else "full"))
        if same and not (retry_failed and rep.get("rows_kept_old_values")):
            log.info("%s: complete in staging, skipped", src.name)
            return rep
        if not same:
            log.info("%s: staging is stale (source or parser changed), redoing", src.name)

    old = pd.read_parquet(src)
    if old["xbrl_url"].isna().any():
        raise RuntimeError(f"{src.name}: rows without xbrl_url cannot be re-parsed "
                           "by url; refusing rather than guessing")
    urls = (old[["symbol", "company", "period_end", "broadcast_dt", "consolidated",
                 "audited", "xbrl_url"]]
            .drop_duplicates("xbrl_url").reset_index(drop=True))
    prog = Progress(stem, phash)
    done = prog.load()
    settled = set(done.loc[done["outcome"] != "failed", "xbrl_url"])
    if retry_failed:
        settled = set(done.loc[done["outcome"].isin(USED), "xbrl_url"])
    todo = urls[~urls["xbrl_url"].isin(settled)]
    if only is not None:
        todo = todo[todo["xbrl_url"].isin(only)]
    log.info("%s: %d rows, %d documents, %d already parsed, %d to fetch",
             src.name, len(old), len(urls), len(urls) - len(todo), len(todo))

    t0 = time.monotonic()
    for attempt in (1, 2):
        for start in range(0, len(todo), chunk):
            part = todo.iloc[start:start + chunk]
            prog.save(fetch(part, workers, rate))
            n = start + len(part)
            el = time.monotonic() - t0
            log.info("%s: pass %d, %d/%d documents (%.1f/s)", src.name, attempt,
                     n, len(todo), n / el if el else 0.0)
        done = prog.load()
        failed = set(done.loc[done["outcome"] == "failed", "xbrl_url"])
        todo = urls[urls["xbrl_url"].isin(failed)]
        if only is not None:
            todo = todo[todo["xbrl_url"].isin(only)]
        if todo.empty or attempt == 2:
            break
        log.info("%s: retrying %d failed documents once", src.name, len(todo))
        t0 = time.monotonic()

    out, acc = build(old, prog.load(), only)
    _check(old, out)
    STAGE.mkdir(parents=True, exist_ok=True)
    tmp = STAGE / f".{src.name}.tmp"
    out.to_parquet(tmp, index=False, compression="zstd")
    os.replace(tmp, staged)

    kept = acc[acc["outcome"].isin(KEPT)]
    rep = {
        "file": src.name, "complete": True, "parser": phash,
        "mode": "targeted" if only is not None else "full",
        "documents_targeted": len(only & set(urls["xbrl_url"])) if only is not None else None,
        "source_sha256": src_hash, "staged": staged.as_posix(),
        "rows": int(len(old)), "documents": int(len(urls)),
        "outcomes_by_row": {k: int(v) for k, v in acc["outcome"].value_counts().items()},
        "rows_changed": int(acc["changed"].sum()),
        "rows_revenue_changed": int(acc["revenue_changed"].sum()),
        "rows_duration_dropped": int(acc["duration_dropped"].sum()),
        "rows_changed_not_by_quarter_rule": int((acc["changed"]
                                                 & ~acc["duration_dropped"]).sum()),
        "rows_kept_old_values": int(len(kept)),
        "kept_old_values": kept[["row", "xbrl_url", "symbol", "outcome"]]
        .to_dict("records"),
        "quarter_span_days": {str(k): int(v) for k, v in
                              out["quarter_span_days"].value_counts(dropna=False)
                              .sort_index().items()},
    }
    (STAGE / "_reports").mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(rep, indent=1, default=str))
    acc[acc["changed"] | acc["outcome"].isin(KEPT) | acc["duration_dropped"]].to_csv(
        STAGE / "_reports" / f"{stem}.rows.csv", index=False)
    log.info("%s: %d rows | changed %d (revenue %d) | duration dropped %d | "
             "kept old values %d", src.name, rep["rows"], rep["rows_changed"],
             rep["rows_revenue_changed"], rep["rows_duration_dropped"],
             rep["rows_kept_old_values"])
    return rep


def select(files: list[str] | None) -> list[Path]:
    everything = fin.statement_files(SOURCE)
    if not files:
        return everything
    by = {p.name: p for p in everything} | {p.stem: p for p in everything}
    unknown = [f for f in files if Path(f).name not in by]
    if unknown:
        raise SystemExit(f"not statement files in {SOURCE}: {unknown}")
    return sorted({by[Path(f).name] for f in files})


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--files", nargs="+", metavar="NAME",
                    help="statement files to re-parse, e.g. 2025-09.parquet "
                         "legacy_2019Q3 (default: all)")
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--rate", type=float, default=8.0,
                    help="combined requests per second to nsearchives (max 8)")
    ap.add_argument("--chunk", type=int, default=250,
                    help="documents between progress saves")
    ap.add_argument("--only-urls", type=Path, default=None,
                    help="re-fetch only the XBRL urls listed in this file, one per line; "
                         "every other row keeps its stored values, marked not_checked")
    ap.add_argument("--retry-failed", action="store_true",
                    help="re-open finished files to retry documents that kept old values")
    args = ap.parse_args(argv)
    if not 0 < args.rate <= 8.0:
        ap.error("--rate must be in (0, 8]: the project cap for nsearchives")
    if STAGE.resolve() == SOURCE.resolve():
        raise SystemExit("staging directory is the live archive; refusing")

    phash = parser_hash()
    files = select(args.files)
    only = None
    if args.only_urls:
        only = {u.strip() for u in args.only_urls.read_text().splitlines() if u.strip()}
        log.info("targeted mode: %d urls listed", len(only))
    log.info("re-parsing %d statement files into %s (parser %s)",
             len(files), STAGE, phash)
    summary = []
    for f in files:
        rep = process(f, args.workers, args.rate, args.chunk, args.retry_failed, phash, only)
        summary.append({k: rep[k] for k in (
            "file", "rows", "documents", "rows_changed", "rows_revenue_changed",
            "rows_duration_dropped", "rows_changed_not_by_quarter_rule",
            "rows_kept_old_values")})
    if summary:
        s = pd.DataFrame(summary)
        # Merge with earlier runs' lines so the summary covers the archive.
        path = STAGE / "_reports" / "summary.csv"
        if path.exists():
            prev = pd.read_csv(path)
            s = pd.concat([prev[~prev["file"].isin(s["file"])], s]).sort_values("file")
        s.to_csv(path, index=False)
        print(s.to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
