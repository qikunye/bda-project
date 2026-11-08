# Streamlit batch dashboard

Run `streamlit run app.py` from the repository environment. The default Alert
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
same application. No real-time or on-demand agent action is provided.

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
IAM changes are made by the UI. Current-contract saved batch runs must exist.

Missing decisions, empty findings, fallback, failed reads and incompatible saved
data have different labels. Old verdict-based records and inconsistent run IDs,
creation times, membership or brief metadata are rejected. The S3 key stored on
the representative is authoritative. No scans, writes, agent runs or silent demo
fallback occur. Real saved daily briefs use the agent's end-of-day cutoff; each
graph displays its own visible horizon, observation duration and cap warnings.

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
