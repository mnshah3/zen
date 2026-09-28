"""Shared test set-up: tests that need the local archive skip cleanly without it.

data/zen.duckdb is built locally from the committed parquet files
(`python -m jobs.rebuild_db`) and is not in the repository, and some reference
files under data/external are not ours to redistribute (data/external/
SOURCES.md says how to fetch them). On a fresh clone the tests that read them
cannot run. They are reported as skipped with that instruction, not as
failures, and nothing may create an empty archive in the meantime: before this
file existed, one test's connection created an empty data/zen.duckdb and every
later test then failed against empty tables.

With the archive present, as on the machine that produces the results,
nothing here changes anything: every failure is reported as a failure.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

REPO = Path(__file__).resolve().parents[1]
DB = REPO / "data" / "zen.duckdb"
SKIP_REASON = ("needs the local archive or external data: build it with "
               "`python -m jobs.rebuild_db` and see data/external/SOURCES.md")


def _archive_ready() -> bool:
    """True only for a built archive: the file exists and has price rows."""
    if not DB.exists():
        return False
    try:
        con = duckdb.connect(str(DB), read_only=True)
        try:
            return con.execute("SELECT count(*) FROM prices").fetchone()[0] > 0
        finally:
            con.close()
    except Exception:
        return False


ARCHIVE_READY = _archive_ready()        # decided once, before any test runs


class ArchiveMissing(FileNotFoundError):
    """The local archive was needed and is not built."""


@pytest.fixture(autouse=True, scope="session")
def _no_empty_archive():
    """Without a built archive, opening the default one raises instead of
    creating it empty. Connections to other paths (synthetic archives in tmp
    folders) are untouched."""
    if ARCHIVE_READY:
        yield
        return
    from zen.data import store
    real = store.connect

    def guarded(path: Path = store.DB_PATH):
        p = Path(path)
        p = p if p.is_absolute() else Path.cwd() / p
        if p.resolve() == DB.resolve():
            raise ArchiveMissing(str(DB))
        return real(path)

    store.connect = guarded
    try:
        yield
    finally:
        store.connect = real


_NEEDS_DATA = (ArchiveMissing, duckdb.IOException, duckdb.CatalogException,
               duckdb.BinderException)


def _needs_data(exc: BaseException) -> bool:
    if isinstance(exc, _NEEDS_DATA):
        return True
    if isinstance(exc, FileNotFoundError):
        where = str(getattr(exc, "filename", "") or exc).replace("\\", "/")
        return "/data/" in where or where.startswith("data/")
    return False


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Without a built archive only: a test that stopped because the archive or
    an external data file is missing is reported as skipped, not failed."""
    outcome = yield
    rep = outcome.get_result()
    if ARCHIVE_READY or call.excinfo is None or rep.skipped:
        return
    if _needs_data(call.excinfo.value):
        rep.outcome = "skipped"
        rep.longrepr = (str(item.path), item.location[1] or 0, f"Skipped: {SKIP_REASON}")
