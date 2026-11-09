# IS459 G1T5: ETL v2 Decisions and Known Limits

Short record of the choices made while finishing ETL v2, so the team can defend them in the report and Q&A.

## 1. Tolerances in the v1 parity checks

| Check | Rule | Why |
|---|---|---|
| BQ1 features | Each of the 26 features within 1e-6 + 1e-9 × value of v1, compared as double | v1 wrote some features through float32. An exact compare failed on 55 rows whose values differed by at most 1.5e-8, which is pure float rounding. With the tolerance, 0 rows differ |
| BQ1 scores | Same frozen XGBoost model (iteration range 0 to 96). At most max(10, 0.01% of rows) may sit outside 1e-6 + 1e-5 × score, and none may differ by more than 0.01 | A feature that moves by 1e-8 can land on the other side of a tree split. That happened on 11 rows (14 Sep), 10 rows (15 Sep) and 12 rows (16 Sep, allowance 295), with a largest change of 0.00077. Risk ranks did not change |

The allowance is written into every BQ1 result file (`feature_tolerance`, `score_mismatch_allowance_rows`, `score_mismatch_allowance_abs`), so a reviewer can see exactly what was accepted.

## 2. History partitions on the v2 Case Graph table

`bq2_graph_edges_medium_v2` has partitions for 1 to 13 Sep that point at the v1 edge files. BQ2 edges for those days are a pure function of Silver, and v2 Silver for 1 to 13 Sep equals v1 row for row (rows, IDs and exact amounts all match). Rebuilding them would have cost money and produced identical files (the schema md5s match). The v2 pipeline writes 14 Sep onwards itself.

## 3. Pipeline days depend on v1 references

The pipeline checks each day it builds against the stored v1 output for that day. v1 scored all 27 days (1 to 27 Sep), so any day up to 27 Sep can run end to end, and 17 Sep was used to prove the CSV path (see section 9). A day with no v1 output would fail the BQ1 check; it would need schema, null and range checks instead. That change was not made, because the assessed runs are about proving v2 matches v1.

## 4. No Iceberg compaction

Silver v2 is 33 data files, about 100 MB each (3.3 GB total, after 16 Sep). That is already the target file size, so a compaction job would cost Glue time and change nothing.

## 5. Graph features paused

Workstream D (graph features for BQ1) is on hold, as the team agreed, because of the leakage risk raised. ETL v2 still produces the BQ2 edge table, which is what the Case Graph view needs. Nothing in v2 blocks adding graph features later.

## 6. Stream replay retired (24 Oct 2026)

14 to 16 Sep were loaded by replaying each day through a one-shard Kinesis stream and Firehose into `bronze/stream/` (one to two hours per day; the 16 Sep landing was proved by the exact reconcile job after the replay hit its own deadline). The team then locked a simpler design: the daily lane is plain batch and reads the raw Bronze CSV, and Kinesis only exists in the real-time lane. The replay and reconcile jobs were deleted (definitions and scripts kept under `results/etl-v2/csv-cutover/`), and `bronze/stream/` stays until the Week 14 cleanup because the 14 to 16 Sep evidence points at it.

## 7. Cost tracking

The account is a member of a payer (management) account, so cost-allocation tags cannot be turned on from this account. ETL v2 cost is measured from the Glue job-run DPU-seconds of every `is459_etl_v2_*` job plus Kinesis, Firehose and Athena usage. AWS Budgets alerts are set up for the account.

## 9. Reading the day straight from the raw CSV

The stream replay sent Silver-shaped rows (exported from v1 Silver), so the CSV path is a real re-implementation of the v1 Bronze-to-Silver transform. It is a port of the v1 job's code (`silver/ibm-aml/deployment/v20261004_18`), not a rewrite:

- `transaction_id` = `txn_` + the first 32 hex characters of SHA-256 over `li_medium`, `LI-Medium_Trans.csv`, the row's physical line number in the whole file, and the 11 raw fields, joined by `\x01`. Days are interleaved through the file, so lines are numbered before the day filter. Checked on the first three lines of the file: the ids equal the ones already in Silver.
- Fields are bound by position (the header has two `Account` columns), trimmed, bank ids lose leading zeros in the account key, amounts are `decimal(23,6)` (rows that would need rounding are rejected), and the timestamp is read in UTC and stored as TimestampNTZ.
- Entity ids, names and flags come from `LI-Medium_accounts.csv` by two left joins, failing closed on an ambiguous account key, with entity flags left NULL when either side is unknown. All exactly as v1.
- `ingest_run_id` stays `medium_live_01` (the v1 Silver run whose transform this ports), so Silver v2 keeps one value in that column. Each run's own id is in the Iceberg snapshot and the result file.

The source proof (rows, unique ids, exact totals per currency) is computed from the same CSV slice with a plain comma split, separately from the parser. It catches dropped or duplicated rows; it cannot catch a wrong parse, which is what the parse-only parity runs on 14, 15 and 16 Sep prove (all 33 columns, both ways, against what is already in Silver).

The BQ1 and BQ2 day jobs used to check each day against the replay's pinned source export. They now read the audit job's result file instead (`--source-proof-key`): same row count, same id hash algorithm, same exact totals, and the same published snapshot. Their 14 to 16 Sep whitelist became "any day in Sep 2022"; the Gold commit watermark still refuses a committed day.

## 10. Chronological loads and agent integration

The audit rejects a day already present in Silver. Gold commit recognises known
`load_id`s and refuses a new load for any day at or before the ledger watermark.
Changing the load ID cannot make a committed day safe to append. The input
contract remains `{day, day_compact, run_tag, source_csv_key}`; `BuildIds` uses
the compact date and tag to derive run/branch names and
`full-day-<day_compact>-<run_tag>`. Run new days in order.

The 18–27 September backfill and rollback are historical pipeline evidence, not
model performance evidence; see [the execution record](evidence.md#backfill-and-rollback-of-18-to-27-sep-7-nov-2026).

The ETL success event carries the input JSON string in `detail.input`. The agent
state machine parses the day itself, then rebuilds label-free tables and runs
investigations. The current submission path uses the Fargate target from
[infra](../../infra/README.md), not the older SQS hook. The legacy
`deploy_agent_hooks.py` remains historical integration code and must not be
confused with the current investigation deployment.

## 11. Daily versus real-time processing

Daily ETL reads the raw Bronze CSV and creates no Kinesis or Firehose resources.
It provides the authoritative daily Silver/Gold outputs. The separate real-time
prototype replays Silver slices through Kinesis and a Lambda scorer with the
same frozen model and feature semantics. Its explainer and session orchestrator
are implemented; they are not pending additions. See [real-time documentation](../../realtime/README.md).

The source proof comes from the same CSV as the parser, so it detects dropped or
duplicated rows but cannot alone detect an incorrect parse. Independent all-column
parity and local synthetic tests supply that evidence. At larger scale, repeatedly
reading the all-days CSV becomes a cost limitation; pre-partitioning the source
by day is a possible future change, not an implemented feature.
