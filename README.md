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

## Run the current app

Install the dependencies in `requirements.txt`, configure AWS access through
the normal AWS credential provider chain, and run:

```bash
streamlit run app.py
```

No AWS credentials should be stored in this repository.
