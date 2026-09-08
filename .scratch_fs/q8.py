import duckdb, pandas as pd
con=duckdb.connect()
con.execute("CREATE VIEW announcements AS SELECT * FROM read_parquet('data/announcements/**/*.parquet', union_by_name=true)")
tot=con.execute("select count(*) from announcements where category='orders'").fetchone()[0]
for lbl,pat in [("bag/win/award/LOA","lower(subject) similar to '.*(bagg|bags |wins |won |letter of award|letter of intent|work order|purchase order|receipt of order|secures|awarded).*'"),
                ("court/NCLT/SEBI order","(lower(subject) like '%nclt%' or lower(subject) like '%sebi order%' or lower(subject) like '%court%' or lower(subject) like '%tribunal%')"),
                ("tender offer / open offer","(lower(subject) like '%tender offer%' or lower(subject) like '%open offer%')"),
                ("contract labour/employment","(lower(subject) like '%contract labour%' or lower(subject) like '%contractual%')")]:
    n=con.execute(f"select count(*) from announcements where category='orders' and {pat}").fetchone()[0]
    print(f"{lbl:28s} {n:6d}  {100*n/tot:5.1f}%")
print("total orders",tot)
print("\nsample 'orders' subjects:")
for s in con.execute("select subject from announcements where category='orders' order by random() limit 8").df().subject: print(" -",s[:150])
