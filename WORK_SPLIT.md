# IS459 AML: remaining work split

Status as of 6 Nov 2026. The ETL and real-time pipelines are built and need no new features (see `ETL_v2_Handover.docx`). What's left is the agent, Streamlit, the evaluation, and the assessment deliverables: **presentation, live demo and recorded demo**. The recorded demo must show how to set up and run the pipeline end to end, with narration or annotations.

## Who does what

| Person | Area | Owner |
|---|---|---|
| 1 | Agent | Claire |
| 2 | Streamlit | TBC (Owen built the earlier app) |
| 3 | Pipeline runs and footage | TBC (JX built the ETL) |
| 4 | Evaluation | TBC |
| 5 | Presentation | TBC |
| 6 | Set-up guide, video edit and live demo | TBC |

## 1. Agent: Claire
- [ ] Publish the output format for Streamlit: `investigate()` result, trace, decision fields and claim templates
- [ ] Case grouping and the deterministic baseline, S1 (milestone 2)
- [ ] LLM loop and validator, tested on 20 alerts (milestone 4)
- [ ] Batch runner, `is459_agent_cases` and the one-command table rebuild (milestone 5)
- [ ] `investigate()` for Streamlit
- [ ] Rank fix for new days, wider date ranges and the `is459-etl-v2-day-ready` consumer (milestone 8)
- [ ] Run the batch on 14-16 Sep and pass the decisions to Person 4
- [ ] Keep `PLAN.md` up to date

## 2. Streamlit

**Can start now**
- [ ] Move the queue and case graph from v1 to the v2 tables
- [ ] Fix the visualisations
- [ ] Add the real-time panel, reading `is459_rt_alerts` and `is459_rt_briefs` from DynamoDB
- [ ] Add the notices: full days end on 16 Sep 2022, reasons are not proof, data is synthetic
- [ ] Decide where the app runs for the demo: laptop or EC2

**Starts once the output format is published, using fake results at first**
- [ ] Switch between BQ1 order and agent order, with verdict badges
- [ ] Investigate button, showing the verdict, claims, evidence and trace
- [ ] Highlight promoted neighbours on the case graph
- [ ] Switch to the real agent and test the demo alert end to end

## 3. Pipeline runs and footage
- [ ] Learn the runbooks in the handover, and test the real-time session on a small slice
- [ ] Confirm the real-time budget gate is clear (`is459-rt-guardrail`)
- [ ] Record the set-up walkthrough of the deploy scripts and AWS resources, without redeploying anything
- [ ] Run 18 Sep once with Person 1 present, recording from the start
- [ ] Record a short real-time session in fresh mode
- [ ] Run a real-time session shortly before the live demo so the panel has fresh alerts

## 4. Evaluation
- [ ] Build the offline harness as a separate module the agent never imports, with S0 recall@1% reproducing about 0.63 (milestone 3)
- [ ] Add S1 once it's ready, then S2 once the batch has run (milestone 7)
- [ ] Report recall and precision at K = 100, 500 and 1,000, chain neighbours recovered, displacement cost, validity rates, and cost and latency
- [ ] Report per day, with 14 Sep as the clean day, because 15-16 Sep have truncated downstream windows
- [ ] Repeat a 50-alert subset 3 times to measure variance
- [ ] Give Person 5 the final tables and charts

## 5. Presentation
- [ ] Draw the simplified architecture diagram
- [ ] Build the slides: problem, architecture, what BQ1, BQ2 and the agent each add, results and limits
- [ ] Assign speakers and run the timing rehearsals
- [ ] Check the brief for other requirements, including whether a written report is needed

## 6. Set-up guide, video edit and live demo
- [ ] Write the README set-up guide: the repo has no ETL or real-time code, so point to the scripts in `s3://is459-aml-g1t5/exported/etl-v2/final/` and `rt/code/`
- [ ] Have someone follow the guide to check it's clear
- [ ] Edit the recorded demo: set-up, one continuous run, narration or annotations, and waits cut
- [ ] Write the live demo script: 14-16 Sep queue, Investigate on the demo alert, real-time panel
- [ ] Prepare backups: cached results and the recording, in case AWS or the network fails
- [ ] Plan the clean-up after grading, in the order given in the handover

## Order of work

| Step | What | Waiting on |
|---|---|---|
| 1 | Output format | Nothing; do this first |
| 2 | Streamlit "can start now" list, S0 evaluation, set-up guide, diagram, set-up footage | Nothing; run in parallel |
| 3 | Agent milestones 2, 4 and 5; Streamlit agent screens with fake results | Step 1 |
| 4 | S1 and S2 evaluation; Streamlit on the real agent | Step 3 |
| 5 | Queue consumer and rank fix | Step 3, and Person 4 confirms 14-16 Sep results are saved |
| 6 | 18 Sep recording and video edit | Steps 4 and 5 |
| 7 | Final slides and live demo rehearsal | Steps 4 and 6 |

## Rules for everyone
- **18 Sep runs once.** Committed days can't be rerun. Agree the time in the group chat first. 19 Sep (244 transactions) is the only good backup.
- **Never rerun 14-16 Sep through the ETL, and don't change or delete v1.** v2 still reads v1, so it stays until after grading.
- **Results come from 14-16 Sep only.** 18 Sep has 353 transactions and no reasons, so it shows the pipeline works, not how good the model or agent is.
- **Labels stay out of the agent.** Only the evaluation module reads them.
- **Streamlit reads DynamoDB, never SQS.** Reading a queue takes messages away from the agent.
- **Stop what you start.** Run `python3 session.py stop` after every real-time session, and keep the ETL schedule disabled.
- **Watch spend.** About US$220 of the US$250 was left on 6 Nov.

## To confirm as a team
- Who takes each area
- The deadline and the time limits for the presentation and video
- Whether a written report is required
