"""Zen: research infrastructure for Indian equities.

Loads .env on import so a local run sees the same variable names GitHub
Actions injects as secrets. python-dotenv was already a dependency and was
never actually called, which is why local runs silently had no Gemini key and
produced a degraded brief that looked like a code fault rather than a missing
variable. Values already present in the environment always win, so this can
never override what CI sets.
"""

from __future__ import annotations

try:
    from dotenv import load_dotenv

    load_dotenv(override=False)
except Exception:  # noqa: BLE001 - a missing .env must never stop a job
    pass


# Cap DuckDB's disk spill for every connection the project opens.
#
# On 2026-09-14 one badly shaped query (a correlated subquery over the full
# price table) spilled 108 GB into ./.tmp, was killed, and left the files
# behind. DuckDB's default spill limit is 90% of free disk, so nothing stopped
# it. With this cap a runaway query fails with an out-of-space error at 20 GB
# instead of filling the laptop. Callers that pass their own value keep it.
try:
    import duckdb as _duckdb

    _connect = _duckdb.connect

    def _capped_connect(database=":memory:", read_only=False, config=None, **kw):
        config = dict(config or {})
        config.setdefault("max_temp_directory_size", "20GB")
        return _connect(database, read_only=read_only, config=config, **kw)

    _duckdb.connect = _capped_connect
except Exception:  # noqa: BLE001 - duckdb is optional for some jobs
    pass
