# Final ETL plan (locked design) — change list for the team

Status: design locked 20 Oct 2026; reviewed 24 Oct 2026 (added D3a to D3c, D7, D8, rewrote section 3, answered open items 1 and 2); **implemented and deployed 24 Oct 2026** (see section 8). Supersedes the "stream into Bronze" step in `ETL_Lambda_Handover.pdf`. Everything not listed under "Changes" stays as built.

## 1. Locked design

```
DAILY LANE (batch)
  raw CSV (all days) --> S3 Bronze --> Step Functions is459-etl-v2-silver-wap
     (read main snapshot -> audit + 11 DQ rules on audit_* branch -> quality gate
      -> publish -> BQ1 day || BQ2 day -> Gold commit)
  --> Gold v2 (bq1_risk_scores / bq1_features / bq2_graph_edges, *_medium_v2) --> Athena

REAL-TIME LANE (simulation)
  CSV slice (cut from Silver v2, label never selected)
  --> Kinesis is459-rt-stream --> Lambda is459-rt-scorer --> DynamoDB is459_rt_alerts
  --> EventBridge Pipe --> SQS is459-rt-alert-events        (lane ends here)
```

Why two lanes: Lambda scores one transaction in about 3 s using per-account state (provisional, early warning). The daily batch has the full day and the 48 h graph (authoritative). Same frozen XGBoost 3.2.0 and same 26 features in both (exact parity on 160,954 streamed rows).

Kinesis exists only in the real-time lane. The daily lane has no Kinesis and no Firehose.

## 2. Changes (daily lane only)

| # | Change | Where |
|---|---|---|
| D1 | Remove the stream-replay step. No Kinesis stream or Firehose is created for a daily run, and nothing is written to `bronze/stream/` any more. | Glue `is459_etl_v2_job-stream-replay`, `..-reconcile-day14-exact`, state machine |
| D2 | New input to the audit job: the day's rows read directly from the raw Bronze CSV, `s3://is459-aml-g1t5/bronze/ibm-aml/li-medium/transactions/` (single ~3 GB all-days file). Filter by timestamp to the run day. | `silver-audit-dq` job |
| D3 | The audit job must parse the CSV itself: 11-field **positional** schema (header has two `Account` columns, do not use crawler tables), canonical account key `<bank_id leading zeros stripped>\|<account_number>`, amounts `decimal(23,6)`, no deduplication. Output must equal the existing Silver v2 columns (33), **including the derived ones** (see D3a to D3c). This is a re-implementation of the v1 CSV-to-Silver transform, so port the v1 Silver job's code instead of rewriting it. | same job |
| D3a | `transaction_id` is not in the CSV. The parser must reproduce v1's ID formula exactly (Gold, BQ2 edges, the RT scores and every v1 comparison join on it). If the formula uses the row's position in the all-days file, the day filter must be applied **after** numbering, not before. | same job |
| D3b | `sender_entity_id` / `receiver_entity_id` and the entity flags (`is_same_entity`, `is_same_entity_different_account`, `is_cross_entity`) are not in the transactions CSV either. Find where v1 gets them (probably the dataset's `LI-Medium_accounts.csv`) and read the same file. | same job |
| D3c | Timestamp convention: CSV timestamps are naive `yyyy/MM/dd HH:mm`; keep v1's UTC TimestampNTZ handling so the day filter and `txn_date` match the existing partitions. | same job |
| D4 | Replace the "landing proof" with a **source proof** computed from the CSV day slice: row count, unique transaction IDs, exact Decimal totals per currency. The audit branch is checked against it. Drop `reconciled_landing` and the stream run id inputs, and remove any DQ rule or promote-gold check that reads the landing proof. Note that this proof now comes from the same file the job parses, so it catches dropped or duplicated rows but not a wrong parse; section 3 is what proves the parse. | audit job, DQ ruleset, `is459-etl-v2-promote-gold`, `etl_v2_pipeline.asl.json` |
| D5 | State machine input becomes `{day, day_compact, run_tag, source_csv_key}`. Keep `day_compact` and `run_tag`: the `BuildIds` state builds every run id and the audit branch name from them, and ASL has no intrinsic that strips dashes from a date. `load_id` stays derived as today (`full-day-<day_compact>-<run_tag>`) unless there is a reason to change it (see 6.2). Update the scripts that start it. | `etl_v2_pipeline.asl.json`, `run_pipeline_day.sh`, `schedule_day.py` |
| D6 | Update `validate_day.sh` and `deploy_etl_v2.py` to match. | `exported/etl-v2/final/` |
| D7 | Retire `is459_etl_v2_job-stream-replay` and `..-reconcile-day14-exact` (delete the jobs, keep their scripts and result files as evidence). Keep the existing `bronze/stream/` data until the Week 14 cleanup, since the 14 to 16 Sep evidence refers to it. | Glue, runbook |
| D8 | Update the docs that still describe the daily lane as streamed: ETL v2 handover doc (At a glance diagram, What was built, How to run a day, Q&A "Daily micro-batch" row, Risks "Replay can hit its deadline" and "Stream left running", Cleanup), `etl_v2_runbook.md`, `etl_v2_evidence.md` section on latency. | docs |

Unchanged: quality gate, publish (fast-forward), BQ1 day, BQ2 day, Gold commit, ledger, `applied_load_ids`, SNS failure email, EventBridge schedule (keep DISABLED outside demos), Gold table names and partitioning, the real-time lane.

Effect: no 1 to 2 hour replay and no Kinesis/Firehose cost (about US$1.10 per day); a day is the ~20 minute pipeline run.

## 3. Testing the change without breaking committed days

14, 15 and 16 Sep are already COMMITTED (ledger, state tables, `applied_load_ids`). Do not rerun them end to end. (A rerun is also blocked by Gold commit: a new load_id for a day at or before the ledger watermark is refused.) Instead, test D2 to D4 **parse-only**, then prove the whole path on a day that has never been loaded:

**3a. Parse parity on 14, 15 and 16 Sep (no writes to Silver)**

Careful: the audit job forks its `audit_*` branch from the **current** main, and main already contains these days. Appending the day again would double every row, and the uniqueness DQ rule would fail. So either:
- (recommended) add a `--parse-test` mode that writes the parsed day to a scratch location (for example Parquet under `results/etl-v2/parse-test/<day>/`), runs the 11 DQ rules on that data, and never creates a branch; or
- fork the branch from the snapshot just before that day (`FOR VERSION AS OF` the pre-day snapshot: pre-15 Sep is `5430669997875323762`; look up pre-14 and pre-16 in the `tx_v2` snapshots metadata table), then drop it.

Then for each day:
1. Compare the parsed rows with Silver main for that day on all 33 columns: row count, unique IDs, exact amount totals, zero differing rows (an `EXCEPT` both ways).
2. Expected counts: 14 Sep 1,891,265; 15 Sep 1,891,498; 16 Sep 2,956,102.
3. The 11 DQ rules PASS on the parsed data.
4. Silver main snapshot id is unchanged afterwards; no branch is left behind.

Because BQ1, BQ2 and Gold commit read the published Silver table (unchanged), they do not need re-proving for these days.

**3b. One genuinely new day end to end (recommended)**

Silver v2 stops at 16 Sep, but v1 Silver and v1 Gold scores also cover the tail days 17 to 27 Sep (1,370 rows in total, all scored by v1). So 17 Sep can run through the new CSV path end to end: it is after the ledger watermark, and BQ1 still has a v1 reference to compare against. That proves D1 to D5 together, cheaply, without touching the committed days. Check first that no DQ rule has a minimum row count the small day would fail, and note that the tail days are unusual (over half laundering-labelled), so present it as a pipeline test, not a model result.

## 4. Real-time lane (no changes planned)

- Keep as built: producer Glue job, Kinesis (1 shard), Lambda (container image), DynamoDB tables, Pipe, bus, SQS.
- SQS `is459-rt-alert-events` is the end of the lane. If Streamlit shows a live alerts feed, read DynamoDB `is459_rt_alerts` (a queue is consumed on read: a dashboard would steal the agent's messages, and each refresh would show different alerts). Details for Streamlit: the table is small, so a `Scan` filtered on `scored_at_ms` (newest first) is fine; numbers are DynamoDB `N` values and come back as strings; use a read-only IAM role limited to that table.
- Decide whether the real-time additions built on 20 Oct are in or out, because "no changes planned" conflicts with them: the session state machine `is459-rt-session` (one-click or scheduled sessions with guaranteed teardown, reaper, alarms) and the alert explainer (separate Lambda behind a second queue, writes `is459_rt_briefs` and Athena `is459_rt.alert_briefs`). Code is written and tested; neither is deployed yet. If the explainer is kept, Streamlit can show each alert's brief next to it from `is459_rt_briefs`, and the "no LLM inside the Lambda" rule still holds because the scorer is unchanged.
- Threshold 0.3605 (500th-highest v2 score on 15 Sep). Changing it: one `deploy_rt.py lambda <image> <threshold>` call.
- Always `python3 session.py stop` after a session. Ground rules from the real-time spec still apply (no LLM inside the Lambda, no real-time graph, no always-on services).

## 5. Hooks the pipeline must expose for the AI agent

- Trigger B: EventBridge rule on Step Functions `is459-etl-v2-silver-wap` execution status SUCCEEDED, passing the run day. The status-change event carries the execution input as a JSON string (`detail.input`), so the agent parses `day` out of it; an input transformer cannot do that by itself.
- Read-only tools through Athena over `bq1_risk_scores_medium_v2`, `bq2_graph_edges_medium_v2`, `bq1_features_medium_v2`. Enforce "no label" with Athena views that leave the label column out, and give the agent's role access to the views only; relying on each tool's SELECT list is too easy to get wrong.
- One decisions table (proposed: DynamoDB), written only by the agent, read by Streamlit. Use the same key name as the other tables (`transaction_id` in Gold, `txn` in the RT tables); pick one and say which.

## 6. Open items to confirm

1. **Answered by the evidence, confirm in code:** the replay sent a "pinned source export" that was checked on "all 33 columns" (`etl_v2_evidence.md` row 2, `etl_v2_decisions.md` section 6), and the audit job "parses the landed day by position". The source was therefore Silver-shaped rows, not the raw 11-field CSV. So D3 is a real re-implementation (D3a to D3c), probably the largest item in this plan. Estimate it from the v1 Silver job, and confirm by reading the replay job's source query.
2. **Answered:** `load_id` can be any unique string. Gold commit treats a known load_id as already applied, and refuses a new load_id for any day at or before the ledger watermark, so a committed day cannot be counted twice whatever the format. Keeping today's format (`full-day-<day_compact>-<run_tag>`) avoids touching the BQ1 and Gold commit arguments at all.
3. The BQ1 check still compares to v1 output. That reference exists for 1 to 27 Sep (v1 scored all 27 days), so 17 to 27 Sep can still run end to end (section 3b). A day with no v1 output needs schema, null and range checks instead (not built).
4. Streamlit and agent are separate workstreams; this plan only fixes the pipeline side.
5. Reading the full ~3 GB CSV for every day is fine at this scale (a few Glue minutes). If it becomes slow, split the CSV by day once into `bronze/ibm-aml/li-medium/by-day/` and point `source_csv_key` at the day file.
6. With the replay gone, the daily lane is plain batch. Make sure the report and the slides place the streaming x-factor on the real-time lane, and drop any line that says transactions stream into Bronze.

## 7. Done when

- Parse parity (section 3a) passes for 14, 15 and 16 Sep on all 33 columns, with Silver main unchanged and no branch left.
- 17 Sep runs end to end through the new path (section 3b), and `./validate_day.sh 2022-09-17` passes against v1.
- No Kinesis or Firehose resource is created by a daily run; `list-streams` and `list-delivery-streams` stay empty.
- The replay and reconcile jobs are retired (D7).
- Updated scripts and `etl_v2_pipeline.asl.json` are in `exported/etl-v2/final/`, and the docs in D8 are updated.

## 8. Implementation status (24 Oct 2026)

| Item | Status | Where |
|---|---|---|
| D1 replay removed from the daily lane | Done | `etl_v2_pipeline.asl.json`; replay and reconcile jobs retired by `deploy_csv.py retire` |
| D2, D3, D3a to D3c CSV parse | Done | `silver_audit_dq_job.py`, a port of the v1 Silver job (`silver/ibm-aml/deployment/v20261004_18`). `transaction_id` = `txn_` + SHA-256(`li_medium`, `LI-Medium_Trans.csv`, whole-file line number, 11 raw fields)[:32]; lines numbered before the day filter. Entity columns from `LI-Medium_accounts.csv`. `ingest_run_id` kept as `medium_live_01` |
| D4 source proof | Done | Audit result `source_proof` + `source_proof_match`; quality gate Lambda checks it. The old gate never read the landing proof, so nothing had to be removed there |
| D4, found in the build: BQ1/BQ2 day jobs | Done | Both checked each day against the replay's pinned source export and only accepted 14 to 16 Sep. Patched to read the audit result (`--source-proof-key`): same rows, same ID hash, same exact totals, same snapshot. BQ1's ledger baseline check now counts Silver rows before the day |
| D5 new input | Done | `{day, day_compact, run_tag, source_csv_key}`; `run_pipeline_day.sh`, `schedule_day.py` |
| D6 scripts | Done | `validate_day.sh` (source proof + no-stream check), `deploy_csv.py`, `parse_test.sh` |
| D7 retire jobs | Done | Deleted 24 Oct; definitions and scripts saved under `results/etl-v2/csv-cutover/202610241450/jobs/` |
| D8 docs | Done | Handover doc, runbook, evidence, decisions |
| 3a parse parity 14, 15, 16 Sep | PASS | 1,891,265 / 1,891,498 / 2,956,102 rows; 0 rows different on 33 columns both ways; 11/11 DQ; main and branches unchanged |
| 3b 17 Sep end to end | PASS | execution `pipeline-20220917-csv1`, 13 min 20 s: 380 rows, source proof match, 11/11 DQ, BQ1 0 feature and 0 score mismatches vs v1, BQ2 240 edges, Gold COMMITTED, ledger 31,250,493 = Silver main, agent queue got the event |
| Section 5 hooks | Done | `deploy_agent_hooks.py`: views `is459_agent.v_bq1_risk_scores` / `v_bq1_features` / `v_bq2_graph_edges` (label columns dropped), DynamoDB `is459_agent_decisions` (key `transaction_id`), rule `is459-etl-v2-day-succeeded` → SQS `is459-etl-v2-day-ready`, policy `is459-agent-readonly` |
