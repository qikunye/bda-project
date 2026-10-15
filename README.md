# AML Alert Triage and Case Expansion

This repository contains the serving and investigation layer for the IS459
anti-money-laundering project.

## Current repository scope

The repository currently contains:

- the live BQ2 Streamlit and Athena investigation application;
- BQ2 evaluation tooling and evaluation results;
- BQ1 serving SQL and its documented handoff contract; and
- scaffolding for a future AI Investigation Copilot.

BQ1 model training and feature engineering are maintained externally. The
shared Silver ETL pipeline is also maintained externally. This repository
consumes their production outputs through Athena and does not contain either
upstream pipeline.

The external data contracts currently used by this repository are:

- Silver transactions: `is459_aml_silver.li_medium_transactions_medium_live_01`
- BQ1 scores: `is459_aml_gold.bq1_risk_scores_medium_v1`
- BQ2 edges: `is459_aml_gold.bq2_graph_edges_medium_v1`

## Current architecture

```text
Silver
 ├── upstream BQ1 pipeline → BQ1 Gold
 └── BQ2 edge processing   → BQ2 Gold

BQ1 Gold + BQ2 Gold
        ↓
      Athena
        ↓
    Streamlit
        ↓
 future AI Copilot
```

The BQ1-to-BQ2 handoff supplies the transaction ID, source account key,
target account key, and transaction timestamp for the selected alert.

## Local environment

Recommended Python: 3.12.4

Create an isolated environment and install the validated direct dependencies:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
streamlit run app.py
```

## Run the current app

Install the dependencies in `requirements.txt`, configure AWS access through
the normal AWS credential provider chain, and run:

```bash
streamlit run app.py
```

## AWS authentication

This project uses AWS CLI login-based credentials through boto3's standard
credential provider chain. Install the complete project requirements before
accessing AWS; the pinned Botocore CRT extra supplies the AWS CRT support that
the login credential provider requires.

An existing AWS CLI login session may expire and need to be refreshed
separately. A missing or expired login session is an authentication problem.
It is distinct from an IAM, Lake Formation, Glue, Athena, or S3 permission
denial received after authentication succeeds.

Never hard-code AWS credentials or store credentials, tokens, or authentication
cache contents in this repository.
