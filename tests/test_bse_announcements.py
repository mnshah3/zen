"""BSE announcements: parsing, BSE's labels in zen's categories, the completeness rule and storage.
No network: BSE's responses are built here in BSE's own shape."""

from __future__ import annotations

from datetime import date, datetime

import duckdb
import pandas as pd
import pytest

from zen.data import bse_announcements as bse


def _row(i: int, sub: str = "General", cat: str = "Company Update", when: str = "2023-06-05T10:01:02.5",
         subject: str = "Acme Ltd - 500001 - Announcement under Regulation 30 (LODR)-General") -> dict:
    return {"NEWSID": f"id-{i}", "SCRIP_CD": 500001 + i % 3, "DissemDT": when, "NEWS_DT": when,
            "CATEGORYNAME": cat, "SUBCATNAME": sub, "NEWSSUB": subject, "HEADLINE": f"headline {i}",
            "SLONGNAME": "Acme Ltd", "ATTACHMENTNAME": f"{i}.pdf"}


class FakeSession:
    """Serves pages of BSE's day response; `report` is the count BSE claims for the day."""

    def __init__(self, rows: list[dict], report: int | None = None):
        self.rows, self.report, self.calls = rows, len(rows) if report is None else report, 0

    def get(self, url, timeout=None, params=None):
        self.calls += 1
        page = int(params["pageno"]) if params else int(url.split("pageno=")[1].split("&")[0])
        chunk = self.rows[(page - 1) * bse.PAGE: page * bse.PAGE]

        class R:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(_self):
                return {"Table": chunk, "Table1": [{"ROWCNT": self.report}]}
        return R()


def test_orders_and_the_rest_map_to_zen_categories():
    assert bse.categorise("Company Update", "Award of Order / Receipt of Order") == "orders"
    assert bse.categorise("Company Update", "Bagging/Receiving of orders/contracts (Sub-para 4-Para B)") == "orders"
    assert bse.categorise("Company Update", "Pendency of any litigation(s) or dispute(s) (Sub-para 8-Para B)") == "regulatory"
    assert bse.categorise("Company Update", "List of Creditors ") == "regulatory"          # BSE's trailing space
    assert bse.categorise("Company Update", "Updates - Corporate Insolvency Resolution Process  (CIRP)") == "regulatory"
    assert bse.categorise("Corp. Action", "Dividend") == "corp_action"
    assert bse.categorise(" Corp Action", "Daily Buy Back of equity shares") == "corp_action"
    assert bse.categorise("Result", "Financial Results") == "results"
    assert bse.categorise("Company Update", "General") == "other"
    assert bse.categorise("Insider Trading / SAST", "Disclosures under Reg. 29(2) of SEBI (SAST) Regulations, 2011") == "routine"
    assert bse.categorise("Others", "Reg. 57 (1) - Certificate of interest payment/Principal in case of NCD") == "routine"
    assert bse.categorise("Others", "Reg. 55 - Credit Rating") == "ratings"
    assert bse.categorise("Company Update", "A label BSE adds next year") == "other"
    assert bse.categorise("Company Update", None) == "other"


def test_an_analyst_meet_is_guidance_only_when_it_is_the_outcome():
    sub = "Analyst / Investor Meet"
    assert bse.categorise("Company Update", sub, "X Ltd - 1 - Announcement under Regulation 30 (LODR)-Analyst / Investor Meet - Outcome") == "guidance"
    assert bse.categorise("Company Update", sub, "X Ltd - 1 - Announcement under Regulation 30 (LODR)-Analyst / Investor Meet - Intimation") == "routine"


def test_every_mapped_label_has_one_category():
    seen = {}
    for cat, names in bse._RAW.items():
        for n in names:
            assert bse._norm(n) not in seen, (n, cat, seen.get(bse._norm(n)))
            seen[bse._norm(n)] = cat
    assert not set(seen) & {bse._norm(v) for v in bse.VAGUE}


def test_parse_uses_dissemination_time_and_the_1530_rule():
    df = bse.parse_rows([_row(1, "Award of Order / Receipt of Order", when="2023-06-05T15:30:00.0"),
                         _row(2, when="2023-06-09T16:10:00"), _row(3, when="")])
    assert list(df["news_id"]) == ["id-1", "id-2"]                       # no time, not kept
    assert df.iloc[0]["dissem_dt"] == datetime(2023, 6, 5, 15, 30)
    assert df.iloc[0]["trade_date"] == date(2023, 6, 6)                  # filed at the close
    assert df.iloc[1]["trade_date"] == date(2023, 6, 12)                 # Friday evening to Monday
    assert df.iloc[0]["zen_category"] == "orders"


def test_a_day_counts_only_when_every_page_adds_up():
    rows = [_row(i) for i in range(120)]                                 # three pages
    s = FakeSession(rows)
    df = bse.fetch_day(date(2023, 6, 5), s, pause=0)
    assert len(df) == 120 and s.calls == 3
    assert bse.fetch_day(date(2023, 6, 5), FakeSession(rows, report=121), pause=0) is None
    assert len(bse.fetch_day(date(2023, 6, 4), FakeSession([]), pause=0)) == 0     # a day with nothing filed


def test_storage_merges_by_news_id_and_rebuilds(tmp_path):
    a = bse.parse_rows([_row(i) for i in range(3)])
    b = bse.parse_rows([_row(2, "Award of Order / Receipt of Order"), _row(7, when="2023-07-01T09:00:00")])
    scrips = pd.DataFrame({"scrip_cd": [500001, 500002], "isin": ["INE000A01011", "INE000B01011"]})
    bse.write(bse.with_isin(a, scrips), tmp_path)
    paths = bse.write(bse.with_isin(b, scrips), tmp_path)
    assert {p.name for p in paths} == {"2023-06.parquet", "2023-07.parquet"}
    june = pd.read_parquet(tmp_path / "2023" / "2023-06.parquet")
    assert len(june) == 3 and june.set_index("news_id").loc["id-2", "zen_category"] == "orders"
    assert june.set_index("news_id").loc["id-0", "isin"] == "INE000A01011"
    assert pd.isna(june.set_index("news_id").loc["id-2", "isin"])        # scrip 500003 is not in the list
    con = duckdb.connect()
    assert bse.rebuild(con, tmp_path) == 4


def test_days_file_round_trip(tmp_path):
    f = tmp_path / "days.csv"
    bse.mark_done({date(2023, 6, 5): 597}, f)
    bse.mark_done({date(2019, 6, 10): 441, date(2023, 6, 5): 598}, f)
    assert bse.done_days(f) == {date(2019, 6, 10): 441, date(2023, 6, 5): 598}


def test_unmapped_reports_new_labels_not_vague_ones():
    df = bse.parse_rows([_row(1, "General"), _row(2, "A label BSE adds next year"), _row(3, "Dividend", "Corp. Action")])
    out = bse.unmapped(df)
    assert list(out.index) == ["Company Update | A label BSE adds next year"]


@pytest.mark.parametrize("bad", [None, "", "not a date"])
def test_bad_times_are_skipped(bad):
    r = _row(1)
    r["DissemDT"], r["NEWS_DT"] = bad, bad
    assert bse.parse_rows([r]).empty


def test_every_material_label_maps_to_its_category():
    want = {"orders", "expansion", "contraction", "regulatory", "licenses"}
    got = {bse.categorise(cat, sub) for cat, sub in bse.GAP_LABELS}
    assert got == want, got
    other = {bse.categorise(cat, sub) for cat, sub in bse.OTHER_MATERIAL_LABELS}
    assert other == {"mna", "capital", "ratings"}, other
    assert len(set(bse.MATERIAL_LABELS)) == len(bse.MATERIAL_LABELS) == 80
    # BSE's own spacing is kept: its filter matches the exact string ("(CIRP)" after two spaces)
    assert ("Company Update", "Updates - Corporate Insolvency Resolution Process  (CIRP)") in bse.GAP_LABELS


def test_a_label_month_counts_only_when_it_adds_up(tmp_path):
    rows = [_row(i, "Award of Order / Receipt of Order") for i in range(60)]
    assert len(bse.fetch_label("Company Update", "Award of Order / Receipt of Order", date(2023, 6, 1),
                               date(2023, 6, 30), FakeSession(rows), pause=0)) == 60
    assert bse.fetch_label("Company Update", "x", date(2023, 6, 1), date(2023, 6, 30),
                           FakeSession(rows, report=61), pause=0) is None
    f = tmp_path / "ranges.csv"
    bse.mark_ranges([("2023-06", "Company Update", "Strike", 1)], f)
    bse.mark_ranges([("2023-05", "Company Update", "Strike", 0)], f)
    assert bse.done_ranges(f) == {("2023-06", "Strike"), ("2023-05", "Strike")}
