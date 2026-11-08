# Streamlit batch dashboard

Work on the `streamlit` branch. From a clean checkout, launch with:

```bash
git switch streamlit
.venv/bin/streamlit run app.py
```

The default Alert
Queue uses **Demo (offline)**. No credentials or network services are needed.
PyVis is pinned in `requirements.txt`; graph JavaScript is loaded from that
installed package and embedded inline, without CDN requests.

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
same application. No real-time pipeline is integrated.

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
DynamoDB writes, ETL triggers or real-time actions have been added. Live access
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

## Verification

```bash
.venv/bin/pytest tests/unit/ tests/ui/ -q
.venv/bin/ruff check app.py src/ui tests/ui
.venv/bin/python -m pip check
git diff --check
```

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
