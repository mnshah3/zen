import duckdb, pandas as pd, numpy as np, sys
sys.path.insert(0,".")
from zen.validation import eventstudy as es
con=duckdb.connect()
for v,p in [("prices","data/daily/**/*.parquet"),("indices","data/indices/*.parquet"),
            ("corpactions","data/corpactions/*.parquet")]:
    con.execute(f"CREATE VIEW {v} AS SELECT * FROM read_parquet('{p}', union_by_name=true)")
def run(name,f):
    ev=pd.read_parquet(f)
    out=es.measure(con,ev,horizons=(12,))
    o=out[(out.horizon_months==12)&out.complete]
    beat=(o.excess_return>0).mean()
    print(f"{name:8s} n={len(o):5d} beat={beat:.1%} med_ret={o.stock_return.median():+.1%} "
          f"med_exc={o.excess_return.median():+.1%} mean_exc={o.excess_return.mean():+.1%} "
          f"p25={o.stock_return.quantile(.25):+.1%} delisted={o.delisted.mean():.1%}")
    return o
a=run("SIGNAL",".scratch_fs/ev.parquet")
b=run("CONTROL",".scratch_fs/ctrl.parquet")
from math import sqrt
p1,n1=(a.excess_return>0).mean(),len(a); p2,n2=(b.excess_return>0).mean(),len(b)
p=(p1*n1+p2*n2)/(n1+n2); z=(p1-p2)/sqrt(p*(1-p)*(1/n1+1/n2))
print(f"\nbeat-rate diff z={z:.2f}")
a["yr"]=pd.to_datetime(a.signal_date).dt.year
print("\nBY YEAR (signal):")
print(a.groupby("yr").agg(n=("excess_return","size"),beat=("excess_return",lambda s:(s>0).mean()),
    med_exc=("excess_return","median")).to_string())
