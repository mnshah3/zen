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
