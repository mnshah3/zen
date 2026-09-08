import duckdb, pandas as pd
pd.set_option("display.width",220); pd.set_option("display.max_rows",100)
con = duckdb.connect()
con.execute("CREATE VIEW announcements AS SELECT * FROM read_parquet('data/announcements/**/*.parquet', union_by_name=true)")
print("== industry non-null ==")
print(con.execute("select count(*) tot, count(industry) nn, count(distinct symbol) syms, count(distinct case when industry is not null then symbol end) syms_ind from announcements").df().to_string())
print("\n== top industries (by distinct symbol) ==")
print(con.execute("select industry, count(distinct symbol) syms from announcements where industry is not null group by 1 order by 2 desc limit 45").df().to_string())
print("\n== results announcements by month, 2025-11 onward ==")
print(con.execute("""select strftime(trade_date,'%Y-%m') m, count(*) n, count(distinct symbol) syms,
 sum(case when has_xbrl then 1 else 0 end) xbrl
 from announcements where category='results' and trade_date >= date '2025-10-01' group by 1 order by 1""").df().to_string())
print("\n== pledge/ratings recency ==")
print(con.execute("""select category, strftime(trade_date,'%Y') y, count(*) n from announcements
 where category in ('pledge','ratings') group by 1,2 order by 1,2""").df().to_string())
