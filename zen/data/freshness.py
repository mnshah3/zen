"""When each data source last refreshed successfully.

A refresh that fails inside a job that is designed never to fail (the brief
always sends) leaves no trace in the job's status. From 10 September 2026 the
announcement refresh failed on every run for over three weeks while every
workflow stayed green. Each update job now records its own success here, and
jobs.data_health reads both this file and the data itself and fails loudly
when anything is stale.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

STATE = Path("state/data_freshness.json")


def read(path: Path = STATE) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def record(source: str, path: Path = STATE, when: datetime | None = None) -> None:
    """Stamp `source` as refreshed now (UTC)."""
    data = read(path)
    data[source] = (when or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")
