"""v3's data prerequisites (spec 15.2.4 to 15.2.6): the freeze, v1's frozen-root
override and the window view of the v1 replay.

None of these touches the live archive, except the last group, which checks the
files the build wrote and is skipped where they are absent.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pandas as pd
import pytest

from jobs import backtest_v1, freeze_v3, v1_reproduction, window_view
from zen.portfolio import engine
from zen.universe import pit

REPO = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- the freeze
def _fake_repo(root: Path) -> None:
    for rel in freeze_v3.FIXED_INPUTS:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(rel.encode() * 3)
    fin = root / "data" / "financials"
    for name in ("2025-03", "legacy_2024Q1", "legacy_index", "legacy_missing"):
        (fin / f"{name}.parquet").parent.mkdir(parents=True, exist_ok=True)
        (fin / f"{name}.parquet").write_bytes(name.encode())


def _sealed(tmp_path: Path):
    root, dest = tmp_path / "repo", tmp_path / "frozen"
    _fake_repo(root)
    qf = tmp_path / "qf.parquet"
    qf.write_bytes(b"quality factor")
    freeze_v3.copy_inputs(dest, root)
    freeze_v3.seal(dest, qf)
    return root, dest


def test_freeze_lists_exactly_the_inputs_and_not_the_ledgers(tmp_path):
    _, dest = _sealed(tmp_path)
    listed = freeze_v3.load_freeze(dest)
    assert set(listed) == {*freeze_v3.FIXED_INPUTS, freeze_v3.QUALITY_FACTOR,
                           "data/financials/2025-03.parquet",
                           "data/financials/legacy_2024Q1.parquet"}      # not legacy_index / _missing
    assert all(len(h) == 64 for h in listed.values())
    assert freeze_v3.verify(dest) == []


def test_raw_adds_the_committed_files_and_changes_no_listed_hash(tmp_path):
    """The checker's raw files come from git at one commit, not from the working copy."""
    import subprocess
    root, dest = _sealed(tmp_path)
    before = freeze_v3.load_freeze(dest)

    def git(*args):
        subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t",
                        "-c", "core.autocrlf=false", *args], check=True, capture_output=True)
    raw = ["data/daily/2020/2020-02.parquet", "data/daily_other/2020/2020-02.parquet",
           "data/corpactions/2020.parquet", "data/announcements/2020/2020-02.parquet",
           "data/indices/2020.parquet"]
    for rel in raw:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(b"committed " + rel.encode())
    git("init", "-q")
    git("add", *freeze_v3.RAW_DIRS)
    git("commit", "-q", "-m", "raw")
    (root / "data/daily/2020/2020-02.parquet").write_bytes(b"edited after the commit")

    added = freeze_v3.add_raw(dest, "HEAD", root)
    assert set(added) == set(raw)
    assert (dest / "data/daily/2020/2020-02.parquet").read_bytes() == \
        b"committed data/daily/2020/2020-02.parquet"                    # the commit's, not the edit
    after = freeze_v3.load_freeze(dest)
    assert {k: after[k] for k in before} == before and set(after) == set(before) | set(added)
    assert freeze_v3.verify(dest) == []
    assert freeze_v3.add_raw(dest, "HEAD", root) == added               # repeatable

    (root / "data/indices/2020.parquet").write_bytes(b"another version")
    git("commit", "-q", "-am", "changed")
    with pytest.raises(RuntimeError, match="already listed with another hash"):
        freeze_v3.add_raw(dest, "HEAD", root)
    assert freeze_v3.load_freeze(dest) == after                          # nothing rewritten


def test_verify_finds_a_changed_missing_and_unlisted_file(tmp_path):
    _, dest = _sealed(tmp_path)
    (dest / "nse_list.json").write_bytes(b"edited")
    (dest / "data" / "reference" / "industry_nse.parquet").unlink()
    (dest / "data" / "extra.csv").write_bytes(b"x")
    problems = freeze_v3.verify(dest)
    assert "changed: nse_list.json" in problems
    assert "missing: data/reference/industry_nse.parquet" in problems
    assert "not listed: data/extra.csv" in problems


def test_a_sealed_freeze_is_not_overwritten_and_a_missing_input_stops_the_copy(tmp_path):
    root, dest = _sealed(tmp_path)
    with pytest.raises(RuntimeError, match="sealed"):
        freeze_v3.copy_inputs(dest, root)
    with pytest.raises(RuntimeError, match="sealed"):
        freeze_v3.seal(dest, tmp_path / "qf.parquet")
    (root / "nse_list.json").unlink()
    with pytest.raises(FileNotFoundError):
        freeze_v3.copy_inputs(tmp_path / "other", root)


def test_a_copy_that_differs_from_its_source_is_refused(tmp_path, monkeypatch):
    src, dst = tmp_path / "a.bin", tmp_path / "out" / "a.bin"
    src.write_bytes(b"abcdef")

    def bad_copy(s, d):
        Path(d).write_bytes(b"abcdeX")
    monkeypatch.setattr(freeze_v3.shutil, "copyfile", bad_copy)
    with pytest.raises(RuntimeError, match="changed while it was copied or the copy differs"):
        freeze_v3._copy_verified(src, dst)


def test_seal_needs_the_rebuilt_quality_factor(tmp_path):
    root, dest = tmp_path / "repo", tmp_path / "frozen"
    _fake_repo(root)
    freeze_v3.copy_inputs(dest, root)
    with pytest.raises(FileNotFoundError):
        freeze_v3.seal(dest, None)
    assert not (dest / freeze_v3.FREEZE_JSON).exists()


# ---------------------------------------------------------------- v1 reads the frozen root
def _frozen_tree(root: Path) -> dict:
    paths = {"db": root / "data" / "zen.duckdb", "nse_list": root / "nse_list.json",
             "tri": root / "data" / "external" / "nifty_tri.parquet",
             "endpoints": root / "data" / "external" / "nifty_price_endpoints.csv"}
    for p in paths.values():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    return paths


def test_input_paths_default_is_the_repository():
    p = backtest_v1.input_paths()
    assert p == {"db": pit.DB_PATH, "nse_list": pit.NSE_LIST, "tri": engine.TRI_PATH,
                 "endpoints": engine.ENDPOINTS_PATH}


def test_input_paths_frozen_are_under_the_root_and_never_fall_back(tmp_path):
    want = _frozen_tree(tmp_path)
    assert backtest_v1.input_paths(tmp_path) == want
    want["endpoints"].unlink()
    with pytest.raises(FileNotFoundError, match="endpoints"):
        backtest_v1.input_paths(tmp_path)


def test_main_with_frozen_stops_before_any_live_read_when_the_freeze_is_unsound(tmp_path):
    _frozen_tree(tmp_path)                                    # files present, no freeze.json
    with pytest.raises(SystemExit, match="freeze.json"):
        backtest_v1.main(["--frozen", str(tmp_path), "--out", str(tmp_path / "out"),
                          "--no-record"])
    with pytest.raises(FileNotFoundError):                    # nothing there at all
        backtest_v1.main(["--frozen", str(tmp_path / "nowhere"), "--out", str(tmp_path / "o2"),
                          "--no-record"])


def test_index_functions_pass_the_frozen_paths_through(monkeypatch):
    seen = []
    monkeypatch.setattr(engine, "tri_nav", lambda *a, path=None, **k: seen.append(("tri", path)) or "x")
    monkeypatch.setattr(engine, "factor_tri_nav",
                        lambda *a, path=None, endpoints_path=None, **k:
                        seen.append(("factor", path, endpoints_path)) or "x")
    monkeypatch.setattr(engine, "tri_value_at_open",
                        lambda *a, path=None, endpoints_path=None, **k:
                        seen.append(("open", path, endpoints_path)) or 1.0)
    t, e = Path("F/tri.parquet"), Path("F/endpoints.csv")
    backtest_v1.index_navs(None, "s", "e", "i", False, tri=t, endpoints=e)
    backtest_v1.index_opens(None, "s", "d", "e", "i", False, tri=t, endpoints=e)
    assert seen and all(s[1] == t for s in seen)
    assert {s[0] for s in seen} == {"tri", "factor", "open"}
    # the two factor and open calls for the factor indices carry the endpoints file
    assert sum(1 for s in seen if len(s) == 3 and s[2] == e) >= 2 * len(backtest_v1.FACTOR_INDICES)


def test_static_labels_read_the_named_nse_list(monkeypatch, tmp_path):
    got = []
    monkeypatch.setattr(pit.Identity, "build", classmethod(lambda cls, con: pit.Identity.trivial()))
    monkeypatch.setattr(pit, "industry_labels", lambda *a, **k: pd.Series(dtype=object))
    monkeypatch.setattr(pit, "company_names", lambda *a, **k: pd.Series(dtype=object))
    monkeypatch.setattr(pit, "taxonomy_backfill", lambda *a, **k: set())
    monkeypatch.setattr(pit, "nse_list_lenders", lambda path=None: got.append(path) or set())
    named = tmp_path / "nse_list.json"
    pit.StaticLabels.load(None, named)
    pit.StaticLabels.load(None)
    assert got == [named, pit.NSE_LIST]


# ---------------------------------------------------------------- the window view
def _replay(tmp_path: Path) -> Path:
    r = tmp_path / "replay"
    r.mkdir()
    pd.DataFrame({"date": ["2019-02-15", "2020-02-14", "2020-02-17", "2020-02-18", "2026-09-18"],
                  "mark": ["open", "close", "close", "close", "open"],
                  "strategy": [500000.0, 600000.5, 610000.25, 620000.0, 900000.0],
                  "universe_ew": [500000.0, 500500.0, 501000.0, 502000.0, 700000.0],
                  "nifty500": [1.0, 1.1, 1.1000000000000001, 1.2, 2.5]}).to_csv(r / "nav.csv", index=False)
    pd.DataFrame({"date": ["2019-02-15", "2020-02-17", "2020-06-01"],
                  "strategy_open": [500000.0, 611481.700983, 650000.0],
                  "universe_ew_open": [500000.0, 509007.829088, 520000.0]}
                 ).to_csv(r / "rebalance_open.csv", index=False)
    pd.DataFrame({"date": ["2019-02-15", "2020-02-14", "2020-02-17", "2020-03-02"],
                  "symbol": list("ABCD"), "side": ["buy"] * 4, "value": [1.0, 2.0, 3.0, 4.0]}
                 ).to_csv(r / "trades.csv", index=False)
    pd.DataFrame({"symbol": list("ABCDE"),
                  "entry": ["2019-02-15", "2019-08-16", "2019-11-15", "2020-02-17", "2020-06-01"],
                  "exit": ["2020-02-14", "2020-02-17", "2020-06-01", "2020-06-01", "2026-09-18"],
                  "exit_reason": ["rebalance"] * 4 + ["open_at_end"]}).to_csv(r / "episodes.csv", index=False)
    pd.DataFrame({"D": ["2019-11-15", "2020-02-17", "2020-06-01"], "symbol": list("XYZ"),
                  "action": ["buy"] * 3}).to_csv(r / "holdings.csv", index=False)
    pd.DataFrame({"portfolio": ["strategy"] * 3, "symbol": list("PQR"),
                  "entry": ["2019-06-03", "2020-02-17", "2021-01-01"]}).to_csv(r / "trade_for_trade.csv", index=False)
    pd.DataFrame({"D": pd.to_datetime(["2019-11-15", "2020-02-17", "2020-06-01"]), "symbol": list("XYZ"),
                  "composite": [0.1, 0.2, 0.3]}).to_parquet(r / "ranks.parquet", index=False)
    (r / "metrics.json").write_text("{}")
    return r


FIRST = {"strategy": 611481.700983, "universe_ew": 509007.829088, "nifty500": 1.125}


def test_window_view_nav_has_a_new_first_mark_then_every_later_mark_unchanged(tmp_path):
    r, out = _replay(tmp_path), tmp_path / "w"
    window_view.window_files(r, out, "2020-02-17", FIRST)
    nav = pd.read_csv(out / "nav.csv", dtype=str)
    assert nav.loc[0].tolist() == ["2020-02-17", "open", "611481.700983", "509007.829088", "1.125"]
    src = pd.read_csv(r / "nav.csv", dtype=str)
    # the 17 Feb close and everything later, text for text; the earlier marks are gone
    pd.testing.assert_frame_equal(nav.iloc[1:].reset_index(drop=True),
                                  src[src["date"] >= "2020-02-17"].reset_index(drop=True))
    assert nav["mark"].tolist() == ["open", "close", "close", "open"]
    assert "2020-02-14" not in set(nav["date"])


def test_window_view_filters_every_file_by_its_date(tmp_path):
    r, out = _replay(tmp_path), tmp_path / "w"
    rep = window_view.window_files(r, out, "2020-02-17", FIRST)
    assert pd.read_csv(out / "trades.csv")["symbol"].tolist() == ["C", "D"]      # the day itself kept
    assert pd.read_csv(out / "holdings.csv")["D"].tolist() == ["2020-02-17", "2020-06-01"]
    assert pd.read_csv(out / "rebalance_open.csv")["date"].tolist() == ["2020-02-17", "2020-06-01"]
    assert pd.read_csv(out / "trade_for_trade.csv")["symbol"].tolist() == ["Q", "R"]
    assert pd.read_parquet(out / "ranks.parquet")["symbol"].tolist() == ["Y", "Z"]
    assert rep["files"]["trades.csv"] == {"rows_in": 4, "rows_out": 2, "filtered_on": "date"}


def test_window_view_episodes_by_entry_and_the_ones_already_open_are_reported(tmp_path):
    r, out = _replay(tmp_path), tmp_path / "w"
    rep = window_view.window_files(r, out, "2020-02-17", FIRST)
    assert pd.read_csv(out / "episodes.csv")["symbol"].tolist() == ["D", "E"]   # entry on or after
    # B (exit on the start) and C (exit later) were entered earlier and open at that open; A exited before
    assert pd.read_csv(out / "episodes_open_at_start.csv")["symbol"].tolist() == ["B", "C"]
    assert rep["episodes_open_at_window_start"] == 2
    assert not (out / "metrics.json").exists() and rep["not_copied"] == ["metrics.json"]


def test_window_view_refuses_a_missing_column_value_or_a_non_session(tmp_path):
    r = _replay(tmp_path)
    with pytest.raises(ValueError, match="no value at the window's first mark"):
        window_view.window_files(r, tmp_path / "w", "2020-02-17", {"strategy": 1.0})
    with pytest.raises(ValueError, match="not a session"):
        window_view.window_files(r, tmp_path / "w", "2020-02-16", FIRST)


# ---------------------------------------------------------------- the reproduction check
def test_reproduction_reports_identical_runs_and_the_first_difference():
    nav = pd.DataFrame({"date": ["d1", "d1", "d2"], "mark": ["open", "close", "close"],
                        "strategy": [100.0, 101.0, 102.0], "universe_ew": [100.0, 99.0, 98.0]})
    same = v1_reproduction.compare_nav(nav, nav.copy())
    assert same["identical"] and same["max_rel_diff_all"] == 0.0 and same["first_difference"] is None
    other = nav.copy()
    other.loc[2, "strategy"] = 102.0 * (1 + 1e-6)
    r = v1_reproduction.compare_nav(other, nav)
    assert not r["identical"]
    assert r["first_difference"] == {"date": "d2", "mark": "close", "marks_differing": 1}
    assert 0.9e-6 < r["max_rel_diff_strategy_and_universe"] < 1.1e-6
    assert v1_reproduction.compare_nav(nav.iloc[:2], nav)["identical"] is False      # a mark missing


def test_reproduction_compares_held_sets_and_fills():
    h = pd.DataFrame({"D": ["d1", "d1", "d1"], "symbol": list("ABC"), "action": ["buy", "keep", "sell"]})
    h2 = pd.DataFrame({"D": ["d1", "d1"], "symbol": list("AD"), "action": ["buy", "buy"]})
    assert v1_reproduction.compare_holdings(h, h.copy()) == []
    assert v1_reproduction.compare_holdings(h, h2) == [
        {"D": "d1", "replay_only": ["B"], "final_only": ["D"]}]
    t = pd.DataFrame({"date": ["d1", "d1"], "symbol": list("AB"), "side": ["buy", "buy"], "value": [1.0, 2.0]})
    u = pd.DataFrame({"date": ["d1", "d1"], "symbol": list("AC"), "side": ["buy", "buy"], "value": [1.5, 2.0]})
    assert v1_reproduction.compare_trades(t, u) == {"replay": 2, "final": 2, "only_in_replay": 1,
                                                    "only_in_final": 1, "common_with_a_different_value": 1}


# ---------------------------------------------------------------- what the build wrote
FROZEN = REPO / "data" / "frozen" / "v3"
REPLAY = REPO / "data" / "backtest" / "v1_replay_v3"
WINDOW = REPO / "data" / "backtest" / "v1_window_2020"


@pytest.mark.skipif(not (FROZEN / "freeze.json").exists(), reason="v3 inputs not frozen here")
def test_the_frozen_directory_matches_its_hash_list():
    assert freeze_v3.verify(FROZEN) == []


@pytest.mark.skipif(not (WINDOW / "window.json").exists(), reason="window view not built here")
def test_the_written_window_view_is_the_replay_from_the_start_on():
    start = "2020-02-17"
    nav, src = pd.read_csv(WINDOW / "nav.csv", dtype=str), pd.read_csv(REPLAY / "nav.csv", dtype=str)
    assert nav.loc[0, "date"] == start and nav.loc[0, "mark"] == "open"
    pd.testing.assert_frame_equal(nav.iloc[1:].reset_index(drop=True),
                                  src[src["date"] >= start].reset_index(drop=True))
    ro = pd.read_csv(REPLAY / "rebalance_open.csv", parse_dates=["date"]).set_index("date")
    assert float(nav.loc[0, "strategy"]) == ro.at[pd.Timestamp(start), "strategy_open"]
    assert float(nav.loc[0, "universe_ew"]) == ro.at[pd.Timestamp(start), "universe_ew_open"]
    assert (pd.read_csv(WINDOW / "trades.csv")["date"] >= start).all()
    assert (pd.read_csv(WINDOW / "episodes.csv")["entry"] >= start).all()
    rep = json.loads((WINDOW / "window.json").read_text())
    assert rep["index_check_at_in_sample_end"]["max_rel_diff"] <= 1e-12
