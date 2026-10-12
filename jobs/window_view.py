"""v1 over v3's window (v3 spec section 14.2, "Window view").

    python -m jobs.window_view --replay data/backtest/v1_replay_v3 \\
        --frozen data/frozen/v3 --out data/backtest/v1_window_2020

Writes data/backtest/v1_window_2020/ from the v1 replay folder, which is
jobs/backtest_v1.py run unchanged on the frozen inputs from 15 February 2019 to
the open of 18 September 2026. The window starts at the open of 17 February
2020, v3's first review:

  nav.csv         starts at a new first mark, (17 Feb 2020, open). v1's value
                  there is its `strategy_open` and `universe_ew_open` in
                  rebalance_open.csv (the book at that open's prices, before the
                  day's trades); each index's value is engine.tri_value_at_open
                  by the rule that index's own column uses at its two opens (the
                  factor-index form for the five factor indices), on the same
                  scale as the replay's nav.csv. Every mark after it is kept
                  exactly as written: the 17 Feb 2020 close and every mark
                  later. Nothing is rebased.
  trades.csv      only fills dated on or after 17 Feb 2020 (v1's own rebalance
                  of that day included).
  episodes.csv    only episodes whose entry is on or after 17 Feb 2020.
                  episodes_open_at_start.csv lists, beside it, the episodes
                  entered earlier and still open at that open (exit on or after
                  it); window.json reports their number.
  other files     filtered by date in the same way: holdings.csv,
                  rebalance_open.csv, universe_funnel.csv, data_quality.csv
                  and ranks.parquet by their decision or mark date, and
                  trade_for_trade.csv by the episode's entry date.

metrics.json and sanity.json describe the whole replay and are not copied.
window.json records what was read, what was kept and the first marks. The
measurement jobs then run on this folder unchanged; the monkey test is not
re-run for it (--monkeys 0), because it would need a replay that starts from
v1's 2020 book.

The index values at the start are computed with `end` set to the in-sample end,
not the replay's end: the level at an open depends only on the TRI close and
the price move before it, so the replay's end does not enter, and no return
after the in-sample end is read. As a check, the same call at the in-sample end
must reproduce in_sample_end_open.csv exactly.

No v3 return is computed here.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

START = "2020-02-17"
# file -> its date column; the window keeps rows with that column >= start
DATE_FILTERED = {"trades.csv": "date", "holdings.csv": "D", "rebalance_open.csv": "date",
                 "universe_funnel.csv": "D", "data_quality.csv": "D",
                 "in_sample_end_open.csv": "date", "trade_for_trade.csv": "entry"}
NAV_KEYS = ("date", "mark")


def _read(path: Path) -> pd.DataFrame:
    """Every field as the text the file holds, so what is kept is kept byte for byte."""
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def _write(df: pd.DataFrame, path: Path) -> None:
    df.to_csv(path, index=False, lineterminator="\n")


def window_files(replay: Path, out: Path, start: str, first: dict) -> dict:
    """Write the window view of `replay` into `out`; return what window.json records.

    `first` maps every nav.csv column other than date and mark to its value at
    the open of `start`."""
    replay, out = Path(replay), Path(out)
    out.mkdir(parents=True, exist_ok=True)
    rep: dict = {"replay": str(replay), "start": start, "files": {}}

    nav = _read(replay / "nav.csv")
    cols = [c for c in nav.columns if c not in NAV_KEYS]
    missing = [c for c in cols if c not in first]
    if missing:
        raise ValueError(f"no value at the window's first mark for nav.csv columns {missing}")
    if not ((nav["date"] == start) & (nav["mark"] == "close")).any():
        raise ValueError(f"nav.csv has no close on {start}: not a session of the replay")
    row = {"date": start, "mark": "open", **{c: repr(float(first[c])) for c in cols}}
    body = nav[(nav["date"] > start) | ((nav["date"] == start) & (nav["mark"] == "close"))]
    new = pd.concat([pd.DataFrame([row], columns=nav.columns), body], ignore_index=True)
    _write(new, out / "nav.csv")
    rep["files"]["nav.csv"] = {"rows_in": len(nav), "rows_out": len(new),
                               "first_mark": {"date": start, "mark": "open",
                                              **{c: float(first[c]) for c in cols}}}

    for name, col in DATE_FILTERED.items():
        if not (replay / name).exists():
            continue
        df = _read(replay / name)
        kept = df[df[col] >= start]
        _write(kept, out / name)
        rep["files"][name] = {"rows_in": len(df), "rows_out": len(kept), "filtered_on": col}

    ep = _read(replay / "episodes.csv")
    kept = ep[ep["entry"] >= start]
    earlier = ep[(ep["entry"] < start) & (ep["exit"] >= start)]
    _write(kept, out / "episodes.csv")
    _write(earlier, out / "episodes_open_at_start.csv")
    rep["files"]["episodes.csv"] = {"rows_in": len(ep), "rows_out": len(kept),
                                    "filtered_on": "entry"}
    rep["episodes_open_at_window_start"] = len(earlier)

    if (replay / "ranks.parquet").exists():
        rk = pd.read_parquet(replay / "ranks.parquet")
        kept = rk[pd.to_datetime(rk["D"]) >= pd.Timestamp(start)]
        kept.to_parquet(out / "ranks.parquet", index=False)
        rep["files"]["ranks.parquet"] = {"rows_in": len(rk), "rows_out": len(kept),
                                         "filtered_on": "D"}
    rep["not_copied"] = [n for n in ("metrics.json", "sanity.json") if (replay / n).exists()]
    return rep


def first_marks(replay: Path, frozen: Path, start: str) -> dict:
    """Every nav.csv column's value at the open of `start`, from the replay's
    rebalance_open.csv and engine.tri_value_at_open on the frozen inputs, plus
    the self-check at the in-sample end. Returns {"values": ..., "check": ...}."""
    from jobs import backtest_v1, freeze_v3
    from zen.universe import pit

    replay, frozen = Path(replay), Path(frozen)
    problems = freeze_v3.verify(frozen)
    if problems:
        raise SystemExit(f"{frozen} does not match its freeze.json: {problems[:5]}")
    m = json.loads((replay / "metrics.json").read_text(encoding="utf-8"))
    d0 = pd.Timestamp(m["decision_dates"][0])
    is_end = pd.Timestamp(m["in_sample_end"])
    d = pd.Timestamp(start)
    if not d0 < d <= is_end:
        raise ValueError(f"{start} is not between the replay's start and the in-sample end")

    ro = pd.read_csv(replay / "rebalance_open.csv", parse_dates=["date"]).set_index("date")
    if d not in ro.index:
        raise ValueError(f"rebalance_open.csv has no row for {start}: not a review of the replay")
    values = {"strategy": float(ro.at[d, "strategy_open"]),
              "universe_ew": float(ro.at[d, "universe_ew_open"])}

    paths = backtest_v1.input_paths(frozen)
    con = pit.connect(paths["db"])
    kw = dict(tri=paths["tri"], endpoints=paths["endpoints"])
    values.update(backtest_v1.index_opens(con, d0, d, is_end, is_end, False, **kw))

    # The same call at the in-sample end must give the replay's own figures.
    chk = backtest_v1.index_opens(con, d0, is_end, is_end, is_end, False, **kw)
    ise = pd.read_csv(replay / "in_sample_end_open.csv").iloc[0]
    worst = max(abs(chk[k] / float(ise[k]) - 1) for k in chk)
    if not worst <= 1e-12:
        raise AssertionError(f"index levels at the in-sample end differ from "
                             f"in_sample_end_open.csv by {worst:.3g} (relative)")
    return {"values": values, "check": {"in_sample_end": str(is_end.date()),
                                        "columns": len(chk), "max_rel_diff": float(worst)}}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--replay", default=str(ROOT / "data" / "backtest" / "v1_replay_v3"))
    ap.add_argument("--frozen", default=str(ROOT / "data" / "frozen" / "v3"))
    ap.add_argument("--out", default=str(ROOT / "data" / "backtest" / "v1_window_2020"))
    ap.add_argument("--start", default=START)
    a = ap.parse_args(argv)

    fm = first_marks(Path(a.replay), Path(a.frozen), a.start)
    rep = window_files(Path(a.replay), Path(a.out), a.start, fm["values"])
    rep["index_check_at_in_sample_end"] = fm["check"]
    rep["frozen"] = a.frozen
    (Path(a.out) / "window.json").write_text(json.dumps(rep, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in rep.items() if k != "files"}, indent=2))
    for n, f in rep["files"].items():
        print(f"{n:28s} {f['rows_in']:>7d} -> {f['rows_out']:>7d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
