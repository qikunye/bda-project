# BQ1 Handover: Alert Prioritisation (transaction risk scoring)

Written 2026-10-10 by the BQ1 owner (AML team g1t5). All data is synthetic (IBM AML dataset): do not quote any number here as a real-world laundering rate.

**In one paragraph.** BQ1 gives every LI-Medium transaction a risk score and a rank, so analysts can review the riskiest transactions first instead of everything. On the held-out test days, reviewing just the top 1% of transactions finds about 63% of the laundering-labelled ones (random picking would find 1%), and reviewing the top 10% finds 93%. The scores are ready to use in Athena now.

## 1. What you can use

| Item | Value |
| --- | --- |
| Table (Athena, us-east-1) | `is459_aml_gold.bq1_risk_scores_medium_v1` |
| Data | `s3://is459-aml-g1t5/gold/bq1/scores/li-medium-v1/` (27 daily partitions, 1.7 GiB) |
| Rows | 31,251,483, one per LI-Medium transaction. Join key: `transaction_id`. |
| Ready-made queries | `docs/BQ1_athena_queries.sql` (top-N alert queue with filters, Recall/Precision at a review budget, workload-versus-detection curve). Edit only the `params` block at the top of each. |

**Columns**

| Column | Meaning |
| --- | --- |
| `transaction_id` | Same id as Silver. Joins to BQ2's `bq2_graph_edges_medium_v1`: 28,752,132 of the rows match; the other 2,499,351 are self-account transactions that BQ2 leaves out. |
| `source_account_key`, `target_account_key` | Sender and receiver, in the Silver account-key format |
| `transaction_timestamp` | Minute resolution, UTC |
| `risk_score` | Model score. Higher is riskier. Only the order matters: it is not a calibrated probability. |
| `risk_rank` | 1 = riskiest. One ranking across all rows, with no gaps or duplicates. |
| `model_name`, `model_version` | `xgboost`, `v1` |
| `data_split` | `train`, `validation`, `test` or `excluded` (see section 3) |
| `evaluation_is_laundering` | The label. For checking results only, never as an input. |

The table is partitioned by `txn_date`.

## 2. What was done

1. **Started from** the Silver LI-Medium transactions (31.25M rows, 16,041 laundering-labelled). Everything was first built and tested on the smaller LI-Small dataset.
2. **Built 26 features per transaction** with an AWS Glue job. They describe the transaction (hour, day, amount, payment format and currency), how active the sender and receiver have been in the last hour and day, whether this sender and receiver have paid each other before, and how unusual the amount is against the sender's own history. Every feature uses only earlier transactions, so nothing from the future leaks in. The label is never used to build a feature. The definitions are in the table below.
3. **Split by time, not at random:** the first 60% of rows to train, the next 20% to tune, and the last 20% to test, so the test days are genuinely unseen.
4. **Trained three models and compared them on the test days:** a simple rules baseline, logistic regression and XGBoost. XGBoost was clearly best and was selected.
5. **Scored all 31.25M transactions** with XGBoost and wrote the Gold table above, then checked it: the row count, the ranks, and the join to BQ2 all match expectations, and the BQ2 demo alert `txn_630dea1dda9482dbd765a966355d6708` is present with the same sender, receiver and timestamp.

### Feature definitions

Features are computed per transaction in time order. "Earlier" always means strictly before this transaction's minute, so same-minute transactions are not counted. A "sender" or "receiver" is an account key. Amounts are never summed across currencies.

| Feature | Plain meaning |
| --- | --- |
| `hour_of_day`, `day_of_week` | Hour (0 to 23) and weekday (Monday = 0) of the transaction, in UTC |
| `log_amount_paid` | `log(1 + amount paid)`, so very large amounts do not dominate |
| `payment_format`, `payment_currency`, `receiving_currency` | Taken straight from Silver (for example ACH, Cheque, Wire; the two currencies) |
| `is_same_account`, `is_same_entity`, `is_same_entity_different_account`, `is_cross_entity`, `is_cross_bank`, `is_cross_currency` | The six yes/no flags already in Silver, passed through. They say whether sender and receiver are the same account or entity, belong to different entities or banks, or use different currencies. |
| `snd_out_cnt_1h`, `snd_out_cnt_24h` | How many transactions this sender made in the previous 1 hour / 24 hours |
| `snd_out_amt_24h_ccy` | Total this sender paid in the previous 24 hours, in the same currency as this payment (0 if none) |
| `rcv_in_cnt_1h`, `rcv_in_cnt_24h` | How many transactions this receiver got in the previous 1 hour / 24 hours |
| `rcv_in_amt_24h_ccy` | Total this receiver got in the previous 24 hours, in the same currency as this receipt (0 if none) |
| `pair_prior_cnt` | How many earlier transactions went from this sender to this receiver |
| `is_new_pair` | True when `pair_prior_cnt` is 0, so this is the first payment between the two |
| `snd_distinct_rcv_to_date` | How many different receivers this sender has paid before |
| `rcv_distinct_snd_to_date` | How many different senders have paid this receiver before |
| `snd_prior_txn_cnt` | This sender's total number of earlier transactions |
| `snd_prior_mean_amt_ccy` | This sender's average earlier amount in the same currency (empty if there is no history) |
| `amt_ratio_to_snd_mean` | This amount divided by that average, so 3 means three times the sender's usual size (empty if there is no history) |
| `snd_secs_since_last_out` | Seconds since this sender's previous payment (empty if this is the first) |
| `amt_vs_ccy_median` | Added at training time: `log_amount_paid` minus the typical (median) value for that currency in the training days. Shows whether the amount is large or small for its currency. |

That makes 27 inputs to the model: the 26 features stored in `gold/bq1/features/li-medium-v1/` plus `amt_vs_ccy_median`. Empty values are left empty and handled by the model. The full machine-readable list, with data types, is `models/bq1/xgboost/v1/feature_schema.json`; the reasoning is in `docs/BQ1_methods_and_decisions.md` section 4.

## 3. How to read the table (important)

Every row is scored, but not every row is a fair test of the model. `data_split` tells you which rows to trust:

| `data_split` | Rows | Dates (UTC) | Fair for measuring performance? |
| --- | --- | --- | --- |
| `train` | 18,748,707 | 1 to 9 Sep | No: the model learned from these |
| `validation` | 6,251,102 | 9 to 14 Sep | No: used to tune the model |
| `test` | 6,250,304 | 14 to 16 Sep | **Yes** |
| `excluded` | 1,370 | 17 to 27 Sep | No: left out on purpose (see below) |

- **For a fair analyst queue or any recall/precision figure, filter to `data_split = 'test'`.** The ready-made queries already do this.
- **The plain global `risk_rank` is valid but its top is a mix.** Of the global top 1,000, only 157 are test rows (305 are excluded rows, 277 validation, 261 train), because those rows score very high. Showing the riskiest rows across the whole dataset with the global rank is fine, but do not present it as how well the model performs.
- Precision looks low because laundering is rare (0.048% of test rows). A perfect top-1% queue would still reach only about 5% precision, so judge the model on recall and on how it compares with the baselines.

**The excluded tail days.** The last 11 days (17 to 27 Sep) have only a few hundred transactions a day, and over half of them are laundering-labelled, unlike the normal days (about 0.05%). They are scored but kept out of every split and every headline number. We checked what happens if they are included: the model ranks them very well (they are easy to separate), and counting them would push Recall@1% from 0.634 up to 0.705. BQ1's decision is to keep them excluded. Details: `docs/BQ1_methods_and_decisions.md` section 13.

## 4. How well it works (test days only: 6.25M transactions, 3,013 laundering-labelled)

Recall@K means "if analysts review the riskiest K% of transactions, what share of the laundering-labelled ones do they find". Alert reduction is the review work saved (reviewing 1% saves 99%).

| Model | Recall@0.1% | Recall@1% | Recall@10% | Precision@1% |
| --- | --- | --- | --- | --- |
| Random picking | 0.001 | 0.010 | 0.100 | 0.05% |
| Rules baseline | 0.009 | 0.188 | 0.689 | 0.9% |
| Logistic regression | 0.183 | 0.446 | 0.885 | 2.2% |
| **XGBoost (selected)** | **0.302** | **0.634** | **0.928** | **3.1%** |

- XGBoost is the best at every review budget up to 10%. Recall@1% is the primary measure: 0.634 against 0.446 and 0.188.
- Recall@1% is uncertain by about ±0.017. On the smaller LI-Small test (791 positives) the same model scored 0.632, so the result is stable.
- Checks passed: no single feature comes anywhere near explaining the labels on its own (the best one scores a PR-AUC of 0.009, against 0.239 for the full model), and no model comes close to perfect, so there is no sign of label leakage.
- The full comparison tables (more K values, precision, PR-AUC) are in `models/bq1/xgboost/v1/eval_table_test_medium.csv`.

## 5. Shared AWS resources created or changed by BQ1

- **Glue job `is459_bq1_features`** (us-east-1): builds the features from Silver. It is only needed again if the features change, and a LI-Medium run costs about US$0.4, so please check with BQ1 before re-running.
- **One IAM change to a shared role:** an inline policy `IS459BQ1FeaturesAccess` was added to `AWSGlueServiceRole-is459-aml-g1t5`. It lets the role read and write only under `gold/bq1/` and `scripts/bq1/` (the role could not write there before).
- **S3 locations, all BQ1's own:** `gold/bq1/features/` (the 26 features, LI-Small and LI-Medium), `gold/bq1/scores/` (the Gold table), `models/bq1/` (trained models and results), `scripts/bq1/` (code), `gold/bq1/athena-results/` (Athena query output). Nothing was written to `bronze/`, `silver/` or other teams' folders.
- **Athena:** the score table above, plus `is459_aml_gold.bq1_features_medium_v1`, a read-only view of the features that BQ1 used for analysis.
- **Cost:** about US$1.3 in total for all BQ1 compute, far below the US$250/month account budget.

## 6. Known limits

- The data is synthetic, so the numbers show how well the ranking recovers a synthetic pattern, not how it would do on real alerts.
- The models use days 1 to 16 Sep of LI-Medium; the 11 tail days are excluded (section 3).
- One time-based split, no cross-validation. The share of laundering differs a little between splits, so compare models within a split.
- `risk_score` is not a probability. Use the rank.
- Train and validation rows are scored on data the model has seen, so use the test split for any performance claim.
- Amounts are never converted between currencies. The amount features compare within a currency or against the account's own history.
- Not built yet: per-transaction explanations (the reasons a transaction was flagged). They depend on the UI owner's answers below.
- Not done: a run on the HI-Medium dataset (it is not in Bronze yet, so it needs a team decision).

## 7. Questions for the UI owner (please reply so BQ1 can finish the explanation fields)

The score table is ready to use. The one piece not built yet is **per-alert explanations**: the top three reasons a transaction was flagged, taken from the XGBoost model. It waits on your schema, so please answer these:

1. **Do you want explanations at all?** If yes, the plan is the top three contributing features per flagged transaction, each with its direction (pushes the score up or down) and size.
2. **Where should they go?** Either (a) extra columns on `bq1_risk_scores_medium_v1`, or (b) a separate table keyed by `transaction_id` that you join. BQ1 recommends (b): it avoids rewriting the 31M-row score table and you only join when you need the reasons.
3. **Which rows need them?** The plan is the top 1% of the ranked rows only. Is that the top 1% of the **test** queue (62,504 rows, the fair queue), or of all rows, or a different K or split?
4. **What format?** For each of the three reasons, do you want the feature name, a signed contribution number, a plain-language sentence (for example "first payment between these two accounts"), or all of these?
5. **Anything else you need in the score table?** For example extra columns, a different partitioning, or a view that already filters to the test split. Tell BQ1 what the UI will query.

Reply to the BQ1 owner with your answers. The explanation step is then a small, cheap job on the existing model and does not change any existing score or rank.

## 8. Where to find more

- `docs/BQ1_athena_queries.sql`: the three ready-made queries, with a plain-language guide at the top.
- `docs/BQ1_methods_and_decisions.md`: the reasoning behind each choice, feature definitions, results, and the tail-day analysis.
- `BQ1_tasks.md`: the full task list and a line-by-line log of what was run, with costs and checks.
