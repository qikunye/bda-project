"""AWS Glue job: BQ1 feature engineering. Thin wrapper around bq1/features.py (no feature logic lives here).

Reads Silver transaction Parquet, calls build_features, writes Snappy Parquet partitioned by txn_date, and logs
input and output row counts. The job fails if the counts differ.

Job arguments
  --silver_path  Silver transactions Parquet directory, e.g.
                 s3://is459-aml-g1t5/silver/ibm-aml/versions/<run_id>/li_small/transactions/
  --out_path     Output directory; must be under .../gold/bq1/ (hard rule: BQ1 never writes to bronze/ or silver/)
  --variant      Feature set passed to build_features: "v1" (production, default) or "v2" (adds pass-through features)

Packaging: bq1/__init__.py and bq1/features.py are zipped as bq1.zip and supplied with
  --extra-py-files s3://is459-aml-g1t5/scripts/bq1/bq1.zip
Build the zip with scripts/package_glue.sh. Glue 5.0 = Spark 3.5, Python 3.11.
"""
import sys


def _get_args():
    names = ["silver_path", "out_path", "variant"]
    try:
        from awsglue.utils import getResolvedOptions
        wanted = [n for n in names if f"--{n}" in sys.argv]
        return getResolvedOptions(sys.argv, wanted)
    except ImportError:  # local run
        import argparse
        p = argparse.ArgumentParser()
        for n in names:
            p.add_argument(f"--{n}")
        return vars(p.parse_args())


def _check_paths(silver_path, out_path):
    if "/gold/bq1/" not in out_path:
        raise ValueError(f"out_path must be under gold/bq1/, got {out_path}")
    for bad in ("/bronze/", "/silver/"):
        if bad in out_path:
            raise ValueError(f"refusing to write under {bad}: {out_path}")
    if out_path.rstrip("/") == silver_path.rstrip("/"):
        raise ValueError("out_path must differ from silver_path")


def main():
    args = _get_args()
    silver_path, out_path = args["silver_path"], args["out_path"]
    variant = args.get("variant") or "v1"
    if variant not in ("v1", "v2"):
        raise ValueError(f"--variant must be v1 or v2, got {variant}")
    _check_paths(silver_path, out_path)

    from pyspark.sql import SparkSession, functions as F
    from bq1.features import build_features

    spark = SparkSession.builder.appName(f"bq1-features-{variant}").getOrCreate()
    spark.conf.set("spark.sql.session.timeZone", "UTC")  # txn_date is the UTC calendar date
    spark.conf.set("spark.sql.parquet.compression.codec", "snappy")

    silver = spark.read.parquet(silver_path)
    n_in = silver.count()
    print(f"[bq1] silver_path={silver_path} variant={variant} input_rows={n_in}", flush=True)

    feats = build_features(silver, variant=variant).withColumn("txn_date", F.to_date("timestamp"))
    # one task per txn_date -> one file per day instead of hundreds of tiny files
    (feats.repartition("txn_date")
          .write.mode("overwrite").partitionBy("txn_date").option("compression", "snappy").parquet(out_path))

    n_out = spark.read.parquet(out_path).count()
    print(f"[bq1] out_path={out_path} output_rows={n_out}", flush=True)
    if n_in != n_out:
        raise RuntimeError(f"row count mismatch: input {n_in} != output {n_out}")
    print("[bq1] row counts match", flush=True)


if __name__ == "__main__":
    main()
