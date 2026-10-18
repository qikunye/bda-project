# AI Copilot handoff

Status as of 18 October 2026. Owner: BQ1 / AI Copilot owner.

The AI Copilot backend produces a **grounded case summary** for one alert: a
short plain-English narrative written by an LLM, checked against the case
evidence before it can be shown, plus account-pattern facts and a review table
computed in Python. It consumes the bounded CaseContext from the AI data layer
([AI_DATA_LAYER_HANDOFF.md](AI_DATA_LAYER_HANDOFF.md)) and adds no Athena
queries of its own.

All data is synthetic (IBM AML dataset). A connected account is a lead to
review, not evidence of wrongdoing.

## Status at a glance

| Item | Status |
| --- | --- |
| Case summary (backend) | Done; prompt `summary-v4`; evaluated on 7 synthetic and 10 real cases |
| Answer validator (grounding, scope, style) | Done; runs in under 1 ms, no tokens |
| Account patterns and review table (Python) | Done |
| Case-aware chat | Not started |
| Streamlit Copilot panel | Not started; waits for the team to settle on one `app.py` |
| BQ1 "why was this flagged" reasons | Not integrated; CaseContext still reports explanations as unavailable (see Open issues) |
| Budget guard and per-case caching | Not started |

## Model provider

- **Amazon Bedrock, Amazon Nova 2 Lite**: `us.amazon.nova-2-lite-v1:0`, set in
  `src/config.py` as `BEDROCK_MODEL_ID`. Region `us-east-1`. Credentials come
  from the standard boto3 provider chain, as for Athena.
- **Why not Claude:** every Anthropic model (and OpenAI's GPT-5/6) on this
  account's Bedrock reports `agreementAvailability = NOT_AVAILABLE` and calls
  return "not available for this account", although the Anthropic use-case
  form was submitted on 18 October. The account belongs to an AWS
  Organization, which most likely blocks AWS Marketplace model subscriptions
  (inference, not confirmed). Amazon Nova and open-weight models are
  available.
- The client is provider-neutral (`StructuredLLMClient` protocol), so moving to
  Claude later, on Bedrock or through the Anthropic API, means adding one
  client class. Nothing else changes.

## How a summary is produced

```text
bounded CaseContext (build_llm_input)
        │
        ├─► build_grounding_index()      lookup sets, built once per case
        │
        ├─► prompt summary-v4 ─► Nova 2 Lite (forced submit_answer tool call)
        │                              │
        │                              ▼
        │              parse ─► grounding ─► scope ─► style checks
        │                              │
        │                  problems? ─► one retry with the problem list
        │
        ├─► build_pattern_summary()      both-sided / repeated-transfer accounts
        └─► build_review_table()         every candidate, one row each
```

The LLM writes only four things: the alert, the BQ1 rank, the case size, and
the first account in review order. Python writes everything that requires
scanning the list of connected accounts, because that is where every factual
error in testing occurred (see Evaluation).

## Public interface

```python
from src.ai.llm_client import BedrockConverseClient, create_bedrock_runtime_client
from src.ai.review import build_review_table
from src.ai.summary import generate_case_summary
from src.config import AWS_REGION

client = BedrockConverseClient(create_bedrock_runtime_client(AWS_REGION))
result = generate_case_summary(llm_input, client)  # llm_input from build_llm_input()
table = build_review_table(llm_input)
```

`generate_case_summary()` returns a `CaseSummaryResult`:

| Field | Meaning |
| --- | --- |
| `status` | `"grounded"`: an answer passed every blocking check. `"rejected"`: every attempt failed; there is no answer to show |
| `response` | `CopilotResponse` with `answer`, `evidence`, `limitations`, `needs_more_context`; `None` when rejected |
| `pattern_summary` | Python-written sentence on both-sided and repeated-transfer accounts. Present whatever the status; `None` only when there are no connected accounts |
| `style_issues` | Wording problems left in the shown answer (non-blocking) |
| `attempts` | Every model call with its token usage, latency and problems |
| `usage` | Total input and output tokens across attempts |
| `model_id`, `prompt_version`, `context_sha256` | Provenance for logging and caching |

### What the UI should show

| Outcome | Show |
| --- | --- |
| `status == "grounded"` | `response.answer`, labelled "AI-generated summary"; each `response.limitations` detail; `pattern_summary`; the review table |
| `status == "rejected"` | "AI summary unavailable for this case", then `pattern_summary` and the review table, which do not depend on the model |
| `LLMClientError` raised | "AI service unavailable: …" (Bedrock access, throttling, network). The BQ1 and BQ2 views stay unaffected |
| `CaseContextError` raised | The CaseContext was invalid; report it as a data error, not an AI error |

Generate a summary only on an explicit user action, and cache the result by
`(transaction_id, window, prompt_version, context_sha256)` so Streamlit reruns
do not call Bedrock again.

## Checks on every answer

All checks are deterministic Python over the bounded CaseContext.

**Grounding (blocking)**, in `src/ai/grounding.py`:

- every cited ID exists in the case under its stated type (transaction,
  relationship, account);
- every ID written in the answer text exists and is cited;
- every number in the answer and in limitation details, including numbers
  written as words, appears in the case. This rejects invented amounts,
  cross-currency sums and scores restated as percentages;
- a transfer count written after a connected account in the same sentence is
  that account's own count;
- lists of 3 or more bare numbers are rejected, because they cannot be
  attributed to accounts;
- an answer without evidence must set `needs_more_context`;
- the required disclosure codes (`evidence_truncated`, `missing_amounts`,
  `invalid_amounts`, `amounts_without_currency`, `bq1_not_available`) are
  present when they apply, and no code is claimed that does not apply.

**Scope (blocking, summary-v4)**, in `src/ai/summary.py`: the answer may name
no connected account except the first candidate, and may not describe
both-sided or repeated-transfer patterns.

**Style (non-blocking)**: JSON field names, or numbers written as words,
trigger one corrective retry. If the problem remains, the best grounded answer
is still shown and the problem is recorded in `style_issues`.

**Known limit:** the number check is set membership. A real number attached to
the wrong fact, or a negative claim such as "no other account…", cannot be
detected by lookup. summary-v4 avoids this by leaving those statements to
Python; the evaluation checks measure the rest.

## Prompt versions

All versions live in `src/ai/prompts.py` so they can be compared;
`DEFAULT_SUMMARY_PROMPT` is `summary-v4`.

| Version | Change | What testing showed |
| --- | --- | --- |
| summary-v1 | First grounded summary | Correct facts, but JSON field names in the text, missed patterns, frequent citation retries |
| summary-v2 | Explains upstream, downstream, both sides and repeated transfers; plain wording; digits | On a real 7-account case it wrote a list of transfer counts attributed to the wrong accounts. The validator could not catch this, which led to the pairing check |
| summary-v3 | At most 3 named accounts, each count next to its account | Fixed the list error, but on a synthetic case it said "no other candidates show these patterns", which was false |
| summary-v4 | The LLM writes the alert, BQ1, case size and first account only; Python writes the patterns | No factual errors found; current default |

## Evaluation

Quality checks (`src/ai/evaluation.py`) score what the analyst reads; for v4
that includes `pattern_summary`. They cover: mentions the alert, covers BQ1
(rank, or says unavailable), states upstream and downstream counts, names the
first candidate, names both-sided and repeated accounts among the first 3, no
field names, digits only, no guilt language.

| Case set | summary-v4 shown | All quality checks passed | Factual errors found on manual review |
| --- | --- | --- | --- |
| 7 synthetic cases (fixture variants and the docs example) | 7/7 | 7/7 | 0 |
| 10 real cases (BQ1 test queue positions 1 to 62,000) | 10/10 | 6/10 | 0 of 10 |

The 4 real-case quality failures are all one style problem: Nova wrote some
counts as words ("three upstream and zero downstream"). The values were
correct, and word-numbers are still checked by the validator.

The real cases were chosen by BQ1 test-queue position only, never by label,
and skip self-transfers, which have no BQ2 trail. Four of them have no
connected accounts; the largest has 7 accounts and 28 transactions. None hit
the 100-transaction projection limit, so truncation has only been tested on
synthetic data.

Synthetic results are committed in
`evaluations/ai/summary_prompt_comparison.csv`. Real cases and results stay
under the Git-ignored `artifacts/aws-smoke/ai-eval/`.

### Reproduce

```bash
# One summary (default: the checked-in synthetic example). Billed Bedrock call.
.venv/bin/python scripts/demo_ai_summary.py
.venv/bin/python scripts/demo_ai_summary.py --input artifacts/aws-smoke/ai-eval/q00002/llm_input.real.json

# Compare prompt versions on the 7 synthetic cases (writes the committed CSV).
.venv/bin/python scripts/evaluate_ai_summary.py --prompts summary-v3 summary-v4

# Fetch 10 real cases from Athena (21 queries, about 3.1 GB scanned), then compare.
.venv/bin/python scripts/fetch_real_ai_eval_cases.py
.venv/bin/python scripts/evaluate_ai_summary.py --cases-dir artifacts/aws-smoke/ai-eval --prompts summary-v4

# Unit tests: no AWS and no paid calls (a scripted client stands in for Bedrock).
.venv/bin/python -m pytest tests -q
```

Run `pytest tests`, not a bare `pytest` from the repository root: the root
`test_athena.py` and `tests/unit/test_athena.py` share a module name and stop
collection.

## Cost

- One summary is about 5,000 to 8,000 input tokens and 300 to 400 output
  tokens per attempt; the tool schema adds about 1,000 of the input tokens.
- All testing on 18 October used about 1.3 million input and 60,000 output
  Nova tokens. Bedrock charges did not yet appear in Cost Explorer on that
  day; check them before quoting a figure.
- Fetching the 10 real cases scanned 3.11 GB in Athena, about US$0.02.
- The AI layer's budget is US$10 for now. Nothing enforces it yet.

## Safety rules

- The LLM never queries AWS. It receives one bounded CaseContext per request.
- Ground-truth fields (`evaluation_is_laundering`, `data_split`, pattern
  labels) are excluded by the data layer and rejected by its validator.
- `risk_score` is a ranking signal, never a probability; the prompt forbids
  restating it as one, and the number check rejects percentages.
- Never commit anything under `artifacts/aws-smoke/`. Use only synthetic
  fixtures in tests, examples, screenshots and shared prompts.

## Open issues

1. **Streamlit panel.** Not built. It needs one agreed `app.py` (the repository
   app and Owen's `aml_ui` app overlap).
2. **Connected-account ranking.** The review order is CaseContext's provisional
   rule (supporting transfers, then closest gap, then account key). Owen's
   dashboard ranks connected accounts by BQ1 risk rank instead. The team needs
   to pick one so the dashboard and Copilot agree.
3. **BQ1 explanations.** Owen built `bq1_explanations_top1pct.csv` (top 3
   reasons for the 62,504 test-queue alerts), but CaseContext still sets
   `model_explanations.status = "unavailable"`, so the Copilot gives no
   reasons. Integrating it needs the BQ1 owner to verify the file, then a
   CaseContext change by the data-layer owner.
4. **Global rank in summaries.** The summary reports BQ1's global
   `risk_rank`. The first alert in the test queue shows as rank 58, because
   training, validation and tail rows rank above it globally. The UI should
   show the test-queue position beside it.
5. **Digits.** Nova sometimes writes counts as words despite the prompt and
   the retry. This is cosmetic; a deterministic word-to-digit pass before
   display would remove it.

## Next steps

1. Case-aware chat, reusing the same client, validator and scope rules.
2. A budget guard (token count per session) and per-case result caching.
3. The Streamlit Copilot panel, once the app question is settled.
4. A truncated real case (more than 100 transactions) in the real evaluation set.
