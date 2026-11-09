-- Epic 9 / T9.7: read-only external table over the LI-Medium Gold features (metadata only; no data copied).
-- The `timestamp` column is left out on purpose (parquet columns are matched by name); `ts` is the epoch seconds.
CREATE EXTERNAL TABLE IF NOT EXISTS is459_aml_gold.bq1_features_medium_v1 (
  transaction_id string, ts bigint, sender_account_key string, receiver_account_key string, is_laundering int,
  hour_of_day int, day_of_week int, log_amount_paid double,
  payment_format string, payment_currency string, receiving_currency string,
  is_same_account boolean, is_same_entity boolean, is_same_entity_different_account boolean,
  is_cross_entity boolean, is_cross_bank boolean, is_cross_currency boolean,
  snd_out_cnt_1h bigint, snd_out_cnt_24h bigint, snd_out_amt_24h_ccy double,
  rcv_in_cnt_1h bigint, rcv_in_cnt_24h bigint, rcv_in_amt_24h_ccy double,
  pair_prior_cnt bigint, is_new_pair boolean, snd_distinct_rcv_to_date bigint, rcv_distinct_snd_to_date bigint,
  snd_prior_txn_cnt bigint, snd_prior_mean_amt_ccy double, amt_ratio_to_snd_mean double, snd_secs_since_last_out bigint)
PARTITIONED BY (txn_date date)
STORED AS PARQUET
LOCATION 's3://is459-aml-g1t5/gold/bq1/features/li-medium-v1/';
