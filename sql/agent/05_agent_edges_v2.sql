-- Materialised label-free edges for the agent's case graph (PLAN.md D13).
--
-- Each edge appears twice, once per endpoint:
--   direction = 'in'  : account_key received the payment (counterparty_key paid it)
--   direction = 'out' : account_key made the payment (counterparty_key received it)
-- Upstream of an alert = 'in' rows of the sender; downstream = 'out' rows of the receiver.
-- This is the same one-hop rule as the BQ2 case query (src/bq2/queries.py, PLAN.md F8).
--
-- Bucketed by account_key, so a lookup for one account reads one bucket per day.
-- Covers 11-16 Sep: enough for the largest window (72 h) before the earliest scored day (14 Sep).
-- No self-transfers exist in the source (F8), so no edge is double-counted for one account.
-- Rebuild the same way as agent_scores_v2 (drop table, delete the S3 prefix, rerun).

CREATE TABLE is459_aml_gold.agent_edges_v2
WITH (
  format = 'PARQUET',
  write_compression = 'SNAPPY',
  external_location = 's3://is459-aml-g1t5/agent/tables/agent_edges_v2/',
  partitioned_by = ARRAY['txn_date'],
  bucketed_by = ARRAY['account_key'],
  bucket_count = 32
) AS
SELECT
  target_account_key AS account_key,
  'in' AS direction,
  source_account_key AS counterparty_key,
  transaction_id,
  transaction_timestamp,
  amount_paid,
  payment_currency,
  payment_format,
  txn_date
FROM is459_aml_gold.v_agent_edges
WHERE txn_date BETWEEN DATE '2022-09-11' AND DATE '2022-09-16'
UNION ALL
SELECT
  source_account_key AS account_key,
  'out' AS direction,
  target_account_key AS counterparty_key,
  transaction_id,
  transaction_timestamp,
  amount_paid,
  payment_currency,
  payment_format,
  txn_date
FROM is459_aml_gold.v_agent_edges
WHERE txn_date BETWEEN DATE '2022-09-11' AND DATE '2022-09-16';
