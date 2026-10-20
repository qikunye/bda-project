-- Label-free BQ1 scores for the agent (PLAN.md 5.1). The agent reads this view, never the base table.
--
-- * evaluation_is_laundering is deliberately not selected.
-- * risk_rank_global is the stored risk_rank (one rank over the whole dataset).
-- * risk_rank_day ranks only the test rows of each day. 14 Sep 00:00-05:37 is validation
--   (488,561 rows the model was tuned on), so those rows get risk_rank_day = NULL and stay
--   out of the daily queue. They still have a score, so they can appear as neighbours.
-- * Always filter on txn_date: it is the partition key and also bounds the window.

CREATE OR REPLACE VIEW is459_aml_gold.v_agent_scores AS
SELECT
  transaction_id,
  source_account_key,
  target_account_key,
  transaction_timestamp,
  risk_score,
  risk_rank AS risk_rank_global,
  CASE
    WHEN data_split = 'test'
    THEN row_number() OVER (PARTITION BY txn_date, data_split ORDER BY risk_rank, transaction_id)
  END AS risk_rank_day,
  data_split,
  model_name,
  model_version,
  txn_date
FROM is459_aml_gold.bq1_risk_scores_medium_v2;
