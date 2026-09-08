import duckdb, pandas as pd
con = duckdb.connect()
con.execute("CREATE VIEW financials AS SELECT * FROM read_parquet('data/financials/*.parquet', union_by_name=true)")
con.execute("CREATE VIEW prices AS SELECT * FROM read_parquet('data/daily/**/*.parquet', union_by_name=true)")
con.execute("CREATE VIEW announcements AS SELECT * FROM read_parquet('data/announcements/**/*.parquet', union_by_name=true)")
con.execute("CREATE VIEW indices AS SELECT * FROM read_parquet('data/indices/*.parquet', union_by_name=true)")

print("== consolidated coverage: symbols with N of the 3 quarters ==")
print(con.execute("""select nq, count(*) from (
  select symbol, count(distinct period_end) nq from financials where consolidated group by 1) group by 1 order by 1""").df().to_string())
print("\n== symbols with BOTH Mar25 and Sep25 balance sheet (consol) ==")
print(con.execute("""select count(*) from (
 select symbol from financials where consolidated and has_balance_sheet
 and period_end in (date '2025-03-31', date '2025-09-30') group by 1 having count(*)=2)""").fetchone())
print("\n== same, standalone ==")
print(con.execute("""select count(*) from (
 select symbol from financials where not consolidated and has_balance_sheet
 and period_end in (date '2025-03-31', date '2025-09-30') group by 1 having count(*)=2)""").fetchone())

print("\n== PRICES schema ==")
print(con.execute("describe prices").df().to_string())
print("\n== price recency ==")
print(con.execute("select min(date), max(date), count(*), count(distinct symbol) from prices").df().to_string())
print(con.execute("select date, count(distinct symbol) n from prices where date > date '2026-08-20' group by 1 order by 1").df().to_string())
