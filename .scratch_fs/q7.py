import duckdb, pandas as pd
pd.set_option("display.width",200)
con=duckdb.connect()
con.execute("CREATE VIEW financials AS SELECT * FROM read_parquet('data/financials/*.parquet', union_by_name=true)")
print("== lenders D/E Sep-25 ==")
print(con.execute("""select symbol, round(debt_to_equity,2) de, round(equity/1e7,0) eq_cr from financials
 where consolidated and period_end=date '2025-09-30' and symbol in
 ('HDFCBANK','ICICIBANK','SBIN','BAJFINANCE','AXISBANK','KOTAKBANK','CHOLAFIN','LICHSGFIN','MUTHOOTFIN','SHRIRAMFIN')
 order by de desc""").df().to_string())
print("\n== how many Sep-25 rows have D/E > 1.5 (would be cut) ==")
print(con.execute("""select count(*) from financials where consolidated and period_end=date '2025-09-30' and debt_to_equity>1.5""").fetchone())
print("\n== negative-equity rows ==")
print(con.execute("""select count(*) from financials where consolidated and period_end=date '2025-09-30' and equity<0""").fetchone())
print("\n== EBITDA<=0 or revenue<=0 at Sep-25 (loss-makers) ==")
print(con.execute("""select sum(case when ebitda<=0 then 1 else 0 end) neg_ebitda,
 sum(case when profit_normalised<=0 then 1 else 0 end) loss, count(*) n
 from financials where consolidated and period_end=date '2025-09-30'""").df().to_string())
