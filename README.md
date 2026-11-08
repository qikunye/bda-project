# IS459 anti-money-laundering alert investigation

An end-to-end big data pipeline that helps an analyst decide which payments to review, what they connect to, and what the money did around them.

It uses the synthetic IBM AML dataset (LI-Medium: 31.25M transactions, 16,041 labelled as laundering). The figures below show how well the system recovers a synthetic pattern. They are not real-world detection performance.

| Stage | Question it answers | Output |
|---|---|---|
| **BQ1** alert prioritisation | Where should the analyst start? | A risk score and rank for every transaction, with reasons for the top alerts |
| **BQ2** case expansion | Where should the analyst investigate next? | The payments into the sender and out of the receiver around an alert |
| **Investigation agent** | What do the payments around this alert show? | An evidence brief per case: money flow, findings with cited payments, gaps and next checks. It gives no verdict |

An analyst-facing Streamlit dashboard ties the three together.

## Architecture

```text
DAILY      Raw CSV
             → Step Functions is459-etl-v2-silver-wap
               (parse → audit + 11 quality rules → publish Silver → BQ1 and BQ2 in parallel → Gold commit)
             → success event on EventBridge rule is459-etl-v2-day-succeeded
             → Step Functions is459-agent-batch, running two Fargate tasks
             → label-free agent tables rebuilt for the new day
             → top 100 alerts of the day grouped into cases
             → agent briefs each case
             → DynamoDB + S3 traces → Streamlit

ON DEMAND  Analyst's Investigate button → same agent on one alert → Streamlit

REAL-TIME  Kinesis → Lambda is459-rt-scorer (same BQ1 model)
             → is459_rt_alerts → is459-rt-explainer → is459_rt_briefs → Streamlit
             Display only: it doesn't go through the agent.
```

Everything runs in AWS `us-east-1`: S3 for storage, Glue and Athena for the ETL and queries, Step Functions and Fargate for orchestration, Bedrock for the LLM, DynamoDB for results.

This repository holds the investigation agent, the serving code around BQ1 and BQ2, the evaluation, the deployment of the agent batch, and the Streamlit dashboard. The ETL, BQ1 training and feature engineering, and the real-time writers live elsewhere.

## BQ1: alert prioritisation

An XGBoost model scores every transaction from features describing the payment, recent sender and receiver activity, whether the pair has paid each other before, and how unusual the amount is. Features use only earlier transactions, and the data is split by time: train on 1-9 Sep, tune on 9-14 Sep, test on 14-16 Sep.

Results on the test days (6.25M transactions, 3,013 labelled as laundering):

| Model | Recall@0.1% | Recall@1% | Recall@10% | Precision@1% |
|---|---|---|---|---|
| Random | 0.001 | 0.010 | 0.100 | 0.05% |
| Rules baseline | 0.009 | 0.188 | 0.689 | 0.9% |
| Logistic regression | 0.183 | 0.446 | 0.885 | 2.2% |
| **XGBoost (selected)** | **0.302** | **0.634** | **0.928** | **3.1%** |

- **Reasons:** the top 1% of the test queue has the model's top three reasons per alert. These explain the score and are not proof of laundering.
- **Scores:** `risk_score` is for ranking, not a probability.
- **Fair comparisons:** performance figures use test rows only. The last days of the data (17-27 Sep) are tiny and mostly laundering, so they are excluded.

More detail: [BQ1 methods and decisions](docs/decisions/BQ1_methods_and_decisions.md).

## BQ2: case expansion

BQ2 turns transactions into directed payment edges and looks one hop around an alert, within a window of 6-72 hours:

- **Upstream:** payments into the alert's sender before the alert.
- **Downstream:** payments out of the alert's receiver after the alert.

The default window is 48 hours, chosen from an evaluation on sampled laundering-pattern alerts ([results](bq2_window_tradeoff_evaluation.csv)). At 48 hours, 89% of cases recovered both the upstream and downstream partner, with a median of 3 candidate accounts. Going from 24 to 48 hours raised that from 70%, and 72 hours reached 98% with more payments to review.

## Investigation agent

BQ1 decides which alerts an analyst sees and in what order. The agent helps investigate them. For each day's top 100 alerts, code groups alerts that share an account within the window into cases. An LLM (Sonnet 4.6 on Bedrock) then calls read-only tools to gather:

- BQ2's payments around the alert (`get_case_graph`),
- each account's other activity (`get_account_activity`),
- BQ1's ranks for the payments nearby (`get_neighbour_scores`),
- other top alerts on the same accounts (`get_related_alerts`),

and writes a short brief, using at most 8 tool steps.

| Part of the brief | What it is |
|---|---|
| Money flow | Where the money came from, where it went, how fast and how much stayed put, per currency. Computed by code |
| Findings | Patterns, each citing the payments behind it: pass-through, fan-out, fan-in, money returning to its source, high-ranked neighbours, repeat alerts on the same accounts |
| Gaps | What could not be seen: no payments, a truncated window, too little time observed after the alert, too little account history |
| Next checks | What the analyst should look at next, from a fixed list |
| Summary | One short sentence from the LLM, with no numbers |
| Observations | Up to three short remarks of the LLM's own, citing the payments they are about. Shown as its reading, since the rules do not check them |

Design safeguards:

- **No labels.** The agent reads label-free tables through an allowlist of columns, and a label reaching it stops the run. Only the evaluation module reads labels, and the agent never imports it.
- **No BQ1 reasons.** They carry almost no information, so the agent never sees them and the dashboard does not show them.
- **Checked evidence.** A finding the LLM claims is accepted only if the same fixed rules, run by code, also find it in that run's tool results. Code checks every cited payment ID, and the displayed sentences take their numbers from tool results, not from the LLM.
- **Nothing lost.** No alert is removed or reordered. The analyst's list is the BQ1 queue, with the brief attached.
- **Bounded runs.** Each run has step, token, time and spend caps. If the LLM fails or its brief stays invalid after one repair, a code-only brief (S1) is used and marked as a fallback.
- **Realistic timing.** The daily batch sees data only up to the end of the alert's day, because that is all a daily run would know.
- **Thresholds set without labels.** The finding thresholds in `src/config.py` come from label-free distributions, with the rule written next to each value.

The output contract for the dashboard is in [docs/agent_output_format.md](docs/agent_output_format.md).

### Evaluation

The agent was run on the three test days (14-16 Sep, top 100 alerts a day, 237 cases) and compared with a code-only baseline (S1) that runs every tool for every case with the same rules. Full results: [docs/evaluation_results.md](docs/evaluation_results.md).

| Measure | Result |
|---|---|
| Briefs valid first time / repaired / fallback | 81% / 14% / 6% |
| Cost | About US$0.10 a case, US$25 for the three days |
| Latency | About 30 s a case (median) |
| Repeatability (14 Sep, 41 cases, three runs) | Same findings in 97.6% of cases |
| LLM vs code-only findings | The LLM kept almost all of the code's findings |

What the agent adds:

- **Evidence an analyst would otherwise gather by hand.** For each case it joins BQ2's money flow with BQ1's ranks and the accounts' other activity, and every finding cites the payments behind it. 94% of briefs passed validation (81% first time, 14% after one repair), and the rest fell back to the code-only brief, so an analyst always gets one. A brief costs about US$0.10 and takes about 30 s.
- **A signal the BQ1 rank does not carry.** The top 100 alerts are almost all laundering (236 of 237 cases), so findings cannot be compared inside it. On a deeper code-only list (top 500 a day, 1,259 cases, 35% laundering), cases with `FAN_OUT` were laundering 83% of the time against 30% without it, and the lift holds within BQ1 rank bands. The agent surfaces this to the analyst. `FAN_IN` points the other way (19% against 38%), so it should be read as context, not a risk sign, and `PASS_THROUGH` shows no lift.
- **Consistent, checkable output.** Rerunning the same cases gave the same findings 97.6% of the time. The LLM kept almost all of the code's findings (dropped 12 of 297, added 6), because a finding is accepted only if the fixed rules also find it.
- **What the LLM contributes beyond the rules.** Since findings come from fixed rules, the LLM's part is to write the one-sentence summary, choose which next checks apply, and add short observations tied to specific payments.

## Streamlit dashboard

The analyst dashboard starts in **Demo (offline)** mode, which needs no credentials. **Live batch (read-only)** reads the saved briefs from DynamoDB and S3 after an explicit load. Pages: Alert Queue, Investigation, Real-Time Alert Lookup and Agent Examples. See [docs/streamlit_dashboard.md](docs/streamlit_dashboard.md).

## Repository layout

| Path | What it is |
|---|---|
| `app.py`, `src/ui/` | Streamlit navigation, ranked queue, saved and on-demand briefs, graphs, evidence navigation, real-time monitoring and offline samples |
| `src/agent/` | Tools, case grouping, code-only baseline, LLM loop, validator, batch runner and storage |
| `src/evaluation/` | Evaluation: the only code that reads labels |
| `src/bq1/`, `src/shared/` | BQ1 score lookup (not used by the agent) |
| `src/bq2/queries.py` | BQ2 case query, used to check that the agent's edges match it |
| `src/bq2/evidence.py`, `transforms.py` | Structured evidence and transformations from BQ2 edges (not used by the agent) |
| `src/aws/athena.py` | Athena execution |
| `src/config.py` | Agent configuration: model, windows, caps and finding thresholds |
| `sql/bq1/`, `sql/agent/` | BQ1 queries; builders for the label-free agent tables |
| `scripts/` | Run the batch, the code-only baseline, samples and the evaluation, and rebuild tables |
| `infra/` | Deploys the Fargate batch and its trigger (Dockerfile, state machine, IAM roles, `deploy.sh`, `destroy.sh`). See [infra/README.md](infra/README.md) |
| `evaluate_bq2*.py` and CSVs | BQ2 window evaluation |
| `tests/` | Unit and UI tests |
| `docs/` | The dashboard guide, the output contract and the evaluation |

## Data

All data is in `s3://is459-aml-g1t5/` and the Athena database `is459_aml_gold`:

- **Gold:** `bq1_risk_scores_medium_v2`, `bq1_features_medium_v2`, `bq2_graph_edges_medium_v2`. These contain labels, so they are for evaluation and BQ1/BQ2 work only.
- **Agent tables:** `agent_scores_v2`, `agent_edges_v2`, `agent_day_queue_v2`. These are label-free copies, partitioned by `txn_date`. The first two are also bucketed for fast point lookups. They are built by `sql/agent/`.
- **Agent results:** DynamoDB `is459_agent_decisions` and `is459_agent_cases`, with traces under `s3://is459-aml-g1t5/agent/traces/`.

## Getting started

Use Python 3.12.4:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

### Commands

```bash
# Analyst dashboard; starts in Demo (offline). Select Live explicitly for AWS reads.
.venv/bin/streamlit run app.py

# Unit tests, with no AWS access or paid calls. Bare `pytest` fails on a name clash.
.venv/bin/python -m pytest tests
```

The evaluation needs access to labels and is run with `python -m scripts.run_evaluation`.

## Deployment

The pipeline is deployed in AWS `us-east-1` as three independent parts, each tagged by project (`is459-g1t5-etl-v2` for the ETL and agent batch, `is459-g1t5-rt` for real time) so that each can be removed on its own.

| Part | What is deployed | Runs |
|---|---|---|
| **ETL and Gold** | Step Functions `is459-etl-v2-silver-wap` (daily) and `is459-etl-v2-backfill`; Glue jobs for parse and audit, publish, BQ1, BQ2 and Gold commit; Lambdas that read the Silver snapshot and promote Gold; Silver on Iceberg, Gold on partitioned Parquet | Once per day, about 15-20 minutes. On success it publishes an event to EventBridge rule `is459-etl-v2-day-succeeded` |
| **Agent batch** | Step Functions `is459-agent-batch` running two Fargate tasks from one image (rebuild the label-free agent tables, then brief the day's cases), five IAM roles, an SNS topic for failures. Deployed from `infra/` | Started by the ETL's success event. About 15-20 minutes and US$8-9 for a day |
| **Real time** | A Kinesis stream created for each session, Lambdas `is459-rt-scorer` and `is459-rt-explainer` (container images in ECR), DynamoDB tables `is459_rt_*`, and Step Functions `is459-rt-session` to run a session unattended | On demand: a session replays a slice of transactions through the stream and tears the stream down afterwards |

The Streamlit dashboard is run locally with `streamlit run app.py` and reads the results from DynamoDB and S3.

### Agent batch

`infra/deploy.sh` builds the batch image and creates the Fargate task, the IAM roles, the state machine and the EventBridge target that starts a run when the ETL succeeds. `DRY_RUN=1 infra/deploy.sh` prints every command and changes nothing, and `infra/destroy.sh` removes it all. The batch role can read only the label-free tables. The table-rebuild role can read Gold, and never runs the LLM. Details: [infra/README.md](infra/README.md).
