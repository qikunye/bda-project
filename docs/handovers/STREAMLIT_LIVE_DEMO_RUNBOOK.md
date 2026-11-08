# Streamlit live-demo runbook

Prepared on 8 November 2026 (Asia/Singapore), for the `streamlit` branch.
Allow about six minutes. The [verification report](STREAMLIT_AWS_VERIFICATION.md)
records completed observations; this guide does not authorise another agent run.

## Before presenting

Use the repository root and the existing Python 3.12.4 environment. Check that
`git branch --show-current` reports `streamlit` and review `git status -sb`.
Keep local work intact; do not merge `main` as part of rehearsal.

For a fresh checkout only, follow the repository setup:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

For the existing environment:

```bash
.venv/bin/python -m pip check
.venv/bin/streamlit run app.py
```

Open `http://localhost:8501` (or the URL printed by Streamlit if using another
port). Alert Queue starts in **Demo (offline)**. Graph assets are bundled locally.
The current dashboard uses visible source selectors; an `AML_DEMO` environment
variable is not needed to select these workflows.

For Live, sign in outside the app using the team's normal AWS CLI login flow
(`aws login` for this project's login credentials), with the intended profile
selected in the shell if applicable. Region is `us-east-1`. Refresh expired login
before presenting. Never copy keys, tokens or login-cache files into this repo.
No login or cloud check was executed while preparing this runbook.

Have these existing resources and permissions available; do not rebuild them
for the demo:

| Workflow | Existing data / permissions |
|---|---|
| Queue | Athena StartQueryExecution, GetQueryExecution and GetQueryResults; Glue catalog reads and S3 reads for `is459_aml_gold.agent_day_queue_v2`; usual query-result S3 read/write access under `s3://is459-aml-g1t5/athena-results/agent/` |
| Saved results | DynamoDB BatchGetItem and GetItem on `is459_agent_decisions`, GetItem on `is459_agent_cases`; S3 GetObject for the stored trace key under `s3://is459-aml-g1t5/agent/traces/` |
| Real-time monitor | S3 ListObjectsV2 for replay keys, then DynamoDB GetItem on `is459_rt_alerts` and `is459_rt_briefs`; optional configured GSI Query |
| New on-demand execution (excluded from this demo) | Agent access to label-free `agent_scores_v2`, `agent_edges_v2`, `agent_day_queue_v2`, Glue/S3/Athena, and Bedrock invocation for the configured model/inference profile |

The v2 tables must contain 14 September 2022 data. The verified saved case needs
compatible decision/case records and its full S3 trace. A queue alone does not
guarantee saved-result coverage. Effective permissions can also depend on the
account's resource policies; this table describes required actions, not an IAM
policy to deploy.

Before the presentation, explicitly load S3 discovery for **2022-09-16** and
check the discovered records. The two previously verified IDs and observed object
keys are also recorded in [the team handover](STREAMLIT_DEMO_TEAM_HANDOVER.md)
for advanced manual lookup. They are historical examples, not hardcoded feed
results or proof of current access. If a read fails, show the error and switch
explicitly to labelled Demo data. Do not discover IDs by scanning tables or
starting a stream. Prior verification found no deployed chronological index;
the optional indexed feed requires the external integration described below.

Keep the [verification report](STREAMLIT_AWS_VERIFICATION.md) open as the durable
backup for the historical on-demand result. Optional local screenshots exist at
`/private/tmp/bda-streamlit-prompt8/approved-result-graph.png` and
`approved-result-details.png` in the same directory. These are historical
verification evidence, not current AWS reads. Do not copy raw dumps into Git.

## Six-minute presentation

| Time | Presenter action and narration |
|---|---|
| 0:00–0:45 | Open **Alert Queue**, choose **Live batch (read-only)**, date **2022-09-14**, **Number of alerts to review = 5**, then **Load / refresh queue**. “BQ1 gives us a practical place to start. The daily rank orders the payments for review. The score is a ranking signal, not a probability of laundering.” |
| 0:45–1:30 | Select `txn_613ac569efe8fc2e024adfbeae71b746`, then **Open Selected Investigation**. Show **Investigation Report**, expand its saved metadata if needed, and show the three members. “This investigation was prepared earlier for a group of connected alerts. Opening it retrieves the stored evidence; it does not ask the model to investigate again.” |
| 1:30–2:30 | Show the native money-flow graph and switch case members. Read one finding in plain language, then click a supporting evidence ID. “BQ2 shows the payments around the alert. We can follow a statement back to its evidence and move between members of the case.” Explain that this saved sample has an anchor payment but no surrounding graph payments for each member. Other account activity is available in the supporting trace. Empty context is a visibility limit, not a broken graph or proof of innocence. |
| 2:30–3:30 | Explain the collapsed **Advanced: Run a new AI investigation** controls without checking the Live acknowledgement or clicking Run. If the original session still offers the **New Investigation** report choice for this exact alert/date/48-hour window, switch to it and back. “We also tested one investigation requested by the analyst. It returned three validated findings in about 50 seconds. It remains separate from the saved case.” If absent, show the report or retained screenshot and identify it as the earlier completed test. |
| 3:30–4:45 | Open **Real-Time Monitoring**, choose **Saved Replay Results**, select **2022-09-16** and click **Load Results**. Show score, transaction time, scoring timestamp, narrative and next checks; select the other record without refreshing. “This is the separate Kinesis and Lambda path. We are reading two stored alerts and their Lambda explanations. We are not starting the stream, checking pipeline health or running the investigation agent.” |
| 4:45–5:30 | Expand **Technical Details** if useful. Explain the independent stream: Kinesis → scorer Lambda → alert table → explainer → brief table → dashboard. S3 discovery lists completed explanations; the optional GSI discovers alerts by UTC processing day. Manual known-ID lookup remains available. |
| 5:30–6:00 | Return to the investigation or queue. “BQ1 prioritises the alerts, BQ2 reveals connected transactions, and the agent organises evidence into a brief with findings, gaps and next checks. The analyst reviews that evidence and makes the final decision. These patterns do not prove money laundering.” |

Allow an extra minute for load latency or questions. Keep raw execution metadata
and the tool trace collapsed until needed to explain a particular citation.

The saved batch uses its historical end-of-day cutoff. The single-alert
on-demand request can see later available transactions up to the dataset horizon;
the verified graph included one onward payment absent from the saved graph.
That is consistent with the later cutoff. Case membership and execution context
also differ, so this is not a controlled comparison or evidence of a bug.

## What was observed, and what can change

The completed 8 November verification observed:

- Five queue rows in daily ranks 1–5; rank-one score `0.9975132942199707`.
- Four decision records with findings and one with no findings in that sample.
- A saved three-member case with three findings, evidence navigation and graphs
  rendered in Chrome. Each saved member graph showed the anchor only; no
  per-currency metric rows were supplied.
- Exactly one approved `investigate` request for the transaction above, date
  `2022-09-14`, `window_h=48`: 49.6 seconds, three validated findings, no repair or
  fallback, three Bedrock calls, ten Athena queries, zero automatic retries.
  Input/output tokens were 17,500/2,018. Configured estimated model cost was
  **US$0.08277 excluding Athena**, not a verified billing charge.
- Browser rendering of the on-demand brief, graph and trace; saved/on-demand
  view switches and evidence navigation caused no extra AWS operations.
- Two real-time alert/brief pairs with source status `ok`, parsed narratives and
  next checks, using four keyed reads. Navigation did not repeat those reads.

These are historical observations, not guarantees for a future session. Data,
publications, permissions, login and service availability can change.

The on-demand result is held only in Streamlit session state. A server restart
or loss of the original session can lose it; a new tab may create a new session.
It was not published to DynamoDB and cannot be recovered through saved-case
browsing. Its private local JSON is evidence, not a supported UI import. Do not
run the agent again to fill this gap. Use the report/screenshots, or demonstrate
the clearly labelled offline equivalent below. The one approved invocation has
already been consumed; another requires new approval.

## Offline backup

1. On **Alert Queue**, choose **Demo (offline)** and **2022-09-14**. Explain the
   visible **DEMO DATA** warning: these are invented fixtures, not newly retrieved
   AWS results. Source changes are explicit; a failed Live read never substitutes
   samples automatically.
2. Open `txn_samplefanmember`. Show the grouped saved example, member selection,
   graph and supporting evidence. These fixtures illustrate result shapes;
   they do not reproduce the live case or its historical daily cutoff.
3. For a simulated on-demand comparison, return to the Demo queue and open
   `txn_samplepass`. Keep **48 hours** and click **Run Agent Investigation**
   only while **Demo (offline)** is selected. This retrieves a matching existing
   fixture, without AWS or an agent run. Show the **SIMULATED ON-DEMAND RESULT**
   notice and separate evidence views. Grouped fixtures do not support this
   simulation; unsupported requests show a notice rather than invented results.
4. On **Real-Time Monitoring**, select **Demo (offline)** and click **Load Recent Alerts**. To show manual scenarios, expand **Advanced: Manual Transaction Lookup**, keep
   **Sample session: mixed alerts**, then **Load known alerts**. Show
   `demo_rt_complete` and `demo_rt_pending`. These static examples do not imply
   that a Lambda finished processing during the demonstration.
5. **Agent Examples** offers nine additional sample scenarios for
   missing data, repaired briefs and fallback if questions arise.

Offline mode requires no credentials, Bedrock, CDN or AWS pipeline. Navigation
contains exactly Alert Queue, Investigation, Real-Time Monitoring and Agent
Examples. The legacy BQ2 page is no longer exposed; the native Investigation
graph remains available.

## Charges and execution boundaries

| UI action | AWS activity / cost exposure |
|---|---|
| Select source, browse Demo, or load a matching simulated result | No AWS operations |
| Load/refresh Live queue | Athena query and keyed DynamoDB reads; Athena scans and normal S3 result storage can incur charges; cached SQL may be reused |
| Open a saved Live brief | DynamoDB/S3 retrieval; service read charges may apply; no Bedrock inference |
| Inspect already loaded findings, click evidence, change members/views/pages, expand trace | Session-local display; no additional AWS calls observed or required by these paths |
| Load/refresh known Live real-time IDs | Up to two DynamoDB GetItem reads per unique ID; no Bedrock, Kinesis or Lambda invocation |
| Live Run Agent Investigation / Run again | New Athena and Bedrock work; excluded from this presentation without new approval |

Saved evidence is read from the published case/trace and rendered locally. Its
citations resolve against that loaded result, which is why inspection does not
need a new model response. Only the separate Live acknowledgement plus explicit
execution button submits an on-demand request. Leave the acknowledgement clear.
Production backend retries still exist; zero retries describes the guarded
historical test, not a general promise about future runs. UI execution cannot
forcibly cancel an active provider request.

This demo does not start ETL, batch, Kinesis or Lambda. If someone independently
starts a real-time session, the pipeline owner must manage its lifecycle using
their verified runner instructions; this dashboard has no session controls.

## Real-time discovery update (local implementation, not new live verification)

The normal real-time flow now starts with source/date selection and **Load Results** for replay data (**Load Recent Alerts** for Demo/indexed sources), followed by selecting a transaction. **Refresh** explicitly repeats the
bounded read. Default load size is 20, maximum 50. Score and explanation-status
filters affect the loaded snapshot only. Details show Qwen pipeline interpretations,
not validated Claude findings. They do not initiate BQ2 or agent execution.

Sources are distinct: **Demo (offline)** uses invented data; **Saved Replay Results** lists
keys under `rt/briefs/dt=2022-09-16/` without downloading bodies and reads paired
DynamoDB records; **indexed DynamoDB** queries a configured processing-day GSI;
**advanced manual lookup** preserves the known-ID workflow with its own source
choice. No automatic AWS requests occur on reruns. Switching to Demo is explicit.

S3 prefixes reflect historical replay partitions and may have incomplete
explanation coverage. Their keys are not ordered by processing time. Prior
observations of the two example IDs are not fresh verification. The GSI uses UTC
processing dates, has eventual consistency, and excludes older items missing
`feed_day`. It is hidden unless `REALTIME_ALERTS_INDEX` is set; setting the variable
alone does not deploy an index. No AWS resource was changed for this enhancement.

See [REALTIME_FEED_INTEGRATION.md](REALTIME_FEED_INTEGRATION.md) for exact external
writer, index, read-permission and enablement instructions. No deployment or
backfill is authorised by these instructions. Existing historical AWS verification
and test counts above describe earlier checkpoints, not this change's live status.

Local regression result for this update: **873 full-suite tests**, including
**277 Streamlit/UI tests**, passed offline. Changed UI/app lint, compilation,
dependency and whitespace checks passed. Repository-wide Ruff still reports
14 findings in unchanged files; no live verification is claimed.

Final terminology: **Data Source → Saved Replay Results → Transaction Date →
Load Results**, under **Replay Results**. S3/index/coverage implementation details
are in the collapsed **Technical Details** expander.

The subsequent [live read verification](REALTIME_REPLAY_READ_VERIFICATION.md)
confirmed both known replay IDs and their actual Qwen content through SDK-backed
Streamlit AppTest. This supersedes the earlier “no live verification” checkpoint
for these two replay examples only; browser visuals and indexed discovery remain
unverified. No GSI or scorer deployment was performed.
