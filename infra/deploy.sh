#!/usr/bin/env bash
# Deploys the agent batch trigger: image, Fargate task, state machine and the extra target on the ETL's rule.
# Run from bda-project/ with AWS credentials for account 941377154820 (us-east-1).
#   DRY_RUN=1 infra/deploy.sh    prints every command and changes nothing
#   infra/deploy.sh              creates the resources (tagged project=is459-g1t5-etl-v2)
set -euo pipefail

REGION=us-east-1
ACCOUNT_ID=941377154820
TAG_KEY=project
TAG_VALUE=is459-g1t5-etl-v2
REPO=is459-agent-batch
CLUSTER=is459-agent
LOG_GROUP=/ecs/is459-agent-batch
STATE_MACHINE=is459-agent-batch
RULE=is459-etl-v2-day-succeeded
IMAGE="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${REPO}:latest"
WORK="$(mktemp -d)"

run() { echo "+ $*"; if [ "${DRY_RUN:-0}" != "1" ]; then "$@"; fi; }
fill() { sed -e "s/__ACCOUNT_ID__/${ACCOUNT_ID}/g" -e "s#__IMAGE__#${IMAGE}#g" "$@"; }

[ "$(aws sts get-caller-identity --query Account --output text)" = "${ACCOUNT_ID}" ] || { echo "Wrong AWS account"; exit 1; }

echo "== Network: default VPC subnets and security group"
VPC=$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)
SUBNETS=$(aws ec2 describe-subnets --filters Name=vpc-id,Values="$VPC" --query 'Subnets[].SubnetId' --output json | tr -d ' \n')
SG=$(aws ec2 describe-security-groups --filters Name=vpc-id,Values="$VPC" Name=group-name,Values=default --query 'SecurityGroups[0].GroupId' --output json | sed 's/^/[/;s/$/]/')
echo "subnets=$SUBNETS security_group=$SG"

echo "== Image"
if ! aws ecr describe-repositories --repository-names "$REPO" >/dev/null 2>&1; then
  run aws ecr create-repository --repository-name "$REPO" --tags Key=$TAG_KEY,Value=$TAG_VALUE
fi
if [ "${DRY_RUN:-0}" != "1" ]; then
  aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"
fi
run docker build --platform linux/amd64 -f infra/Dockerfile -t "$IMAGE" .
run docker push "$IMAGE"

echo "== Roles"
make_role() {  # name trust-file policy-file
  if ! aws iam get-role --role-name "$1" >/dev/null 2>&1; then
    run aws iam create-role --role-name "$1" --assume-role-policy-document "file://infra/iam/$2" --tags Key=$TAG_KEY,Value=$TAG_VALUE
  fi
  if [ -n "${3:-}" ]; then
    fill "infra/iam/$3" > "$WORK/$3"
    run aws iam put-role-policy --role-name "$1" --policy-name inline --policy-document "file://$WORK/$3"
  fi
}
make_role is459-agent-task-exec-role trust-ecs-tasks.json
run aws iam attach-role-policy --role-name is459-agent-task-exec-role --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
make_role is459-agent-task-role trust-ecs-tasks.json task-role-policy.json
make_role is459-agent-rebuild-role trust-ecs-tasks.json rebuild-role-policy.json   # D43: table rebuild only, can read Gold
make_role is459-agent-sfn-role trust-states.json states-role-policy.json
make_role is459-agent-events-role trust-events.json events-role-policy.json
[ "${DRY_RUN:-0}" = "1" ] || sleep 10   # let IAM settle

echo "== Logs, cluster, task definition"
run aws logs create-log-group --log-group-name "$LOG_GROUP" --tags $TAG_KEY=$TAG_VALUE 2>/dev/null || true
run aws logs put-retention-policy --log-group-name "$LOG_GROUP" --retention-in-days 30
if ! aws ecs describe-clusters --clusters "$CLUSTER" --query 'clusters[?status==`ACTIVE`]' --output text | grep -q .; then
  run aws ecs create-cluster --cluster-name "$CLUSTER" --tags key=$TAG_KEY,value=$TAG_VALUE
fi
fill infra/task-definition.json > "$WORK/task-definition.json"
run aws ecs register-task-definition --cli-input-json "file://$WORK/task-definition.json" --tags key=$TAG_KEY,value=$TAG_VALUE

echo "== State machine"
fill infra/state_machine.asl.json | sed -e "s#__SUBNETS__#${SUBNETS}#" -e "s#__SECURITY_GROUPS__#${SG}#" > "$WORK/state_machine.json"
SM_ARN="arn:aws:states:${REGION}:${ACCOUNT_ID}:stateMachine:${STATE_MACHINE}"
SFN_ROLE="arn:aws:iam::${ACCOUNT_ID}:role/is459-agent-sfn-role"
if aws stepfunctions describe-state-machine --state-machine-arn "$SM_ARN" >/dev/null 2>&1; then
  run aws stepfunctions update-state-machine --state-machine-arn "$SM_ARN" --definition "file://$WORK/state_machine.json" --role-arn "$SFN_ROLE"
else
  run aws stepfunctions create-state-machine --name "$STATE_MACHINE" --definition "file://$WORK/state_machine.json" --role-arn "$SFN_ROLE" --type STANDARD --tags key=$TAG_KEY,value=$TAG_VALUE
fi

echo "== Extra target on the ETL's rule (the existing SQS target is left alone)"
run aws events put-targets --rule "$RULE" --targets \
  "Id=agent-batch,Arn=${SM_ARN},RoleArn=arn:aws:iam::${ACCOUNT_ID}:role/is459-agent-events-role,RetryPolicy={MaximumRetryAttempts=5,MaximumEventAgeInSeconds=3600}"

echo "Done. Test: aws stepfunctions start-execution --state-machine-arn ${SM_ARN} --input file://infra/test_event.json"
