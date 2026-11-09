# Streamlit analyst dashboard

From the submission checkout, follow the [environment setup](../README.md#getting-started), then launch from the repository root:

```bash
.venv/bin/streamlit run app.py
```

The default Alert Queue uses **Demo (offline)**. No credentials or network services are needed.
PyVis is pinned in `requirements.txt`; graph JavaScript is loaded from that
installed package and embedded inline, without CDN requests.

Navigation: **Alert Queue → Investigation → Real-Time Monitoring → Agent Examples**. Batch and real-time sources have separate
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
Selecting the source alone makes no AWS calls. Historical sampled verification is recorded below.

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
the representative is authoritative. Opening a saved brief performs keyed reads without table scans or
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
Live on-demand execution incurs Athena and Bedrock charges and requires the
separate acknowledgement and Run action. Results remain only in that Streamlit session.

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

## Real-Time Monitoring journey

1. Open **Real-Time Monitoring** and choose **Demo (offline)** or **Saved Replay Results**. Indexed recent alerts appear only when
   `REALTIME_ALERTS_INDEX` is set to a deployed index name.
2. For S3 discovery, choose **Transaction Date** **2022-09-16**, leave the
   record count at **20** (maximum 50), and click **Load Results**.
3. Review the table, apply local score/status filters and select a transaction
   to see its Qwen narrative, typology and suggested checks. Missing scores and
   thresholds are unavailable, never treated as zero or proof of suspicion.
4. Click **Refresh** deliberately to retrieve again. Source/date/count changes
   require another Load. Selection, filtering and detail expansion do not call AWS.
5. Use **Advanced: Manual Transaction Lookup** for the preserved known-ID workflow:
   independently choose Demo or Live DynamoDB, enter 1–20 IDs or a demo scenario,
   and click **Load known alerts**. Its refresh uses the loaded ID set; invalid
   input preserves its snapshot and selection.

| Source | Discovery and time meaning |
|---|---|
| Demo (offline) | Invented static records; no AWS; never a live result |
| Live Recent Alerts (indexed DynamoDB) | Bounded GSI Query for one UTC processing day; descending processing time, separate from historical transaction time |
| Saved Replay Results | Bounded key listing under a historical replay-date prefix; not chronological and not complete scoring coverage |
| Manual Live DynamoDB lookup | Keyed reads in requested-ID order; no automatic discovery |

All sources are explicit. A failure never silently substitutes another source.
Both discovery modes reuse existing canonical alert/brief mapping via keyed
lookups. Limits are three discovery pages and 50 IDs per action; the UI reports
limited listings. S3 bodies are not downloaded. The retrieval timestamp is not
transaction freshness or processing latency. The dashboard reads stored outputs;
it does not monitor pipeline health or start BQ2/Claude investigation.

The optional DynamoDB recent-alert GSI was **not deployed** and is **not required
for this submission**. Leave `REALTIME_ALERTS_INDEX` unset for Saved Replay Results
and manual lookup. Writer and infrastructure sources are in [realtime](../realtime/README.md).

### Real-time contract: evidence and explicit gaps

The deployed table metadata, exported writers under
`s3://is459-aml-g1t5/rt/code/`, and two keyed alert/brief pairs were checked on
8 November 2026. Source exports were read as text; no runner or writer was
executed. The sampled observations and their limits are recorded below.

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
| Chronological feed | No deployed index in prior verification | Optional processing-day GSI interface; S3 key discovery is separately labelled |

The isolated mappers `map_documented_alert` and `map_documented_brief` accept
verified fields, reject malformed known types and ignore unknown attributes.
Raw DynamoDB records are never dumped into the page. `AlertSummary`, `Explanation`
and `RealtimeRecord` remain UI presentation DTOs; demo values are invented and
are separate from the live contract. Original agent fixtures are unchanged.

The live service exposes `RealtimeService.lookup(txn)` and `lookup_many(known_ids)`.
It creates a DynamoDB resource lazily only on explicit lookup. It makes at most
two GetItem operations per unique ID. Reads of the two tables are separate, not a
transactional snapshot. Errors remain distinct from missing records, and schema
or identity mismatches are rejected. Manual lookup makes no chronological claim; the separate feed adapter supports bounded GSI pagination.

Live lookup requires normal AWS credentials and DynamoDB GetItem permission on
both tables in the configured region. This page needs no SQS, Kinesis, Lambda,
Bedrock, Athena or write permissions. Two available pairs with source status `ok`
were observed live. Pending, missing, malformed and fallback states are covered
by synthetic regressions; they were not observed in that live sample. No pipeline
health or completeness claim follows from two successful reads.

## Final laptop rehearsal

Use the existing project environment and check dependencies with
`.venv/bin/python -m pip check`. Keep both source selectors
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

The dashboard has no real-time session controls. An externally started replay
session must be stopped by its owner using `python3 session.py stop` from
`realtime/` (the runner directory in this checkout).

## Presentation walkthrough (about six minutes)

1. Start offline with the 14 September queue and `txn_samplefanmember`. Show
   BQ1 order, the saved case, member graphs and a finding citation. Demo ranks,
   scores and records are invented fixtures, not historical batch replays.
2. For a simulated new investigation, open `txn_samplepass`, keep 48 hours and
   run in Demo. Switch between previous and new reports; neither calls AWS.
3. If AWS access and compatible saved publications are available, explicitly load
   Live batch for **2022-09-14**, with five alerts. The historically verified ID is
   `txn_613ac569efe8fc2e024adfbeae71b746`. Explain that opening it retrieves a
   saved three-member case. Its sampled graphs showed anchors only, although
   other account activity supported findings in the trace.
4. Explain the advanced Live investigation controls without running them during
   saved-results browsing. Daily briefs stop at the day's end; an on-demand
   single-alert brief can see later data and has different membership.
5. Open Real-Time Monitoring → Saved Replay Results → **2022-09-16** →
   **Load Results** (20 records). Show Qwen narrative and checks, then select
   another row without refreshing. This reads outputs of an earlier replay.
6. Explain that BQ1 prioritises, BQ2 traces connected payments, and the agent
   organises cited evidence. The analyst decides; synthetic patterns do not
   establish real-world laundering performance.

If Live access fails, show the error and explicitly select Demo. No source
silently substitutes samples. Agent Examples provides nine offline scenarios.

## Historical AWS verification evidence

These observations were recorded on **8 November 2026**, before submission
cleanup. They were not repeated during cleanup and do not establish current
pipeline health, complete coverage or ongoing access.

- Batch: one top-five Athena queue query returned ranks 1–5 (400,986 bytes
  scanned, 903 ms engine time). One DynamoDB BatchGetItem returned five compatible
  decisions, including one with no findings. A keyed case read and S3 GetObject
  retrieved a three-member, three-finding case with fallback false. Its stored
  envelope and full trace validated, and its native graphs rendered in Chrome.
- A separately approved on-demand test for the batch ID above, 14 September,
  48 hours, completed in 49.6 seconds: three validated findings, no repair or
  fallback, ten Athena queries (130,973,587 bytes total) and three Bedrock calls.
  The historical model was Sonnet 4.5; the current configured model is Sonnet 4.6.
  It used 17,500 input and 2,018 output tokens, with configured estimated model
  cost US$0.08277 excluding Athena, not a verified billing charge. Its single-alert
  graph included one onward payment absent from the saved daily graph. The
  result was session-only and did not overwrite the saved case. It is not a
  durable dashboard import or a controlled comparison with the grouped batch.
- Replay: real SDK adapters and Streamlit AppTest discovered and displayed
  `txn_3a56e8f5ad713566b162a9def3eb4d90` and
  `txn_5d363f8f9e859e450d511cb541d05fd5`. Both canonical DynamoDB alert/brief
  pairs were available with explanation status `ok`; actual Qwen narrative,
  typology and every suggested check were checked against returned records.
  Initial Load, second Load and Refresh each made one S3 ListObjectsV2
  (`rt/briefs/dt=2022-09-16/`, MaxKeys 20) and four DynamoDB GetItem reads:
  **three listings and twelve keyed reads total**, zero transport retries.
  No S3 bodies were downloaded. Selection, filtering and passive reruns made
  zero extra reads. No demo records were substituted.

Replay verification used AppTest, **not browser visual verification**. Chrome
checks applied to the earlier batch/on-demand journeys only. Missing, malformed,
pending and fallback replay states were tested offline, not observed in that
live sample. No indexed discovery, index deployment, backfill, Lambda invocation
or Kinesis session was part of the replay read check. Two successful examples
cannot establish population-wide availability or explanation quality.

## Optional indexed discovery and replay limits

Saved Replay Results lists explanation keys under a historical transaction-date
prefix, then reads the canonical alert and brief by ID. It is incomplete scoring
coverage and is not sorted by processing time. Separate alert and brief reads
are not a transactional snapshot; a rewrite can change an item during retrieval.
Retrieval time is neither transaction freshness nor end-to-end processing latency.
The real-time path is a historical-replay prototype, not continuous production
streaming, and its Qwen interpretations are separate from validated agent findings.

Discovery accepts 1–50 IDs, defaults to 20, and makes at most three logical
listing/query requests (page size equal to the limit), then at most 100 keyed
GetItem reads. SDK retries can add transport attempts. Invalid filenames are
skipped; a limited listing is labelled incomplete. Local filters apply only to
loaded records. Manual lookup accepts 1–20 known IDs. No scans are used.

For a future indexed feed, the scorer in `realtime/app.py` must derive string `feed_day`
from the same numeric UTC epoch milliseconds stored as `scored_at_ms` on each
alert write. The owning infrastructure must add `rt-alerts-by-processing-day`
with HASH `feed_day` (S), RANGE `scored_at_ms` (N), projection KEYS_ONLY, retaining
`txn` as the table key. Capacity must match the owning table's billing mode.
Grant Query on that index, wait until ACTIVE, verify writer output, then set
`REALTIME_ALERTS_INDEX=rt-alerts-by-processing-day`. Configuration alone deploys
nothing. This optional integration remains undeployed; the writer source is now included.

The index uses **UTC processing dates**, not 2022 transaction dates, and is
sorted newest first within the returned set. It is eventually consistent;
older items without `feed_day` are absent, with no backfill included. Reprocessed
IDs replace their canonical record, not an immutable event history. Ties at a
limit boundary have no guaranteed global order.

Saved-replay discovery needs S3 ListBucket scoped to `rt/briefs/dt=????-??-??/*`
and GetItem on both real-time tables; it needs no S3 body reads or model invocation.
An indexed feed additionally needs Query on the deployed index. The dashboard
creates none of these resources.

## Offline validation

```bash
.venv/bin/python -m pytest tests
.venv/bin/python -m pytest tests/ui -q
.venv/bin/ruff check app.py src/ui tests/ui
.venv/bin/python -m pip check
```

The UI suite covers source isolation, queue selection, member/evidence navigation,
independent saved/new reports, simulated execution, read errors and bounded replay
pagination. It blocks real AWS and agent execution. Full results for submission
cleanup are in the local validation report supplied with the cleanup summary.
