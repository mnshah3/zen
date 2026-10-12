"""v3 spec, final revision: companies per quarter and basis in the financials
archive, and 'holes' (a company with an income statement for the quarter before
and the quarter after, on the same basis, but none for this one). Archive
structure only: no prices, no returns. Read-only DB. All rows as stored, so this
describes the archive as it stands, not what was known at any review date."""
import argparse, os, warnings
from pathlib import Path
warnings.filterwarnings("ignore")
import duckdb, pandas as pd

# python research/strategy/v3_evidence/quarters.py --frozen data/frozen/v3 --out DIR
# (section 15.2.5: the same counts, on the frozen copy)
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
SELECT DISTINCT symbol, consolidated,
       year(period_end) * 4 + (month(period_end) - 1) // 3 AS q
FROM financials
WHERE period_end BETWEEN DATE '2018-03-31' AND DATE '2026-06-30'
  AND coalesce(revenue, total_income, other_income, employee_cost, ebitda,
               profit_normalised) IS NOT NULL
""").df()
first_b = con.execute("SELECT min(broadcast_dt) FROM financials").fetchone()[0]
rows = []
for cons, g in df.groupby("consolidated"):
    have = set(zip(g["symbol"], g["q"]))
    syms = g["symbol"].unique()
    for q in sorted(g["q"].unique()):
        n = sum((s, q) in have for s in syms)
        both = [s for s in syms if (s, q - 1) in have and (s, q + 1) in have]
        holes = sum((s, q) not in have for s in both)
        # for the first quarter: companies with the next two quarters but not this one
        lead = sum(((s, q + 1) in have and (s, q + 2) in have and (s, q) not in have)
                   for s in syms) if q == g["q"].min() else None
        y, m = divmod(int(q), 4)
        rows.append(dict(quarter=f"{y}-{3 * m + 3:02d}", basis="cons" if cons else "sa",
                         companies=n, with_both_neighbours=len(both), holes=holes,
                         hole_pct=round(100 * holes / len(both), 2) if both else None,
                         first_quarter_lead_missing=lead))
out = pd.DataFrame(rows)
wide = out.pivot(index="quarter", columns="basis",
                 values=["companies", "holes", "hole_pct"])
wide.columns = [f"{a}_{b}" for a, b in wide.columns]
wide.to_csv(os.path.join(HERE, "quarters.csv"))
pd.set_option("display.width", 200)
print("first broadcast in the archive:", first_b)
print(wide.to_string())
print(out[out["first_quarter_lead_missing"].notna()].to_string())
