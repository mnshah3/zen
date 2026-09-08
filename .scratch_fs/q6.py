import duckdb, pandas as pd
pd.set_option("display.width",220)
con=duckdb.connect()
con.execute("CREATE VIEW financials AS SELECT * FROM read_parquet('data/financials/*.parquet', union_by_name=true)")
print("== consol symbols with all 3 quarters AND ebitda in all 3 ==")
print(con.execute("""select count(*) from (select symbol from financials where consolidated
 and ebitda is not null and revenue is not null group by 1 having count(distinct period_end)=3)""").fetchone())
print("\n== of those, how many also have Sep-25 balance sheet / D/E ==")
print(con.execute("""with tri as (select symbol from financials where consolidated
 and ebitda is not null and revenue is not null group by 1 having count(distinct period_end)=3)
 select count(*) from tri join financials f on f.symbol=tri.symbol
 where f.consolidated and f.period_end=date '2025-09-30' and f.debt_to_equity is not null""").fetchone())
print("\n== D/E distribution Sep-25 consol ==")
print(con.execute("""select count(*) n, median(debt_to_equity) med,
 sum(case when debt_to_equity<=1.5 then 1 else 0 end) under15,
 sum(case when debt_to_equity<0 then 1 else 0 end) neg
 from financials where consolidated and period_end=date '2025-09-30' and debt_to_equity is not null""").df().to_string())
print("\n== eps_basic sanity: is it quarterly? RELIANCE ==")
print(con.execute("""select symbol,period_end,revenue,ebitda,profit_normalised,eps_basic,equity,debt_to_equity
 from financials where symbol in ('RELIANCE','TCS','TATASTEEL') and consolidated order by symbol,period_end""").df().to_string())
