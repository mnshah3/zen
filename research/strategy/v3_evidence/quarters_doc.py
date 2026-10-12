"""Document holes per quarter and basis: a company with ANY stored filing row for
the quarter before and after (same basis) and NO stored row for this quarter.
Unlike quarters.py, a filing whose income statement Clarification 38 refused still
counts as a document here. Archive structure only; read-only; no prices."""
import argparse, os, warnings
from pathlib import Path
warnings.filterwarnings("ignore")
import duckdb, pandas as pd
# python research/strategy/v3_evidence/quarters_doc.py --frozen data/frozen/v3 --out DIR
_ap = argparse.ArgumentParser()
_ap.add_argument("--frozen", required=True)
_ap.add_argument("--out", required=True)
_a = _ap.parse_args()
HERE = os.path.abspath(_a.out)
os.makedirs(HERE, exist_ok=True)
con = duckdb.connect(str(Path(_a.frozen) / "data" / "zen.duckdb"), read_only=True)
os.makedirs(os.path.join(HERE, "duck_q"), exist_ok=True)
con.execute(f"SET temp_directory='{os.path.join(HERE, 'duck_q').replace(os.sep, '/')}'")
df = con.execute("""
SELECT DISTINCT symbol, consolidated, year(period_end) * 4 + (month(period_end) - 1) // 3 AS q
FROM financials WHERE period_end BETWEEN DATE '2018-03-31' AND DATE '2026-06-30'""").df()
rows = []
for cons, g in df.groupby("consolidated"):
    have = set(zip(g["symbol"], g["q"]))
    syms = g["symbol"].unique()
    for q in sorted(g["q"].unique()):
        both = [s for s in syms if (s, q - 1) in have and (s, q + 1) in have]
        holes = sum((s, q) not in have for s in both)
        y, m = divmod(int(q), 4)
        rows.append(dict(quarter=f"{y}-{3*m+3:02d}", basis="cons" if cons else "sa",
                         docs=sum((s, q) in have for s in syms), both=len(both), doc_holes=holes,
                         doc_hole_pct=round(100*holes/len(both), 2) if both else None))
out = pd.DataFrame(rows)
w = out.pivot(index="quarter", columns="basis", values=["docs", "doc_holes", "doc_hole_pct"])
w.columns = [f"{a}_{b}" for a, b in w.columns]
w.to_csv(os.path.join(HERE, "quarters_doc.csv"))
pd.set_option("display.width", 200)
print(w.to_string())
