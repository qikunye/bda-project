# IS459 AML: agent plan

Owner: Claire (agent and evaluation). Last updated 7 Nov 2026. Team split: `WORK_SPLIT.md` (deleted from the working folder but still in git: `git show HEAD:WORK_SPLIT.md`). ETL and real-time details: `ETL_v2_Handover.docx` (kept outside this repo).

**Status:** the ETL and real-time pipelines are finished. On 7 Nov the agent was rebuilt as an investigation assistant (D31): it writes an evidence brief per case with no verdict. The tools (5.3), the finding rules (`findings.py`), the LLM loop and validator, the code-only brief S1 (`baseline.py`), storage rows, review list and sample data are all done and covered by 492 unit tests, which use fakes. **Nothing new has run on real Athena or Bedrock yet**, and the finding thresholds in `config.py` are placeholders. Streamlit has not started, so the output format (`docs/agent_output_format.md`) needs no one's agreement and can still change. The old triage version is frozen in commit `91e344e` (not pushed) as a fallback. DynamoDB holds only out-of-date 14 Sep rows (F29), and no one has looked at labels.

## Where to continue

1. **Run the new tools on real Athena:** `python -m scripts.smoke_agent_tools` (extended for `get_account_activity` and the new graph fields). Check bytes scanned per call, and the query time and size for a busy account. Nothing in the new tool layer has touched Athena yet.
2. **Set the thresholds** in `config.py` (section 6.3), from label-free distributions of the 14-16 Sep top alerts and of ordinary accounts. Run `python -m scripts.run_s1 --day 2022-09-14 --out <file>` to see how often each finding fires. Write the rule you used next to each value. They are placeholders now.
3. **Dry run on 14 Sep**, then check the step cap of 8 (it was 5), cost and fallback rate, and **freeze in a commit** before anyone looks at labels. Then run 14, 15 and 16 Sep with `--save` (about US$12; the per-case cost will be higher now, so measure it) and record the run IDs here.
4. **Evaluation** (section 13), then the **new-day path** and **recordings** (section 19).
5. **Still open from D31:** `ACTIVITY_SPIKE` is built but is the lowest priority; drop it if the dry run shows it adds noise. The loop only lets the LLM pass account IDs that tools have returned to `get_related_alerts`.
6. Done on 7 Nov: output format (`docs/agent_output_format.md`), tools in 5.3, S1 as a brief, LLM brief with validator and sentences, review list in BQ1 order, reasons removed from the agent and its SQL (D32, F35). No `nan` fix is needed.

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
6. **Bounded.** At most 8 tool steps per run (`AGENT_MAX_STEPS`; was 5, confirm after the dry run), plus token, timeout and global spend caps.
7. **Reasons are model explanations, not proof.** They stay in the data, but no agent query selects them and Streamlit doesn't show them (D32).

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
- **Gaps:** no graph, a truncated or short observed window, or short account history.
- **Next checks:** what the analyst should look at next, from a fixed list.
- **Summary:** one short sentence from the LLM, with no numbers.

The brief gives no verdict: the analyst decides. The current code still produces triage verdicts, as described in D27-D30.

## 4. Triggers

- **B (batch):** top 100 alerts of a day, grouped into cases, seeing data only up to the end of that day (D28). Run with `run_batch --day`, or from the queue message for a new day.
- **C (on demand):** `investigate()` inside Streamlit. Doesn't write decisions by default. No cutoff, so it can differ from the batch (D28).
- **A (real-time):** out of scope (D17).

## 5. Tools

All are in `src/agent/tools.py`, and every result has an `ids` list.

- `get_alert(txn_id, txn_date)`: score, day rank, payment details. No reasons (D32).
- `get_case_graph(alert, window_h=48)`: up to 50 edges per direction, counts and totals per currency, `no_graph`, `window_truncated`, `edges_capped`. An optional `as_of` hides later payments.
- `get_neighbour_scores(txn_ids, txn_dates)`: at most 200 IDs. Dates outside the scored days return `scored: false`.
- `get_related_alerts(accounts, anchor_ts, window_h)`: other top-K alerts on the same accounts. An optional `as_of` hides later alerts.

**5.1 Tables:** `v_agent_scores` and `v_agent_edges` are the label-free sources for the agent tables. **5.2** Tools cache by SQL text, so repeats cost nothing.

**5.3 Final tool set (D31, built).** Four LLM tools stay, two change, one is new, plus `submit_brief`. Everything stays one hop (D33): no tool follows a counterparty's own payments. All results carry `ids` and honour `as_of` (D28). Build in this order.

| # | Tool | Change | What it gives |
|---|---|---|---|
| 1 | `get_case_graph` | extended | As now, plus: `hours_observed_after_alert` (alert to the end of visible data, capped at the window, so "no onward payment after N hours" is evidence, F34); `returned_to_source` (downstream payments to an account that is upstream or is the alert's sender, with both payment IDs); `flow` per currency (onward share of upstream money, fastest and median onward gap) |
| 2 | `get_account_activity(txn_id, side, window_h)` | new | `side` is `sender` or `receiver`. That account's other payments, in and out, within ±window of the alert, excluding the alert: counts, distinct counterparties, totals per currency, up to 50 payments per direction (`edges_capped`). Also daily counts and totals for earlier days back to 11 Sep, with `history_days`. Reads `agent_edges_v2`; no new table |
| 3 | `get_related_alerts` | extended | Also accepts the graph's counterparty accounts, not only the alert's sender and receiver |
| 4 | `get_alert` | changed | No reasons (D32) |
| 5 | `get_neighbour_scores` | unchanged | |

History is short: `agent_edges_v2` starts on 11 Sep, so a 14 Sep alert has at most 3 earlier days. Onward gap = minutes from the alert to each downstream payment. `get_related_alerts` takes up to `AGENT_MAX_RELATED_ACCOUNTS` accounts; in the loop the LLM may add account IDs a tool returned. The pure parts of the graph and activity tools are `case_graph_from_frame` and `account_activity_from_frames`, which the samples also use.

## 6. Agent loop (built: `src/agent/llm.py`, `prompts.py`, `validate.py`, `loop.py`, `findings.py`, `brief.py`, `evidence.py`)

```
seed = get_alert(...)   # by code
for step in 1..8: LLM sees policy + seed + prior results, returns a tool call or submit
invalid after 1 repair, or no answer by step 8 → S1 brief, fallback=true
```

Bedrock Converse with native tool calling, temperature 0. Log steps, tokens, latency and validation outcome. The last step must be the submit call. A repair is one extra reply outside the step cap.

**6.1 Input (D31).**
- **Batch** (`run_day`): the day's top 100 from `agent_day_queue_v2`, grouped into cases (7.1), seeing data only up to the end of the day (D28). The LLM's first message has the day, the default window (48 h), the `case_id`, the member IDs best rank first, and the representative's `get_alert` result. No reasons, no labels.
- **On demand** (`investigate(txn_id, txn_date, window_h)`): the same, for one alert and its case, with no cutoff.
- **Data:** only the label-free agent tables, through the tools in 5.3.

**6.2 Prompt (built):** an investigation assistant with read-only tools and no labels. Gather the flow around the alert and its accounts, report the patterns the results show, and pick next checks from the list. Cite IDs, give no verdict, and write no numbers.

**6.3 Output and validation (built).** `Decision` in `src/agent/contracts.py` was replaced by `Brief`. `findings.py` holds one set of rules used three ways: S1 reports what fires, the validator accepts an LLM finding only if the same rule fires on this run's tool results (and the finding cites its core payments and nothing outside the rule's IDs), and the displayed sentence comes from it. Thresholds are placeholders (see `config.py`) and also include a minimum onward share for `PASS_THROUGH` (not in the original plan) so that a trivial onward payment does not count.

*What the LLM submits:* `findings` (list of code + `evidence_ids`), `next_checks` (list of codes), `summary`.

*What code adds:* `case_id` (D25); `gaps`, set from tool flags; the `money_flow` for every member; the displayed sentence for every finding, gap and next check; `members`, `stats` and `trace`. Money flow per member is counts and totals per currency, hours observed, and fastest onward gap.

| Finding | Validator requires |
|---|---|
| `PASS_THROUGH` | At least one upstream and one downstream payment from `get_case_graph`, downstream after upstream, onward gap within the configured threshold |
| `FAN_OUT` | At least the configured number of payments from the sender to distinct receivers, from `get_account_activity(sender)` |
| `FAN_IN` | The same for payments into the receiver, from `get_account_activity(receiver)` |
| `MONEY_RETURNS` | Both payment IDs of a `returned_to_source` entry |
| `NEIGHBOURS_HIGH_RISK` | Neighbours that `get_neighbour_scores` shows in the day's top K |
| `REPEAT_ACCOUNT_ALERTS` | Alerts returned by `get_related_alerts` |
| `ACTIVITY_SPIKE` | Window activity above the earlier days' daily mean by the configured factor, with enough `history_days`. Lowest priority; drop if time is short |

| Gap (set by code) | From |
|---|---|
| `NO_GRAPH_AVAILABLE` | `no_graph` |
| `TRUNCATED_WINDOW` | `window_truncated` |
| `SHORT_OBSERVATION` | `hours_observed_after_alert` below the configured minimum |
| `SHORT_HISTORY` | `history_days` below the configured minimum, or no `get_account_activity` call |

| Next check | Allowed only with |
|---|---|
| `CHECK_ONWARD_ACCOUNTS` | `PASS_THROUGH` or `FAN_OUT` |
| `CHECK_SOURCE_ACCOUNTS` | `PASS_THROUGH` or `FAN_IN` |
| `CHECK_RETURN_FLOW` | `MONEY_RETURNS` |
| `REVIEW_RELATED_ALERTS` | `REPEAT_ACCOUNT_ALERTS` or `NEIGHBOURS_HIGH_RISK` |
| `CHECK_ACCOUNT_HISTORY` | `ACTIVITY_SPIKE` or `SHORT_HISTORY` |
| `RECHECK_AFTER_MORE_DATA` | `SHORT_OBSERVATION` or `TRUNCATED_WINDOW` |

Code must check:
1. The JSON matches the schema, and every code is valid.
2. Every `evidence_ids` member appears in a tool result's `ids` from this run.
3. Each finding meets its row in the finding table, using that run's tool results.
4. Each next check meets its row in the next-check table.
5. `summary` is at most 200 characters, with no digits or spelled-out numbers.

*What Streamlit receives* (`InvestigateResult` and `DecisionRow` are changed): `case_id`, rendered `findings` (code, sentence, `evidence_ids`), `gaps`, `next_checks`, `summary`, `money_flow` and `members`, plus `stats`, `trace` and `fallback`. `verdict` and `confidence` are gone. Thresholds live in `config.py`, chosen before the freeze without labels.

## 7. Cases and review list (built)

**7.1** `src/agent/cases.py`: link alerts that share an account within the window; cap of 20; best rank is the representative.

**7.2** `src/agent/review_list.py` keeps BQ1's order and attaches the brief's codes to each alert.

**7.3** Each result has `members`: every alert in the case with its payments. Code fetches any graph the LLM skipped, after it answers, so these never reach the LLM.

## 8. S1 baseline (built)

`src/agent/baseline.py`: a brief built by code alone. It runs every tool for every member and applies the same finding rules, with fixed thresholds in `config.py` and a template summary. It is also the LLM's fallback. The evaluation then shows what the LLM adds over code.

S0 = BQ1 order with no brief. S1 = code-only brief. S2 = LLM brief.

## 9. LLM

`AGENT_MODEL_ID` is Sonnet 4.5, `us.anthropic.claude-sonnet-4-5-20250929-v1:0` (D23). Backup: Nova 2 Lite, `us.amazon.nova-2-lite-v1:0`.

## 10. Storage

- `is459_agent_decisions`: key `transaction_id`, rows as in `DecisionRow`. Holds only the out-of-date 14 Sep run (F29).
- `is459_agent_cases` (on-demand, tagged `project=is459-g1t5-etl-v2`): key `day` + `case_id`. One item per case, singles included (`CaseRow` in `contracts.py`).
- Traces: `s3://is459-aml-g1t5/agent/traces/<day>/<txn_id>.json`.
- A batch rerun writes the new rows first, then deletes that day's rows it did not rewrite (scan on `day`, no index). If any case failed, nothing is written or deleted. Each alert is in one case, so each transaction gets one row.
- The brief changed the row contents, not the tables or keys: `verdict`, `confidence` and `claims` are gone, and `finding_codes`, `gap_codes`, `next_check_codes` and `summary` are stored so rates can be counted.

## 11. Streamlit

Owned by Person 2, not started. Contract: `docs/agent_output_format.md` (matches D31 and the code). The app must say full days end on 16 Sep 2022 and the data is synthetic. It doesn't show BQ1 reasons (D32). It reads DynamoDB, never SQS.

## 12. Milestones

| # | Milestone | Owner | Status |
|---|---|---|---|
| 1 | Data access | Claire | Done |
| 2 | Case grouping + S1 | Claire | Done (code-only brief); thresholds are placeholders |
| 3 | Evaluation harness | Claire | To do; waits on D31 |
| 4 | LLM loop + validator | Claire | Done (brief version); not yet run on Bedrock |
| 5 | Batch runner, `investigate()` | Claire | Done; saved 14-16 Sep runs wait on thresholds and the dry run |
| 6 | Streamlit | Person 2 | Not started; output format is ready |
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
  - **Some reasons contain `nan`.** For 8 of 78 alerts, e.g. "Amount is nanx the sender's usual payment". It comes from the reasons data (D5). F35 has the full count.
- **F35** BQ1 reasons carry almost no information (7 Nov, `agent_scores_v2`, top 100 per day on 14-16 Sep, 300 alerts, no labels). "First payment between these two accounts" and "Paid by ACH" are on all 300, with identical text. The third reason is one of nine features (`snd_prior_txn_cnt` 105, `snd_secs_since_last_out` 105, then 24 or fewer each). Only 10 distinct feature combinations exist. 36 of 300 alerts (12%) have `nan` in a sentence. Treat it as a stated limit of BQ1's explanations, not of its ranking (Recall@1% 0.634). F34's "78 alerts" is the 14 Sep case count; this result covers all 300. Cause not investigated.

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
| D27 | Old triage code: verdicts `raise`, `dont_raise`, `not_sure`. Replaced by D31 on 7 Nov; kept in commit `91e344e`. Code still groups related alerts. |
| D28 | The batch and S1 see data only up to the end of the alert's day, because that is all a daily run knows. `investigate()` has no cutoff, because an analyst clicks later. |
| D30 | The triage rules are frozen in commit `91e344e`, before anyone looked at labels. This is the fallback if D31 is not agreed. Any version is frozen in a commit before labels are read. |
| D31 | *Owner decided 7 Nov; implemented the same day.* The agent becomes an investigation assistant: BQ1 keeps the order, and the agent writes an evidence brief per alert or case (section 3), with no verdict. Tools in 5.3, input and output in section 6. S1 becomes a code-only brief. Evaluation as in section 13. |
| D32 | BQ1 reasons stay in the data but are not shown in Streamlit and not given to the agent. The agent never explains the BQ1 score: it reports evidence around the alert. The F35 numbers are reported as a limit of BQ1's explanations. |
| D33 | One hop only. BQ2 is defined as one hop (6-72 h) and only that is evaluated, so the agent doesn't follow a counterparty's own payments. A second hop would be new analysis, not BQ2 output. |

## 18. Data access (built)

`src/agent/`: `sql.py` (builders, allowlist, label guard), `data.py` (runner, cache, scan log), `tools.py`, `contracts.py`. Three test alerts scanned 224 MB in total, at most 105 MB per call, and edges match the BQ2 query at 6, 48 and 72 h.

## 19. Remaining agent work (Claire)

**Step 4: Batch runner and `investigate()` (milestone 5), built**
- `src/agent/batch.py` (`run_day` computes, `publish` saves), `store.py` (DynamoDB and S3), `investigate.py`, and `report.py`. CLIs: `scripts/run_batch.py --day <day> [--save]` (dry run unless `--save`), `create_agent_cases_table.py`, `rebuild_tables.py` (dry run unless `--yes`; refuses to delete outside `agent/tables/`, and fails if a day's ranks changed). Concurrency of 4, on a shared cache that is thread-safe.
- Checked with in-memory stores, and `publish` against the real tables with a fake day (`2099-01-01`): reruns replace exactly the day's rows.
- Not done: `investigate()` has no save option (a save would overwrite the batch row for that alert). `rebuild_tables.py --yes` has not been run for real.

**Step 5: Investigation brief (D31), built 7 Nov.** `contracts.py`, `evidence.py`, `findings.py`, `brief.py`, `validate.py`, `prompts.py`, `loop.py`, `baseline.py`, `decisions.py`, `review_list.py`, `report.py`, `sample_results.py` and the tools in 5.3 are done, with `claims.py` replaced by `brief.py`. What remains is real runs: smoke test, thresholds, one 14 Sep dry run, freeze, then 14-16 Sep with `--save` ("Where to continue").

**Step 6: New-day path (milestone 8)**
- Rank `excluded` rows too (F22), and check that 14-16 Sep ranks stay identical.
- Widen `sql/agent/04-06`, `AGENT_SCORED_DATES` and `AGENT_DATA_HORIZON`.
- Consumer for `is459-etl-v2-day-ready`: rebuild, run the batch, delete the message only on success (F20).
- New days have no BQ1 reasons (F23), which no longer matters because the agent never reads them. Test with a fake message.
- Done when a test message starts the batch with no manual step. Don't start until the 14-16 Sep results are saved.

**Step 7: Recordings (no live demo)**
- Record the setup pipeline and the run pipeline.
- For the one-time 18 Sep run, be present with Person 3 and confirm the consumer is running first.
