# Agent batch trigger (Fargate)

When the ETL state machine succeeds, the rule `is459-etl-v2-day-succeeded` starts the state machine
`is459-agent-batch`. It reads the day from `detail.input`, then runs two Fargate tasks from the same image, both
with `AGENT_LAST_DAY=<day>`: `scripts.rebuild_tables --yes` under `is459-agent-rebuild-role` (widens the agent tables
to the day; does nothing if they already hold it) and `scripts.run_batch --day <day> --spend-cap 20 --save` under
the label-free task role. A failure emails the SNS topic. See the [architecture decisions](../docs/architecture/README.md).

| File | What it is |
|---|---|
| `Dockerfile`, `requirements-batch.txt` | The image: Python 3.12 with only what the batch needs |
| `task-definition.json` | 1 vCPU, 4 GB, logs to `/ecs/is459-agent-batch` |
| `state_machine.asl.json` | Parse and check the day, rebuild tables, investigate, notify on failure |
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

## Prerequisites and boundaries

Deployment requires Docker, AWS CLI credentials, existing Gold and explanation
sources, S3 data/query-output locations, Glue catalog, the existing ETL success
rule, a usable default VPC and model access. Review the account, region, network
and resource constants in `deploy.sh` before deploying to another environment.
The script deploys the agent batch only; ETL, BQ1 training and real-time
sources are in the sibling `etl_v2/`, `src/bq1/` and `realtime/` directories. This
agent deployment script does not deploy those components. CLI deployment is not a complete declarative platform
stack. `destroy.sh` removes the agent resources managed by this package.

The rebuild role is separate from the investigation role because rebuilding reads
Gold while investigations must read label-free tables. Rebuilds skip already
covered dates, refuse to remove later coverage and check existing daily ranks.
Agent tools are unavailable during rebuilding; overlapping new-day executions
are unsupported. The default `AGENT_LAST_DAY` is 16 September 2022; the state
machine passes the event day to both tasks. A hand-run investigation on extended
data must use matching `AGENT_LAST_DAY` coverage.

The CLI defaults to computation without publication; Athena and Bedrock still
incur charges in this mode. `--save` publishes decisions, cases and traces, replacing
the day's prior rows. LLM-call/spend failures block saving unless explicitly
allowed by `--allow-llm-failures`. No deployment or paid execution is part of
submission cleanup. Offline checks are documented in the [README](../README.md#getting-started).
