-- BQ1 Athena queries for the UI owner (T8.2)
-- Table: is459_aml_gold.bq1_risk_scores_medium_v1   (region us-east-1, database is459_aml_gold)
-- How to run: paste ONE query at a time into the Athena query editor and edit only its `params` block (top of the query).
-- In the Athena console set the query result location to s3://is459-aml-g1t5/gold/bq1/athena-results/
--   (the default workgroup location belongs to another bucket).
--
-- =====================================================================================================
-- READ THIS FIRST: what the table holds and how to use risk_rank and data_split
-- =====================================================================================================
-- The table has ONE ROW PER TRANSACTION for ALL 31,251,483 LI-Medium transactions, including the excluded tail days.
--   risk_score  = the XGBoost model's score. Higher = riskier. Only the ORDER means anything (it is not a calibrated
--                 probability).
--   risk_rank   = a single global ordering of ALL rows: 1 = riskiest, 31,251,483 = least risky. It is correct as
--                 specified (dense, unique, ties broken by transaction_id), but it mixes four kinds of rows (below).
--   data_split  = which group the row belongs to. It tells you how much to trust the score for that row:
--
--     data_split   rows         what it is                                            fair for performance?
--     ----------   ----------   ---------------------------------------------------   ----------------------
--     train        18,748,707   1 to 9 Sep (first 60%). The model learned from these.   NO  (in-sample, looks too good)
--     validation    6,251,102   9 to 14 Sep. Used to stop training and pick settings.    NO  (seen during training)
--     test          6,250,304   14 to 16 Sep (last 20%). The model never saw them.       YES (this is the headline)
--     excluded          1,370   17 to 27 Sep tail days (a few hundred rows a day with    NO  (left out on purpose: tiny,
--                               a 56% laundering rate). Scored, but never used to        unlike normal days, and they
--                               train or to measure.                                     inflate every metric)
--
-- Why this matters: the plain global risk_rank sorts all four groups together. Train, validation and excluded rows
-- score very high (they are in-sample or easy), so they crowd the top: of the global top 1,000 only 157 (16%) are
-- test rows; 305 are excluded, 277 validation and 261 train. So:
--
--   If you want ...                                              Do this
--   -----------------------------------------------------------  --------------------------------------------------
--   a fair analyst queue, or any Recall / Precision number        filter data_split = 'test', then use queue_position
--                                                                (Queries 1 to 3 already do this)
--   the riskiest rows across the whole dataset (a demo browse)     use the plain global risk_rank (fine, but do not
--                                                                present it as model performance)
--   to look at the tail days on their own                         filter data_split = 'excluded' (set in Query 1)
--
-- Other reading rules:
--   * queue_position (Query 1) is the row's place INSIDE the filtered list (1 = riskiest of what you filtered).
--     risk_rank is its place among ALL rows. They differ whenever a filter is applied.
--   * evaluation_is_laundering is the label. It is for EVALUATION ONLY: show it in a back-office check, never feed it
--     to a model or present it as something an analyst would know in advance.
--   * Recall@K = share of all laundering-labelled test rows found in the top K% of the test queue.
--     Precision@K = share of the reviewed alerts that are laundering-labelled. Precision is low because laundering is
--     rare (0.048% of test rows), so a perfect top-1% queue would still only reach about 5%.
--   * Data is synthetic. Do not quote any number as a real-world laundering rate.


-- =====================================================================================================
-- QUERY 1. Top-N alert queue with filters
-- Returns the N riskiest rows that pass the filters, in review order. queue_position counts within the filtered set.
-- Settings: split ('test' | 'validation' | 'train' | 'excluded'), date range (UTC txn_date), minimum score,
--           optional account key (matches sender OR receiver; NULL = no account filter), top_n.
-- Why the default split is 'test': it is the only group the model has not seen, so this is the queue an analyst
-- would really get. Pick another split only to inspect it (for example 'excluded' for the tail days).
-- risk_rank is the row's place among ALL rows; queue_position is its place inside this filtered list.
-- Note the date range must lie inside the split's dates (test = 14 to 16 Sep) or the list will be empty.
-- =====================================================================================================
WITH params AS (
  SELECT
    'test'                    AS split,
    DATE '2022-09-14'         AS date_from,
    DATE '2022-09-16'         AS date_to,
    CAST(0.0 AS double)       AS min_score,
    CAST(NULL AS varchar)     AS account_key,      -- e.g. '120075|81294D7A0'
    100                       AS top_n
),
filtered AS (
  SELECT
    s.risk_rank,
    s.transaction_id,
    s.source_account_key,
    s.target_account_key,
    s.transaction_timestamp,
    s.risk_score,
    s.data_split,
    s.evaluation_is_laundering,
    row_number() OVER (ORDER BY s.risk_rank) AS queue_position
  FROM is459_aml_gold.bq1_risk_scores_medium_v1 s
  CROSS JOIN params p
  WHERE s.data_split = p.split
    AND s.txn_date BETWEEN p.date_from AND p.date_to
    AND s.risk_score >= p.min_score
    AND (p.account_key IS NULL OR s.source_account_key = p.account_key OR s.target_account_key = p.account_key)
)
SELECT queue_position, risk_rank, transaction_id, source_account_key, target_account_key,
       transaction_timestamp, risk_score, data_split, evaluation_is_laundering
FROM filtered
WHERE queue_position <= (SELECT top_n FROM params)
ORDER BY queue_position;


-- =====================================================================================================
-- QUERY 2. Recall@K and Precision@K at a chosen review budget (TEST split only)
-- Give either a share of rows (budget_share, e.g. 0.01 = review the top 1%) or a fixed number of alerts.
-- Edit the VALUES list to compare several budgets at once: (label, budget_share, budget_alerts), with the
-- unused one NULL. K is a share of the TEST rows. Rank rule: risk_rank (score descending, ties by transaction_id).
-- Why test only: recall and precision are only honest on rows the model has not seen. Both are measured against
-- the 3,013 laundering-labelled test rows. Example: a 1% budget = review 62,504 rows and find 1,911 of the 3,013 (63.4%).
-- Columns: alerts_reviewed, tp, recall, precision, alert_reduction (= 1 - K), fp_per_tp.
-- =====================================================================================================
WITH budgets(label, budget_share, budget_alerts) AS (
  VALUES ('0.1%',    CAST(0.001 AS double), CAST(NULL AS bigint)),
         ('0.5%',    CAST(0.005 AS double), CAST(NULL AS bigint)),
         ('1%',      CAST(0.01  AS double), CAST(NULL AS bigint)),
         ('5%',      CAST(0.05  AS double), CAST(NULL AS bigint)),
         ('10%',     CAST(0.10  AS double), CAST(NULL AS bigint)),
         ('5,000 alerts', CAST(NULL AS double), CAST(5000 AS bigint))
),
test_rows AS (
  SELECT evaluation_is_laundering AS y,
         row_number() OVER (ORDER BY risk_rank) AS pos
  FROM is459_aml_gold.bq1_risk_scores_medium_v1
  WHERE data_split = 'test'
),
totals AS (SELECT count(*) AS n, sum(y) AS positives FROM test_rows),
sized AS (
  SELECT b.label,
         CASE WHEN b.budget_alerts IS NOT NULL THEN b.budget_alerts
              ELSE CAST(ceil(b.budget_share * t.n) AS bigint) END AS m,
         t.n, t.positives
  FROM budgets b CROSS JOIN totals t
)
SELECT z.label,
       z.m                                                  AS alerts_reviewed,
       sum(CASE WHEN r.pos <= z.m THEN r.y ELSE 0 END)      AS tp,
       CAST(sum(CASE WHEN r.pos <= z.m THEN r.y ELSE 0 END) AS double) / z.positives AS recall,
       CAST(sum(CASE WHEN r.pos <= z.m THEN r.y ELSE 0 END) AS double) / z.m         AS precision,
       1.0 - CAST(z.m AS double) / z.n                      AS alert_reduction,
       CAST(z.m - sum(CASE WHEN r.pos <= z.m THEN r.y ELSE 0 END) AS double)
         / NULLIF(sum(CASE WHEN r.pos <= z.m THEN r.y ELSE 0 END), 0)               AS fp_per_tp,
       z.n AS test_rows, z.positives AS test_positives
FROM sized z CROSS JOIN test_rows r
GROUP BY z.label, z.m, z.n, z.positives
ORDER BY z.m;


-- =====================================================================================================
-- QUERY 3. Workload-versus-detection curve (TEST split only)
-- 200 review budgets, geometric from 0.01% to 20% of the test rows. For each: how many alerts, what share of
-- laundering-labelled rows they catch (recall), how clean the queue is (precision), and the random-ranking recall
-- (= K) for comparison. Plot recall (y) against alert_share (x, log axis) or against alerts_reviewed.
-- Why test only: same reason as Query 2. The further the curve sits above the random line (recall = K), the more
-- review work the model saves. Read it as: "if analysts review this many alerts, they catch this share of laundering".
-- =====================================================================================================
WITH test_rows AS (
  SELECT evaluation_is_laundering AS y,
         row_number() OVER (ORDER BY risk_rank) AS pos,
         sum(evaluation_is_laundering) OVER (ORDER BY risk_rank ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS cum_tp
  FROM is459_aml_gold.bq1_risk_scores_medium_v1
  WHERE data_split = 'test'
),
totals AS (SELECT count(*) AS n, sum(y) AS positives FROM test_rows),
grid AS (
  SELECT i,
         0.0001 * power(2000.0, i / 199.0) AS k      -- 0.01% (i = 0) to 20% (i = 199)
  FROM UNNEST(sequence(0, 199)) AS u(i)
),
sized AS (
  SELECT g.i, g.k, CAST(ceil(g.k * t.n) AS bigint) AS m, t.n, t.positives
  FROM grid g CROSS JOIN totals t
)
SELECT z.i                                          AS step,
       z.k                                          AS alert_share,
       z.m                                          AS alerts_reviewed,
       r.cum_tp                                     AS tp,
       CAST(r.cum_tp AS double) / z.positives       AS recall,
       CAST(r.cum_tp AS double) / z.m               AS precision,
       z.k                                          AS random_recall,
       1.0 - z.k                                    AS alert_reduction
FROM sized z
JOIN test_rows r ON r.pos = z.m
ORDER BY z.i;


-- =====================================================================================================
-- Checked 2026-10-10 against known results (all run in Athena, workgroup primary):
--   Query 2 reproduces the T7.5 test table exactly (Recall@1% 0.6343, Recall@0.1% 0.3024, 5,000 alerts: 846 tp).
--   Query 3 matches models/bq1/xgboost/v1/curve_medium.csv (same 200 alert counts, recall diff about 1e-16).
--   Query 1 returns rows in risk_rank order with queue_position 1..N; the account filter finds the BQ2 demo alert
--   txn_630dea1dda9482dbd765a966355d6708 at global rank 6,142 (train split).
-- Cost: Query 1 scans about 0.4 GB, Queries 2 and 3 about 40 MB each; a few seconds each.
-- =====================================================================================================
