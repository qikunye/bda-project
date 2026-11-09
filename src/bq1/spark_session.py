"""Local Spark session with the BQ1 settings (T0.5). Not used by the Glue job, which supplies its own session."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def get_spark(app="bq1", driver_mem="8g"):
    os.environ.setdefault("JAVA_HOME", "/opt/homebrew/opt/openjdk@17")
    from pyspark.sql import SparkSession
    tmp = ROOT / "data" / "spark_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    return (SparkSession.builder.master("local[*]").appName(app)
            .config("spark.driver.memory", driver_mem)
            .config("spark.sql.session.timeZone", "UTC")
            .config("spark.sql.shuffle.partitions", "64")
            .config("spark.local.dir", str(tmp))
            .config("spark.ui.enabled", "false")
            .getOrCreate())
