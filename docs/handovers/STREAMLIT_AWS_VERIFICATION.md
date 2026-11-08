# Streamlit AWS verification

**Date:** 8 November 2026 (Asia/Singapore)
**Branch / resumed baseline:** `streamlit`, `c2ef834`
**Region:** `us-east-1`
**Outcome:** bounded AWS read verification succeeded after the user refreshed login.
At the Prompt 7 boundary, browser and live on-demand execution were untested.
The Prompt 8 section below records subsequent actual Chrome verification and the
subsequent explicitly approved on-demand run. This is a sampled
integration check, not a pipeline health assessment.

## Identity and Git

The first attempt stopped at Botocore `LoginRefreshRequired`, before an identity
response. The user then signed in; one resumed STS GetCallerIdentity succeeded.
Account identifiers, credentials and identity responses were not displayed or
committed. The earlier authentication categorisation fix remains covered by tests.

The previously authorised push published `42498b1` on `streamlit`. The subsequent
authentication fix and this verification work remain local; no further push was
performed. `main` was not modified. No infrastructure, IAM or credential files
were changed.

## Metadata verified

Three Glue GetTable reads confirmed Parquet tables in `is459_aml_gold`, each
partitioned by **`txn_date date`**:

| Table | Non-partition columns and Glue types |
|---|---|
| `agent_day_queue_v2` | transaction_id/source_account_key/target_account_key: string; transaction_timestamp: timestamp; risk_score: double; risk_rank_global/risk_rank_day: bigint; has_reasons: boolean |
| `agent_scores_v2` | transaction_id/source_account_key/target_account_key: string; transaction_timestamp: timestamp; risk_score: double; risk_rank_global/risk_rank_day: bigint; amount_paid: decimal(23,6); payment_currency/payment_format: string; feature_1..3/reason_1..3: string; contribution_1..3: double |
| `agent_edges_v2` | account_key/counterparty_key/transaction_id: string; direction: varchar(3); transaction_timestamp: timestamp; amount_paid: decimal(23,6); payment_currency/payment_format: string |

DescribeTable and DescribeTimeToLive confirmed:

| DynamoDB table | Status | Primary key | GSI / LSI | TTL |
|---|---|---|---|---|
| `is459_agent_decisions` | ACTIVE | transaction_id (HASH, S) | None | DISABLED |
| `is459_agent_cases` | ACTIVE | day (HASH, S), case_id (RANGE, S) | None | DISABLED |
| `is459_rt_alerts` | ACTIVE | txn (HASH, S) | None | DISABLED |
| `is459_rt_briefs` | ACTIVE | txn (HASH, S) | None | DISABLED |

These results establish the tested metadata and read access only. They do not
prove every permission needed for arbitrary queries or agent execution.

## One bounded queue query and saved case

Exactly one Athena execution used `tools.load_day_queue` with a runner configured
without automatic retries. Its reviewed SQL selected seven queue columns from
`agent_day_queue_v2`, with `txn_date = DATE '2022-09-14'`,
`risk_rank_day <= 5`, ordered by risk_rank_day. No diagnostic query was needed.

- Rows: **5**, daily ranks **1–5**, valid transaction IDs, scores and timestamps;
  order preserved through queue enrichment.
- Bytes scanned: **400,986**; engine time: **903 ms**; measured wall time:
  **3,327 ms**.
- One BatchGetItem returned all five requested decision records with compatible
  current metadata: **four saved briefs and one saved brief with no findings**.
  Missing/incompatible/legacy decisions were not observed in this sample.
- One selected case was fetched by its day/case key and its full trace fetched
  using the representative's stored S3 trace key. Envelope and nested-result
  validation succeeded: **three members, three findings, fallback false**.
- All three native member graphs and bundled HTML built locally from that saved
  result, with no further Athena query.

The sampled decisions were representative records; an additional member-pointer
fetch was not needed. Member-pointer resolution, missing/old/incompatible records
and fallback remain covered by synthetic offline tests, not by this live sample.
No population-wide saved-result coverage is claimed.

## Real-time writer and keyed reads

A bounded ListObjectsV2 of `s3://is459-aml-g1t5/rt/code/` (MaxKeys 25) found the
writer exports. Eight objects were read as text, never executed: `README.md`,
`app.py`, `explainer_app.py`, `explainer_common.py`, `session.py`,
`session_helper.py`, `deploy_rt.py`, `deploy_session.py`. Exported source was
corroborated by real persisted items; deployed container digests were not checked.

A separate listing of `rt/briefs/` (MaxKeys 5) yielded two distinct transaction IDs
from writer-defined object names. These are a bounded key sample, not recent-time
ordering or a session manifest. Two alert/brief pairs were read by exact `txn`
keys: **four GetItem calls**, both pairs available, both source statuses `ok`.
No real-time brief object bodies were fetched from S3. Missing/delayed/failed
briefs were not observed live and remain synthetic regression scenarios.

| Persisted fields | Verified encoding / display behavior |
|---|---|
| txn | String key, exact requested identity required on both records |
| Alert amount, txn_ts, sender, receiver, currency, payment_format | Strings; amount preserved; txn_ts is dataset transaction time, with no invented timezone |
| Alert score, threshold | DynamoDB Number, returned as Decimal; score mapped, threshold not shown as a rank or finding |
| Alert model_version, scored_at_ms, arrival_ms, ingest_to_score_ms | String model and numeric scoring/arrival epochs and reported latency; source epochs displayed in UTC, separate from transaction time |
| Brief narrative, typology | Strings, displayed as pipeline interpretation separate from agent evidence |
| Brief next_checks | String containing a JSON list of strings; parsed explicitly |
| Brief status | `ok` or `needs_review`; the latter is the writer's template fallback after unsuccessful validation attempts |
| Brief model_version, latency_ms | String model and numeric explainer duration; duration is not total queue latency |
| Brief confidence, input, typology_hint, score, txn_ts, alert_scored_at_ms | Persisted source fields; no raw input dump or calibrated confidence badge added |
| Brief completion timestamp | Not stored in the DynamoDB writer; S3-only written_at_ms is not inferred from score time or lookup time |

The adapter now maps the verified fields and rejects malformed known encodings.
Unknown fields are ignored. The source's `ok` status reflects its schema/numeric
grounding checks; it does not prove the narrative interpretation correct.
Neither ranks nor model reasons nor session IDs are invented.

Both tables have no chronological index. The exported session helper obtains IDs
with DynamoDB Scan; that code was read but never executed or reused. The UI keeps
explicit known-ID lookup and manual refresh. No recent-feed/session-health claim
or automatic pipeline control is enabled. `session.py stop` is present in the
export, but no session command was run and no local runner directory is assumed.

## Workflow and regression verification

After cloud reads, the actual recorded responses were replayed locally through
LiveBatchService and Streamlit AppTest, with SDK calls prohibited. Explicit queue
load produced the same five ordered rows; opening the selected saved case rendered
a native graph iframe. Two recorded real-time pairs were replayed after restoring
observed Decimal fields, and their mapped explanation text rendered successfully.
Passive reruns made no additional reader calls. This is local application-path
verification using recorded responses, **not a second live query or an interactive
visual browser test**. Interactive browser automation was unavailable.

Only invented records enter the committed regression tests. New cases cover
Decimal score/timestamps, JSON-string next checks, `needs_review`, unknown fields,
malformed JSON/types/status, non-finite/bool scores and out-of-range epochs.

- Full unit/UI suite: **753 passed** (743 previous plus 10 new cases).
- Focused real-time suite: **49 passed**.
- UI Ruff, Python compilation, dependency consistency and Git whitespace checks:
  passed.
- Repository-wide Ruff: **14 unchanged baseline findings**, outside these UI
  changes; compared with `c2ef834`.

## Operation accounting and remaining limits

Resumed explicit API calls (SDK retries disabled for verification):

| Service | Operations / counts |
|---|---|
| STS | GetCallerIdentity 1 successful; earlier credential-provider failure recorded separately |
| Glue | GetTable 3 |
| DynamoDB | DescribeTable 4; DescribeTimeToLive 4; BatchGetItem 1; GetItem 5 (one case, four real-time records) |
| S3 | ListObjectsV2 2; GetObject 9 (eight source exports, one saved trace) |
| Athena | StartQueryExecution 1; GetQueryExecution 2; GetQueryResults 1 |

Athena's normal service-managed query output was the only query-result write.
No application data was written. No Scan, Bedrock, agent investigation, batch,
ETL, Lambda invocation, Kinesis, SQS, IAM or infrastructure mutation occurred.
No additional cloud calls were made for the replay or regressions.

Temporary raw records, query identifiers and writer exports are outside Git under
`/private/tmp/bda-streamlit-verify/`; none are included in this commit. The report
contains aggregate counts, public project resource names and field contracts only.

Remaining verification limits: interactive visual browser testing, live on-demand
agent execution (deliberately excluded), unsampled saved-result states, real-time
missing/delayed/fallback examples, and a bounded chronological/session ID source.
Authentication is no longer the blocker. Offline demo and explicit live-read
paths remain separate; failed reads never substitute sample data.

## Prompt 8 — live browser verification and approval boundary

**Date:** 8 November 2026 (Asia/Singapore). **Baseline:** `streamlit`,
`36019e9`, initially clean and aligned with `origin/streamlit`. This section
supersedes the earlier interactive-browser limitation. No merge, push or change
to `main` occurred.

STS identity succeeded again without displaying identifiers. The existing app
was launched from the project environment on `http://127.0.0.1:8502`. Installed
Chrome was controlled headlessly through its local DevTools protocol; screenshots
were inspected. No browser package or monitoring tool was installed. A temporary
Python startup guard outside Git recorded SDK operation names and permitted only
the bounded reads below, blocking Bedrock runtime, scans, writes and a second
queue submission. No app/agent code was replaced with replay responses.

### Actual browser results

- **Queue:** switching to Live made zero AWS calls. Explicit load for
  `2022-09-14`, top K 5, returned five real alerts in ranks 1–5. Screenshot and
  browser table checks confirmed ordering, displayed scores, Live source and
  saved availability; no demo transaction appeared. Four decisions had findings
  and one had none. One Athena execution scanned **400,986 bytes**, with
  **682 ms engine** and **873 ms total Athena execution time**. These are this
  run's service metrics, separate from Prompt 7's timing.
- **Saved case:** opened the rank-one representative and verified its saved run
  identity, case ID, three members, three finding sentences, gaps, next checks,
  money-flow presentation, execution metadata and tool trace. Switched through all
  three members in Chrome. Each graph canvas rendered and its embedded node/edge
  IDs matched the corresponding saved member graph. All three saved graphs have
  **zero surrounding payments and one anchor payment**; this is visibly labelled,
  not a claim of three expanded networks. Overall findings can cite account
  activity in the saved trace outside those displayed graphs. No per-currency
  metric rows were supplied for these members; the UI shows the explicit empty
  state rather than fabricated totals. Nonempty currency-table formatting remains
  covered by offline tests.
- **Evidence:** clicked both trace-based and graph-based real supporting IDs.
  Trace details appeared for references outside the graph; graph references
  selected the appropriate member and highlighted the exact edge. Expanded
  execution metadata and the tool trace. These actions, member switches and
  cost-acknowledgement toggles and a graph-canvas click made **zero additional AWS
  calls**. No agent ran.
- **On-demand gate:** the real Live button was disabled before acknowledgement,
  enabled after checking it, and disabled again after clearing it. It was never
  clicked. Checking acknowledgement alone caused no AWS operations.
- **Real-time:** selected Live without reads, then explicitly loaded the same two
  known IDs retained outside Git from Prompt 7. Four keyed GetItem calls returned
  two available pairs. Both displayed matching source timestamps, amounts,
  numeric scores, narratives and parsed next-check strings; screenshots were
  inspected. Both source statuses were `ok`; no fallback/missing brief occurred
  in this live sample. Missing/fallback/failure rendering remains verified by
  existing offline tests, not manufactured live records.
- **Navigation:** switching among queue, investigation and real-time pages reused
  loaded state with zero additional AWS calls. Graph/evidence selection and
  trace expansion did not submit Athena or Bedrock operations. No actual failed
  cloud read was induced; the no-demo-on-failure behavior remains covered by the
  regression suite. Offline demos and legacy BQ2 passed that suite.

No product integration defect was found, so no production code or new fixtures
were changed. Browser-driver timing/selector corrections were confined to the
private temporary automation scripts.

### Observed operations for Prompt 8, before approval

| Service | SDK operations |
|---|---|
| STS | GetCallerIdentity 1 |
| Athena | StartQueryExecution 1; GetQueryExecution 2; GetQueryResults 1 |
| DynamoDB | BatchGetItem 1; GetItem 5 (one case and four real-time items) |
| S3 | GetObject 1 (stored case trace) |
| Bedrock control plane | GetInferenceProfile 1; GetFoundationModelAvailability 1 |
| Bedrock runtime / agent execution | **0** |

These are SDK operation counts, not packet-level attempt telemetry. No metadata
reinspection, S3 listing, Scan, application write, pipeline run or infrastructure
change was needed. Athena wrote only its normal query output. Raw browser text,
screenshots, response data, instrumentation and known real-time IDs remain under
`/private/tmp/bda-streamlit-prompt8/` or the previous private temporary directory,
outside Git.

### Prepared invocation — before approval

Proposed request, through the existing Streamlit button after explicit approval:

```python
investigate(
    "txn_613ac569efe8fc2e024adfbeae71b746",
    "2022-09-14",
    window_h=48,
)
```

This is daily rank **1**, BQ1 score **0.9975132942199707**, with an existing
compatible saved batch brief. The saved case has three members; the proposed
on-demand result is an unsaved **single-alert** investigation. Its observation
cutoff may include later payments, unlike the batch end-of-day cutoff, so a
finding difference is not a controlled model comparison.

The configured model is `us.anthropic.claude-sonnet-4-5-20250929-v1:0` in
`us-east-1`. GetInferenceProfile returned ACTIVE / SYSTEM_DEFINED with three
model destinations. GetFoundationModelAvailability returned agreement,
entitlement and region AVAILABLE and authorization AUTHORIZED. These metadata
results do **not** prove a Converse call will succeed under runtime IAM, routing,
capacity or service-control restrictions. No runtime probe was sent.

Inspected safeguards in `investigate.py`, `llm.py`, `loop.py` and `config.py`:

| Control | Implemented behavior |
|---|---|
| Replies | Eight ordinary LLM replies; at most one additional brief-repair reply |
| Tool requests | At most four handled tool requests per ordinary reply |
| Output | Explicit maximum 1,500 tokens per reply |
| Run tokens | 120,000 input + output; checked before a reply, so one reply can overshoot |
| Run time | 150 seconds checked before each LLM reply; seed reads, active provider calls, fallback/final graph work are not forcibly cancelled |
| Provider timeout | Bedrock read timeout 90 seconds per HTTP attempt; not a whole-run cancellation guarantee |
| Estimated spend | $10 process-wide estimated LLM cap, shared by runs; checked before a reply, not reserved or a hard billing cap |
| Estimator | Configured $3 / million input tokens and $15 / million output tokens; not independently verified current AWS billing rates |
| Persistence | On-demand result stays in Streamlit session; saved batch result is preserved; no DynamoDB publication |

Expected operations are a date-filtered alert lookup plus a variable number of
label-free Athena score, case-graph, account-activity, neighbour-score and
related-alert queries chosen by the agent. Graph completion or code-only fallback
can perform additional Athena reads. Bedrock uses Converse over multiple turns
(up to eight ordinary plus one repair), not one HTTP request. Athena scan volume
and exact token use cannot be known before execution.

For scale only, the existing **grouped saved run** reports 22,186 input and 1,616
output tokens: **$0.090798 using the configured estimator**, plus Athena. This is
historical saved metadata, neither a new execution nor a prediction or exact AWS
bill for the proposed single-alert run. New model token, latency and cost results
are unavailable because the proposed run has not occurred.

The production Bedrock client currently configures three SDK retries (up to four
HTTP attempts), and the Athena runner can retry internal query failures. To meet
Prompt 8's **no automatic retry** condition, any approved run must use a temporary
test-session guard that suppresses SDK retries and blocks repeated failed query
submission. Agent rules, prompts, token/time/spend settings and production retry
code will remain unchanged. No second investigation or failure-driven resubmission
is authorised. A provider failure must be recorded and stop the test; an existing
backend code-only fallback must be labelled accurately if returned.

**At this checkpoint, explicit approval was pending and no investigation had
run.** The subsequent approved-execution section below supersedes that state.

### Regression status

Full unit/UI suite: **753 passed**; UI Ruff and dependency checks passed. The same
14 repository-wide Ruff findings remain in files unchanged from `36019e9`.
Whitespace checks passed. No code fix required a new regression test. At this
checkpoint, on-demand execution/runtime access and its measurements were still
unverified; the next section records their verification. Unsampled live
missing/fallback states remain a limitation.


### One approved live investigation — completed

The user explicitly approved the proposed request with **“yes i approve”**.
On 8 November 2026, exactly one on-demand investigation was submitted using the
existing app's **Run Agent Investigation** button for
`txn_613ac569efe8fc2e024adfbeae71b746`, `2022-09-14`, window **48 hours**.
No second investigation or resubmission occurred.

A separate local test process on port 8503 rendered the unchanged repository
`app.py` through a private wrapper. It preserved the original UI, OnDemandService,
agent, model, prompts and analytical safeguards. Process-scoped instrumentation:

- Set SDK `total_max_attempts=1`, preserving other client settings including the
  model read timeout, and set the Athena runner's internal attempt count to one.
- Required the exact approved request and created an exclusive durable marker
  before calling the original service, blocking a second investigation.
- Allowed Bedrock Converse only during that call, with the configured model and
  1,500-token reply limit; enforced at most nine turns.
- Allowed only Athena operations during agent execution, blocking writes, scans,
  session/pipeline operations and any unrelated service access.

These controls live outside Git and do not modify production retry code or
backend decisions. The wrapper made no substitute model/data responses. The
cost acknowledgement was visibly required and checked before the single button
click. One refreshed Live top-five queue and one saved case/trace read prepared
this new browser session; these are counted separately below. STS succeeded
again without displaying identifiers.

| Actual result | Value |
|---|---|
| Status | `ok` |
| Model | `us.anthropic.claude-sonnet-4-5-20250929-v1:0` |
| Validation | `valid`; repair_used false; fallback false |
| Agent turns / Converse operations | **3 / 3** |
| Input / output tokens | **17,500 / 2,018** |
| Backend latency / measured service wall time | **49,544 / 49,561 ms** |
| Result membership | **One** member, case_id null; correct approved alert/date/window |
| Findings / gaps / next checks | **3 / 0 / 2** |
| Tool trace | **7** steps |
| Native graph | Anchor plus **one onward payment**, three account nodes; real browser canvas rendered |
| SDK retry attempts / provider errors | **0 / 0** |
| Configured estimated model spend | **$0.08277**, excluding Athena; estimate, not an AWS billing amount |

The UI displayed the separate **On-Demand Brief**, actual model/token/latency
metadata, all finding and next-check sentences, the explicit no-gaps state, and
the tool trace. The embedded graph's transaction IDs matched the returned native
member graph, and screenshots were inspected. A supporting evidence reference
was clicked successfully. Switching to Saved Batch Brief retained its original
three-member case/run/summary; switching back restored the single-alert result.
Page changes and evidence/view reruns made **zero additional AWS operations**
and did not repeat execution. The cost acknowledgement was cleared using browser
pointer input and Run again became disabled, also without AWS calls. No product
defect was found and no code fix was needed.

Both saved and on-demand briefs returned finding codes `FAN_OUT`,
`NEIGHBOURS_HIGH_RISK` and `REPEAT_ACCOUNT_ALERTS`. That agreement is not proof of
identical evidence: the saved case has three members and an end-of-day cutoff;
the on-demand result has one member and 48 hours after the alert, including one
onward payment absent from the saved graph. The on-demand horizon is explicitly
shown. The saved batch publication was never overwritten.

Operations **for this approved-execution turn only**, additional to the earlier
Prompt 8 reads:

| Phase | Actual operations |
|---|---|
| Identity | STS GetCallerIdentity 1 |
| Browser preparation | Athena StartQueryExecution 1, GetQueryExecution 2, GetQueryResults 1; DynamoDB BatchGetItem 1, GetItem 1; S3 GetObject 1 |
| Single agent execution | Athena StartQueryExecution **10**, GetQueryExecution **25**, GetQueryResults **10**; Bedrock Converse **3** |

Preparation query: **400,986 bytes**, **853 ms engine**, **1,047 ms Athena total**.
The agent's ten successful queries scanned **130,973,587 bytes** combined, with
**8,931 ms summed engine time** and **11,211 ms summed Athena execution time**.
These sums cover multiple queries and are not whole-run wall latency. Athena
performed its usual query-output writes only. No DynamoDB writes, SQS/Kinesis
consumption, Lambda invocation, ETL, agent batch, IAM or infrastructure change
occurred. Returned SDK metadata reported zero transport retries on every logged
operation. No failure-driven query resubmission occurred.

The model estimate uses the existing configured rates:
`17,500 × $3 / million + 2,018 × $15 / million = $0.08277`.
It excludes Athena and is not a verified billing charge. Raw results, response
telemetry, marker files, screenshots and the temporary wrapper remain outside Git
under `/private/tmp/bda-streamlit-prompt8/`. No real record or generated output
was added to fixtures or committed.

Full unit/UI regressions ran again: **753 passed**. UI Ruff and dependency checks
passed; repository Ruff remains the same 14 unrelated diagnostics in unchanged
files. No new regression test was needed because no product bug was found.

The one approved invocation is consumed. Additional investigations require new
approval. Remaining limits are broader population coverage, real-time missing/
fallback states not observed live, exact AWS billing, and interruption behavior
for a provider timeout (not deliberately induced). A successful run does not
turn the backend's between-call timeout into a hard cancellation guarantee.
