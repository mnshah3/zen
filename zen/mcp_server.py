"""zen as an MCP server: Claude (or any MCP client) can query zen's archive, read-only and
point in time.

    python C:/Users/mnsha/projects/zen/zen/mcp_server.py        # stdio; normally started by the client

Register it with Claude Code (once, from any folder):

    claude mcp add zen --scope user -- <zen>/.venv/Scripts/python.exe <zen>/zen/mcp_server.py

The tools are thin wrappers over zen/research_api.py, which holds the logic and the tests. The
`mcp` package is a local tool only (requirements-mcp.txt): nothing scheduled imports this file.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)                                  # zen's modules read data/ and state/ relative to the repo
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcp.server.fastmcp import FastMCP          # noqa: E402

from zen import research_api as api             # noqa: E402

mcp = FastMCP(
    "zen",
    instructions=(
        "zen is a point-in-time archive of Indian equities (NSE): prices since 2015 including "
        "delisted companies, quarterly results and filings as broadcast, shareholding and pledges. "
        "Pass as_of (YYYY-MM-DD) to see only what was public by the end of that day; this is how "
        "to avoid look-ahead. Prices from price_history are as traded, not split-adjusted. "
        "Lenders filing in the banking format have only total income in quarterly_results; "
        "bank_results has their net interest income, provisions, profit and NPAs."),
)


@mcp.tool()
def search_companies(query: str, limit: int = 10) -> list[dict]:
    """Find NSE companies by symbol prefix or part of the name."""
    return api.search_companies(query, limit)


@mcp.tool()
def fundamentals(symbol: str, as_of: str | None = None) -> dict:
    """zen's point-in-time fundamentals for a company (trailing revenue, EBITDA, profit, shares)."""
    return api.fundamentals(symbol, as_of)


@mcp.tool()
def quarterly_results(symbol: str, as_of: str | None = None, quarters: int = 8) -> dict:
    """Quarterly results as filed and known by as_of, latest revision of each quarter, Rs crore."""
    return api.quarterly_results(symbol, as_of, quarters)


@mcp.tool()
def bank_results(symbol: str, as_of: str | None = None, quarters: int = 8) -> dict:
    """A lender's quarters from its banking-format filings: net interest income, provisions, net profit, EPS, NPAs."""
    return api.bank_results(symbol, as_of, quarters)


@mcp.tool()
def filings(symbol: str, since: str | None = None, until: str | None = None, material_only: bool = True,
            limit: int = 30) -> list[dict]:
    """A company's NSE filings between two dates, newest first, with links to the documents."""
    return api.filings(symbol, since, until, material_only, limit)


@mcp.tool()
def shareholding(symbol: str, as_of: str | None = None, periods: int = 8) -> list[dict]:
    """Promoter and public holding by quarter, as last filed by as_of."""
    return api.shareholding(symbol, as_of, periods)


@mcp.tool()
def price_history(symbol: str, start: str | None = None, end: str | None = None) -> dict:
    """Daily prices as traded (not split-adjusted), at most 1,500 sessions."""
    return api.price_history(symbol, start, end)


@mcp.tool()
def corporate_actions(symbol: str, as_of: str | None = None) -> list[dict]:
    """Splits, bonuses and other corporate actions up to as_of."""
    return api.corporate_actions(symbol, as_of)


@mcp.tool()
def market_on(day: str | None = None) -> dict:
    """Breadth and index closes for a session from zen's archive."""
    return api.market_on(day)


if __name__ == "__main__":
    mcp.run()
