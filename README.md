# IS459 anti-money-laundering alert investigation

An end-to-end big data pipeline that helps an analyst decide which payments to review, what they connect to, and which cases deserve attention first.

It uses the synthetic IBM AML dataset (LI-Medium: 31.25M transactions, 16,041 labelled as laundering). The numbers here show how well the system recovers a synthetic pattern, not real-world detection performance.

| Stage | Question it answers | Output |
|---|---|---|
| **BQ1** alert prioritisation | Where should the analyst start? | A risk score and rank for every transaction, with reasons for the top alerts |
| **BQ2** case expansion | Where should the analyst investigate next? | The payments into the sender and out of the receiver around an alert |
| **Investigation agent** | What do the payments around this alert show? | An evidence brief per case: money flow, findings with cited payments, gaps and next checks, with no verdict |

## Pipeline

```text
DAILY      Raw CSV
             → Step Functions is459-etl-v2-silver-wap
               (parse → audit + 11 quality rules → publish Silver → BQ1 and BQ2 in parallel → Gold commit)
             → success message on SQS is459-etl-v2-day-ready
             → label-free agent tables rebuilt
             → top 100 alerts of the day grouped into cases
             → agent decides each case
             → DynamoDB + S3 traces → Streamlit

ON DEMAND  Analyst's Investigate button → same agent on one alert → Streamlit

REAL-TIME  Kinesis → Lambda is459-rt-scorer (same BQ1 model)
             → is459_rt_alerts → is459-rt-explainer → is459_rt_briefs → Streamlit
             Display only: it doesn't go through the agent.
```

The automatic link from the SQS message to the agent isn't built yet, so the batch is currently started by hand. See [Status](#status).

## BQ1: alert prioritisation

An XGBoost model scores every transaction from features describing the payment, recent sender and receiver activity, whether the pair has paid each other before, and how unusual the amount is. Features use only earlier transactions, and the data is split by time: train on 1-9 Sep, tune on 9-14 Sep, test on 14-16 Sep.

Results on the test days (6.25M transactions, 3,013 labelled as laundering):

| Model | Recall@0.1% | Recall@1% | Recall@10% | Precision@1% |
|---|---|---|---|---|
| Random | 0.001 | 0.010 | 0.100 | 0.05% |
| Rules baseline | 0.009 | 0.188 | 0.689 | 0.9% |
| Logistic regression | 0.183 | 0.446 | 0.885 | 2.2% |
| **XGBoost (selected)** | **0.302** | **0.634** | **0.928** | **3.1%** |

- **Reasons:** the top 1% of the test queue has the model's top three reasons per alert. These explain the score, and they aren't proof of laundering.
- **Scores:** `risk_score` is for ranking, not a probability.
- **Fair comparisons:** performance figures must use test rows only. The last days of the data (17-27 Sep) are tiny and mostly laundering, so they're excluded.

More detail: [BQ1 handover](docs/handovers/BQ1_HANDOVER.md) and [methods and decisions](docs/decisions/BQ1_methods_and_decisions.md).

## BQ2: case expansion

BQ2 turns transactions into directed payment edges and looks one hop around an alert, within a window of 6-72 hours:

- **Upstream:** payments into the alert's sender before the alert.
- **Downstream:** payments out of the alert's receiver after the alert.

The default window is 48 hours, chosen from an evaluation on sampled laundering-pattern alerts ([results](bq2_window_tradeoff_evaluation.csv)). At 48 hours, 89% of cases recovered both the upstream and downstream partner, with a median of 3 candidate accounts. Going from 24 to 48 hours raised that from 70%, and 72 hours reached 98% with more payments to review.

## Investigation agent

BQ1 decides which alerts an analyst sees and in what order. The agent helps investigate them. For each day's top 100 alerts, code groups alerts that share an account within the window into cases. An LLM (Sonnet 4.5 on Bedrock) then uses read-only tools to gather BQ2's payments, each account's other activity, BQ1's ranks for the payments nearby and other top alerts on the same accounts, and writes a short brief. It gives no verdict.

| Part of the brief | What it is |
|---|---|
| Money flow | Where the money came from, where it went, how fast and how much stayed put, per currency. Code only |
| Findings | Patterns, each citing the payments behind it: pass-through, fan-out, fan-in, money returning to its source, high-ranked neighbours, repeat alerts on the same accounts, activity spikes |
| Gaps | What could not be seen: no payments, a truncated window, too little time observed after the alert, too little account history |
| Next checks | What the analyst should look at next, from a fixed list |
| Summary | One short sentence from the LLM, with no numbers |

The main safeguards:

- **No labels:** the agent reads label-free tables, and a label reaching it stops the run.
- **No BQ1 reasons:** they carry almost no information (F35), so the agent never sees them.
- **Checked evidence:** the same fixed rules that a code-only brief (S1) uses must also find each finding the LLM claims. Code checks every cited ID, and the displayed sentences take their numbers from tool results.
- **Nothing lost:** no alert is ever removed or reordered. The analyst's list is the BQ1 queue, with the brief attached.
- **Limited runs:** each run has step, token, time and spend caps. If the LLM fails, S1 writes the brief and the result is marked as a fallback.
- **Realistic timing:** the batch sees data only up to the end of the alert's day, because that's all a daily run would know.

The finding thresholds in `src/config.py` are placeholders until they are set from label-free distributions and frozen. The agent has no final results yet. See [PLAN.md](PLAN.md) for the design and decisions, and the [output format](docs/agent_output_format.md) for the Streamlit contract.

## Status

| Part | Status |
|---|---|
| ETL and Gold tables | Done |
| BQ1 model, scores and reasons | Done |
| BQ2 edges and window evaluation | Done |
| Real-time path | Done, as a prototype proven on replay slices |
| Agent code | Built and unit tested with fakes; not yet run on real data |
| Agent batch on 14-16 Sep | To do |
| Agent evaluation | To do |
| Agent screens in Streamlit | Not started |
| Automatic trigger from the SQS message | To do |

## What's in this repository

This repository holds the investigation agent and the serving code around BQ1 and BQ2. These parts live elsewhere: the ETL, BQ1 training and feature engineering, the real-time path, and the Stage 5 dashboard (see its [handoff](docs/handovers/STREAMLIT_HANDOFF_OWEN.md)).

| Path | What it is |
|---|---|
| `src/agent/` | Tools, case grouping, baseline, LLM loop, validator, batch runner and storage |
| `src/bq1/`, `src/shared/` | BQ1 score lookup (not used by the agent) |
| `src/bq2/queries.py` | BQ2 case query, used to check that the agent's edges match it |
| `src/bq2/evidence.py`, `transforms.py` | Structured evidence and transformations from BQ2 edges (not used by the agent) |
| `src/aws/athena.py` | Athena execution |
| `sql/bq1/` | Ready-made BQ1 queries |
| `sql/agent/` | Builds the label-free agent tables |
| `scripts/` | Run the batch, baseline and sample, and rebuild tables |
| `evaluate_bq2*.py` and CSVs | BQ2 window evaluation |
| `docs/` | Handovers, decisions and the Streamlit contract |

## Data

Everything is in AWS `us-east-1`, in `s3://is459-aml-g1t5/` and the Athena database `is459_aml_gold`:

- **Gold:** `bq1_risk_scores_medium_v2`, `bq1_features_medium_v2`, `bq2_graph_edges_medium_v2`. These contain labels, so they're for evaluation and BQ1/BQ2 work only.
- **Agent:** `agent_scores_v2`, `agent_edges_v2`, `agent_day_queue_v2`. These are label-free.
- **Agent results:** DynamoDB `is459_agent_decisions` and `is459_agent_cases`, with traces under `s3://is459-aml-g1t5/agent/traces/`.

The BQ1 handover describes the earlier `v1` tables. `v1` and `v2` match on the test days.

## Setup

Use Python 3.12.4:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

## Commands

```bash
# Unit tests, with no AWS access or paid calls. Bare `pytest` fails on a name clash.
.venv/bin/python -m pytest tests

# One day's agent batch. A dry run unless you add --save. Spends money on Athena and Bedrock.
python -m scripts.run_batch --day 2022-09-14 --out batch.json

# The fixed-rule baseline for one day. Spends a little on Athena.
python -m scripts.run_s1 --day 2022-09-14 --out s1.json
```

## AWS authentication

This project uses AWS CLI login credentials through boto3's standard credential chain. The pinned Botocore CRT extra provides the support that login needs. A missing or expired login is an authentication problem, which is different from a permission denied after login succeeds.

Never hard-code AWS credentials or store credentials, tokens or authentication caches in this repository.
