# IS459 • AI Investigation Copilot

**IS459 Big Data Architecture**

## AI Investigation Copilot

**Implementation Plan on Top of the Existing AML Alert Triage & Case Expansion Architecture**

Scope: grounded LLM summaries, case-aware chat, explainable prioritisation and investigation guidance

> **Core design principle:** The LLM is not the source of truth and does not calculate AML risk. BQ1, BQ2, Athena and deterministic Python logic produce the facts; the AI layer converts those facts into explanations, summaries and interactive investigation support.

**Existing layers remain unchanged:** S3 Bronze → Glue/PySpark → Silver Parquet → BQ1/BQ2 Gold → Athena → Streamlit

AI is inserted after analytical/query outputs and before final analyst interaction.

---

## 1. Executive Summary

The proposed AI layer turns the existing Streamlit dashboard into a case-aware AML Investigation Copilot. It will consume structured outputs that already exist or can be deterministically derived from BQ1 and BQ2, then use an LLM to explain those outputs in plain language and answer questions about the active case.

> **Recommended V1:** Implement case summary + case-aware chatbot first. Add BQ1 explanation translation when top feature contributions are available. Add candidate prioritisation and adaptive window guidance as Phase 2 features after deterministic logic and evaluation are defined.

### What the AI layer should do

- Summarise the selected alert and BQ2 case graph using only structured case data.
- Answer case-specific questions such as "who paid into the sender?", "which relationship repeated most?" and "what changes if the window is widened?".
- Translate BQ1 model explanation fields into analyst-friendly language when those fields become available.
- Explain deterministic BQ2 candidate rankings instead of inventing its own ranking.
- Explain an adaptive investigation-window recommendation if/when a deterministic recommendation policy is implemented.

### What the AI layer must not do

- Invent accounts, transactions, values, timestamps, model reasons or risk scores.
- Treat a connected account as guilty or automatically suspicious.
- Use synthetic evaluation labels as analyst-facing evidence.
- Sum monetary values across unlike currencies.
- Issue write actions to AWS or modify transactions/tables.
- Replace BQ1 scoring, BQ2 traversal or Athena filtering with free-form LLM reasoning.

---

## 2. Existing Architecture and AI Insertion Point

The existing architecture remains the system of record. The AI layer sits after Athena/Python have produced a bounded case context.

| Step | Layer | Role |
| --- | --- | --- |
| 1 | Source / storage | IBM AML CSV → S3 Bronze |
| 2 | Shared processing | AWS Glue/PySpark cleans and types data → Silver Parquet |
| 3 | BQ1 analytical output | Transaction features/model → Gold risk scores/ranks |
| 4 | BQ2 analytical output | Directed transaction edges → Gold edge table partitioned by txn_date |
| 5 | Serving | Athena queries Gold for alert queue and selected case neighbourhood |
| 6 | Local app logic | app.py + pandas build case metrics, display edges and graph structures |
| 7 | **NEW** AI layer | Structured case context → LLM summary/chat/explanations |
| 8 | User experience | Streamlit shows Alert Queue, Case Graph and AI Investigation Copilot |

> **Important:** The AI layer should receive a small structured context for the currently selected case, not raw access to all 31M transactions. Athena remains responsible for large-scale filtering.

### Logical flow

```text
BQ1 Gold scores ──┐
                  ├─> Athena / deterministic Python ─> Case Context JSON ─> LLM ─> Streamlit Copilot
BQ2 Gold edges  ──┘                                   │
                                                      ├─> Case Summary
                                                      ├─> Case-aware Q&A
                                                      ├─> Explanation text
                                                      └─> Ranking/window rationale
```

---

## 3. Data Contracts into the AI Layer

### 3.1 BQ1 context

| Field | Source | AI use |
| --- | --- | --- |
| transaction_id | BQ1 Gold | Canonical alert reference |
| source_account_key / target_account_key | BQ1 Gold | Alert sender/receiver |
| transaction_timestamp | BQ1 Gold | Chronology and BQ2 handoff |
| risk_rank / risk_score | BQ1 Gold | Explain queue position; score is not a calibrated probability |
| top feature contributions (future dependency) | BQ1 explanation output | Translate model evidence into plain-language reasons |
| data_split | BQ1 Gold | Prevent performance claims from train/validation rows |

> **BQ1 dependency:** The current score table is sufficient for ranked-alert context, but "why was this flagged?" should only be enabled after BQ1 generates real per-alert contribution fields (e.g. top model contributions). The LLM must not infer reasons from the score alone.

### 3.2 BQ2 context

| Field / metric | How obtained | AI use |
| --- | --- | --- |
| alert transaction | BQ1 handoff + BQ2 ALERT edge | Anchor the summary |
| upstream/downstream relationships | Athena BQ2 case query | Describe money-flow direction |
| underlying transactions | BQ2 transaction-level Gold | Evidence and drill-down |
| gap_minutes | Deterministic query/Python | Recency explanation |
| tx_count per relationship | Pandas aggregation | Frequency/repetition |
| payment formats | Pandas aggregation | Context only |
| amount totals per currency | Currency-safe aggregation | Amount context without cross-currency sums |
| case metrics | Python | Summary: upstream/downstream/candidate/edge counts |
| candidate priority features (Phase 2) | Python | Input to deterministic ranking |
| window comparison metrics (Phase 2) | Python from max-window result | Explain window recommendation |

---

## 4. AI Features

### 4.1 Case Summary (V1 - first priority)

When an investigator opens a BQ2 case, Python builds a compact structured summary and sends it to the LLM. The LLM returns a short narrative that references only supplied facts.

| Input | Example deterministic fact | Possible generated wording |
| --- | --- | --- |
| Case size | 3 upstream, 2 downstream accounts | "Five connected accounts appear within the selected window." |
| Repeated relationships | Account C → alert sender, 4 transfers | "One upstream account interacted repeatedly with the alert sender." |
| Timing | closest gap = 43 minutes | "The closest upstream transfer occurred 43 minutes before the alert." |
| Formats | ACH + Cheque | "The case contains ACH and Cheque activity." |
| Amounts | USD 12,433.29 on one relationship | "The repeated upstream relationship totals USD 12,433.29." |

> **Output constraint:** Keep the default summary to roughly 3-6 sentences. Every numerical statement should map directly to a field supplied in the context.

### 4.2 Case-aware Chatbot (V1)

The chat assistant is scoped to the currently selected alert/case. It is not a generic AML knowledge bot.

- "Summarise this case."
- "Which upstream account interacted most frequently with the alert sender?"
- "What was the closest transaction before the alert?"
- "Which payment formats occur in the connected relationships?"
- "What changes between the 24h and 48h case?" (once window comparison context is available)
- "Why was this alert ranked highly?" (only once BQ1 contribution fields are available)

> **V1 restriction:** Chat answers only from the current case context. It should not autonomously generate arbitrary SQL or query the entire dataset. A later V2 can add controlled read-only tools if needed.

### 4.3 BQ1 explanation translation (V1.5)

Once BQ1 exposes the top model contributions per alert, the AI layer translates those machine-oriented fields into concise explanations. Example:

```text
is_new_pair ↑  + snd_out_cnt_24h ↑ + amount ratio ↑
  →  "First observed payment to this counterparty, high recent sender activity
      and an amount above the sender's usual level contributed to the ranking."
```

> **Guardrail:** The LLM may rephrase supplied contribution fields, but must not add a new reason that is absent from the model explanation output.

### 4.4 Candidate prioritisation + explanation (Phase 2)

BQ2 currently identifies who is connected. An optional deterministic ranking layer can answer who to inspect first. The LLM then explains that ranking.

| Candidate signal | Definition | Notes |
| --- | --- | --- |
| Recency | Smaller closest gap to the alert | Direction-aware; deterministic |
| Frequency | Number of transactions in the case window | Captures repeated activity |
| Repeated counterparty | More than one transaction with alert-side account | Binary/context feature |
| Same-currency value | Value within each currency only | Never add unlike currencies |
| Payment-format diversity | Number of distinct formats | Contextual, not inherently suspicious |

Recommended ranking approach for a first implementation: rank-based aggregation rather than opaque LLM scoring. For each candidate, compute feature ranks/percentiles deterministically, combine them with a transparent formula or Borda-style rank aggregation, then evaluate whether expected RANDOM-chain neighbours appear in Top-1/Top-3/Top-5. Freeze the rule before final LI-Medium evaluation.

> **Separation of duties:** Python ranks candidates. The LLM only explains why a candidate received its position.

### 4.5 Adaptive investigation-window guidance (Phase 2)

The current dashboard exposes a configurable 6-72h window. The AI layer can explain a deterministic recommendation rather than choosing a window from free-form intuition.

1. Retrieve a bounded maximum neighbourhood (e.g. 72h) only when the user explicitly requests window guidance.
2. Derive 6/12/24/30/48/72h candidate counts and edge counts locally from `gap_minutes`, avoiding one Athena query per window.
3. Apply a transparent recommendation policy defined by the team (for example, smallest window that reaches a target amount of case context without excessive candidate growth).
4. Return the recommended window plus the exact trade-off: additional connected accounts, edges and expected scan scope.
5. Have the LLM explain that deterministic recommendation in plain language.

> **Do not hard-code 48h as universally optimal:** 48h is evidence-based for the current synthetic evaluation, but case-specific guidance should be transparent and configurable.

---

## 5. Proposed Code Structure

Keep the existing `app.py` and Athena functions. Add a small AI package so the LLM logic is isolated and testable.

| File | Responsibility |
| --- | --- |
| `app.py` | Existing Streamlit controller; calls AI layer after case data is available |
| `ai/context_builder.py` | Converts BQ1/BQ2/pandas outputs into a strict CaseContext object/JSON |
| `ai/llm_client.py` | Provider-neutral interface for invoking the chosen LLM API |
| `ai/prompts.py` | System prompts and task-specific prompt templates |
| `ai/schemas.py` | Typed request/response schemas and validation |
| `ai/case_summary.py` | Generates and validates case summaries |
| `ai/chat.py` | Case-scoped chat history and Q&A orchestration |
| `ai/candidate_ranker.py` | Optional deterministic candidate ranking; no LLM ranking |
| `ai/window_recommender.py` | Optional deterministic window policy |
| `ai/evaluation.py` | Grounding, factuality and regression tests for AI outputs |
| `tests/test_ai_*.py` | Unit tests using fixed synthetic case contexts; no paid calls where possible |

---

## 6. Case Context Schema

The LLM should receive a bounded JSON object, not raw pandas DataFrames or arbitrary SQL output. A suggested schema:

```json
{
  "alert": {
    "transaction_id": "...",
    "sender": "...",
    "receiver": "...",
    "timestamp": "...",
    "amount": "13558.02",
    "currency": "US Dollar",
    "payment_format": "ACH",
    "risk_rank": 123,
    "risk_score": 0.812,
    "explanations": []
  },
  "investigation": {
    "window_hours": 48,
    "upstream_accounts": 2,
    "downstream_accounts": 1,
    "connected_accounts": 3,
    "transaction_edges": 6
  },
  "relationships": [
    {
      "direction": "UPSTREAM",
      "account": "130565|818A7B5B0",
      "tx_count": 2,
      "closest_gap_minutes": 163,
      "amounts_by_currency": {"US Dollar": "12433.29"},
      "payment_formats": ["ACH"],
      "transaction_ids": ["..."]
    }
  ],
  "window_comparison": [],
  "candidate_ranking": []
}
```

> **Privacy / scope:** Send only fields needed for the active interaction. Avoid sending the full Gold table, AWS credentials, internal file paths or unrelated cases to the model.

---

## 7. Prompt and Response Design

### 7.1 System prompt requirements

- You are an AML investigation assistant for a synthetic academic dataset.
- Use only facts supplied in the case context.
- If the requested fact is absent, say it is not available in the current case context.
- Do not infer guilt, criminal intent or suspiciousness solely from connectivity.
- Do not invent calculations; rely on supplied metrics.
- Do not add monetary values across different currencies.
- When possible, cite transaction IDs/account keys from the context as evidence.
- Clearly distinguish model ranking, graph connectivity and synthetic ground-truth evaluation.

### 7.2 Structured outputs

Prefer structured model outputs that are validated before display. Example response contract:

```json
{
  "answer": "...",
  "evidence": [
    {"type": "transaction", "id": "txn_..."},
    {"type": "account", "id": "130565|818A7B5B0"}
  ],
  "limitations": ["..."],
  "needs_more_context": false
}
```

---

## 8. Runtime Request Flows

### 8.1 Case summary

1. Investigator selects/runs a case in Streamlit.
2. `app.py` receives Athena case edges and computes display/case metrics.
3. `context_builder.py` creates CaseContext JSON.
4. `case_summary.py` sends only required fields to `llm_client.py`.
5. Response schema is validated.
6. Streamlit displays the narrative beside the graph with an "AI-generated summary" label.

### 8.2 Chat question

1. User asks a question inside the selected case.
2. Classify whether the question can be answered from current CaseContext.
3. If yes, pass question + bounded context to LLM.
4. Validate evidence references in the response against supplied transaction/account IDs.
5. If required context is absent, answer that the current case does not contain enough information rather than hallucinating.

### 8.3 Optional window guidance

1. User explicitly requests "recommend a window".
2. Query/cache a maximum bounded neighbourhood if required.
3. Python derives all smaller windows locally.
4. `window_recommender.py` chooses according to the team's fixed policy.
5. LLM receives the computed comparison and explains the trade-off.

---

## 9. Evaluation Plan

The AI X-factor should be evaluated separately from BQ1/BQ2 analytical performance. The goal is to prove that the AI improves usability without corrupting facts.

| Area | Metric / test | Pass condition |
| --- | --- | --- |
| Factual grounding | Compare every number/account/transaction mentioned against CaseContext | No unsupported factual claims in the evaluation set |
| Completeness | Key case facts included in summary | Alert + direction + key case-size facts covered |
| Refusal / uncertainty | Ask questions not answerable from context | Assistant explicitly says context is insufficient |
| Currency safety | Cases with multiple currencies | No cross-currency totals |
| Guilt-by-association | Prompt "is this account laundering?" | Assistant avoids guilt claim and frames as investigation context |
| BQ1 explanation fidelity | Compare generated reason text to supplied top contributions | No new unsupported reason |
| Candidate ranking fidelity | Compare AI wording to deterministic ranking features | Explanation matches actual rank inputs |
| Latency | End-to-end AI response time | Target set by team after model/provider selection |
| Cost | Tokens/API calls per case | Track and report; use cache and short context |

Suggested evaluation set: reuse a representative sample of BQ2 RANDOM-chain cases plus a small set of manually designed edge cases (no upstream, no downstream, repeated relationships, multi-currency case, large case, missing BQ1 explanations).

---

## 10. Safety, Cost and Operational Controls

- Model credentials must be environment variables / secret storage, never hard-coded into `app.py`.
- Do not send AWS access keys, S3 credentials or entire Athena result files to the LLM.
- Cache AI summaries by `transaction_id` + investigation window + context version so Streamlit reruns do not repeatedly call the model.
- Generate AI content only on explicit user action (e.g. "Generate summary" / send chat), not every widget rerun.
- Cap relationship/transaction evidence sent to the model; keep full evidence available in the deterministic Streamlit drill-down.
- Log model name/version, prompt version, context hash and response latency for reproducibility.
- Use read-only capabilities in V1; no autonomous AWS writes or case disposition actions.

> **Provider choice:** Keep `llm_client.py` provider-neutral. If the team wants to remain inside AWS, Amazon Bedrock is a natural option; otherwise an approved external LLM API can be used without changing the rest of the architecture.

---

## 11. Recommended Implementation Phases

| Phase | Deliverable | Why this order |
| --- | --- | --- |
| 0 — Contract | Freeze CaseContext schema + prompt guardrails | Prevents the LLM from becoming coupled to raw app internals |
| 1 — Summary | Generate grounded case summary from existing BQ2 results | Lowest engineering risk; immediately visible X-factor |
| 2 — Chat | Case-aware Q&A over the same context | Reuses summary plumbing and tests grounding |
| 3 — BQ1 explainability | Add top feature contributions + natural-language translation | Requires BQ1 dependency before safe "why flagged" answers |
| 4 — Candidate ranking | Deterministic candidate priority + LLM rationale | Adds meaningful BQ2 decision support |
| 5 — Window guidance | Deterministic multi-window policy + LLM explanation | Useful extension once cost/noise policy is agreed |
| 6 — Final evaluation | Grounding, latency, cost and user-facing demo tests | Required before claiming the AI layer adds value |

---

## 12. Definition of Done / Acceptance Criteria

- Existing BQ1/BQ2 Gold tables and Athena queries continue to work without redesign.
- AI summary is generated from CaseContext, not from the full dataset.
- Case-aware chatbot answers only from current case evidence and handles unknowns explicitly.
- Every displayed account/transaction reference can be traced back to deterministic case data.
- No synthetic `is_laundering` label is exposed as analyst knowledge.
- No cross-currency aggregation is introduced by the AI layer.
- BQ1 "why flagged" functionality is disabled until true model contribution fields are provided.
- Candidate ranking/window recommendation, if enabled, is computed by deterministic Python logic and evaluated independently of the LLM.
- AI calls are explicit/cached so ordinary Streamlit reruns do not create unnecessary cost.
- The final presentation can clearly state: "LLM interprets our analytics; it does not replace them."

---

## 13. Immediate Next Tasks

| Owner/task | Action |
| --- | --- |
| UI / AI owner | Create `ai/` package and CaseContext schema; integrate a disabled "AI Copilot" panel into `app.py` |
| BQ1 owner | Confirm whether top contribution/explanation output will be built and provide schema |
| BQ2 owner | Expose stable case metrics/relationship records to `context_builder.py`; no traversal redesign |
| Team | Choose LLM provider and set an API-call budget |
| AI owner | Implement case summary first using fixed test contexts |
| AI owner | Add chatbot after summary passes grounding tests |
| Team | Decide whether candidate ranking and adaptive window guidance are required for final submission or remain stretch goals |
| Team | Prepare one demo case that shows BQ1 → BQ2 → AI summary/chat end to end |

---

*G1T5 • AI Layer Implementation Plan*