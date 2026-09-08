import duckdb, numpy as np, pandas as pd, sys
sys.path.insert(0,".")
from zen.validation import eventstudy as es
con=duckdb.connect()
for v,p in [("prices","data/daily/**/*.parquet"),("indices","data/indices/*.parquet"),
            ("corpactions","data/corpactions/*.parquet"),("announcements","data/announcements/**/*.parquet")]:
    con.execute(f"CREATE VIEW {v} AS SELECT * FROM read_parquet('{p}', union_by_name=true)")
px=con.execute("""SELECT date,symbol,open,close,turnover FROM prices
 WHERE isin_code LIKE 'INE%' AND close>0 AND series IN ('EQ','BE') ORDER BY symbol,date""").df()
px["date"]=pd.to_datetime(px["date"])
acts=es.adjustment_factors(con); a=acts.rename(columns={"ex_date":"date"})[["symbol","date","cum_factor"]]
a["date"]=pd.to_datetime(a["date"])
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
liquid=(px["turn60"]>=5e6)&(px["c"]>=10)
QUIET=(px["c"]>=0.75*px["hi252"])&(px["c"]<=0.98*px["hi252"])&(px["v60"]<px["v252"])&(px["c"]>=px["mean60"])
def score(ev,tag):
    o=es.measure(con,ev,horizons=(12,)); o=o[(o.horizon_months==12)&o.complete]
    if not len(o): print(tag,"empty"); return
    ex=o[pd.to_datetime(o.signal_date).dt.year!=2020]
    print(f"{tag:30s} n={len(o):5d} beat={(o.excess_return>0).mean():.1%} med_exc={o.excess_return.median():+.1%} "
          f"med_ret={o.stock_return.median():+.1%} delist={o.delisted.mean():.1%} | ex2020 beat={(ex.excess_return>0).mean():.1%}")
# TEST 5: quiet-and-intact, no drawdown precondition
ev=px.loc[QUIET&liquid&px["date"].between("2016-01-01","2025-09-01"),["symbol","date"]].copy()
ev["q"]=ev["date"].dt.to_period("Q"); ev=ev.sort_values("date").drop_duplicates(["symbol","q"])[["symbol","date"]]
ev.columns=["symbol","signal_date"]
print("QUIET signals",len(ev)); score(ev.sample(6000,random_state=5) if len(ev)>6000 else ev,"QUIET (no drawdown)")
# TEST 6/7: order filings, all vs order+QUIET
orders=con.execute("""select symbol, trade_date as signal_date from announcements
 where category='orders' and trade_date between date '2022-01-01' and date '2023-12-31'""").df()
orders["signal_date"]=pd.to_datetime(orders["signal_date"])
orders=orders.drop_duplicates()
st=px.loc[liquid,["symbol","date"]].assign(liq=1)
q=px.loc[QUIET&liquid,["symbol","date"]].assign(quiet=1)
om=orders.merge(st,left_on=["symbol","signal_date"],right_on=["symbol","date"],how="inner")[["symbol","signal_date"]]
oq=orders.merge(q,left_on=["symbol","signal_date"],right_on=["symbol","date"],how="inner")[["symbol","signal_date"]]
print("orders-liquid",len(om),"orders-quiet",len(oq))
score(om.drop_duplicates().sample(min(2500,len(om)),random_state=5),"ORDERS (liquid)")
score(oq.drop_duplicates(),"ORDERS + QUIET")
