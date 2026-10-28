"""jobs.live_wire: the day's filings, newest first, for the site's wire."""
import pandas as pd

from jobs import live_wire


def test_the_wire_is_newest_first_with_the_archives_categories():
    df = pd.DataFrame({"an_dt": pd.to_datetime(["2026-10-27 21:02:00", "2026-10-27 21:05:51"]), "symbol": ["PPLPHARMA", "PPLPHARMA"],
                       "company": ["Piramal Pharma Limited"] * 2, "category": ["results", "guidance"],
                       "subject": ["Outcome of Board Meeting: results for the period ended September 30, 2026.", "Investor Presentation"],
                       "url": ["u1", "u2"]})
    out = live_wire.build(df)
    assert out["cols"] == ["t", "s", "n", "cat", "material", "gist", "url"] and out["n"] == 2
    assert [r[0] for r in out["rows"]] == ["2026-10-27 21:05:51", "2026-10-27 21:02:00"]
    assert out["rows"][1][3] == "results" and out["rows"][1][4] is True and out["rows"][1][6] == "u1"
