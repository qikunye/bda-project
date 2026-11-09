#!/bin/bash
# Read-only readiness checks for the pieces around the daily lane: agent hooks and the real-time lane.
set +H
source "$(dirname "$0")/athena.sh"
A=941377154820
echo "== Agent views: rows per view and no label column"
q "select (select count(*) from is459_agent.v_bq1_risk_scores) scores, (select count(*) from is459_agent.v_bq1_features) features, (select count(*) from is459_agent.v_bq2_graph_edges) edges,
(select max(txn_date) from is459_agent.v_bq1_risk_scores) last_scored_day"
q "select table_name, count(*) label_like_cols from information_schema.columns where table_schema='is459_agent' and (lower(column_name) like '%launder%' or lower(column_name) in ('label','is_laundering','y')) group by 1"
echo "== Agent hook resources"
aws events list-targets-by-rule --rule is459-etl-v2-day-succeeded --query 'Targets[].Arn' --output text
aws dynamodb describe-table --table-name is459_agent_decisions --query 'Table.[TableStatus,KeySchema[0].AttributeName,ItemCount]' --output text
aws iam get-policy --policy-arn arn:aws:iam::$A:policy/is459-agent-readonly --query 'Policy.[PolicyName,DefaultVersionId,AttachmentCount]' --output text
echo "== Real-time lane resources"
for f in $(aws lambda list-functions --query 'Functions[?starts_with(FunctionName,`is459`)].FunctionName' --output text); do
  echo "$(aws lambda get-function-configuration --function-name $f --query '[FunctionName,State,LastUpdateStatus,MemorySize,Timeout]' --output text) $(aws lambda get-function-configuration --function-name $f --query 'Environment.Variables' --output json | tr -d '\n ' | grep -oiE '"[A-Z_]*THRESHOLD[A-Z_]*":"[^"]*"')"
done
for t in $(aws dynamodb list-tables --query 'TableNames' --output text | tr '\t' '\n' | grep is459); do
  echo "$t $(aws dynamodb describe-table --table-name $t --query 'Table.[TableStatus,ItemCount]' --output text)"; done
aws pipes list-pipes --query 'Pipes[?starts_with(Name,`is459`)].[Name,CurrentState]' --output text
aws cloudwatch describe-alarms --alarm-name-prefix is459 --query 'MetricAlarms[].[AlarmName,StateValue]' --output text
echo "== Explainer briefs and RT alerts in Athena"
q "select count(*) briefs, max(created_at) last_brief from is459_rt.alert_briefs" 2>&1 | tail -2
echo "== Leftover Iceberg audit branches (should be none)"
aws s3 ls s3://is459-aml-g1t5/results/etl-v2/live-audit/ | tail -3
