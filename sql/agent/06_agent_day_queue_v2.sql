-- The top 5,000 alerts of each scored day (PLAN.md D13), for get_related_alerts, case grouping,
-- the batch runner and the Streamlit queue. About 15k rows, so reading a whole day costs < 1 MB.
-- agent_scores_v2 is bucketed by transaction_id, which scatters top ranks across every file, so a
-- "risk_rank_day <= K" query on it reads the whole day (about 225 MB).
-- 5,000 covers every review budget in the evaluation (K = 100, 500, 1,000) and the baseline's R2.
-- Rebuild together with agent_scores_v2 (drop table, delete the S3 prefix, rerun).

CREATE TABLE is459_aml_gold.agent_day_queue_v2
WITH (
  format = 'PARQUET',
  write_compression = 'SNAPPY',
  external_location = 's3://is459-aml-g1t5/agent/tables/agent_day_queue_v2/',
  partitioned_by = ARRAY['txn_date']
) AS
SELECT
  transaction_id,
  source_account_key,
  target_account_key,
  transaction_timestamp,
  risk_score,
  risk_rank_global,
  risk_rank_day,
  feature_1 IS NOT NULL AS has_reasons,
  txn_date
FROM is459_aml_gold.agent_scores_v2
WHERE risk_rank_day <= 5000;
