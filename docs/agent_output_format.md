# Agent output format for Streamlit

> **Status: current (D31).** The agent code returns this format, and the sample data below uses it. Streamlit hasn't started, so if a field is awkward for your screens, tell Claire and it can still change.

The agent no longer decides whether to raise an alert. BQ1 still decides which alerts the analyst sees and in what order. For each alert (or group of related alerts, called a case) the agent writes a short **evidence brief**: where the money came from, where it went, what patterns the payments show, what is missing, and what to check next. The analyst decides.

You can build the screens against sample results now. When the agent is ready, replace the sample call with the real one; the shape stays the same. The exact types are in [`src/agent/contracts.py`](../src/agent/contracts.py). If you need a field changed, tell Claire first, because the agent and the app both depend on it.

## Get sample data

```python
from src.agent.sample_results import sample_decision_rows, sample_investigations

results = sample_investigations()       # dict of InvestigateResult, by situation
result = results["fan_out_case"]
rows = sample_decision_rows()           # list of DecisionRow, as stored in DynamoDB
```

All sample IDs, numbers and sentences are invented, and `stats["model"]` is `"sample"`. Never show them as real. They are built by the real tool and brief code, so their shape is the shape of real results.

| Sample | What your screen should handle |
|---|---|
| `pass_through` | One finding, with money in and out. No gaps. A single alert, so `case_id` is `None` |
| `fan_out_case` | A case of two alerts, with several findings (fan-out, fan-in, neighbours, repeat alerts) and one observation |
| `money_returns` | Money that went back to an account it came from, with one observation |
| `quiet_truncated` | No findings, and two gaps: the window runs past the end of the data, and too little time is observed. Typical of late alerts on 16 Sep |
| `no_graph` | A self-transfer: no payments, `amount`, `currency` and `payment_format` are `None`, and the only gap is `NO_GRAPH_AVAILABLE` |
| `code_only` | A brief built by code alone (S1), with the template summary |
| `repaired` | The LLM's brief passed only after one retry |
| `fallback` | `fallback` is `True`: code alone built the brief because the LLM failed |
| `not_found` | `status` is `"not_found"`: `summary`, `stats` and the lists are empty or `None` |

## What a brief contains

| Part | What it is | Who writes it |
|---|---|---|
| `summary` | One short sentence. Never contains numbers | The LLM (code template if the LLM failed) |
| `money_flow` | Per alert in the case: payments in and out, totals per currency, how much went on, how fast, how long the data was observed | Code only |
| `findings` | Patterns, each with the payments behind it (`evidence_ids`) | The LLM picks the code and the evidence; code checks it and writes the sentence |
| `gaps` | What the agent could not see or check | Code only |
| `next_checks` | What the analyst should look at next | The LLM picks from a fixed list; code checks it and writes the sentence |
| `observations` | Up to three short remarks beyond the findings, each with the IDs it is about | The LLM writes them. Code checks the IDs and wording only, not that they are right. One that names a pattern the findings do not have, or breaks the wording rules, is dropped |

There is **no verdict and no confidence**. Do not show a "raise" or "risk" badge from the agent. The BQ1 score and rank still come from `alert`.

## The result of `investigate()`

```python
from src.agent.investigate import investigate

result = investigate(txn_id, txn_date, window_h=48)
```

It takes about 20 seconds or more, so show a spinner. Unlike the batch it has no end-of-day cutoff, so it can see later payments and its brief can differ from the batch's. The date is required, because looking up an ID without it scans far more data. Allowed windows are 6-72 hours.

| Field | Type | Show |
|---|---|---|
| `status` | `"ok"` or `"not_found"` | If `"not_found"`, show a message and stop. Everything below is empty or `None` |
| `txn_id`, `txn_date`, `window_h` | | What was asked for |
| `alert` | `Alert` | Score, day rank, payment details. **No `reasons` field** (see Rules) |
| `case_id` | `str` or `None` | `None` for a single-alert call (F28), so don't assume one |
| `summary` | `str` | One line at the top of the brief |
| `findings` | list of `{code, sentence, evidence_ids}` | One line each, with clickable evidence IDs. May be empty: that means no pattern was found, not that the alert is harmless |
| `gaps` | list of `{code, sentence}` | Show next to the findings, as a warning-style note |
| `next_checks` | list of `{code, sentence}` | A short checklist |
| `observations` | list of `{text, evidence_ids}` | Show apart from the findings, labelled as the agent's reading and not checked. May be empty, and is always empty when `fallback` is `True` |
| `money_flow` | list of `MemberMoneyFlow`, best rank first | A small table or card per alert |
| `members` | list of `{alert, graph}`, best rank first | Every alert in the case with its payments, so the analyst needn't look them up |
| `fallback` | `bool` | `True` means code alone built the brief (no LLM). Label it clearly |
| `stats` | model, steps, tokens, latency, validation outcome, `observations_dropped` | A "how this was made" panel |
| `trace` | list of steps | The steps in order. `actor` says whether code or the LLM chose each one |

`money_flow[i]`:

| Field | Meaning |
|---|---|
| `txn_id` | The alert it describes |
| `no_graph` | `True` if there are no payments to look at (such as a self-transfer) |
| `n_upstream`, `n_downstream` | Payments into the sender before the alert, and out of the receiver after it |
| `per_currency[currency]` | `upstream_total`, `downstream_total`, `quick_onward_share` (only onward payments within the pass-through gap threshold, 360 minutes for now, as a share of the alert amount, only in the alert's own currency, so `None` in other currencies; can be above 1; 0.0 means nothing went on quickly, even if `downstream_total` is large), `fastest_onward_gap_minutes` and `median_onward_gap_minutes` (minutes from the alert to an onward payment, `None` if nothing went on), and `fastest_onward_txn_id` |
| `hours_observed_after_alert` | How long after the alert the data reaches, capped at the window. "Nothing went on" only means something if this is large |

## Finding codes

Use these as labels or icons. Always show the `sentence`, not just the code.

| Code | Meaning |
|---|---|
| `PASS_THROUGH` | Money came in, and payments out of the receiver soon after came to a large share of the alert amount |
| `FAN_OUT` | The sender paid many different accounts around the alert |
| `FAN_IN` | The receiver was paid by many different accounts around the alert |
| `MONEY_RETURNS` | Money went back to an account it came from, or to the alert's sender |
| `NEIGHBOURS_HIGH_RISK` | Payments around the alert were themselves ranked high by BQ1 that day |
| `REPEAT_ACCOUNT_ALERTS` | Other top alerts involve the same accounts |

## Gap codes

Set by code from what the tools could not see. They are not failures: they say how far to trust a quiet result.

| Code | Meaning |
|---|---|
| `NO_GRAPH_AVAILABLE` | No payments into the sender or out of the receiver within the window. The accounts may still have other activity, which `get_account_activity` shows |
| `TRUNCATED_WINDOW` | The window runs past the end of the dataset (16 Sep), so later payments can never be seen. A batch run's own end-of-day cutoff does not set it; `SHORT_OBSERVATION` covers that |
| `SHORT_OBSERVATION` | Too little time after the alert was observed to say money did not move on |
| `SHORT_HISTORY` | The accounts' earlier activity was not checked, or too few earlier days are available to compare. Alerts on 14-16 Sep have at least three earlier days, so in practice this means the agent did not check |

## Next-check codes

| Code | Meaning |
|---|---|
| `CHECK_ONWARD_ACCOUNTS` | Look at where the money went next |
| `CHECK_SOURCE_ACCOUNTS` | Look at where the money came from |
| `CHECK_RETURN_FLOW` | Look at the money that came back |
| `REVIEW_RELATED_ALERTS` | Open the other alerts on these accounts |
| `CHECK_ACCOUNT_HISTORY` | Look at the account's earlier activity |
| `RECHECK_AFTER_MORE_DATA` | Come back once more days are loaded |

## The review list

The agent **does not reorder** the list any more. The analyst sees BQ1's order. The brief is attached to each alert.

Read `is459_agent_decisions` by `transaction_id`, using the IDs in the day's queue. Agent results won't exist there until the batch has run, so an alert with no row just has no brief.

| `role` | Meaning | Has the brief? |
|---|---|---|
| `representative` | The alert the agent reviewed for this case | Yes |
| `member` | Another alert in the same case | No: use `representative_txn_id` to find it |

Each row stores `finding_codes`, `gap_codes`, `next_check_codes` (lists of codes) and `observation_count` (the observation text lives in the saved result), so a badge or a filter does not need the trace. Rows no longer have `verdict`, `confidence` or `claims`. A natural list badge is the number of findings, or the finding codes as small tags.

Rows don't store the rendered sentences. `investigate()` returns them directly. For batch rows, read the object at `trace_key`: its `result` has the same fields as `investigate()`.

`is459_agent_cases` has one item per case (`day` + `case_id`): the member IDs, the finding codes, `fallback`, and `trace_key`.

`src/agent/review_list.py` has `build_review_list(queue_txn_ids, rows)`. It returns BQ1's order with each alert's `role`, `case_id`, `representative_txn_id`, `finding_codes`, `gap_codes` and `has_brief`. A member shows its representative's codes.

## Rules

- **Show `sentence`; treat `summary` as optional extra text.** Sentences get their numbers from tool results. `summary` is written by the LLM and contains no numbers.
- **Do not show BQ1 reasons** (D32). `alert` has no `reasons` field. The reasons carry almost no information (F35): the same two appear on all 300 top alerts, and some contain "nan". Keep the app's existing risk-score and rank display.
- **Observations are not verified.** Unlike findings, nothing checks that an observation is right. Label them clearly, never give them the weight of a finding, and keep them out of any count or filter built on findings.
- **Never add amounts across currencies.** Every total is already split by currency.
- **Never remove or hide an alert.** The BQ1 order is the order.
- **Findings are evidence, not accusations.** An empty `findings` list does not mean an alert is safe, and a finding does not mean it is laundering.
- **Always show these notices:** the data is synthetic; full days end on 16 Sep 2022.
- **`no_graph` doesn't mean harmless.** It means there were no payments to check, such as for a self-transfer.

## Choices to confirm when you start

1. Are `findings`, `gaps`, `next_checks` and `summary` the right split for your screens, or do you want them merged into one list?
2. Is `money_flow` as a separate list right, or do you want it inside each `members` entry? (It is the same data.)
3. Should the review-list badge be a finding count, finding-code tags, or nothing? The rows already hold the codes, so either works without a change.
4. Is a missing `case_id` for a single-alert `investigate()` call fine?
