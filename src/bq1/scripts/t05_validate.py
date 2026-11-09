import sys; sys.path.insert(0, ".")
from src.bq1.spark_session import get_spark
from pyspark.sql import functions as F
spark = get_spark()
df = spark.read.parquet("data/silver/li_small/transactions/")
r = df.agg(F.count("*").alias("n"), F.sum("is_laundering").alias("pos"),
           F.countDistinct("transaction_id").alias("d")).first()
print(r, "| rows ok:", r.n == 6924049, "| pos ok:", r.pos == 3565, "| ids unique:", r.d == r.n)
