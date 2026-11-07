# IS459 AML: agent plan

Owner: Claire (agent). Last updated 7 Nov 2026. Team split: `WORK_SPLIT.md` (deleted from the working folder but still in git: `git show HEAD:WORK_SPLIT.md`). ETL and real-time details: `ETL_v2_Handover.docx` (kept outside this repo).

**Status:** the ETL and real-time pipelines are finished. Agent data access, the output format, S1, the LLM loop, the batch runner and `investigate()` are built, and 424 unit tests pass. The current rules (D27, D28) have only run on fake data in tests, not on AWS or the real LLM. The saved 14 Sep batch (F29) predates them, so its rows are out of date.

## Where to continue

Changes made on 7 Nov, in commit `451b0c8` on `feat/agent`: the use case is now triage; promotions and the merge verdict are gone; the batch has an end-of-day cutoff; no payments means `not_sure`; results list every case member; the review list is simpler; the Copilot and old `app.py` are deleted; the README covers the whole project.

1. **Commit the BQ2 restore.** `src/bq2/evidence.py`, `transforms.py` and their tests are back but not committed yet.
2. **Tell the team.** Person 2: the contract changed (`docs/agent_output_format.md`: new `members` field, no `promoted_txn_ids`). Person 4: the evaluation changed (section 13). Don't use the saved 14 Sep rows.
3. **Check billing.** Confirm whether credits cover Sonnet (section 15).
4. **Rerun the sample** (about US$1.50): `python -m scripts.run_agent_sample --day 2022-09-14 --limit 20 --out <file>`. Check the share of cases raised against F29's 92%, plus the valid, repair and fallback rates.
5. **If it still raises nearly everything,** tighten the prompt in `src/agent/prompts.py`, so only stronger evidence raises a case, and rerun the sample.
6. **Run the batch** on 14, 15 and 16 Sep with `--save` (step 5 in section 19), and hand the run IDs to Person 4.
7. **Then build the new-day path** (step 6), including the risks in section 15.

Keep this file short: current facts and decisions only. Section, finding and decision numbers have gaps because code comments cite them, so don't renumber them. Git history has everything removed.

## 1. Facts

- **Dataset:** IBM AML LI-Medium, synthetic. Don't claim real-world detection performance.
- **AWS:** us-east-1, account 941377154820, bucket `s3://is459-aml-g1t5/`, IAM user `g1team5`. About US$220 of US$250 left on 6 Nov.
- **BQ1:** frozen XGBoost 3.2.0, iterations 0-96, 26 features. Test days are 14-16 Sep 2022. Recall@1% 0.634, precision@1% about 3.1%. `risk_score` ranks; it is not a probability.
- **BQ2:** one-hop, directed edges, window 6-72 h, default 48 h. Upstream = payments into the alert's sender before it. Downstream = payments out of its receiver after it.
- **Gold tables** (`is459_aml_gold`, partitioned by `txn_date`): `bq1_risk_scores_medium_v2`, `bq1_features_medium_v2`, `bq2_graph_edges_medium_v2`. All contain labels.
- **Agent tables** (label-free, `agent/tables/`, read by the tools): `agent_scores_v2`, `agent_edges_v2`, `agent_day_queue_v2`. Built by `sql/agent/01-06`.
- **Demo alert:** `txn_a0f703e20da6f9bf0bdad4c0d62862de`, 14 Sep 10:57, day rank 2, has reasons, complete 48 h graph (22 neighbours).
- **Commands:** tests `.venv/bin/python -m pytest tests` (bare `pytest` fails on a name clash). Tools `python -m scripts.smoke_agent_tools`. S1 `python -m scripts.run_s1 --day <day> --out <file> [--compare-to <earlier file>]`.

## 2. Hard rules

1. **Read-only tools.** The agent writes only to its decisions and cases tables and its trace prefix. It doesn't touch ETL, Silver, Gold or v1.
2. **No labels.** Label-free tables, a column allowlist and a test that fails on a label. `data_split` is banned too. Only the evaluation module reads labels, and the agent never imports it.
3. **Never remove an alert from the BQ1 queue.** The agent only produces a second, reordered list.
4. **All numbers and IDs come from tools.** The LLM picks tools, verdicts and claim codes. Code checks every cited ID and writes the displayed sentences.
5. **Currency-safe.** Never sum across currencies.
6. **Bounded.** At most 5 tool steps per run, plus token, timeout and global spend caps.
7. **Reasons are model explanations, not proof.**

## 3. Pipeline

```
DAILY      raw CSV → Step Functions is459-etl-v2-silver-wap → Gold
           (parse → audit + 11 quality rules → publish Silver → BQ1 and BQ2 in parallel → Gold commit)
           success message → SQS is459-etl-v2-day-ready
           → agent tables rebuilt → case grouping → agent per case → DynamoDB + S3 traces → Streamlit
           Analyst's Investigate button → same agent on one alert → Streamlit

REAL-TIME  Kinesis → Lambda is459-rt-scorer (same model, 26 features)
           → is459_rt_alerts at score >= 0.3605 → is459-rt-explainer → is459_rt_briefs → Streamlit
           Display only: no agent (D17). A prototype proven on replay slices.
```

The agent answers: *is this alert worth an analyst's time, and what else belongs with it?* It combines the BQ1 alert, BQ2 money flows and BQ1 scores on those neighbours. The use case is triage: raise the alerts that matter, with evidence, so the analyst needn't gather it by hand. The test, not a claim: raised alerts are more often laundering than alerts not raised.

## 4. Triggers

- **B (batch):** top 100 alerts of a day, grouped into cases, seeing data only up to the end of that day (D28). Run with `run_batch --day`, or from the queue message for a new day.
- **C (on demand):** `investigate()` inside Streamlit. Doesn't write decisions by default. No cutoff, so it can differ from the batch (D28).
- **A (real-time):** out of scope (D17).

## 5. Tools

All are in `src/agent/tools.py`, and every result has an `ids` list.

- `get_alert(txn_id, txn_date)`: score, day rank, reasons or null, payment details.
- `get_case_graph(alert, window_h=48)`: up to 50 edges per direction, counts and totals per currency, `no_graph`, `window_truncated`, `edges_capped`. An optional `as_of` hides later payments.
- `get_neighbour_scores(txn_ids, txn_dates)`: at most 200 IDs. Dates outside the scored days return `scored: false`.
- `get_related_alerts(accounts, anchor_ts, window_h)`: other top-K alerts on the same accounts. An optional `as_of` hides later alerts.

**5.1 Tables:** `v_agent_scores` and `v_agent_edges` are the label-free sources for the agent tables. **5.2** Tools cache by SQL text, so repeats cost nothing.

## 6. Agent loop (built: `src/agent/llm.py`, `prompts.py`, `validate.py`, `loop.py`)

```
seed = get_alert(...)   # by code
for step in 1..5: LLM sees policy + seed + prior results, returns a tool call or submit_decision
invalid after 1 repair, or no decision by step 5 → S1 decision, fallback=true
```

Bedrock Converse with native tool calling, temperature 0. Log steps, tokens, latency and validation outcome.

**6.2 Policy prompt (short):** triage assistant with read-only tools and no labels; decide only from tool results and cite IDs; write no numbers. Guide, not hard rules:
Three verdicts (D27): `raise`, `dont_raise`, `not_sure`. Code groups related alerts, so there is no merge verdict.
- Graph supports the reasons, or several neighbours rank high → `raise`.
- Payments exist but don't support the reasons → `dont_raise`.
- No graph, or not enough evidence either way → `not_sure`.
- Truncated window → say so. Truncation alone is no reason to avoid a verdict.

**6.3 Output and validation:** the `Decision` type in `src/agent/contracts.py`. Code must check:
1. The JSON matches the schema, and the verdict and claim codes are valid.
2. Every `evidence_ids` member appears in a tool result's `ids` from this run. Code sets `case_id` (D25).
3. `dont_raise` is rejected when every graph fetched has no payments.
4. `note` is at most 200 characters, with digits only if they appear in tool results.
5. `NO_GRAPH_AVAILABLE`, `TRUNCATED_WINDOW` and `NO_REASONS_AVAILABLE` match the tool flags.

## 7. Cases and review list (built)

**7.1** `src/agent/cases.py`: link alerts that share an account within the window; cap of 20; best rank is the representative.

**7.2** `src/agent/review_list.py`: `raise` cases first, then the rest by rank, then `dont_raise` alerts. No alert leaves the list.

**7.3** Each result has `members`: every alert in the case with its payments. Code fetches any graph the LLM skipped, after the decision, so these never reach the LLM.

## 8. S1 baseline (built)

`src/agent/baseline.py`, same tools and fixed rules, first match wins:
1. **raise:** at least 2 high-ranked transactions (members, plus neighbours in the top 500).
2. **not_sure:** anything else, including no payments to check.

S1 has no `dont_raise` rule, so only the LLM can move an alert down.

S0 = BQ1 order. S1 = these rules. S2 = LLM agent.

## 9. LLM

`AGENT_MODEL_ID` is Sonnet 4.5, `us.anthropic.claude-sonnet-4-5-20250929-v1:0` (D23). Backup: Nova 2 Lite, `us.amazon.nova-2-lite-v1:0`. Measure tokens and cost on the first 20 alerts.

## 10. Storage

- `is459_agent_decisions` (holds the outdated 14 Sep run, F29): key `transaction_id`, rows as in `DecisionRow`.
- `is459_agent_cases` (created 8 Nov, on-demand, tagged `project=is459-g1t5-etl-v2`): key `day` + `case_id`. One item per case, singles included (`CaseRow` in `contracts.py`).
- Traces: `s3://is459-aml-g1t5/agent/traces/<day>/<txn_id>.json`.
- A batch rerun writes the new rows first, then deletes that day's rows it did not rewrite (scan on `day`, no index). If any case failed, nothing is written or deleted. Each alert is in one case, so each transaction gets one row.

## 11. Streamlit

Owned by Person 2. Contract: `docs/agent_output_format.md`. The app must say full days end on 16 Sep 2022, reasons are not proof, and the data is synthetic. It reads DynamoDB, never SQS.

## 12. Milestones

| # | Milestone | Owner | Status |
|---|---|---|---|
| 1 | Data access | Claire | Done |
| - | Output format | Claire | Done |
| 2 | Case grouping + S1 | Claire | Done |
| 3 | Evaluation harness | Person 4 | To do |
| 4 | LLM loop + validator | Claire | Built, rules changed 7 Nov. 20-case rerun next |
| 5 | Batch runner, `investigate()` | Claire | Built, rules changed 7 Nov. 14-16 Sep runs to do (F29 is out of date) |
| 6 | Streamlit | Person 2 | To do |
| 7 | Full evaluation | Person 4 | To do |
| 8 | New-day path, queue consumer | Claire | To do; required for the recorded run |

## 13. Evaluation (Person 4)

- **Days:** 14-16 Sep only, test rows only, reported per day. Every day is seen as of its own end (D28). Anything reading Gold must filter to these dates.
- **Budget:** K = 100, because the batch reviews the top 100. Recall@K alone can't measure triage, because reordering inside K doesn't change it.
- **Main test:** laundering rate by verdict (`raise`, `not_sure`, `dont_raise`) against the top-100 average, and how many `dont_raise` alerts were actually laundering. Report counts with rates: 3 days is about 230 cases.
- **Also:** share of cases raised, review items per case, valid/repair/fallback/rejected-citation rates, cost and latency, S1-S2 agreement.
- **Repeatability:** run a 50-alert subset 3 times.
- **Honesty:** if S2 doesn't beat S1, report that, and what the LLM adds in traces and flexibility.

## 15. Risks and open items

- **Sonnet billing:** check whether Anthropic usage is billed through Marketplace and covered by credits before running beyond 20 alerts. It wasn't visible in Cost Explorer on 7 Nov.
- **Where the batch runs:** undecided. A Lambda that starts a Glue job doesn't fit "delete the message only on success", because a Lambda SQS trigger removes the message when the Lambda returns. Glue Python shell may also be too old, as `contracts.py` needs 3.10+ (check this). Suggested for the one recorded run: a simple poller on a laptop or small EC2 instance. CloudShell may time out when idle.
- **Queue timing:** the visibility timeout is 15 minutes (F20), but the batch takes about 11 minutes plus the rebuild. The message can reappear mid-run and start a duplicate, so the consumer must keep extending the timeout while it works.
- **Silent fallback:** Bedrock errors and the spend cap become S1 decisions, not failures, so an outage could still publish as S2. Consider blocking `publish` above a set fallback rate.
- **Missing sibling payments:** the graph leaves out the sender's other outgoing payments and the receiver's other incoming ones, which matter for fan-out and fan-in. `agent_edges_v2` already has both directions, so adding them should be a small change. Not started.
- **Over-raising:** 72 of 78 cases moved up on 14 Sep (F29), so the agent may barely filter. If the rerun looks the same, tighten the prompt so only stronger evidence raises a case.
- **`dont_raise`:** these alerts may be one-off transactions rather than harmless.
- **S1 as a comparison:** it never says `dont_raise`, so compare it with S2 as raised against not raised.
- **18 Sep run:** can happen only once, so record from the start, and rehearse the whole path first on a test message. 19 Sep (244 transactions) is the backup. It's too small and has no reasons, so it isn't a result. With about 350 transactions, every same-day neighbour is inside the top 500, so expect mostly `raise`.
- **To confirm:** D24, D25.

## 16. Findings still in force

- **F2** Labels are in the scores, edge and features tables.
- **F3** v1 = v2 on 14-16 Sep. v2 edges for 1-13 Sep point at v1's S3 folders.
- **F4** 14 Sep 00:00-05:37 is `validation` (488,561 rows). Queues and evaluation use test rows only.
- **F6** Data effectively ends 16 Sep. With the end-of-day cutoff (D28), nearly every batch alert is truncated downstream on every day, so truncation is normal.
- **F7** Neighbours before 14 Sep have no v2 score.
- **F8** `build_alert_lookup_query` has no date filter (0.9 GB), so don't use it. The v2 edges have no self-transfers.
- **F9** Workgroup `primary`. Always set the output to `s3://is459-aml-g1t5/athena-results/agent/`.
- **F13** Random IDs defeat Parquet pruning, and bucketing fixes it. Athena can't insert into bucketed tables, so adding a day means a rebuild (D15).
- **F14** About 2% of scored transactions are self-transfers: `amount` is null, with no graph.
- **F16** All of each day's top 5,000 have reasons on 14-16 Sep.
- **F18** Later days are tiny: 17 Sep 380 scores (committed), 18 Sep 353, 19 Sep 244, down to 1-16 a day by 23-27 Sep. Committed days can't rerun, and the next loadable day is 18 Sep.
- **F20** `is459-etl-v2-day-ready`: raw event, 15-minute visibility, dead-letter queue after 5 receives. The day is in `json.loads(detail.input)["day"]`.
- **F21** `is459_rt_alerts` (key `txn`) has no reasons, and `amount` is a string. `is459_rt_briefs` (key `txn`) holds the narrative, next checks and typology. Typology macro-F1 is 0.345, so don't lean on it.
- **F22** New days have `data_split = 'excluded'`, but `v_agent_scores` ranks only `test` rows, so a new day has an empty queue until fixed (step 6).
- **F23** New days have no reasons.
- **F24** `is459-agent-readonly` misses the agent tables, S3 `agent/`, `is459_agent_cases`, `BatchGetItem` and Bedrock, and allows labelled tables. Irrelevant while running as `g1team5`.
- **F26** (old rules: names predate D27, and it had promotions and no cutoff) S1 on 15 Sep: 100 alerts → 77 cases. need_more 36, escalate 30, deprioritise 7, promote 4. All low confidence (F6). 5.6 GB, about US$0.03, about 12 minutes. Two runs matched. No labels used yet.

## 17. Decisions in force

| ID | Decision |
|---|---|
| D2 | Agent code lives in `src/agent/`. |
| D3 | `AGENT_*` config constants are separate from v1's. `BQ1_SCORE_TABLE` stays for `src/bq1`. |
| D4 | No separate IAM role for labels; rule 2's controls apply instead. |
| D5 | Reasons come from Owen's CSV, valid for the 14-16 Sep top 1%. |
| D6 | `risk_rank_day` ranks test rows only (step 6 extends it to new days). |
| D7 | Neighbours outside the scored days are `scored: false`, with no v1 fallback. |
| D8, D13 | Tools read three materialised agent tables, not the views. |
| D10 | At most 50 edges per direction go to the LLM; totals use all edges. |
| D11 | `get_alert` needs the date, to avoid scanning every partition. |
| D14 | `data_split` never reaches the agent; tools expose `in_queue`. |
| D15 | Adding a day rebuilds the agent tables and widens the dates in config (about 2.7 GB). |
| D16 | Reuse `is459_agent_decisions`, keyed on `transaction_id`, with `day` as an attribute. |
| D17 | Real-time alerts are display only. |
| D18 | Results come from 14-16 Sep only. The demo is recorded only, as a setup pipeline and a run pipeline. The run follows 18 Sep end to end. There is no live demo. |
| D19 | The batch reads the existing queue; no new EventBridge rule. |
| D20 | Tools don't use JX's `is459_agent` views. |
| D21 | The AI Copilot (`src/ai`, `src/context`) and old `app.py` were removed on 7 Nov and remain in git. `src/bq1`, `src/shared`, `src/bq2/evidence.py` and `src/bq2/transforms.py` are kept but the agent doesn't use them. |
| D22 | Team split in `WORK_SPLIT.md` (other owners TBC). |
| D23 | Sonnet 4.5 first; Nova 2 Lite as backup. |
| D24 | Full row for the representative, short pointer rows for other members. *To confirm.* |
| D25 | Code sets `case_id`. `investigate(txn_id, txn_date, window_h)`. *To confirm.* |
| D26 | S1 order is raise, not_sure. High-ranked = member or top-500 neighbour. No graph means low confidence. Truncation caps the LLM's confidence at medium. |
| D27 | Verdicts are `raise`, `dont_raise`, `not_sure`. `merge_into_case`, `promote` and `promoted_txn_ids` are gone: code groups related alerts, and the agent judges only the top 100. No graph means `not_sure`. |
| D28 | The batch and S1 see data only up to the end of the alert's day, because that is all a daily run knows. `investigate()` has no cutoff, because an analyst clicks later. |

- **F27** (superseded by F29) LLM sample, 20 cases on 14 Sep, Sonnet 4.5, run once with a validator bug: 12 valid, 6 repaired, 2 fallback. About 17k tokens in, 1.5k out and 38 s per run, about US$0.075 per run (list price). The bug kept only the last `get_related_alerts` call, so correct citations were rejected and both fallbacks followed. It is fixed and tested, but not rerun, so these rates are too pessimistic. Verdicts: merge_into_case 16, escalate 3, deprioritise 1, promote 0. Confidence high in 13 (14 Sep has complete graphs). Saved in a temporary folder, so it may disappear: `/private/tmp/claude-501/-Users-claire-big-data-bq1-repo/b2eb27de-ce80-4d1d-a2a6-7d6b874c44b8/scratchpad/sample_20.json`.

- **F28** `investigate()` on the demo alert (real run): valid, 4 steps, 23.6k tokens in, 46 s, graph 8 in and 14 out. It is a single alert, so `case_id` is null even when the verdict is `merge_into_case`, and its verdict may differ from the batch's, where the same alert sits in a case. The UI should not assume a `raise` has a `case_id`.

- **F29** 14 Sep batch, run `batch-2022-09-14-20261107T034826Z`, saved to DynamoDB and S3 (100 decision rows: 78 representatives and 22 members; 78 case rows; 78 traces; no collisions). 78 cases, 0 errored. Valid 66 (85%), repaired 12 (15%), fallback 0, so the F27 validator fix worked. About 16k tokens in and 1.4k out per run, 34 s per run, 11 minutes, US$5.43 estimated, 5.5 GB Athena. Verdicts: merge_into_case 54, escalate 18, deprioritise 3, need_more 3, promote 0, so 72 of 78 cases (92%) move up. Merge was used for 37 of the 58 single-alert cases, where nothing can be merged, so the label carries little. Only 7 cases had a neighbour ranked 501-5000, the rest were far lower (median about 977,000), and none was promoted. S1 promotes in that band. Old verdict names (D27). The saved 14 Sep rows still use them and could see later days, so rerun 14 Sep before Person 4 uses them. Local copy, in a temporary folder that may disappear: `/private/tmp/claude-501/-Users-claire-big-data-bq1-repo/b2eb27de-ce80-4d1d-a2a6-7d6b874c44b8/scratchpad/batch_0914.json`.

## 18. Data access (built)

`src/agent/`: `sql.py` (builders, allowlist, label guard), `data.py` (runner, cache, scan log), `tools.py`, `contracts.py`. Three test alerts scanned 224 MB in total, at most 105 MB per call, and edges match the BQ2 query at 6, 48 and 72 h.

## 19. Remaining agent work (Claire)

Steps 1-4 are built and tested (424 tests). File names for later steps are proposals.

**Step 3: LLM loop and validator (milestone 4), built**
- Files: `llm.py`, `prompts.py`, `validate.py`, `loop.py`, and `scripts/run_agent_sample.py` for the sample run.
- Choices beyond section 6: the 5th step is forced to be `submit_decision`; the repair is one extra reply outside the 5; the model may only use IDs a result already showed; code adds missing caveat claims (no graph, truncated, no reasons), sets confidence to low without a graph, and caps it at medium when truncated. Caps are in `config.AGENT_*`: 120k tokens and 150 s per run, US$10 spend per process.
- Left: rerun `python -m scripts.run_agent_sample --day 2022-09-14 --limit 20 --out <file>` (about US$1.5, 13 minutes) and record the rates as a new finding.

**Step 4: Batch runner and `investigate()` (milestone 5), built**
- `src/agent/batch.py` (`run_day` computes, `publish` saves), `store.py` (DynamoDB and S3), `investigate.py`, and `report.py`. CLIs: `scripts/run_batch.py --day <day> [--save]` (dry run unless `--save`), `create_agent_cases_table.py`, `rebuild_tables.py` (dry run unless `--yes`; refuses to delete outside `agent/tables/`, and fails if a day's ranks changed). Concurrency of 4, on a shared cache that is now thread-safe.
- Checked: unit tests with in-memory stores, plus `publish` against the real tables with a fake day, `2099-01-01`: a rerun gave identical keys, a smaller rerun deleted exactly the stale rows, cleanup left nothing.
- Not done: `investigate()` has no save option (a save would overwrite the batch row for that alert). `rebuild_tables.py --yes` has not been run for real.

**Step 5: Run the batch on 14-16 Sep**
- 14 Sep ran under the old rules (F29), so rerun it, then run 15 and 16 Sep. About US$5.50 and 11 minutes a day. First confirm the Sonnet billing item in section 15.
- Pass run IDs to Person 4, fix what the evaluation shows, and rerun. Reruns replace the day's rows.
- Don't start step 6 until the results are saved.

**Step 6: New-day path (milestone 8)**
- Rank `excluded` rows too (F22), and check that 14-16 Sep ranks stay identical.
- Widen `sql/agent/04-06`, `AGENT_SCORED_DATES` and `AGENT_DATA_HORIZON`.
- Consumer for `is459-etl-v2-day-ready`: rebuild, run the batch, delete the message only on success (F20).
- Check that runs with no reasons still give valid output (F23). Test with a fake message.
- Done when a test message starts the batch with no manual step.

**Step 7: Recordings (no live demo)**
- Record the setup pipeline and the run pipeline.
- For the one-time 18 Sep run, be present with Person 3 and confirm the consumer is running first.
