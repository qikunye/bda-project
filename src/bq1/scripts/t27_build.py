import sys, time, shutil; sys.path.insert(0, ".")
from src.bq1.spark_session import get_spark
from src.bq1.features import build_features
from pyspark.sql import functions as F
spark = get_spark()
SRC, OUT = "data/silver/li_small/transactions/", "data/features/li_small_v1/"
shutil.rmtree(OUT, ignore_errors=True)
silver = spark.read.parquet(SRC)
t0 = time.time()
build_features(silver).write.mode("overwrite").option("compression", "snappy").parquet(OUT)
rt = time.time() - t0
out = spark.read.parquet(OUT)
n_in, n_out = silver.count(), out.count()
d_in, d_out = silver.select("transaction_id").distinct().count(), out.select("transaction_id").distinct().count()
print(f"runtime {rt:.0f}s | rows in {n_in} out {n_out} | distinct in {d_in} out {d_out}")
print("rows match:", n_in == n_out, "| ids match:", d_in == d_out == n_out)
