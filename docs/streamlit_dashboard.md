# Streamlit analyst dashboard

Work on the `streamlit` branch. From a clean checkout, launch with:

```bash
git switch streamlit
.venv/bin/streamlit run app.py
```

The default Alert Queue uses **Demo (offline)**. No credentials or network services are needed.
PyVis is pinned in `requirements.txt`; graph JavaScript is loaded from that
installed package and embedded inline, without CDN requests.

Navigation: **Alert Queue → Investigation → Real-Time Alert Lookup → Agent Examples**. Batch and real-time sources have separate
selectors; their loaded records never substitute for one another. The collapsed
**Data and interpretation limits** section summarises the project limitations.

## Analyst review experience

The queue opens as **AML Alert Review**, with a short three-step guide. Source
selection is in the sidebar; date, review limit and refresh are together above
six columns: daily rank, transaction ID, risk score, transaction time, brief
status and finding count. New sessions start with 20 alerts, enough to include
all nine current 14 September Demo examples. The configured backend maximum is
unchanged. Select a table row and choose **Open Selected Investigation**. The
selected-alert detail panel retains secondary fields; no second transaction
dropdown is required.
The table always preserves the supplied BQ1 order.

Every loaded queue has a new table generation. Selection is validated before
keyed widgets render, including date/source/limit changes and page returns.
An invalid Open callback shows a friendly notice, keeps the active queue and
performs no saved-result read. Empty queues disable investigation navigation.

Investigations show the supplied summary and plain-English finding headings,
followed by the native graph alongside a readable supporting-transaction card.
Click a finding citation to see supplied sender/receiver, amount/currency, time,
role and relative gap, with the matching edge labelled **Selected**. Trace-only
references show their source tool/step, explicit account-activity direction when
available, and missing fields honestly. Raw evidence JSON is optional.

Money-flow metrics, observations and execution metadata are expandable. Gaps
and recommended next checks remain readable, and interpretations stay separate
from validated findings. **Advanced: Run a new AI investigation** is below the
saved result and collapsed by default; its acknowledgement/button and independent
session results are unchanged. No new execution occurs when inspecting evidence.

See the [UX verification and screenshots](handovers/STREAMLIT_UX_VERIFICATION.md)
for the guarded offline browser rehearsal and regression coverage.

## Offline walkthrough

1. Open the 14 September sample queue. Its ranks and scores are invented fixture
   values; tied/gapped ranks are retained. Top K limits the number of demo rows.
2. Select `txn_samplefanmember` and choose **Open Selected Investigation**. The selected
   member remains active while the representative's overall case brief is shown.
3. Inspect the native member graph and choose a finding's supporting ID. Graph
   transactions highlight directly; references to another member switch members.
   Account-activity and related-alert references show their saved trace records.
4. Browse `txn_samplenograph` for a stored brief with no findings,
   `txn_samplefallback` for fallback, and `txn_demounprocessed` for no saved brief.
5. Open 16 September for the truncated-window fixture. There are no fixtures for
   15 September, so its demo queue is empty.

The existing Agent Examples screen retains all nine original
scenarios, including not-found, code-only and repaired. Legacy BQ2 is removed from
application navigation; its original root UI is retained behind an unconditional
stop, and the shared BQ2 backend and native investigation graph remain intact. The real-time page reads persisted records only; no pipeline
control is integrated.

The demo persistence adapter wraps copies of the existing fixtures. The
`quiet_truncated` fixture has a 14 September request date but a 16 September alert;
only its in-memory envelope/decision day is normalised to the alert date for daily
browsing. Original fixtures are unchanged. These samples illustrate result shapes
and are **not historical end-of-day batch replays**.

## Live batch reads

Select **Live batch (read-only)** and explicitly choose **Load / refresh queue**.
Selecting the source alone makes no AWS calls. Bounded AWS verification on
8 November 2026 loaded five real queue rows, compatible decision metadata and one
saved case/trace. See the [verification report](handovers/STREAMLIT_AWS_VERIFICATION.md)
for query costs, sampled coverage, successful Chrome and on-demand verification,
and remaining limitations. Use the [live-demo runbook](handovers/STREAMLIT_LIVE_DEMO_RUNBOOK.md)
for the final presentation and the [review handoff](handovers/STREAMLIT_REVIEW_READINESS.md)
for completion status and offline regression results.

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
4. The **New Investigation** appears without changing the saved result or writing
   DynamoDB. Ordinary reruns reuse it. Use **Run again** for another explicit
   submission; failures do not automatically retry or replace saved evidence.

Report controls reflect what is available:

- Saved only: **Investigation Report**, with retrieval/no-rerun guidance and no radio.
- Saved and new: choose **Previous Investigation** or **New Investigation**.
- New only: **New Investigation Report**, with no radio.
- Neither: a helpful empty state and the existing explicit advanced execution controls.

Live saved reports identify AWS retrieval; Demo reports identify synthetic fixtures.
Old report-selection labels are migrated before keyed widgets render. Changing the
number of reports never submits an investigation. The two reports are independent:

| View | Membership and observation scope |
|---|---|
| Previous Investigation | Original saved case, window, run ID and creation time; historical end-of-day cutoff |
| New Investigation | Unsaved single-alert result, its own requested window and completion time; later payments may be visible up to the dataset horizon |

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

The legacy BQ2 submit action is inaccessible from this application. No SQS reads,
DynamoDB writes, ETL triggers or real-time pipeline execution have been added.
Bounded live reads and exactly one separately approved live on-demand investigation
succeeded. That approval is consumed; do not execute another investigation for a
presentation without new approval. Historical measurements are in the verification
report. The result is retained only in its original Streamlit session; use the
report or clearly labelled offline samples if that session is unavailable.

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

## Real-Time Alert Lookup journey

1. Select **Real-Time Alert Lookup**. Its source defaults to **Demo (offline)**.
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
Empty input shows “Enter at least one transaction ID from the real-time pipeline.”
More than 20 IDs, oversized IDs and IDs containing spaces/control characters show
local input guidance before service/client creation, with no debug reference or
new retrieval timestamp. A compatible previously loaded snapshot, selected record
and lookup time remain usable after invalid input. Refresh still uses the loaded
ID set, not invalid text in the form. Batch queue IDs are not necessarily present
in the separate real-time pipeline; no batch IDs are filled in automatically.

The service performs keyed reads of the alert and corresponding brief. It keeps
input order and deduplicates IDs; this is **not a chronological recent feed**.
There is no live session selector or automatic listing: deployed tables have no
chronological index, and the exported session helper uses Scan rather than a
bounded ID manifest. No scan is used by this UI.

Selecting a source, choosing a loaded record, expanding details or navigating
away and back performs no reads. The snapshot and selected record remain in the
session until explicit refresh or source change. **Refresh loaded alerts** reads
the previously loaded ID set; changed form inputs require **Load known alerts**.
The displayed lookup time is retrieval time, not event freshness or processing
latency. The page does not report pipeline health, infer a permanently missing
record or declare a failed explanation from an absent brief alone.

### Real-time contract: evidence and explicit gaps

The deployed table metadata, exported writers under
`s3://is459-aml-g1t5/rt/code/`, and two keyed alert/brief pairs were checked on
8 November 2026. Source exports were read as text; no runner or writer was
executed. See the [verification report](handovers/STREAMLIT_AWS_VERIFICATION.md)
for the exact bounded operation counts and limitations.

| Item | Verified contract | Live adapter behavior |
|---|---|---|
| Alert / brief tables | `is459_rt_alerts` / `is459_rt_briefs`; txn HASH key, string; no sort key or indexes | Two exact GetItem reads per known ID, matching returned identities |
| Alert amount | String | Preserved as supplied |
| Alert txn_ts, sender, receiver, currency, payment_format | Strings | Dataset transaction time and payment context displayed |
| Alert score | DynamoDB Number (SDK Decimal) | Numeric score; no invented rank or model reasons |
| Alert model_version, scored_at_ms, arrival_ms, ingest_to_score_ms | Model string, numeric epoch milliseconds and latency | Collapsed scoring provenance; UTC epoch display separate from dataset time |
| Brief narrative, typology | Strings | Pipeline interpretation, separate from batch-agent validated evidence |
| Brief next_checks | String containing a JSON list of strings | Parsed and displayed as suggested checks |
| Brief status | `ok` / `needs_review` | Source validation status / template fallback shown, without claiming interpretation correctness |
| Brief model_version, latency_ms | String and DynamoDB Number | Model and reported explainer duration, not total queue latency |
| Brief completion timestamp | DynamoDB writer omits S3-only written_at_ms | Unavailable; never inferred from scoring or lookup time |
| Brief confidence / input | Source confidence enum and JSON input string | No calibrated confidence badge or raw input dump |
| TTL | DISABLED on both tables | Absence does not establish processing failure or retention policy |
| Session identity / chronological feed | No bounded ID source verified; exported helper scans | Known-ID lookup only |

The isolated mappers `map_documented_alert` and `map_documented_brief` accept
verified fields, reject malformed known types and ignore unknown attributes.
Raw DynamoDB records are never dumped into the page. `AlertSummary`, `Explanation`
and `RealtimeRecord` remain UI presentation DTOs; demo values are invented and
are separate from the live contract. Original agent fixtures are unchanged.

The live service exposes `RealtimeService.lookup(txn)` and `lookup_many(known_ids)`.
It creates a DynamoDB resource lazily only on explicit lookup. It makes at most
two GetItem operations per unique ID. Reads of the two tables are separate, not a
transactional snapshot. Errors remain distinct from missing records, and schema
or identity mismatches are rejected. No chronological pagination is claimed.

Live lookup requires normal AWS credentials and DynamoDB GetItem permission on
both tables in the configured region. This page needs no SQS, Kinesis, Lambda,
Bedrock, Athena or write permissions. Two available pairs with source status `ok`
were observed live. Pending, missing, malformed and fallback states are covered
by synthetic regressions; they were not observed in that live sample. No pipeline
health or completeness claim follows from two successful reads.

## Final laptop rehearsal

Use the existing project environment and check dependencies with
`.venv/bin/python -m pip check`. Launch on `streamlit`, keep both source selectors
in Demo, and rehearse the saved-case journey above. For a simulated on-demand
comparison, open `txn_samplepass` and submit its original 48-hour request. Then
run the real-time sample-session walkthrough. All three journeys work offline;
no recording is created by these checks.

For a separately authorised live rehearsal, confirm credentials, action-specific
permissions and current batch publications. Obtain known real-time transaction
IDs from the pipeline owner; the bounded verification sample is not a recent feed. Loading records
does not start a stream and does not prove that a pipeline is currently healthy.
Keep the offline sample data available as a clearly labelled fallback selected
by the presenter, not an automatic substitution after a live error.

The external `WORK_SPLIT.md` dated 6 November documents **`python3 session.py stop`**
after every real-time session. The exported `session.py` confirms that command, but its actual working directory
is not established locally. If a session was started outside this dashboard, have the
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

## Live score and navigation reliability fix

Athena queue records may contain string risk scores. The shared presentation
formatter accepts finite numeric strings, floats, integers and Decimals, and
renders scores to four decimals. Missing/null/blank/malformed/non-finite scores
show **Unavailable**, never an invented zero. The queue table, selected summary,
missing-saved investigation and advanced request caption use this formatter.
Raw source scores, IDs, BQ1 ordering and SQL remain unchanged. An entirely absent
score column is handled as unavailable while other required queue fields retain
their existing validation. Saved metric/amount presentation uses the same safe
numeric handling. Real-time DynamoDB fields remain validated by their mapper.

The four navigation values are now the four displayed names. Old Agent Evidence
Brief (Demo) session state migrates to Agent Examples; obsolete Legacy BQ2 or
unknown values resolve to Alert Queue before the keyed radio renders. Every
supported page stops explicitly, with a final unconditional stop guarding the
retained legacy root code. No native graph or `src/bq2/` module was removed.
Row selection and snapshot-generation guards remain; the optional separate queue
dropdown was removed.

Offline verification: **810 unit/UI tests passed** (773 previous plus 37 focused
score/navigation cases). New coverage includes the actual string-score failure,
Decimal and unavailable values, an injected 20-row Live queue, selecting the
previously verified ID from mocked data, missing-saved rendering, four routes and
obsolete navigation migration. Source records are checked for mutation. Existing
stale-selection and graph/evidence coverage remains in place. Tests blocked all
real AWS/agent operations and recorded zero forbidden attempts. Touched-file
Ruff, dependency and whitespace checks passed; 14 unrelated repository Ruff
findings remain unchanged.

The isolated Chrome walkthrough also passed: four visible navigation options,
Demo queue → native saved graph → queue, then a clearly labelled fake Live
backend with 20 string-score rows, native row selection and the `0.9958` selected
summary. Opening the mocked record, visiting Real-Time Alert Lookup and Agent
Examples caused no formatting exception or legacy UI fall-through. The harness
performed no AWS/Bedrock operations; its temporary scripts remain outside Git at
`/private/tmp/bda-queue-fix/`.

## Report and lookup guidance verification

The focused follow-up to `4130387` passed **836 unit/UI tests**, including 26 new
cases for report availability, stale choices, independent evidence, local input
validation, snapshot retention, classified read failures and old navigation state.
**Real-Time Monitoring** session state safely migrates to **Real-Time Alert Lookup**.
The on-demand execution implementation, agent contracts, publication and storage
remain unchanged. The existing 14 unrelated repository Ruff findings remain;
touched-file Ruff, dependency and whitespace checks pass.

An isolated Chrome Demo walkthrough confirmed the single-report heading without
a radio, previous/new report switching with separate evidence, the renamed page,
pending explanations and retention of the loaded record/time after invalid input.
Empty Live input showed actionable local guidance without calling a service.
Both the full test run and browser guard recorded zero forbidden AWS/agent
operations. Temporary browser harnesses/screenshots are outside Git under
`/private/tmp/bda-report-lookup/`; no live verification or real investigation was
repeated.
