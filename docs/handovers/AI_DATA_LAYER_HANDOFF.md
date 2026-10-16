# AI data layer handoff

This document is the developer entry point for integrating an LLM with the
validated IS459 AML investigation data layer. The detailed schema contract
remains [AI_CASE_CONTEXT_CONTRACT.md](AI_CASE_CONTEXT_CONTRACT.md).

The data layer supplies deterministic evidence. It does not determine guilt,
write prompts, call an LLM, or generate an investigation conclusion.

## Architecture and data flow

```text
BQ1 Gold risk scores ─┐
                     ├─ Athena/reused dashboard data
BQ2 Gold graph edges ┘               │
                                     ▼
                              load_case_context()
                                     │
                                     ▼
                           validated full CaseContext
                                     │
                                     ▼
                              build_llm_input()
                                     │
                                     ▼
                       validated bounded JSON evidence
                                     │
                                     ▼
                    downstream prompt/model/chatbot/UI
```

The configured serving tables are:

- BQ1: `is459_aml_gold.bq1_risk_scores_medium_v1`
- BQ2: `is459_aml_gold.bq2_graph_edges_medium_v1`

BQ1 contributes optional metadata for the selected alert: risk score, global
risk rank, model name, and model version. The score is a ranking signal, not a
calibrated laundering probability.

BQ2 supplies the alert edge plus upstream and downstream transaction evidence
within the requested horizon. The standard 48-hour request does not establish
anything about activity outside those 48 hours; a wider window requires a new,
explicitly authorised query.

## CaseContext and its two JSON forms

`CaseContext` is the schema-versioned factual boundary between deterministic
analytics and the future LLM layer. It contains the alert, optional BQ1
metadata, BQ2 transactions, relationships, ranked candidates, window
comparisons, data-quality disclosures, and provenance.

- The full context is the complete record of all evidence fetched for the
  requested horizon.
- The bounded input retains full-case statistics but limits individual
  candidates and transactions for model input. Its `projection` and
  `evidence_truncation` sections state the limits and what was omitted.

The real validation files are local operator artefacts and are Git-ignored:

```text
artifacts/aws-smoke/case_context.real.json
artifacts/aws-smoke/llm_input.real.json
```

Never commit genuine AWS investigation JSON. Checked-in synthetic examples are
available at:

- [examples/case_context.synthetic.json](examples/case_context.synthetic.json)
- [examples/llm_input.synthetic.json](examples/llm_input.synthetic.json)

## Public Python interface

The supported imports are:

```python
from src.context import build_case_context, build_llm_input, load_case_context
from src.context.validation import (
    serialize_case_context,
    validate_case_context,
    validate_llm_input,
)
from src.shared.contracts import AlertHandoff
```

The implemented signatures are:

```python
@dataclass(frozen=True)
class AlertHandoff:
    transaction_id: str
    source_account_key: str
    target_account_key: str
    transaction_timestamp: datetime

def build_case_context(
    *,
    handoff: AlertHandoff,
    raw_edges: pd.DataFrame,
    bq1_result: BQ1MetadataResult,
    queried_window_hours: int,
) -> dict[str, Any]: ...

def load_case_context(
    *,
    handoff: AlertHandoff,
    query_executor: Callable[..., pd.DataFrame] | None = None,
    raw_edges: pd.DataFrame | None = None,
    bq1_result: BQ1MetadataResult | None = None,
    bq1_row: Mapping[str, Any] | pd.Series | None = None,
    fetch_bq1: bool = False,
    queried_window_hours: int = 48,
    bq1_table: str = BQ1_SCORE_TABLE,
    bq2_table: str = BQ2_EDGE_TABLE,
) -> dict[str, Any]: ...

def build_llm_input(
    context: dict[str, Any],
    *,
    max_candidates: int = 20,
    max_transactions: int = 100,
) -> dict[str, Any]: ...

def validate_case_context(context: dict[str, Any]) -> None: ...
def validate_llm_input(context: dict[str, Any]) -> None: ...
def serialize_case_context(context: dict[str, Any]) -> str: ...
```

`query_executor` is dependency-injected, accepts `query=...`, and returns a
Pandas `DataFrame`. `load_case_context()` creates no AWS client itself.

## Scenario A: reuse an open Streamlit case

The dashboard already holds `raw_edges` and the four selected alert fields.
Pass the frame directly so the service issues no BQ2 query. Reuse an existing
BQ1 result when available; otherwise preserve its absence explicitly.

```python
from datetime import datetime

import pandas as pd

from src.bq1.contracts import BQ1MetadataResult
from src.context import build_llm_input, load_case_context
from src.context.validation import (
    serialize_case_context,
    validate_case_context,
    validate_llm_input,
)
from src.shared.contracts import AlertHandoff


def build_open_case_json(
    raw_edges: pd.DataFrame,
    *,
    transaction_id: str,
    source_account_key: str,
    target_account_key: str,
    transaction_timestamp: datetime,
    bq1_result: BQ1MetadataResult | None = None,
) -> tuple[str, str]:
    handoff = AlertHandoff(
        transaction_id=transaction_id,
        source_account_key=source_account_key,
        target_account_key=target_account_key,
        transaction_timestamp=transaction_timestamp,
    )
    if bq1_result is None:
        bq1_result = BQ1MetadataResult.unavailable(
            "BQ1 metadata was not loaded for this open dashboard case."
        )

    context = load_case_context(
        handoff=handoff,
        raw_edges=raw_edges,
        bq1_result=bq1_result,
        queried_window_hours=48,
    )
    llm_input = build_llm_input(
        context,
        max_candidates=20,
        max_transactions=100,
    )
    validate_case_context(context)
    validate_llm_input(llm_input)
    return (
        serialize_case_context(context),
        serialize_case_context(llm_input),
    )
```

This path performs zero application-level queries. Do not call Athena again on
each chatbot turn: retain the validated context in session state and rebuild
the projection only when the evidence or limits change.

## Scenario B: standalone CaseContext request

Create one reusable Athena client and inject the existing executor. With no
prefetched data and `fetch_bq1=True`, this service call issues exactly one BQ2
query and one BQ1 query.

```python
from functools import partial

from src.aws.athena import create_athena_client, execute_query
from src.config import ATHENA_DATABASE, AWS_REGION, BQ2_ATHENA_OUTPUT
from src.context import build_llm_input, load_case_context
from src.context.validation import (
    serialize_case_context,
    validate_case_context,
    validate_llm_input,
)
from src.shared.contracts import AlertHandoff


def retrieve_case_json(handoff: AlertHandoff) -> tuple[str, str]:
    athena_client = create_athena_client(AWS_REGION)
    query_executor = partial(
        execute_query,
        athena_client,
        database=ATHENA_DATABASE,
        output_location=BQ2_ATHENA_OUTPUT,
    )
    context = load_case_context(
        handoff=handoff,
        query_executor=query_executor,
        fetch_bq1=True,
        queried_window_hours=48,
    )
    llm_input = build_llm_input(
        context,
        max_candidates=20,
        max_transactions=100,
    )
    validate_case_context(context)
    validate_llm_input(llm_input)
    return (
        serialize_case_context(context),
        serialize_case_context(llm_input),
    )
```

Credentials come from Boto3's standard provider chain. Never put credentials
in this function, a prompt, source code, or the repository.

## Evidence semantics and safety rules

- BQ1 `risk_score` is a ranking signal, not a laundering probability.
- `risk_rank` is global across scored BQ1 rows.
- Candidate `candidate_rank` is a provisional investigation-order heuristic
  based on supporting transaction count, closest time gap, and account-key
  tiebreaking. It is not a probability or BQ1 rank.
- Evidence does not establish guilt. A downstream response must distinguish
  observed facts from hypotheses requiring analyst review.
- No verified XGBoost feature attributions are served. When
  `model_explanations.status` is `unavailable`, the LLM must not invent feature
  importance, reasons, or model explanations.
- Financial totals are grouped by currency without conversion. Never add
  amounts across currencies.
- Candidate and relationship `transaction_ids` reference records in
  `bq2.transactions`; candidate `relationship_ids` reference records in
  `bq2.relationships`.
- Candidate order is deterministic. Preserve the supplied order and rank.
- Available comparison windows never exceed `queried_horizon_hours`. Treat
  entries in `unavailable_windows_hours` as unavailable, not as zero activity.
- Ground-truth fields such as `evaluation_is_laundering`, `data_split`, and
  laundering-pattern labels are prohibited from LLM-facing context.
- If `evidence_truncation.truncated` is true, the input is not exhaustive.
  Use ID-only references as disclosures, not as evidence details.

## BQ1 availability

`bq1.status` has three meanings:

- `available`: validated metadata exists for the selected alert.
- `not_found`: an authorised lookup completed successfully but returned no
  matching row.
- `unavailable`: no reliable lookup result was supplied or requested.

For `not_found` and `unavailable`, metadata is `null`; do not fabricate a
score, rank, model, or explanation. A query failure is an error and must not be
converted to either missing-data status.

## Errors the integration must handle

Allow these failures to reach an explicit UI/error boundary:

- `AthenaQueryError` from `src.aws.athena` for failed or cancelled Athena work;
- `BQ1MetadataQueryError` and `BQ1MetadataIntegrityError` from
  `src.bq1.service`;
- `CaseContextServiceError` from `src.context` for missing or contradictory
  retrieval inputs;
- `EvidenceSchemaError` and `EvidenceIntegrityError` from `src.bq2.evidence`;
- `CaseContextIntegrityError` and `CaseContextSerializationError` from
  `src.context`; and
- `TypeError` or `ValueError` for invalid types, timestamps, or limits.

Never catch these errors by inventing evidence. Authentication, IAM, catalog,
data integrity, and missing-data outcomes must remain distinguishable.

## Why the bounded real payload is larger

The validated real files contain the same 8 transactions, 4 candidates, and 5
relationships because the configured limits did not truncate this case. The
full file is 22,509 bytes; the bounded file is 25,199 bytes, an increase of
2,690 bytes (11.95%).

This increase is expected under schema version `1.0`:

- `projection`, `evidence_truncation`, and their surrounding JSON structure
  add about 692 bytes;
- candidate and relationship `embedded_transaction_ids`,
  `id_only_transaction_ids`, and `transaction_details_complete` annotations
  add about 1,947 bytes; and
- the projected completeness flag and remaining formatting account for the
  small balance.

No full transaction record is duplicated. The material overhead is duplicated
reference IDs plus explicit completeness metadata. This is valid according to
the current contract and makes partial projections auditable.

Simple downstream improvements that preserve evidence and validation are:

1. Send only `llm_input.real.json`, not both full and bounded JSON, to a model.
2. Cache the validated projection for the case and limit settings instead of
   rebuilding or retransmitting it on every UI rerun.
3. If the provider accepts structured objects, pass the validated dictionary
   directly. If transport bytes matter, minify a copy only after validation;
   retain the canonical deterministic serialization for audit and testing.
4. Estimate provider tokens before submission and choose explicit candidate
   and transaction limits rather than deleting fields ad hoc.

Do not remove reference disclosures, omit quality fields, or weaken validators
to make the payload smaller.

## Downstream teammate checklist

1. Create/activate Python 3.12 `.venv` and run
   `python -m pip install -r requirements.txt`.
2. Run `.venv/bin/python scripts/demo_case_context_integration.py`; it is
   offline and uses synthetic Athena-shaped data.
3. Read both synthetic files under `docs/handovers/examples/` and the complete
   schema contract in `AI_CASE_CONTEXT_CONTRACT.md`.
4. Use `load_case_context()` as the retrieval/reuse boundary and
   `build_llm_input()` as the model-input boundary.
5. Handle explicit BQ1 availability states and the documented exceptions; do
   not transform failures into missing data.
6. Reuse `raw_edges`, BQ1 results, and validated context held by Streamlit.
   Never repeat Athena queries merely because the chat UI reran.
7. Build prompts from the bounded structured JSON, preserving identifiers as
   evidence references rather than flattening everything into prose.
8. Require generated claims to cite supporting transaction or relationship
   IDs and distinguish facts from analytical hypotheses.
9. Read `data_quality` and `evidence_truncation` before describing evidence as
   complete; disclose ID-only references and omitted counts.
10. Keep real files under `artifacts/aws-smoke/` local and out of Git. Use only
    synthetic fixtures in tests, examples, screenshots, or shared prompts.
11. Keep provider clients, prompts, response validation, chatbot state, and AI
    Streamlit UI outside `src/context/`; do not alter deterministic semantics
    to accommodate a model response.

## Downstream ownership

The downstream teammate now owns:

- LLM-provider selection, authentication, client lifecycle, and cost controls;
- grounded system/user prompt design using the bounded JSON;
- model-output schema and validation against cited evidence;
- chatbot conversation state and analyst-facing error handling;
- Streamlit AI components; and
- LLM-specific evaluation, safety testing, and observability.

The completed backend continues to own Athena retrieval, BQ1/BQ2 evidence
semantics, deterministic assembly, projection, validation, and serialization.
