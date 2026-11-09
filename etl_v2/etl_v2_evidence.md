# IS459 G1T5: ETL v2 Verification Evidence

All checks were run against the live AWS account (us-east-1) on 17 Oct 2026 (UTC) with Athena, Glue, Step Functions and S3 result files. Raw run results are in `s3://is459-aml-g1t5/results/etl-v2/` (`gold-commit/`, `bq1/staged/`, `bq2-staged/`, `live-audit/`, `live-publish/`, `stream/`). The Athena checks for any day can be rerun with `./validate_day.sh <day>`; the outputs for 15 and 16 Sep are kept as `logs/validate15.log` and `logs/validate16.log` in the team CloudShell.

## Checklist (plan items, with graph features paused)

| # | Check | Result | Evidence |
|---|---|---|---|
| 1 | Backfill matches v1 | PASS | 1 to 13 Sep: v2 Silver rows, unique IDs and exact decimal `amount_paid` totals equal v1 for every day (24,511,248 rows) |
| 2 | Stream lands every row | PASS | 14 Sep 1,891,265 rows, 15 Sep 1,891,498 rows, 16 Sep 2,956,102 rows; every row content-verified against the pinned source export, 0 failed records; stream and Firehose deleted after each session. On 16 Sep the replay job sent all 29,562 chunks but reached its own deadline before its landing check finished, so the exact reconcile job proved the landing instead (`RECOVERED_LANDING_VERIFIED`: 29,562 / 29,562 chunks, 2,956,102 rows and unique IDs, exact Decimal totals, full 33-column match) |
| 3 | Repeat safety | PASS | Every published day has unique IDs = rows (no duplicates). Gold commit is idempotent: per-row `applied_load_ids` guard plus ledger check; Gold partitions are overwritten, never appended |
| 4 | Clean load passes and publishes | PASS | 15 and 16 Sep: 11/11 Glue DQ rules passed, `main` fast-forwarded by the pipeline |
| 5 | Bad load is blocked | PASS | Broken file stayed on branch `audit_broken_20261015` (+2 rows), never reached `main`; execution `quality-fail-20261015` ended in NotifyFailure → Fail. Email alerts go to the subscribed address |
| 6 | Time travel | PASS | `FOR VERSION AS OF 5430669997875323762` returns 26,402,513 rows (before 15 Sep); `main` returned 31,250,113 after 16 Sep and 31,250,493 after 17 Sep |
| 7 | Automatic run | PASS | 15 Sep: full Step Functions run green end to end in 14 min 48 s. 16 Sep: started by the EventBridge schedule (execution `016ad3b6-1815-4aac-9d19-1a2020d02a4c`), green end to end in 19 min 12 s; schedule set back to DISABLED afterwards |
| 8 | Rebuilt features and scores match v1 | PASS | All 26 features within 1e-6 (+1e-9 × value) of v1 on every row of 14, 15 and 16 Sep (0 mismatches each day). Scores: 11 / 10 / 12 rows per day differ by at most 0.00077 (float32 rounding at a tree split), inside the recorded allowance (≤ 0.01% of rows, each ≤ 0.01). Risk ranks identical |
| 9 | Graph features | PAUSED | Team decision (leakage risk). BQ2 edge table is still produced |
| 10 | v2 outputs exist | PASS | Gold v2 scores and features for 14, 15 and 16 Sep (row counts equal v1); BQ2 v2 edges identical to v1 in both directions (1,854,285, 1,854,488 and 2,901,609); Alert Queue and Case Graph queries run on v2 |
| 11 | Evidence recorded | PASS | Latency, incremental-vs-rebuild cost and file-size checks below |
| 12 | Budget | PASS | About US$6 to 7 for all ETL v2 work, against a US$30 target (see Spend) |
| 13 | Teardown | PASS | After the 16 Sep run: no Kinesis streams, no Firehose delivery streams, schedule DISABLED, 0 Glue job runs and 0 Step Functions executions running |
| 14 | Daily lane reads the raw CSV (24 Oct) | PASS | Parse parity on 14, 15, 16 Sep on all 33 columns; 17 Sep end to end in 13 min 20 s, BQ1 exact against v1 (see CSV cutover) |

## BQ1 state correctness

Each day's features are built from running totals that the Gold commit step advanced with the day before. On 15 and 16 Sep, every one of the 26 features matched v1 on every row (1,891,498 and 2,956,102 rows), so the state update is exact. After each commit, every state table sums to the ledger total (26,402,513 after 14 Sep, 28,294,011 after 15 Sep, 31,250,113 after 16 Sep).

## Latency (transaction to Alert Queue)

Since 24 Oct the daily lane is plain batch: once a day's transactions are in the raw Bronze CSV, one Step Functions run reads that day straight from the CSV and publishes its alerts. There is no replay step any more. Per-transaction scoring in seconds is the separate real-time lane.

| Stage | 15 Sep (1.89M rows) | 16 Sep (2.96M rows) |
|---|---|---|
| Day available → alerts in the Gold v2 Alert Queue (Step Functions run: audit + DQ, publish, BQ1 + BQ2, Gold commit) | 14 min 48 s | 19 min 12 s |
| Stream transport on one Kinesis shard (how 14 to 16 Sep were loaded, now retired) | about 1 hour | about 1 hour 40 min |

The CSV audit step reads the whole 3 GB file and still finishes in about 4 to 5 minutes for a full day (see the CSV cutover table below), so a day's alerts land about 15 to 20 minutes after the day is available.

## Cost of keeping the Alert Queue current

| Approach | Glue DPU-seconds | US$ |
|---|---|---|
| Incremental: one day through the whole pipeline (15 Sep) | 3,803 | 0.47 |
| Rebuild from scratch (v1 Silver + v1 BQ1 features only, scoring and BQ2 not included) | 11,600 | 1.42 |

Adding a day with the incremental pipeline costs about a third of the cheapest possible full rebuild, and it includes the quality gate, scoring and BQ2 edges, which the rebuild figure leaves out.

## Case Graph lookup cost

For the top 15 Sep alert, a 48-hour upstream and downstream lookup scanned about 76 to 126 MB in Athena (about US$0.0004 to 0.0006) and returned in 1.3 to 1.7 s. With 16 Sep loaded, the top 15 Sep alert's receiving account now shows 2 onward transfers within 48 hours (175 MB scanned, 2.2 s).

## File sizes

Silver v2 holds 33 data files (3.3 GB, about 100 MB each), so small files are not a problem and Iceberg compaction was left off to save cost. The raw stream files from the retired replay (`bronze/stream/`) are small Firehose batches; they are kept only as evidence for 14 to 16 Sep.

## Spend

| Item | US$ |
|---|---|
| Glue, every `is459_etl_v2_*` job run ever (from job-run DPU-seconds, US$0.44 per DPU-hour) | 5.15 |
| Kinesis, Firehose, Athena, S3 for ETL v2 (Cost Explorer, October, plus today's not yet billed usage) | about 1 to 1.5 |
| **ETL v2 total** | **about 6 to 7** |

Account-wide October spend in Cost Explorer was US$10.75 on 17 Oct (it lags about a day). Forecast for the month is US$18.15, against the course's US$250 account budget and the US$40 ETL guardrail budget. US$4.20 of October spend is "EC2 - Other", the line where the storage for four stopped t2.micro instances shows up. Those instances are not part of ETL v2.

## CSV cutover (24 Oct 2026)

The daily lane now reads each day from `bronze/ibm-aml/li-medium/transactions/LI-Medium_Trans.csv` (ETL_Final_Plan.md D1 to D8). Committed days were not rerun. The parse was proved on them with `--mode parse-test`, which never creates a branch, and the whole path was proved on 17 Sep, the first day after the ledger watermark.

**Parse parity on committed days** (`./parse_test.sh <day>`, results in `results/etl-v2/parse-test/<day>/`)

| Check | 14 Sep | 15 Sep | 16 Sep |
|---|---|---|---|
| Rows parsed from the CSV = rows in Silver main | 1,891,265 = 1,891,265 | 1,891,498 = 1,891,498 | 2,956,102 = 2,956,102 |
| Rows different on any of the 33 columns (EXCEPT ALL, both ways) | 0 / 0 | 0 / 0 | 0 / 0 |
| Source proof (rows, unique IDs, exact totals per currency) | match | match | match |
| Glue DQ rules on the parsed day | 11 / 11 | 11 / 11 | 11 / 11 |
| Silver main snapshot and every branch afterwards | unchanged | unchanged | unchanged |
| Audit job run time (10 G.1X workers, whole 3 GB file read) | 4 min 25 s | 4 min 47 s | 4 min 57 s |

**17 Sep end to end through the new path** (execution `pipeline-20220917-csv1`)

| Step | Result |
| --- | --- |
| Whole CSV read | 31,251,483 data lines (the full Bronze file), 0 quoted, 0 undateable; 380 lines on 17 Sep, spread from line 12,054,988 to 31,241,486 |
| Parse + source proof | 380 rows = 380 in the CSV slice (v1 Silver also has 380), 380 unique IDs, exact totals per currency match, all on the day, 0 rejects |
| Audit branch + DQ | 11 / 11 rules passed; branch holds 31,250,493 rows = 31,250,113 + 380; audit step 2 min 40 s of job time |
| Publish | Silver `main` fast-forwarded from snapshot 8131115754297620570 to 3551179523339518083 |
| BQ2 day | PASS against the CSV source proof: 380 rows in, 240 edges written, 140 self-transfers left out |
| BQ1 day | Checked against the CSV source proof and v1: 380 rows, 0 feature mismatches on all 26 features, 0 scores outside tolerance, largest score difference 0.0 |
| Gold commit | COMMITTED; ledger now 5 loads, 31,250,493 rows (= Silver `main`) |
| Whole run | SUCCEEDED in 13 min 20 s (10:36:58 to 10:50:19 UTC) |
| Agent trigger | The SUCCEEDED event reached `is459-etl-v2-day-ready` (1 message waiting) |
| `validate_day.sh 2022-09-17` | Silver v2 = v1 (380 rows, 380 IDs, amount total 57,787,611.84 both, 0 rows unmatched); Gold scores 380 = 380, max difference 0.0, 0 outside tolerance, 0 rank differences; features 380 = 380; BQ2 edges 240 = 240, 0 only in v1, 0 only in v2; ledger 5 loads = 31,250,493 = Silver `main`; source proof PASS; `NO_DAILY_STREAMS_OK`. Log: `results/etl-v2/final/validate_20220917.log` |

The tail days are unusual (more than half of 17 to 27 Sep is labelled laundering), so 17 Sep is a pipeline test, not a model result.

**Other checks**

- The first three lines of the CSV give the same `transaction_id`s as Silver (`txn_72e1…`, `txn_54bc…`, `txn_dc23…`), so the ported ID formula is the v1 one.
- A local Spark test (`tests/test_parse_local.py`) covers numbering before the day filter, duplicate lines keeping separate IDs, bank normalisation, `decimal(23,6)` without rounding (a 7-decimal amount is rejected), entity joins, NULL entity flags and an ambiguous account key being refused.
- The replay and reconcile Glue jobs were deleted with `deploy_csv.py retire` on 24 Oct (definitions and scripts saved under `results/etl-v2/csv-cutover/202610241450/jobs/`); a status check afterwards found both gone, 0 pipeline runs in progress and the schedule DISABLED.
- No Kinesis stream or Firehose exists after the runs (`NO_DAILY_STREAMS_OK`), and the EventBridge schedule was not used (runs were started with `run_pipeline_day.sh`).

## Backfill and rollback of 18 to 27 Sep (7 Nov 2026)

**Backfill.** `backfill-20220918-20220927-bf1107` on `is459-etl-v2-backfill` ran the ten days one after another, 08:07 to 10:27 UTC (2 h 20 min, every day SUCCEEDED). Gold commit results are in `results/etl-v2/gold-commit/gold-commit-202209<dd>-bf1107.json`.

| Day | Silver rows | BQ1 scores | BQ2 edges | Feature / score mismatches vs v1 | v1 changed |
|---|---|---|---|---|---|
| 18 Sep | 353 | 353 | 225 | 0 / 0 | no |
| 19 Sep | 244 | 244 | 162 | 0 / 0 | no |
| 20 Sep | 211 | 211 | 133 | 0 / 0 | no |
| 21 Sep | 73 | 73 | 47 | 0 / 0 | no |
| 22 Sep | 65 | 65 | 40 | 0 / 0 | no |
| 23 Sep | 16 | 16 | 9 | 0 / 0 | no |
| 24 Sep | 11 | 11 | 7 | 0 / 0 | no |
| 25 Sep | 12 | 12 | 7 | 0 / 0 | no |
| 26 Sep | 1 | 1 | 1 | 0 / 0 | no |
| 27 Sep | 4 | 4 | 3 | 0 / 0 | no |
| **Total** | **990** | **990** | **634** | **0 / 0** | |

After the backfill Silver held 31,251,483 rows, which is every data line of the Bronze CSV. `validate_range.sh 2022-09-14 2022-09-27` passed on every day against v1.

**Rollback.** The 7 Nov backfill used up the 18 Sep run that the team wanted to record live, so it was undone the same day (Glue job `is459_etl_v2_job-rollback-0918-0927`, script `review/rollback_days_job.py`). Silver `tx_v2` and the four BQ1 state tables went back to their last snapshot before the backfill with Iceberg `rollback_to_snapshot`, after a guard checked that every later snapshot came from the backfill. The 18 to 27 Sep Gold partitions were dropped from the catalog (files kept), and the 10 backfill messages on the unused SQS queue were purged. Readback: Silver 31,250,493 rows = ledger 31,250,493, 5 loads, latest day 17 Sep. Record with the undo snapshot ids: `results/etl-v2/rollback/rollback-20220918-20220927.json`.
