import duckdb, numpy as np, pandas as pd, sys
sys.path.insert(0,".")
from zen.validation import eventstudy as es
con = duckdb.connect()
for v,p in [("prices","data/daily/**/*.parquet"),("indices","data/indices/*.parquet"),
            ("corpactions","data/corpactions/*.parquet")]:
    con.execute(f"CREATE VIEW {v} AS SELECT * FROM read_parquet('{p}', union_by_name=true)")

px = con.execute("""SELECT date, symbol, open, close, turnover FROM prices
 WHERE isin_code LIKE 'INE%' AND close>0 AND series IN ('EQ','BE') ORDER BY symbol,date""").df()
px["date"]=pd.to_datetime(px["date"])
acts = es.adjustment_factors(con)
if not acts.empty:
    a=acts.rename(columns={"ex_date":"date"})[["symbol","date","cum_factor"]]
    a["date"]=pd.to_datetime(a["date"])
    px=pd.merge_asof(px.sort_values("date"),a.sort_values("date"),on="date",by="symbol",
                     direction="forward",allow_exact_matches=False)
    px["f"]=px["cum_factor"].fillna(1.0)
else: px["f"]=1.0
px["c"]=px["close"]*px["f"]
px=px.sort_values(["symbol","date"]).reset_index(drop=True)
print("panel", px.shape)

g=px.groupby("symbol",sort=False)
px["hi252"]=g["c"].transform(lambda s: s.rolling(252,min_periods=252).max())
px["lo252"]=g["c"].transform(lambda s: s.rolling(252,min_periods=252).min())
px["mean60"]=g["c"].transform(lambda s: s.rolling(60,min_periods=60).mean())
px["turn60"]=g["turnover"].transform(lambda s: s.rolling(60,min_periods=60).median())
r=g["c"].pct_change()
px["v60"]=r.groupby(px["symbol"],sort=False).transform(lambda s: s.rolling(60,min_periods=60).std())
px["v252"]=r.groupby(px["symbol"],sort=False).transform(lambda s: s.rolling(252,min_periods=252).std())
# sessions since the 252d low
px["argmin_age"]=g["c"].transform(lambda s: 251-s.rolling(252,min_periods=252).apply(np.argmin,raw=True))
print("features done")

LIQ=5e6
liquid = (px["turn60"]>=LIQ)&(px["c"]>=10)
sig = (liquid
   & (px["c"] <= 0.60*px["hi252"])          # fell 40%+ from the 1y high
   & (px["argmin_age"] >= 63)               # the low is at least 3 months old
   & (px["v60"] < px["v252"])               # volatility contracting
   & (px["c"] >= px["mean60"])              # holding above its own base
   & (px["c"] <= 1.20*px["mean60"]))        # but not yet rallying
ev = px.loc[sig & px["date"].between("2016-01-01","2025-09-01"),["symbol","date"]].copy()
ev["q"]=ev["date"].dt.to_period("Q")
ev=ev.sort_values("date").drop_duplicates(["symbol","q"])[["symbol","date"]]
ev.columns=["symbol","signal_date"]
print("SIGNALS",len(ev),"symbols",ev.symbol.nunique())

# control: random liquid picks on the same dates, same count per quarter
uni = px.loc[liquid & px["date"].between("2016-01-01","2025-09-01"),["symbol","date"]]
uni["q"]=uni["date"].dt.to_period("Q")
rng=np.random.default_rng(7); ctrl=[]
for q,n in ev.assign(q=ev.signal_date.dt.to_period("Q")).groupby("q").size().items():
    pool=uni[uni["q"]==q]
    if len(pool)==0: continue
    ctrl.append(pool.sample(min(n*3,len(pool)),random_state=int(rng.integers(1e6))))
ctrl=pd.concat(ctrl)[["symbol","date"]].drop_duplicates(); ctrl.columns=["symbol","signal_date"]
print("CONTROL",len(ctrl))
ev.to_parquet(".scratch_fs/ev.parquet"); ctrl.to_parquet(".scratch_fs/ctrl.parquet")
