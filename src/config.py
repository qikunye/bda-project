"""Shared settings for the investigation agent."""

AWS_REGION = "us-east-1"
ATHENA_DATABASE = "is459_aml_gold"

# Used by src/bq1, which the agent does not use. The agent reads AGENT_SCORES_TABLE below.
BQ1_SCORE_TABLE = "is459_aml_gold.bq1_risk_scores_medium_v1"

# --- Investigation agent. Reads only the label-free agent tables (sql/agent/).
# Scores + payment details, bucketed by transaction_id. The table also holds the risk model's reasons,
# which the agent never selects.
AGENT_SCORES_TABLE = "is459_aml_gold.agent_scores_v2"
# One row per edge endpoint (direction in/out), bucketed by account_key.
AGENT_EDGES_TABLE = "is459_aml_gold.agent_edges_v2"
# The top AGENT_QUEUE_MAX_K alerts of each day, for the day queue.
AGENT_QUEUE_TABLE = "is459_aml_gold.agent_day_queue_v2"
AGENT_QUEUE_MAX_K = 5000
# The primary workgroup's default output is another team's bucket, so always set this.
AGENT_ATHENA_OUTPUT = "s3://is459-aml-g1t5/athena-results/agent/"

# The dataset ends here. A daily batch stops earlier, at the end of its own day.
AGENT_DATA_HORIZON = "2022-09-16 23:59:59"
# Days with v2 BQ1 scores. Neighbours on other days are returned as unscored.
AGENT_SCORED_DATES = ("2022-09-14", "2022-09-15", "2022-09-16")

AGENT_TOP_K = 500
AGENT_WINDOW_H_DEFAULT = 48
AGENT_WINDOW_H_MIN = 6
AGENT_WINDOW_H_MAX = 72
AGENT_MAX_EDGES_PER_DIRECTION = 50
AGENT_MAX_NEIGHBOUR_IDS = 200
AGENT_MAX_RETURNED_TO_SOURCE = 10  # Entries of returned_to_source shown per graph.
# Accounts one get_related_alerts call may check: the alert's two plus graph counterparties.
AGENT_MAX_RELATED_ACCOUNTS = 25
# First day agent_edges_v2 covers (sql/agent/05). Account history cannot go back further.
AGENT_EDGES_FIRST_DAY = "2022-09-11"

# Case grouping and the code-only brief.
AGENT_BATCH_TOP_N = 100
AGENT_MAX_CASE_SIZE = 20
AGENT_S1_MAX_EVIDENCE = 10  # IDs the code-only brief cites per finding.

# Finding and gap thresholds. PLACEHOLDERS: set them from label-free distributions
# of the 14-16 Sep top alerts and of ordinary accounts, then commit them before any label is read.
# Do not tune them on results. Change one only with a note saying why and when.
AGENT_PASS_THROUGH_MAX_GAP_MIN = 360  # PASS_THROUGH: first onward payment within this many minutes.
AGENT_PASS_THROUGH_MIN_ONWARD_SHARE = 0.5  # ...and onward payments within that time of at least this share of the alert amount.
AGENT_FAN_MIN_COUNTERPARTIES = 5  # FAN_OUT / FAN_IN: distinct counterparties in the window.
AGENT_NEIGHBOURS_HIGH_MIN = 1  # NEIGHBOURS_HIGH_RISK: neighbours in the day's top K.
AGENT_MIN_HISTORY_DAYS = 2  # Below this, SHORT_HISTORY.
# Below this many hours of data after the alert, "no onward payment" says nothing (SHORT_OBSERVATION).
# Keep it equal to the PASS_THROUGH gap in hours.
AGENT_MIN_HOURS_OBSERVED = AGENT_PASS_THROUGH_MAX_GAP_MIN / 60
AGENT_MAX_EVIDENCE_IDS = 50  # IDs one finding may cite.

# The model the agent uses. Anthropic access was denied on 21 Oct and worked on 6 Nov.
AGENT_MODEL_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"

# The LLM loop. Every run is bounded by steps, tokens and time, and every
# process by a spend cap. The spend figures are estimates from list prices, not the bill.
AGENT_MAX_STEPS = 8  # LLM replies per run. A repair after an invalid brief is one extra.
AGENT_MAX_PARALLEL_CALLS = 4  # Tool calls the LLM may make in one reply.
AGENT_MAX_OUTPUT_TOKENS = 1500  # Per LLM reply.
AGENT_RUN_TOKEN_CAP = 120_000  # Input plus output tokens over one run.
AGENT_RUN_TIMEOUT_S = 150  # Checked before each LLM reply.
AGENT_SPEND_CAP_USD = 10.0  # Estimated spend per process, shared by all runs in it.
# USD per million (input, output) tokens: Sonnet 4.5 list price. Used for any model, so it
# over-estimates cheaper ones. Check the Bedrock pricing page before relying on it.
AGENT_PRICE_USD_PER_M_TOKENS = (3.0, 15.0)
AGENT_SUMMARY_MAX_CHARS = 200
# Free-form remarks the LLM may add beyond the verified findings. Code checks their IDs and wording,
# not whether the interpretation is right, so the app shows them as the agent's reading.
AGENT_MAX_OBSERVATIONS = 3
AGENT_OBSERVATION_MAX_CHARS = 240

# Batch runner and storage.
AGENT_DECISIONS_TABLE = "is459_agent_decisions"  # Key: transaction_id.
AGENT_CASES_TABLE = "is459_agent_cases"  # Key: day (hash) + case_id (range).
AGENT_TRACE_BUCKET = "is459-aml-g1t5"  # Traces go under agent/traces/<day>/.
AGENT_BATCH_CONCURRENCY = 4
AGENT_TABLE_TAG = ("project", "is459-g1t5-etl-v2")  # Same tag as is459_agent_decisions.
# The only S3 location rebuild_tables.py may delete from.
AGENT_TABLES_S3_PREFIX = "s3://is459-aml-g1t5/agent/tables/"

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
