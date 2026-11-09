# ETL operational utilities

All six utilities are retained after dependency review. Run from `etl_v2/` as
specified in the [runbook](../docs/runbook.md). These tools use the existing AWS
environment; none is an offline test or part of repository cleanup execution.

| File | Classification | Purpose / effects |
|---|---|---|
| `deploy_backfill.py` | Deployment and ordered recovery | Deploys/runs/checks a sequential multi-day Step Functions workflow; deploy/run mutate AWS |
| `rollback_days_job.py` | Historical one-off with recovery evidence | Fixed 18–27 September rollback, with guarded snapshot IDs and undo record; mutates Iceberg/catalog; retained to explain and reproduce the historical recovery, not as a general rollback command |
| `validate_range.sh` | Reproducible verification | Athena range parity checks against v1 plus independent invariants; queries incur costs |
| `review_checks.sh` | Reproducible verification | Inspects integration resources, alarms, tables, branches and brief metadata; includes Athena queries |
| `status.sh` | Operational verification | Inspects workflow, schedule, ledger and streaming status |
| `fetch_agent_code.sh` | Deployment inspection / recovery | Downloads agent image layers into `~/agent` and local temporary storage without Docker; useful for identifying deployed source independently of this checkout |

No file was established to be obsolete. The snapshot-bound rollback is explicitly
historical, while its code and the [backfill/rollback evidence](../docs/evidence.md#backfill-and-rollback-of-18-to-27-sep-7-nov-2026)
remain relevant to academic review. The old SQS hook inspected by some tools is
historical; current agent deployment is owned by [infra](../../infra/README.md).
