"""build_features(df) -> df. Pure PySpark, no I/O.

All rolling/to-date windows are causal: a row at time T sees only rows with ts < T
(rangeBetween(..., -1) on minute-resolution epoch seconds, so same-minute rows are excluded).
`is_laundering` is carried through as a label column and is never used to build a feature.
Time features derive from `ts` (epoch seconds, UTC) so they do not depend on the session timezone.
"""
from pyspark.sql import DataFrame, Window, functions as F

HOUR, DAY = 3600, 86400

PASSTHROUGH_CATEGORICAL = ["payment_format", "payment_currency", "receiving_currency"]
SILVER_FLAGS = ["is_same_account", "is_same_entity", "is_same_entity_different_account",
                "is_cross_entity", "is_cross_bank", "is_cross_currency"]
KEY_COLS = ["transaction_id", "timestamp", "ts", "sender_account_key", "receiver_account_key", "is_laundering"]
FEATURE_COLS = (
    ["hour_of_day", "day_of_week", "log_amount_paid"] + PASSTHROUGH_CATEGORICAL + SILVER_FLAGS
    + ["snd_out_cnt_1h", "snd_out_cnt_24h", "snd_out_amt_24h_ccy",
       "rcv_in_cnt_1h", "rcv_in_cnt_24h", "rcv_in_amt_24h_ccy",
       "pair_prior_cnt", "is_new_pair", "snd_distinct_rcv_to_date", "rcv_distinct_snd_to_date",
       "snd_prior_txn_cnt", "snd_prior_mean_amt_ccy", "amt_ratio_to_snd_mean", "snd_secs_since_last_out"]
)


# v2 (T2.10): pass-through features from a long "account events" table (one 'out' event for the sender and
# one 'in' event for the receiver of every transaction).
V2_EXTRA_COLS = ["snd_in_cnt_24h", "snd_secs_since_last_in", "rcv_out_cnt_24h"]
FEATURE_COLS_V2 = FEATURE_COLS + V2_EXTRA_COLS


def feature_cols(variant="v1"):
    return FEATURE_COLS_V2 if variant == "v2" else FEATURE_COLS


def _roll(key_cols, secs):
    return Window.partitionBy(*key_cols).orderBy("ts").rangeBetween(-secs, -1)


def _to_date(key_cols):
    return Window.partitionBy(*key_cols).orderBy("ts").rangeBetween(Window.unboundedPreceding, -1)


def build_features(df: DataFrame, variant: str = "v1") -> DataFrame:
    snd, rcv = ["sender_account_key"], ["receiver_account_key"]
    snd_ccy, rcv_ccy = snd + ["payment_currency"], rcv + ["receiving_currency"]
    pair = snd + rcv
    one = F.lit(1)

    d = (df.withColumn("ts", F.col("timestamp").cast("long"))
           .withColumn("amount_paid", F.col("amount_paid").cast("double"))
           .withColumn("amount_received", F.col("amount_received").cast("double")))

    # T2.1 transaction features
    d = (d.withColumn("hour_of_day", ((F.col("ts") % DAY) / HOUR).cast("int"))
          .withColumn("day_of_week", (((F.col("ts") / DAY).cast("long") + 3) % 7).cast("int"))  # Monday = 0
          .withColumn("log_amount_paid", F.log1p("amount_paid")))

    # T2.2 sender velocity
    d = (d.withColumn("snd_out_cnt_1h", F.count(one).over(_roll(snd, HOUR)))
          .withColumn("snd_out_cnt_24h", F.count(one).over(_roll(snd, DAY)))
          .withColumn("snd_out_amt_24h_ccy",
                      F.coalesce(F.sum("amount_paid").over(_roll(snd_ccy, DAY)), F.lit(0.0))))

    # T2.3 receiver velocity
    d = (d.withColumn("rcv_in_cnt_1h", F.count(one).over(_roll(rcv, HOUR)))
          .withColumn("rcv_in_cnt_24h", F.count(one).over(_roll(rcv, DAY)))
          .withColumn("rcv_in_amt_24h_ccy",
                      F.coalesce(F.sum("amount_received").over(_roll(rcv_ccy, DAY)), F.lit(0.0))))

    # T2.4 counterparty
    first_in_pair = F.row_number().over(Window.partitionBy(*pair).orderBy("ts", "transaction_id")) == 1
    d = (d.withColumn("pair_prior_cnt", F.count(one).over(_to_date(pair)))
          .withColumn("is_new_pair", F.col("pair_prior_cnt") == 0)
          .withColumn("_is_first_pair_row", first_in_pair.cast("int"))
          .withColumn("snd_distinct_rcv_to_date",
                      F.coalesce(F.sum("_is_first_pair_row").over(_to_date(snd)), F.lit(0)))
          .withColumn("rcv_distinct_snd_to_date",
                      F.coalesce(F.sum("_is_first_pair_row").over(_to_date(rcv)), F.lit(0))))

    # T2.5 behavioural deviation
    d = (d.withColumn("snd_prior_txn_cnt", F.count(one).over(_to_date(snd)))
          .withColumn("snd_prior_mean_amt_ccy", F.avg("amount_paid").over(_to_date(snd_ccy)))
          .withColumn("amt_ratio_to_snd_mean",
                      F.when(F.col("snd_prior_mean_amt_ccy") > 0,
                             F.col("amount_paid") / F.col("snd_prior_mean_amt_ccy")))
          .withColumn("snd_secs_since_last_out",
                      F.col("ts") - F.max("ts").over(_to_date(snd))))

    # T2.6 output contract
    base = d.select(*KEY_COLS, *FEATURE_COLS)
    if variant == "v1":
        return base
    if variant == "v2":
        return _add_passthrough(base)
    raise ValueError(f"unknown variant {variant!r}")


def passthrough_features(narrow: DataFrame) -> DataFrame:
    """T2.10: how much the sender recently RECEIVED and how much the receiver recently SENT. Causal (ts strictly
    earlier). A sender that was just paid and now pays on is a pass-through pattern.
    Input needs only: transaction_id, ts, sender_account_key, receiver_account_key (narrow, so the shuffle is small).
    Output: transaction_id + V2_EXTRA_COLS."""
    out_ev = narrow.select("transaction_id", "ts", F.col("sender_account_key").alias("acct"), F.lit(0).alias("is_in"))
    in_ev = narrow.select("transaction_id", "ts", F.col("receiver_account_key").alias("acct"), F.lit(1).alias("is_in"))
    ev = out_ev.unionByName(in_ev)
    w24 = _roll(["acct"], DAY)
    w_all = _to_date(["acct"])
    ev = (ev.withColumn("_in24", F.sum("is_in").over(w24))
            .withColumn("_out24", F.sum(1 - F.col("is_in")).over(w24))
            .withColumn("_last_in", F.max(F.when(F.col("is_in") == 1, F.col("ts"))).over(w_all)))
    snd_side = (ev.filter(F.col("is_in") == 0)
                  .select("transaction_id",
                          F.coalesce("_in24", F.lit(0)).cast("long").alias("snd_in_cnt_24h"),
                          (F.col("ts") - F.col("_last_in")).alias("snd_secs_since_last_in")))
    rcv_side = (ev.filter(F.col("is_in") == 1)
                  .select("transaction_id", F.coalesce("_out24", F.lit(0)).cast("long").alias("rcv_out_cnt_24h")))
    return snd_side.join(rcv_side, "transaction_id", "inner")


def _add_passthrough(base: DataFrame) -> DataFrame:
    narrow = base.select("transaction_id", "ts", "sender_account_key", "receiver_account_key")
    return base.join(passthrough_features(narrow), "transaction_id", "left")
