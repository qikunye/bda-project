#!/usr/bin/env bash
# Removes everything infra/deploy.sh created. Leaves the ETL's rule and queue alone, except for our target on the rule.
set -uo pipefail
REGION=us-east-1; ACCOUNT_ID=941377154820
run() { echo "+ $*"; if [ "${DRY_RUN:-0}" != "1" ]; then "$@"; fi; }

run aws events remove-targets --rule is459-etl-v2-day-succeeded --ids agent-batch
run aws stepfunctions delete-state-machine --state-machine-arn "arn:aws:states:${REGION}:${ACCOUNT_ID}:stateMachine:is459-agent-batch"
for arn in $(aws ecs list-task-definitions --family-prefix is459-agent-batch --query 'taskDefinitionArns[]' --output text); do
  run aws ecs deregister-task-definition --task-definition "$arn" >/dev/null
done
run aws ecs delete-cluster --cluster is459-agent
run aws logs delete-log-group --log-group-name /ecs/is459-agent-batch
run aws ecr delete-repository --repository-name is459-agent-batch --force
for role in is459-agent-task-exec-role is459-agent-task-role is459-agent-rebuild-role is459-agent-sfn-role is459-agent-events-role; do
  for p in $(aws iam list-attached-role-policies --role-name "$role" --query 'AttachedPolicies[].PolicyArn' --output text); do
    run aws iam detach-role-policy --role-name "$role" --policy-arn "$p"
  done
  run aws iam delete-role-policy --role-name "$role" --policy-name inline
  run aws iam delete-role --role-name "$role"
done
