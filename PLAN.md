# IS459 AML — Agent workstream: final plan (context for Claude Code)

Owner: Claire (agent workstream). Date: 21 Oct 2026, last updated 6 Nov 2026.
Assumption: the teammate has rebuilt the ETL as described in `ETL_Final_Plan.md` (daily lane: raw CSV -> S3 Bronze -> Step Functions; real-time lane: CSV slice -> Kinesis -> Lambda -> SQS). The agent only depends on the Gold v2 outputs, which do not change.
**Status 6 Nov:** the ETL rebuild is **finished** (`ETL_v2_Handover.docx` by JX; the CSV cutover was on 24 Oct), and the real-time path is built as a prototype. Agent milestone 1 is done; milestones 2-8 are not started and are all required. **The agent work, in order, is in section 19.** The team split is in `WORK_SPLIT.md` (D22). The handover is kept outside this repo, which has no ETL or real-time code: that code is in `s3://is459-aml-g1t5/exported/etl-v2/final/` and `rt/code/`. See section 16 for progress and findings F17-F25, and section 17 for decisions D16-D21.
> **Update 6 Nov:** the 21 Oct status said the rebuild had not started. The assumption above is also out of date in two details: the daily lane no longer uses Kinesis, and the real-time producer replays Silver slices.

**This file is the working document.** Progress, findings and decisions go in sections 16-17. Where a finding contradicts an earlier section, that section has a `> **Update:**` note. The old handovers in `docs/handovers/` are out of date.

Items marked **[VERIFY]** are things I believe but could not confirm from the docs. Check them in the repo or Athena before coding against them. Items marked **[PROPOSED]** are design choices, not facts. Resolved items are marked **[VERIFIED 21 Oct]**.

---

## 0. Instructions to Claude Code

1. Before writing code, run the checklist in section 14 ("Verify first") and report discrepancies.
2. Build in the order in section 12. Each milestone has an acceptance test; do not move on until it passes.
3. Hard rules (section 2) override anything else, including convenience.
4. Do not touch ETL, Silver, Gold, v1, or the real-time lane. The agent is read-only against Gold and writes only to its own DynamoDB tables and S3 prefix.
5. Prefer small, testable modules. Every tool is a plain Python function with typed inputs/outputs and unit tests that run without the LLM.

---

## 1. Project snapshot (facts)

- Dataset: IBM AML, LI-Medium. Region us-east-1, account 941377154820, bucket `s3://is459-aml-g1t5/`. AWS budget US$250 (roughly US$230 left).
- Two fixed business questions:
  - **BQ1**: alert prioritisation. XGBoost (frozen 3.2.0, iteration range 0-96), 26 causal features (+ `amt_vs_ccy_median` as 27th model input). Chronological split 60/20/20; test days are 14-16 Sep 2022. Recall@0.1% 0.302, @1% 0.634, @10% 0.928; Precision@1% about 3.1%. `risk_score` is a ranking score, not a probability. `risk_rank` is a **global** rank over the dataset (a per-day cut needs `row_number()` over the day).
  - **BQ2**: time-bounded upstream/downstream case expansion around a flagged transaction. Directed transaction-level edges (self-transfers excluded), configurable 6-72 h window, default 48 h. On 176 random-chain cases, 48 h finds both neighbours 89.2% of the time and at least one 100%; 72 h 98.3% but noisier.
- Gold tables (Parquet, partitioned by `txn_date`, read via Athena):
  - `is459_aml_gold.bq1_risk_scores_medium_v2` (scores, ranks, split; **[VERIFY]** it contains a label column, believed `evaluation_is_laundering`)
  - `is459_aml_gold.bq1_features_medium_v2`
  - `is459_aml_gold.bq2_graph_edges_medium_v2` (v1 equivalents exist with `_v1`)
- Reasons: `bq1_explanations_top1pct.csv`, 62,504 rows = top 1% of the **test** queue, keyed by `transaction_id`, 3 reasons per row with direction and size. **[VERIFY]** S3 location and exact column names. Alerts outside the top 1% have **no reasons**; tools must return `reasons: null` and the agent must cope.
- BQ2 lookup cost: a 48 h upstream+downstream lookup scans 76-126 MB, 1.3-1.7 s. An unpartitioned `transaction_id` lookup scans about 0.9 GB, so always filter by `txn_date`.
- Data horizon: v2 Gold edges cover up to **16 Sep 2022** (v2 loaded 14-16 Sep; 1-13 Sep point at v1 files). Alerts late on 15-16 Sep have a **truncated downstream window**. Tools must return `window_truncated: true` when the window passes the horizon. **[VERIFY]** the horizon by `SELECT max(...)` on the edge table.
  > **Update 6 Nov (F18):** Gold v2 now also holds 17 Sep (380 scores, 240 edges). The full days still end on 16 Sep, and the agent tables and config still stop there.
- Synthetic data warning: the dataset is simulated. A one-off ACH pattern is a known bias; do not claim real-world detection performance.
- Real-time lane (reference only): Lambda `is459-rt-scorer`, alert threshold 0.3605 (500th-highest v2 score on 15 Sep), alerts land in DynamoDB `is459_rt_alerts` and SQS `is459-rt-alert-events`. Out of scope for the agent (section 13).
  > **Update 6 Nov (F21, D17):** the Lambda uses the same model and 26 features as the daily lane. Alerts reach SQS through the DynamoDB stream, an EventBridge Pipe and a bus; the Lambda does not publish them itself. `is459-rt-explainer` writes a brief per alert to `is459_rt_briefs`, and `is459-rt-session` runs a whole session unattended. It is a prototype proven on replay slices only.
- Demo identifiers: alert `txn_630dea1dda9482dbd765a966355d6708` (sender `120075|81294D7A0`, receiver `15126|8127C1990`, 2022-09-03 12:27:00; in both BQ1 and BQ2 tables). Account key format: `<bank_id with leading zeros stripped>|<account_number>`.
  > **Update:** this alert is on 3 Sep, so it has **no v2 score**: v2 scores cover only 14-16 Sep. **New demo alert (D9):** `txn_a0f703e20da6f9bf0bdad4c0d62862de`, 2022-09-14 10:57, sender `6066|802DF3A20` → receiver `56081|814E08FE0`, 1,389,714.01 Rupee, per-day rank 2 (global 66), has reasons. Its 48 h graph is complete: 8 upstream edges from 2 accounts, 14 downstream edges to 5 accounts. 16 of the 22 neighbours are scored: 1 is in the day's top 500 (day rank 58) and 15 are outside it (promotion candidates).

---

## 2. Hard rules

1. **Read-only tools.** The agent cannot write anywhere except its own decisions tables/trace prefix.
2. **No labels.** The label column must never reach the agent or its prompts. Enforce twice: (a) agent queries run only against label-free Athena views (section 5), (b) the SQL builder uses a column allowlist. Labels are used only by the offline evaluation script, under a different IAM role/credentials.
3. **The agent never removes an alert from the BQ1 queue.** The full ranked queue always exists. The agent only produces an *agent-adjusted review list* (section 7) and annotations.
4. **All numbers and IDs come from tools.** The LLM chooses tools and verdicts; it does not type numbers or IDs from memory. Every cited ID is checked against tool outputs.
5. **Currency-safe.** Never sum amounts across currencies; aggregate per currency.
6. **Bounded.** Max 5 tool steps per run, a token cap, a per-run timeout, and a global spend cap.
7. **Reasons are model explanations, not proof.** Do not present them as evidence of wrongdoing.
8. ~~**No Bedrock.** Use an external OpenAI-compatible LLM API (section 9).~~ **Superseded (D1):** use Bedrock with the Converse API, starting with Nova 2 Lite. Only Anthropic models are blocked on this account.

---

## 3. What the agent is for

For each alert, answer: *is this worth an analyst's time, and what else belongs with it?* It does this by combining:
- **BQ1** (seed): rank, score, top-3 reasons.
- **BQ2** (evidence): who sent money into / out of the accounts within the window.
- **BQ1 again on the neighbours**: how risky BQ1 thinks each neighbour is. This is the loop-closing step and the main source of value.

Hypothesis to test (not a claim): BQ1 misses some laundering transactions (recall@1% about 0.63) that sit in chains next to ones it ranks highly; pulling those neighbours into the review list recovers some of them.

---

## 4. Triggers

| Trigger | When | Scope | Output |
|---|---|---|---|
| **B** (batch) | After the day's Gold commit | Top N cases of the day (default N=100, configurable) | Decisions + case queue in DynamoDB |
| **C** (on demand) | Analyst clicks "investigate" in Streamlit | One transaction | Verdict + full trace, shown live |
| A (real-time) | Alert on SQS | Stretch only | Not planned |

- **B in production**: EventBridge rule on Step Functions execution status change (source `aws.states`, status `SUCCEEDED`, state machine `is459-etl-v2-silver-wap`), reading `day` from the execution input. **[VERIFY]** input shape after the teammate's rebuild (expected `{day, source_csv_key, load_id}`).
  > **Update 6 Nov (F20, D19):** this rule already exists: `is459-etl-v2-day-succeeded` → SQS `is459-etl-v2-day-ready`. The input is `{day, day_compact, run_tag, source_csv_key}` per the handover; the agent only needs `day`, from `json.loads(detail.input)["day"]`.
- **B for 14-16 Sep**: these days are already committed and will not be rerun through the pipeline, so run B manually: `python -m agent.run_batch --day 2022-09-15`.
- **Where B runs [PROPOSED]**: a Glue Python shell job (same pattern as `is459_rt_producer`; Lambda's 15 min limit is too short for 100 cases x ~20 s). Locally, the same entrypoint runs from CloudShell/laptop.
- **C**: imports the same `agent` package inside the Streamlit process (Streamlit is hosted on EC2 per the course lab; credentials via instance role or `~/.aws/credentials`).
- Rate limits: bounded concurrency (default 4) with exponential backoff and `Retry-After`.

---

## 5. Data access: Athena views and tools

### 5.1 Label-free views [PROPOSED]
Create views in `is459_aml_gold`:
- `v_agent_scores` = `bq1_risk_scores_medium_v2` minus the label column (and any other label-derived column)
- `v_agent_edges` = `bq2_graph_edges_medium_v2` (**[VERIFY]** it has no label column; drop it if present)
- reasons table/view over `bq1_explanations_top1pct.csv` (external table or loaded copy)

The agent's role gets Athena + Glue read + S3 read on the Gold prefixes needed by these views only. It gets **no access to Silver** (which holds the label).
> **Update (D4):** there is no separate role. Everyone uses the one IAM user `g1team5`, and IAM cannot hide a column inside a Parquet file anyway. The label is kept out by (a) the label-free views, (b) the column allowlist in the SQL builder, and (c) a unit test that fails if a label column appears. The evaluation code is a separate module that the agent never imports.
> **Update:** the label `evaluation_is_laundering` is **also in the edge table**, so `v_agent_edges` must drop it. The features table calls it `is_laundering`.
> **Update 6 Nov (D20):** the ETL work also created label-free views in a separate database (`is459_agent.v_bq1_risk_scores`, `v_bq1_features`, `v_bq2_graph_edges`). The agent does not use them: its tools keep reading the bucketed agent tables (D13).

### 5.2 Tools
All tools return JSON with an `ids` list (every transaction/account ID in the result) so the validator can check citations. All SQL is parameterised from an allowlist; no free-form SQL from the LLM.

```
get_alert(txn_id) -> {txn_id, txn_date, ts, sender, receiver, amount, currency,
                      risk_score, risk_rank_global, risk_rank_day, reasons|null, ids}

get_case_graph(txn_id, window_h=48) -> {upstream:[edge...], downstream:[edge...],
                      per_currency_totals, n_upstream, n_downstream,
                      window_truncated, horizon_end, ids}
   # edge: {txn_id, ts, sender, receiver, amount, currency}
   # filter on txn_date range first (partition pruning); semantics per BQ2 handover

get_neighbour_scores(txn_ids, txn_dates) -> [{txn_id, risk_score, risk_rank_global,
                      risk_rank_day, in_day_top_k, has_reasons}]
   # must receive txn_dates so the query prunes partitions (never scan by txn_id alone)

get_related_alerts(accounts, window_h, day) -> [{txn_id, ts, risk_rank_day, account}]
   # other top-K alerts touching the same accounts in the window

submit_decision(decision_json) -> validated by code (section 6.3); the only way to finish
```

(`get_account_state` from earlier drafts is dropped; DynamoDB real-time state belongs to the real-time lane.)

Caching: per-process LRU on tool arguments so repeated calls and reruns do not re-bill Athena. Athena cost is about US$0.0005 per lookup at 100 MB, negligible.

---

## 6. The agent loop

### 6.1 Loop
```
seed = get_alert(txn_id)  (executed by code, not the LLM)
for step in 1..5:
    LLM sees: policy prompt + seed + all prior tool results (compact JSON)
    LLM returns either: a tool call, or submit_decision
    code executes the tool, appends result
    if submit_decision: validate (6.3); on pass -> done
on step 5 without a valid decision, or on failed validation after 1 repair retry:
    fallback = deterministic baseline verdict (section 8), flagged fallback=true
```
Use temperature 0. Use native tool/function calling if the model supports it; otherwise a strict JSON action protocol.

### 6.2 Policy prompt (outline, keep it short)
- Role: triage assistant for an alert review team; tools are read-only; you cannot see labels.
- Goal: decide a verdict using only tool results; cite evidence IDs.
- Decision guide (examples, not hard rules):
  - Reasons corroborated by the graph (e.g. reason says burst/new pair and the graph shows it) -> `escalate`.
  - Several neighbours also rank high in BQ1 -> `merge_into_case`.
  - A neighbour with a low BQ1 rank that sits in a fast chain with high-ranked members -> `promote` it.
  - Graph shows nothing that supports the reasons -> `deprioritise`.
  - No graph (self-transfer, no edges) or truncated window -> say so, lower confidence.
- Do not write numbers from memory; use claim codes and IDs.

### 6.3 Output schema and validation
```json
{
  "verdict": "escalate | merge_into_case | promote | deprioritise | need_more",
  "confidence": "low | medium | high",
  "case_id": "string|null",
  "promoted_txn_ids": ["..."],
  "claims": [
    {"code": "REASONS_CORROBORATED | NEIGHBOURS_HIGH_RISK | FAST_CHAIN |
              REPEAT_ACCOUNT_ALERTS | REASONS_NOT_SUPPORTED |
              NO_GRAPH_AVAILABLE | TRUNCATED_WINDOW | NO_REASONS_AVAILABLE",
     "evidence_ids": ["..."]}
  ],
  "note": "optional, max 200 chars"
}
```
Validation (all in code):
1. JSON parses and matches the schema; verdict and claim codes are in the enums.
2. Every `evidence_ids`, `promoted_txn_ids` and `case_id` member appears in some tool result `ids` from this run.
3. `promote` requires each promoted ID to come from `get_neighbour_scores` with `in_day_top_k = false`.
4. `note` may contain digits only if the digits appear in tool outputs; otherwise strip or reject.
5. `NO_GRAPH_AVAILABLE` / `TRUNCATED_WINDOW` claims must match the tool flags (code checks the flags itself; the LLM cannot assert them falsely).

Streamlit renders sentences from **claim code + tool numbers via templates**. The LLM never writes the displayed numbers. (This is the lesson from the earlier summariser attempt; do not reintroduce free-text numeric summaries.)

Log per run: steps, tool calls, tokens in/out, latency, validation outcome, `repair_used`, `fallback`.

---

## 7. Cases and the agent-adjusted review list

### 7.1 Case grouping (deterministic, no LLM)
Input: top-N alerts for the day by `risk_rank_day`.
Union-find: connect two alerts if they share an account (sender or receiver) and their timestamps are within the window (default 48 h). Cap case size (default 20). Representative = best-ranked member. B runs the agent once per case, seeded by the representative, with all members available via `get_related_alerts`.

### 7.2 Review list construction (budget K, default 500 per day)
- Baseline list L0: top K by BQ1 `risk_rank_day`.
- Agent list L1 [PROPOSED]:
  1. Start from L0.
  2. `escalate` / `merge_into_case`: members move up, grouped by case.
  3. `promote`: promoted neighbours are inserted near their case; to hold the budget at K, evict from the **bottom** of L0, preferring items the agent verdicted `deprioritise`, then lowest-ranked unreviewed items.
  4. `deprioritise`: moved down within the list; stays in the queue (never deleted).
- Honest consequence: at a fixed budget K, promotions displace items and **can lower recall if wrong**. Report both the fixed-K result and an extended-budget variant (K + number of promotions).

---

## 8. Deterministic baseline (required)

Same tools, no LLM, a fixed script, to show what the LLM adds:
- For each top-N alert: `get_case_graph(48h)`, `get_neighbour_scores`.
- Promote neighbours whose `risk_rank_day <= R2` (default R2 = 5,000) and that are within the 48 h chain; group alerts into cases with the same union-find; escalate cases with at least M high-ranked members (M=2); deprioritise alerts with no neighbours.
- It is also the **fallback** when the LLM output is invalid.
- Evaluation systems: S0 = BQ1 order; S1 = deterministic triage; S2 = LLM agent.

---

## 9. LLM client and cost

> **Update 6 Nov (D23):** the agent now starts on **Sonnet 4.5** (`us.anthropic.claude-sonnet-4-5-20250929-v1:0`), which returned the correct tool call in 3 of 3 tests. The note below that Anthropic models are denied is out of date.
>
> **Update (D1):** the first two bullets are superseded. Use the **Bedrock Converse API** with **Nova 2 Lite** (`us.amazon.nova-2-lite-v1:0`); the model ID is a config value. On 21 Oct a tool-calling test passed for: Nova 2 Lite, Nova Pro, `openai.gpt-oss-120b-1:0`, `qwen.qwen3-next-80b-a3b`, `deepseek.v3.2`, `mistral.mistral-large-3-675b-instruct`, `zai.glm-4.7`. Anthropic models are AccessDenied. If Nova does poorly on the 20-alert test (milestone 4), try gpt-oss-120b or DeepSeek V3.2 next.

- Use an OpenAI-compatible client with config via env/Secrets Manager: `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`. Swappable between OpenRouter, Gemini's OpenAI-compatible endpoint, etc. by changing config only.
- Why external: no Bedrock access. Self-hosting on a GPU (g5.xlarge about US$1/h) was rejected: cost shape, weaker tool calling, GPU quota risk.
- Free tiers: OpenRouter `:free` models are capped (20 req/min; 50/day, or 1,000/day after buying 10 credits). Gemini free limits are low and unofficial. Use free models only for development.
- Budget estimate **[PROPOSED]** (not measured): about 25k input + 1.5k output tokens per run, about 1,000 runs total -> roughly US$8-9 on a Flash-Lite-class model, about US$25 on a Flash-class model. Measure on the first 20 runs and update. Add a hard global spend/run cap in config.
- The dataset is synthetic, so sending it to an external API has no privacy problem.

---

## 10. Storage

> **Update 6 Nov (F19, D16):** `is459_agent_decisions` already exists, is empty, and has the key `transaction_id` only, not `day` + `txn_id`. Reuse it and store `day` as an attribute. `is459_agent_cases` does not exist yet and still has to be created.

DynamoDB (on demand), tables [PROPOSED]:
- `is459_agent_decisions`: pk `day` (e.g. `2022-09-15`), sk `txn_id`. Attributes: `trigger` (B/C), `verdict`, `confidence`, `case_id`, `claims`, `promoted_txn_ids`, `note`, `fallback`, `repair_used`, `steps`, `tokens_in`, `tokens_out`, `latency_ms`, `model`, `created_at`, `trace_key`.
- `is459_agent_cases`: pk `day`, sk `case_id`. Attributes: `members`, `representative`, `verdict`, `review_order`.
- Traces: `s3://is459-aml-g1t5/agent/traces/<day>/<txn_id>.json` (full tool calls and results).
- Idempotency: rerunning B for a day overwrites that day's rows (same keys). No versioning. Trigger C does not write decisions by default; it returns the result and trace to Streamlit (optionally save to `agent/traces/`).
- Cast DynamoDB numbers/strings explicitly (the real-time alert events carry numbers as strings).

Tag every resource `project=is459-g1t5-agent`. Add a small AWS Budget guardrail for the agent (suggest US$20).

---

## 11. Streamlit contract

> **Update:** the Streamlit app will probably change again after the agent loop and the ETL rebuild are done, so keep the agent's interface (`investigate()`, the DynamoDB tables, the claim templates) independent of the UI. The app must state that **the data ends on 16 Sep 2022**, so later alerts have a shortened downstream window. Where the app runs (laptop or EC2) is not decided; both the current app and Owen's use the default boto3 credential chain.

> **Update 6 Nov (F18):** Gold now holds a few rows after 16 Sep, so the notice should say that **full days end on 16 Sep 2022**, and later days are very small.

Streamlit is being redesigned; this is what the agent must provide:
- Queue view: day selector; table from `v_agent_scores` ordered by BQ1 rank, joined with `is459_agent_decisions` (verdict badge, case id). Toggle between BQ1 order and agent-adjusted order.
- Case view: BQ2 graph for the selected alert (window selector 6-72 h), highlighting promoted neighbours.
- "Investigate" button: calls `agent.investigate(txn_id, window_h)`; shows verdict, claims rendered via templates, evidence list (clickable IDs), and the step trace.
- Live alerts panel (optional): reads `is459_rt_alerts` (not SQS).
  > **Update 6 Nov (F21, D17):** the panel can also show briefs from `is459_rt_briefs` (same key, `txn`). These alerts get no agent verdict.
- Always show the disclaimer: reasons are model explanations; data is synthetic.

---

## 12. Build order and acceptance tests

> **Update 6 Nov (D22):** Claire owns milestones 2, 4, 5 and 8, and `investigate()`. Person 4 owns the evaluation (3, 7), and Person 2 owns the Streamlit screens (6). These no longer run strictly in this order: section 19 has the agent order. Milestone 4 starts on Nova 2 Lite (D1), not a free model.

1. **Data access** — create label-free views; `get_alert`, `get_case_graph`, `get_neighbour_scores`, `get_related_alerts` with unit tests. Accept: demo alert `txn_630dea...` returns graph and scores; each lookup scans <= ~150 MB; label column absent from every result (test fails if it appears).
2. **Case grouping + deterministic baseline (S1)** — run for 15 Sep. Accept: deterministic, reproducible output; cases and promotions listed.
3. **Evaluation harness** — offline script (separate credentials) computing metrics for S0 and S1 on 14-16 Sep. Accept: S0 recall@1% reproduces about 0.63.
4. **LLM loop + validation** — with a free model first. Accept: on 20 test alerts, report valid-output rate, repair rate, fallback rate, tokens per run.
5. **Batch runner (B)** — `run_batch --day`, bounded concurrency, writes decisions/cases/traces. Accept: a rerun is idempotent.
6. **Streamlit integration (C)** — `investigate()` plus trace rendering. Accept: demo alert end to end.
7. **Full evaluation** — S0 vs S1 vs S2 on 14-16 Sep with the chosen model; produce the report tables. Accept: all metrics in section 13 computed.
8. **EventBridge wiring for B** (last, optional for the demo).
   > **Update 6 Nov (D18, D19):** this milestone is now **required**, because the recorded demo must show one day end to end. The rule and queue already exist, so the work is a **consumer for `is459-etl-v2-day-ready`** plus the new-day changes in section 19. Accept: the 18 Sep success message starts the batch with no manual step, decisions appear in `is459_agent_decisions`, and Streamlit shows them. Rehearse it before the real run, and load 18 Sep only after the 14-16 Sep results are saved.

---

## 13. Evaluation plan

- Days: 14, 15, 16 Sep (BQ1 test days). Note the truncated downstream window on 16 Sep; report with and without it.
  > **Update 6 Nov (D18):** Gold already holds 17 Sep, and will hold 18 Sep, so anything that reads Gold directly must filter to 14-16 Sep. Truncation also affects 15 Sep (F6).
- Review budget: K = 500/day (matches the real-time alert threshold); also report K = 100 and 1,000.
- Systems: S0 (BQ1 order), S1 (deterministic triage), S2 (LLM agent).
- Metrics:
  - Recall@K and precision@K of laundering transactions (labels used here only)
  - Chain neighbours recovered: laundering transactions outside BQ1's top-K that enter the review list
  - Displacement cost: laundering transactions pushed out of the top-K
  - Workload: review items per case
  - Agent validity: valid-output rate, repair rate, fallback rate, rejected-citation rate
  - Cost and latency per run; agreement and reversal rate between S1 and S2
- Reproducibility: temperature 0; repeat a 50-alert subset 3 times to report variance.
- Claims must be modest: synthetic data; reasons are not proof; if S2 does not beat S1, report that and what the LLM adds qualitatively (traces, flexibility) rather than overstating.

Out of scope / stretch: Trigger A (real-time). If attempted later, the tools need an `as_of` timestamp so they cannot see data after the alert time, and downstream evidence will not exist yet (verdicts would be provisional). A separate pipeline-ops agent is also out of scope.

---

## 14. Verify first (Claude Code checklist)

1. Gold table schemas: exact column names in `bq1_risk_scores_medium_v2`, `bq1_features_medium_v2`, `bq2_graph_edges_medium_v2` (txn id, timestamps, account keys, amount, currency, split, rank, label column name).
2. Whether the label column exists in the edge table.
3. Edge table date horizon (`max(txn_date)`) and the exact window semantics in the BQ2 code/handover (anchor transaction, upstream/downstream definition, whether the anchor is included).
4. Location and schema of `bq1_explanations_top1pct.csv`; whether a table already exists.
5. Per-day rank: how to compute `risk_rank_day` (`row_number()` over day) and whether `data_split` marks test rows.
6. Athena workgroup, result location, and IAM role the agent should use; whether views can be created in `is459_aml_gold`.
7. Existing code to reuse: BQ2 lookup queries, BQ1 explanation code, the earlier Copilot design (`IS459_AI_Investigation_Copilot_Implementation_Plan.docx`: guardrails and `CaseContext`; the LLM summariser itself is dropped).
8. Rebuilt ETL: state machine name, execution input shape, and that Gold v2 table names and partitioning are unchanged.
9. Existing Streamlit code and how it authenticates to AWS.

---

## 15. Risks and open decisions

- **LLM choice and key** — undecided; pick after the 20-alert validity test (section 12, step 4).
- **N (cases per day) and K (review budget)** — defaults above; tune against cost and rate limits.
- **Truncated horizon** — alerts near the end of 16 Sep cannot see their full 48 h downstream; mitigate with the flag, or optionally read v1 edges for a complete horizon (**[VERIFY]** whether v1 edge data covers later days).
  > **Update (F6):** this is wider than the end of 16 Sep. With 48 h, **every 15 Sep and 16 Sep alert** is truncated downstream; only 14 Sep has the full window. v1 has just ~900 edges after 16 Sep, so it cannot fill the gap. The flag is the only mitigation, and the evaluation reports results per day with 14 Sep as the clean day.
  > **Update 6 Nov (F18):** still true. 17-27 Sep hold only 1,370 scored transactions in v1, so loading them does not fill the gap for 15-16 Sep alerts.
- **18 Sep run (F18, F22, F23):** the ETL part can run only once, so record it from the start; 19 Sep (244 transactions) is the backup. 18 Sep is very small, needs the rank fix in F22, and has no reasons. It proves the pipeline works, but is not a model result, so keep it out of the evaluation.
- **Live demo:** the ETL takes 13-20 minutes and cannot be repeated, so the live demo uses only repeatable steps on 14-16 Sep, with the recording as backup (D18).
- **Displacement risk** — promotions can hurt recall at fixed K; that is why S1 and the extended-budget comparison exist.
- **LLM adds little over S1** — possible; then the honest story is explainability and trace, not accuracy.
- **Synthetic-data bias** — state it in the report.
- **Cost drift** — enforce the spend cap; use cached tool results; reruns overwrite the same keys.
---

## 16. Progress log and findings

### Progress

| Date | Milestone | What was done |
|---|---|---|
| 21 Oct | 0 Verify first | Section 14 checklist run (findings F1-F12). Bedrock tool-calling test across 8 models. AWS spend checked. |
| 21 Oct | 1 Data access | Uploaded the reasons CSV and created `bq1_explanations_top1pct_v2`. Created views `v_agent_scores` and `v_agent_edges`, and the bucketed agent tables `agent_scores_v2`, `agent_edges_v2` and `agent_day_queue_v2` (D13). Built `src/agent/` (sql, data, tools, contracts) and `execute_query_with_stats`, plus 49 new unit tests (348 in total pass, no AWS needed). Smoke run on real Athena passed (numbers below). Not committed yet. |
| 21 Oct | **1 Data access: DONE** | All acceptance checks pass (section 18). |
| 6 Nov | Replan | Read `ETL_v2_Handover.docx` (the ETL is finished) and ran read-only AWS checks (F17-F25). Recorded decisions D16-D21. No agent code changed. Milestone 1 is committed on `feat/agent` (`a578227`); `src/agent/` holds only milestone 1. Next is milestone 2. |
| 6 Nov | Scope | The assessment needs a presentation, live demo and recorded demo (D18), so milestone 8 is now required. Work split agreed in `WORK_SPLIT.md` (D22). The agent order is in section 19. |

**Milestone 1 smoke run** (`python -m scripts.smoke_agent_tools`, 21 Oct). Each figure is one call; repeats are served from the cache at 0 MB.

| Case | get_alert | get_case_graph 48h | get_neighbour_scores | get_related_alerts |
|---|---|---|---|---|
| Demo, 14 Sep (22 neighbours) | 4.9 MB, 3.2 s | 44.0 MB, 2.6 s | 104.5 MB, 3.4 s | 1.2 MB (3 day queues, cached afterwards) |
| 16 Sep rank 10 (truncated) | 7.7 MB, 2.5 s | 27.6 MB, 2.6 s | 23.8 MB, 2.6 s | cached |
| 15 Sep rank 40,000 (no reasons, no graph) | 5.0 MB, 2.6 s | 5.5 MB, 2.5 s | (no neighbours) | cached |

That is 224 MB in total for the three alerts. The first design, which read `v_agent_scores` and Gold edges directly, scanned 3,077 MB and up to 839 MB per call. Each call takes about 2.5 s of Athena overhead. With `--compare-bq2`, the agent's case query returns **exactly the same edges** as the BQ2 owner's query at 6, 48 and 72 h for all three alerts.

Setup notes: `.venv` uses Python 3.13 (3.12 is not installed; all the pinned dependencies work). Run the tests with `pytest tests`: a bare `pytest` fails at collection because the root `test_athena.py` and `tests/unit/test_athena.py` have the same module name. That problem was already in the repo.

AWS spend (Cost Explorer, 21 Oct): Sep US$8.00; Oct to date US$17.60 (forecast US$22.89). About **US$224 left** of the US$250 course credit, assuming nothing was spent before September. The AWS budget alarms are monthly and account-wide (the ETL and real-time "guardrails" show the same total), so none of them tracks the total credit.

### Findings (section 14 checklist, 21 Oct)

- **F1 Schemas [VERIFIED].**
  - `bq1_risk_scores_medium_v2`: `transaction_id, source_account_key, target_account_key, transaction_timestamp, risk_score, risk_rank, model_name, model_version, data_split, evaluation_is_laundering, txn_date`.
  - `bq2_graph_edges_medium_v2`: `transaction_id, transaction_timestamp, source/target_account_key, source/target_entity_id, source/target_entity_name, amount_paid decimal(23,6), payment_currency, amount_received, receiving_currency, payment_format, is_cross_entity, is_cross_bank, is_cross_currency, evaluation_is_laundering, txn_date`.
  - `bq1_features_medium_v2`: the 26 features plus `is_laundering`.
- **F2 The label is in the edge table too** (`evaluation_is_laundering`). `v_agent_edges` must drop it.
- **F3 v1 = v2 on 14-16 Sep.** Same rows, scores and ranks in the BQ1 scores table, and the same edge counts per day. v2 scores cover **only 14-16 Sep**. The v2 edge table covers 1-16 Sep: its 1-13 Sep partitions point at v1's S3 folders, and 14-16 Sep at v2's own folders.
- **F4 14 Sep has a validation slice.** 00:00-05:37 on 14 Sep is `data_split = 'validation'` (488,561 rows); the rest of 14-16 Sep is `test`. The per-day queue and the evaluation must use test rows only.
- **F5 Reasons file.** Owen built it (`AML_Streamlit_App_Owen.zip`: `data/bq1_explanations_top1pct.csv` and `explanations/make_explanations.py`), from v1. All 62,504 IDs equal the top 1% of the v2 test queue, so it is valid for v2. Columns: `transaction_id, feature_N, reason_N, contribution_N` (N = 1-3). Reasons start with ↑/↓. Some contain quoted commas, so it needs OpenCSVSerde. Now in Athena as `is459_aml_gold.bq1_explanations_top1pct_v2` (S3 `gold/bq1/v2/explanations/`).
- **F6 The data ends on 16 Sep 23:59.** v1 has only ~900 edges for 17-27 Sep. With 48 h, every 15-16 Sep alert's downstream window is truncated; only 14 Sep is complete. Upstream is unaffected.
- **F7 Neighbours before 14 Sep have no v2 score.** Upstream edges for an early 14 Sep alert can fall on 12-13 Sep, and v2 scores do not cover those days. v1 has scores for them, but those rows are train/validation, which the model has seen.
- **F8 BQ2 window semantics** (`src/bq2/queries.py::build_case_edges_query`):
  - The graph is one hop.
  - Upstream = edges **into the alert's sender** with `ts <= alert_ts` and gap <= window.
  - Downstream = edges **out of the alert's receiver** with `ts >= alert_ts` and gap <= window.
  - The alert itself is returned as an `ALERT` row; each edge has a `gap_minutes` value.
  - It prunes on `txn_date`, and the v2 edges contain no self-transfers.
  - `build_alert_lookup_query` has **no date filter** (about 0.9 GB scan), so the agent must not use it.
- **F9 Athena.** Workgroup `primary`, which has a 10 GB per-query scan limit. Its default output location is someone else's bucket, so the agent must set `s3://is459-aml-g1t5/athena-results/agent/`. We can create tables and views in `is459_aml_gold`.
- **F10 Cost of `v_agent_scores`.** One lookup recomputes the whole day's rank: about **134 MB and 11 s** on 15 Sep. That is under the 150 MB target but slow, and a lookup across several days would be several times more (see D8).
- **F11 IAM / auth.** One IAM user (`g1team5`) for everything, no roles. Both Streamlit apps use the default boto3 credential chain (laptop login).
- **F12 ETL.** State machine `is459-etl-v2-silver-wap` exists (the pre-rebuild version); its last run succeeded on 18 Oct. Current execution input: `{"day", "day_compact", "run_tag", "stream_run_id", "reconciled_landing"}`. It has `day`, which is all trigger B needs. Re-check after the rebuild (milestone 8).

### Findings (milestone 1, 21 Oct)

- **F13 Random keys defeat Parquet pruning; bucketing fixes it.**
  - Transaction IDs and account keys are random hashes, so Athena reads the whole column for every day it touches. A transaction-ID lookup reads 60-100 MB per day, and the edges for one account read 10-26 MB per day per column.
  - Every selected column is read in full for those days, so the BQ2 query's unused entity name/ID columns roughly doubled its scan.
  - Hive bucketing (32 buckets) on the lookup key cut one ID lookup from 64 MB to 1.3 MB.
  - Athena cannot `INSERT INTO` a bucketed table, so adding a day means rebuilding the table (D13).
- **F14 About 2% of scored transactions are self-transfers** (36,980 / 37,010 / 54,493 on 14 / 15 / 16 Sep). They are scored and ranked but have no edge, so `get_alert` returns `amount: null` and the graph is empty (`no_graph: true`). Two of them are in the top 1% with reasons.
- **F15 The neighbour lookup grows with the number of IDs.** A long `IN (...)` list hits most buckets: 22 neighbours scanned 104.5 MB. Calls are capped at 200 IDs, but a call near the cap on 16 Sep could exceed 150 MB. That is acceptable (cost about US$0.001); watch it in milestone 4.
- **F16 The top of the queue all has reasons.** The top 5,000 of each day all fall within the top 1% (62,504 over the three days), so every alert the batch runner seeds (N = 100) has reasons. Alerts without reasons appear only as neighbours, or when an analyst investigates a lower-ranked alert.

### Findings (ETL handover and AWS checks, 6 Nov)

F17 comes from the handover, which I did not check step by step against the deployed state machine. F18-F24 were checked read-only in AWS on 6 Nov, except where marked.

- **F17 The daily lane is finished.** Step Functions `is459-etl-v2-silver-wap` runs: read the Silver snapshot → parse the day from the raw CSV → write to an Iceberg audit branch and run 11 quality rules → quality gate → publish to Silver → BQ1 and BQ2 in parallel → Gold commit. Any failure stops the run and sends an email. BQ1 uses incremental features and the frozen XGBoost model, so **scoring is part of the daily pipeline**. Handover figures: all outputs match v1, and a run takes about 15-20 minutes.
- **F18 Days after 16 Sep are tiny.** v2 Gold holds 17 Sep (380 scores, 240 edges). v1 row counts for later days are 18 Sep 353 scores and 225 edges, falling to 1-16 rows a day by 23-27 Sep. Per the handover, over half of 17 Sep is labelled laundering. These are the end of the dataset, not normal days. Committed days cannot be rerun, and only days after the watermark can load, so the next day must be 18 Sep.
- **F19 Decisions table.** `is459_agent_decisions` is ACTIVE, on demand, has 0 items, no secondary index, and key `transaction_id`. `is459_agent_cases` does not exist.
- **F20 Trigger.** Rule `is459-etl-v2-day-succeeded` is ENABLED, matches SUCCEEDED runs of the state machine, and targets `is459-etl-v2-day-ready` with no input change, so a message is the raw event. The queue is empty, so the message shape was checked from the rule, not a real message. Visibility timeout is 900 s, and after 5 receives a message moves to the dead-letter queue. A consumer must extend the timeout or delete promptly: a long batch that overruns 15 minutes could be redelivered.
- **F21 Real-time tables.** `is459_rt_alerts` (key `txn`, 12 items) holds `txn_ts, sender, receiver, amount, currency, payment_format, score, threshold, model_version, arrival_ms, scored_at_ms, ingest_to_score_ms`; `amount` is a string. `is459_rt_briefs` (key `txn`, 2 items) holds `narrative, next_checks, typology, typology_hint, confidence, status, input, score, txn_ts, latency_ms, model_version`. No reasons are stored on alerts. Handover: typology macro-F1 is 0.345, so do not lean on that field.
- **F22 New days get no daily rank (blocks the 18 Sep run).** New days have `data_split = 'excluded'`, but `v_agent_scores` only ranks `'test'` rows (D6). A new day would have `risk_rank_day = NULL` and an empty queue. **To fix before milestone 8:** rank `'excluded'` rows too in `sql/agent/02_v_agent_scores.sql`, keeping `'validation'` rows out, and check that 14-16 Sep ranks stay identical.
- **F23 New days have no reasons.** The reasons file only covers the 14-16 Sep top 1%, so F16 does not hold for new days. Every 18 Sep alert returns `reasons: null`, and the agent uses `NO_REASONS_AVAILABLE`.
- **F24 `is459-agent-readonly` does not match the agent as designed.** It misses the `agent_*` tables in Glue, S3 `agent/`, `is459_agent_cases`, DynamoDB `BatchGetItem` and Bedrock. It also allows the Gold base tables, which contain the label. This has no effect while everything runs as `g1team5` (D4), but the policy would need extending before any role uses it.
- **F25 Spend.** Cost Explorer: Sep US$8.00, Oct US$21.56, Nov to date US$0.14, so US$29.71 in total and about **US$220 left** of US$250, assuming nothing was spent before September. `is459-rt-guardrail` has a limit of US$26 and shows US$0.14, the same as November's account spend. So it looks monthly, and the "next session is blocked" warning in the handover has probably cleared. Not tested by starting a session.

---

## 17. Decisions

| ID | Decision | Why | Status |
|---|---|---|---|
| D1 | LLM = **Bedrock Converse API, Nova 2 Lite** first; the model ID is config. Fall back to gpt-oss-120b / DeepSeek V3.2 if quality is poor. Replaces rule 8 and section 9's external API. | Bedrock works (only Anthropic is blocked); no external key; swapping models is a one-line change. | Agreed 21 Oct |
| D2 | Agent code lives in **`src/agent/`** (entry points `python -m src.agent.run_batch`, `src.agent.investigate`). | Matches the existing `src/` layout and imports. | Done 21 Oct |
| D3 | Add **new `AGENT_*` constants** to `src/config.py`; leave the existing v1 constants alone. | 12 existing files import the v1 settings; changing them could break the old app and tests. Once the ETL rebuild is done, remove the v1 constants (and the code using them) if nothing still needs them. | Agreed 21 Oct; the v1 removal is **changed by D21** |
| D4 | **No separate IAM user/role** for labels. Use views + column allowlist + a label test; the evaluation code is a separate module. | IAM cannot hide a column inside a Parquet file; Lake Formation is too heavy for this project. | Agreed 21 Oct |
| D5 | Reasons come from Owen's CSV, uploaded as-is to `gold/bq1/v2/explanations/`; no rebuild. | It equals the v2 test top 1% exactly (F5). | Done 21 Oct |
| D6 | `risk_rank_day` ranks **test rows only**; validation rows keep their score but get rank NULL. | F4. | Done 21 Oct (`v_agent_scores`) |
| D7 | Neighbours dated outside 14-16 Sep return `scored: false`; **do not fall back to v1 scores**. | Those v1 rows are train/validation, so their scores are inflated (F7). Owen's app labelled them "indicative only". | Done 21 Oct |
| D8 | **Materialise the label-free scores** as `is459_aml_gold.agent_scores_v2` (CTAS, Parquet, partitioned by `txn_date`, with `risk_rank_day` precomputed; S3 `agent/tables/agent_scores_v2/`). The tools read this table, and `v_agent_scores` stays as its definition. Trigger B adds the new day after each ETL commit. | F10: the view recomputes ranks on every call. A one-off ~250 MB build makes each later lookup scan a few MB. It also means the agent's scores data has no label column at all. | Agreed 21 Oct; extended by D13 |
| D9 | The demo alert is picked from **14 Sep test rows** (full 48 h window): top 1% (has reasons), at least one upstream and one downstream edge, and ideally a neighbour outside the top K. | F6, and PLAN's alert is on 3 Sep (no v2 score). | Done 21 Oct: `txn_a0f703e2…` (section 1) |
| D10 | `get_case_graph` sends **at most 50 edges per direction** to the LLM (closest in time first). Counts and per-currency totals use all edges; an `edges_capped` flag shows when the list was cut. | Busy accounts can have hundreds of edges, which would overflow the token budget. | Done 21 Oct |
| D11 | `get_alert` takes **`(txn_id, txn_date)`**. | Lookup by ID alone scans every partition (F8). Streamlit and the batch runner always know the date. | Done 21 Oct |
| D12 | The Streamlit app must say **"data ends on 16 Sep 2022"**. The agent stays UI-independent, because the app will change after the agent and the ETL rebuild. | F6; the user's note of 21 Oct. | Agreed 21 Oct |
| D13 | The tools read **three bucketed or small agent tables** built from the label-free views (`sql/agent/04-06`), all under `s3://is459-aml-g1t5/agent/tables/`:<br>• `agent_scores_v2`: scores + per-day rank + payment details + reasons, bucketed by `transaction_id`<br>• `agent_edges_v2`: each edge once per endpoint with `direction` in/out, bucketed by `account_key`, 11-16 Sep<br>• `agent_day_queue_v2`: the top 5,000 per day<br>`get_case_graph` uses its **own slim case query** instead of calling `build_case_edges_query`. It applies the same window rule and returns identical edges (checked at 6/48/72 h). | F13: the first design scanned up to 839 MB per call; this one stays at or under 105 MB, and 224 MB in total for three alerts. One table read per tool. | Done 21 Oct |
| D14 | **`data_split` never reaches the agent.** It is not in the agent tables and is on the forbidden-column list, matching the existing CaseContext guardrail. The tools expose `in_queue` (has a per-day rank) instead. | `data_split` says which rows the model trained on, so it should not go into prompts. | Done 21 Oct |
| D15 | **Adding a day = rebuilding the agent tables**: drop each table, delete its S3 prefix, and rerun `04`, `05`, `06` with the new date range. This scans about 2.7 GB (about US$0.014). Milestone 5 wraps it in one command, run before the batch (trigger B). Remember to widen `AGENT_SCORED_DATES` / `AGENT_DATA_HORIZON` in config. | Athena cannot insert into bucketed tables (F13); a full rebuild is simple and idempotent. | Agreed with D13; implement in milestone 5 |
| D16 | **Reuse JX's `is459_agent_decisions`**, key `transaction_id`, with `day` as an attribute. Streamlit fetches decisions by the IDs in the day's queue. `is459_agent_cases` is still created by us. A batch rerun must overwrite or clear earlier rows for that day. | The table already exists and joins straight onto the alert queue (F19). | Agreed 6 Nov |
| D17 | **Real-time alerts are display only.** Streamlit shows alerts and briefs from DynamoDB; the agent runs on daily data only. | A just-arrived alert has no downstream evidence yet, and the real-time lane has no BQ2. | Agreed 6 Nov |
| D18 | **Report results on 14-16 Sep only.** The **recorded demo** follows one continuous day, 18 Sep: raw CSV → Step Functions → Gold → queue message → agent batch → Streamlit. The **live demo** uses repeatable steps on 14-16 Sep: the queue, Investigate on the demo alert, and the real-time panel. 18 Sep is a pipeline proof, not a model result, and needs the F22 fix. | The assessment needs a presentation, live demo and recorded demo, and the recording must show set-up and an end-to-end run. Committed days cannot be rerun, a run takes 13-20 minutes, and later days are tiny (F18). | Agreed 6 Nov |
| D19 | The batch runner **consumes `is459-etl-v2-day-ready`**. No new EventBridge rule. | It already exists (F20). | Agreed 6 Nov |
| D20 | The tools keep reading the **bucketed agent tables**, not the `is459_agent` views. | The bucketed tables keep scans small (D13, F13). | Agreed 6 Nov |
| D21 | **Keep v1 until after grading.** Then delete in the order given in the handover. | v2's 1-13 Sep edges and its parity checks read v1. | Agreed 6 Nov |
| D22 | **Work split:** Claire does the agent, one person does Streamlit, and four people take the rest (`WORK_SPLIT.md`). The evaluation moves to Person 4 as its own module. | The agent and Streamlit can run side by side once the output format is agreed. | Proposed 6 Nov; other owners TBC |
| D23 | The agent uses **Claude Sonnet 4.5 on Bedrock first**, through the Converse API, with `AGENT_MODEL_ID` as the setting. Replaces Nova 2 Lite as the starting model in D1. The older summary code stays on `BEDROCK_MODEL_ID`. | User's choice on 6 Nov. Tool calling passed 3 of 3, and the cross-region limit is 5M tokens per minute. | Agreed 6 Nov. **To check before large runs:** how Anthropic usage is billed, and whether course credits cover it. AWS lists the model as `LEGACY`, and access was denied on 21 Oct, so keep a second model ready |
| D24 | **One decision row per transaction in a case.** The representative gets the full row. Each other member, and each promoted neighbour, gets a short row with the verdict, `case_id` and `representative_txn_id`. | The table is keyed per transaction (D16), but a run reviews a whole case. Every alert can show a badge without repeating the evidence. | Proposed by Claude and built 6 Nov; Claire to confirm |
| D25 | **Code sets `case_id`** as `case_<representative txn id>`; the LLM does not choose it. `investigate()` takes `(txn_id, txn_date, window_h)`, not `(txn_id, window_h)`. This changes section 6.3 rule 2 and section 11. | A case ID comes from deterministic grouping, not a tool result (rule 4). The date avoids scanning every partition (D11). | Proposed by Claude and built 6 Nov; Claire to confirm |

---

## 18. Milestone 1 plan: data access (detailed)

**Status: DONE 21 Oct.** Results are in section 16. The plan below is kept as written; the build differs from it as follows:
- Extra tables: `sql/agent/05_agent_edges_v2.sql` and `06_agent_day_queue_v2.sql`. `04` is bucketed and also holds the reasons and payment details (D13). The tools read only `agent_scores_v2`, `agent_edges_v2` and `agent_day_queue_v2`. The views and the reasons table are only build sources.
- `get_case_graph` uses its own slim query, not `build_case_edges_query`. The equivalence check is `--compare-bq2`. The query does not return the alert row.
- `data_split` is not returned (D14); `in_queue` replaces it.
- `get_neighbour_scores` returns `{neighbours: [...], ids}` and `get_related_alerts` returns `{alerts: [...], top_k, window_h, ids}`, so every tool output is a dict with `ids`.
- `get_related_alerts(accounts, anchor_ts, window_h, exclude_txn_ids=())`; top K is at most 5,000 (the size of the queue table).
- Config names as built: `AGENT_SCORES_TABLE`, `AGENT_EDGES_TABLE`, `AGENT_QUEUE_TABLE`, `AGENT_QUEUE_MAX_K`, `AGENT_MAX_NEIGHBOUR_IDS`, `AGENT_FORBIDDEN_COLUMNS` (it also includes `data_split` and the laundering-pattern fields).

Goal: four read-only tools that return compact, label-free JSON with an `ids` list, tested without AWS, plus one real-AWS smoke run on the demo alert.

### Files

```
sql/agent/01_bq1_explanations_top1pct_v2.sql   (done)
sql/agent/02_v_agent_scores.sql                (done)
sql/agent/03_v_agent_edges.sql                 label-free edges view
sql/agent/04_agent_scores_v2.sql               CTAS + per-day INSERT (if D8 is accepted)
src/config.py                                  + AGENT_* constants (D3)
src/aws/athena.py                              + execute_query_with_stats() (bytes scanned, ms); execute_query unchanged
src/agent/__init__.py
src/agent/contracts.py                         TypedDicts for the tool outputs
src/agent/sql.py                               query builders: column allowlist, label guard, input validation
src/agent/data.py                              Athena runner: LRU cache keyed on SQL, scan log, runtime label check
src/agent/tools.py                             get_alert, get_case_graph, get_neighbour_scores, get_related_alerts
tests/unit/test_agent_sql.py
tests/unit/test_agent_tools.py                 fake runner, no AWS
scripts/smoke_agent_tools.py                   real AWS: demo alert end to end, prints bytes/latency
```

### Config constants (D3)

`AGENT_SCORES_TABLE` (D8 table, or `v_agent_scores` until then), `AGENT_EDGES_VIEW = v_agent_edges`, `AGENT_REASONS_TABLE = bq1_explanations_top1pct_v2`, `AGENT_ATHENA_OUTPUT = s3://is459-aml-g1t5/athena-results/agent/`, `AGENT_DATA_HORIZON = 2022-09-16 23:59:59`, `AGENT_SCORED_DATES = 14-16 Sep`, `AGENT_TOP_K = 500`, `AGENT_WINDOW_H_DEFAULT = 48` (allowed 6-72), `AGENT_MAX_EDGES_PER_DIRECTION = 50`, `AGENT_MODEL_ID = us.amazon.nova-2-lite-v1:0`, `LABEL_COLUMNS = {evaluation_is_laundering, is_laundering}`.

### Tool contracts

- **`get_alert(txn_id, txn_date)`** returns `{found, txn_id, txn_date, ts, sender, receiver, amount, currency, payment_format, risk_score, risk_rank_global, risk_rank_day, data_split, in_day_top_k, reasons, ids}`.
  - One query: scores (filtered by date) LEFT JOIN reasons, plus the alert's edge row (filtered by date) for the amount and currency.
  - `reasons` is a list of `{feature, sentence, direction: up|down, contribution}`, or `null` when the alert is not in the top 1%.
  - Not found returns `{found: false}`.
- **`get_case_graph(alert, window_h=48)`** returns `{upstream:[edge], downstream:[edge], n_upstream, n_downstream, n_upstream_accounts, n_downstream_accounts, per_currency_totals:{upstream:{ccy:amt}, downstream:{ccy:amt}}, window_h, window_truncated, horizon_end, edges_capped, ids}`.
  - An edge is `{txn_id, txn_date, ts, sender, receiver, amount, currency, payment_format, gap_minutes}`.
  - Reuses `build_case_edges_query` **unchanged** (its SQL is hash-pinned by existing tests), pointed at `v_agent_edges`. A `window_minutes` value is computed from `window_h`.
  - `window_truncated = alert_ts + window > AGENT_DATA_HORIZON`, worked out in code from config, not by the LLM.
  - Totals are grouped per currency and never summed across currencies (rule 5).
- **`get_neighbour_scores(txn_ids, txn_dates)`** returns a list of `{txn_id, scored, risk_score, risk_rank_global, risk_rank_day, data_split, in_day_top_k, has_reasons}`.
  - It needs dates and filters `txn_date IN (...)`. At most 200 IDs per call.
  - Dates outside 14-16 Sep return `scored: false` without a query (D7).
- **`get_related_alerts(accounts, anchor_ts, window_h)`** returns a list of `{txn_id, ts, risk_rank_day, account, role: sender|receiver}`.
  - Other top-K alerts on the same accounts within ±window of the anchor.
  - Served from a cached **day queue**: the top K of each day, one small query per day per process, filtered in Python. No per-call Athena scan.

All tools: inputs are validated (IDs `txn_[A-Za-z0-9]+`, account keys without quotes, dates inside the allowed set, window 6-72); SQL is built only from allowlisted columns; results go through a runtime check that raises if a label column appears; every transaction and account ID in the result is in `ids`; numbers are cast explicitly (Athena returns strings).

### Steps (in order)

0. Create `.venv` from `requirements.txt` (there is no local environment with boto3 yet).
1. SQL: write and apply `03_v_agent_edges.sql`. If D8 is accepted, write and apply `04_agent_scores_v2.sql` (CTAS for 14-16 Sep). Verify row counts against F3/F4 and that no label column exists.
2. Add the config constants and `execute_query_with_stats`.
3. `src/agent/sql.py` + `test_agent_sql.py`: builders, allowlist, input validation, label guard (the test fails if any builder could emit a label column).
4. `src/agent/data.py`: runner, cache, scan log, runtime label check.
5. `src/agent/tools.py` + `test_agent_tools.py` with a fake runner: output shapes, `ids` complete, `reasons: null` path, truncation flag at the edges of the horizon (14 Sep vs 15/16 Sep), per-currency totals, the edge cap, unscored neighbours, empty graph.
6. Pick the demo alert (D9) with one query; record it in section 1.
7. `scripts/smoke_agent_tools.py` on real AWS: run all four tools on the demo alert plus one 16 Sep alert (truncated) and one without reasons; record bytes scanned and latency per tool in section 16.

### Acceptance (milestone 1 is done when)

- `pytest` passes with no AWS access, including the existing tests.
- The smoke run returns the demo alert with reasons, a graph with at least one upstream and one downstream edge, and neighbour scores.
- Each tool call scans **<= 150 MB**, and a repeated call is served from the cache (0 bytes).
- No label column appears in any result (unit test plus runtime check).
- Section 16 is updated with the measured numbers and the demo alert.

---

## 19. Remaining agent work (Claire)

Do these in order. Sections 6-8 and 10 hold the design, and this section lists only the order and what has changed. File names are **[PROPOSED]**. Read `src/ai/llm_client.py` before writing a new Bedrock client, in case any of it can be reused.

### Step 1: Publish the output format
> **Done 6 Nov.** Types are in `src/agent/contracts.py`, sentences in `src/agent/claims.py`, rows in `src/agent/decisions.py`, sample results in `src/agent/sample_results.py`, and the guide for Streamlit in `docs/agent_output_format.md`. See D24 and D25. Not committed yet.

This comes first because Streamlit (Person 2) builds against it, using fake results until the agent is ready.
- Define the `investigate(txn_id, window_h)` result, the trace format, the decision fields (section 10 with D16) and the sentence template for each claim code.
- Add types to `src/agent/contracts.py`, and a small file of fake results for Streamlit.
- **Done when:** Person 2 can build the agent screens without asking questions. Neither side changes the format without telling the other.

### Step 2: Case grouping and S1 (milestone 2)
- `src/agent/cases.py`: union-find on shared accounts within the window, case size cap, best-ranked member as representative (7.1).
- `src/agent/baseline.py`: the fixed rules in section 8. It is both the comparison system and the LLM's fallback.
- Handle truncated windows, unscored neighbours (D7) and alerts with no graph (F14).
- **Done when:** a run on 15 Sep gives identical output twice, cases and promotions are listed, and unit tests pass without AWS. Then give Person 4 the output format for S1.

### Step 3: LLM loop and validator (milestone 4)
- `src/agent/llm.py`: Bedrock Converse client, with the model ID from config (D1), temperature 0 and backoff.
- `src/agent/validate.py`: all five checks in 6.3, with the code checking the flags itself.
- `src/agent/loop.py`: seed with `get_alert` in code, at most 5 tool steps, 1 repair, then S1 fallback. Add token, timeout and spend caps.
- Unit-test the loop with a fake LLM, including a test that fails if a label column reaches a prompt.
- **Done when:** a 20-alert run reports valid-output, repair and fallback rates, and tokens per run. This starts on Sonnet 4.5 (D23), with Nova 2 Lite as the backup. Check the billing point in D23 before running beyond the 20 alerts. Replace the section 9 cost estimate with measured numbers.

### Step 4: Batch runner and `investigate()` (milestone 5)
- `src/agent/run_batch.py`: `--day`, concurrency of 4, writes decisions, cases and traces.
- Create `is459_agent_cases` with a tagged, repeatable script.
- **Reruns (D16):** the decisions table is keyed on `transaction_id` with no day index. A rerun must overwrite or clear that day's earlier rows, or stale verdicts remain. Store `day` and a run ID on every row.
- `src/agent/rebuild_tables.py`: the one-command rebuild (D15).
- `src/agent/investigate.py`: same loop, returns the result and trace, and does not write decisions by default.
- Decide where the batch runs. Laptop or CloudShell is enough unless a real need for Glue appears.
- **Done when:** a rerun leaves identical rows and no stale ones, and `investigate()` works on the demo alert.

### Step 5: Run the batch on 14-16 Sep
- Save the decisions and traces, and tell Person 4 they are ready for S2.
- Fix what the evaluation shows, then rerun. Do not start step 6 until Person 4 confirms the results are saved.

### Step 6: New-day path (milestone 8)
- **Rank fix (F22):** rank `'excluded'` rows in `sql/agent/02_v_agent_scores.sql`, and keep `'validation'` rows out. Check that 14-16 Sep ranks stay identical.
- **Date ranges (D15):** widen `sql/agent/04-06`, `AGENT_SCORED_DATES` and `AGENT_DATA_HORIZON` so the rebuild takes the new day. Keep the evaluation on 14-16 Sep.
- **Consumer (D19):** read `is459-etl-v2-day-ready`, take `json.loads(detail.input)["day"]`, rebuild the tables, run the batch, then delete the message.
- **Timeout (F20):** the message is hidden for 15 minutes and moves to the dead-letter queue after 5 receives. Extend the timeout during a run, and delete the message only after success.
- **No reasons (F23):** check that a run with `reasons: null` everywhere still gives valid output. Test with a fake message, because the real ETL run can only happen once.
- **Done when:** a test message starts the whole batch with no manual step.

### Step 7: Recording and live demo support
- Be present for the one-time 18 Sep run with Person 3. Confirm the consumer is running before it starts.
- Before the live demo, check Investigate on the demo alert, and keep a saved result as a backup.

### What the agent work needs from others
| From | What | Needed for |
|---|---|---|
| Person 2 | Feedback on the output format | Step 1 |
| Person 3 | The agreed 18 Sep run time | Step 7 |
| Person 4 | Label-based results on S1 and S2, and confirmation they are saved | Steps 5 and 6 |
