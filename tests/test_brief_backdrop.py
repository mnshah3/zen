"""The morning brief's market backdrop (zen.monitor.backdrop) and its rendering."""

from datetime import date, datetime

import duckdb
import pandas as pd

from zen.monitor import backdrop
from zen.notify import render


def _con(rows):
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE indices (date DATE, index_name VARCHAR, close DOUBLE, pct_change DOUBLE, pe DOUBLE)")
    con.executemany("INSERT INTO indices VALUES (?, ?, ?, ?, ?)", rows)
    return con


def test_indices_for_the_session_only_and_sectors_ranked():
    d0, d1 = date(2026, 10, 5), date(2026, 10, 6)
    con = _con([(d0, "Nifty 50", 22000.0, 0.1, 19.0), (d1, "Nifty 50", 22132.0, 0.6, 19.3),
                (d1, "Nifty IT", 100.0, -1.2, 25.0), (d1, "Nifty Metal", 100.0, 2.1, 12.0),
                (d0, "Nifty Bank", 50000.0, 0.3, 13.0)])          # no print on d1: left out
    bd = backdrop.indices(con, d1)
    assert [r["name"] for r in bd["headline"]] == ["Nifty 50"]
    assert [r["name"] for r in bd["sectors"]] == ["Nifty Metal", "Nifty IT"]
    assert bd["nifty_pe"] == 19.3


def test_global_item_change_and_staleness():
    pts = [(date(2026, 10, 5), 5.31), (date(2026, 10, 6), 5.27)]
    it = backdrop._item("US 10-year", "yield", pts, date(2026, 10, 8))
    assert round(it["change"], 6) == -4.0
    lvl = backdrop._item("S&P 500", "level", [(date(2026, 10, 6), 100.0), (date(2026, 10, 7), 101.0)],
                         date(2026, 10, 8))
    assert round(lvl["change"], 6) == 1.0
    assert backdrop._item("Brent", "level", pts, date(2026, 10, 20)) is None     # older than MAX_AGE


def test_global_backdrop_never_raises(monkeypatch):
    def boom(*a, **k):
        raise OSError("offline")
    monkeypatch.setattr(backdrop, "_fred", boom)
    monkeypatch.setattr(backdrop, "_fx", boom)
    assert backdrop.global_backdrop(date(2026, 10, 8)) == []


def _market(adv, dec):
    return {"breadth": {"advancers": adv, "decliners": dec, "unchanged": 0, "median_ret": -0.5,
                        "turnover_cr": 118036.0}, "session": "2026-10-06"}


def _bd(pct):
    return {"headline": [{"name": "Nifty 50", "close": 22132.0, "pct": pct, "pe": 19.3}]}


def test_verdict_reads_the_index_and_breadth_together():
    narrow = {"divergence": {"diverging": True, "gap": 2.8}}
    assert render._verdict(_market(928, 1579), narrow, _bd(0.6)) == \
        "Nifty 50 rose 0.60% to 22,132 but decliners led 1,579 to 928"
    assert render._verdict(_market(928, 1579), narrow, _bd(-0.6)) == \
        "Nifty 50 fell 0.60% to 22,132 and decliners led 1,579 to 928"
    assert render._verdict(_market(1800, 700), {}, _bd(1.1)).startswith("Nifty 50 rose 1.10% to 22,132 with broad buying")
    assert render._verdict(_market(1800, 700), {}, _bd(-0.2)) == \
        "Nifty 50 fell 0.20% to 22,132 but advancers led 1,800 to 700"
    assert render._verdict(_market(1200, 1150), {}, None) == "1,200 up, 1,150 down. A mixed session"
    for v in (render._verdict(_market(928, 1579), narrow, None), render._verdict(_market(1, 2), {}, _bd(0))):
        assert "—" not in v


def test_publisher_suffix_is_cleaned_from_headlines():
    assert render._clean_title("S&P 500 ends down as yields rise - Reuters", "Reuters Business") == \
        "S&P 500 ends down as yields rise"
    assert render._clean_title("Tata Motors | Q2 preview", "Mint") == "Tata Motors"
    assert render._clean_title("Oil surges 5% on tanker attacks", "ET Markets") == "Oil surges 5% on tanker attacks"


def test_brief_renders_with_and_without_the_backdrop():
    base = dict(sections={}, market=_market(928, 1579), insights={}, charts={}, explanations={},
                facts_map={}, bridge_text="", filings=None, when=datetime(2026, 10, 7, 7, 0))
    subj, html, _ = render.daily_brief(**base)
    assert "Morning Brief" in html and "Markets" not in html
    bd = {"headline": [{"name": "Nifty 50", "close": 22132.0, "pct": 0.6, "pe": 19.3}],
          "sectors": [{"name": "Nifty Metal", "close": 1.0, "pct": 2.1, "pe": None},
                      {"name": "Nifty IT", "close": 1.0, "pct": -1.2, "pe": None}], "nifty_pe": 19.3}
    glob = [{"label": "S&P 500", "value": 7801.77, "change": -0.22, "kind": "level", "date": date(2026, 10, 7)},
            {"label": "US 10-year", "value": 5.27, "change": -4.0, "kind": "yield", "date": date(2026, 10, 6)}]
    subj, html, _ = render.daily_brief(**base, backdrop=bd, global_items=glob)
    assert "Nifty 50 +0.60%" in subj
    for s in ("22,132.00", "Metal", "&minus;1.20%", "7,801.77", "&minus;4 bp", "Overnight"):
        assert s in html, s


def test_filing_gist_drops_the_exchange_boilerplate():
    from zen.data.filing_types import gist
    assert gist("Bagging/Receiving of orders/contracts: GPT Infraprojects Limited has informed the Exchange "
                "about Bagging/Receiving of orders/contracts of value Rs 114.82 Crore") == \
        "Bagging/Receiving of orders/contracts of value Rs 114.82 Crore"
    long = gist("Spurt in Volume: " + "word " * 80, 50)
    assert long.endswith("…") and len(long) <= 51 and not long[:-1].endswith(" ")


def test_dismissal_filter_catches_does_not_reach():
    from jobs.daily_brief import IRRELEVANT
    assert IRRELEVANT.search("This development does not reach Indian equities.")


def test_no_control_characters_in_the_changed_sources():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    for rel in ("zen/data/filing_types.py", "zen/monitor/bridge.py", "zen/monitor/backdrop.py",
                "zen/notify/render.py", "zen/monitor/explain.py", "jobs/daily_brief.py"):
        text = (root / rel).read_text(encoding="utf-8")
        assert not any(ord(c) < 32 and c not in "\n\t\r" for c in text), rel
