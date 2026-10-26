"""Credit rating actions read from the filed letters by fixed rules (zen.data.ratings)."""

from datetime import datetime

import pandas as pd
import pytest

from zen.data import ratings as R


@pytest.mark.parametrize("text, head, acts", [
    ("You may please note that ICRA has upgraded the long-term rating to [ICRA]A+ (Stable).", "upgrade", ["upgrade"]),
    ("CARE A+; Stable (Revised from CARE A; Stable) and CARE A1+ (Reaffirmed)", "upgrade", ["upgrade", "reaffirmed"]),
    ("[ICRA]BBB(Negative); revised from [ICRA]A-(Stable)", "downgrade", ["downgrade"]),
    ("Outlook revised to Negative; rating reaffirmed at CRISIL A", "outlook negative", ["outlook negative", "reaffirmed"]),
    ("IND BB+ (ISSUER NOT COOPERATING) was assigned on non-cooperation by the issuer", "not cooperating", ["not cooperating"]),
    ("Crisil AA+; Rating Watch with Developing Implications. Crisil Ratings has assigned its rating", "watch developing",
     ["watch developing", "assigned"]),
    ("The ratings were withdrawn at the request of the company, the bonds having been redeemed.", "withdrawn", ["withdrawn"]),
    # CONTROLPR, 2 Sep 2026: out of non-cooperation, to a higher grade; earlier mentions are the old state
    ("Crisil A/Stable (Migrated from 'Crisil BB/Stable ISSUER NOT COOPERATING'). It was in the Issuer Not Cooperating category",
     "upgrade", ["upgrade"]),
    # KRISHANA, 8 Oct 2026: a watch lifted is not a watch
    ("Crisil A+/Stable (Removed from 'Rating Watch with Developing Implications'); placed on Rating Watch with Developing Implications in May",
     None, []),
    ("Long Term rating continues to be on Rating Watch with Developing Implications", "watch developing", ["watch developing"]),
    ("Rating Rationale Chembond Material Technologies Limited Issuer not cooperating; Rating continues", "not cooperating",
     ["not cooperating"]),
])
def test_actions_the_letter_states(text, head, acts):
    assert R.classify(text) == (head, acts)


@pytest.mark.parametrize("text", [
    "CRISIL Ratings reserves the right to withdraw or revise the ratings assigned. The rating could be upgraded or downgraded.",
    "The rating is liable to be withdrawn.",
    "The bank's ratings would be lowered if India's sovereign rating is downgraded, and could be upgraded if the BCA is upgraded.",
    "Factors that could, individually or collectively, lead to negative rating action/downgrade: a fall in margins.",
    "Where the issuer does not cooperate, the rating symbol shall be accompanied by 'ISSUER NOT COOPERATING'.",
    "Status of non cooperation with previous CRA: Not applicable.",
])
def test_disclaimers_conditions_and_definitions_are_not_actions(text):
    assert R.classify(text) == (None, [])


def test_the_rating_history_table_is_not_read():
    text = "CRISIL AA/Stable (Reaffirmed). Annexure - Rating History for last 3 Years: 2024 Downgraded, 2023 Upgraded"
    assert R.classify(text) == ("reaffirmed", ["reaffirmed"])


def test_agency():
    assert R.agency_of("ICRA Limited ... [ICRA]AA+ ... ICRA reserves") == "ICRA"
    assert R.agency_of("Moody's Ratings affirms the Ba1 ratings") == "Moody's"
    assert R.agency_of("no agency named") is None


def test_a_file_with_no_text_is_stored_as_unread():
    pytest.importorskip("pdfminer")              # the reader's dependency
    assert R.read(b"not a pdf") == {"action": None, "actions": "", "agency": None, "read": False, "chars": 0}


def test_each_filing_stored_once(tmp_path):
    row = {"an_dt": pd.Timestamp("2026-10-22 18:57:29"), "symbol": "RAMCOSYS", "url": "u/1.pdf", "action": "upgrade",
           "actions": "upgrade", "agency": "ICRA", "read": True, "chars": 2400, "observed_dt": datetime(2026, 10, 25, 23)}
    R.write(pd.DataFrame([row], columns=R.COLUMNS), tmp_path)
    R.write(pd.DataFrame([{**row, "action": "reaffirmed", "observed_dt": datetime(2026, 10, 26)}], columns=R.COLUMNS), tmp_path)
    d = R.stored(tmp_path)
    assert len(d) == 1 and d["action"].iloc[0] == "upgrade"          # the first reading stays


def test_update_reads_each_filing_once_and_retries_failures(tmp_path):
    pytest.importorskip("pdfminer")
    class Resp:
        def __init__(self, ok):
            self.ok, self.content = ok, b"not a pdf"

        def raise_for_status(self):
            if not self.ok:
                raise RuntimeError("503")

    class Session:
        def __init__(self):
            self.asked = []

        def get(self, url, timeout=None):
            self.asked.append(url)
            return Resp(url != "u/down.pdf")

    todo = pd.DataFrame({"an_dt": pd.to_datetime(["2026-10-20 10:00", "2026-10-21 10:00"]), "symbol": ["A", "B"],
                         "url": ["u/ok.pdf", "u/down.pdf"]})
    s = Session()
    assert R.update(todo, s, out_dir=tmp_path, pause=0, now=datetime(2026, 10, 25, 23)) == 1
    d = R.stored(tmp_path)
    assert d["url"].tolist() == ["u/ok.pdf"] and not d["read"].iloc[0]             # stored, as not readable
    s.asked.clear()
    R.update(todo, s, out_dir=tmp_path, pause=0)
    assert s.asked == ["u/down.pdf"]                                                # only the failed one again
