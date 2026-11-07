# Agent output format for Streamlit

You can build the agent screens now using made-up results. When the agent is ready, you replace the sample call with the real one, and the shape of the data stays the same.

The exact types are in [`src/agent/contracts.py`](../src/agent/contracts.py). If you need a field changed, tell Claire first, because the agent and the app both depend on it.

## Get sample data

```python
from src.agent.sample_results import sample_decision_rows, sample_investigations

results = sample_investigations()       # dict of InvestigateResult, by situation
result = results["raise_case"]
rows = sample_decision_rows()           # list of DecisionRow, as stored in DynamoDB
```

All sample IDs, numbers and verdicts are invented, and `stats["model"]` is `"sample"`. Never show them as real.

| Sample | What your screen should handle |
|---|---|
| `raise`, `raise_case`, `dont_raise`, `not_sure` | Each verdict. `raise_case` has other alerts in its case |
| `no_reasons` | `alert["reasons"]` is `None` |
| `no_graph` | No edges, verdict `not_sure`, and `amount`, `currency` and `payment_format` are `None` |
| `truncated` | Downstream window cut off. This is normal for batch results, which see data only up to the end of the alert's day |
| `repaired` | Agent output passed only after one retry |
| `fallback` | `stats["fallback"]` is `True`: the fixed rules made the verdict, not the LLM |
| `not_found` | `status` is `"not_found"`: `decision`, `stats`, `graph` and others are `None` |

## The three verdicts

| `verdict` | Meaning | In the agent's review list |
|---|---|---|
| `raise` | Show the analyst this case | Moves up with its case members |
| `dont_raise` | The case has payments, and they don't support the model's reasons | Moves to the bottom. It never leaves the list |
| `not_sure` | Not enough evidence either way, including no payments to check | Stays where BQ1 ranks it |

Related alerts are grouped into cases by code, so there is no merge verdict. The agent reviews only the day's top 100 and adds nothing from outside it. Related payments appear as evidence instead.

## The Investigate button

The real call will be:

```python
from src.agent.investigate import investigate

result = investigate(txn_id, txn_date, window_h=48)
```

It takes about 20 seconds or more, so show a spinner. Unlike the batch it has no end-of-day cutoff, so it can see later payments and its verdict can differ from the batch's. The date is required, because looking up an ID without it scans far more data. Allowed windows are 6-72 hours.

| Field | Show |
|---|---|
| `status` | If `"not_found"`, show a message and stop |
| `decision["verdict"]`, `decision["confidence"]` | Verdict badge and confidence |
| `rendered_claims[i]["sentence"]` | The explanation, one line per claim |
| `rendered_claims[i]["evidence_ids"]` | Clickable evidence IDs |
| `members` | Every alert in the case with its `alert` and `graph`, best rank first. Show these so the analyst needn't look them up. Empty only when `status` is `"not_found"` |
| `graph`, `neighbours`, `related` | What the agent looked at when deciding. Any of these can be `None` if the tool didn't run, so use `members` for each alert's payments |
| `trace` | The steps, in order. `actor` says whether code or the LLM chose each one |
| `stats` | Model, steps, tokens and latency. Label fallback results clearly |

## The review list

Read `is459_agent_decisions` by `transaction_id`, using the IDs in the day's queue. Agent results won't exist there until the batch has run, so an alert with no row just has no badge.

| `role` | Meaning | Has claims and stats? |
|---|---|---|
| `representative` | The alert the agent reviewed for this case | Yes |
| `member` | Another alert in the same case | No: use `representative_txn_id` to find them |

To show the evidence for a `member` row, fetch its representative's row. The saved run itself is in S3 at `trace_key`.

Rows don't store the rendered sentences. `investigate()` returns them directly. For batch rows, read the object at `trace_key`: its `result` has the same fields as `investigate()`, including `rendered_claims` and `members`.

For the agent's order, `src/agent/review_list.py` has `build_review_list(queue_txn_ids, rows)`: raised cases, then the rest by rank, then `dont_raise`.

## Rules

- **Show `sentence`, and treat `note` as optional extra text.** The sentences get their numbers from tool results. `note` is written by the LLM, and may be `None`.
- **Never add amounts across currencies.** `graph["per_currency_totals"]` is already split by currency.
- **Never remove an alert.** The BQ1 order must stay available, and the agent order is a separate view.
- **Always show these notices:** reasons are model explanations, not proof; the data is synthetic; full days end on 16 Sep 2022.
- **`no_graph` doesn't mean harmless.** It means there were no payments to check, such as for a self-transfer, so the verdict is `not_sure`.
