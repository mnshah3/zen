import duckdb, pandas as pd
con = duckdb.connect()
con.execute("CREATE VIEW financials AS SELECT * FROM read_parquet('data/financials/*.parquet', union_by_name=true)")
n=con.execute("select count(*) from financials").fetchone()[0]
print("ROWS", n)
print(con.execute("""select period_end, consolidated, count(*) n,
 sum(case when has_balance_sheet then 1 else 0 end) bs,
 count(distinct symbol) syms,
 cast(min(broadcast_dt) as date) minb, cast(max(broadcast_dt) as date) maxb
 from financials group by 1,2 order by 1,2""").df().to_string())
cols=[c[0] for c in con.execute("describe financials").fetchall()]
rows=[]
for c in cols:
    nn=con.execute(f'select count("{c}") from financials').fetchone()[0]
    rows.append((c, nn, round(100*nn/n,1)))
print("\nNULL PROFILE"); print(pd.DataFrame(rows, columns=["col","nonnull","pct"]).to_string())
