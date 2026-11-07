# Agent batch trigger (Fargate)

When the ETL state machine succeeds, the rule `is459-etl-v2-day-succeeded` starts the state machine
`is459-agent-batch`. It reads the day from `detail.input`, runs `scripts.run_batch --day <day> --spend-cap 20 --save`
as a Fargate task, and emails the SNS failure topic if it fails. See PLAN.md D40.

| File | What it is |
|---|---|
| `Dockerfile`, `requirements-batch.txt` | The image: Python 3.12 with only what the batch needs |
| `task-definition.json` | 1 vCPU, 4 GB, logs to `/ecs/is459-agent-batch` |
| `state_machine.asl.json` | Parse the day, check it, run the task, notify on failure |
| `iam/` | Four roles. The task role can read only the label-free agent tables and use Claude |
| `deploy.sh`, `destroy.sh` | Create or remove everything (`DRY_RUN=1` prints the commands only) |
| `test_event.json` | A fake ETL event for 14 Sep |

Redeploy after changing `src/` or `scripts/`: `infra/deploy.sh` (needs Docker running and AWS credentials).
Test: `aws stepfunctions start-execution --state-machine-arn arn:aws:states:us-east-1:941377154820:stateMachine:is459-agent-batch --input file://infra/test_event.json`.
That runs a real day: about US$11 of Bedrock, and `--save` replaces that day's rows.

Not done yet: rebuilding the agent tables for a new day before the batch (PLAN.md section 19, step 6).
