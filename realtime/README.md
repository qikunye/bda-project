# IS459 G1T5: Real-time scoring path (prototype)

A Kinesis -> Lambda path that scores each transaction with the frozen v2 XGBoost model within seconds of arrival, beside the verified daily ETL v2 pipeline. v2 stays the system of record; this path is parity-tested against v2 Gold on a replay slice.

Everything is in us-east-1, bucket `is459-aml-g1t5`, tagged `project=is459-g1t5-rt`. Code: `s3://is459-aml-g1t5/rt/code/`.

## Pieces

| Piece | Resource | Notes |
|---|---|---|
| Stream | Kinesis `is459-rt-stream`, 1 shard | Created by `session.py start`, deleted by `session.py stop` |
| Producer | Glue Python shell job `is459_rt_producer` (1/16 DPU) | Replays a bootstrapped slice in timestamp order, label never included, partition key = sender |
| Scorer | Lambda `is459-rt-scorer` (container image in ECR `is459-rt-scorer`) | xgboost 3.2.0, iteration range 0 to 96, model and schema read from S3 with SHA-256 pins |
| Event source mapping | batch 300, window 1 s, parallelization 1, 3 retries, bisect on error | On failure: SQS `is459-rt-scorer-dlq` |
| State | DynamoDB `is459_rt_state` (on demand) | One item per sender, sender+currency, receiver, receiver+currency, pair; per-minute buckets for the 1 h / 24 h windows |
| Exactly-once | DynamoDB `is459_rt_seen` (TTL 14 days) | Seen markers written in the same transaction as the state and score rows |
| Scores | DynamoDB `is459_rt_scores` | One row per transaction: score, 26 features, arrival and scoring times |
| Alerts | DynamoDB `is459_rt_alerts` + stream -> EventBridge Pipe `is459-rt-alert-pipe` -> bus `is459-rt-alerts` -> rule -> SQS `is459-rt-alert-events` | The queue stands in for the agent. Event follows the spec contract |
| Guardrail | AWS Budgets `is459-rt-guardrail` (US$15) | Alerts at 50, 80, 100% |

## Results (20 Oct 2026, replay of 16 Sep, threshold 0.3605 = 500th highest v2 score on 15 Sep)

| Check | Result |
|---|---|
| Offline gate, first 10 min of 01:00 (20,126 rows) | 0 feature mismatches, scores identical (max diff 0.0) |
| Offline, full hour 01:00-02:00 (120,761 rows) | 0 feature mismatches, scores identical |
| Live run A, 01:00-02:00 at 4x speed | 120,761 / 120,761 scored, 0 feature mismatches, max score diff 0.0, 8 alerts, 8 events delivered |
| Idempotency: first minute (2,096 rows) re-sent | All skipped as seen; state SHA-256 unchanged (519c07fb...), score rows unchanged |
| Live state vs Silver rebuild at 02:00 and 02:15 | 4,364 and 23,738 overlapping accounts, 0 mismatches |
| Live run B, 02:00-02:15 at 1x, minute bursts | 30,029 scored, parity exact; latency p50 10.0 s, p95 17.4 s, max 20.6 s |
| Live run C, 02:15-02:20 at 1x, arrivals spread over each minute | 10,164 scored, parity exact; latency p50 2.4 s, p95 3.0 s, p99 7.2 s, max 8.0 s |
| Alert events | 10 alerts in total, 10 events delivered to the agent queue; DLQ empty |
| v1 / v2 untouched | S3 fingerprint and Iceberg pointers identical before and after every run |

Latency is Kinesis arrival to committed score. Run B is slower only because the source timestamps are minute-precise, so the producer released each minute's ~2,000 rows at once; run C spreads them across the minute, as live traffic would arrive.

Throughput: one shard + one Lambda (parallelization 1) sustains about 130 transactions/s, about 2.6x the busiest hour in the replay data (178k rows in 16 Sep 00:00-01:00, about 50/s).

## Cost of testing (measured, 19-20 Oct 2026)

| Item | Usage | US$ |
|---|---|---|
| DynamoDB on demand (bootstrap loads + 163,050 streamed records) | 4.25M write units, 1.26M read units | 2.81 |
| Lambda | 2,005 GB-seconds | 0.03 |
| Kinesis | about 1 shard-hour, 163,050 records | 0.02 |
| Glue producer (1/16 DPU) | 4 runs | 0.02 |
| Athena bootstrap scans | about 7 GB | 0.04 |
| **Total** | | **about 3** |

Left in place between sessions: DynamoDB storage (about 0.4 GB, about US$0.10 a month) and the 1 GB image in ECR (about US$0.10 a month). Nothing bills by the hour once `session.py stop` has run.

## Spec acceptance criteria

| Criterion | Status |
|---|---|
| Own stream + producer, label stripped, ordered per sender | Done: separate stream, label never selected, one shard so total order; PutRecords calls never mix minutes |
| xgboost 3.2.0, iteration range 0-96, feature order from feature_schema.json | Done: same wheel as v2, model and schema SHA-256 pinned, matrix() copied verbatim |
| Idempotency test | Done: 2,096 re-sent records skipped, state checksum unchanged; crash-and-retry test passes locally |
| Parity on a 1-2 hour slice within v2 tolerances | Done: 160,954 live-scored rows, 0 feature mismatches, scores identical (results in rt/runs/*/validate.json) |
| Latency p50/p95/max | Done: p50 2.4 s, p95 3.0 s, max 8.0 s (run C) |
| Alert event to is459_rt_alerts + EventBridge | Done: DynamoDB stream -> Pipe -> bus -> rule -> SQS. Numbers arrive as strings (DynamoDB N values), consumers cast them |
| Zero writes to v1/v2 | Done: fingerprint identical for runs A, B, C |
| Tagged, teardown run, stream gone | Done |
| Cost recorded | Done: about US$3 |

## Run a session (from `realtime/` or an equivalent CloudShell copy)

```bash
python3 bootstrap.py 2022-09-16T01:00:00 2022-09-16T02:00:00 s0116h1      # slice + starting state + v2 Gold for comparison (read-only Athena)
python3 deploy_rt.py base <alert email>                                    # once
REPO=941377154820.dkr.ecr.us-east-1.amazonaws.com/is459-rt-scorer           # build and push the scorer image
docker build -t is459-rt-scorer:v1 . && docker tag is459-rt-scorer:v1 $REPO:v1
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin 941377154820.dkr.ecr.us-east-1.amazonaws.com
docker push $REPO:v1                                                       # note the sha256 digest it prints
python3 deploy_rt.py lambda $REPO@sha256:<digest> 0.36046239733695984
docker run --rm --entrypoint python3 -v $HOME/rt/rtdata:/data -w /var/task is459-rt-scorer:v1 \
  offline_gate.py /data/s0116h1 10 /data/model.ubj /data/feature_schema.json   # go/no-go before streaming
python3 load_state.py rtdata/s0116h1                                       # starting state for slice accounts only
python3 validate_rt.py guard-before <run_id>                               # fingerprint v1/v2
python3 session.py start
aws glue start-job-run --job-name is459_rt_producer --arguments '{"--slice_key":"rt/bootstrap/s0116h1/slice.csv","--run_id":"<run_id>","--speedup":"4","--minutes":"0","--resend":"0"}'
python3 validate_rt.py check <run_id> rtdata/s0116h1                        # parity, latency, alerts, state checksum, v1/v2 untouched
python3 session.py stop                                                    # always
```

The offline gate needs `rtdata/model.ubj` and `rtdata/feature_schema.json` from the frozen project assets in S3. The scorer pins their SHA-256 values in `app.py`; the checked-in LI-Small model is not a substitute. Container examples using `$HOME/rt/rtdata` assume the historical CloudShell layout; change that host bind mount to your actual `realtime/rtdata` directory.

To continue with the next slice without resetting, bootstrap it, then `python3 state_check.py rtdata/<next_tag> --load-missing`: it checks the live state of accounts already seen against Silver and preloads only accounts the stream has not touched. Add `"--spread":"1"` to the producer arguments to spread each minute's rows across the minute (realistic arrival). Rerunning a slice already processed needs empty tables, because state has advanced and every transaction is marked seen.

## Tests

- `tests/test_local.py`: engine vs brute-force Spark window semantics on synthetic data with same-minute ties (0 mismatches).
- `tests/test_handler.py`: handler against an in-memory DynamoDB with an injected crash mid-batch, the Lambda retry and a re-send of 300 records; final state and features identical to one clean pass.
- `offline_gate.py`: go/no-go on real data inside the scorer image, against v2 Gold, before any streaming resource is built.

## Teardown

`python3 session.py stop` after every session. End of term: delete everything tagged `project=is459-g1t5-rt` (Lambda, ECR repo, 4 DynamoDB tables, 2 SQS queues, EventBridge bus, rule and pipe, Glue job, 3 IAM roles, budget) and the `rt/` and `athena-results/rt/` prefixes.

## Automated sessions (Phase A)

One Step Functions run replaces the manual commands above. `deploy_session.py all <email>` creates it once:

| Piece | Resource |
|---|---|
| Orchestrator | Step Functions `is459-rt-session`: init and budget check, bootstrap (skipped if present), starting state (`fresh` = load, `continue` = check against Silver then preload), guard, start stream + mapping, produce, wait until every record is scored, wait for briefs, validate, always stop, email |
| Control steps | Glue Python shell `is459_rt_control` (runs `bootstrap.py`, `load_state.py`, `state_check.py`, `validate_rt.py` unchanged) |
| Small steps + reaper | Lambda `is459-rt-session-helper` (start, drain, briefs, stop, summary; hourly `reap` via rule `is459-rt-reaper-hourly`) |
| Notifications | SNS `is459-rt-session-topic` (summary email per session, alarm emails) |
| Alarms | `is459-rt-scorer-dlq`, `is459-rt-explain-dlq`, `is459-rt-stream-lag`, `is459-rt-explainer-errors`, `is459-rt-session-failed` |
| Schedule | EventBridge Scheduler `is459-rt-session-schedule`, DISABLED outside demos |

```bash
python3 deploy_session.py start '{"slice_start":"2022-09-16T02:20:00","slice_end":"2022-09-16T03:20:00","tag":"s0220h1","speedup":"1","spread":"1","mode":"continue"}'
python3 deploy_session.py schedule 2026-10-21T02:00:00 '<same input>'   # one-time, UTC
python3 deploy_session.py disable
```

Every state after the stream exists has a Catch to the stop step, so a failed session still deletes the stream and mapping; the hourly reaper deletes them if a session was stopped by hand. Results: `rt/runs/<run_id>/` (`producer.json`, `guard_before.json`, `validate.json`, `session_summary.txt`).

## Alert explainer

A fine-tuned Qwen2.5-1.5B (Apache 2.0, Q4_K_M GGUF, about 0.9 GB) in Lambda `is459-rt-explainer` writes one grounded JSON brief per alert: typology guess, confidence, a narrative whose every number appears in the input, and up to three next checks for the case graph. XGBoost still decides who is flagged.

- Trigger: second target of the alert rule -> SQS `is459-rt-explain-queue` -> alias `live` (batch 1, max concurrency 2, DLQ after 3 tries).
- Inputs (allowlist in `explainer_common.py`): score, amount, the 26 features, five as-of context numbers (sender's inflow and receiver's outflow over the previous 24 h, reverse payments over 4 days; Athena over Silver, strictly before the alert), and a typology hint from a small XGBoost classifier. Account ids and labels never enter.
- Output: DynamoDB `is459_rt_briefs` and `s3://is459-aml-g1t5/rt/briefs/dt=<day>/<txn>.json`, queryable as Athena `is459_rt.alert_briefs` for the dashboard.
- Training: `explainer_export.py` (read-only Athena) -> Kaggle notebook `kaggle_explainer.py prep | teacher | train | upload` (labels from `LI-Medium_Patterns.txt`, teacher Qwen2.5-7B-Instruct, LoRA on 1 to 13 Sep, graded on 14 to 16 Sep).
- Release: `python3 release.py <model_version>` runs the unit tests, builds and pushes the image, publishes a `candidate` version, evaluates it on 300 held-out alerts (`eval_explainer.py`) and moves `live` only if every gate holds, including beating the untuned base model on the teacher reference (see below). If the base model changes, re-record the comparison point with `python3 eval_explainer.py <id> --qualifier <base_version> --as-baseline`. Roll back with `python3 deploy_explainer.py rollback <version>`.

### Explainer release results (25 Oct 2026, model v20261025a)

| | First release (model picks typology) | Live release (`TYPOLOGY_MODE=hint`) | Negative test: untuned base model |
|---|---|---|---|
| Alerts evaluated | 300 | 300 | 100 |
| Schema-valid / invented numbers | 100% / 0 | 100% / 0 | 100% / 0 |
| needs_review (template fallback) | 2 | 3 | 0 |
| Typology macro-F1 (classifier hint alone) | 0.241 (0.345) | 0.345 (0.345) | 0.293 (0.293) |
| Warm latency p50 / p95 | 13.5 s / 17.4 s | 13.4 s / 18.8 s | 10.9 s / 16.5 s |
| Gates | failed (macro-F1), not promoted | all passed, promoted to `live` (version 2) | all passed (evaluated without `--promote`) |

- The teacher was told the true typology, so the student learned to override the classifier hint and did worse than the hint. `TYPOLOGY_MODE=hint` pins the typology to the XGBoost hint through the grammar (typology is generated first, so the narrative is written for it). The language model writes the narrative, confidence and next checks.
- The negative test showed the original gates prove a brief is safe and grounded, not that fine-tuning improved it: the untuned base model passed them too. Added on 26 Oct: a teacher-reference gate. `eval_explainer.py` now stores every brief (`rt/builds/<id>/briefs.jsonl`) and scores it against the teacher's reference brief for the same alert (`rt/explainer-data/teacher_eval_ref.jsonl`, 288 of the 300 alerts): mean of ROUGE-L on narrative plus next checks and recall of the numbers the teacher cited (`explainer_common.ref_score`). A release must beat the recorded base-model run (`rt/explainer-data/baseline_ref.json`) by at least 0.02 and win more than half of the head-to-head alerts.

| Teacher-reference gate, 26 Oct, 300 alerts | Base model (version 3, `refgate-base-20261026`) | Live fine-tuned model (version 2, `refgate-v2-20261026`) |
|---|---|---|
| Mean reference score | 0.107 | 0.513 |
| Mean ROUGE-L | 0.188 | 0.459 |
| Head-to-head (288 alerts) | 2 wins | 286 wins |
| All other gates | passed | passed (macro-F1 0.345, needs_review 3, warm p95 18.2 s) |
| Result | recorded as baseline | PASSED every gate, including both new ones |

- Training data: 19,302 candidates; 3,452 train / 1,186 held-out rows; teacher kept 3,593 of 3,752 briefs (156 dropped for invented numbers, 3 unparsed); LoRA on 3,305 examples, 2 epochs; Q4_K_M GGUF 986 MB, sha256 `c537a06e...`.
- Links to Kaggle: `kaggle_links.py get | put <version>` signs 1-hour S3 links with role `is459-rt-kaggle-transfer` (CloudShell credentials expire within minutes). No AWS keys go to Kaggle.

### Session notes

- Use `"mode":"fresh"` for demos. `continue` is only safe when the slice starts exactly where the last session ended; otherwise `state_check` finds live state that disagrees with Silver and the session stops before any stream exists (seen on 25 Oct: 4,373 accounts).
- Budget gate: a session starts only if month-to-date spend is at most 80% of `is459-rt-guardrail`. The limit was raised to US$26 on 25 Oct (spend US$20.14).
- Schedule tested on 25 Oct: `deploy_session.py schedule` fired the state machine at the set minute; `deploy_session.py disable` afterwards.
- Scheduled session on 25 Oct (EventBridge fired at 17:10:01 UTC, slice 03:35 to 03:50, fresh): PASSED, 30,234 rows, parity 0 mismatches / 0.0, 0 alerts, teardown clean, US$0.81. Latency p50 2.0 s but p95 27.7 s (the first, 03:20 session had p95 2.1 s); worth watching, likely cold starts while the explainer evaluation ran in parallel.
- Rollback tested on 25 Oct: `deploy_explainer.py rollback 1` moved `live` to version 1 (model-picks-typology build), `rollback 2` moved it back. `candidate` reset to 2 after the negative test (version 3 = base model, never live).

## Local structure and deployment contracts

Local regression scripts are grouped under [tests](tests/README.md). Both
`release.py` and direct test commands use their new paths; the scorer handler
test still imports the synthetic generator from `tests/test_local.py`.

Runtime and deployment files deliberately stay together:

| Files | Path dependency |
|---|---|
| `app.py`, `rt_core.py`, `bootstrap_logic.py`, `offline_gate.py` | Scorer Dockerfile COPY and `app.handler`; offline gate executes inside that image |
| `explainer_app.py`, `explainer_common.py` | Explainer Dockerfile COPY and `explainer_app.handler`; common code is also used by export/training/evaluation |
| `eval_explainer.py` | `release.py` invokes it directly during the promotion gate |
| `explainer_export.py`, `kaggle_explainer.py`, `kaggle_links.py` | Coupled export/Kaggle handoff commands and shared imports; retained as the documented model workflow |
| `deploy_*.py`, `release.py`, `session*.py`, `rt_control.py` | Shell commands, ZIP packaging, local source reads and Lambda handler names assume this directory |
| `bootstrap.py`, `load_state.py`, `state_check.py`, `validate_rt.py`, `rt_producer.py`, `cost_rt.py` | Session/control uploads and command entry points use these filenames |

Use `realtime/` as the Docker build context (`docker build ... .`) and working
directory for operational commands. Moving these files would require changing
image or upload contracts rather than simply grouping supporting files.

The source is now in this checkout. Deployment still requires project-specific
AWS resources and source/model data: the pinned frozen XGBoost model/schema,
Qwen GGUF, classifier and metadata, replay bootstrap files, teacher references
and baseline comparison objects. These are S3/Kaggle or run artefacts, not all
checked in. Root requirements support Streamlit, not the complete training or
container inference environment. The Dockerfiles define inference dependencies;
release, deployment and live evaluation incur charges and must not be used as
an offline validation command. No continuous production stream is claimed.
