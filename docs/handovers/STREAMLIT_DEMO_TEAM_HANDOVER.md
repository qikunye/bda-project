# IS459 AML Streamlit | Demo Team Handover

**Prepared:** 8 November 2026  
**Repository:** `qikunye/bda-project`  
**Development branch:** `streamlit`  
**Latest local commit reported by Streamlit owner:** `5d09265` (not yet pushed at handover time)  
**Scope:** Professor-facing demonstration and handover of the AWS-connected Streamlit application, including verified real-time lookup IDs.

> **Important:** The underlying IBM AML transaction dataset is synthetic. “Live AWS” means that Streamlit reads the team's deployed AWS resources; it does **not** mean that these are financial transactions occurring now.

## 1. Two real-time IDs for the demonstration

For saved replay results, choose **Real-Time Monitoring → Saved Replay Results**, select **2022-09-16**, and click **Load Results**. Select a discovered transaction to review its Qwen explanation. This lists completed-explanation keys, not recent processing order.

For manual lookup, expand **Advanced: Manual Transaction Lookup**, select **Live DynamoDB (read-only)** and paste these exact transaction IDs into **Known transaction IDs**. Use one ID per line, then click **Load known alerts**.

```text
txn_3a56e8f5ad713566b162a9def3eb4d90
txn_5d363f8f9e859e450d511cb541d05fd5
```

The IDs were recovered from these observed S3 brief-object keys:

```text
rt/briefs/dt=2022-09-16/txn_3a56e8f5ad713566b162a9def3eb4d90.json
rt/briefs/dt=2022-09-16/txn_5d363f8f9e859e450d511cb541d05fd5.json
```

Earlier bounded AWS verification found **two matching records in `is459_rt_alerts` and `is459_rt_briefs`**, with source brief status `ok`. These are suitable known IDs for a persisted-output demonstration. Recheck using the app at demo time; the S3 keys alone would not establish that the DynamoDB records are still accessible. The `dt=2022-09-16` prefix is a historical dataset partition, **not** a date when the pipeline was last running.

**Expected UI:** Two requested IDs; scored alert details where stored, brief available, narrative explanation, typology and recommended next checks. The real-time explanation is produced by the **Lambda explanation pipeline**, not by the separate Bedrock investigation agent. It is an explanation for review, not a validated laundering verdict. No raw DynamoDB scan, Kinesis session or Lambda invocation is needed for this lookup.

**If you see “Alert not found”:** Confirm the exact ID and that the source is **Live DynamoDB**, not Demo. Do not paste a batch BQ1 ID as a substitute. If the request fails due to AWS login, run `aws login` and explicitly reload. If either explanation is missing, show the status honestly rather than claiming the pipeline failed or creating a fake result.

## 2. Start the dashboard

From a Mac with the team's repository and existing Python virtual environment:

```bash
cd /Users/qikunye/Documents/GitHub/bda-project
git switch streamlit
source .venv/bin/activate
aws login                        # if AWS authentication needs refreshing
streamlit run app.py
```

Open `http://localhost:8501` if it does not open automatically. The `streamlit` branch must be **pushed and pulled by teammates first**; the reported latest local commit is not yet on GitHub at the time of this handover. Teammates should verify the current branch and commit before a demo, rather than assuming the latest features are present.

Do not place AWS credentials, access tokens or full raw AWS response dumps into presentation materials.

## 3. Recommended 5–7 minute demonstration

| Approx. time | What to do | What to explain |
|---|---|---|
| 0:00–1:00 | **Alert Queue** → `Live batch (read-only)` → analysis date **2022-09-14** → **5** alerts → **Load / refresh queue** | BQ1 prioritises alerts by daily risk ranking. Scores are ranking signals, **not** laundering probabilities. Loading reads Athena and saved-decision metadata; it does not call the agent. |
| 1:00–2:00 | Select the rank-one batch transaction `txn_613ac569efe8fc2e024adfbeae71b746`; **Open Selected Investigation** | This opens an **existing** investigation. The batch agent previously produced the brief, saved the metadata to DynamoDB and the full trace to S3. |
| 2:00–3:30 | On **Investigation Report**, inspect summary, findings, case members, graph and supporting evidence | The verified saved case contains **three members**. A finding describes evidence that deserves review, not proof of laundering. The historical graphs can be sparse, including anchor payments without surrounding edges. Do not invent network links. |
| 3:30–4:15 | Open technical/provenance details, then expand **Advanced: Run a new AI investigation** without submitting | The control runs a new **single-alert** Bedrock/Athena investigation only when explicitly clicked and acknowledged. Viewing saved results and clicking evidence do not rerun the LLM. |
| 4:15–6:00 | **Real-Time Monitoring** → **Saved Replay Results** → **2022-09-16** → **Load Results** → select each discovered transaction | Separate real-time pipeline: Kinesis → scoring Lambda → DynamoDB alerts → explanation Lambda → DynamoDB briefs. S3 discovery lists completed explanation keys; it is not chronological. Indexed recent alerts need the separate writer/GSI integration; no source polls automatically. |
| 6:00–7:00 | Summarise roles and limitations | BQ1 selects priorities; the money-flow graph shows context; the agent produces evidence-based investigation briefs; the real-time pipeline provides separate rapid explanations; a human analyst makes the final judgement. |

### Batch demonstration ID

```text
txn_613ac569efe8fc2e024adfbeae71b746
```

This batch ID is **not one of the two real-time demo IDs**. Do not use it as a real-time lookup key unless the pipeline owner independently confirms the same ID was processed there.

### Previously completed on-demand verification

The team has already completed **one explicitly authorised** live investigation of the rank-one batch alert with a 48-hour requested window. It succeeded in **49.6 seconds**, returned **three validated findings**, used **three Bedrock calls and ten Athena queries**, with **zero retries**, **no repair** and **no code-only fallback**. It used **17,500 input** and **2,018 output tokens**. The estimated model cost from configured rates was **US$0.08277 excluding Athena**, not an AWS billing figure.

That result was **session-only**. It may not be available after a restart. **Do not repeat it to demonstrate the app without new explicit approval.** The historical saved batch result and an on-demand result have different observation cutoffs and possibly different case membership; they are not directly comparable on identical evidence.

## 4. Safe operation and troubleshooting

| Action | External behaviour |
|---|---|
| Open app, explore Demo examples | No AWS or Bedrock inference |
| Load live batch queue | Athena query plus keyed DynamoDB reads; may incur small query/read costs |
| Open saved batch report | Keyed DynamoDB/S3 reads; **no Bedrock** |
| Click an evidence reference, change member, open trace | Uses already loaded result; **no new agent execution** |
| Load known real-time IDs | Keyed DynamoDB `GetItem` reads for alert and brief; **no streaming or Lambda execution** |
| Click **Run Agent Investigation** with cost acknowledgement | A **new billable Athena/Bedrock run**; current on-demand result is not published into the batch agent's DynamoDB/S3 stores |

On-demand results are kept in Streamlit session state only; the existing batch publication is separate. Normal Athena query-result S3 objects and service logs may still be generated during a live run. Do not claim absolutely zero AWS side effects.

**Common issues**

- **Authentication error:** run `aws login` and retry a deliberate read. Do not change IAM or redeploy resources from the dashboard.
- **No saved investigation:** the alert may never have been processed by the daily batch; it is not necessarily a query failure. A new investigation is optional and billable.
- **No surrounding graph edges:** the available historical observation window may be limited. A sparse graph is not automatically a bug or evidence that a transaction is safe.
- **Real-time “not found”:** check that the transaction ID came from the real-time pipeline; historical batch IDs need not exist there.
- **Indexed recent feed unavailable:** prior verification found no chronological index. Use S3 discovery for completed historical explanations, or advanced manual lookup. The UI never scans.
- **No network/expired AWS login during presentation:** switch explicitly to **Demo (offline)** and identify the results as synthetic examples. Do not present Demo results as live output.

## 5. Teammate handover checklist

- [ ] Push the latest reviewed `streamlit` branch and confirm teammates have pulled it.
- [ ] Launch the application and confirm the expected professor-facing navigation.
- [ ] Verify live batch queue for **14 September 2022**, Top 5; open the previously verified rank-one saved case.
- [ ] Load S3 discovery for **2022-09-16** and confirm discovered records remain readable; use Section 1 IDs for manual lookup if needed.
- [ ] Capture fresh screenshots of the actual interface after verification. Keep real/demonstration source labels visible.
- [ ] Avoid re-running Bedrock or the streaming pipeline unless separately authorised.
- [ ] Update final presentation/runbook language to distinguish **persisted saved batch brief**, **session-only on-demand result**, and **real-time Lambda explanation**.
- [ ] Review limitations and avoid overstating model accuracy, real-time freshness or automatic alert discovery.
- [ ] Hand over the existing verification reports and regression suite. Latest reported test count: **836 passing** after local commit `5d09265` (not independently rerun for this handover document).

## 6. Reference documents to keep with the handover

- `docs/handovers/STREAMLIT_LIVE_DEMO_RUNBOOK.md` — main live demonstration instructions, as maintained on the local `streamlit` branch.
- `docs/handovers/STREAMLIT_AWS_VERIFICATION.md` — bounded AWS resource and live-read verification.
- `docs/handovers/STREAMLIT_UX_VERIFICATION.md` — UI and browser regression evidence.
- `docs/streamlit_dashboard.md` — configuration, service semantics and technical limitations.

**Documentation integration note:** This handover was generated separately because the latest local, unpushed runbook is not accessible here. Incorporate Section 1 and the real-time demo steps into the team's latest `STREAMLIT_LIVE_DEMO_RUNBOOK.md` rather than overwriting that runbook with an older GitHub copy. Teammates can then edit the final documentation on `streamlit` and submit changes for review.

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
