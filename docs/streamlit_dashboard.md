# Streamlit analyst dashboard

Work on the `streamlit` branch. From a clean checkout, launch with:

```bash
git switch streamlit
.venv/bin/streamlit run app.py
```

The default Alert Queue uses **Demo (offline)**. No credentials or network services are needed.
PyVis is pinned in `requirements.txt`; graph JavaScript is loaded from that
installed package and embedded inline, without CDN requests.

Navigation: **Alert Queue → Investigation → Real-Time Monitoring → Agent Evidence
Brief (Demo) → BQ2 Money Trail (Legacy)**. Batch and real-time sources have separate
selectors; their loaded records never substitute for one another. The collapsed
**Data and interpretation limits** section summarises the project limitations.

## Offline walkthrough

1. Open the 14 September sample queue. Its ranks and scores are invented fixture
   values; tied/gapped ranks are retained. Top K limits the number of demo rows.
2. Select `txn_samplefanmember` and choose **Open Investigation**. The selected
   member remains active while the representative's overall case brief is shown.
3. Inspect the native member graph and choose a finding's supporting ID. Graph
   transactions highlight directly; references to another member switch members.
   Account-activity and related-alert references show their saved trace records.
4. Browse `txn_samplenograph` for a stored brief with no findings,
   `txn_samplefallback` for fallback, and `txn_demounprocessed` for no saved brief.
5. Open 16 September for the truncated-window fixture. There are no fixtures for
   15 September, so its demo queue is empty.

The existing Agent Evidence Brief (Demo) screen retains all nine original
scenarios, including not-found, code-only and repaired. Legacy BQ2 remains in the
same application. The real-time page reads persisted records only; no pipeline
control is integrated.

The demo persistence adapter wraps copies of the existing fixtures. The
`quiet_truncated` fixture has a 14 September request date but a 16 September alert;
only its in-memory envelope/decision day is normalised to the alert date for daily
browsing. Original fixtures are unchanged. These samples illustrate result shapes
and are **not historical end-of-day batch replays**.

## Live batch reads

Select **Live batch (read-only)** and explicitly choose **Load / refresh queue**.
Selecting the source alone makes no AWS calls. This mode has been tested with
fakes only; real AWS reads have not been exercised as part of this implementation.

The queue reuses `tools.load_day_queue` and `AgentData`, reads decision metadata
by key, and fetches representatives outside the visible slice when necessary.
Cases and full traces are fetched only when an alert is opened. Navigation,
member selection and evidence reruns reuse the session's loaded result. Reload
the queue to refresh saved metadata; repeated queue SQL can use AgentData's cache.

Read prerequisites are the configured v2 agent Athena tables, DynamoDB
`BatchGetItem` on decisions and `GetItem` on cases, and S3 `GetObject` on the stored
trace keys. Athena also needs its usual Glue/S3 access and result-file permissions.
Existing default credential configuration is used lazily; no infrastructure or
IAM changes are made by the UI. Opening a saved brief requires a current-contract
published result; queue browsing and on-demand execution do not.

Missing decisions, empty findings, fallback, failed reads and incompatible saved
data have different labels. Old verdict-based records and inconsistent run IDs,
creation times, membership or brief metadata are rejected. The S3 key stored on
the representative is authoritative. Saved browsing performs no scans, writes or
agent runs and never falls back to demo data. Real saved daily briefs use the agent's end-of-day cutoff; each
graph displays its own visible horizon, observation duration and cap warnings.
These saved-data reads never run the agent.

A loaded queue with no decisions is valid: its alerts remain in BQ1 order with
**No saved brief**. Open any such alert to use the explicit on-demand controls.
The dashboard never runs a batch or publishes missing records automatically.

## Explicit on-demand investigation

Open an alert from the queue. The request controls show its transaction ID,
date, source and BQ1 rank/score. The request targets the **opened queue alert**;
case-member browsing is a separate display operation.

1. Choose an on-demand window between 6 and 72 hours; the default is 48.
2. In Live batch, check the acknowledgement that Athena/Bedrock calls may incur
   charges. Checking it does not run anything.
3. Click **Run Agent Investigation** separately. The existing agent runs
   synchronously and may take 20 seconds or longer. Its own timeout, token,
   validation and spend controls remain authoritative. There is no background
   executor and the UI cannot forcibly cancel an active provider call.
4. The **On-Demand Brief** appears without changing the saved result or writing
   DynamoDB. Ordinary reruns reuse it. Use **Run again** for another explicit
   submission; failures do not automatically retry or replace saved evidence.

The two evidence views are independent:

| View | Membership and observation scope |
|---|---|
| Saved Batch Brief | Original saved case, window, run ID and creation time; historical end-of-day cutoff |
| On-Demand Brief | Unsaved single-alert result, its own requested window and completion time; later payments may be visible up to the dataset horizon |

Switch views to inspect each graph and brief. Evidence references and member
selection are separate between views. Do not interpret different findings as a
controlled comparison: windows, membership and cutoffs can differ.

Session results are keyed by source, transaction ID, date and requested window.
Changing any of these cannot relabel a previous result as the new request.
Results are retained only in the current Streamlit session, not with
`st.cache_data` or DynamoDB.

In Demo mode, the execution button uses only an existing **matching single-alert
fixture** and labels the result simulated. Its original ID, request date and
48-hour window must match. For example, `txn_samplepass`, `txn_samplerepaired`
and `txn_samplefallback` work on 14 September. Grouped fixtures, member-only IDs,
the invented unprocessed alert and unsupported windows have no simulated result.
The truncated fixture's inconsistent request date also prevents a matching
16 September simulation. No fixture is rewritten or substituted to make a run
appear successful. All nine scenarios remain available in the separate Demo
viewer.

## External operations and errors

| Action | External operation |
|---|---|
| Open app, select a source, browse Demo | None |
| Load / refresh Live queue | Existing Athena queue query and keyed DynamoDB reads |
| Open a saved Live investigation | Keyed DynamoDB case read and S3 trace read |
| Acknowledge costs | None |
| Click approved Live Run / Run again | Existing Athena/Bedrock investigation agent |
| Change evidence, case member, result view or expand trace | None |

The legacy BQ2 submit action retains its original Athena behavior. No SQS reads,
DynamoDB writes, ETL triggers or real-time pipeline execution have been added. Live access
and inference have **not** been tested against AWS during this implementation.

On-demand permissions additionally require Bedrock model invocation for the
configured `AGENT_MODEL_ID` and the agent's label-free Athena/Glue/S3 access.
These augment the batch-read permissions above; the UI changes no IAM policies.

Authentication failures ask you to refresh the environment's normal AWS login
outside the application. Permission failures ask you to check access for the
specific action. Missing tables/resources, transient service failures, empty
queues, missing saved decisions and incompatible records remain distinct.
The source stays Live on failure; no sample data is substituted. No credential,
token, stack trace or full AWS response is shown.

Errors include a debug reference. Sanitised server logs contain its category and
exception type; the original exception is retained in the server-side failure
object (`cause`) for debugger inspection, never rendered as JSON in the UI.
After correcting access or connectivity, retry with a new explicit action.

## Real-time monitoring journey

1. Select **Real-Time Monitoring**. Its source defaults to **Demo (offline)**.
2. Leave **Sample session: mixed alerts** selected and click **Load known alerts**.
   The session is a clearly invented presentation group containing three alerts,
   not a claim that a production session manifest exists.
3. Read `demo_rt_complete`, including its invented explanation, next checks and
   typology. These are separate Lambda-style interpretations, not batch-agent
   validated findings.
4. Select `demo_rt_pending` to show an alert whose brief was not stored. Click
   **Refresh loaded alerts** deliberately. Demo fixtures are static: refreshing
   does not pretend that Kinesis or Lambda completed a run.
5. Other sample sets cover missing optional fields, missing alerts, separate alert
   and brief read failures, limited narrative, an incompatible record, and a brief
   without its matching alert. The last example does not establish an actual
   arrival order or TTL policy.

For live reads, choose **Live DynamoDB (read-only)**, enter known transaction IDs
(one per line or comma-separated, at most 20), and click **Load known alerts**.
The service performs keyed reads of the alert and corresponding brief. It keeps
input order and deduplicates IDs; this is **not a chronological recent feed**.
There is no live session selector or automatic listing, because no index or
bounded production ID manifest contract was available locally. No scan is used.

Selecting a source, choosing a loaded record, expanding details or navigating
away and back performs no reads. The snapshot and selected record remain in the
session until explicit refresh or source change. **Refresh loaded alerts** reads
the previously loaded ID set; changed form inputs require **Load known alerts**.
The displayed lookup time is retrieval time, not event freshness or processing
latency. The page does not report pipeline health, infer a permanently missing
record or declare a failed explanation from an absent brief alone.

### Real-time contract: evidence and explicit gaps

Source: `PLAN.md`, section 16, **F21**; the pipeline names also appear in `README.md`.
Searches of this repository, local GitHub project folders, BigData coursework,
Downloads and Desktop did not find the Lambda writers, the ETL v2 handover,
persisted record examples, a session runner or a session manifest. No AWS metadata
was read during implementation.

| Item | Documented or verified locally | Live adapter behavior |
|---|---|---|
| Alert table | `is459_rt_alerts`, key named `txn` | `GetItem(Key={"txn": supplied_id})` |
| Brief table | `is459_rt_briefs`, key named `txn` | Same requested ID, exact identity match required |
| Alert `amount` | Documented as a string | Preserved as supplied; nullable/absent is unavailable |
| Model reasons | F21 says none on real-time alerts | Not displayed |
| Narrative, next checks, typology | Content concepts documented; exact attribute names unknown | Record existence shown, content mapping explicitly unavailable |
| Partition/sort-key types and GSIs | Not independently verified | String `txn`-only request is the documented-key assumption; key validation errors are incompatible, never a scan fallback |
| Alert/brief timestamps, score/rank, currencies, counterparties | Exact persisted field names/types/semantics unknown | Not guessed or mapped in live mode |
| Processing status, failure markers, TTL and retention | Unknown | Missing briefs remain pending/unavailable; no permanent failure claim |
| Session identity / recent-ID manifest | Not found locally | Explicit known-ID lookup only |

The isolated mappers `map_documented_alert` and `map_documented_brief` in
`src/ui/services/realtime.py` deliberately map only documented names. Even a field
that looks like `narrative` or `score` is not trusted as the real contract without
the writer's schema. Raw DynamoDB records are never dumped into the page.

`AlertSummary`, `Explanation` and `RealtimeRecord` are **UI presentation DTOs**,
not asserted Lambda output schemas. Demo timestamps, sender/receiver, currency,
score and explanation text are invented directly in those DTOs. They do not
establish production field names. The original nine agent fixtures are unchanged.

The live read service exposes `RealtimeService.lookup(txn)` and
`lookup_many(known_ids)`. It creates a DynamoDB resource lazily only on explicit
lookup. It makes at most two `GetItem` operations per supplied unique ID. No
chronological query or pagination interface is claimed; there is no verified
listing access pattern to paginate. Reads of the two tables are separate, not a
transactional snapshot. Errors remain distinct from missing records, and schema
or identity mismatches are rejected.

Before completing the content adapter or enabling a controlled live smoke test,
obtain from the pipeline owner:

- Redacted alert and brief items for the same `txn`, including optional/null,
  delayed, failed or limited-content examples.
- Table `KeySchema`/attribute types, any GSIs and their key schemas, and TTL
  configuration. Confirm whether either table requires a sort key.
- Scorer/explainer record-writing source and the meanings/formats of timestamps,
  score, narrative, next checks, typology and status attributes.
- The session-runner handover and any bounded manifest/recent-ID source. Otherwise
  continue using explicit known IDs rather than adding a table scan.

Live real-time lookup requires the normal credential chain and DynamoDB
`GetItem` permission on both configured tables in the project region. This page
needs no SQS, Kinesis, Lambda, Bedrock, Athena or write permissions. It makes no
infrastructure changes and has not been tested against AWS during this milestone.

## Final laptop rehearsal

Use the existing project environment and check dependencies with
`.venv/bin/python -m pip check`. Launch on `streamlit`, keep both source selectors
in Demo, and rehearse the saved-case journey above. For a simulated on-demand
comparison, open `txn_samplepass` and submit its original 48-hour request. Then
run the real-time sample-session walkthrough. All three journeys work offline;
no recording is created by these checks.

For a separately authorised live rehearsal, confirm credentials, action-specific
permissions, current batch publications and the real-time schema prerequisites
above. Obtain real-time transaction IDs from the pipeline owner. Loading records
does not start a stream and does not prove that a pipeline is currently healthy.
Keep the offline sample data available as a clearly labelled fallback selected
by the presenter, not an automatic substitution after a live error.

The external `WORK_SPLIT.md` dated 6 November documents **`python3 session.py stop`**
after every real-time session. The runner and its actual working directory are
not present locally. If a session was started outside this dashboard, have the
pipeline owner run that documented command from the verified runner directory;
do not guess a directory or run it from this repository. This dashboard neither
starts nor stops sessions. No shutdown command was executed during implementation.

## Verification

```bash
.venv/bin/pytest tests/unit/ tests/ui/ -q
.venv/bin/ruff check app.py src/ui tests/ui
.venv/bin/python -m pip check
git diff --check
```

The lightweight end-to-end offline readiness check is:

```bash
.venv/bin/pytest tests/ui/test_demo_readiness.py -q
```

It exercises ranked queue selection, saved cases, graph/evidence navigation,
member selection, provenance, explicit simulated on-demand execution and result
switching, then completed/pending real-time records and manual refresh. All UI
tests use fail-on-use SDK and agent guards, covering SQS, DynamoDB writes,
Kinesis/Lambda calls and any other real SDK operation as well as Athena/Bedrock.

UI tests fail on AWS client creation, SDK operations, Athena execution, Bedrock
calls or real agent execution. Persistence tests inject fake DynamoDB/S3 objects
and a fake Athena runner. The repository's 14 pre-existing Ruff diagnostics remain
outside this UI change.

Inject `LiveBatchService(data=fake_agent_data, saved=fake_reader)` for batch
integration tests and `OnDemandService(runner=fake_investigate)` for execution
tests. The latter validates using existing agent validators and returns the
runner's result unchanged. Streamlit tests inject these through session-scoped
`ui_batch_backends` and `ui_on_demand_services` maps. Passive navigation, separate
cost acknowledgement, exact call counts, request identity, independent evidence
views and safe error rendering are all tested without AWS.
