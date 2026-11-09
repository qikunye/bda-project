# IS459 G1T5: ETL v2 Runbook

How to set up, run and tear down the v2 daily ETL pipeline (batch, read straight from the raw Bronze CSV; updated 24 Oct 2026 for the CSV cutover). Everything lives in us-east-1, account 941377154820, bucket `is459-aml-g1t5`. Every v2 resource is tagged `project=is459-g1t5-etl-v2`. Nothing here touches the v1 tables, v1 jobs, Bronze CSVs or the v1 model.

## What runs where

| Step | AWS resource | What it does |
|---|---|---|
| Read Silver main | Lambda `is459-etl-v2-main-snapshot` | Returns the current `main` snapshot id of the Iceberg Silver table |
| Silver audit + checks | Glue job `is459_etl_v2_job-silver-audit-dq` (script `silver_audit_dq_job.py`) | Reads the run day from `bronze/ibm-aml/li-medium/transactions/LI-Medium_Trans.csv` by position (same parse and `transaction_id` formula as v1 Silver), builds a source proof from the raw CSV slice (rows, unique IDs, exact totals per currency), writes the day to a new Iceberg `audit_*` branch, runs 11 Glue Data Quality rules. `--mode parse-test` parses a day already in Silver and compares it with `main` instead (no branch) |
| Quality gate | Lambda `is459-etl-v2-promote-gold` + Choice state | Reads the real Glue DQ result and the source-proof match; only PASS continues |
| Publish | Glue job `is459_etl_v2_job-publish` | Fast-forwards Silver `main` to the audit branch |
| BQ1 (parallel) | Glue job `is459_etl_v2_job-bq1-day` | Incremental features from the running state + frozen v1 XGBoost scores; checked against v1 (features within 1e-6 + 1e-9×value; scores: at most 0.01% of rows may differ, each by at most 0.01); staged |
| BQ2 (parallel) | Glue job `is459_etl_v2_job-bq2-day` | Directed edges for the day (self-transfers excluded); staged. Both BQ jobs check the published day against the audit job's CSV source proof (`--source-proof-key`) |
| Gold commit | Glue job `is459_etl_v2_job-gold-commit` | Writes the day's Gold partitions, registers them in the catalog, advances the BQ1 running state and ledger, checks every state table sums to the ledger |
| Orchestration | Step Functions `is459-etl-v2-silver-wap` | Runs all of the above in order; any failure goes to SNS `is459-etl-v2-failure-topic` and stops |
| Schedule | EventBridge Scheduler `is459-etl-v2-silver-schedule` | Starts the state machine; kept DISABLED outside demos |

Gold tables read by the app (Athena, database `is459_aml_gold`): `bq1_risk_scores_medium_v2`, `bq1_features_medium_v2`, `bq2_graph_edges_medium_v2`, all partitioned by `txn_date`.

## Run one day end to end (CloudShell)

No replay, no Kinesis, no Firehose. A day is one state machine run (about 20 minutes).

1. Start the pipeline for the day. Either directly:
   ```bash
   ./run_pipeline_day.sh 2022-09-17 <run_tag>            # CSV defaults to bronze/ibm-aml/li-medium/transactions/
   ```
   or through the EventBridge schedule (fires once, two minutes from now):
   ```bash
   python3 schedule_day.py once 2022-09-17 <run_tag> 2
   python3 schedule_day.py disable   # afterwards
   ```
   The state machine input is `{day, day_compact, run_tag, source_csv_key}`. `load_id` is still `full-day-<day_compact>-<run_tag>`.
2. Watch it in the Step Functions console, then check it with `./validate_day.sh 2022-09-17` (read-only Athena checks against v1, the source proof, and a check that no Kinesis stream or Firehose exists apart from the real-time stream during an RT session).
3. Only days after the ledger watermark can be loaded (14 to 17 Sep are committed; Gold commit refuses a new load for them). The audit job also refuses a day that is already in Silver `main`. The next day to load is **18 Sep**, kept for the recorded live run (see "Current state" below).
4. A successful run also fires the agent: the EventBridge rule `is459-etl-v2-day-succeeded` starts the agent's Step Functions machine `is459-agent-batch` (agent PLAN D40). Its old second target, SQS `is459-etl-v2-day-ready`, is still attached but nothing reads it.

## Current state (8 Nov 2026)

- Committed: 14, 15, 16, 17 Sep. Silver `main` and the ledger hold 31,250,493 rows (5 loads).
- 18 to 27 Sep were loaded and checked on 7 Nov, then rolled back the same day so 18 Sep can be recorded live (evidence: "Backfill and rollback").
- To undo the rollback (bring 18 to 27 Sep back without rerunning), call `rollback_to_snapshot` to the `undo_to_backfill_main` ids in `results/etl-v2/rollback/rollback-20220918-20220927.json` and re-add the Gold partitions (files are still in S3).

## Several days in one go

```bash
python3 review/deploy_backfill.py deploy                                   # once
python3 review/deploy_backfill.py run 2022-09-18 2022-09-27 <run_tag>
python3 review/deploy_backfill.py status
```
`deploy` creates the Step Functions machine `is459-etl-v2-backfill`, which starts the pipeline for each day in order, one at a time, and stops at the first failure. Check the range afterwards with `review/validate_range.sh 2022-09-14 2022-09-27`.

## Review scripts (read-only unless stated)

In `review/`, also in `s3://is459-aml-g1t5/exported/etl-v2/final/review/`. All read-only except `deploy_backfill.py` and `rollback_days_job.py`.

| Script | What it does |
|---|---|
| `status.sh` | Live status of ETL v2 and the real-time side: state machines, last runs, schedule, ledger, streams |
| `validate_range.sh <from> <to>` | Every day in the range against v1 in one query per table, plus checks that need no v1 |
| `review_checks.sh` | Readiness of the pieces around the daily lane: agent trigger and the real-time lane |
| `deploy_backfill.py` | The backfill machine above (writes) |
| `rollback_days_job.py` | Glue job that rolled back 18 to 27 Sep on 7 Nov (writes; one-off, kept as the record) |
| `fetch_agent_code.sh` | Copies the agent's code out of its ECR image into CloudShell, without Docker |

## Check the parse on a committed day (no writes to Silver)

```bash
./parse_test.sh 2022-09-15 <tag>
```
Runs the audit job in `--mode parse-test`: parses the day from the CSV into `results/etl-v2/parse-test/<day>/parse-test-<yyyymmdd>-<tag>/data/`, runs the 11 DQ rules there, and compares all 33 columns with Silver `main` both ways (EXCEPT ALL). It never creates a branch. The script prints the result and fails if `main` or any branch moved.

## Prove the quality gate

Start the state machine for a day whose CSV slice cannot pass (for example an unknown day such as `2022-09-30`, which has no rows), or run `is459_etl_v2_job-audit-dq-broken`. The run must end in `NotifyFailure` → `PipelineFailed`, an email must arrive, and `main` must keep its snapshot id.

## Tear down after a session

- The daily lane creates no Kinesis stream or Firehose. Check: `aws firehose list-delivery-streams` is empty and `aws kinesis list-streams` shows nothing (or only `is459-rt-stream` while a real-time session runs; `python3 session.py stop` removes it).
- Keep the schedule disabled: `aws scheduler get-schedule --name is459-etl-v2-silver-schedule --query State`.
- No Glue job should be running.

## Full removal (end of term)

Delete everything tagged `project=is459-g1t5-etl-v2` (Glue jobs, Lambdas, state machine, schedule, SNS topic, roles), then the v2 prefixes `silver/v2/`, `gold/bq1/v2/`, `gold/bq2/v2/`, `bronze/stream/` (stream-era evidence for 14 to 16 Sep, keep until then), `results/etl-v2/`, `exported/etl-v2/`, and the v2 Glue tables. v1 stays usable throughout.

## Retired (24 Oct 2026)

`is459_etl_v2_job-stream-replay` and `is459_etl_v2_job-reconcile-day14-exact` were deleted by `deploy_csv.py retire`. Their job definitions and scripts are saved under `results/etl-v2/csv-cutover/202610241450/jobs/`, and their run results stay under `results/etl-v2/managed-replay/` and `results/etl-v2/stream/` as evidence for how 14 to 16 Sep were loaded.
