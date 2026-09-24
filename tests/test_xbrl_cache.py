"""The local XBRL cache must be invisible in the parsed output.

What is pinned here:

* get/put round-trip the exact bytes, at data/raw_xbrl/<2 hex>/<sha1(url)>.xml.gz
* a stored copy is never overwritten, a non-XML body is never stored, and a
  damaged copy is a miss rather than an error
* feeding identical bytes through the download path and the cache path gives
  byte-identical parsed rows, for both financials_legacy.fetch_documents and
  jobs/update_financials, with the network replaced by a stub. No test here
  makes a real request: the stub session raises on any url it was not given.

Set ZEN_XBRL_SAMPLE_DIR to a directory holding sample.parquet and docs/<sha>.xml.gz
to run the same equivalence over real NSE filings as well.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from zen.data import financials as fin, financials_legacy as fl, xbrl_cache

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

HEAD = ('<?xml version="1.0" encoding="UTF-8"?>'
        '<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" '
        'xmlns:in-bse-fin="http://www.bseindia.com/xbrl/fin/2020-03-31/in-bse-fin">')
TAIL = "</xbrli:xbrl>"


def ctx(cid, start=None, end=None, instant=None):
    period = (f"<xbrli:instant>{instant}</xbrli:instant>" if instant else
              f"<xbrli:startDate>{start}</xbrli:startDate><xbrli:endDate>{end}</xbrli:endDate>")
    return (f'<xbrli:context id="{cid}"><xbrli:entity><xbrli:identifier '
            f'scheme="http://www.nseindia.com">X</xbrli:identifier></xbrli:entity>'
            f"<xbrli:period>{period}</xbrli:period></xbrli:context>")


def fact(tag, ref, value):
    return f'<in-bse-fin:{tag} contextRef="{ref}" unitRef="INR">{value}</in-bse-fin:{tag}>'


def doc(revenue, equity=5000) -> bytes:
    return (HEAD + ctx("OneD", "2025-07-01", "2025-09-30") + ctx("OneI", instant="2025-09-30")
            + fact("RevenueFromOperations", "OneD", revenue)
            + fact("ProfitLossForPeriod", "OneD", revenue // 10)
            + fact("Equity", "OneI", equity) + TAIL).encode()


BASE = "https://nsearchives.nseindia.com/corporate/xbrl/"


def synthetic_docs() -> dict[str, tuple[int, bytes]]:
    """url -> (status, body): real facts, a 404, an HTML error page served with
    a 200, and a well-formed document the parser reads as empty."""
    docs = {BASE + f"INDAS_{i}.xml": (200, doc(1000 * (i + 1), 5000 + i)) for i in range(6)}
    docs[BASE + "GONE.xml"] = (404, b"")
    docs[BASE + "HTMLERR.xml"] = (200, b"<html><body>Service unavailable")
    docs[BASE + "EMPTY.xml"] = (200, (HEAD + TAIL).encode())
    return docs


# ------------------------------------------------------------------ the stub

class FakeResponse:
    def __init__(self, status: int, content: bytes):
        self.status_code, self.content = status, content

    def raise_for_status(self):
        if self.status_code != 200:
            raise RuntimeError(f"HTTP {self.status_code}")


class StubSession:
    """Serves only the urls it was given and records every request."""

    def __init__(self, docs, calls):
        self.docs, self.calls = docs, calls

    def get(self, url, timeout=None):
        self.calls.append(url)
        if url not in self.docs:
            raise AssertionError(f"unexpected request: {url}")
        return FakeResponse(*self.docs[url])

    def close(self):
        pass


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    root = tmp_path / "raw_xbrl"
    monkeypatch.setattr(xbrl_cache, "CACHE_DIR", root)
    return root


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(fl.time, "sleep", lambda s: None)


def frame_bytes(df: pd.DataFrame) -> bytes:
    """A canonical serialisation, so 'identical' means byte-identical."""
    if df.empty:
        return b""
    df = df.sort_values(list(df.columns[:1]) + ["xbrl_url"]).reset_index(drop=True)
    buf = io.BytesIO()
    df.to_parquet(buf, index=False)
    return buf.getvalue()


# ------------------------------------------------------------------ the store

def test_path_layout(cache_dir):
    url = BASE + "INDAS_1.xml"
    h = hashlib.sha1(url.encode()).hexdigest()
    assert xbrl_cache.path_for(url) == cache_dir / h[:2] / f"{h}.xml.gz"


def test_round_trip_is_exact_and_gzip(cache_dir):
    url, body = BASE + "INDAS_1.xml", doc(123)
    assert xbrl_cache.get(url) is None
    assert xbrl_cache.put(url, body) is True
    assert xbrl_cache.get(url) == body
    stored = xbrl_cache.path_for(url).read_bytes()
    assert gzip.decompress(stored) == body
    # mtime=0 in the header: the same document always gives the same file
    assert stored == gzip.compress(body, compresslevel=6, mtime=0)
    assert not [p for p in cache_dir.rglob("*") if p.name.startswith(".tmp-")]


def test_never_overwrites(cache_dir):
    url = BASE + "INDAS_1.xml"
    assert xbrl_cache.put(url, doc(1)) is True
    assert xbrl_cache.put(url, doc(2)) is False
    assert xbrl_cache.get(url) == doc(1)


def test_non_xml_is_not_stored(cache_dir):
    url = BASE + "HTMLERR.xml"
    assert xbrl_cache.put(url, b"<html><body>Service unavailable") is False
    assert xbrl_cache.put(url, b"") is False
    assert xbrl_cache.get(url) is None
    assert not xbrl_cache.path_for(url).exists()


def test_corrupt_copy_is_a_miss_and_is_moved_aside(cache_dir):
    url, body = BASE + "INDAS_1.xml", doc(7)
    xbrl_cache.put(url, body)
    p = xbrl_cache.path_for(url)
    p.write_bytes(p.read_bytes()[:-6])                # truncated write
    assert xbrl_cache.get(url) is None
    assert not p.exists()
    assert list(p.parent.glob(p.name + ".corrupt-*"))
    assert xbrl_cache.put(url, body) is True          # slot is free again
    assert xbrl_cache.get(url) == body


def test_evict(cache_dir):
    url = BASE + "INDAS_1.xml"
    xbrl_cache.put(url, doc(1))
    assert xbrl_cache.evict(url) is True
    assert xbrl_cache.get(url) is None
    assert xbrl_cache.evict(url) is False


# ------------------------------------------- financials_legacy.fetch_documents

def legacy_index(urls) -> pd.DataFrame:
    return pd.DataFrame([{
        "symbol": f"S{i}", "company": f"Co {i}", "period_end": date(2025, 9, 30),
        "broadcast_dt": datetime(2025, 11, 1, 10) + timedelta(minutes=i), "consolidated": i % 2 == 0,
        "audited": False, "has_xbrl": True, "xbrl_url": u} for i, u in enumerate(urls)])


def run_legacy(monkeypatch, docs, calls, index):
    monkeypatch.setattr(fl, "document_session", lambda: StubSession(docs, calls))
    return fl.fetch_documents(None, index, workers=3, max_rate=0)


def uncached(monkeypatch):
    """The code path as it was before the cache: nothing read, nothing stored."""
    monkeypatch.setattr(xbrl_cache, "get", lambda url, root=None: None)
    monkeypatch.setattr(xbrl_cache, "put", lambda url, content, root=None: False)


def assert_legacy_equivalent(monkeypatch, docs):
    index = legacy_index(list(docs))
    with monkeypatch.context() as m:
        uncached(m)
        base_rows, base_bad = run_legacy(m, docs, [], index)

    first_calls, second_calls = [], []
    rows1, bad1 = run_legacy(monkeypatch, docs, first_calls, index)   # cold cache
    rows2, bad2 = run_legacy(monkeypatch, docs, second_calls, index)  # warm cache

    for rows, bad in ((rows1, bad1), (rows2, bad2)):
        assert frame_bytes(rows) == frame_bytes(base_rows)
        assert frame_bytes(bad) == frame_bytes(base_bad)
    stored = {u for u in docs if xbrl_cache.get(u) is not None}
    for u in stored:
        assert xbrl_cache.get(u) == docs[u][1]
    assert set(first_calls) == set(docs)
    # the warm run asks the network only for what could not be stored
    assert set(second_calls) == set(docs) - stored
    return base_rows, base_bad, stored


def test_fetch_documents_identical_cached_and_uncached(monkeypatch, cache_dir):
    docs = synthetic_docs()
    rows, bad, stored = assert_legacy_equivalent(monkeypatch, docs)
    assert len(rows) == 6
    assert dict(zip(bad["xbrl_url"], bad["outcome"])) == {
        BASE + "GONE.xml": "missing", BASE + "HTMLERR.xml": "empty",
        BASE + "EMPTY.xml": "empty"}
    # the parse-empty document is kept for a future parser; 404 and HTML are not
    assert stored == set(docs) - {BASE + "GONE.xml", BASE + "HTMLERR.xml"}


SAMPLE = os.environ.get("ZEN_XBRL_SAMPLE_DIR")


@pytest.mark.skipif(not SAMPLE, reason="ZEN_XBRL_SAMPLE_DIR not set")
def test_fetch_documents_identical_on_real_filings(monkeypatch, cache_dir):
    s = pd.read_parquet(Path(SAMPLE) / "sample.parquet")
    docs = {}
    for url, sha in zip(s["xbrl_url"], s["sha"]):
        with gzip.open(Path(SAMPLE) / "docs" / f"{sha}.xml.gz") as f:
            docs[url] = (200, f.read())
    rows, bad, stored = assert_legacy_equivalent(monkeypatch, docs)
    assert stored == set(docs)
    assert len(rows) + len(bad) == len(docs)


# ------------------------------------------------------ jobs/update_financials

def run_update(monkeypatch, docs, calls):
    import jobs.update_financials as uf

    listing = pd.DataFrame([{
        "symbol": f"S{i}", "company": f"Co {i}", "period_end": date(2025, 9, 30),
        "broadcast_dt": datetime(2025, 11, 1, 10) + timedelta(minutes=i), "consolidated": True,
        "audited": False, "xbrl_url": u} for i, u in enumerate(docs)])
    written = []
    monkeypatch.setattr(uf.store, "connect", lambda: duckdb.connect(":memory:"))
    monkeypatch.setattr(uf.fin, "listing", lambda start, end: listing)
    monkeypatch.setattr(uf.fin, "_session", lambda: StubSession(docs, calls))
    monkeypatch.setattr(uf.fin, "write_parquet", lambda df: written.append(df.copy()))
    monkeypatch.setattr(sys, "argv", ["update_financials", "--start", "2025-11-01",
                                      "--end", "2025-11-02"])
    uf.main()
    return pd.concat(written, ignore_index=True) if written else pd.DataFrame()


def test_update_financials_identical_cached_and_uncached(monkeypatch, cache_dir):
    docs = synthetic_docs()
    with monkeypatch.context() as m:
        uncached(m)
        base = run_update(m, docs, [])
    first, second = [], []
    cold = run_update(monkeypatch, docs, first)
    warm = run_update(monkeypatch, docs, second)
    assert len(base) == 6
    assert frame_bytes(cold) == frame_bytes(base)
    assert frame_bytes(warm) == frame_bytes(base)
    assert set(second) == {BASE + "GONE.xml", BASE + "HTMLERR.xml"}
