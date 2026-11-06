# Agent output format for Streamlit

You can build the agent screens now using made-up results. When the agent is ready, you replace the sample call with the real one, and the shape of the data stays the same.

The exact types are in [`src/agent/contracts.py`](../src/agent/contracts.py). If you need a field changed, tell Claire first, because the agent and the app both depend on it.

## Get sample data

```python
from src.agent.sample_results import sample_decision_rows, sample_investigations

results = sample_investigations()       # dict of InvestigateResult, by situation
result = results["promote"]
rows = sample_decision_rows()           # list of DecisionRow, as stored in DynamoDB
```

All sample IDs, numbers and verdicts are invented, and `stats["model"]` is `"sample"`. Never show them as real.

| Sample | What your screen should handle |
|---|---|
| `escalate`, `merge_into_case`, `promote`, `deprioritise`, `need_more` | Each verdict |
| `no_reasons` | `alert["reasons"]` is `None` |
| `no_graph` | No edges, and `amount`, `currency` and `payment_format` are `None` |
| `truncated` | Downstream window cut off by the end of the data |
| `repaired` | Agent output passed only after one retry |
| `fallback` | `stats["fallback"]` is `True`: the fixed rules made the verdict, not the LLM |
| `not_found` | `status` is `"not_found"`: `decision`, `stats`, `graph` and others are `None` |

## The Investigate button

The real call will be:

```python
from src.agent.investigate import investigate   # Not built yet

result = investigate(txn_id, txn_date, window_h=48)
```

It takes about 20 seconds or more, so show a spinner. The date is required, because looking up an ID without it scans far more data. Allowed windows are 6-72 hours.

| Field | Show |
|---|---|
| `status` | If `"not_found"`, show a message and stop |
| `decision["verdict"]`, `decision["confidence"]` | Verdict badge and confidence |
| `rendered_claims[i]["sentence"]` | The explanation, one line per claim |
| `rendered_claims[i]["evidence_ids"]` | Clickable evidence IDs |
| `decision["promoted_txn_ids"]` | Neighbours to highlight on the graph |
| `graph`, `neighbours`, `related` | Supporting detail. Any of these can be `None` if the tool didn't run |
| `trace` | The steps, in order. `actor` says whether code or the LLM chose each one |
| `stats` | Model, steps, tokens and latency. Label fallback results clearly |

## The review list

Read `is459_agent_decisions` by `transaction_id`, using the IDs in the day's queue. Agent results won't exist there until the batch has run, so an alert with no row just has no badge.

| `role` | Meaning | Has claims and stats? |
|---|---|---|
| `representative` | The alert the agent reviewed for this case | Yes |
| `member` | Another alert in the same case | No: use `representative_txn_id` to find them |
| `promoted` | A low-ranked neighbour the agent pulled in | No: use `representative_txn_id` to find them |

To show the evidence for a `member` or `promoted` row, fetch its representative's row. The saved run itself is in S3 at `trace_key`.

Rows don't store the rendered sentences. `investigate()` returns them directly. For batch rows, `src/agent/claims.py` has `render_claims`, which needs the tool results from the trace in S3.

## Rules

- **Show `sentence`, and treat `note` as optional extra text.** The sentences get their numbers from tool results. `note` is written by the LLM, and may be `None`.
- **Never add amounts across currencies.** `graph["per_currency_totals"]` is already split by currency.
- **Never remove an alert.** The BQ1 order must stay available, and the agent order is a separate view.
- **Always show these notices:** reasons are model explanations, not proof; the data is synthetic; full days end on 16 Sep 2022.
- **`no_graph` isn't an innocent verdict.** It means there were no payments to check, such as for a self-transfer.
