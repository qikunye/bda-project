# Agent batch trigger (Fargate)

When the ETL state machine succeeds, the rule `is459-etl-v2-day-succeeded` starts the state machine
`is459-agent-batch`. It reads the day from `detail.input`, then runs two Fargate tasks from the same image, both
with `AGENT_LAST_DAY=<day>`: `scripts.rebuild_tables --yes` under `is459-agent-rebuild-role` (widens the agent tables
to the day; does nothing if they already hold it) and `scripts.run_batch --day <day> --spend-cap 20 --save` under
the label-free task role. A failure emails the SNS topic. See PLAN.md D40 and D43.

| File | What it is |
|---|---|
| `Dockerfile`, `requirements-batch.txt` | The image: Python 3.12 with only what the batch needs |
| `task-definition.json` | 1 vCPU, 4 GB, logs to `/ecs/is459-agent-batch` |
| `state_machine.asl.json` | Parse the day, check it, run the task, notify on failure |
| `iam/` | Five roles. The task role can read only the label-free agent tables and use Claude. The rebuild role can read Gold (which holds the label column) and write `agent/tables/`, and never runs the LLM |
| `deploy.sh`, `destroy.sh` | Create or remove everything (`DRY_RUN=1` prints the commands only) |
| `test_event.json` | A fake ETL event for 14 Sep (edit the day to test another) |

Redeploy after changing `src/` or `scripts/`: `infra/deploy.sh` (needs Docker running and AWS credentials).
Test: `aws stepfunctions start-execution --state-machine-arn arn:aws:states:us-east-1:941377154820:stateMachine:is459-agent-batch --input file://infra/test_event.json`.
That runs a real day: about US$11 of Bedrock, and `--save` replaces that day's rows.

Tested 8 Nov on 17 Sep: a forced rebuild under the rebuild role (14-16 Sep rank identically, 17 Sep added), then the
whole state machine from a fake 17 Sep event: the rebuild step correctly did nothing and the batch briefed
cases on Sonnet 4.6 until Sonnet 4.6's daily token quota ran out (the evaluation runs had used it), so the test
was stopped before saving. Bedrock's daily quotas are per model and reset about 24 h after use. Run new days one at a time. To rebuild by
hand: `AGENT_LAST_DAY=<day> python -m scripts.rebuild_tables --yes [--force]`.
