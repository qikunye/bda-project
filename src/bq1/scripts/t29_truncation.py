import sys, time, shutil; sys.path.insert(0, ".")
from src.bq1.spark_session import get_spark
from src.bq1.features import build_features, FEATURE_COLS
from pyspark.sql import functions as F
spark = get_spark()
SRC, OUT, TRUNC = "data/silver/li_small/transactions/", "data/features/li_small_v1/", "data/features/li_small_v1_trunc/"
shutil.rmtree(TRUNC, ignore_errors=True)
silver = spark.read.parquet(SRC)
cut = silver.select(F.col("timestamp").cast("long").alias("ts")).approxQuantile("ts", [0.5], 0.0)[0]
t0 = time.time()
build_features(silver.filter(F.col("timestamp").cast("long") <= cut)).write.mode("overwrite").parquet(TRUNC)
print(f"truncated rebuild {time.time()-t0:.0f}s cutoff ts={cut}")
full = spark.read.parquet(OUT).filter(F.col("ts") <= cut); tr = spark.read.parquet(TRUNC)
print("rows full-subset", full.count(), "truncated", tr.count())
j = full.alias("a").join(tr.alias("b"), "transaction_id").cache()
print("joined", j.count())
FLOATS = {"snd_out_amt_24h_ccy", "rcv_in_amt_24h_ccy", "snd_prior_mean_amt_ccy", "amt_ratio_to_snd_mean", "log_amount_paid"}
exprs = []
for c in FEATURE_COLS:
    a, b = F.col(f"a.{c}"), F.col(f"b.{c}")
    same = ((a.isNull() & b.isNull()) | (F.abs(a - b) <= 1e-9 * F.greatest(F.abs(a), F.abs(b), F.lit(1.0)))) if c in FLOATS else a.eqNullSafe(b)
    exprs.append(F.sum((~same).cast("int")).alias(c))
res = j.agg(*exprs).first().asDict()
bad = {k: v for k, v in res.items() if v}
print("T2.9 mismatching rows per feature:", bad if bad else "none (all features identical)")
shutil.rmtree(TRUNC, ignore_errors=True)
