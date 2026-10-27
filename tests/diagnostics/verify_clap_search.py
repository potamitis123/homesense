"""Reproduce CLAP search and print the failure clearly."""
import os
import sys
import traceback
from datetime import datetime, timedelta

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

from ast_dash.sql_to_pandas import SQL_to_Pandas, convert_array

end = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
start = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d %H:%M:%S")
print(f"window {start} .. {end}")

s = SQL_to_Pandas()
df = s.get_date_from_to(start, end)
print(f"rows={len(df)} cols={list(df.columns)}")
if "clap_embedding" not in df.columns:
    print("FAIL: clap_embedding column missing")
    sys.exit(1)

n = int(df["clap_embedding"].notna().sum())
print(f"non-null embeddings={n}")
if n == 0:
    print("FAIL: no embeddings in date window")
    # peek whole table count
    import sqlite3
    from misc.load_parameters import parameters
    db = os.path.join(ROOT, parameters.get_parameters()["sqlite"]["db_name"])
    conn = sqlite3.connect(db)
    total = conn.execute("SELECT COUNT(*) FROM ast_table").fetchone()[0]
    with_emb = conn.execute(
        "SELECT COUNT(*) FROM ast_table WHERE clap_embedding IS NOT NULL"
    ).fetchone()[0]
    print(f"db total={total} with_embedding={with_emb}")
    conn.close()
    sys.exit(1)

sample = df["clap_embedding"].dropna().iloc[0]
print(f"sample type={type(sample)} nbytes={len(sample) if hasattr(sample,'__len__') else '?'}")
try:
    arr = convert_array(sample)
    print(f"converted shape={arr.shape} dtype={arr.dtype}")
except Exception:
    traceback.print_exc()

print("\nrunning search for 'speech' threshold=0.1 ...")
try:
    res = s.get_clap_search_results("speech", 0.1, start, end)
    print(f"results={len(res)}")
    if len(res):
        cols = [c for c in ("Timestamp", "date", "Device", "device_name", "similarity") if c in res.columns]
        print(res[cols].head(5).to_string())
    else:
        print("empty result set")
except Exception:
    traceback.print_exc()
    sys.exit(1)
