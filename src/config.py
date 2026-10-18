"""Canonical configuration values for future shared application modules.

The current Streamlit application does not import this module yet. These
constants mirror its existing configuration without changing runtime behavior.
"""

AWS_REGION = "us-east-1"
ATHENA_DATABASE = "is459_aml_gold"

SILVER_TRANSACTION_TABLE = (
    "is459_aml_silver.li_medium_transactions_medium_live_01"
)
BQ1_SCORE_TABLE = "is459_aml_gold.bq1_risk_scores_medium_v1"
BQ2_EDGE_TABLE = "is459_aml_gold.bq2_graph_edges_medium_v1"

BQ2_ATHENA_OUTPUT = "s3://is459-aml-g1t5/athena-results/bq2/"

# Anthropic models are not available on this account's Bedrock; Amazon Nova is.
BEDROCK_MODEL_ID = "us.amazon.nova-2-lite-v1:0"
