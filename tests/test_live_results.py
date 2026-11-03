"""jobs.live_wire --results-out: the figures of the results NSE broadcasts, read as they arrive."""
from datetime import datetime

import pandas as pd

from jobs import live_wire


def _archive(tmp_path):
    rows = [  # Reliance's September 2025 and June 2026 quarters as the archive holds them (rupees)
        {"symbol": "RELIANCE", "period_end": pd.Timestamp("2025-09-30"), "broadcast_dt": pd.Timestamp("2025-10-17 19:36:44"), "consolidated": True,
         "revenue": 2.58898e12, "excise_duty": 4.275e10, "ebitda": 4.5885e11, "pbt": 2.9e11, "profit_reported": 2.2092e11, "eps_basic": 13.42,
         "exceptional_items": 0.0, "quarter_span_days": None},
        {"symbol": "RELIANCE", "period_end": pd.Timestamp("2025-09-30"), "broadcast_dt": pd.Timestamp("2025-10-17 19:36:07"), "consolidated": False,
         "revenue": 1.3061e12, "excise_duty": 4.275e10, "ebitda": 1.4389e11, "pbt": 1.2e11, "profit_reported": 9.129e10, "eps_basic": 6.75,
         "exceptional_items": 0.0, "quarter_span_days": None},
    ]
    pd.DataFrame(rows).to_parquet(tmp_path / "2025-09.parquet", index=False)
    pd.DataFrame([{**rows[0], "period_end": pd.Timestamp("2026-06-30"), "broadcast_dt": pd.Timestamp("2026-07-17 19:50:03"),
                   "revenue": 3.1185e12, "excise_duty": None, "ebitda": 4.7517e11, "profit_reported": 2.3196e11, "eps_basic": 15.48}]
                 ).to_parquet(tmp_path / "2026-06.parquet", index=False)


def _listing(*urls):
    return pd.DataFrame([{"symbol": "RELIANCE", "company": "Reliance Industries Limited", "period_end": pd.Timestamp("2026-09-30"),
                          "broadcast_dt": pd.Timestamp("2026-10-30 21:32:55"), "consolidated": True, "audited": False, "xbrl_url": u} for u in urls])


FILED = {"revenue": 3.32861e12, "excise_duty": 4.0e10, "ebitda": 5.6616e11, "pbt": 4.0e11, "profit_reported": 2.955e11, "eps_basic": 18.88,
         "exceptional_items": 0.0, "quarter_span_days": 92}


def test_a_result_carries_its_figures_and_the_quarters_it_is_compared_with(tmp_path):
    _archive(tmp_path)
    out = live_wire.build_results(None, _listing("x1"), lambda m: FILED, datetime(2026, 10, 30, 21, 40), tmp_path)
    assert out["n"] == 1 and out["read"] == 1 and out["cols"] == live_wire.RESULT_COLS
    r = dict(zip(out["cols"], out["rows"][0]))
    assert r["s"] == "RELIANCE" and r["pe"] == "2026-09-30" and r["c"] is True and r["t"] == "2026-10-30 21:32:55"
    assert r["rev"] == 328861.0 and r["ebitda"] == 56616.0 and r["np"] == 29550.0 and r["eps"] == 18.88   # crore, net of excise
    assert r["pe_py"] == "2025-09-30" and r["rev_py"] == 254623.0 and r["np_py"] == 22092.0               # the consolidated year-ago quarter
    assert r["pe_pq"] == "2026-06-30" and r["rev_pq"] == 311850.0 and r["eps_pq"] == 15.48


def test_a_filing_read_once_is_not_read_again_and_old_ones_drop_out(tmp_path):
    _archive(tmp_path)
    first = live_wire.build_results(None, _listing("x1"), lambda m: FILED, datetime(2026, 10, 30, 21, 40), tmp_path)
    calls = []
    again = live_wire.build_results(first, _listing("x1"), lambda m: calls.append(m) or FILED, datetime(2026, 11, 2, 9, 0), tmp_path)
    assert calls == [] and again["n"] == 1 and again["read"] == 0
    later = live_wire.build_results(first, None, lambda m: FILED, datetime(2026, 11, 4, 0, 0), tmp_path)
    assert later["n"] == 0                                                   # past KEEP_DAYS


def test_a_revised_filing_replaces_the_first_and_a_half_year_is_not_a_quarter(tmp_path):
    _archive(tmp_path)
    lst = _listing("x1", "x2")
    lst.loc[1, "broadcast_dt"] = pd.Timestamp("2026-10-31 10:00:00")
    out = live_wire.build_results(None, lst, lambda m: {**FILED, "eps_basic": 18.9 if m["xbrl_url"] == "x2" else 18.88},
                                  datetime(2026, 10, 31, 11, 0), tmp_path)
    assert out["n"] == 1 and dict(zip(out["cols"], out["rows"][0]))["eps"] == 18.9
    half = live_wire.build_results(None, _listing("x3"), lambda m: {**FILED, "quarter_span_days": 183}, datetime(2026, 10, 31, 11, 0), tmp_path)
    assert half["n"] == 0


def test_a_failed_read_is_tried_again_next_round(tmp_path):
    _archive(tmp_path)
    out = live_wire.build_results(None, _listing("x1"), lambda m: None, datetime(2026, 10, 30, 21, 40), tmp_path)
    assert out["n"] == 0
    blank = {k: None for k in FILED}                                       # parsed, but no figure read
    out = live_wire.build_results(out, _listing("x1"), lambda m: blank, datetime(2026, 10, 30, 21, 42), tmp_path)
    assert out["n"] == 0
    out = live_wire.build_results(out, _listing("x1"), lambda m: FILED, datetime(2026, 10, 30, 21, 45), tmp_path)
    assert out["n"] == 1
