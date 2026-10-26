"""Three things for the morning brief from zen's own tables (26 Oct 2026): the insider trades, credit
rating actions and foreign investors' sector flows that reached zen since the last brief.

  insiders   promoters', directors' and key managers' market trades broadcast in the last
             WINDOW_H hours, netted for each company and group, the largest buys and sells
             (zen.data.insider)
  ratings    every rating letter of the window whose action is more than a reaffirmation,
             assignment or withdrawal (zen.data.ratings: read from the letter by fixed rules)
  fpi        NSDL's fortnightly report, when zen first read one in the window: the sectors
             foreign investors bought and sold most (zen.data.fpi_sectors)

Each part is empty without its table, so the brief never waits on one. Facts only: what a study
of zen's history found about insider trades is on the owner's signals page, not here."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
WINDOW_H = 30
TOP = 5
NOTABLE = ("downgrade", "not cooperating", "watch negative", "outlook negative", "upgrade", "outlook positive",
           "watch positive", "watch developing")


def _tables(con) -> set:
    return {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}


def _names(con) -> str:
    """A subquery of each symbol's company name, from the filings."""
    return "(SELECT symbol, any_value(company) AS company FROM announcements GROUP BY symbol)"


def collect(con, now: datetime | None = None) -> dict:
    now = now or datetime.now(IST).replace(tzinfo=None)
    since = now - timedelta(hours=WINDOW_H)
    tabs, out = _tables(con), {}
    if "insider" in tabs:
        rows = con.execute(f"""
            WITH t AS (
              SELECT i.symbol, max(i.broadcast_dt) AS last_dt,
                CASE WHEN lower(i.category) LIKE '%promoter%' THEN 'Promoters' ELSE 'Directors or key managers' END AS who,
                sum(CASE WHEN lower(i.txn) = 'sell' THEN -1 ELSE 1 END * coalesce(i.value, 0)) AS net, count(*) AS n
              FROM insider i
              WHERE i.broadcast_dt >= ? AND i.broadcast_dt < ?
                AND lower(i.mode) IN ('market purchase', 'market sale') AND lower(i.txn) IN ('buy', 'sell')
                AND lower(coalesce(i.instrument, '')) LIKE 'equity%'
                AND regexp_matches(lower(coalesce(i.category, '')), 'promoter|director|kmp|key managerial|designated')
              GROUP BY 1, 3)
            SELECT t.symbol, coalesce(c.company, t.symbol), t.who, t.net, t.n, t.last_dt FROM t LEFT JOIN {_names(con)} c USING (symbol)
            WHERE t.net <> 0 ORDER BY abs(t.net) DESC""", [since, now]).fetchall()
        rec = [{"symbol": r[0], "company": r[1], "who": r[2], "net_cr": r[3] / 1e7, "n": r[4]} for r in rows]
        out["insiders"] = {"buys": [x for x in rec if x["net_cr"] > 0][:TOP], "sells": [x for x in rec if x["net_cr"] < 0][:TOP]}
    if "ratings" in tabs:
        rows = con.execute(f"""
            SELECT r.symbol, coalesce(c.company, r.symbol), r.action, r.actions, r.agency, r.an_dt, r.url FROM ratings r
            LEFT JOIN {_names(con)} c USING (symbol)
            WHERE r.an_dt >= ? AND r.an_dt < ? AND r.action IN ({', '.join('?' * len(NOTABLE))}) ORDER BY r.an_dt DESC""",
                           [since, now, *NOTABLE]).fetchall()
        out["ratings"] = [{"symbol": r[0], "company": r[1], "action": r[2], "agency": r[4], "url": r[6]} for r in rows]
    if "fpi_sectors" in tabs:
        last = con.execute("SELECT max(period_end), min(observed_dt) FROM fpi_sectors WHERE period_end = "
                           "(SELECT max(period_end) FROM fpi_sectors)").fetchone()
        if last and last[0] is not None and last[1] is not None and since <= last[1] < now:
            rows = con.execute("SELECT sector, net_eq_cr FROM fpi_sectors WHERE period_end = ? AND sector NOT IN ('Total', 'Sovereign', 'Others') "
                               "AND net_eq_cr IS NOT NULL ORDER BY net_eq_cr DESC", [last[0]]).fetchall()
            total = con.execute("SELECT net_eq_cr, period_start FROM fpi_sectors WHERE period_end = ? AND sector = 'Total'", [last[0]]).fetchone()
            out["fpi"] = {"end": last[0], "start": total[1] if total else None, "total_cr": total[0] if total else None,
                          "bought": [{"sector": r[0], "cr": r[1]} for r in rows[:3] if r[1] > 0],
                          "sold": [{"sector": r[0], "cr": r[1]} for r in rows[::-1][:3] if r[1] < 0]}
    return out
