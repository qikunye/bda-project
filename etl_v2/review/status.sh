#!/bin/bash
# Read-only live status of the IS459 ETL v2 + real-time side. Usage: ./status.sh  (run from the folder holding athena.sh)
set +H
source "$(dirname "$0")/athena.sh"
A=941377154820; R=us-east-1
echo "== Step Functions state machines"
aws stepfunctions list-state-machines --query "stateMachines[?starts_with(name,'is459')].name" --output text | tr '\t' '\n'
for SM in is459-etl-v2-silver-wap is459-rt-session; do
  echo "== Last executions: $SM"
  aws stepfunctions list-executions --state-machine-arn arn:aws:states:$R:$A:stateMachine:$SM --max-results 6 \
    --query 'executions[].[name,status,startDate]' --output text
done
echo "== Schedules"
aws scheduler list-schedules --query "Schedules[?starts_with(Name,'is459')].[Name,State]" --output text
aws events list-rules --name-prefix is459 --query 'Rules[].[Name,State]' --output text
echo "== Glue jobs (is459*) and any running"
aws glue list-jobs --max-results 200 --query 'JobNames' --output text | tr '\t' '\n' | grep -i is459 | sort
echo "== Streams (should be empty outside an RT session)"
aws kinesis list-streams --query StreamNames --output text; aws firehose list-delivery-streams --query DeliveryStreamNames --output text
echo "== SQS depth"
for Q in $(aws sqs list-queues --queue-name-prefix is459 --query QueueUrls --output text); do
  echo "$(basename $Q) $(aws sqs get-queue-attributes --queue-url $Q --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible --query 'Attributes' --output text)"
done
echo "== Lambda aliases (explainer) and RT scorer config"
aws lambda list-aliases --function-name is459-rt-explainer --query 'Aliases[].[Name,FunctionVersion]' --output text
echo "== Budgets"
aws budgets describe-budgets --account-id $A --query 'Budgets[].[BudgetName,BudgetLimit.Amount,CalculatedSpend.ActualSpend.Amount]' --output text
echo "== Silver v1 rows per day (source coverage) vs Silver v2"
q "select d, v1, coalesce(v2,0) v2 from (select date(\"timestamp\") d, count(*) v1 from is459_aml_silver.li_medium_transactions_medium_live_01 group by 1) a
left join (select date(\"timestamp\") d2, count(*) v2 from is459_aml_silver_v2.tx_v2 group by 1) b on a.d=b.d2 order by d"
echo "== Gold v2 score rows per day vs v1"
q "select coalesce(a.txn_date,b.txn_date) d, a.n v1, b.n v2 from (select txn_date, count(*) n from is459_aml_gold.bq1_risk_scores_medium_v1 group by 1) a
full join (select txn_date, count(*) n from is459_aml_gold.bq1_risk_scores_medium_v2 group by 1) b on a.txn_date=b.txn_date order by 1"
echo "== Ledger"
q "select count(*) loads, sum(row_count) ledger_rows, max(processed_at_utc) last_commit from is459_aml_silver_v2.bq1_v2_state_load where status='COMMITTED'"
q "select count(*) silver_v2_rows from is459_aml_silver_v2.tx_v2"
