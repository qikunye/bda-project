# IS459 AML: agent plan

Owner: Claire (agent and evaluation). Last updated 7 Nov 2026. Team split: `WORK_SPLIT.md` (deleted from the working folder but still in git: `git show HEAD:WORK_SPLIT.md`). ETL and real-time details: `ETL_v2_Handover.docx` (kept outside this repo).

**Status:** the ETL and real-time pipelines are finished. The agent is built as a triage tool: it gives each case a verdict of `raise`, `dont_raise` or `not_sure`. Its rules are frozen in commit `91e344e` (not pushed), and 435 unit tests pass. Tests on 14 Sep (F33, F34) showed the verdict mostly repeats BQ1, so we propose turning the agent into an investigation assistant (section 3). Nothing from the current rules is saved yet. DynamoDB holds only out-of-date 14 Sep rows (F29), and no one has looked at labels.

## Where to continue

1. **Agree the change with the team (D31, proposed).** Person 2 builds the app against the output format, so they must agree before any code changes. Check that the timeline allows it. If not, fall back to the frozen triage version: run its batch, and evaluate `raise` against `not_sure`.
2. **Write the new output format** in `docs/agent_output_format.md` (section 6.3), and get Person 2's agreement.
3. **Extend the tools** (section 5, proposed fields): hours observed, the sender's and receiver's other payments, and money returning to its source.
4. **Build S1 as a brief from code alone** (section 8). It finds the same patterns by fixed rules, with no LLM.
5. **Build the LLM brief (S2):** prompt, contract, validator and claim rendering (section 6). The review list keeps BQ1's order (section 7.2).
6. **Fix the `nan` reason sentences** (F34) in the reasons data, or hide them in the app.
7. **Do a dry run on 14 Sep, then freeze** before anyone looks at labels. Then run 14, 15 and 16 Sep with `--save` (about US$12) and record the run IDs here.
8. **Evaluation** (section 13), then the **new-day path** and **recordings** (section 19).

Keep this file short: current facts and decisions only. Section, finding and decision numbers have gaps because code comments cite them, so don't renumber them. Git history has everything removed.

## 1. Facts

- **Dataset:** IBM AML LI-Medium, synthetic. Don't claim real-world detection performance.
- **Patterns file:** `s3://is459-aml-g1t5/bronze/ibm-aml/li-medium/patterns/LI-Medium_Patterns.txt` lists the laundering attempts and their types. It is ground truth, so only the evaluation may read it.
- **AWS:** us-east-1, account 941377154820, bucket `s3://is459-aml-g1t5/`, IAM user `g1team5`. About US$210 of US$250 credit left on 7 Nov (estimated). Sonnet billing is confirmed. Cost Explorer shows Sonnet charges late, so use the run's own spend estimate.
- **BQ1:** frozen XGBoost 3.2.0, iterations 0-96, 26 features. Test days are 14-16 Sep 2022. Recall@1% 0.634, precision@1% about 3.1%. `risk_score` ranks; it is not a probability.
- **BQ2:** one-hop, directed edges, window 6-72 h, default 48 h. Upstream = payments into the alert's sender before it. Downstream = payments out of its receiver after it.
- **Gold tables** (`is459_aml_gold`, partitioned by `txn_date`): `bq1_risk_scores_medium_v2`, `bq1_features_medium_v2`, `bq2_graph_edges_medium_v2`. All contain labels.
- **Agent tables** (label-free, `agent/tables/`, read by the tools): `agent_scores_v2`, `agent_edges_v2`, `agent_day_queue_v2`. Built by `sql/agent/01-06`.
- **Demo alert:** `txn_a0f703e20da6f9bf0bdad4c0d62862de`, 14 Sep 10:57, day rank 2, has reasons, complete 48 h graph (22 neighbours).
- **Cost:** about US$0.05 per case with Sonnet 4.5; a day's top 100 alerts (about 78 cases) costs about US$4 and takes about 10 minutes (F33).
- **Commands:** tests `.venv/bin/python -m pytest tests` (bare `pytest` fails on a name clash). Tools `python -m scripts.smoke_agent_tools`. S1 `python -m scripts.run_s1 --day <day> --out <file>`. LLM sample `python -m scripts.run_agent_sample --day <day> --limit 20 --out <file>`. Batch `python -m scripts.run_batch --day <day> --out <file>` is a dry run; add `--save` to write.

## 2. Hard rules

1. **Read-only tools.** The agent writes only to its decisions and cases tables and its trace prefix. It doesn't touch ETL, Silver, Gold or v1.
2. **No labels.** Label-free tables, a column allowlist and a test that fails on a label. `data_split` and the patterns file are banned too. Only the evaluation module reads labels, and the agent never imports it.
3. **Never remove an alert from the BQ1 queue.** BQ1 decides the order; the agent adds evidence.
4. **All numbers and IDs come from tools.** The LLM picks tools and finding codes, and writes text with no numbers. Code checks every cited ID and writes the displayed sentences.
5. **Currency-safe.** Never sum across currencies.
6. **Bounded.** At most 5 tool steps per run, plus token, timeout and global spend caps.
7. **Reasons are model explanations, not proof.**

## 3. Pipeline and purpose

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

**Purpose (proposed, D31).** BQ1 already decides which alerts an analyst sees. The agent helps investigate them: for each alert or case, it gathers the BQ2 money flow and BQ1 scores around it into a short brief, so the analyst doesn't have to compare BQ1 and BQ2 by hand. A brief has:
- **Money flow:** where the money came from, where it went, how quickly, and how much stayed put. Code fills in all amounts, per currency.
- **Findings:** patterns, each citing the payments behind it. These are pass-through, fan-out, fan-in, money returning to its source, high-ranked neighbours, and repeat alerts on the same accounts.
- **Gaps:** no graph, a short observed time, or no reasons.
- **Next checks:** what the analyst should look at next, from a fixed list.
- **Summary:** one short sentence from the LLM, with no numbers.

The current code still produces triage verdicts, as described in D27-D30.

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

**5.3 Proposed additions (D31):**
- `hours_observed_after_alert` on `get_case_graph`: from the alert to the end of visible data, capped at the window. "No onward payment after N hours" then becomes evidence, not just an unknown (F34).
- **Sibling payments:** the sender's other outgoing and the receiver's other incoming payments, for fan-out and fan-in. `agent_edges_v2` already has both directions.
- **Money returning to its source:** flag when a downstream payment goes to an account that appears upstream.

## 6. Agent loop (built: `src/agent/llm.py`, `prompts.py`, `validate.py`, `loop.py`)

```
seed = get_alert(...)   # by code
for step in 1..5: LLM sees policy + seed + prior results, returns a tool call or submit
invalid after 1 repair, or no answer by step 5 → S1 result, fallback=true
```

Bedrock Converse with native tool calling, temperature 0. Log steps, tokens, latency and validation outcome. The 5th step must be the submit call. A repair is one extra reply outside the 5.

**6.2 Prompt (proposed):** an investigation assistant with read-only tools and no labels. Gather the flow around the alert, report the patterns the results show, name the gaps, and pick the next checks. Cite IDs, and write no numbers.

**6.3 Output and validation.** The current `Decision` type in `src/agent/contracts.py` holds a verdict. The proposal replaces it with a brief: `findings` (code + `evidence_ids`), `gaps`, `next_checks` and `summary`. Code must check:
1. The JSON matches the schema, and every code is valid.
2. Every `evidence_ids` member appears in a tool result's `ids` from this run. Code sets `case_id` (D25).
3. Each finding is backed by its tool results. For example, a pass-through must cite payments listed by `get_case_graph`.
4. `summary` is at most 200 characters, with no digits or spelled-out numbers.
5. `NO_GRAPH_AVAILABLE`, `TRUNCATED_WINDOW` and `NO_REASONS_AVAILABLE` match the tool flags.

## 7. Cases and review list (built)

**7.1** `src/agent/cases.py`: link alerts that share an account within the window; cap of 20; best rank is the representative.

**7.2** `src/agent/review_list.py` currently orders by verdict. Proposed: keep BQ1's order, and attach the brief to each alert.

**7.3** Each result has `members`: every alert in the case with its payments. Code fetches any graph the LLM skipped, after it answers, so these never reach the LLM.

## 8. S1 baseline (built)

`src/agent/baseline.py`, same tools, fixed rules. It is currently a triage rule: `raise` with at least 2 high-ranked transactions (members plus neighbours in the top 500), otherwise `not_sure`. It is also the LLM's fallback.

Proposed: S1 becomes a brief built by code alone. It finds the same patterns by fixed thresholds. The evaluation then shows what the LLM adds over code.

S0 = BQ1 order with no brief. S1 = code-only brief. S2 = LLM brief.

## 9. LLM

`AGENT_MODEL_ID` is Sonnet 4.5, `us.anthropic.claude-sonnet-4-5-20250929-v1:0` (D23). Backup: Nova 2 Lite, `us.amazon.nova-2-lite-v1:0`.

## 10. Storage

- `is459_agent_decisions`: key `transaction_id`, rows as in `DecisionRow`. Holds only the out-of-date 14 Sep run (F29).
- `is459_agent_cases` (on-demand, tagged `project=is459-g1t5-etl-v2`): key `day` + `case_id`. One item per case, singles included (`CaseRow` in `contracts.py`).
- Traces: `s3://is459-aml-g1t5/agent/traces/<day>/<txn_id>.json`.
- A batch rerun writes the new rows first, then deletes that day's rows it did not rewrite (scan on `day`, no index). If any case failed, nothing is written or deleted. Each alert is in one case, so each transaction gets one row.
- The brief changes the row contents, not the tables or keys.

## 11. Streamlit

Owned by Person 2. Contract: `docs/agent_output_format.md` (changes with D31). The app must say full days end on 16 Sep 2022, reasons are not proof, and the data is synthetic. It reads DynamoDB, never SQS. Don't present `REASONS_CORROBORATED` as strong evidence (F34).

## 12. Milestones

| # | Milestone | Owner | Status |
|---|---|---|---|
| 1 | Data access | Claire | Done |
| 2 | Case grouping + S1 | Claire | Done (triage rules); code-only brief proposed |
| 3 | Evaluation harness | Claire | To do; waits on D31 |
| 4 | LLM loop + validator | Claire | Done for triage (`91e344e`); brief version proposed |
| 5 | Batch runner, `investigate()` | Claire | Done; saved 14-16 Sep runs wait on D31 |
| 6 | Streamlit | Person 2 | To do; output format changes with D31 |
| 7 | Full evaluation | Claire | To do |
| 8 | New-day path, queue consumer | Claire | To do; required for the recorded run |

## 13. Evaluation (Claire, proposed for D31)

- **Days:** 14-16 Sep only, test rows only, top 100 alerts per day, reported per day. Every day is seen as of its own end (D28). Anything reading Gold must filter to these dates.
- **Labels only after the freeze.** Tune without labels, freeze in a commit, then evaluate. Report any later change separately, as tuned after the results.
- **Are the findings accurate?** Every finding must cite tool results. Report the rejected-citation, repair and fallback rates.
- **Do the clues carry information?** For each finding code, compare the laundering rate of alerts with it and without it. Report counts with rates: 3 days is about 300 alerts.
- **Does it name the right pattern?** For alerts in a laundering attempt from the patterns file, compare the agent's pattern findings with the attempt's type. For reference, the real-time explainer's typology macro-F1 is 0.345 (F21).
- **What does the LLM add?** Compare S2 with S1 on the measures above. If the LLM adds nothing over code, report that.
- **Also:** cost and latency, and repeatability: run a 50-alert subset 3 times.
- **Not measurable here:** time saved for analysts. Show example briefs instead.

## 15. Risks and open items

- **Timeline:** the change touches the output format, the prompt, the validator, S1 and the evaluation. If time is short, fall back to the frozen triage version (`91e344e`).
- **Code may do most of the work:** many patterns can be found by fixed rules. If S2 adds little over S1, the LLM's value is the summary and next checks, so say so.
- **Patterns file fit:** attempts may span days or accounts outside the top 100, so only some alerts will match an attempt. Check coverage before relying on it.
- **Where the batch runs:** undecided. A Lambda that starts a Glue job doesn't fit "delete the message only on success", because a Lambda SQS trigger removes the message when the Lambda returns. Glue Python shell may also be too old, as `contracts.py` needs 3.10+ (check this). Suggested for the one recorded run: a simple poller on a laptop or small EC2 instance. CloudShell may time out when idle.
- **Queue timing:** the visibility timeout is 15 minutes (F20), but the batch takes about 10 minutes plus the rebuild. The message can reappear mid-run and start a duplicate, so the consumer must keep extending the timeout while it works.
- **Silent fallback:** Bedrock errors and the spend cap become S1 results, not failures, so an outage could still publish as S2. Consider blocking `publish` above a set fallback rate.
- **18 Sep run:** can happen only once, so record from the start, and rehearse the whole path first on a test message. 19 Sep (244 transactions) is the backup. It's too small and has no reasons, so it isn't a result.
- **To confirm:** D24, D25.

## 16. Findings still in force

- **F2** Labels are in the scores, edge and features tables.
- **F3** v1 = v2 on 14-16 Sep. v2 edges for 1-13 Sep point at v1's S3 folders.
- **F4** 14 Sep 00:00-05:37 is `validation` (488,561 rows). Queues and evaluation use test rows only.
- **F6** Data effectively ends 16 Sep. With the end-of-day cutoff (D28), nearly every batch alert's window is truncated, so the flag alone says little. See F34 for hours actually observed.
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
- **F28** `investigate()` on a single alert sets `case_id` to null, and its result can differ from the batch's, where the same alert may sit in a case. The app must not assume a `case_id`.
- **F29** DynamoDB and S3 hold an out-of-date 14 Sep run (`batch-2022-09-14-20261107T034826Z`) with old verdict names and no cutoff. Don't use it. The next saved 14 Sep run replaces it.
- **F33** Final triage dry run on 14 Sep, frozen rules, not saved: 78 cases (100 alerts), raise 42, not_sure 36, dont_raise 0. Valid 72, repaired 6, fallback 0. Raise rates are nearly flat across the top 100. US$4.12, 2.06 GB Athena. Local copy, in a temporary folder that may disappear: `/private/tmp/claude-501/-Users-claire-big-data-bq1-repo/77926439-18a2-424e-a652-47c5a6dabd0a/scratchpad/batch_0914_dry2.json`.
- **F34** What the triage runs showed (no labels used):
  - **The verdict mostly repeats BQ1.** Two of the three signals behind `raise` are BQ1 scores: high-ranked neighbours and repeat alerts. Only the fast chain is new evidence from BQ2.
  - **The reasons can't be disproved by payments.** All 78 alerts have "Paid by ACH" and "First payment between these two accounts". So `REASONS_CORROBORATED` says little, and `dont_raise` (unsupported reasons) almost never applies.
  - **No onward payments isn't the cutoff's fault.** The 18 cases with payments but no further signal show 0 downstream payments, though they had a median of 8.2 h (up to 14.4 h) before the end of the day. Meanwhile, 25 of the 42 raised cases show same-day downstream payments. The LLM treats the gap as unknown because no field says how many hours were observed.
  - **Some reasons contain `nan`.** For 8 of 78 alerts, e.g. "Amount is nanx the sender's usual payment". It comes from the reasons data (D5).

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
| D26 | S1 uses the same tools with fixed rules and is the LLM's fallback. High-ranked = member or top-500 neighbour. No graph means low confidence; truncation caps the LLM's confidence at medium. |
| D27 | Current code: verdicts `raise`, `dont_raise`, `not_sure`; code groups related alerts. Replaced if D31 is agreed. |
| D28 | The batch and S1 see data only up to the end of the alert's day, because that is all a daily run knows. `investigate()` has no cutoff, because an analyst clicks later. |
| D30 | The triage rules are frozen in commit `91e344e`, before anyone looked at labels. This is the fallback if D31 is not agreed. Any version is frozen in a commit before labels are read. |
| D31 | *Proposed, needs team agreement.* The agent becomes an investigation assistant: BQ1 keeps the order, and the agent writes an evidence brief per alert or case (section 3). S1 becomes a code-only brief. Evaluation as in section 13. |

## 18. Data access (built)

`src/agent/`: `sql.py` (builders, allowlist, label guard), `data.py` (runner, cache, scan log), `tools.py`, `contracts.py`. Three test alerts scanned 224 MB in total, at most 105 MB per call, and edges match the BQ2 query at 6, 48 and 72 h.

## 19. Remaining agent work (Claire)

**Step 4: Batch runner and `investigate()` (milestone 5), built**
- `src/agent/batch.py` (`run_day` computes, `publish` saves), `store.py` (DynamoDB and S3), `investigate.py`, and `report.py`. CLIs: `scripts/run_batch.py --day <day> [--save]` (dry run unless `--save`), `create_agent_cases_table.py`, `rebuild_tables.py` (dry run unless `--yes`; refuses to delete outside `agent/tables/`, and fails if a day's ranks changed). Concurrency of 4, on a shared cache that is thread-safe.
- Checked with in-memory stores, and `publish` against the real tables with a fake day (`2099-01-01`): reruns replace exactly the day's rows.
- Not done: `investigate()` has no save option (a save would overwrite the batch row for that alert). `rebuild_tables.py --yes` has not been run for real.

**Step 5: Investigation brief (D31)** — steps 2-7 in "Where to continue". Change `contracts.py`, `prompts.py`, `validate.py`, `claims.py`, `baseline.py`, `review_list.py` and `docs/agent_output_format.md`, and extend `tools.py`. Then do one 14 Sep dry run, freeze, and run 14-16 Sep with `--save`.

**Step 6: New-day path (milestone 8)**
- Rank `excluded` rows too (F22), and check that 14-16 Sep ranks stay identical.
- Widen `sql/agent/04-06`, `AGENT_SCORED_DATES` and `AGENT_DATA_HORIZON`.
- Consumer for `is459-etl-v2-day-ready`: rebuild, run the batch, delete the message only on success (F20).
- Check that runs with no reasons still give valid output (F23). Test with a fake message.
- Done when a test message starts the batch with no manual step. Don't start until the 14-16 Sep results are saved.

**Step 7: Recordings (no live demo)**
- Record the setup pipeline and the run pipeline.
- For the one-time 18 Sep run, be present with Person 3 and confirm the consumer is running first.
