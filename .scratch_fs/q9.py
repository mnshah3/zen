import duckdb
con=duckdb.connect()
con.execute("CREATE VIEW announcements AS SELECT * FROM read_parquet('data/announcements/**/*.parquet', union_by_name=true)")
print("== exact NSE prefix 'Bagging/Receiving of orders/contracts' across ALL categories ==")
print(con.execute("""select category, count(*) n, count(distinct symbol) s, min(trade_date) mn, max(trade_date) mx
 from announcements where subject like 'Bagging/Receiving of orders/contracts%' group by 1 order by 2 desc""").df().to_string())
print("\n== by year ==")
print(con.execute("""select strftime(trade_date,'%Y') y, count(*) n, count(distinct symbol) s
 from announcements where subject like 'Bagging/Receiving of orders/contracts%' group by 1 order by 1""").df().to_string())
