import sys, time, shutil; sys.path.insert(0, ".")
import numpy as np, pyarrow.parquet as pq, pyarrow as pa
from src.bq1.spark_session import get_spark
from src.bq1.features import passthrough_features, V2_EXTRA_COLS
from pyspark.sql import functions as F
spark = get_spark()
SRC, V2, TRUNC = "data/silver/li_small/transactions/", "data/features/li_small_v2_extra/", "data/features/li_small_v2_extra_trunc/"
shutil.rmtree(V2, ignore_errors=True)
def narrow(df):
    return df.select("transaction_id", F.col("timestamp").cast("long").alias("ts"), "sender_account_key", "receiver_account_key")
silver = spark.read.parquet(SRC)
t0 = time.time()
passthrough_features(narrow(silver)).write.mode("overwrite").option("compression", "snappy").parquet(V2)
print(f"pass-through build {time.time()-t0:.0f}s", flush=True)
o2 = spark.read.parquet(V2)
print("rows", o2.count(), "distinct ids", o2.select("transaction_id").distinct().count(), flush=True)
# brute-force correctness check lives in scripts/t210_check.py (shared key encoding)

# truncation test on the new features
shutil.rmtree(TRUNC, ignore_errors=True)
cut = silver.select(F.col("timestamp").cast("long").alias("ts")).approxQuantile("ts", [0.5], 0.0)[0]
passthrough_features(narrow(silver.filter(F.col("timestamp").cast("long") <= cut))).write.mode("overwrite").parquet(TRUNC)
full = o2.join(narrow(silver).select("transaction_id", "ts"), "transaction_id").filter(F.col("ts") <= cut)
tr = spark.read.parquet(TRUNC)
j = full.alias("a").join(tr.alias("b"), "transaction_id").cache()
res = j.agg(*[F.sum((~F.col(f"a.{c}").eqNullSafe(F.col(f"b.{c}"))).cast("int")).alias(c) for c in V2_EXTRA_COLS]).first().asDict()
bad = {k: v for k, v in res.items() if v}
print("truncation test on", j.count(), "rows, mismatches:", bad if bad else "none (all features identical)")
shutil.rmtree(TRUNC, ignore_errors=True)
