"""jobs.build_nifty_tri refuses bad or inconsistent index exports."""

from __future__ import annotations

import pandas as pd
import pytest

from jobs import build_nifty_tri as b

HDR3 = '"IndexName","Date","Total Returns Index"\n'
HDR4 = '"IndexName","Date","Total Returns Index","Net Total Return Index"\n'
DAYS = ["20 Sep 2026", "21 Sep 2026", "22 Sep 2026", "23 Sep 2026"]


def csv(name, rows, net=False):
    out = HDR4 if net else HDR3
    for r in rows:
        out += ",".join(f'"{x}"' for x in (name,) + tuple(r)) + "\n"
    return out


@pytest.fixture
def env(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    monkeypatch.setattr(b, "SRC", src)
    monkeypatch.setattr(b, "OUT", tmp_path / "tri.parquet")
    (src / "n500.csv").write_text(
        csv("NIFTY 500", [(d, f"{100 + i}.00", f"{90 + i}.00") for i, d in enumerate(DAYS)], net=True))
    return src


def run(env):
    t, errors, notes = b.build()
    return t, errors


def test_clean_build(env):
    (env / "f.csv").write_text(csv("NIFTY ALPHA 50", [(d, f"{50 + i}.5") for i, d in enumerate(DAYS)]))
    t, errors = run(env)
    assert errors == []
    assert set(t.index_name) == {"NIFTY 500", "NIFTY ALPHA 50"}
    assert t.loc[t.index_name == "NIFTY ALPHA 50", "net_tri"].isna().all()
    assert t.loc[t.index_name == "NIFTY 500", "net_tri"].notna().all()


def test_both_net_header_spellings_and_dash(env):
    (env / "n500.csv").write_text(HDR4.replace("Return Index", "Returns Index")
                                  + "".join(f'"NIFTY 500","{d}","{100 + i}.00","-"\n'
                                            for i, d in enumerate(DAYS)))
    t, errors = run(env)
    assert errors == [] and t["net_tri"].isna().all()


def test_duplicate_session_that_disagrees_is_refused(env):
    (env / "a.csv").write_text(csv("NIFTY ALPHA 50", [(d, "50.0") for d in DAYS]))
    (env / "b.csv").write_text(csv("NIFTY ALPHA 50", [(DAYS[1], "50.1")]))
    _, errors = run(env)
    assert any("files disagree" in e for e in errors)


def test_identical_duplicate_is_fine(env):
    (env / "a.csv").write_text(csv("NIFTY ALPHA 50", [(d, "50.0") for d in DAYS]))
    (env / "b.csv").write_text(csv("NIFTY ALPHA 50", [(DAYS[1], "50.0")]))
    _, errors = run(env)
    assert errors == []


def test_missing_calendar_session_is_refused(env):
    rows = [(d, "50.0") for d in DAYS if d != "22 Sep 2026"]
    (env / "a.csv").write_text(csv("NIFTY ALPHA 50", rows))
    _, errors = run(env)
    assert any("missing 1 NIFTY 500 sessions" in e for e in errors)


def test_extra_session_is_refused(env):
    rows = [(d, "50.0") for d in DAYS] + [("19 Sep 2026", "50.0")]   # a Saturday
    (env / "a.csv").write_text(csv("NIFTY ALPHA 50", rows))
    _, errors = run(env)
    assert any("does not have" in e for e in errors)


def test_non_positive_value_is_refused(env):
    (env / "a.csv").write_text(csv("NIFTY ALPHA 50", [(DAYS[0], "0")]))
    with pytest.raises(ValueError, match="non-positive"):
        run(env)


def test_unexpected_header_is_refused(env):
    (env / "a.csv").write_text('"IndexName","Date","Close"\n"X","20 Sep 2026","1"\n')
    with pytest.raises(ValueError, match="unexpected header"):
        run(env)


def test_changing_an_existing_row_is_refused(env):
    t, errors = run(env)
    assert errors == []
    t.to_parquet(b.OUT, index=False)
    (env / "n500.csv").write_text(
        csv("NIFTY 500", [(d, f"{100 + i}.01", f"{90 + i}.00") for i, d in enumerate(DAYS)], net=True))
    _, errors = run(env)
    assert any("differ from the current parquet" in e for e in errors)


def test_losing_an_existing_row_is_refused(env):
    t, _ = run(env)
    t.to_parquet(b.OUT, index=False)
    (env / "n500.csv").write_text(
        csv("NIFTY 500", [(d, f"{100 + i}.00", f"{90 + i}.00") for i, d in enumerate(DAYS[:3])], net=True))
    _, errors = run(env)
    assert any("are gone" in e for e in errors)


def test_live_parquet_passes_check():
    """The committed builder reproduces the parquet on disk (skipped on a fresh clone)."""
    if not b.OUT.exists() or not any(b.SRC.glob("*.csv")):
        pytest.skip("external index data not present")
    t, errors, _ = b.build()
    assert errors == []
    on_disk = pd.read_parquet(b.OUT)
    assert len(t) == len(on_disk)
