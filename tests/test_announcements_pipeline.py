"""The announcement pipeline end to end: fetch -> upsert -> parquet -> brief.

Every test here is a contract that broke silently once. From 10 September 2026
fetch() produced ten columns for an eleven-column table, so each daily refresh
raised a Binder Error that the brief caught; new filings were categorised with
an old regex the archive no longer used; the brief asked for categories
("volume_query", "litigation") that no longer existed; and filings were never
committed.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from jobs import data_health
from zen.data import announcements as ann, filing_types, freshness
from zen.monitor import bridge
from zen.notify import render


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _Session:
    def __init__(self, payload):
        self.payload = payload

    def get(self, url, timeout=None):
        return _Resp(self.payload)


def _row(an_dt, symbol, desc, body=""):
    return {"an_dt": an_dt, "symbol": symbol, "desc": desc, "attchmntText": body,
            "sm_isin": "INE000000001", "sm_name": f"{symbol} Ltd", "smIndustry": "X",
            "attchmntFile": "https://example/x.pdf", "hasXbrl": False}


PAYLOAD = [
    _row("02-Oct-2026 11:00:00", "AAA", "Outcome of Board Meeting",
         "approved the unaudited financial results for the quarter"),
    _row("02-Oct-2026 17:10:00", "BBB", "Copy of Newspaper Publication", "results advert"),
    _row("05-Oct-2026 10:00:00", "CCC", "Updates", "business update"),
]


def _con():
    con = duckdb.connect()
    ann.ensure_schema(con)
    con.execute("CREATE TABLE prices (date DATE, symbol VARCHAR, series VARCHAR, "
                "isin_code VARCHAR)")
    # AAA trades on the 2nd; BBB (filed after the close on the 2nd) next trades
    # on the 5th; nothing has traded for CCC since its filing yet.
    con.executemany("INSERT INTO prices VALUES (?, ?, 'EQ', 'INE000000001')", [
        (date(2026, 10, 2), "AAA"), (date(2026, 10, 1), "BBB"),
        (date(2026, 10, 5), "BBB"), (date(2026, 10, 1), "CCC")])
    return con


def test_fetch_returns_the_table_columns_and_the_archive_categories():
    df = ann.fetch(date(2026, 10, 2), date(2026, 10, 2), session=_Session(PAYLOAD))
    assert list(df.columns) == ann.COLUMNS
    assert df["session_date"].isna().all()
    for subject, category in zip(df["subject"], df["category"]):
        assert category == filing_types.categorise(subject)


def test_fetch_strict_raises_instead_of_skipping_a_failed_day():
    class Broken:
        def get(self, url, timeout=None):
            raise ConnectionError("blocked")
    assert ann.fetch(date(2026, 10, 2), date(2026, 10, 2), session=Broken()).empty
    with pytest.raises(ConnectionError):
        ann.fetch(date(2026, 10, 2), date(2026, 10, 2), session=Broken(), strict=True)


def test_upsert_inserts_into_the_eleven_column_table_and_fills_sessions():
    con = _con()
    df = ann.fetch(date(2026, 10, 2), date(2026, 10, 2), session=_Session(PAYLOAD))
    assert ann.upsert(con, df) == 3
    got = dict(con.execute("SELECT symbol, session_date FROM announcements").fetchall())
    assert got["AAA"] == date(2026, 10, 2)         # filed in hours, traded that day
    assert got["BBB"] == date(2026, 10, 5)         # after the close: next session it traded
    assert got["CCC"] is None                      # no session yet: filled later
    assert ann.upsert(con, df) == 0                # idempotent on the key


def test_write_parquet_keeps_every_row_and_takes_the_database_session(tmp_path):
    con = _con()
    df = ann.fetch(date(2026, 10, 2), date(2026, 10, 2), session=_Session(PAYLOAD))
    ann.write_parquet(df, out_dir=tmp_path)                    # first: no sessions
    ann.upsert(con, df)
    ann.write_parquet(df, out_dir=tmp_path, con=con)           # then: database wins
    stored = pd.read_parquet(tmp_path / "2026" / "2026-10.parquet")
    assert len(stored) == 3
    assert set(stored.columns) == set(ann.COLUMNS)
    assert str(stored["session_date"].dtype) == "datetime64[us]"
    by = stored.set_index("symbol")["session_date"]
    assert by["BBB"] == pd.Timestamp("2026-10-05")
    assert pd.isna(by["CCC"])


def test_write_parquet_never_adds_database_rows(tmp_path):
    """A row stored in the file of the month before its trade_date (70 legacy
    rows are) must not be copied into its trade_date month as well: the key
    would then exist twice and rebuild_from_parquet would fail on every job."""
    con = _con()
    df = ann.fetch(date(2026, 10, 2), date(2026, 10, 2), session=_Session(PAYLOAD))
    ann.upsert(con, df)
    legacy = ann._typed(df[df["symbol"] == "AAA"])
    (tmp_path / "2026").mkdir()
    legacy.to_parquet(tmp_path / "2026" / "2026-09.parquet", index=False)
    ann.write_parquet(df[df["symbol"] != "AAA"], out_dir=tmp_path, con=con)
    stored = pd.read_parquet(tmp_path / "2026" / "2026-10.parquet")
    assert "AAA" not in set(stored["symbol"])
    fresh = duckdb.connect()
    assert ann.rebuild_from_parquet(fresh, out_dir=tmp_path) == 3


def test_refetch_without_database_keeps_the_stored_session(tmp_path):
    con = _con()
    df = ann.fetch(date(2026, 10, 2), date(2026, 10, 2), session=_Session(PAYLOAD))
    ann.upsert(con, df)
    ann.write_parquet(df, out_dir=tmp_path, con=con)
    ann.write_parquet(df, out_dir=tmp_path)          # a refetch: sessions are null
    stored = pd.read_parquet(tmp_path / "2026" / "2026-10.parquet").set_index("symbol")
    assert stored.loc["BBB", "session_date"] == pd.Timestamp("2026-10-05")


def test_refetch_of_a_row_held_by_the_previous_month_is_not_duplicated(tmp_path):
    con = _con()
    df = ann.fetch(date(2026, 10, 2), date(2026, 10, 2), session=_Session(PAYLOAD))
    (tmp_path / "2026").mkdir()
    ann._typed(df[df["symbol"] == "AAA"]).to_parquet(
        tmp_path / "2026" / "2026-09.parquet", index=False)    # misfiled, as 70 rows are
    ann.write_parquet(df, out_dir=tmp_path)                    # a refetch of all three
    assert ann.rebuild_from_parquet(duckdb.connect(), out_dir=tmp_path) == 3


ROOT = Path(__file__).resolve().parents[1]


def test_committed_archive_has_each_filing_once():
    archive = ROOT / "data" / "announcements"
    if not list(archive.rglob("*.parquet")):
        pytest.skip("no announcements archive in this checkout")
    con = duckdb.connect()
    total, unique = con.execute(
        "SELECT count(*), count(DISTINCT (an_dt, symbol, subject)) FROM "
        f"read_parquet('{archive.as_posix()}/**/*.parquet', union_by_name=true)").fetchone()
    assert total == unique, f"{total - unique} filings stored twice"


def test_for_session_reads_session_date_not_trade_date():
    con = _con()
    df = ann.fetch(date(2026, 10, 2), date(2026, 10, 2), session=_Session(PAYLOAD))
    ann.upsert(con, df)
    con.execute("UPDATE announcements SET category = 'results'")
    # BBB was filed after the close on Friday 2 Oct, so its trade_date is
    # Monday 5 Oct, and the 5th is also its session.
    assert set(ann.for_session(con, date(2026, 10, 5))["symbol"]) == {"BBB"}
    assert set(ann.for_session(con, date(2026, 10, 2))["symbol"]) == {"AAA"}


def test_brief_vocabulary_matches_the_archive_categories():
    known = set(filing_types.SIGN)
    assert ann.MATERIAL <= known, ann.MATERIAL - known
    assert set(bridge.LABELS) == ann.MATERIAL
    assert set(render.FILING_LABELS) == ann.MATERIAL


def test_freshness_round_trip(tmp_path):
    p = tmp_path / "f.json"
    when = datetime(2026, 10, 4, 15, 0, tzinfo=timezone.utc)
    freshness.record("announcements", path=p, when=when)
    assert freshness.read(p) == {"announcements": "2026-10-04T15:00:00+00:00"}


def test_data_health_flags_stale_sources(monkeypatch):
    now = datetime(2026, 10, 9, 15, 0, tzinfo=timezone.utc)
    con = duckdb.connect()
    con.execute("CREATE TABLE prices (date DATE)")
    con.execute("CREATE TABLE indices (date DATE)")
    con.execute("CREATE TABLE announcements (an_dt TIMESTAMP)")
    con.execute("CREATE TABLE financials (broadcast_dt TIMESTAMP)")
    con.execute("INSERT INTO prices VALUES ('2026-10-08')")
    con.execute("INSERT INTO indices VALUES ('2026-10-08')")
    con.execute("INSERT INTO announcements VALUES ('2026-09-09 23:58:05')")   # the real gap
    con.execute("INSERT INTO financials VALUES ('2026-09-30 10:00:00')")
    fresh = (now - timedelta(hours=6)).isoformat()
    monkeypatch.setattr(freshness, "read", lambda *a, **k: {
        "announcements": fresh, "financials": fresh})                     # corpactions missing
    rows = {name: ok for name, _, ok in data_health.check(con, now=now)}
    assert rows["prices"] and rows["indices"] and rows["financials"]
    assert not rows["announcements"]
    assert rows["announcements job"] and rows["financials job"]
    assert not rows["corpactions job"]


@pytest.mark.parametrize("latest, now, ok", [
    ("2022-04-13", datetime(2022, 4, 18, 1, 45, tzinfo=timezone.utc), True),   # Thu+Fri holidays: 3 weekdays
    ("2026-10-01", datetime(2026, 10, 5, 1, 45, tzinfo=timezone.utc), True),   # 2 Oct holiday: 2 weekdays
    ("2026-09-28", datetime(2026, 10, 2, 15, 0, tzinfo=timezone.utc), False),  # 4 weekdays: stale
])
def test_data_health_counts_price_lag_in_weekdays(latest, now, ok, monkeypatch):
    con = duckdb.connect()
    for t, col in (("prices", "date DATE"), ("indices", "date DATE"),
                   ("announcements", "an_dt TIMESTAMP"), ("financials", "broadcast_dt TIMESTAMP")):
        con.execute(f"CREATE TABLE {t} ({col})")
    con.execute("INSERT INTO prices VALUES (?)", [latest])
    con.execute("INSERT INTO indices VALUES (?)", [latest])
    monkeypatch.setattr(freshness, "read", lambda *a, **k: {})
    rows = {name: good for name, _, good in data_health.check(con, now=now)}
    assert rows["prices"] is ok and rows["indices"] is ok
