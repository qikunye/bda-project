# AML CaseContext contract

`CaseContext` is the validated factual handoff between the project's
deterministic BQ1/BQ2 analytics and a future LLM integration. It contains
evidence and documented semantics; it does not contain an investigation
conclusion, prompt, or model-generated explanation.

The contract schema version is `1.0`.

## Integration flow

```text
BQ1 metadata (optional)
          +
BQ2 raw_edges
          +
AlertHandoff
          ↓
build_case_context()
          ↓
Full CaseContext
          ↓
build_llm_input()
          ↓
Validated LLM-ready JSON
          ↓
Teammate's LLM implementation
```

The context package performs no AWS calls. The caller is responsible for
retrieving BQ1 metadata and BQ2 rows before invoking it.

## Complete CaseContext

The complete object has exactly these top-level fields:

```json
{
  "schema_version": "1.0",
  "alert": {},
  "bq1": {},
  "bq2": {
    "case_statistics": {},
    "transactions": [],
    "relationships": [],
    "candidates": [],
    "window_comparison": {}
  },
  "data_quality": {},
  "provenance": {}
}
```

- `alert` is the authoritative selected transaction identity from
  `AlertHandoff`, enriched only with amount, amount status, currency, and
  payment format from the matching BQ2 `ALERT` record.
- `bq1` preserves the existing `BQ1MetadataResult` shape: `status`, optional
  `metadata`, and optional `detail`. It adds an explicit
  `model_explanations` availability object.
- `bq2.case_statistics` describes the complete fetched case, including the
  anchor transaction.
- `bq2.transactions` contains every distinct BQ2 source transaction.
- `bq2.relationships` groups transactions by direction, source, target, and
  currency. Each relationship has a stable `relationship_id`.
- `bq2.candidates` contains external accounts in deterministic investigation
  order. Its transaction and relationship IDs link back to the underlying
  evidence records.
- `bq2.window_comparison` contains only windows supported by the fetched
  horizon. A larger window is explicitly unavailable.
- `data_quality` reports BQ1 availability, amount quality, window
  completeness, explanation availability, and ranking limitations.
- `provenance` identifies known source tables, model version, fetched horizon,
  ranking-policy version, and CaseContext version. It does not invent query
  IDs or timestamps.

All timestamps in the assembled context use UTC ISO 8601, for example
`2022-09-03T12:00:00Z`. The alert follows the source's minute-resolution
convention. Financial amounts are decimal strings so that JSON conversion
does not lose precision.

## Build and validate a complete context

```python
from src.context import build_case_context, serialize_case_context

context = build_case_context(
    handoff=alert_handoff,
    raw_edges=already_retrieved_bq2_dataframe,
    bq1_result=bq1_metadata_result,
    queried_window_hours=48,
)
json_payload = serialize_case_context(context)
```

Signature:

```python
def build_case_context(
    *,
    handoff: AlertHandoff,
    raw_edges: pd.DataFrame,
    bq1_result: BQ1MetadataResult,
    queried_window_hours: int,
) -> dict[str, Any]: ...
```

`build_case_context()` invokes the existing BQ2 evidence functions and then
validates identity, counts, amounts, references, ranking policy, and time
horizon. It neither queries Athena nor mutates its inputs.

## Using CaseContext with Real AWS Data

`load_case_context()` is the orchestration boundary for already-retrieved or
Athena-retrieved data:

```python
def load_case_context(
    *,
    handoff: AlertHandoff,
    query_executor: QueryExecutor | None = None,
    raw_edges: pd.DataFrame | None = None,
    bq1_result: BQ1MetadataResult | None = None,
    bq1_row: Mapping[str, Any] | pd.Series | None = None,
    fetch_bq1: bool = False,
    queried_window_hours: int = 48,
    bq1_table: str = BQ1_SCORE_TABLE,
    bq2_table: str = BQ2_EDGE_TABLE,
) -> dict[str, Any]: ...
```

The caller creates and reuses the existing Athena client. Credentials come
from boto3's normal credential provider chain and are not embedded in code:

```python
from functools import partial

from src.aws.athena import create_athena_client, execute_query
from src.config import (
    ATHENA_DATABASE,
    AWS_REGION,
    BQ2_ATHENA_OUTPUT,
)
from src.context.projection import build_llm_input
from src.context.service import load_case_context
from src.context.validation import serialize_case_context

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
    queried_window_hours=48,
    fetch_bq1=True,
)
payload = build_llm_input(
    context,
    max_candidates=20,
    max_transactions=100,
)
json_payload = serialize_case_context(payload)
```

The service builds the existing alert-anchored BQ2 query, explicitly converts
hours to minutes, and preserves its `txn_date` predicates. It never performs a
separate transaction-ID alert lookup. BQ1 lookup is partition-pruned to the
selected alert's UTC date and occurs only when `fetch_bq1=True`.

When the dashboard already has BQ2 edges, pass them directly to avoid another
Athena query:

```python
context = load_case_context(
    handoff=handoff,
    raw_edges=existing_raw_edges,
    bq1_result=existing_bq1_result,
    queried_window_hours=48,
)
```

The BQ1 inputs are mutually exclusive. Precedence is enforced by rejecting
contradictory combinations: use one existing `bq1_result`, one `bq1_row`, or
`fetch_bq1=True`. With none of these, BQ1 is explicitly `unavailable` and no
BQ1 query is issued. A successful empty lookup remains `not_found`.

Expected application-level query counts are:

| Inputs | BQ2 queries | BQ1 queries |
|---|---:|---:|
| Existing BQ2 frame and BQ1 result | 0 | 0 |
| Existing BQ2 frame and `fetch_bq1=True` | 0 | 1 |
| Handoff only | 1 | 0 |
| Handoff and `fetch_bq1=True` | 1 | 1 |

Callers should handle `CaseContextServiceError`, `AthenaQueryError`,
`BQ1MetadataQueryError`, `BQ1MetadataIntegrityError`, `EvidenceSchemaError`,
`EvidenceIntegrityError`, `CaseContextIntegrityError`, and
`CaseContextSerializationError`. Query and integrity failures propagate; they
are never converted into missing-data statuses.

## BQ1 availability and interpretation

`bq1.status` is exactly one of:

- `available`: `metadata` contains the verified BQ1 record.
- `not_found`: the lookup completed but found no matching record.
- `unavailable`: no reliable lookup result is available, for example because
  lookup was intentionally not attempted.

For `not_found` and `unavailable`, `metadata` is `null` and BQ2 evidence still
forms a valid case. A query failure must not be translated to `not_found`.

When present, `risk_score` is a ranking signal, not a calibrated laundering
probability. `risk_rank` is the global rank over all scored rows. Candidate
`candidate_rank` is a separate provisional investigation-order heuristic
based on supporting transaction count, closest time gap, and account-key
tiebreaking. It is not a BQ1 risk score or a laundering probability.

No verified feature-attribution data currently exists in the serving
contract. Therefore:

```json
"model_explanations": {
  "status": "unavailable",
  "explanations": null,
  "detail": "No verified feature-attribution evidence is available in the current BQ1 serving contract."
}
```

The downstream implementation must not invent top features or explanations.

## Evidence references and integrity

Candidate `transaction_ids` and relationship `transaction_ids` refer to
records in `bq2.transactions`. Candidate `relationship_ids` refer to records
in `bq2.relationships`. The complete context rejects dangling references.

The validator also checks:

- exactly one distinct `ALERT` transaction exists;
- its transaction ID, accounts, and UTC timestamp match `AlertHandoff`;
- available BQ1 metadata describes that same alert;
- distinct transaction and connected-account counts match the evidence;
- valid, missing, invalid, and true-zero amounts remain distinct;
- totals are separated by currency and valid amounts remain strings;
- candidate order and ranks follow the existing policy; and
- available comparison windows do not exceed the retrieved horizon.

An alert with no neighbours is valid: transactions and relationships still
contain its anchor evidence, while candidates are an empty list and neighbour
statistics are zero. Missing anchor evidence is an integrity error.

## Bounded LLM input

The full context is the record of all fetched evidence. Use the projection for
a bounded model request:

```python
from src.context import build_llm_input, serialize_case_context

llm_input = build_llm_input(
    context,
    max_candidates=20,
    max_transactions=100,
)
llm_json = serialize_case_context(llm_input)
```

Signature:

```python
def build_llm_input(
    context: dict[str, Any],
    *,
    max_candidates: int = 20,
    max_transactions: int = 100,
) -> dict[str, Any]: ...
```

The projection always keeps the alert, BQ1 section, full-case statistics,
window summaries, data quality, and provenance. It selects candidates by
existing rank and, when transaction limits apply, selects the alert first and
then transactions supporting the retained higher-ranked candidates.

Full `transaction_ids` remain source references. Each projected candidate and
relationship adds:

- `embedded_transaction_ids`: records present in projected `transactions`;
- `id_only_transaction_ids`: valid full-context IDs whose details were
  omitted from the projection; and
- `transaction_details_complete`: whether all referenced details are
  embedded.

`evidence_truncation` reports original, included, and omitted transaction,
candidate, and relationship counts, plus the number of relationships with
partial transaction details. `projection.case_statistics_scope` remains
`complete_fetched_case`; statistics are never recalculated from the subset.
The input must not be described as exhaustive when `truncated` is `true`.

## Serialization

```python
def serialize_case_context(context: dict[str, Any]) -> str: ...
```

The serializer validates either the complete or projected form and uses
strict, deterministic JSON. It rejects NaN, Infinity, `Decimal`, Pandas/NumPy
objects, sets, non-string dictionary keys, and arbitrary objects rather than
stringifying them.

Optional values are represented as JSON `null`. These include unavailable
BQ1 metadata and detail, nullable BQ1 score/rank/model fields, unavailable
model explanations, missing alert amount/currency/format, optional entity
metadata, incomplete relationship totals, and the unimplemented recommended
window.

## Safety exclusions

`evaluation_is_laundering`, `data_split`, laundering-pattern labels, and other
ground-truth evaluation fields are deliberately absent and rejected by the
validator. They are unavailable to the LLM because exposing them would leak
answers that an investigator would not possess at inference time.

## Exceptions callers must handle

- `BQ1MetadataIntegrityError` when available BQ1 metadata conflicts with the
  handoff;
- `BQ1MetadataQueryError` when the optional BQ1 lookup fails;
- `AthenaQueryError` when direct BQ2 retrieval fails or is cancelled;
- `CaseContextServiceError` for missing or contradictory service inputs;
- `EvidenceSchemaError` or `EvidenceIntegrityError` for malformed BQ2 rows;
- `CaseContextIntegrityError` for cross-section inconsistency;
- `CaseContextSerializationError` for non-JSON-safe values; and
- `TypeError` or `ValueError` for incorrect input types and invalid limits.

Do not weaken or catch these errors by fabricating fallback evidence.

## Synthetic examples

The checked-in examples use only identifiers beginning with `SYNTHETIC_` or
`txn_synthetic_`:

- `examples/case_context.synthetic.json` is a complete context.
- `examples/llm_input.synthetic.json` is a deliberately bounded projection.

A short alert/BQ1 excerpt is:

```json
{
  "alert": {
    "transaction_id": "txn_synthetic_alert",
    "source_account_key": "SYNTHETIC_ACCOUNT_A",
    "target_account_key": "SYNTHETIC_ACCOUNT_B",
    "transaction_timestamp": "2022-09-03T12:00:00Z",
    "amount": "100.25",
    "currency": "USD"
  },
  "bq1": {
    "status": "available",
    "metadata": {
      "risk_score": 0.84,
      "risk_rank": 12,
      "risk_score_semantics": "ranking_signal_not_calibrated_probability"
    }
  }
}
```

Regenerate both files from the implementation and synthetic test fixture:

```bash
.venv/bin/python -m scripts.generate_case_context_examples
```

## LLM teammate checklist

1. Receive or load the complete `CaseContext`; never query AWS inside the LLM
   client.
2. Call `build_llm_input()` with explicit limits appropriate to the request.
3. Call `serialize_case_context()` and send the resulting structured JSON as
   evidence input without flattening it into a prose paragraph.
4. Treat `risk_score` and `candidate_rank` according to their documented
   semantics, never as laundering probabilities.
5. Resolve claims to transaction and relationship IDs and state when details
   are ID-only because projection was truncated.
6. Read `data_quality` and `evidence_truncation` before claiming completeness.
7. Never infer unavailable BQ1 explanations or window recommendations.
8. Keep prompt engineering, provider calls, response validation, UI, and
   generated narrative outside `src/context/`.
