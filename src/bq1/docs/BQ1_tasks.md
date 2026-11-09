# BQ1 Alert Prioritisation: Epics and Tasks

Reference plan for Claude Code. Work top to bottom. Each task has an ID, an action, and a "done when" check. Do not start a task until its dependencies are done.

**Deadline:** BQ1 Gold ready for integration by Sunday 11 Oct 2026. LI-Small training should finish by Saturday 10 Oct.

---

## STATUS AND PICKUP NOTES (written 2026-10-10 ~00:30 +08; read this first)

**Done: Epics 0 to 6, and Epic 7 (T7.1 to T7.10, 2026-10-10).** Epic 7 runs on SageMaker notebook `is459-bq1-epic7` (user chose SageMaker); next: nothing required; optional T8.3 (waits on the UI owner), T8.4 (needs a team decision), T9.10 (S3 upload, waits on the user), then Athena T7.8 to T7.9 from the Mac. See the Progress log for details. Deadline for BQ1 Gold is **Sunday 11 Oct 2026**.
Full reasoning, numbers and problems are in `docs/BQ1_methods_and_decisions.md`; the line-by-line log is the Progress log at the bottom of this file.

**Epic 9, tail-day analysis: DONE except the optional S3 upload (T9.10, waiting for the user's yes/no) and the optional Small repeat (T9.9, skipped)** (how the model ranks the excluded tail-day rows, reported separately; the split and headline numbers do not change). See the Epic 9 section below.

### Where to restart (historical: items 1 to 4 below are all DONE as of 2026-10-10; see the Progress log. Remaining work is Epic 8.)
1. **Open decision (ask the user first): where to run Epic 7.**
   - Plan: a SageMaker notebook (about 32 GB RAM, e.g. `ml.m5.2xlarge`, about US$0.46/hour). T7.1 is a human gate: the user launches it and stops it at T7.10. It must read `gold/bq1/features/`, write `gold/bq1/` and `models/`.
   - Alternative offered but NOT yet answered: train on the Mac (16 GB RAM, 8 cores, only about 6 GB free disk). The Medium features are 2.6 GiB on S3; XGBoost on 31M rows x 27 float32 columns needs roughly 4 to 6 GB of RAM if `transaction_id` strings are loaded only at the end. Risky but avoids cost and setup. Athena and S3 steps work from the Mac either way.
2. **T7.3 first**: repeat T1.1 and T1.5 on LI-Medium (daily counts, tail days under 10% of the median), apply the same tail rule, then compute the 60/20/20 boundaries with `bq1/split.py`. `split_config.json` for Medium is a new file; do not reuse the LI-Small dates (`cfg["window_start_ts"]`, `window_end_ts`, `t60_ts`, `t80_ts` are LI-Small values).
3. **T7.4**: refit train-only stats and retrain all three models with the LI-Small settings. Do not re-tune on test. XGBoost: `scale_pos_weight=5, max_depth=8, min_child_weight=20`, rest as in `artifacts/small/xgb_tuned_params.json`; early stopping on validation is allowed (it is not test).
4. T7.5 to T7.10 as written. `bq1/score.py` is still an empty stub. `risk_rank` must be a global, dense, unique rank (sort by score descending, ties by `transaction_id`, same as `bq1/metrics.py`).

### What exists (all verified)
- **LI-Medium features (Gold input for Epic 7):** `s3://is459-aml-g1t5/gold/bq1/features/li-medium-v1/`, 27 files, one per `txn_date` (1 to 27 Sep), 31,251,483 rows, distinct `transaction_id` = rows, 16,041 positives. Hive-partitioned: `txn_date` is in the path, not in the files. Columns = 6 key columns + 26 features (see `bq1/features.py`). There is no `_SUCCESS` marker.
- **LI-Small features:** `data/features/li_small_v1/` (local) and `s3://is459-aml-g1t5/gold/bq1/features/li-small-v1/`.
- **LI-Small models and results:** `artifacts/small/` locally and `s3://is459-aml-g1t5/models/bq1/{xgboost,logistic_regression,rules}/v1-small/` (18 objects).
- **Glue job** `is459_bq1_features` (us-east-1, Glue 5.0, G.1X x 10). Script and `bq1.zip` are in `s3://is459-aml-g1t5/scripts/bq1/`. To re-run: `aws glue start-job-run --region us-east-1 --job-name is459_bq1_features --arguments '{"--silver_path":"...","--out_path":"s3://is459-aml-g1t5/gold/bq1/...","--variant":"v1"}'`. If `bq1/features.py` changes, rebuild with `scripts/package_glue.sh` and re-upload `dist/bq1.zip`.
- **IAM:** inline policy `IS459BQ1FeaturesAccess` was added to the Glue role (gold/bq1 and scripts/bq1 only). Rollback command is in the Progress log (T6.2).
- **Code:** `bq1/features.py` (frozen, v1), `bq1/split.py`, `bq1/train.py`, `bq1/metrics.py`, `glue_bq1_features.py`; scripts in `scripts/` (one per step).

### Headline LI-Small results (test split, 791 positives)
| Model | Recall@0.1% | Recall@0.5% | Recall@1% (primary) | Precision@1% | PR-AUC |
| --- | --- | --- | --- | --- | --- |
| Rules | 0.011 | 0.057 | 0.161 | 0.9% | 0.009 |
| Logistic Regression | 0.138 | 0.285 | 0.407 | 2.3% | 0.034 |
| **XGBoost (tuned, selected)** | **0.272** | **0.525** | **0.632** | **3.6%** | **0.219** |
User approved XGBoost at T5.4. Recall has a 95% interval of about +/-0.03. Precision is capped by prevalence (a perfect 1% queue reaches about 5.7%); Precision@0.1% is 15.5%. LI-Medium numbers from T7.5 are the headline numbers for the report.

### Deviations from this plan (all approved or recorded)
- Epics 0 and 1 ran on Colab. Colab then crashed (out of RAM), so **everything from T2.7 onward ran locally on the Mac** (user approved; installed `openjdk@17` and `libomp` via Homebrew; `.venv/`).
- Tail days 11 to 17 Sep dropped from LI-Small (user decision): window 2022-09-01 to 2022-09-10 UTC.
- XGBoost was tuned on validation (extra step the user requested): `scale_pos_weight=5, max_depth=8, min_child_weight=20`. v2 pass-through features (T2.10) were built and verified but gave no gain, so **v1 is the production feature set**; the `variant="v2"` code path stays but is unused.
- `amt_vs_ccy_median` was defined by me (log amount minus the train per-currency median; the plan gave no formula). See the methods doc, section 5.
- T5.1 was pulled forward into Epic 4 (needed for the leakage audit).
- T6.2: the Glue role could not write to `gold/bq1/`; user approved adding the inline policy.
- Costs were far below the plan: LI-Small Glue run about US$0.22, LI-Medium about US$0.39 (estimates at US$0.44 per DPU-hour). AWS Budgets shows a US$250/month account budget; US$4.63 spent in October at the last update on 2026-10-09 (lags a few hours; whole account).

### Gotchas
- The AWS CLI here has **no default region**: add `--region us-east-1` to `glue`, `athena`, `logs` and similar calls (S3 works without).
- Shell is zsh: a variable holding several file names is not split into words; use arrays.
- Disk is nearly full (about 6 GB free). Spark spills about 3 GB for the LI-Small feature build. Delete temporary downloads when done. A safety check blocks `rm` with globs/unresolved paths; use explicit paths.
- pyarrow's own S3 client timed out once on DNS; the `aws s3` CLI is reliable.
- Local run recipe: `export JAVA_HOME=/opt/homebrew/opt/openjdk@17 PATH=$JAVA_HOME/bin:$PATH` then `.venv/bin/python scripts/<script>.py`.
- Package versions differ from Colab (local: scikit-learn 1.9.1, pandas 3.0.6, pyarrow 26.0.0). The LR pickle depends on scikit-learn 1.9.1; a SageMaker notebook may have a different version, so prefer retraining there over loading the pickle.
- `day_of_week` has about 2.6% of XGBoost gain; with 10 days it can proxy for specific dates. Re-check on Medium (27 days).
- All data is synthetic; never describe results as real-world laundering rates (rule 10).

### Needs a human
- **Ask the user:** SageMaker or local for Epic 7 (above).
- T7.1 launch and T7.10 stop of the SageMaker notebook (user), with total runtime and cost recorded.
- The user said they will **tell the team about the new Glue job** `is459_bq1_features` (and the added IAM policy) in the shared account. Confirm it has been done.
- Deactivate the unused IAM access key left from the failed Colab attempts (user, in IAM).
- The Epic 1 EDA CSVs and the Epic 1 `split_config.json` are only in Google Drive (`MyDrive/bq1/eda/`); the LI-Small `split_config.json` here (`artifacts/small/`) includes the window.

---

## 0. Context and ground rules

### Business question
How much can transaction risk scoring reduce AML analyst review volume while retaining a high proportion of laundering-labelled transactions? Output is a ranked risk queue, judged on Recall@K and Precision@K, not accuracy.

### Fixed inputs (do not modify)
| Item | Value |
| --- | --- |
| AWS account / region | g1team5 (941377154820), `us-east-1` |
| Bucket | `s3://is459-aml-g1t5/` |
| Dev Silver table | `is459_aml_silver.li_small_transactions_small_live_03` (6,924,049 rows, 3,565 laundering, 1 to 17 Sep 2022) |
| Final Silver table | `is459_aml_silver.li_medium_transactions_medium_live_01` (31,251,483 rows, 16,041 laundering, 1 to 27 Sep 2022) |
| Silver Parquet root | `s3://is459-aml-g1t5/silver/ibm-aml/versions/<run_id>/` (list it to get the exact sub-path) |
| BQ2 Gold table | `is459_aml_gold.bq2_graph_edges_medium_v1` (28,752,132 edges; excludes self-account rows) |
| Glue role | `AWSGlueServiceRole-is459-aml-g1t5` |

### Silver transaction columns (verify with `printSchema()` in T0.5)
`transaction_id`, `timestamp`, `from_bank`, `from_account`, `to_bank`, `to_account`, `amount_received`, `amount_paid` (both `decimal(23,6)`), `receiving_currency`, `payment_currency`, `payment_format`, `is_laundering` (int 0/1), `sender_account_key`, `receiver_account_key`, sender/receiver `entity_id` and `entity_name`, flags `is_same_account`, `is_same_entity`, `is_same_entity_different_account`, `is_cross_entity`, `is_cross_bank`, `is_cross_currency`, `txn_month`, lineage columns.

### Hard rules
1. **Never** write to, or delete from, `bronze/` or `silver/`. BQ1 writes only under `gold/bq1/` and `models/`.
2. **Never** use `is_laundering` as a feature or to build a feature. It is for training labels and evaluation only.
3. Every rolling or cumulative feature must be **causal**: a row at time T uses only rows with timestamp strictly before T. Timestamps are minute-resolution, so same-minute rows are excluded.
4. Do not sum or compare amounts across currencies. Use within-currency or account-relative features. No FX table.
5. Do not deduplicate transactions.
6. Account keys stay in the Silver format `<bank without leading zeros>|<account>`. Do not rebuild them.
7. No AWS keys in notebook cells, files, or output. Read them from Colab Secrets.
8. **HUMAN GATE:** stop and ask the user before any action marked `[GATE]`. These are paid AWS launches, IAM changes, and decisions that change the evaluation design.
9. Use fixed random seed `42` everywhere.
10. The data is synthetic. Do not describe results as real-world laundering rates.

### Code layout (so Colab code ports to AWS unchanged)
```
bq1/
  features.py      # build_features(df) -> df. Pure PySpark, no I/O.
  split.py         # chronological split helpers
  metrics.py       # recall_at_k, precision_at_k, summary table
  train.py         # rules, LR, XGBoost training
  score.py         # chunked batch scoring
glue_bq1_features.py   # thin Glue wrapper: read Silver, call build_features, write Parquet
notebooks/bq1_small_dev.ipynb
```
In Colab, create the modules with `%%writefile` and import them. Keep all logic in the modules, not in notebook cells.

---

## Epic 0: Environment and data access (Colab)

- [x] **T0.1 [HUMAN]** User adds `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` to Colab Secrets and grants the notebook access.
  - Done when: `userdata.get("AWS_ACCESS_KEY_ID")` returns a value (do not print it).
- [x] **T0.2** Install dependencies: `pyspark`, `boto3`, `awscli`, `xgboost`, `scikit-learn`, `pyarrow`, `pandas`.
  - Done when: all import; versions printed and recorded.
- [x] **T0.3** Set AWS env vars from Secrets, region `us-east-1`. Run `aws sts get-caller-identity`.
  - Done when: account `941377154820` is returned.
- [x] **T0.4** List `s3://is459-aml-g1t5/silver/ibm-aml/versions/small_live_03/` and confirm `_READY` exists. Sync only the transactions Parquet to `/content/data/silver/li_small/transactions/`.
  - Done when: local Parquet files exist; total size printed.
- [x] **T0.5** Start Spark in local mode (`spark.driver.memory=8g`, `spark.sql.session.timeZone=UTC`, `spark.sql.shuffle.partitions=64`). Read the Parquet, print schema.
  - Done when: row count = 6,924,049; `sum(is_laundering)` = 3,565; distinct `transaction_id` = row count. Record any column-name differences from section 0.
- [x] **T0.6** Create the `bq1/` package skeleton with empty modules.

## Epic 1: Split EDA and decision gate (LI-Small)

- [x] **T1.1** Per day: row count, laundering count, laundering rate. Save as `eda_daily.csv` and show a table.
- [x] **T1.2** Laundering rate and counts by `payment_format`, by `payment_currency`, and by each Silver flag.
- [x] **T1.3** Transactions per `sender_account_key` and per `receiver_account_key`: max, p99, p99.9, and the top 10 accounts. This sizes window-partition skew.
- [x] **T1.4** Count rows that share (sender, timestamp) and (receiver, timestamp), to quantify same-minute ties.
- [x] **T1.5** Flag "tail days": any day whose row count is below 10% of the median daily row count.
- [x] **T1.6 [GATE]** Report T1.1 to T1.5 to the user. If tail days exist, propose dropping them from all splits and state the rows and positives lost. Wait for the user's decision.
  - Done when: the usable date window is fixed and written to `split_config.json`.

## Epic 2: Feature engineering v1 (PySpark, `bq1/features.py`)

Shared setup inside `build_features(df)`: add `ts = timestamp cast to long` (epoch seconds); cast both amounts to `double`. Define windows as `partitionBy(key).orderBy("ts").rangeBetween(-secs, -1)` for rolling, and `rangeBetween(Window.unboundedPreceding, -1)` for to-date.

- [x] **T2.1 Transaction features**
  - `hour_of_day`, `day_of_week`, `log_amount_paid = log1p(amount_paid)`
  - Pass through: `payment_format`, `payment_currency`, `receiving_currency`, all six Silver flags
  - Do not add date, day-of-month, or `txn_month`.
- [x] **T2.2 Sender velocity**
  - `snd_out_cnt_1h`, `snd_out_cnt_24h` (partition: `sender_account_key`)
  - `snd_out_amt_24h_ccy`: sum of `amount_paid`, partition `(sender_account_key, payment_currency)`
- [x] **T2.3 Receiver velocity**
  - `rcv_in_cnt_1h`, `rcv_in_cnt_24h` (partition: `receiver_account_key`)
  - `rcv_in_amt_24h_ccy`: sum of `amount_received`, partition `(receiver_account_key, receiving_currency)`
- [x] **T2.4 Counterparty**
  - `pair_prior_cnt`: to-date count, partition `(sender_account_key, receiver_account_key)`
  - `is_new_pair = (pair_prior_cnt == 0)`
  - `snd_distinct_rcv_to_date`: build `is_first_pair_row` with `row_number()` over the pair ordered by `(ts, transaction_id)`, then take a to-date sum of it over the sender window
  - `rcv_distinct_snd_to_date`: same, over the receiver window
  - Do not use `countDistinct` or `collect_set` over windows.
- [x] **T2.5 Behavioural deviation**
  - `snd_prior_txn_cnt`: to-date count over sender
  - `snd_prior_mean_amt_ccy`: to-date mean of `amount_paid`, partition `(sender_account_key, payment_currency)`
  - `amt_ratio_to_snd_mean = amount_paid / snd_prior_mean_amt_ccy` (null when no history; keep null)
  - `snd_secs_since_last_out`: `ts` minus the max prior `ts` over the sender window (null when none)
- [x] **T2.6** Output columns: `transaction_id`, `timestamp`, `ts`, `sender_account_key`, `receiver_account_key`, `is_laundering`, plus all features. No other Silver columns.
- [x] **T2.7** Run on LI-Small, write to `/content/data/features/li_small_v1/` as Snappy Parquet.
  - Done when: output row count = input row count; distinct `transaction_id` unchanged; runtime recorded.
- [x] **T2.8 Causality unit test.** Pick 20 random transactions. For each, recompute `snd_out_cnt_24h`, `pair_prior_cnt` and `snd_distinct_rcv_to_date` by brute-force filter on the raw frame (strictly earlier `ts`) and compare.
  - Done when: 20/20 match on all three.
- [x] **T2.9 Truncation test.** Drop all rows after the 50th-percentile timestamp, rebuild features, and compare with the full run for the surviving rows.
  - Done when: feature values are identical. Any difference means a feature is reading the future.
- [x] **T2.10 (optional v2, only if Epic 5 is done early)** Pass-through features using a long "account events" table: `snd_secs_since_last_in`, `snd_in_cnt_24h`, `rcv_out_cnt_24h`.

## Epic 3: Chronological split (`bq1/split.py`)

- [x] **T3.1** Within the usable window from T1.6, find the timestamps at the 60th and 80th row percentiles. Train: `ts < t60`. Validation: `t60 <= ts < t80`. Test: `ts >= t80`. All rows sharing a timestamp fall on the same side.
  - Done when: per split, rows, positives and prevalence are printed, and each split has at least 300 positives. If not, **[GATE]** propose new boundaries.
- [x] **T3.2** Save boundaries to `split_config.json`.
- [x] **T3.3** Load the feature Parquet into pandas with float32 numerics. Fit on **train only**: per-currency median of `log_amount_paid`, giving `amt_vs_ccy_median`; the category lists for the three categorical columns. Save both in `feature_schema.json`.
- [x] **T3.4** Write `feature_schema.json`: ordered feature names, dtypes, category lists, fitted medians, feature version `v1`.

## Epic 4: Models (`bq1/train.py`, LI-Small)

- [x] **T4.1 Rules baseline.** Candidate indicators: top-lift payment format, `is_new_pair`, `is_cross_currency`, `snd_out_cnt_24h >= train p95`, `amt_ratio_to_snd_mean >= 3`, `rcv_distinct_snd_to_date >= train p95`. Weight = `max(0, ln(train rate with indicator / train base rate))`. Score = sum of weights. Everything is fitted on train only.
  - Done when: weights saved to `rules_v1.json`; test scores produced.
- [x] **T4.2 Logistic Regression.** Train on all train positives plus a 5% random sample of train negatives. Pipeline: one-hot categoricals, median-impute plus missing-indicator for nullable numerics, `log1p` on count features, standard scale, `LogisticRegression(class_weight="balanced", max_iter=1000)`.
  - Done when: scores for full validation and test (natural prevalence) are produced; coefficients saved.
- [x] **T4.3 XGBoost.** Full train set. `tree_method="hist"`, `enable_categorical=True`, `max_depth=6`, `learning_rate=0.1`, `subsample=0.8`, `colsample_bytree=0.8`, `eval_metric="aucpr"`, up to 2000 rounds, early stopping 50 on validation. Try `scale_pos_weight` in {`sqrt(neg/pos)`, `neg/pos`} and keep the better validation PR-AUC.
  - Done when: best iteration, chosen `scale_pos_weight` and validation PR-AUC are recorded.
- [x] **T4.4** XGBoost gain importance table, top 20.
- [x] **T4.5 Leakage audit.** For each feature alone, compute test PR-AUC. Flag any single feature with PR-AUC above 0.5 or any model with test Recall@1% above 0.99.
  - Done when: flagged items are reported to the user. **[GATE]** if anything is flagged.

## Epic 5: Evaluation and model selection (`bq1/metrics.py`)

- [x] **T5.1** Implement `recall_at_k`, `precision_at_k`, `fp_per_tp_at_k`. Rank by score descending, ties broken by `transaction_id`. K is a percentage of test rows.
- [x] **T5.2** Produce one table on the **test** split for Rules, LR and XGBoost at K = 0.1%, 0.5%, 1%, 5%, 10%: alerts reviewed, Recall@K, Precision@K, alert reduction (`1 - K`), FP per TP. Add PR-AUC per model and a random-ranking row.
- [x] **T5.3** Save the workload-versus-detection curve data (recall at 200 K values from 0.01% to 20%) to `curve_small.csv`.
- [x] **T5.4 [GATE]** Report the table and recommend a model. Primary metric is Recall@1%, with 0.1% and 0.5% shown beside it. XGBoost is kept only if it clearly beats LR and Rules at the primary K. User confirms.
- [x] **T5.5** Save artefacts locally and to `s3://is459-aml-g1t5/models/bq1/<model>/v1-small/`: `model.ubj` (or LR pickle), `feature_schema.json`, `rules_v1.json`, `split_config.json`, `metrics.json`, `model_metadata.json` (dataset, run IDs, seed, library versions, hyperparameters, train time).
  - Done when: objects are listed in S3.

## Epic 6: LI-Medium features on AWS Glue

- [x] **T6.1** Write `glue_bq1_features.py`. Args: `--silver_path`, `--out_path`, `--variant`. It reads Silver Parquet, calls `build_features`, writes Snappy Parquet partitioned by `txn_date`, and logs input and output row counts. It imports the same `bq1/features.py`, packaged as a zip.
- [x] **T6.2 [GATE]** Check that `AWSGlueServiceRole-is459-aml-g1t5` can write under `gold/bq1/`. If not, tell the user what policy change is needed. Do not change IAM without approval.
- [x] **T6.3 [GATE]** Upload the script and zip to `s3://is459-aml-g1t5/scripts/bq1/`. Create Glue job `is459_bq1_features`: Glue 5.0, G.1X, 10 workers, timeout 60 min, retries 0, max concurrency 1. User confirms before creation and tells the team.
- [x] **T6.4 [GATE]** Parity run on LI-Small, output `s3://is459-aml-g1t5/gold/bq1/features/li-small-v1/` (about US$0.60).
  - Done when: row count = 6,924,049 and, for 1,000 sampled `transaction_id`s, every feature matches the Colab output.
- [x] **T6.5 [GATE]** Run on LI-Medium, output `s3://is459-aml-g1t5/gold/bq1/features/li-medium-v1/` (estimate US$2 to 3).
  - Done when: status SUCCEEDED; row count = 31,251,483; distinct `transaction_id` = row count; runtime and DPU-hours recorded.
- [x] **T6.6** If the job fails on memory or skew: raise `spark.sql.shuffle.partitions`, then drop the 24h amount-sum features, then report to the user. Do not retry blindly; each run costs money.

## Epic 7: LI-Medium training, scoring and Gold

- [x] **T7.1 [GATE]** User launches a SageMaker notebook instance with about 32 GB RAM (for example `ml.m5.2xlarge`) with read access to `gold/bq1/features/` and write access to `gold/bq1/` and `models/`. Record the start time.
- [x] **T7.2** Copy the `bq1/` package and the training notebook. Load LI-Medium features with float32 numerics.
- [x] **T7.3** Repeat T1.1 and T1.5 on LI-Medium, apply the same tail rule, and rebuild the 60/20/20 split. Save `split_config.json` for Medium.
- [x] **T7.4** Refit the train-only statistics (T3.3), then retrain all three models with the LI-Small hyperparameters. Do not re-tune on test.
- [x] **T7.5** Re-run T5.2 and T5.3 on the LI-Medium test split. These are the headline numbers. Save `metrics.json` and `curve_medium.csv`.
- [x] **T7.6** Save final artefacts to `s3://is459-aml-g1t5/models/bq1/<model>/v1/`.
- [x] **T7.7** Score **all** LI-Medium rows with the selected model, one `txn_date` partition at a time. Write `s3://is459-aml-g1t5/gold/bq1/scores/li-medium-v1/` as Snappy Parquet partitioned by `txn_date`, with columns:
  - `transaction_id`, `source_account_key` (= `sender_account_key`), `target_account_key` (= `receiver_account_key`), `transaction_timestamp`, `risk_score`, `risk_rank` (1 = highest, global), `model_name`, `model_version`, `data_split` (train/validation/test/excluded), `evaluation_is_laundering`
  - Done when: 31,251,483 rows written; `risk_rank` is unique and dense.
- [x] **T7.8** Create the Athena table `is459_aml_gold.bq1_risk_scores_medium_v1` over that path and load partitions.
  - Done when: `SELECT count(*)` = 31,251,483.
- [x] **T7.9** Integration checks in Athena:
  - Join to `is459_aml_gold.bq2_graph_edges_medium_v1` on `transaction_id`: expect 28,752,132 matches (the gap is the 2,499,351 self-account rows).
  - `txn_630dea1dda9482dbd765a966355d6708` exists with the same sender, receiver and timestamp as the BQ2 demo alert.
- [x] **T7.10 [HUMAN]** User stops the SageMaker notebook instance. Record total runtime and cost.

## Epic 8: Handoff

- [x] **T8.1** Write `BQ1_HANDOVER.md`: feature list with definitions, split boundaries, tail-day decision, model comparison table (Small and Medium), selected model and reason, Gold schema, S3 paths, Glue job config, costs, known limits.
- [x] **T8.2** Give the UI owner three ready Athena queries: top-N alert queue with filters, Recall@K and Precision@K at a chosen review budget (test split only), and the workload-versus-detection curve.
- [ ] **T8.3 (deferred until the UI owner confirms the schema)** Per-alert explanation fields: top three SHAP contributions for the top 1% of ranked rows only, written as extra columns or a side table keyed by `transaction_id`.
- [ ] **T8.4 (optional)** HI-Medium robustness run. Not in Bronze yet, so this needs a team decision first.

## Epic 9: Tail-day analysis (separate evaluation of the excluded rows)

**Added 2026-10-10 at the user's request. Started 2026-10-10.** Background: the tail days (Medium 17 to 27 Sep: 1,370 rows, 771 positives, rate about 56%) are dropped from every split (Epic 1 decision, T7.3) and appear in Gold with `data_split = 'excluded'`. They sit after the test window, so the model never saw them and their scores are out of sample. This epic reports how the model ranks them, **without changing the split, the models or the headline numbers**. No retraining, no SageMaker run, no new Glue run. Everything is Athena reads over existing Gold tables plus local analysis.

Scope decisions already made:
- The tail-day rule and the headline test split stay as they are. Epic 9 is a second, separate report.
- Medium is the primary target. Small (7 tail days, 223 rows, 134 positives) is optional (T9.9).
- Rule 2 applies: `evaluation_is_laundering` is used only to evaluate, never as an input.

Method notes (so the tasks below are unambiguous):
- Because tail rows are scored out of sample, rank them against the **test** queue, not against the global `risk_rank` (which mixes in-sample train rows).
- "Test threshold at K" = the `risk_score` of the m-th ranked test row, m = ceil(K x 6,250,304). A tail row would be flagged at K if its score is at or above that threshold. XGBoost scores rarely tie; check and note any ties at the threshold.
- Athena calls need `--region us-east-1` and a per-query result location under `s3://is459-aml-g1t5/gold/bq1/athena-results/` (the workgroup default belongs to another bucket).

- [x] **T9.1 Sanity check the excluded rows.** In Athena on `is459_aml_gold.bq1_risk_scores_medium_v1` run `data_split = 'excluded'`: row count, `sum(evaluation_is_laundering)`, rows and positives per `txn_date`.
  - Done when: 1,370 rows and 771 positives, 11 dates (17 to 27 Sep), matching the figures above. Any mismatch stops the epic and is reported.
- [x] **T9.2 Test thresholds.** For K in {0.1%, 0.5%, 1%, 5%, 10%} compute the test-split `risk_score` threshold (m-th highest score among `data_split = 'test'`, m = ceil(K x 6,250,304)). Save as `tail_thresholds_medium.csv` (K, m, threshold, whether the next score is tied).
  - Done when: 5 thresholds saved; the K = 1% alert count is 62,504 (check).
- [x] **T9.3 Tail rows against the test queue.** For each K, count excluded rows and excluded positives with `risk_score >=` the T9.2 threshold. Compute tail Recall@K (flagged positives / 771) and tail precision (flagged positives / flagged rows).
  - Done when: a 5-row table is saved as `tail_vs_test_queue_medium.csv`. This answers "would the test-time review budget have caught the tail-day laundering?"
- [x] **T9.4 Ranking within the tail set.** Rank the 1,370 tail rows among themselves (score descending, ties by `transaction_id`). Compute Recall@K and Precision@K for K in {1%, 5%, 10%, 25%, 50%} of the tail rows, and tail PR-AUC. Compare with the tail prevalence (about 56%), which is the random baseline here.
  - Done when: `tail_within_medium.csv` saved. Note that precision is high for any ranking because prevalence is high, so judge by lift over 56%.
- [x] **T9.5 Score distributions.** Summaries of `risk_score` (min, p25, median, p75, p95, max) for tail positives, tail negatives, test positives, test negatives. One chart: the four distributions on a log-scaled score axis (read the `dataviz` skill first).
  - Done when: `tail_score_summary_medium.csv` and the chart are saved.
- [x] **T9.6 Sensitivity: what the headline would have been.** Pool test and excluded rows, re-rank by score, and recompute Recall@K, Precision@K and PR-AUC for XGBoost at the usual K grid. Compare with the T7.5 headline (Recall@1% 0.634).
  - Done when: `headline_with_tail_medium.csv` saved with both versions side by side. This quantifies the distortion that justified dropping the days. Do not replace the headline.
- [x] **T9.7 Why the model ranks them where it does (descriptive).** Join excluded rows to `gold/bq1/features/li-medium-v1/` on `transaction_id`. For tail positives and negatives compare the medians of the main features (`snd_out_cnt_24h`, `snd_prior_txn_cnt`, `pair_prior_cnt`, `is_new_pair` share, `payment_format` mix). Also read the model's top gain features from `xgb_importance_top20.csv`.
  - Done when: a short table and 3 to 5 sentences say which features separate tail positives from tail negatives, and whether the tail rows look unlike the normal days. Mark any claim about the generator as inference, not fact.
- [x] **T9.8 Write up.** Add a section to `docs/BQ1_methods_and_decisions.md` ("Tail-day analysis, LI-Medium") with the T9.1 to T9.7 tables and a plain-language reading. Update `BQ1_HANDOVER.md` section 10 (known limits) with the tail-day result. Add a Progress log line per task.
  - Done when: both files updated; every number traces to a saved CSV.
- [ ] **T9.9 (optional) LI-Small repeat.** The Small tail rows (223 rows, 134 positives) are not in `scores_small.parquet`. Score them locally with `artifacts/small/xgboost/model.ubj` (local Spark features in `data/features/li_small_v1/`, filter `ts >= window_end_ts`), then repeat T9.2 to T9.4 against the Small test split.
  - Done when: `tail_*_small.csv` saved. Skip if T9.3 and T9.4 on Medium already answer the question.
- [ ] **T9.10 [GATE] Upload results.** Ask the user before copying the Epic 9 CSVs and chart to `s3://is459-aml-g1t5/models/bq1/xgboost/v1/tail_analysis/` (allowed prefix; the prefix must be listed empty first).
  - Done when: the user approves and the objects are listed in S3, or the user declines and files stay under `artifacts/medium_tail/`.
- [x] **T9.11 [GATE] Report and decide.** Present T9.3, T9.4 and T9.6 to the user. Ask whether the tail-day rule should change (for example, keep dropping them, or report a combined number). Change nothing in the split without an explicit decision.
  - Done when: the user's decision is written in the Progress log.

Dependency order: T9.1 -> T9.2 -> T9.3 and T9.4 and T9.5 (independent) -> T9.6 and T9.7 -> T9.8 -> T9.10 -> T9.11. T9.9 is independent and optional.
Expected cost: Athena scans of a few GB at most, negligible. No Glue, SageMaker or retraining.

---

## Dependency order

```
E0 -> E1 (gate) -> E2 -> E3 -> E4 -> E5 (gate) -> E6 (gates) -> E7 (gates) -> E8
```
E6.1 to E6.3 can be prepared while E4 and E5 run, since they depend only on `features.py` being frozen after T2.9.

## Progress log

Append one line per finished task: task ID, date and time, result, anything that differed from this plan.

- T0.1 2026-10-09: done. Needed two re-entries of the secrets before STS accepted them (earlier values were mismatched).
- T0.2 2026-10-09: done. pyspark 4.0.4, boto3 1.43.110, xgboost 3.4.1, scikit-learn 1.6.1, pyarrow 23.0.1, pandas 2.2.3, aws-cli 1.46.1. Harmless sphinx/docutils pip warning.
- T0.3 2026-10-09: done. Account 941377154820, user g1team5.
- T0.4 2026-10-09: done. `_READY` present. 36 transaction Parquet files, 399.3 MiB, synced to /content/data/silver/li_small/transactions/.
- T0.5 2026-10-09: done. Rows 6,924,049; laundering 3,565; distinct transaction_id = rows. Schema differences vs section 0: all columns listed in section 0 are present. Extra columns: dataset_variant, source_file, ingest_run_id, row_line_no, from_bank_norm, to_bank_norm, both_entities_resolved, entity_resolution_status. `timestamp` is a Spark timestamp type.
- T0.6 2026-10-09: done. /content/bq1/ has empty modules (features, split, metrics, train, score, __init__). /content/notebooks/ created.
- T1.1-T1.5 2026-10-09: done. Outputs in Colab /content/eda/ and Drive MyDrive/bq1/eda/. 7 tail days (11-17 Sep, 223 rows, 134 positives, laundering rate 56-71%). Top sender 70|10042B660 has 222,037 txns; p99.9 is 158. Same-minute ties: 14.6% of rows (sender), 4.8% (receiver).
- T1.6 2026-10-09: done. User chose to drop tail days. Window 2022-09-01 to 2022-09-10 UTC (6,923,826 rows, 3,431 positives). split_config.json written (window only; T3.1 adds t60/t80).
- ENV CHANGE 2026-10-09: Colab session crashed (out of RAM in toPandas of 6.9M rows) and the free-tier quota was low. User approved switching to local Mac (16 GB, 8 cores). Installed openjdk@17 and libomp via Homebrew. Venv at bq1/.venv; data under bq1/data/ (silver, features, eda). Local versions: pyspark 4.0.4, boto3 1.43.110, xgboost 3.4.1, scikit-learn 1.9.1, pyarrow 26.0.0, pandas 3.0.6 (sklearn/pandas/pyarrow differ from the Colab run). Added bq1/spark_session.py (local Spark helper, not used by Glue). Disk is tight (about 6.8 GB free).
- T2.1-T2.6 2026-10-09: done. bq1/features.py. Choices not in plan: time features computed from `ts` in UTC (day_of_week Monday=0); summed 24h amount features and the distinct-counterparty counts are coalesced to 0 when there is no history; amt_ratio_to_snd_mean is null when no history or prior mean is 0.
- T2.7 2026-10-09: done. Local: 117 s (Colab run was 581 s). Rows in = out = 6,924,049; distinct transaction_id unchanged. Features are computed on ALL rows including the 7 tail days (no effect on earlier rows); the tail-day drop is applied at the split step.
- T2.8 2026-10-09: done. 20/20 match on snd_out_cnt_24h, pair_prior_cnt, snd_distinct_rcv_to_date (seed 42). Passed on both the Colab and the local build.
- T2.9 2026-10-09: done. Cutoff ts 1662379920 (50th pct). 3,462,207 surviving rows; no mismatches on any of the 22 features (exact for ints/booleans/strings; doubles within 1e-9 relative).
- T3.1 2026-10-09: done. Window 1 to 10 Sep (6,923,826 rows, 3,431 pos). t60 = 1662471060 (2022-09-06 13:31 UTC), t80 = 1662653160 (2022-09-08 16:06 UTC). train 4,153,991 rows / 1,813 pos (0.0436%); validation 1,385,006 / 827 (0.0597%); test 1,384,829 / 791 (0.0571%). All splits >= 300 positives, no gate.
- T3.2 2026-10-09: done. artifacts/small/split_config.json (also holds the Epic 1 window and tail-day decision; the Epic 1 copy on Drive has the window only).
- T3.3-T3.4 2026-10-09: done. artifacts/small/feature_schema.json, 27 features (17 float32 numeric incl. amt_vs_ccy_median, 7 int8 flags, 3 categoricals). Fitted on train only. Deviations/choices: amt_vs_ccy_median defined as log_amount_paid minus the train median log_amount_paid for that payment_currency (global train median if unseen); unseen categories map to "__other__" (none occur outside train). Nullable numerics: snd_prior_mean_amt_ccy, amt_ratio_to_snd_mean, snd_secs_since_last_out. Code: bq1/split.py, scripts/epic3_split.py.
- T4.1 2026-10-09: done. artifacts/small/rules_v1.json. Top-lift format ACH (min support 1,000 rows). Weights: ACH 1.878, snd_out_cnt_24h>=p95 1.142, is_new_pair 0.855, rcv_distinct_snd>=p95 0.644, amt_ratio>=3 0.169, is_cross_currency 0 (zero train positives, rate 0 gives weight 0).
- T4.2 2026-10-09: done. LR on 1,813 train positives + 207,609 sampled negatives (5%, seed 42). Coefficients in lr_coefficients.csv, model in lr_model.pkl. log1p applied to 13 nonneg count/amount/gap columns before imputing and scaling.
- T4.3 2026-10-09: done. spw=sqrt(neg/pos)=47.9 chosen (val PR-AUC 0.2436, best iteration 346) over neg/pos=2290 (val PR-AUC 0.1739, best iteration 404). Early stopping used the validation split (not test).
- T4.4 2026-10-09: done. xgb_importance_top20.csv. Top gain: is_new_pair 29%, payment_format 14%, is_same_account 8%, is_cross_currency 6%, pair_prior_cnt 6%.
- T4.5 2026-10-09: done, nothing flagged. Best single feature test PR-AUC 0.0069 (pair_prior_cnt) vs prevalence 0.00057. Test Recall@1%: Rules 0.161, LR 0.407, XGB 0.612 (none above 0.99). No gate. Pulled forward T5.1 (bq1/metrics.py) to compute Recall@1%.
  Scores for validation and test saved in artifacts/small/scores_small.parquet. Run log in artifacts/small/epic4.log.
- TUNING (user request, between E4 and E5) 2026-10-09: XGBoost tuned on VALIDATION only (artifacts/small/xgb_tuning_log.csv, xgb_tuned_params.json). Staged search: scale_pos_weight {5,10,20,47.9}, then max_depth {4,8}, min_child_weight {5,20}, reg_lambda {10,50}; adopt only on >=1% relative val gain. Chosen: scale_pos_weight=5, max_depth=8, min_child_weight=20 (others default). Val PR-AUC 0.2438 -> 0.2662. Test (read once): PR-AUC 0.2091 -> 0.2191, Recall@1% 0.612 -> 0.632, Precision@0.1% 0.1545 -> 0.1552. Small gain, near noise at 791 test positives.
- T2.10 2026-10-09: done, NOT adopted. Added variant="v2" to build_features (v1 output unchanged, default) with passthrough_features(): snd_in_cnt_24h, snd_secs_since_last_in, rcv_out_cnt_24h. Local build computed on a narrow table (a first attempt on the wide table ran out of disk, 6.6 GB free). Brute-force check 40/40 (20 random + 20 with non-zero inbound history; scripts/t210_check.py); truncation test on 3,462,207 rows: no mismatches. v1 vs v2 with XGBoost (artifacts/small/v1_vs_v2_xgb.csv): tuned v1 val 0.2662 / test PR-AUC 0.2191 / R@1% 0.632; tuned v2 val 0.2675 / test 0.2195 / R@1% 0.643. No meaningful gain, so v1 stays the production feature set. features.py v2 path kept (Glue --variant) but unused.
- T5.1 2026-10-09: done (pulled forward during Epic 4). bq1/metrics.py: recall_at_k, precision_at_k, fp_per_tp_at_k, pr_auc, summary_table. Rank by score descending, ties by transaction_id ascending; K is a share of the rows ranked.
- T5.2 2026-10-09: done. artifacts/small/eval_table_test.csv. Test 1,384,829 rows, 791 positives. Recall@1%: Rules 0.161, LR 0.407, XGBoost (tuned) 0.632. Recall@0.1%: 0.011 / 0.138 / 0.272. Recall@0.5%: 0.057 / 0.285 / 0.525. PR-AUC 0.0093 / 0.0342 / 0.2191 (random 0.0006). Added a 95% recall interval column (about +/-0.03). Rules precision is flat at low K because of heavy score ties.
- T5.3 2026-10-09: done. artifacts/small/curve_small.csv (200 K values, geometric from 0.01% to 20%, recall and precision for all three models plus random).
- T5.4 2026-10-09: GATE reached, waiting for the user. Recommendation: XGBoost (tuned).
- Docs 2026-10-09: docs/BQ1_methods_and_decisions.md written (methods, deviations, problems, results to date). Also added artifacts/small/model_metadata.json and metrics.json; the earlier xgboost/xgb_summary.json was replaced with the tuned summary (old one kept as xgb_summary_baseline_superseded.json).
- T5.4 2026-10-09: done. User approved XGBoost (tuned) as the selected model (primary metric Recall@1%: 0.632 vs LR 0.407, Rules 0.161).
- T5.5 2026-10-09: done. 18 objects under s3://is459-aml-g1t5/models/bq1/{xgboost,logistic_regression,rules}/v1-small/ (target prefix was empty first). Each folder holds feature_schema.json, rules_v1.json, split_config.json, metrics.json, model_metadata.json; xgboost also has model.ubj (684 KiB), logistic_regression has lr_model.pkl and lr_coefficients.csv. The rules folder has no separate model file because rules_v1.json is the model. Plan said "model.ubj (or LR pickle)"; the pickle depends on scikit-learn 1.9.1. Only models/ was written; bronze/ and silver/ untouched.
- T6.1 2026-10-09: done. glue_bq1_features.py (args --silver_path, --out_path, --variant; getResolvedOptions on Glue, argparse locally). Reads Silver Parquet, calls build_features, adds txn_date (UTC date), repartitions by txn_date, writes Snappy Parquet partitioned by txn_date, logs input and output row counts and fails if they differ. Guard: out_path must contain /gold/bq1/ and must not contain /bronze/ or /silver/. "--variant" is read as the FEATURE variant (v1 default, v2 optional); the plan did not say which, so confirm. dist/bq1.zip (bq1/__init__.py + bq1/features.py only) built by scripts/package_glue.sh; not uploaded (T6.3 is gated).
  Local dry run matching Glue 5.0 (Python 3.11.13, PySpark 3.5.4 in .venv-glue, importing from the zip, run outside the project dir): input 6,924,049 = output 6,924,049; 17 txn_date folders. Compared against the Spark 4.0.4 v1 features for ALL rows and ALL features (scripts/compare_glue_local.py): identical (doubles at 1e-9 relative). The unmodified wrapper failed once with Java OOM because bare local PySpark defaults to 1 GB driver memory; fixed in the test setup via PYSPARK_SUBMIT_ARGS (--driver-memory 8g), not in the script (Glue sets memory by worker type). Local test output kept at data/gold/bq1/features/li-small-v1/ (582 MB; safe to delete).
- Cleanup 2026-10-09: deleted data/gold/ (local Glue dry-run output) and .venv-glue/ with user approval.
- T6.2 2026-10-09: checked (read-only IAM calls + policy simulator, nothing changed). RESULT: the role CANNOT write under gold/bq1/ and cannot read scripts/bq1/. Role AWSGlueServiceRole-is459-aml-g1t5 has: managed AWSGlueServiceRole; inline IS459SilverETLScopedAccess (read bronze li-small/li-medium, read+write silver/ibm-aml/*); customer-managed ...-EZCRC-s3Policy (get+put on bronze li-medium transactions/accounts). Simulator: GetObject silver = allowed; PutObject gold/bq1/*, DeleteObject gold/bq1/*, GetObject scripts/bq1/* = implicitDeny. ListBucket for gold/bq1 and scripts/bq1 is also not granted (the only ListBucket statement is conditioned on bronze/silver prefixes; read from the policy, the simulator call for it failed on argument syntax). Proposed fix in docs/iam_bq1_glue_policy.json (3 statements, scoped to gold/bq1/* and scripts/bq1/* only). NOT APPLIED: waiting for user approval.
- T6.2 2026-10-09: done. User approved option 1; applied inline policy IS459BQ1FeaturesAccess (docs/iam_bq1_glue_policy.json) to AWSGlueServiceRole-is459-aml-g1t5 with aws iam put-role-policy. Verified with the policy simulator: allowed = GetObject silver, PutObject/GetObject/DeleteObject gold/bq1/*, GetObject scripts/bq1/*, ListBucket with prefixes gold/bq1/ and scripts/bq1/. Still implicitDeny = gold/bq2/*, gold/other/*, scripts/other/*, models/*. Rollback: aws iam delete-role-policy --role-name AWSGlueServiceRole-is459-aml-g1t5 --policy-name IS459BQ1FeaturesAccess. The AWS CLI has no default region here, so Glue commands need --region us-east-1.
- T6.3 prep 2026-10-09: nothing uploaded or created yet. Facts: job name is459_bq1_features does not exist; another job is459_silver exists (not ours); s3://is459-aml-g1t5/scripts/ is empty; LI-Medium Silver transactions at silver/ibm-aml/versions/medium_live_01/li_medium/transactions/ (36 objects, 1.6 GiB, _READY present). Proposed job definition: docs/glue_job_is459_bq1_features.json. Waiting for user confirmation (gate).
- T6.3 (part 1) 2026-10-09: user approved the upload. Uploaded s3://is459-aml-g1t5/scripts/bq1/glue_bq1_features.py (3.2 KiB) and bq1.zip (2.2 KiB). Glue job NOT created yet: waiting for the user's explicit go-ahead on creation and on telling the team. The user asked where --variant appears (answered: wrapper args/validation/app name/log, build_features(), job default arg).
- T6.3 2026-10-09: done. User approved creation (will tell the team afterwards). Created Glue job is459_bq1_features in us-east-1: Glue 5.0, G.1X x 10, timeout 60 min, 0 retries, max concurrency 1, role AWSGlueServiceRole-is459-aml-g1t5, script s3://is459-aml-g1t5/scripts/bq1/glue_bq1_features.py, --extra-py-files bq1.zip, default --variant v1 (user confirmed v1 as the production variant). No run started, so no cost yet. Existing job is459_silver untouched. Rollback: aws glue delete-job --region us-east-1 --job-name is459_bq1_features.
- T6.4 2026-10-10 (run started 2026-10-09 23:54 +08): done, SUCCEEDED. Job run jr_f421ebdc7ba98bf74436f35ac75f8a9202db9c0cee57415dde9525b58b8e976e, args --silver_path small_live_03/li_small/transactions/, --out_path s3://is459-aml-g1t5/gold/bq1/features/li-small-v1/, --variant v1. Execution 182 s (about 3 min), 10 x G.1X, 1,820 DPU-seconds = 0.51 DPU-hours; estimated cost about US$0.22 at US$0.44 per DPU-hour (estimate, not from billing data; the plan estimated US$0.60). Job log: input_rows=6,924,049, output_rows=6,924,049, "row counts match". Output: 17 objects (one parquet per txn_date, 2022-09-01 to 2022-09-17), 581.7 MiB; Glue 5.0 wrote no _SUCCESS marker (the local dry run did). Parity: downloaded the full output and compared ALL 6,924,049 rows and ALL features with the local Colab/Mac features (scripts/compare_glue_local.py): identical (doubles at 1e-9 relative); stronger than the 1,000-row sample in the plan. Temporary local copy deleted.
- BUDGET 2026-10-09: AWS Budgets shows "My Monthly Cost Budget" US$250/month for the whole account; actual spend US$4.63 so far in October (last updated 19:45 +08, lags a few hours), forecast US$11.01. Excludes credits/refunds.
- T6.5 2026-10-10: done, SUCCEEDED. User approved. Run jr_a0e6231f9517848988339cef53a85ddc766e92b5443da9aa30429d09b912e796 (2026-10-10 00:19 to 00:24 +08), args --silver_path medium_live_01/li_medium/transactions/, --out_path s3://is459-aml-g1t5/gold/bq1/features/li-medium-v1/, --variant v1; the output prefix was empty beforehand. Execution 319 s (5.3 min), 10 x G.1X, 3,196 DPU-seconds = 0.89 DPU-hours, estimated cost about US$0.39 at US$0.44 per DPU-hour (estimate; the plan estimated US$2 to 3). No retries, no memory or skew problem, so T6.6 not triggered. Checks: job log input_rows = output_rows = 31,251,483 "row counts match"; independently from the downloaded output: 27 files (one per txn_date, 2022-09-01 to 2022-09-27), rows 31,251,483, distinct transaction_id 31,251,483 (equals rows), sum(is_laundering) 16,041 (matches Silver). Output 2.6 GiB. Temporary local copy deleted. (pyarrow's own S3 client timed out on DNS once; the AWS CLI download worked.)
- T6.6 2026-10-10: not needed (no failure).
- T7.1 2026-10-10: done. User chose SageMaker and asked me to create the notebook. Created `is459-bq1-epic7` in us-east-1 at 21:05 +08: ml.m5.2xlarge, 30 GB volume, platform notebook-al2023-v1 (notebook-al2-v3 was rejected), role `SageMakerExecutionRole` (existing; has AmazonS3FullAccess, so broader than the plan needed; no IAM change made; writes kept to gold/bq1/ and models/bq1/). InService afterwards. Billing ~US$0.46/hour until stopped at T7.10 (record total runtime and cost then).
- T7.2 2026-10-10: done. Code in `s3://is459-aml-g1t5/scripts/bq1/bq1_epic7.zip` (bq1/ modules + scripts/epic7_run.py with stages split, train, score; bq1/score.py written). Run on the notebook in ~/SageMaker/bq1_work (features synced there, 27 partitions). Stage logs upload to models/bq1/epic7_logs/. First split run was killed (out of memory loading 31M transaction_id strings); fixed by loading only ts and label for the split stage, loading no ids for training except the test rows' ids (tie-break), and using a numeric (hi, lo) tie-break key for scoring (checked equal to the string rule on tied scores).
- T7.3 2026-10-10: done. Tail days 17 to 27 Sep (11 days, under 10% of median daily rows 814,076; 1,370 rows, 771 positives dropped); window 2022-09-01 to 2022-09-16 UTC, 31,250,113 rows, 15,270 positives. t60=1662755880 (2022-09-09 20:38 UTC), t80=1663133880 (2022-09-14 05:38 UTC). train 18,748,707 rows / 8,137 pos (0.0434%); validation 6,251,102 / 4,120 (0.0659%); test 6,250,304 / 3,013 (0.0482%). No gate (all splits >= 300 positives). split_config.json is on the notebook (uploaded to models/bq1/*/v1/ at the train stage).
- T7.4 2026-10-10: done. Train stage on the notebook, 615 s total (load 53 s; LR 35 s; XGBoost 359 s). LI-Small settings, no re-tuning on test: XGBoost scale_pos_weight=5, max_depth=8, min_child_weight=20, early stopping on validation: best iteration 95, val PR-AUC 0.2366. LR on 8,137 train positives + 937,028 sampled negatives (5%). Rules weights refitted on train (ACH 1.782, is_new_pair 1.156, snd_out_cnt_24h>=p95 1.187, amt_ratio>=3 0.355, rcv_distinct_snd>=p95 per rules_v1.json, is_cross_currency 0). day_of_week gain share 1.9% (was 2.6% on Small), so no sign of date proxying. Leakage audit: best single feature test PR-AUC 0.0089 (amt_ratio_to_snd_mean) vs prevalence 0.00048; nothing flagged, no model has Recall@1% above 0.99.
- T7.5 2026-10-10: done. LI-Medium test split (6,250,304 rows, 3,013 positives, prevalence 0.048%). Recall@1% (primary): Rules 0.188, LR 0.446, XGBoost 0.634 (95% interval about +/-0.017). Recall@0.1%: 0.009 / 0.183 / 0.302. Recall@0.5%: 0.080 / 0.329 / 0.561. Recall@5%: 0.637 / 0.734 / 0.777. Recall@10%: 0.689 / 0.885 / 0.928. Precision@1%: 0.9% / 2.2% / 3.1%; Precision@0.1%: 0.4% / 8.8% / 14.6%. PR-AUC: Rules 0.0087, LR 0.0400, XGBoost 0.2394 (random 0.0005). XGBoost still clearly beats LR and Rules at the primary K, as on Small (0.632 there). Files: eval_table_test_medium.csv, curve_medium.csv, metrics.json (in models/bq1/xgboost/v1/ and the notebook). Synthetic data: not real-world laundering rates.
- T7.6 2026-10-10: done. Artefacts under s3://is459-aml-g1t5/models/bq1/{xgboost,logistic_regression,rules}/v1/ (targets were empty beforehand): xgboost has model.ubj (1.1 MiB), feature_schema/rules_v1/split_config/metrics/model_metadata plus xgb_importance_top20, eval_table_test_medium, curve_medium, eda_daily_medium and the leakage audit CSVs; logistic_regression has lr_model.pkl and lr_coefficients.csv; rules has the five common JSON files. Stage logs in models/bq1/epic7_logs/. Selected model stays XGBoost (approved at T5.4; Medium confirms it).
- T7.7 2026-10-10: done. Score stage on the notebook: 31,251,483 rows scored in 128 s, whole stage 292 s. Global risk_rank checked dense and unique (1 to 31,251,483) before writing. Output s3://is459-aml-g1t5/gold/bq1/scores/li-medium-v1/: 27 files (txn_date=2022-09-01 to 2022-09-27), 1.7 GiB, Snappy Parquet, columns as in the plan (transaction_timestamp written as microsecond timestamp for Athena). Model xgboost v1. data_split counts: train 18,748,707 / validation 6,251,102 / test 6,250,304 / excluded (tail days) 1,370. Tie-break uses a numeric (hi, lo) key of the hex transaction_id, equal to the string order.
- T7.8 2026-10-10: done. DDL in docs/athena_bq1_risk_scores.sql; table is459_aml_gold.bq1_risk_scores_medium_v1 (partition txn_date date), MSCK REPAIR added 27 partitions. count(*) = 31,251,483; distinct transaction_id = 31,251,483; risk_rank min 1, max 31,251,483, distinct 31,251,483; sum(evaluation_is_laundering) = 16,041. Athena query results go to s3://is459-aml-g1t5/gold/bq1/athena-results/ (the primary workgroup's default result bucket belongs to another bucket, so I overrode it per query to stay inside gold/bq1/).
- T7.9 2026-10-10: done. Join to is459_aml_gold.bq2_graph_edges_medium_v1 on transaction_id: 28,752,132 matches (expected); for all 28,752,132 the sender, receiver and timestamp are equal and evaluation_is_laundering is equal. txn_630dea1dda9482dbd765a966355d6708 exists: sender 120075|81294D7A0, receiver 15126|8127C1990, timestamp 2022-09-03 12:27:00, risk_rank 6,142, data_split train; identical in BQ2.
- T7.10 2026-10-10: done. Notebook is459-bq1-epic7 launched 21:05:01 +08, stop command issued 22:30:58 (user asked me to run it), status Stopped 22:31:50. Billed InService time about 1 h 26 min = about 1.43 h x US$0.46 = about US$0.66 (estimate at the on-demand rate, not from billing data). The stopped instance still keeps its 30 GB volume (small ongoing storage charge, roughly US$0.14 per day at standard rates; estimate) until the instance is deleted; it holds the downloaded features and scores_out copies, all of which are also in S3. Delete the instance when no longer needed (user decision): aws sagemaker delete-notebook-instance --region us-east-1 --notebook-instance-name is459-bq1-epic7 (it must be Stopped first).
- T8.1 2026-10-10: done. BQ1_HANDOVER.md written (sections: deliverable, Gold schema, how to read the table, split and tail-day decision, feature definitions, Small and Medium model tables, selected model and reason, S3 paths, Glue job config, costs, known limits, open items). All numbers copied from the Medium/Small logs and Athena checks above; costs are estimates. T8.2 not started (user asked for 8.1 only).
- T9.1 2026-10-10: done. Athena on bq1_risk_scores_medium_v1, data_split = 'excluded': 1,370 rows, 771 positives, 11 dates (2022-09-17 to 2022-09-27). Per date rows/positives: 17th 380/218, 18th 353/198, 19th 244/140, 20th 211/114, 21st 73/41, 22nd 65/34, 23rd 16/9, 24th 11/7, 25th 12/7, 26th 1/1, 27th 4/2. Matches the Epic 1/T7.3 figures; no mismatch.
- T9.2-T9.4, T9.6 2026-10-10: done (T9.5 numbers done, chart pending). Script scripts/epic9_tail.py (log artifacts/medium_tail/epic9_run.log); all ranking arithmetic runs in Athena (rank rule: score desc, ties by transaction_id) because downloading 440 MB of scores was too slow (a hung download also produced a corrupt CSV, deleted; the big Athena result object was removed from gold/bq1/athena-results/). CSVs in artifacts/medium_tail/.
  T9.2 test thresholds (risk_score of the m-th test row): K 0.1% m=6,251 thr 0.2354; 0.5% m=31,252 thr 0.0501; 1% m=62,504 thr 0.01928; 5% m=312,516 thr 0.00766; 10% m=625,031 thr 0.00498. No ties at any threshold; 1% alert count 62,504 as expected.
  T9.3 tail rows against the test queue (771 tail positives, 1,370 rows): flagged at the test top-0.1% threshold: 761 rows / 693 positives (tail recall 0.899, precision 0.911); 0.5%: 838 / 748 (0.970); 1%: 864 / 760 (0.986, precision 0.880); 5%: 887 / 764 (0.991); 10%: 903 / 766 (0.994, precision 0.848). So the test-time review budget would have flagged 63% of tail rows at 1% and caught 98.6% of tail positives.
  T9.4 within the tail (prevalence 0.563, tail PR-AUC 0.956): top 1% (14 rows) precision 1.00, top 10% (137) precision 1.00, top 25% (343) precision 0.971, recall 0.432, top 50% (685) precision 0.912, recall 0.811. Lift over prevalence 1.6 to 1.8x. High precision partly reflects the 56% base rate.
  T9.5 score summaries (tail exact; test quartiles/p95 from Athena approx_percentile): tail positives median 0.94 (p25 0.686), tail negatives median 0.0014 (p95 0.84), test positives median 0.086, test negatives median 0.000026.
  T9.6 headline vs pooled test + tail: test only (recomputed in SQL, matches T7.5) Recall@1% 0.6343, Precision@1% 0.0306, PR-AUC 0.2394; pooled (6,251,674 rows, 3,784 positives) Recall@1% 0.7051, Precision@1% 0.0427, PR-AUC 0.3849; Recall@0.1% 0.302 -> 0.414; Recall@10% 0.928 -> 0.941. The tail rows are easy for the model, so including them inflates every metric (Recall@1% +0.07, PR-AUC +0.15), which is the distortion that justified dropping them. Headline numbers unchanged.
- T9.5 2026-10-10: done. artifacts/medium_tail/tail_score_summary_medium.csv, tail_score_percentiles_medium.csv and the chart tail_score_distributions_medium.png (scripts/epic9_chart.py). Read the dataviz skill first; colour pair blue/orange validated with validate_palette.js (all checks pass), line style marks tail vs test; rendered and inspected, no label collisions. Installed matplotlib 3.11.2 into the project .venv for this. Test-curve percentiles are Athena approx_percentile (approximate); tail curves exact. The first chart title said tail negatives score "very low"; corrected after checking (tail negatives score higher than test negatives).
- T9.7 2026-10-10: done. Created a read-only external Athena table is459_aml_gold.bq1_features_medium_v1 over gold/bq1/features/li-medium-v1/ (metadata only, DDL docs/athena_bq1_features.sql; 31,251,483 rows, 16,041 positives, 27 partitions) so the tail rows could be joined to their features (scripts/epic9_features.py). Findings: all tail rows are ACH (test negatives 12.7%, test positives 69.5%); tail negatives are 82.3% same-account cross-currency, tail positives 96.4% new pair and 99.6% cross-entity with 0.4% same-account; median snd_out_cnt_24h is 0 in the tail (5 test negatives, 2 test positives). The separation comes from is_same_account (12% of gain) and is_new_pair (9%), not payment_format (20%, constant in the tail). Statement about the generator is marked as inference.
- T9.8 2026-10-10: done. Added section 13 "Tail-day analysis, LI-Medium" to docs/BQ1_methods_and_decisions.md (tables for T9.1 to T9.7 plus a plain reading) and a tail-day bullet in BQ1_HANDOVER.md section 10 (plus a note about the new helper table in section 7). Every number traces to a CSV in artifacts/medium_tail/.
- T9.9 2026-10-10: not done on purpose. The plan says to skip if T9.3 and T9.4 on Medium already answer the question; they do. LI-Small tail (223 rows, 134 positives) can be added if the user wants it.
- T9.10 2026-10-10: GATE reached, waiting for the user (upload of Epic 9 files to S3 not done).
- T9.11 2026-10-10: done. User decision: keep excluding the tail days (rule unchanged, headline stays test-only) and keep the findings documented. Documented in docs/BQ1_methods_and_decisions.md section 13, BQ1_HANDOVER.md section 10 and artifacts/medium_tail/. Split, models and Gold scores unchanged.
- T8.2 2026-10-10: done. docs/BQ1_athena_queries.sql with three queries (edit only the params block): (1) top-N alert queue with filters (split, txn_date range, min score, optional account key matching sender or receiver, top_n; queue_position counts within the filtered set); (2) Recall@K and Precision@K at a chosen budget (share of test rows or a fixed alert count, several budgets in one run), test split only; (3) workload-versus-detection curve, 200 budgets geometric from 0.01% to 20%, test split only. Tested in Athena: Q2 reproduces the T7.5 test table exactly (Recall@0.1% 0.3024, 0.5% 0.5606, 1% 0.6343, 5% 0.7766, 10% 0.9280); Q3 matches models/bq1/xgboost/v1/curve_medium.csv (200 of 200 alert counts equal, recall difference about 1e-16); Q1 checked for rank order, positions, and three filter variants (account filter finds the demo alert at global rank 6,142; min-score and date filters; excluded split). Scans: Q1 about 0.4 GB, Q2 and Q3 about 40 MB, a few seconds each. Finding added to the SQL header and BQ1_HANDOVER.md section 3: the global top of risk_rank is a valid rank but a mixed list, not a fair queue (top 1,000: 305 excluded tail, 277 validation, 261 train, only 157 test), so a fair queue or any evaluation must filter on data_split (wording softened after the user's question: the rank is correct as specified, tail rows are included in the table with data_split = 'excluded'). HANDOVER section 3 and 11 now point to the file.
- T8.2 follow-up 2026-10-10: user decided NOT to change the Gold table (no rank_within_split column). Rewrote the header of docs/BQ1_athena_queries.sql so the queries explain themselves: what the table holds, what risk_score and the global risk_rank are, a table of the four data_split values (rows, dates, what each is, whether it is fair for performance), why the plain global rank mixes them (global top 1,000: 157 test, 305 excluded, 277 validation, 261 train), a "if you want X, do Y" guide, queue_position vs risk_rank, what Recall@K and Precision@K mean and why precision is low, plus a short "why test only" note above each query. Re-ran all three queries after the edit: they still succeed (Q1 100 rows, Q2 6 rows, Q3 200 rows).
- HANDOVER trim 2026-10-10: user asked to remove the internal open items from BQ1_HANDOVER.md section 11 and keep a question for the UI owner. Section 11 is now "Questions for the UI owner": five questions for T8.3 (want explanations?, extra columns vs a side table keyed by transaction_id (BQ1 recommends the side table), which rows (top 1% of the test queue = 62,504 rows, or other), format (feature name, signed contribution, plain sentence), anything else needed in the score table). The removed internal items are still open for the user and are tracked here, not in the handover: tell the team about the Glue job and IAM policy; deactivate the unused IAM key; decide whether to delete the stopped notebook is459-bq1-epic7; Epic 1 EDA CSVs only in Google Drive. T8.3 stays deferred until the UI owner answers.
- HANDOVER rewrite 2026-10-10: user asked to make BQ1_HANDOVER.md relevant for teammates, explain what was done clearly and leave out details not relevant to their parts. Rewritten as 8 sections: one-paragraph summary; what you can use (table, columns, queries); what was done (5 plain steps); how to read the table (data_split table, global-rank caveat, tail days); how well it works (one Medium table, 4 rows); shared AWS resources created or changed; known limits; questions for the UI owner; where to find more. Removed from the handover (still in docs/BQ1_methods_and_decisions.md and this log): the 27-row feature definition table, exact split timestamps, the LI-Small results table, rules weights, XGBoost/LR hyperparameters, importance table, scikit-learn/pickle notes, the model artefact listing, the Glue re-run command and job config, the cost table and rates, the SageMaker role note, and the internal open items. Kept because teammates share the account: the Glue job, the IAM policy added to the shared Glue role, the S3 prefixes BQ1 writes to, the extra Athena tables, and the cost total. Wording fixes after re-reading: single-feature leakage check no longer called "near random" (best single feature PR-AUC 0.009 vs 0.239 for the full model); tail-day decision attributed to BQ1; IAM line names gold/bq1/.
- HANDOVER 2026-10-10: user asked to add the feature definition table back. Added a "Feature definitions" subsection (plain-language table of all 26 stored features plus amt_vs_ccy_median, and a note on causality, currencies and empty values) after section 2 of BQ1_HANDOVER.md; step 2 now points to it.
