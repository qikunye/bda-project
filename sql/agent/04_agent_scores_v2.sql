-- Materialised label-free alert table for the agent (docs/architecture/README.md).
--
-- One row per scored transaction, with what get_alert and get_neighbour_scores need in one place:
-- score, global and per-day rank (test rows only), payment details, and BQ1 reasons (top 1% only,
-- NULL otherwise). Built from the label-free views, so it never contains the label or data_split.
--
-- Bucketed by transaction_id: a lookup by ID reads one bucket (about 1 MB instead of 60-100 MB).
-- Athena cannot INSERT INTO a bucketed table, so adding a day means rebuilding it:
--   DROP TABLE is459_aml_gold.agent_scores_v2;
--   aws s3 rm --recursive s3://is459-aml-g1t5/agent/tables/agent_scores_v2/
--   then rerun this statement with the new date range (about 0.5 GB scanned).

CREATE TABLE is459_aml_gold.agent_scores_v2
WITH (
  format = 'PARQUET',
  write_compression = 'SNAPPY',
  external_location = 's3://is459-aml-g1t5/agent/tables/agent_scores_v2/',
  partitioned_by = ARRAY['txn_date'],
  bucketed_by = ARRAY['transaction_id'],
  bucket_count = 32
) AS
SELECT
  s.transaction_id,
  s.source_account_key,
  s.target_account_key,
  s.transaction_timestamp,
  s.risk_score,
  s.risk_rank_global,
  s.risk_rank_day,
  e.amount_paid,
  e.payment_currency,
  e.payment_format,
  r.feature_1,
  r.reason_1,
  CAST(r.contribution_1 AS double) AS contribution_1,
  r.feature_2,
  r.reason_2,
  CAST(r.contribution_2 AS double) AS contribution_2,
  r.feature_3,
  r.reason_3,
  CAST(r.contribution_3 AS double) AS contribution_3,
  s.txn_date
FROM is459_aml_gold.v_agent_scores s
-- The edge date filter stays in ON: self-transfers are scored but have no edge (F8), and a WHERE
-- filter on e would silently drop them.
LEFT JOIN is459_aml_gold.v_agent_edges e
  ON e.transaction_id = s.transaction_id
 AND e.txn_date = s.txn_date
 AND e.txn_date BETWEEN DATE '2022-09-14' AND DATE '2022-09-16'
LEFT JOIN is459_aml_gold.bq1_explanations_top1pct_v2 r
  ON r.transaction_id = s.transaction_id
WHERE s.txn_date BETWEEN DATE '2022-09-14' AND DATE '2022-09-16';
