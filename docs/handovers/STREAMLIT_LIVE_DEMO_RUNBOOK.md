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
| Real-time monitor | DynamoDB GetItem on `is459_rt_alerts` and `is459_rt_briefs`; exact known transaction IDs |
| New on-demand execution (excluded from this demo) | Agent access to label-free `agent_scores_v2`, `agent_edges_v2`, `agent_day_queue_v2`, Glue/S3/Athena, and Bedrock invocation for the configured model/inference profile |

The v2 tables must contain 14 September 2022 data. The verified saved case needs
compatible decision/case records and its full S3 trace. A queue alone does not
guarantee saved-result coverage. Effective permissions can also depend on the
account's resource policies; this table describes required actions, not an IAM
policy to deploy.

Before the presentation, obtain the two previously verified real-time IDs from
the pipeline owner or the retained private verification files. They are locally
available in `/private/tmp/bda-streamlit-verify/realtime-private.json`; inspect
privately, without committing raw records. Temporary files may disappear and are
not a team distribution mechanism. If IDs are unavailable, prepare the labelled
real-time Demo view instead. Do not discover IDs by scanning tables or starting
a stream. The monitor has no chronological index or recent-alert feed.

Keep the [verification report](STREAMLIT_AWS_VERIFICATION.md) open as the durable
backup for the historical on-demand result. Optional local screenshots exist at
`/private/tmp/bda-streamlit-prompt8/approved-result-graph.png` and
`approved-result-details.png` in the same directory. These are historical
verification evidence, not current AWS reads. Do not copy raw dumps into Git.

## Six-minute presentation

| Time | Presenter action and narration |
|---|---|
| 0:00–0:45 | Open **Alert Queue**, choose **Live batch (read-only)**, date **2022-09-14**, **Review top K = 5**, then **Load / refresh queue**. “BQ1 gives us a practical place to start. The daily rank orders the payments for review. The score is a ranking signal, not a probability of laundering.” |
| 0:45–1:30 | Select `txn_613ac569efe8fc2e024adfbeae71b746`, then **Open Investigation**. Show **Saved Batch Brief**, saved run identity and three members. “This investigation was prepared earlier for a group of connected alerts. Opening it retrieves the stored evidence; it does not ask the model to investigate again.” |
| 1:30–2:30 | Show the native money-flow graph and switch case members. Read one finding in plain language, then click a supporting evidence ID. “BQ2 shows the payments around the alert. We can follow a statement back to its evidence and move between members of the case.” Explain that this saved sample has an anchor payment but no surrounding graph payments for each member. Other account activity is available in the supporting trace. Empty context is a visibility limit, not a broken graph or proof of innocence. |
| 2:30–3:30 | Explain the **Run an investigation** controls without checking the Live acknowledgement or clicking Run. If the original session still offers **On-Demand Brief** for this exact alert/date/48-hour window, switch to it and back. “We also tested one investigation requested by the analyst. It returned three validated findings in about 50 seconds. It remains separate from the saved case.” If absent, show the report or retained screenshot and identify it as the earlier completed test. |
| 3:30–4:45 | Open **Real-Time Monitoring**, choose **Live DynamoDB (read-only)**, enter the two known IDs and click **Load known alerts**. Show score, transaction time, scoring timestamp, narrative and next checks; select the other record without refreshing. “This is the separate Kinesis and Lambda path. We are reading two stored alerts and their Lambda explanations. We are not starting the stream, checking pipeline health or running the investigation agent.” |
| 4:45–5:30 | Expand **Pipeline and integration scope** if useful. Explain the independent stream: Kinesis → scorer Lambda → alert table → explainer → brief table → dashboard. This page supports known-ID lookup, not automatic recent-alert discovery. |
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
4. On **Real-Time Monitoring**, separately select **Demo (offline)**, keep
   **Sample session: mixed alerts**, then **Load known alerts**. Show
   `demo_rt_complete` and `demo_rt_pending`. These static examples do not imply
   that a Lambda finished processing during the demonstration.
5. **Agent Evidence Brief (Demo)** offers nine additional sample scenarios for
   missing data, repaired briefs and fallback if questions arise.

Offline mode requires no credentials, Bedrock, CDN or AWS pipeline. Avoid the
**BQ2 Money Trail (Legacy)** page for the offline fallback: its original query
submission uses Athena.

## Charges and execution boundaries

| UI action | AWS activity / cost exposure |
|---|---|
| Select source, browse Demo, or load a matching simulated result | No AWS operations |
| Load/refresh Live queue | Athena query and keyed DynamoDB reads; Athena scans and normal S3 result storage can incur charges; cached SQL may be reused |
| Open a saved Live brief | DynamoDB/S3 retrieval; service read charges may apply; no Bedrock inference |
| Inspect already loaded findings, click evidence, change members/views/pages, expand trace | Session-local display; no additional AWS calls observed or required by these paths |
| Load/refresh known Live real-time IDs | Up to two DynamoDB GetItem reads per unique ID; no Bedrock, Kinesis or Lambda invocation |
| Live Run Agent Investigation / Run again | New Athena and Bedrock work; excluded from this presentation without new approval |
| Submit Legacy BQ2 lookup | Existing Athena query path and associated scan/result costs |

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
