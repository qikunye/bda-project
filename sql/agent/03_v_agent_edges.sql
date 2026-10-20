-- Label-free BQ2 edges for the agent (PLAN.md 5.1, F2).
--
-- * evaluation_is_laundering is deliberately not selected (it is in the base table).
-- * Partitions 1-13 Sep point at v1 files and 14-16 Sep at v2 files (F3); the data ends 16 Sep 23:59 (F6).
-- * Always filter on txn_date: an unfiltered transaction_id lookup scans about 0.9 GB.

CREATE OR REPLACE VIEW is459_aml_gold.v_agent_edges AS
SELECT
  transaction_id,
  transaction_timestamp,
  source_account_key,
  target_account_key,
  source_entity_id,
  source_entity_name,
  target_entity_id,
  target_entity_name,
  amount_paid,
  payment_currency,
  amount_received,
  receiving_currency,
  payment_format,
  is_cross_entity,
  is_cross_bank,
  is_cross_currency,
  txn_date
FROM is459_aml_gold.bq2_graph_edges_medium_v2;
