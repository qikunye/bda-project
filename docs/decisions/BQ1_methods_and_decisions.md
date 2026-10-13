# BQ1 methods, decisions and results (LI-Small, through Epic 5)

Working document. `BQ1_tasks.md` holds the plan and the one-line progress log. This file records the reasoning, the
choices that were not in the plan, deviations, problems met, and the numbers. It feeds `BQ1_HANDOVER.md` (T8.1).
Data is synthetic (IBM AML); none of the rates below are real-world laundering rates.

Last updated: 2026-10-09.

## 1. Where things are

All paths are relative to `/Users/claire/big_data/bq1/`.

| What | Where |
| --- | --- |
| Plan and progress log | `BQ1_tasks.md` |
| This document | `docs/BQ1_methods_and_decisions.md` |
| Feature code (v1 production, v2 unused path) | `bq1/features.py` |
| Split, fitted statistics, schema helpers | `bq1/split.py` |
| Models: rules, logistic regression, XGBoost, leakage audit | `bq1/train.py` |
| Metrics (Recall@K, Precision@K, FP per TP, PR-AUC) | `bq1/metrics.py` |
| Local Spark session helper (not used by Glue) | `bq1/spark_session.py` |
| Runnable scripts, one per step | `scripts/` |
| Silver transactions synced from S3 (400 MB) | `data/silver/li_small/transactions/` |
| LI-Small v1 features (509 MB Parquet) | `data/features/li_small_v1/` |
| All Epic 3 to 5 outputs (configs, models, tables, logs) | `artifacts/small/` |
| Epic 1 EDA CSVs | Google Drive `MyDrive/bq1/eda/` (written on Colab, not copied here) |

`bq1/score.py` is still an empty stub (Epic 7).

## 2. Environment

- Epic 0 and Epic 1 ran on Colab. The Colab session crashed during T2.8/T2.9 (out of RAM: `toPandas()` on 6.9M rows with an 8 GB
  driver, on top of a low free-tier allowance). Colab files under `/content/` were lost.
- With the user's approval everything from T2.7 onward runs **locally on the Mac** (8 cores, 16 GB). Installed with Homebrew: `openjdk@17`, `libomp`.
  Python venv at `.venv/`. Spark local mode: driver 8 GB, UTC session timezone, 64 shuffle partitions, spill directory `data/spark_tmp/`.
- Versions (local): pyspark 4.0.4, boto3 1.43.110, xgboost 3.4.1, scikit-learn 1.9.1, pandas 3.0.6, pyarrow 26.0.0, Python 3.13.
  The Colab run had scikit-learn 1.6.1, pandas 2.2.3, pyarrow 23.0.1. All reported numbers after T2.7 are from the local run.
- Disk is tight (about 6.7 GB free, 97% full). One Spark run already failed once with "No space left on device".
- AWS access: IAM user `g1team5`, account 941377154820, `us-east-1`. Keys are not stored in any file. Locally they come from the AWS CLI config.
  The first Colab secret values were rejected by AWS (`SignatureDoesNotMatch`); re-entering both values from the working key fixed it.
  An unused access key from the failed attempts may still be active in IAM and should be deactivated.

## 3. Data (Epic 0 and Epic 1)

- Silver LI-Small transactions: 6,924,049 rows, 3,565 laundering, distinct `transaction_id` = rows. All columns listed in section 0 of the plan are present.
  Extra Silver columns that are not used: `dataset_variant`, `source_file`, `ingest_run_id`, `row_line_no`, `from_bank_norm`, `to_bank_norm`,
  `both_entities_resolved`, `entity_resolution_status`. `timestamp` is a Spark timestamp (not a string).
- Per-day volume is uneven (days 1 and 2 are large; 3, 4 and 10 Sep are low-volume weekend days with a higher laundering rate of about 0.1%).
- **Tail days.** 11 to 17 Sep have 3 to 77 rows per day (under 10% of the 282,877 median). They hold 223 rows and **134 positives** (rates 56 to 71%).
  The user decided to drop them from every split. Usable window: **2022-09-01 00:00 to 2022-09-11 00:00 UTC**, 6,923,826 rows, 3,431 positives.
- Laundering concentration: ACH carries 2,611 of 3,565 positives (rate 0.33%); Wire and Reinvestment have none. Cross-currency rows have none.
  Same-account rows almost none (3).
- Account skew: top sender `70|10042B660` has 222,037 transactions; p99.9 is 158 per sender. Receivers are far less skewed (max 1,553).
- Same-minute ties: 14.6% of rows share (sender, minute) with another row; 4.8% share (receiver, minute). The strictly-earlier rule leaves
  same-minute rows out of every feature.

## 4. Features v1 (Epic 2) - frozen

`build_features(df, variant="v1")` in `bq1/features.py`. 26 columns after the 6 key columns, all causal
(`rangeBetween(..., -1)` on epoch seconds, so only strictly earlier rows). `is_laundering` is carried as the label only.

Choices that the plan did not fix:
- `hour_of_day` and `day_of_week` come from `ts` in UTC (Monday = 0), so they do not depend on the Spark session timezone.
- Sums and distinct-counterparty counts are 0 (not null) when there is no history. `amt_ratio_to_snd_mean` is null with no history or a zero prior mean.
  `snd_prior_mean_amt_ccy` and `snd_secs_since_last_out` are null with no history.
- Features are computed on all 6,924,049 rows including the tail days; the tail-day drop is applied at the split step. This does not change any
  earlier row because the features only look backwards.
- Distinct-counterparty counts use `row_number()` over the pair, then a strictly-earlier running sum (no `countDistinct` over a window).

Validation:
- T2.7: output rows = input rows = 6,924,049, distinct IDs unchanged. 117 s locally (581 s on Colab).
- T2.8: 20 of 20 random transactions match a brute-force recompute of `snd_out_cnt_24h`, `pair_prior_cnt`, `snd_distinct_rcv_to_date`
  (seed 42; passed on both the Colab and local builds).
- T2.9: dropping everything after the median timestamp and rebuilding gives identical features for all 3,462,207 surviving rows (doubles compared at 1e-9 relative).

## 5. Split (Epic 3)

60/20/20 by row over the usable window; a timestamp shared by several rows goes entirely to the later split. Boundaries (UTC):
t60 = 2022-09-06 13:31 (ts 1662471060), t80 = 2022-09-08 16:06 (ts 1662653160).

| Split | Rows | Positives | Prevalence |
| --- | --- | --- | --- |
| Train | 4,153,991 | 1,813 | 0.0436% |
| Validation | 1,385,006 | 827 | 0.0597% |
| Test | 1,384,829 | 791 | 0.0571% |

All splits have more than 300 positives, so no gate was needed. Train prevalence is lower than validation/test because train contains the two large, low-rate days.

Train-only fitted statistics (`feature_schema.json`, 27 model features: 17 float32 numeric, 7 int8 flags, 3 categoricals):
- `amt_vs_ccy_median` = `log_amount_paid` minus the train median of `log_amount_paid` for that `payment_currency` (the plan did not give a formula).
  A currency not seen in train uses the global train median. Bitcoin's median is 0.08; the others range from 6.5 to 11.5.
- Category lists: 7 payment formats, 15 payment currencies, 15 receiving currencies, plus `__other__` for unseen values (none occur outside train).

## 6. Models (Epic 4)

- **Rules.** Six indicators, weight = max(0, ln(train rate with indicator / train base rate)), score = sum of weights. Weights: ACH 1.878,
  `snd_out_cnt_24h >= p95` 1.142, `is_new_pair` 0.855, `rcv_distinct_snd_to_date >= p95` 0.644, `amt_ratio_to_snd_mean >= 3` 0.169,
  `is_cross_currency` 0 (no train positives). Top-lift format picked with a minimum support of 1,000 train rows.
  Rules produce few distinct scores, so many rows tie; ties are broken by `transaction_id`, which is effectively random order.
  That is why Rules precision is flat between K=0.1% and K=0.5%.
- **Logistic regression.** All 1,813 train positives plus a 5% random sample of train negatives (207,609, seed 42). `log1p` on 13 nonnegative
  count/amount/gap columns, median imputation with missing indicators, standard scaling, one-hot categoricals, `class_weight="balanced"`, `max_iter=1000`.
- **XGBoost.** Full train set, `hist`, categorical support, `learning_rate=0.1`, `subsample=0.8`, `colsample_bytree=0.8`, `aucpr`, up to 2000 rounds, early stopping 50 on validation.
  - First pass (plan settings): `scale_pos_weight` = sqrt(neg/pos) = 47.9 beat neg/pos = 2,290 (val PR-AUC 0.2436 vs 0.1739).
  - Tuning (extra, requested by the user, validation only): staged search over `scale_pos_weight` {5, 10, 20, 47.9}, then `max_depth` {4, 8},
    `min_child_weight` {5, 20}, `reg_lambda` {10, 50}; a change was kept only for a gain of at least 1% relative on validation PR-AUC.
    Chosen: **`scale_pos_weight` 5, `max_depth` 8, `min_child_weight` 20**. Val PR-AUC 0.2438 to 0.2662; best iteration 74.
    The full log is `artifacts/small/xgb_tuning_log.csv`. These LI-Small settings are reused for LI-Medium without re-tuning (T7.4).
  - Early stopping uses the validation split, so validation PR-AUC is slightly optimistic.
- **v2 features (T2.10) tried and not adopted.** Added `snd_in_cnt_24h`, `snd_secs_since_last_in`, `rcv_out_cnt_24h` from a long account-events table.
  Verified (brute force 40/40, truncation test clean) but no real gain (tuned XGBoost test PR-AUC 0.2195 vs 0.2191). v1 stays production.
  The `variant="v2"` code path is kept for the Glue `--variant` argument but is unused.
- **Importance (T4.4).** Top gain for the first XGBoost: `is_new_pair` 29%, `payment_format` 14%, `is_same_account` 8%, `is_cross_currency` 6%, `pair_prior_cnt` 6%.
  The importance file `xgb_importance_top20.csv` is for the current tuned model.
- **Leakage audit (T4.5).** Best single feature on test: `pair_prior_cnt` PR-AUC 0.0069 (prevalence 0.00057; flag threshold 0.5). No model had Recall@1% above 0.99. Nothing flagged, no gate.
  Single-feature scores use the better of x and -x; categoricals use a train-rate encoding.
- `day_of_week` is a mild concern: with only ten days it can proxy for specific dates. It has about 2.6% of gain. Test weekdays (Thu to Sat) all appear in train. Re-check on LI-Medium.

## 7. Evaluation on the test split (Epic 5)

Test split: 1,384,829 rows, 791 positives (prevalence 0.057%). Rank by score descending, ties by `transaction_id`. K = share of test rows.
Recall 95% interval is +/- 1.96 * sqrt(r(1-r)/791), about +/-0.03 at the middle K values. Full table: `artifacts/small/eval_table_test.csv`;
curve data (200 K values, 0.01% to 20%, geometric): `curve_small.csv`.

| Model | K | Alerts | Recall | Precision | FP per TP |
| --- | --- | --- | --- | --- | --- |
| Rules | 0.1% | 1,385 | 0.011 | 0.0065 | 152.9 |
| Rules | 0.5% | 6,925 | 0.057 | 0.0065 | 152.9 |
| Rules | 1% | 13,849 | 0.161 | 0.0092 | 108.0 |
| Rules | 5% | 69,242 | 0.679 | 0.0078 | 127.9 |
| Rules | 10% | 138,483 | 0.724 | 0.0041 | 240.7 |
| Logistic Regression | 0.1% | 1,385 | 0.138 | 0.0787 | 11.7 |
| Logistic Regression | 0.5% | 6,925 | 0.285 | 0.0325 | 29.8 |
| Logistic Regression | 1% | 13,849 | 0.407 | 0.0233 | 42.0 |
| Logistic Regression | 5% | 69,242 | 0.764 | 0.0087 | 113.6 |
| Logistic Regression | 10% | 138,483 | 0.872 | 0.0050 | 199.7 |
| **XGBoost (tuned)** | 0.1% | 1,385 | 0.272 | 0.1552 | 5.4 |
| **XGBoost (tuned)** | 0.5% | 6,925 | 0.525 | 0.0599 | 15.7 |
| **XGBoost (tuned)** | 1% | 13,849 | 0.632 | 0.0361 | 26.7 |
| **XGBoost (tuned)** | 5% | 69,242 | 0.779 | 0.0089 | 111.4 |
| **XGBoost (tuned)** | 10% | 138,483 | 0.925 | 0.0053 | 188.2 |
| Random | any K | - | K | 0.0006 | 1,749.7 |

PR-AUC on test: Rules 0.0093, Logistic Regression 0.0342, XGBoost 0.2191, Random 0.0006.

Reading the table:
- At the primary K (1%), XGBoost recall 0.632 (+/-0.034) versus LR 0.407 (+/-0.034) and Rules 0.161: a clear win. The same holds at 0.1% and 0.5%.
- At 5% and 10% the three models are close (XGBoost 0.779 vs LR 0.764 at 5%; Rules is competitive at 5%). The advantage is concentrated at the top of the ranking.
- Precision is bounded by prevalence: with 791 positives, a perfect 1% queue (13,849 alerts) could reach at most 5.7% precision. XGBoost's 3.6% is about 63% of that ceiling.
  Precision@0.1% (15.5%) is the more informative precision figure.

## 8. Problems met and how they were handled

| Problem | Cause | Fix |
| --- | --- | --- |
| AWS `SignatureDoesNotMatch` in Colab | Secret values did not pair with a valid key | Re-entered both from the working key; confirmed with STS |
| Colab kernel crash, then lost files | `toPandas()` of 6.9M rows into 8 GB driver; free-tier limits | Switched to local Mac with user approval |
| Local disk filled during first v2 build | Wide 26-column table shuffled through a join | Pass-through features computed on a narrow 4-column table (`passthrough_features`) |
| v2 brute-force check reported 0/40 | Check script encoded sender and receiver keys with two different dictionaries | Rewrote the check with one shared encoding: 40/40. The features were correct |
| Stale Colab task notifications | Old background runs reporting after the crash | Ignored; T2.9 was redone locally |
| Colab notebook could not save | Notebook view/permission problem | Moved on to the local run |

## 9. Decisions and artefact upload (end of Epic 5)

- **T5.4 (2026-10-09):** the user approved **XGBoost (tuned)** as the selected model. Primary metric Recall@1% = 0.632 versus LR 0.407 and Rules 0.161.
- **T5.5 (2026-10-09):** artefacts uploaded to `s3://is459-aml-g1t5/models/bq1/<model>/v1-small/` for `xgboost`, `logistic_regression`, `rules`
  (18 objects; the prefix was empty beforehand). Each folder has `feature_schema.json`, `rules_v1.json`, `split_config.json`, `metrics.json`,
  `model_metadata.json`. `xgboost/` adds `model.ubj`; `logistic_regression/` adds `lr_model.pkl` and `lr_coefficients.csv`; `rules/` has no separate
  model file because `rules_v1.json` is the model. The LR pickle depends on scikit-learn 1.9.1. Nothing outside `models/` was written.

## 10. Glue wrapper (T6.1)

- `glue_bq1_features.py` (project root) is a thin wrapper: all feature logic stays in `bq1/features.py`. Arguments: `--silver_path`, `--out_path`, `--variant`.
  It adds `txn_date` (UTC calendar date), repartitions by it (one file per day), writes Snappy Parquet partitioned by `txn_date`, counts input and
  output rows, and raises if they differ.
- Safety guard (hard rule 1): `out_path` must contain `/gold/bq1/` and must not contain `/bronze/` or `/silver/`.
- Packaging: `scripts/package_glue.sh` builds `dist/bq1.zip` with `bq1/__init__.py` and `bq1/features.py` only. It is supplied to Glue with
  `--extra-py-files`. Not uploaded yet (T6.3 is gated).
- `--variant` is interpreted as the **feature variant** (`v1` default, `v2` optional). The plan did not say whether it meant that or the dataset variant (Silver has a `dataset_variant` column). Confirm with the team.
- Local dry run that mimics Glue 5.0 (Spark 3.5.4, Python 3.11.13, imports from the zip): 6,924,049 rows in and out. All rows and all features are identical to the Spark 4.0.4 v1 features
  (`scripts/compare_glue_local.py`). This is a stronger check than the 1,000-row sample in T6.4, but T6.4 is still needed to prove the job on Glue itself.
- The first dry run failed with a Java out-of-memory error. Cause: a bare local PySpark session defaults to 1 GB of driver memory. It is a test-setup issue (Glue sets memory by worker type), not a wrapper bug.
- Not covered by the local test: S3 reads and writes, Glue role permissions, `awsglue` argument parsing (`getResolvedOptions`), and the real Medium-scale performance.

## 11. Glue IAM (T6.2)

- Finding: `AWSGlueServiceRole-is459-aml-g1t5` could read Silver but could not write under `gold/bq1/`, delete there (Spark overwrite), read
  `scripts/bq1/`, or list those prefixes. Existing grants: managed `AWSGlueServiceRole`; inline `IS459SilverETLScopedAccess` (read Bronze LI-Small/LI-Medium,
  read and write `silver/ibm-aml/*`); customer-managed `...-EZCRC-s3Policy` (read and write Bronze LI-Medium transactions/accounts).
- Fix (approved by the user, applied 2026-10-09): inline policy `IS459BQ1FeaturesAccess`, file `docs/iam_bq1_glue_policy.json`. Three statements:
  list only the `gold/bq1/` and `scripts/bq1/` prefixes, read `scripts/bq1/*`, read/write/delete `gold/bq1/*`. Verified with the IAM policy simulator;
  `gold/bq2/*`, other `gold/` prefixes, `scripts/other/*` and `models/*` remain denied.
- Rollback: `aws iam delete-role-policy --role-name AWSGlueServiceRole-is459-aml-g1t5 --policy-name IS459BQ1FeaturesAccess`.
- The role can still write to Bronze LI-Medium through the EZCRC policy. Out of BQ1 scope; the wrapper's path guard refuses such paths. Worth telling the role's owner.
- The AWS CLI on this machine has no default region; Glue commands need `--region us-east-1`.

## 12. Open items before LI-Medium

- T6.3 (done 2026-10-09): uploaded `glue_bq1_features.py` and `bq1.zip` to `s3://is459-aml-g1t5/scripts/bq1/` and created job `is459_bq1_features` in `us-east-1`
  (`docs/glue_job_is459_bq1_features.json`: Glue 5.0, G.1X x 10, 60 min timeout, 0 retries, max concurrency 1, default `--variant v1`). No run has been started, so no cost yet.
  `--silver_path` and `--out_path` are not defaults; each run passes them. The user confirmed v1 as the production variant and will tell the team after creation.
  Another job, `is459_silver`, already exists in the account (not ours). Rollback: `aws glue delete-job --region us-east-1 --job-name is459_bq1_features`.
- T6.4 (done 2026-10-10): LI-Small parity run on Glue SUCCEEDED. Run `jr_f421ebdc7ba98bf74436f35ac75f8a9202db9c0cee57415dde9525b58b8e976e`,
  182 s on 10 x G.1X, 1,820 DPU-seconds (0.51 DPU-hours), estimated cost about US$0.22 (estimate at US$0.44 per DPU-hour, not billing data; plan estimate was US$0.60).
  Job log: 6,924,049 rows in and out, "row counts match". Output `s3://is459-aml-g1t5/gold/bq1/features/li-small-v1/`: 17 files (one per `txn_date`), 581.7 MiB.
  Glue 5.0 wrote no `_SUCCESS` marker (the local run did), so downstream code must not rely on it. Full parity: all 6,924,049 rows and all features identical to the local features
  (`scripts/compare_glue_local.py <dir>`). Speed: Glue 3 min vs about 2 min on the Mac for LI-Small.
- T6.5 (done 2026-10-10): LI-Medium run SUCCEEDED first time. Run `jr_a0e6231f9517848988339cef53a85ddc766e92b5443da9aa30429d09b912e796`, 319 s on 10 x G.1X,
  3,196 DPU-seconds (0.89 DPU-hours), estimated cost about US$0.39 (estimate at US$0.44 per DPU-hour; plan estimate US$2 to 3). No retry, memory or skew problem (T6.6 not needed).
  Output `s3://is459-aml-g1t5/gold/bq1/features/li-medium-v1/`: 27 files (one per `txn_date`, 2022-09-01 to 2022-09-27), 2.6 GiB. Checks: 31,251,483 rows in the job log and in the downloaded output,
  distinct `transaction_id` = rows, `sum(is_laundering)` = 16,041 (matches Silver). The 4.5x data volume took 1.75x the time of LI-Small, so Glue scaled well.
  Parity of the Medium output against local features was not possible (no local Medium features), but the same code gave identical results at LI-Small scale on Glue (T6.4).
- Epic 7: SageMaker notebook (gated), retrain with the LI-Small settings, score all LI-Medium rows, Athena table.
- Deactivate the unused IAM access key left from the failed Colab attempts.
- Copy or regenerate the Epic 1 EDA files locally if they are needed on this machine (they are in Drive).


## 13. Tail-day analysis, LI-Medium (Epic 9, 2026-10-10)

**Question.** The tail days (17 to 27 Sep: 1,370 rows, 771 positives, laundering rate 56%) are excluded from every split (section 5, T7.3) and appear in Gold as `data_split = 'excluded'`.
They come after the test window, so the model never saw them and their scores are out of sample. How does the model rank them? This is a separate report; the split, the models and the headline numbers did not change.
**Method.** Rank arithmetic ran in Athena over `bq1_risk_scores_medium_v1` (rank = score descending, ties by `transaction_id`); only small tables came back (`scripts/epic9_tail.py`, `scripts/epic9_features.py`, `scripts/epic9_chart.py`).
Files are in `artifacts/medium_tail/`. The in-SQL recomputation of the headline reproduced T7.5 exactly (Recall@1% 0.6343, PR-AUC 0.2394), which checks the arithmetic.

**T9.1.** 1,370 rows, 771 positives, 11 dates; per-date counts match the T7.3 daily table.

**T9.2 and T9.3: would the test-time review budget have caught the tail laundering?** The threshold is the score of the m-th ranked test row; a tail row is flagged if its score is at or above it. No ties at any threshold.

| K (test queue) | Test threshold | Tail rows flagged | Tail positives flagged | Tail recall | Tail precision |
| --- | --- | --- | --- | --- | --- |
| 0.1% (m = 6,251) | 0.2354 | 761 | 693 | 0.899 | 0.911 |
| 0.5% (m = 31,252) | 0.0501 | 838 | 748 | 0.970 | 0.893 |
| 1% (m = 62,504) | 0.0193 | 864 | 760 | 0.986 | 0.880 |
| 5% (m = 312,516) | 0.0077 | 887 | 764 | 0.991 | 0.861 |
| 10% (m = 625,031) | 0.0050 | 903 | 766 | 0.994 | 0.848 |

Yes. At the same score cut-off that gives 1% alerts on the test days, the model flags 63% of tail rows and 98.6% of tail laundering rows.
That is far above the 63.4% recall at 1% on the test days, so tail laundering is easier to spot than test-day laundering.

**T9.4: ranking within the tail only (prevalence 0.563, tail PR-AUC 0.956).**

| Top K of tail rows | Rows | Recall | Precision | Lift over 0.563 |
| --- | --- | --- | --- | --- |
| 1% | 14 | 0.018 | 1.000 | 1.78 |
| 10% | 137 | 0.178 | 1.000 | 1.78 |
| 25% | 343 | 0.432 | 0.971 | 1.73 |
| 50% | 685 | 0.811 | 0.912 | 1.62 |

Precision is high for any ranking here because over half the rows are positive, so judge by the lift (1.6x to 1.8x), not the raw precision.

**T9.5: score distributions** (`tail_score_summary_medium.csv`, chart `tail_score_distributions_medium.png`; tail exact, test quartiles from Athena `approx_percentile`).
Median `risk_score`: tail positives 0.94, tail negatives 0.0014, test positives 0.086, test negatives 0.000026. Tail rows of both labels score higher than their test counterparts, and tail positives are pushed close to 1.

**T9.6: what the headline would have been with the tail pooled in** (6,251,674 rows, 3,784 positives).

| K | Recall, test only | Recall, test + tail | Precision, test only | Precision, test + tail |
| --- | --- | --- | --- | --- |
| 0.1% | 0.302 | 0.414 | 14.6% | 25.0% |
| 0.5% | 0.561 | 0.640 | 5.4% | 7.8% |
| 1% | 0.634 | 0.705 | 3.1% | 4.3% |
| 5% | 0.777 | 0.820 | 0.75% | 0.99% |
| 10% | 0.928 | 0.941 | 0.45% | 0.57% |

PR-AUC rises from 0.239 to 0.385. Pooling the tail would inflate every metric (Recall@1% by 0.07), because those rows are easy and nearly all positive. This quantifies the distortion that justified dropping the tail days. The headline stays the test-only number.

**T9.7: why the model ranks them where it does (descriptive; `tail_feature_compare_medium.csv`, `tail_payment_format_medium.csv`).** Joined to the Gold features (read-only external table `bq1_features_medium_v1`, `docs/athena_bq1_features.sql`).

| | Tail negatives | Tail positives | Test negatives | Test positives |
| --- | --- | --- | --- | --- |
| Rows | 599 | 771 | 6,247,291 | 3,013 |
| Payment format ACH | 100% | 100% | 12.7% | 69.5% |
| `is_new_pair` | 27.0% | 96.4% | 2.4% | 57.2% |
| `is_same_account` | 82.3% | 0.4% | 1.9% | 0.1% |
| `is_cross_entity` | 17.7% | 99.6% | 97.7% | 99.4% |
| `is_cross_currency` | 82.3% | 0% | 1.6% | 0% |
| Median `snd_out_cnt_24h` | 0 | 0 | 5 | 2 |
| Median `snd_prior_txn_cnt` | 12 | 18 | 66 | 20 |
| Median `pair_prior_cnt` | 1 | 0 | 23 | 0 |
| Median `amt_ratio_to_snd_mean` | 0.81 | 0.84 | 0.22 | 0.03 |

Reading: every tail row is ACH, so the top-gain feature `payment_format` (20% of gain) cannot separate them. What does separate them is `is_same_account` (12% of gain) and `is_new_pair` (9%):
82% of tail negatives are same-account cross-currency rows, which the model has learned are benign, while 96% of tail positives are new, cross-entity pairs, which the model has learned are risky.
The tail rows also look unlike normal days: a few hundred rows a day against 0.8 to 4.4 million, a median of zero outgoing transactions in the previous 24 hours, and a 56% laundering rate against 0.05%.
Inference, not verified against the generator: late in the window ordinary traffic dries up while laundering-labelled ACH transfers continue, which makes the remaining rows easy to separate.
There is no sign of leakage: the features are causal (Epic 2 tests) and the leakage audit passed on the test split.

**Decision (user, 2026-10-10).** Keep excluding the tail days from every split and from the headline numbers. The findings above are kept as a separate, documented analysis. The split, the models and the Gold scores are unchanged.
