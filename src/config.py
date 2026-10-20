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

# --- Triage agent (PLAN.md). Reads only the label-free agent tables (sql/agent/).
# The v1 constants above stay until the ETL rebuild is done (PLAN.md D3).
# Scores + reasons + payment details, bucketed by transaction_id.
AGENT_SCORES_TABLE = "is459_aml_gold.agent_scores_v2"
# One row per edge endpoint (direction in/out), bucketed by account_key.
AGENT_EDGES_TABLE = "is459_aml_gold.agent_edges_v2"
# The top AGENT_QUEUE_MAX_K alerts of each day, for the day queue.
AGENT_QUEUE_TABLE = "is459_aml_gold.agent_day_queue_v2"
AGENT_QUEUE_MAX_K = 5000
# The primary workgroup's default output is another team's bucket, so always set this.
AGENT_ATHENA_OUTPUT = "s3://is459-aml-g1t5/athena-results/agent/"

# The dataset ends here; downstream windows past it are truncated (PLAN.md F6).
AGENT_DATA_HORIZON = "2022-09-16 23:59:59"
# Days with v2 BQ1 scores. Neighbours on other days are returned as unscored (D7).
AGENT_SCORED_DATES = ("2022-09-14", "2022-09-15", "2022-09-16")

AGENT_TOP_K = 500
AGENT_WINDOW_H_DEFAULT = 48
AGENT_WINDOW_H_MIN = 6
AGENT_WINDOW_H_MAX = 72
AGENT_MAX_EDGES_PER_DIRECTION = 50
AGENT_MAX_NEIGHBOUR_IDS = 200

AGENT_MODEL_ID = BEDROCK_MODEL_ID

# Ground-truth columns that must never reach the agent or its prompts.
AGENT_FORBIDDEN_COLUMNS = frozenset(
    {
        "evaluation_is_laundering",
        "is_laundering",
        "laundering_pattern",
        "laundering_pattern_id",
        "pattern_type",
        "data_split",
    }
)
