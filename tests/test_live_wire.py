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


def test_call_schedules_recordings_and_transcripts_are_not_material_on_the_wire():
    """29 Oct 2026: the evening's "material" filings were mostly "Link of Recording" and "Schedule of meet"."""
    subj = {"Analysts/Institutional Investor Meet/Con. Call Updates: Schedule of meet": False,
            "Recording of Analysts/Institutional Investor Meet/Con. Call: Link of Recording": False,
            "Transcript of Analysts/Institutional Investor Meet/Con. Call: Transcript": False,
            "Investor Presentation: Investor Presentation": True}
    from zen.data import filing_types
    for s, want in subj.items():
        assert live_wire.material(filing_types.categorise(s), s) is want, s
    assert live_wire.material("results", "Financial Result Updates: results for the quarter") is True
