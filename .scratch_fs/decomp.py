import duckdb, numpy as np, pandas as pd, sys
sys.path.insert(0,".")
from zen.validation import eventstudy as es
con=duckdb.connect()
for v,p in [("prices","data/daily/**/*.parquet"),("indices","data/indices/*.parquet"),
            ("corpactions","data/corpactions/*.parquet")]:
    con.execute(f"CREATE VIEW {v} AS SELECT * FROM read_parquet('{p}', union_by_name=true)")
px = con.execute("""SELECT date, symbol, open, close, turnover FROM prices
 WHERE isin_code LIKE 'INE%' AND close>0 AND series IN ('EQ','BE') ORDER BY symbol,date""").df()
px["date"]=pd.to_datetime(px["date"])
acts=es.adjustment_factors(con)
a=acts.rename(columns={"ex_date":"date"})[["symbol","date","cum_factor"]]; a["date"]=pd.to_datetime(a["date"])
px=pd.merge_asof(px.sort_values("date"),a.sort_values("date"),on="date",by="symbol",direction="forward",allow_exact_matches=False)
px["c"]=px["close"]*px["cum_factor"].fillna(1.0)
px=px.sort_values(["symbol","date"]).reset_index(drop=True)
g=px.groupby("symbol",sort=False)
px["hi252"]=g["c"].transform(lambda s:s.rolling(252,min_periods=252).max())
px["mean60"]=g["c"].transform(lambda s:s.rolling(60,min_periods=60).mean())
px["turn60"]=g["turnover"].transform(lambda s:s.rolling(60,min_periods=60).median())
r=g["c"].pct_change()
px["v60"]=r.groupby(px["symbol"],sort=False).transform(lambda s:s.rolling(60,min_periods=60).std())
px["v252"]=r.groupby(px["symbol"],sort=False).transform(lambda s:s.rolling(252,min_periods=252).std())
px["age"]=g["c"].transform(lambda s:251-s.rolling(252,min_periods=252).apply(np.argmin,raw=True))
liquid=(px["turn60"]>=5e6)&(px["c"]>=10)
win=px["date"].between("2016-01-01","2025-09-01")
dd   = px["c"]<=0.60*px["hi252"]
base = (px["age"]>=63)&(px["v60"]<px["v252"])&(px["c"]>=px["mean60"])&(px["c"]<=1.20*px["mean60"])
mild = px["c"].between(0.85*px["hi252"],1.00*px["hi252"])   # near highs, NOT drawn down
def mk(mask,tag):
    ev=px.loc[mask&liquid&win,["symbol","date"]].copy()
    ev["q"]=ev["date"].dt.to_period("Q")
    ev=ev.sort_values("date").drop_duplicates(["symbol","q"])[["symbol","date"]]
    ev.columns=["symbol","signal_date"]
    if len(ev)>6000: ev=ev.sample(6000,random_state=3)
    o=es.measure(con,ev,horizons=(12,)); o=o[(o.horizon_months==12)&o.complete]
    ex=o[pd.to_datetime(o.signal_date).dt.year!=2020]
    print(f"{tag:26s} n={len(o):5d} beat={(o.excess_return>0).mean():.1%} med_exc={o.excess_return.median():+.1%} "
          f"delist={o.delisted.mean():.1%} | ex-2020 n={len(ex)} beat={(ex.excess_return>0).mean():.1%} med_exc={ex.excess_return.median():+.1%}")
mk(dd,"deep drawdown only")
mk(base,"base/vol-contraction only")
mk(mild,"near 1y high (no drawdown)")
