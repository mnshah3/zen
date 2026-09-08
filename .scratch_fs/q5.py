import duckdb, pandas as pd
pd.set_option("display.width",250); pd.set_option("display.max_colwidth",120)
con = duckdb.connect()
con.execute("CREATE VIEW announcements AS SELECT * FROM read_parquet('data/announcements/**/*.parquet', union_by_name=true)")
print("== sample ratings subjects 2026 ==")
print(con.execute("select subject from announcements where category='ratings' and trade_date>=date '2026-01-01' limit 12").df().to_string())
print("\n== ratings subject upgrade/downgrade keyword hit rate ==")
print(con.execute("""select
 sum(case when lower(subject) like '%upgrad%' then 1 else 0 end) up,
 sum(case when lower(subject) like '%downgrad%' then 1 else 0 end) down,
 sum(case when lower(subject) like '%revis%' then 1 else 0 end) rev,
 count(*) tot from announcements where category='ratings'""").df().to_string())
print("\n== pledge subjects sample ==")
print(con.execute("select trade_date, subject from announcements where category='pledge' and trade_date>=date '2025-01-01' limit 10").df().to_string())
print("\n== shareholding pattern mentions anywhere ==")
print(con.execute("""select category, count(*) n, count(distinct symbol) s, max(trade_date) mx from announcements
 where lower(subject) like '%shareholding pattern%' or lower(subject) like '%shareholding%' group by 1 order by 2 desc""").df().to_string())
print("\n== 'encumbr' / pledge text anywhere ==")
print(con.execute("""select strftime(trade_date,'%Y') y, count(*) n, count(distinct symbol) s from announcements
 where lower(subject) like '%encumbr%' or lower(subject) like '%pledge%' group by 1 order by 1""").df().to_string())
