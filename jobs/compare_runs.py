"""Compare two strategy run folders written in the v2 REQUIRED OUTPUTS format.

Generic: it knows the seven agreed files, not either engine. Each file is
joined on its natural key and every shared column is compared, numbers at a
relative tolerance (default 1e-9), text exactly. Nothing here reads a price,
recomputes a return or opens the database; it only reads the two folders.

    .venv/Scripts/python.exe -m jobs.compare_runs A_DIR B_DIR --out REPORT_DIR
        [--label-a production] [--label-b checker] [--rtol 1e-9] [--atol 1e-12]
        [--show-levels]

Files and keys
    universe.csv   (D, symbol)                        membership only
    ranks.csv      (D, symbol)                        every measure, percentile,
                                                      group score, composite, rank
                                                      and any other shared column
    decisions.csv  (D)                                every shared column
    holdings.csv   (D, symbol)                        ticker, rank, sector, sigma,
                                                      target_weight, status,
                                                      sold_reason (+ shared extras)
    fills.csv      (D, symbol, kind, side, n)         n = order of the row within
                                                      that group by (scheduled_date,
                                                      date); every shared column
    a1.json        headline figure and counts; per position (D, symbol)
    nav.csv        (date, mark)                       every column, each divided by
                                                      its own first value, so a
                                                      rupee NAV and an index at 1.0
                                                      compare on the same scale

Known spellings. The two engines were asked for identical formats but a few
column names and labels differ. They are mapped to one name before comparing,
and the mapping is written into the report so nothing is hidden:
    ranks     X_pct -> pct_X; quality/growth/value/momentum/risk -> grp_*;
              quality_basis -> quality_measure
    a1.json   see A1_HEADLINE_ALIASES and A1_POSITION_ALIASES
    labels    sold_reason is compared raw; where the raw text differs, the
              difference is classed 'label' when both fall in the same class
              (SOLD_REASON_CLASSES) and 'value' otherwise.

Difference classes in differences.csv
    value           both present, not equal within tolerance (or text differs)
    blank_vs_value  one engine left the cell blank, the other did not
    label           raw text differs, same meaning under SOLD_REASON_CLASSES
    only_in_a / only_in_b   the key exists in one folder only
    missing_column  a required column is absent from one folder

NAV levels are returns, and this project reads none in passing: for nav.csv a
difference is written with its relative size only, its two values shown as
'(masked)', unless --show-levels is given.

Outputs in --out: report.md (summary and the first rows of each difference
class per file and column), differences.csv (every difference), summary.json.
Exit code 0 when no difference of class value, blank_vs_value, only_in_* or
missing_column is found in any file, else 1.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

MEASURES = ["margin", "roce", "stability", "rev_growth", "profit_growth",
            "earnings_yield", "sales_yield", "mom_12_1", "low_vol"]
GROUPS = ["quality", "growth", "value", "momentum", "risk"]

REQUIRED = {
    "universe.csv": ["D", "symbol"],
    "ranks.csv": ["D", "symbol"] + MEASURES + [f"pct_{m}" for m in MEASURES]
    + [f"grp_{g}" for g in GROUPS] + ["composite", "rank"],
    "decisions.csv": ["D", "trend_c1", "trend_c2", "trend_c3", "cash_fraction", "n_universe",
                      "n_excluded_hard_filters", "n_excluded_no_balance_sheet",
                      "n_ranked_on_roce", "n_held", "n_held_unlabelled_sector"],
    "holdings.csv": ["D", "symbol", "ticker", "rank", "sector", "sigma", "target_weight",
                     "status", "sold_reason"],
    "fills.csv": ["date", "symbol", "ticker", "side", "units_adjusted", "price_adjusted",
                  "value", "cost", "kind", "scheduled_date", "D"],
    "nav.csv": ["date", "mark", "strategy", "universe_ew", "nifty500", "midcap150",
                "smallcap250", "momentum30", "value50", "quality30", "lowvol30", "alpha50",
                "momentum30_no_overnight", "value50_no_overnight", "quality30_no_overnight",
                "lowvol30_no_overnight", "alpha50_no_overnight"],
}

KEYS = {
    "universe.csv": ["D", "symbol"],
    "ranks.csv": ["D", "symbol"],
    "decisions.csv": ["D"],
    "holdings.csv": ["D", "symbol"],
    "fills.csv": ["D", "symbol", "kind", "side", "n"],
    "nav.csv": ["date", "mark"],
}

# canonical name <- other spellings
RANK_ALIASES = {**{f"pct_{m}": [f"{m}_pct"] for m in MEASURES},
                **{f"grp_{g}": [g] for g in GROUPS},
                "quality_measure": ["quality_basis"]}
A1_HEADLINE_ALIASES = {
    "v_weighted": ["v_weighted_ratio", "v_weighted_relative_price"],
    "n_positions": ["n_positions", "n_new_positions"],
    "n_with_fills": ["n_with_fills", "n_with_a_fill"],
    "n_without_fills": ["n_without_fills", "n_with_no_fill"],
}
A1_POSITION_ALIASES = {
    "ratio": ["ratio", "relative_price"],
    "avg_price": ["avg_price", "avg_fill_price_adjusted"],
    "close_before_D": ["close_before_D", "ref_close_adjusted"],
    "V": ["V"],
    "tranches_filled": ["tranches_filled"],
    "units": ["units"],
    "value": ["value"],
}

# sold_reason classes: text that starts with any prefix maps to the class
SOLD_REASON_CLASSES = {
    "outside_band": ["outside_band", "rank_outside_"],
    "left_universe": ["left_universe"],
    "thesis_break": ["thesis_break", "hard_filter"],
    "not_selected": ["not_selected"],
}

TEXT_COLUMNS = {"D", "date", "mark", "symbol", "ticker", "sector", "status", "sold_reason",
                "side", "kind", "scheduled_date", "quality_measure", "cancelled_kind",
                "reason", "sector_scheme", "weight_bound", "tri_date"}
BOOL_COLUMNS = {"trend_c1", "trend_c2", "trend_c3"}
DIFF_COLUMNS = ["file", "key", "column", "a", "b", "class", "rel_diff"]
MASKED_FILES = ("nav.csv",)


# ------------------------------------------------------------------ helpers
def close_enough(a: float, b: float, rtol: float, atol: float) -> bool:
    if math.isnan(a) and math.isnan(b):
        return True
    if math.isnan(a) or math.isnan(b):
        return False
    return abs(a - b) <= rtol * max(abs(a), abs(b)) + atol


def rel_diff(a: float, b: float) -> float:
    if math.isnan(a) or math.isnan(b):
        return float("nan")
    m = max(abs(a), abs(b))
    return 0.0 if m == 0 else abs(a - b) / m


def sold_reason_class(x: str) -> str:
    for cls, prefixes in SOLD_REASON_CLASSES.items():
        if any(x.startswith(p) for p in prefixes):
            return cls
    return x


def _text(s: pd.Series) -> pd.Series:
    return s.astype(object).where(s.notna(), "").astype(str).str.strip()


def _bool(s: pd.Series) -> pd.Series:
    t = _text(s).str.lower()
    return t.map({"true": "True", "1": "True", "1.0": "True",
                  "false": "False", "0": "False", "0.0": "False", "": ""}).fillna(t)


def _disp(x) -> str:
    return "" if x is None or (isinstance(x, float) and math.isnan(x)) else str(x).strip()


def _num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce").astype(float)


def _is_numeric(s: pd.Series) -> bool:
    t = _text(s)
    t = t[t != ""]
    return len(t) == 0 or pd.to_numeric(t, errors="coerce").notna().all()


def apply_aliases(df: pd.DataFrame, aliases: dict[str, list[str]]) -> tuple[pd.DataFrame, list[str]]:
    renamed = []
    for canon, others in aliases.items():
        if canon in df.columns:
            continue
        for o in others:
            if o in df.columns:
                df = df.rename(columns={o: canon})
                renamed.append(f"{o} -> {canon}")
                break
    return df, renamed


def read_csv(folder: Path, name: str) -> pd.DataFrame | None:
    p = folder / name
    if not p.exists():
        return None
    return pd.read_csv(p, dtype=str, keep_default_na=False, na_values=[""])


# ------------------------------------------------------------------ core
class Comparison:
    def __init__(self, a: Path, b: Path, rtol: float = 1e-9, atol: float = 1e-12,
                 label_a: str = "a", label_b: str = "b", show_levels: bool = False):
        self.a, self.b = Path(a), Path(b)
        self.rtol, self.atol = rtol, atol
        self.label_a, self.label_b = label_a, label_b
        # NAV levels are returns: by default only their relative difference is written
        self.masked = set() if show_levels else set(MASKED_FILES)
        self.diffs: list[dict] = []
        self.notes: list[str] = []
        self.stats: dict[str, dict] = {}

    # ---- record
    def add(self, file, key, column, a, b, cls, rd=float("nan")):
        if file in self.masked and cls == "value":
            a = b = "(masked)"
        self.diffs.append({"file": file, "key": key, "column": column, "a": a, "b": b,
                           "class": cls, "rel_diff": rd})

    # ---- frames
    def compare_frames(self, file: str, fa: pd.DataFrame, fb: pd.DataFrame, keys: list[str],
                       skip: set[str] = frozenset()) -> None:
        req = [c for c in REQUIRED.get(file, []) if c not in keys and c != "n"]
        for c in req:
            if c not in fa.columns or c not in fb.columns:
                self.add(file, "", c, "present" if c in fa.columns else "absent",
                         "present" if c in fb.columns else "absent", "missing_column")
        for k in keys:
            fa[k] = _text(fa[k])
            fb[k] = _text(fb[k])
        dup_a = fa.duplicated(keys).sum()
        dup_b = fb.duplicated(keys).sum()
        if dup_a or dup_b:
            self.notes.append(f"{file}: duplicate keys {self.label_a}={dup_a} {self.label_b}={dup_b}; "
                              "later duplicates dropped")
            fa = fa.drop_duplicates(keys)
            fb = fb.drop_duplicates(keys)
        m = fa.merge(fb, on=keys, how="outer", suffixes=("__a", "__b"), indicator=True)
        keystr = m[keys].astype(str).agg("|".join, axis=1)
        for i in np.flatnonzero((m["_merge"] == "left_only").to_numpy()):
            self.add(file, keystr.iat[i], "(row)", "present", "", "only_in_a")
        for i in np.flatnonzero((m["_merge"] == "right_only").to_numpy()):
            self.add(file, keystr.iat[i], "(row)", "", "present", "only_in_b")
        both = (m["_merge"] == "both").to_numpy()
        shared = [c for c in fa.columns if c in fb.columns and c not in keys and c not in skip]
        st = {"rows_a": len(fa), "rows_b": len(fb), "rows_joined": int(both.sum()),
              "columns_compared": shared,
              "columns_only_in_a": [c for c in fa.columns if c not in fb.columns],
              "columns_only_in_b": [c for c in fb.columns if c not in fa.columns],
              "max_rel_diff": {}}
        mb = m[both]
        kb = keystr[both]
        for c in shared:
            sa, sb = mb[c + "__a"], mb[c + "__b"]
            if c in BOOL_COLUMNS:
                ta, tb = _bool(sa), _bool(sb)
                self._text_diffs(file, c, kb, ta, tb)
            elif c not in TEXT_COLUMNS and _is_numeric(sa) and _is_numeric(sb):
                na, nb = _num(sa).to_numpy(), _num(sb).to_numpy()
                mx = 0.0
                for j in range(len(na)):
                    x, y = na[j], nb[j]
                    if close_enough(x, y, self.rtol, self.atol):
                        if not (math.isnan(x) or math.isnan(y)):
                            mx = max(mx, rel_diff(x, y))
                        continue
                    if math.isnan(x) != math.isnan(y):
                        self.add(file, kb.iat[j], c, _disp(sa.iat[j]), _disp(sb.iat[j]),
                                 "blank_vs_value")
                    else:
                        rd = rel_diff(x, y)
                        self.add(file, kb.iat[j], c, repr(float(x)), repr(float(y)), "value", rd)
                st["max_rel_diff"][c] = mx
            else:
                self._text_diffs(file, c, kb, _text(sa), _text(sb))
        self.stats[file] = st

    def _text_diffs(self, file, c, kb, ta, tb):
        neq = (ta != tb).to_numpy()
        for j in np.flatnonzero(neq):
            x, y = ta.iat[j], tb.iat[j]
            if x == "" or y == "":
                cls = "blank_vs_value"
            elif c == "sold_reason" and sold_reason_class(x) == sold_reason_class(y):
                cls = "label"
            else:
                cls = "value"
            self.add(file, kb.iat[j], c, x, y, cls)

    # ---- files
    def run(self) -> "Comparison":
        for f in ("universe.csv", "ranks.csv", "decisions.csv", "holdings.csv", "fills.csv",
                  "nav.csv"):
            fa, fb = read_csv(self.a, f), read_csv(self.b, f)
            if fa is None or fb is None:
                self.add(f, "", "(file)", "absent" if fa is None else "present",
                         "absent" if fb is None else "present", "missing_column")
                continue
            getattr(self, "_" + f.replace(".csv", ""))(fa, fb)
        self._a1()
        return self

    def _universe(self, fa, fb):
        self.compare_frames("universe.csv", fa[["D", "symbol"]].copy(),
                            fb[["D", "symbol"]].copy(), KEYS["universe.csv"])

    def _ranks(self, fa, fb):
        fa, ra = apply_aliases(fa, RANK_ALIASES)
        fb, rb = apply_aliases(fb, RANK_ALIASES)
        if ra:
            self.notes.append(f"ranks.csv {self.label_a} renamed: " + ", ".join(ra))
        if rb:
            self.notes.append(f"ranks.csv {self.label_b} renamed: " + ", ".join(rb))
        self.compare_frames("ranks.csv", fa, fb, KEYS["ranks.csv"])

    def _decisions(self, fa, fb):
        self.compare_frames("decisions.csv", fa, fb, KEYS["decisions.csv"])

    def _holdings(self, fa, fb):
        self.compare_frames("holdings.csv", fa, fb, KEYS["holdings.csv"])

    @staticmethod
    def _number_fills(f: pd.DataFrame) -> pd.DataFrame:
        f = f.copy()
        for c in ("D", "symbol", "kind", "side", "scheduled_date", "date"):
            if c in f.columns:
                f[c] = _text(f[c])
        order = [c for c in ("scheduled_date", "date") if c in f.columns]
        f = f.sort_values(["D", "symbol", "kind", "side"] + order, kind="mergesort")
        f["n"] = f.groupby(["D", "symbol", "kind", "side"]).cumcount().astype(str)
        return f

    def _fills(self, fa, fb):
        self.compare_frames("fills.csv", self._number_fills(fa), self._number_fills(fb),
                            KEYS["fills.csv"])

    def _nav(self, fa, fb):
        for f in (fa, fb):
            f["date"] = _text(f["date"])
            f["mark"] = _text(f["mark"])
        cols = [c for c in fa.columns if c not in ("date", "mark")]
        firsts = {}
        for lab, f in ((self.label_a, fa), (self.label_b, fb)):
            for c in [c for c in f.columns if c not in ("date", "mark")]:
                s = _num(f[c])
                first = s.dropna().iloc[0] if s.notna().any() else float("nan")
                firsts.setdefault(c, {})[lab] = first
                f[c] = (s / first).astype(float) if first and not math.isnan(first) else s
        start_diff = {c: v for c, v in firsts.items()
                      if len(v) == 2 and not close_enough(v[self.label_a], v[self.label_b],
                                                          self.rtol, self.atol)}
        if start_diff:
            self.notes.append("nav.csv starting values differ (compared after dividing each "
                              "column by its first value): "
                              + "; ".join(f"{c}: {self.label_a}={float(v[self.label_a])!r} "
                                          f"{self.label_b}={float(v[self.label_b])!r}"
                                          for c, v in start_diff.items()))
        self.compare_frames("nav.csv", fa, fb, KEYS["nav.csv"])
        # first divergence per column, in clock order
        st = self.stats["nav.csv"]
        first_div = {}
        nav_d = [d for d in self.diffs if d["file"] == "nav.csv" and d["column"] in cols]
        for d in nav_d:
            first_div.setdefault(d["column"], d["key"])
        st["first_divergence"] = first_div
        st["start_values"] = firsts

    def _a1(self):
        pa, pb = self.a / "a1.json", self.b / "a1.json"
        if not (pa.exists() and pb.exists()):
            self.add("a1.json", "", "(file)", "present" if pa.exists() else "absent",
                     "present" if pb.exists() else "absent", "missing_column")
            return
        ja, jb = json.loads(pa.read_text()), json.loads(pb.read_text())
        rows = []
        for canon, names in A1_HEADLINE_ALIASES.items():
            va = next((ja[n] for n in names if n in ja), None)
            vb = next((jb[n] for n in names if n in jb), None)
            rows.append((canon, va, vb))
        ha = pd.DataFrame({"D": ["headline"]})
        hb = pd.DataFrame({"D": ["headline"]})
        for canon, va, vb in rows:
            if va is not None and vb is not None:
                ha[canon] = [str(va)]
                hb[canon] = [str(vb)]
            elif va is not None or vb is not None:
                self.add("a1.json", "headline", canon, "" if va is None else str(va),
                         "" if vb is None else str(vb), "missing_column")
        self.compare_frames("a1.json:headline", ha, hb, ["D"])

        def positions(j):
            p = pd.DataFrame(j.get("positions", []))
            if p.empty:
                return pd.DataFrame(columns=["D", "symbol"])
            p, _ = apply_aliases(p, A1_POSITION_ALIASES)
            keep = ["D", "symbol"] + [c for c in A1_POSITION_ALIASES if c in p.columns]
            return p[keep].astype(str).replace({"None": "", "nan": ""})

        self.compare_frames("a1.json:positions", positions(ja), positions(jb), ["D", "symbol"])

    # ---- report
    def frame(self) -> pd.DataFrame:
        return pd.DataFrame(self.diffs, columns=DIFF_COLUMNS)

    def summary(self) -> dict:
        d = self.frame()
        out = {"a": str(self.a), "b": str(self.b), "label_a": self.label_a,
               "label_b": self.label_b, "rtol": self.rtol, "atol": self.atol,
               "notes": self.notes, "files": {}}
        files = list(self.stats) + [f for f in d["file"].unique() if f not in self.stats]
        for f in files:
            sub = d[d["file"] == f]
            st = self.stats.get(f, {})
            out["files"][f] = {
                "rows_a": st.get("rows_a"), "rows_b": st.get("rows_b"),
                "rows_joined": st.get("rows_joined"),
                "columns_compared": st.get("columns_compared", []),
                "columns_only_in_a": st.get("columns_only_in_a", []),
                "columns_only_in_b": st.get("columns_only_in_b", []),
                "n_differences": int(len(sub)),
                "by_class": {k: int(v) for k, v in sub["class"].value_counts().items()},
                "by_column": {k: int(v) for k, v in sub["column"].value_counts().items()},
                "max_rel_diff_within_tolerance": st.get("max_rel_diff", {}),
                **({"first_divergence": st["first_divergence"]} if "first_divergence" in st else {}),
            }
        hard = d["class"].isin(["value", "blank_vs_value", "only_in_a", "only_in_b",
                                "missing_column"])
        out["n_differences"] = int(len(d))
        out["n_substantive"] = int(hard.sum())
        out["identical_within_tolerance"] = bool(len(d) == 0)
        return out

    def write(self, out: Path, max_rows: int = 25) -> dict:
        out = Path(out)
        out.mkdir(parents=True, exist_ok=True)
        d = self.frame()
        d.to_csv(out / "differences.csv", index=False)
        s = self.summary()
        (out / "summary.json").write_text(json.dumps(s, indent=2, default=str))
        L = [f"# Run comparison: {self.label_a} vs {self.label_b}", "",
             f"- {self.label_a}: `{self.a}`", f"- {self.label_b}: `{self.b}`",
             f"- tolerance: |a-b| <= {self.rtol:g} x max(|a|,|b|) + {self.atol:g}; "
             "nav.csv columns divided by their first value", "",
             f"**{s['n_differences']} differences, {s['n_substantive']} of class value, "
             "blank_vs_value, only_in_\\* or missing_column**", ""]
        if self.notes:
            L += ["## Notes", ""] + [f"- {n}" for n in self.notes] + [""]
        L += ["## By file", "", "| file | rows a | rows b | joined | differences | by class |",
              "|---|---|---|---|---|---|"]
        for f, v in s["files"].items():
            bc = ", ".join(f"{k} {n}" for k, n in v["by_class"].items()) or "none"
            L.append(f"| {f} | {v['rows_a']} | {v['rows_b']} | {v['rows_joined']} | "
                     f"{v['n_differences']} | {bc} |")
        L.append("")
        for f, v in s["files"].items():
            if v["columns_only_in_a"] or v["columns_only_in_b"]:
                L.append(f"- {f}: columns only in {self.label_a}: {v['columns_only_in_a']}; "
                         f"only in {self.label_b}: {v['columns_only_in_b']} (not compared)")
        L.append("")
        for f in s["files"]:
            sub = d[d["file"] == f]
            if sub.empty:
                continue
            L += [f"## {f}", ""]
            fd = s["files"][f].get("first_divergence")
            if fd:
                L += ["First divergence per column (date|mark): "
                      + ", ".join(f"{c} {k}" for c, k in fd.items()), ""]
            for (col, cls), g in sub.groupby(["column", "class"], sort=False):
                L.append(f"### {col}: {cls}, {len(g)} rows"
                         + (f", max rel diff {g['rel_diff'].max():.3g}"
                            if g["rel_diff"].notna().any() else ""))
                L += ["", "| key | a | b | rel diff |", "|---|---|---|---|"]
                for _, r in g.head(max_rows).iterrows():
                    rd = "" if pd.isna(r["rel_diff"]) else f"{r['rel_diff']:.3g}"
                    L.append(f"| {r['key']} | {r['a']} | {r['b']} | {rd} |")
                if len(g) > max_rows:
                    L.append(f"| ... {len(g) - max_rows} more in differences.csv | | | |")
                L.append("")
        (out / "report.md").write_text("\n".join(L), encoding="utf-8")
        return s


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("a")
    ap.add_argument("b")
    ap.add_argument("--out", required=True)
    ap.add_argument("--label-a", default="a")
    ap.add_argument("--label-b", default="b")
    ap.add_argument("--rtol", type=float, default=1e-9)
    ap.add_argument("--atol", type=float, default=1e-12)
    ap.add_argument("--max-rows", type=int, default=25)
    ap.add_argument("--show-levels", action="store_true",
                    help="write the (scaled) nav.csv values of each difference; by default only "
                         "the relative difference is written, so no return is read in passing")
    args = ap.parse_args(argv)
    c = Comparison(Path(args.a), Path(args.b), args.rtol, args.atol, args.label_a, args.label_b,
                   args.show_levels).run()
    s = c.write(Path(args.out), args.max_rows)
    print(f"{s['n_differences']} differences ({s['n_substantive']} substantive); "
          f"report: {Path(args.out) / 'report.md'}")
    for f, v in s["files"].items():
        print(f"  {f:22s} rows {v['rows_a']}/{v['rows_b']} joined {v['rows_joined']} "
              f"diffs {v['n_differences']} {v['by_class']}")
    return 0 if s["n_substantive"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
