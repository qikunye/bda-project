# IS459 AML: agent plan

Owner: Claire (agent and evaluation). Last updated 8 Nov 2026. Team split: `WORK_SPLIT.md` (deleted from the working folder but still in git: `git show HEAD:WORK_SPLIT.md`). ETL and real-time details: `ETL_v2_Handover.docx` (kept outside this repo).

**Status:** the ETL and real-time pipelines are finished. On 7 Nov the agent was rebuilt as an investigation assistant (D31): it writes an evidence brief per case with no verdict. On 7-8 Nov the new tools, S1 and the LLM loop ran on real Athena and Bedrock for 14 Sep (F37), the thresholds were set from label-free distributions (D36, F36), and the problems the runs showed were fixed (D37-D39). 595 unit tests pass, using fakes. The evaluation harness (`src/evaluation/`, `scripts/run_evaluation.py`, instructions in `docs/evaluation.md`) is written but has not run, because it reads labels. **Nothing is frozen yet:** the freeze commit and the saved 14-16 Sep runs are next. Streamlit has not started, so the output format (`docs/agent_output_format.md`) needs no one's agreement and can still change. The old triage version is frozen in commit `91e344e` (not pushed) as a fallback. DynamoDB holds only out-of-date 14 Sep rows (F29), and no one has looked at labels.

## Where to continue

1. **Freeze in a commit** (before anyone looks at labels): `git add src tests docs scripts` and commit. That fixes the thresholds and rules in `src/config.py` (D36), the prompt, the S1 neighbour scoring (D37) and the Athena retry (D38). `runs/` holds large local outputs and is not committed. After the freeze, any change to `src/` is reported as "tuned after the results".
2. **Done 8 Nov** (D42, Sonnet 4.6): saved runs `batch-2022-09-1{4,5,6}-20261108T020412Z`, evaluation and repeats. Results in `docs/evaluation_results.md`.
3. **Evaluation:** follow `docs/evaluation.md`. Mark about 50 observations by hand first (`--review-only`), then run `scripts/run_evaluation.py ... --confirm-frozen`. The harness has not been run on real data, so expect small fixes.
4. Then the **new-day path** and **recordings** (section 19).
5. **Open choices** the owner may want to revisit: `REPEAT_ACCOUNT_ALERTS` fires on 37% of cases and has no threshold (it fires for any other top-100 alert on the same accounts), and `PASS_THROUGH` needs an upstream payment in the alert's own currency (F36, F38).

## 1. Facts

- **Dataset:** IBM AML LI-Medium, synthetic. Don't claim real-world detection performance.
- **Patterns file:** `s3://is459-aml-g1t5/bronze/ibm-aml/li-medium/patterns/LI-Medium_Patterns.txt` lists the laundering attempts and their types. It is ground truth, so only the evaluation may read it.
- **AWS:** us-east-1, account 941377154820, bucket `s3://is459-aml-g1t5/`, IAM user `g1team5`. About US$210 of US$250 credit left on 7 Nov (estimated). Sonnet billing is confirmed. Cost Explorer shows Sonnet charges late, so use the run's own spend estimate.
- **BQ1:** frozen XGBoost 3.2.0, iterations 0-96, 26 features. Test days are 14-16 Sep 2022. Recall@1% 0.634, precision@1% about 3.1%. `risk_score` ranks; it is not a probability.
- **BQ2:** one-hop, directed edges, window 6-72 h, default 48 h. Upstream = payments into the alert's sender before it. Downstream = payments out of its receiver after it.
- **Gold tables** (`is459_aml_gold`, partitioned by `txn_date`): `bq1_risk_scores_medium_v2`, `bq1_features_medium_v2`, `bq2_graph_edges_medium_v2`. All contain labels.
- **Agent tables** (label-free, `agent/tables/`, read by the tools): `agent_scores_v2`, `agent_edges_v2`, `agent_day_queue_v2`. Built by `sql/agent/01-06`.
- **Demo alert:** `txn_a0f703e20da6f9bf0bdad4c0d62862de`, 14 Sep 10:57, day rank 2, has reasons, complete 48 h graph (22 neighbours).
- **Cost:** about US$0.14 per case with Sonnet 4.5; a day's top 100 alerts (about 78 cases) costs about US$11 and takes about 17 minutes (F37). The default spend cap of US$10 a process is too low for a full day: use `--spend-cap 20`.
- **Commands:** tests `.venv/bin/python -m pytest tests` (bare `pytest` fails on a name clash). Tools `python -m scripts.smoke_agent_tools`. S1 `python -m scripts.run_s1 --day <day> --out <file>`. LLM sample `python -m scripts.run_agent_sample --day <day> --limit 20 --out <file>`. Batch `python -m scripts.run_batch --day <day> --out <file>` is a dry run; add `--save` to write. Evaluation `python -m scripts.run_evaluation` (see `docs/evaluation.md`).

## 2. Hard rules

1. **Read-only tools.** The agent writes only to its decisions and cases tables and its trace prefix. It doesn't touch ETL, Silver, Gold or v1.
2. **No labels.** Label-free tables, a column allowlist and a test that fails on a label. `data_split` and the patterns file are banned too. Only the evaluation module reads labels, and the agent never imports it.
3. **Never remove an alert from the BQ1 queue.** BQ1 decides the order; the agent adds evidence.
4. **All numbers and IDs come from tools.** The LLM picks tools and finding codes, and writes text with no numbers. Code checks every cited ID and writes the displayed sentences. The one exception is observations (D34): up to three short remarks the LLM writes itself, with no numbers, shown separately as its reading and not checked by the finding rules.
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
- **Observations (D34):** up to three short remarks of the LLM's own, each citing tool-returned IDs, shown as the agent's reading. Code checks IDs and wording only.

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
| 1 | `get_case_graph` | extended | As now, plus: `hours_observed_after_alert` (alert to the end of visible data, capped at the window, so "no onward payment after N hours" is evidence, F34); `returned_to_source` (downstream payments to an account that is upstream or is the alert's sender, with both payment IDs); `flow` per currency (onward payments within the `PASS_THROUGH` gap threshold as a share of the alert amount in the alert's currency, fastest and median onward gap) |
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

**6.3 Output and validation (built).** `Decision` in `src/agent/contracts.py` was replaced by `Brief`. `findings.py` holds one set of rules used three ways: S1 reports what fires, the validator accepts an LLM finding only if the same rule fires on this run's tool results (and the finding cites its core payments and nothing outside the rule's IDs), and the displayed sentence comes from it. Thresholds were set on 8 Nov from label-free distributions, with the rule next to each value in `config.py` (D36), and also include a minimum onward share for `PASS_THROUGH` (not in the original plan) so that a trivial onward payment does not count. The share (`quick_onward_share`) is onward payments within the gap threshold ÷ the alert amount, in the alert's currency, because the alert is the payment that links sender and receiver (an earlier version divided by the sender's inflows, which was wrong). One-hop data cannot show that the receiver's onward payments were funded by the alert, so the finding is consistent with pass-through, not proof of it. Case members are never counted as `REPEAT_ACCOUNT_ALERTS`.

*What the LLM submits:* `findings` (list of code + `evidence_ids`), `next_checks` (list of codes), `summary`, and optionally `observations` (list of `text` + `evidence_ids`; at most `AGENT_MAX_OBSERVATIONS`, each at most `AGENT_OBSERVATION_MAX_CHARS` characters, with no digits, no spelled-out numbers and no accusation or verdict words such as laundering, fraud, illicit, criminal, suspicious, mule, layering or structuring). Every cited ID must come from a tool result. An observation may not use a finding's wording (such as "pass through", "fan out", "went back", "other alerts" or "spike") unless the brief has that finding, so a pattern the rules did not confirm cannot return as an unchecked remark. A bad observation is dropped, not rejected: the rest of the brief stands, there is no repair, and the trace records why (`RunStats.observations_dropped` counts them). Nothing checks that an observation is right.

*What code adds:* `case_id` (D25); `gaps`, set from tool flags; the `money_flow` for every member; the displayed sentence for every finding, gap and next check; `members`, `stats` and `trace`. Money flow per member is counts and totals per currency, hours observed, and fastest onward gap.

| Finding | Validator requires |
|---|---|
| `PASS_THROUGH` | At least one upstream payment in the alert's currency and one downstream payment from `get_case_graph`, downstream after upstream, onward gap and share within the configured thresholds |
| `FAN_OUT` | At least the configured number of payments from the sender to distinct receivers, from `get_account_activity(sender)` |
| `FAN_IN` | The same for payments into the receiver, from `get_account_activity(receiver)` |
| `MONEY_RETURNS` | Both payment IDs of a `returned_to_source` entry |
| `NEIGHBOURS_HIGH_RISK` | Payments listed by `get_case_graph` or `get_account_activity`, outside the case, that `get_neighbour_scores` shows in the day's top K. Scored related alerts don't count: they are top K by definition and would repeat `REPEAT_ACCOUNT_ALERTS` |
| `REPEAT_ACCOUNT_ALERTS` | Alerts returned by `get_related_alerts` |

| Gap (set by code) | From |
|---|---|
| `NO_GRAPH_AVAILABLE` | `no_graph` |
| `TRUNCATED_WINDOW` | `window_truncated`: the window runs past the end of the dataset (`AGENT_DATA_HORIZON`). A run's own cutoff (D28) does not set it; it shows in `hours_observed_after_alert` (D35) |
| `SHORT_OBSERVATION` | `hours_observed_after_alert` below the configured minimum |
| `SHORT_HISTORY` | `history_days` below the configured minimum, or no `get_account_activity` call |

| Next check | Allowed only with |
|---|---|
| `CHECK_ONWARD_ACCOUNTS` | `PASS_THROUGH` or `FAN_OUT` |
| `CHECK_SOURCE_ACCOUNTS` | `PASS_THROUGH` or `FAN_IN` |
| `CHECK_RETURN_FLOW` | `MONEY_RETURNS` |
| `REVIEW_RELATED_ALERTS` | `REPEAT_ACCOUNT_ALERTS` or `NEIGHBOURS_HIGH_RISK` |
| `CHECK_ACCOUNT_HISTORY` | `SHORT_HISTORY` |
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

`AGENT_MODEL_ID` is Sonnet 4.6, `us.anthropic.claude-sonnet-4-6` (D42; Sonnet 4.5 until 8 Nov, D23). Backup: Nova 2 Lite, `us.amazon.nova-2-lite-v1:0`.

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
| 2 | Case grouping + S1 | Claire | Done; thresholds set (D36), freeze pending |
| 3 | Evaluation harness | Claire | Written (`src/evaluation/`, `docs/evaluation.md`); not run, waits on the freeze |
| 4 | LLM loop + validator | Claire | Done; ran on Bedrock for 14 Sep (F37) |
| 5 | Batch runner, `investigate()` | Claire | Done; dry run on 14 Sep done, saved 14-16 Sep runs wait on the freeze |
| 6 | Streamlit | Person 2 | Not started; output format is ready |
| 7 | Full evaluation | Claire | To do; follows the saved runs |
| 8 | New-day path, queue consumer | Claire | To do; required for the recorded run |

## 13. Evaluation (Claire, for D31)

The harness is `src/evaluation/` (the only code that reads labels; a test checks that `src/agent` never imports it) and `scripts/run_evaluation.py`. Step-by-step instructions for whoever runs it: `docs/evaluation.md`. It is written and tested with fakes but has not run on real data.

- **Days:** 14-16 Sep only, test rows only, top 100 alerts per day, reported per day. Every day is seen as of its own end (D28). Anything reading Gold must filter to these dates.
- **Labels only after the freeze.** Tune without labels, freeze in a commit, then evaluate. Report any later change separately, as tuned after the results.
- **What S2 can and cannot add (read first).** The validator accepts only findings that the same fixed rules find on the tool results, and S1 runs every tool for every member. So S2's findings are almost always a subset of S1's. S2 can go beyond S1 only by choosing a different window, by adding counterparty accounts to `get_related_alerts`, by scoring neighbours S1 does not look up (activity payments), and through its observations (D34). For findings, S2 against S1 therefore measures how much the LLM leaves out. What the LLM can add is the choice of which findings and evidence to show, the summary, and the next checks. Report it that way.
- **Are the findings accurate?** Every finding must cite tool results. Report the rejected-citation, repair and fallback rates.
- **Do the clues carry information?** For each finding code, compare the laundering rate of alerts with it and without it. Positives are scarce: precision@1% is about 3.1%, and the top-100 precision is unknown until labels are read (probably higher, but do not assume it), so 300 alerts may hold only about 10 laundering alerts and per-code rates would mean little. Answer this with S1 (code only, no LLM) on a deeper list: the top 500 per day (about 100 GB scanned a day, about US$0.5), or up to 5,000 (about US$5 a day and many hours; `brief_day` is serial today, so it needs a concurrent runner). Keep S2 on the top 100. Report counts with rates and intervals. `NEIGHBOURS_HIGH_RISK` and `REPEAT_ACCOUNT_ALERTS` come from BQ1 scores (F34), so compare with and without them within BQ1 rank bands, or they will look informative just because BQ1 is. The payment-based codes (`PASS_THROUGH`, `FAN_OUT`, `FAN_IN`, `MONEY_RETURNS`) do not depend on BQ1.
- **Does it name the right pattern?** For alerts in a laundering attempt from the patterns file, compare the agent's pattern findings with the attempt's type. For reference, the real-time explainer's typology macro-F1 is 0.345 (F21).
- **Are the observations useful?** They are the one place S2 says something the finding rules cannot, so judge them by hand: read a sample of about 50 briefs and mark each observation as accurate and useful, accurate but obvious, or wrong or unsupported. Report the rates, the rate of briefs that have any, and how many the validator dropped (`observations_dropped`), by reason. No label can score them, so do this before looking at what labels say.
- **What does the LLM add?** Compare S2 with S1 on the measures above, keeping in mind the first bullet: list the findings S2 drops that S1 keeps, and judge the summary and next checks on example briefs. If the LLM adds nothing over code, report that.
- **Also:** cost and latency, and repeatability: run a 50-alert subset 3 times.
- **Not measurable here:** time saved for analysts. Show example briefs instead.

## 15. Risks and open items

- **Timeline:** the change touches the output format, the prompt, the validator, S1 and the evaluation. If time is short, fall back to the frozen triage version (`91e344e`).
- **Code does most of the work:** see the first bullet of section 13. The LLM's value is the choice of findings, the summary and the next checks.
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
- **F6** Data effectively ends 16 Sep. With the end-of-day cutoff (D28), every batch alert's 48 h window passes its cutoff, so that alone says nothing; since D35 it no longer sets `window_truncated`. See F34 for hours actually observed.
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
- **F36** Thresholds basis (8 Nov, no labels): 690 alerts on 14-16 Sep, being the top 100 of each day (300), ranks 101-5,000 (183) and ordinary alerts (207). Counterparties in the window: top-100 median 1 and 90th percentile 6; 18% of top-100 senders and 13% of receivers have 5 or more, against 1% of ordinary receivers (99th percentile 4). 16% of ordinary senders pay 20 or more accounts (payment hubs, some over 20,000); no top-100 sender does (maximum about 15). Fastest onward gap, where there is one: median 3.4 h in the top 100 and 4.4 h in ordinary alerts. Half the alert amount going on within 6 h: 13% of top-100 alerts and 15% of ordinary ones, so `PASS_THROUGH` does not separate top alerts from ordinary ones, and the share can be far above 1 for a busy receiver (340 times in one case). Account history is 3 to 5 days everywhere, so `SHORT_HISTORY` never fires. 15% of top-100 alerts have under 6 h observed.
- **F37** 14 Sep runs on real data (8 Nov, not saved; local files in `runs/`, which is not committed). S1: 78 cases, 39 with a finding (`REPEAT_ACCOUNT_ALERTS` 29, `NEIGHBOURS_HIGH_RISK` 29, `FAN_OUT` 16, `FAN_IN` 7, `PASS_THROUGH` 1, `MONEY_RETURNS` 0), 26.6 min, 6.4 GB. S2 dry run with `--spend-cap 20`: valid 64 (82%), repaired 12 (15%), fallback 2 (2.6%), no errors; 150 observations in 73 cases, 10 dropped; US$10.93 (US$0.14 a case), 17 min, 6.1 GB; steps mean 4.4, maximum 8. S2 added no finding to S1's and dropped one `NEIGHBOURS_HIGH_RISK`, so for findings the LLM adds nothing over code and its value is the summary, next checks and observations. The first dry run (cap US$10) had 12% fallback, six of them from the cap, and one case lost to a transient Athena error (D38). Remaining repair reasons: `FAN_OUT` claimed without support (7), `RECHECK_AFTER_MORE_DATA` without its gap (5, was 13 before the prompt line), `PASS_THROUGH` claimed without support (3).
- **F38** `NO_GRAPH_AVAILABLE` was on 18 of 78 cases on 14 Sep. For 13 of them no payment goes into the sender or out of the receiver even without the end-of-day cutoff; for 5 the receiver's onward payments exist but came after midnight, so D28 hides them. The accounts often have other activity, which `get_account_activity` sees (7 of the 18 still had findings such as `FAN_OUT`), so the gap text now says "no payments into the sender or out of the receiver". `PASS_THROUGH` needs an upstream payment in the alert's own currency: on 14 Sep, 5 of 6 top-100 alerts that met the gap and share rules had inflows only in another currency. Cross-currency alerts are otherwise not an issue (0 of the top 500 a day).
- **F39** First run through the Fargate state machine (8 Nov, 14 Sep): event to saved result in 14.5 min, 100 decision rows, 78 cases, 78 traces written. But 29 of 78 cases fell back, 26 of them from Bedrock `ThrottlingException: Too many tokens per day`: the account's daily token quota for Sonnet 4.5 was used up by the day's earlier runs (about US$30 to 35 of estimated spend, roughly 9 to 10 million input tokens). The 14 Sep rows in DynamoDB are therefore mostly fallback briefs and must be rerun; the evaluation is not affected, because it uses its own S1 and S2 files. Three full days need about three times that, so run them on separate days or ask for a higher quota in Service Quotas. The state machine had one bug, fixed: `StringMatches` supports only `*`, not `?`.

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
| D19 | *Superseded by D40.* The batch reads the existing queue; no new EventBridge rule. |
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
| D34 | *7 Nov.* Observations: besides the verified findings, the LLM may add up to three free-form remarks (a `text` plus the `evidence_ids` it is about). Code checks the IDs, length, no numbers, no accusation words and no finding wording without that finding, not whether the remark is right, so the app shows them as the agent's reading. A bad observation is dropped, never rejecting the brief, so D34 cannot raise the fallback rate. Code-only briefs and fallbacks have none. Rows store only `observation_count`. |
| D35 | *7 Nov, before the freeze.* (1) `TRUNCATED_WINDOW` is set only when the window runs past the end of the dataset, not by a run's own end-of-day cutoff: that cutoff passed every 48 h batch window, so the gap and `RECHECK_AFTER_MORE_DATA` were on every brief. `SHORT_OBSERVATION` covers a cutoff soon after the alert. (2) `ACTIVITY_SPIKE` is dropped. Its history starts 11 Sep, overlaps the window (12-13 Sep for a 14 Sep alert) and is 3, 4 or 5 days long on 14, 15 and 16 Sep, so it could not be judged the same way on each day. `get_account_activity` still returns `daily_history`, and an observation may not claim a jump in activity either. `CHECK_ACCOUNT_HISTORY` now follows only `SHORT_HISTORY`. |
| D36 | *8 Nov, before the freeze.* The `config.py` thresholds stay at their placeholder values, which become final, because F36 gives no reason to move them. The rule behind each value is written next to it (`AGENT_FAN_MIN_COUNTERPARTIES` 5: above the 99th percentile of ordinary receivers; `AGENT_PASS_THROUGH_MAX_GAP_MIN` 360 and `AGENT_PASS_THROUGH_MIN_ONWARD_SHARE` 0.5; `AGENT_NEIGHBOURS_HIGH_MIN` 1; `AGENT_MIN_HISTORY_DAYS` 2). Nothing is loosened to make a rare finding fire. `PASS_THROUGH` and `MONEY_RETURNS` are rare and are reported as such. |
| D37 | *8 Nov.* S1 now sends every payment listed by the graph and by account activity to `get_neighbour_scores` (up to `AGENT_MAX_NEIGHBOUR_IDS`), as the `NEIGHBOURS_HIGH_RISK` rule counts them. Before, it scored only graph payments and found 6 cases against the LLM's 28; now 29 against 28. |
| D38 | *8 Nov.* The Athena runner retries a query that fails with an "internal error" up to 3 times, because one such failure stopped a whole day's save. Final runs use `--spend-cap 20`, because a day costs about US$11. |
| D39 | *8 Nov.* Wording and naming: the money-flow field `onward_share` is now `quick_onward_share`, because it counts only payments within the pass-through gap; the `NO_GRAPH_AVAILABLE` text and prompt say "no payments into the sender or out of the receiver"; the prompt tells the model the exact next-check conditions and not to spell out numbers. |
| D40 | *8 Nov.* The batch is triggered without a polling script: a second target `agent-batch` on the ETL's rule `is459-etl-v2-day-succeeded` (ETL owner agreed) starts the Step Functions state machine `is459-agent-batch`, which reads the day from `detail.input` and runs `scripts.run_batch --day <day> --spend-cap 20 --save` as a Fargate task. A failure sends an SNS email. Everything is in `infra/` (Dockerfile, task and state machine definitions, IAM policies, `deploy.sh`, `destroy.sh`), tagged `project=is459-g1t5-etl-v2`. The container's role can read only the label-free agent tables, so it cannot reach labels. The old target `agent-queue` (SQS `is459-etl-v2-day-ready`) is still on the rule and nothing reads it; remove the target and then the queue and its DLQ when convenient. |
| D41 | *8 Nov, after the freeze, outside `src/`.* `scripts/run_batch.py` refuses `--save` when any case fell back because the LLM could not be used (a failed call such as a quota error, or the spend cap), unless `--allow-llm-failures` is passed. It changes no brief, finding or threshold. |
| D42 | *8 Nov, agreed with the team.* The agent moves from Sonnet 4.5 to Sonnet 4.6 (`us.anthropic.claude-sonnet-4-6`, same list price) because Sonnet 4.5's daily token quota (27M, not adjustable) blocks running three days of evaluation (F39). No prompt, finding rule or threshold changes. A 10-alert dry run on 14 Sep: 9 valid, 1 repaired, 0 fallback, US$0.10 a case. `infra/iam/task-role-policy.json` now allows Sonnet 4.6; the deployed role and image still need `deploy.sh` (or the policy update alone, plus `--model` in the state machine) before the Fargate trigger can use it. The evaluation runs for 14 to 16 Sep were made from CloudShell with `--model us.anthropic.claude-sonnet-4-6`. |
| D43 | *8 Nov.* New-day path. `AGENT_LAST_DAY` (environment) sets the last day in the agent tables and `AGENT_SCORED_DATES`/`AGENT_DATA_HORIZON` follow it; default 16 Sep, so the evaluated runs are unchanged. `v_agent_scores` now ranks `excluded` rows per day too (F22): 14-16 Sep ranks unchanged, 17 Sep gets 1-380. The state machine runs a `RebuildTables` Fargate step before `RunBatch`, under a separate role `is459-agent-rebuild-role` that can read Gold (which holds the label column) and write `agent/tables/`; the batch keeps the label-free role. `rebuild_tables.py` does nothing if the tables already hold every day up to `AGENT_LAST_DAY`, and refuses to drop a later day, so a replayed event never shrinks the tables. One day at a time: two overlapping new-day runs would rebuild the same tables. |
| D33 | One hop only. BQ2 is defined as one hop (6-72 h) and only that is evaluated, so the agent doesn't follow a counterparty's own payments. A second hop would be new analysis, not BQ2 output. |

## 18. Data access (built)

`src/agent/`: `sql.py` (builders, allowlist, label guard), `data.py` (runner, cache, scan log), `tools.py`, `contracts.py`. Three test alerts scanned 224 MB in total, at most 105 MB per call, and edges match the BQ2 query at 6, 48 and 72 h.

## 19. Remaining agent work (Claire)

**Step 4: Batch runner and `investigate()` (milestone 5), built**
- `src/agent/batch.py` (`run_day` computes, `publish` saves), `store.py` (DynamoDB and S3), `investigate.py`, and `report.py`. CLIs: `scripts/run_batch.py --day <day> [--save]` (dry run unless `--save`), `create_agent_cases_table.py`, `rebuild_tables.py` (dry run unless `--yes`; refuses to delete outside `agent/tables/`, and fails if a day's ranks changed). Concurrency of 4, on a shared cache that is thread-safe.
- Checked with in-memory stores, and `publish` against the real tables with a fake day (`2099-01-01`): reruns replace exactly the day's rows.
- Not done: `investigate()` has no save option (a save would overwrite the batch row for that alert). `rebuild_tables.py --yes` has not been run for real.

**Step 5: Investigation brief (D31), built 7 Nov.** `contracts.py`, `evidence.py`, `findings.py`, `brief.py`, `validate.py`, `prompts.py`, `loop.py`, `baseline.py`, `decisions.py`, `review_list.py`, `report.py`, `sample_results.py` and the tools in 5.3 are done, with `claims.py` replaced by `brief.py`. Smoke test, thresholds and the 14 Sep S1 and S2 runs are done (F36, F37). What remains is the freeze, then 14-16 Sep with `--save` ("Where to continue").

**Step 6: New-day path (milestone 8)**
- Rank `excluded` rows too (F22), and check that 14-16 Sep ranks stay identical.
- Widen `sql/agent/04-06`, `AGENT_SCORED_DATES` and `AGENT_DATA_HORIZON`.
- Built 8 Nov (D43): the state machine rebuilds the agent tables for the new day before the batch.
- New days have no BQ1 reasons (F23), which no longer matters because the agent never reads them. Test with a fake message.
- Done when a test event for a new day starts the state machine and a brief is saved with no manual step. Don't start until the 14-16 Sep evaluation is finished, because this changes files the evaluated runs depend on.

**Step 7: Recordings (no live demo)**
- Record the setup pipeline and the run pipeline.
- For the one-time 18 Sep run, be present with Person 3 and confirm the consumer is running first.
