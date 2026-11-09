"""Parity: a Glue-job output dir (arg 1) vs the original local v1 features. All rows, all features."""
import sys, numpy as np, pandas as pd; sys.path.insert(0, ".")
from src.bq1.features import FEATURE_COLS
a = pd.read_parquet("data/features/li_small_v1").set_index("transaction_id").sort_index()
GLUE = sys.argv[1] if len(sys.argv) > 1 else "data/gold/bq1/features/li-small-v1/"
b = pd.read_parquet(GLUE).set_index("transaction_id").sort_index()
print("rows", len(a), len(b), "| same ids:", a.index.equals(b.index), "| txn_date present:", "txn_date" in b.columns)
print("txn_date dates:", sorted(b["txn_date"].astype(str).unique())[:3], "...", b["txn_date"].nunique(), "days")
bad = {}
for c in ["ts", "is_laundering", "sender_account_key", "receiver_account_key"] + FEATURE_COLS:
    x, y = a[c], b[c]
    if x.dtype.kind == "f" or y.dtype.kind == "f":
        x, y = x.astype("float64"), y.astype("float64")
        same = (x.isna() & y.isna()) | ((x - y).abs() <= 1e-9 * np.maximum(np.maximum(x.abs(), y.abs()), 1.0))
    elif c == "timestamp":
        same = x == y
    else:
        same = (x.astype(str) == y.astype(str))
    n = int((~same).sum())
    if n: bad[c] = n
print("columns with mismatches:", bad if bad else "none - all feature values identical across all rows")
