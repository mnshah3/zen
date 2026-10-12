"""Does the v1 replay reproduce v1_final? (v3 spec section 14.2, "Reproduction check".)

    python -m jobs.v1_reproduction [--replay data/backtest/v1_replay_v3] \\
                                   [--final data/backtest/v1_final]

Compares the replay's nav.csv with v1_final's at every common mark (a mark is a
date and open or close) and writes reproduction.json into the replay folder:

  max_rel_diff       the largest |replay / final - 1| over the common marks, per
                     column and over the strategy and universe columns together
                     (the number the verdict reports);
  identical          True when that is within 1e-9, in which case the replay is
                     v1_final and A2's bars use v1_final's figures; otherwise the
                     bars use the replay, with v1_final's window figures beside
                     them;
  first_difference   the first mark at which the strategy column differs by more
                     than 1e-9, and how many marks do;
  holdings           the review dates whose held set differs, with the symbols
                     on each side;
  trades             how many fills each has, and how many differ.

Reads two finished runs; computes no new return and records no trial.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
TOL = 1e-9
KEYS = ["date", "mark"]


def compare_nav(replay: pd.DataFrame, final: pd.DataFrame, tol: float = TOL) -> dict:
    cols = [c for c in replay.columns if c in final.columns and c not in KEYS]
    m = replay.merge(final, on=KEYS, suffixes=("_r", "_f"), how="inner")
    out = {"marks_replay": len(replay), "marks_final": len(final), "marks_common": len(m),
           "columns": cols, "max_rel_diff": {}}
    for c in cols:
        r, f = m[c + "_r"].astype(float), m[c + "_f"].astype(float)
        both = r.notna() & f.notna()
        rel = ((r[both] / f[both]) - 1.0).abs()
        out["max_rel_diff"][c] = float(rel.max()) if len(rel) else None
        if r.isna().ne(f.isna()).any():
            out["max_rel_diff"][c] = float("inf")           # one has a value where the other has none
    head = [c for c in ("strategy", "universe_ew") if c in cols]
    vals = [out["max_rel_diff"][c] for c in head] or [None]
    out["max_rel_diff_strategy_and_universe"] = max(v for v in vals if v is not None)
    out["max_rel_diff_all"] = max(v for v in out["max_rel_diff"].values() if v is not None)
    out["identical"] = bool(out["max_rel_diff_all"] <= tol and len(m) == len(replay) == len(final))
    if "strategy" in cols:
        rel = ((m["strategy_r"] / m["strategy_f"]) - 1.0).abs()
        bad = m[rel > tol]
        out["first_difference"] = ({"date": str(bad["date"].iloc[0]), "mark": str(bad["mark"].iloc[0]),
                                    "marks_differing": int(len(bad))} if len(bad) else None)
    return out


def compare_holdings(replay: pd.DataFrame, final: pd.DataFrame) -> list[dict]:
    """Review dates (in both runs) whose held set (action keep or buy) differs."""
    def held(h):
        h = h[h["action"] != "sell"]
        return {d: set(g["symbol"]) for d, g in h.groupby("D")}
    a, b = held(replay), held(final)
    out = []
    for d in sorted(set(a) & set(b)):
        if a[d] != b[d]:
            out.append({"D": str(d), "replay_only": sorted(a[d] - b[d]),
                        "final_only": sorted(b[d] - a[d])})
    return out


def compare_trades(replay: pd.DataFrame, final: pd.DataFrame) -> dict:
    key = ["date", "symbol", "side"]
    m = replay.merge(final, on=key, how="outer", suffixes=("_r", "_f"), indicator=True)
    both = m[m["_merge"] == "both"]
    value_diff = int((~np.isclose(both["value_r"].astype(float), both["value_f"].astype(float),
                                  rtol=TOL, atol=0)).sum()) if len(both) else 0
    return {"replay": len(replay), "final": len(final),
            "only_in_replay": int((m["_merge"] == "left_only").sum()),
            "only_in_final": int((m["_merge"] == "right_only").sum()),
            "common_with_a_different_value": value_diff}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--replay", default=str(ROOT / "data" / "backtest" / "v1_replay_v3"))
    ap.add_argument("--final", default=str(ROOT / "data" / "backtest" / "v1_final"))
    a = ap.parse_args(argv)
    replay, final = Path(a.replay), Path(a.final)
    def rel(q: Path) -> str:
        """The folder as the repository names it, not as this machine does."""
        try:
            return Path(q).resolve().relative_to(ROOT).as_posix()
        except ValueError:
            return str(q)

    rep = {"replay": rel(replay), "final": rel(final), "tolerance": TOL,
           "nav": compare_nav(pd.read_csv(replay / "nav.csv"), pd.read_csv(final / "nav.csv")),
           "holdings": compare_holdings(pd.read_csv(replay / "holdings.csv"),
                                        pd.read_csv(final / "holdings.csv")),
           "trades": compare_trades(pd.read_csv(replay / "trades.csv"),
                                    pd.read_csv(final / "trades.csv"))}
    rep["identical"] = bool(rep["nav"]["identical"] and not rep["holdings"]
                            and rep["trades"]["only_in_replay"] == 0
                            and rep["trades"]["only_in_final"] == 0)
    (replay / "reproduction.json").write_text(json.dumps(rep, indent=2), encoding="utf-8")
    n = rep["nav"]
    print(f"common marks {n['marks_common']} of {n['marks_replay']} / {n['marks_final']}; "
          f"max rel diff (strategy, universe_ew) {n['max_rel_diff_strategy_and_universe']:.3g}, "
          f"all columns {n['max_rel_diff_all']:.3g}; identical {rep['identical']}")
    if n.get("first_difference"):
        print("first strategy difference:", n["first_difference"])
    print(f"review dates with a different held set: {len(rep['holdings'])}")
    print("trades:", rep["trades"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
