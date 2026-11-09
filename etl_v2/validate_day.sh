#!/bin/bash
# Check one pipeline day of ETL v2 against v1 with Athena. Usage: ./validate_day.sh 2022-09-17
# Works for small days too (17 to 27 Sep have v1 output). Also checks the daily lane created no Kinesis or Firehose.
# Needs athena.sh (the q function) in the same folder. Read-only: every query is a SELECT.
set +H
source "$(dirname "$0")/athena.sh"
D=$1
S1=is459_aml_silver.li_medium_transactions_medium_live_01; S2=is459_aml_silver_v2.tx_v2; G=is459_aml_gold

echo "== Silver v2 vs v1 for $D (rows, unique ids, exact amount total, rows missing or different in v2)"
q "with a as (select transaction_id, \"timestamp\" ts, amount_paid from $S1 where date(\"timestamp\")=date '$D'),
b as (select transaction_id, \"timestamp\" ts, amount_paid from $S2 where date(\"timestamp\")=date '$D')
select (select count(*) from a) v1_rows, (select count(*) from b) v2_rows, (select count(distinct transaction_id) from b) v2_ids,
(select sum(amount_paid) from a) v1_amt, (select sum(amount_paid) from b) v2_amt,
(select count(*) from a left join b on a.transaction_id=b.transaction_id where b.transaction_id is null or a.ts<>b.ts or a.amount_paid<>b.amount_paid) v1_rows_not_matched"

echo "== Silver v2 main total rows"
q "select count(*) main_rows from $S2"

echo "== Gold scores v2 vs v1"
q "select count(*) joined, (select count(*) from $G.bq1_risk_scores_medium_v2 where txn_date=date '$D') v2_rows,
(select count(*) from $G.bq1_risk_scores_medium_v1 where txn_date=date '$D') v1_rows,
max(abs(a.risk_score-b.risk_score)) max_diff, sum(case when abs(a.risk_score-b.risk_score)>1e-6+1e-5*abs(a.risk_score) then 1 else 0 end) outside_tol,
sum(case when a.risk_rank<>b.risk_rank then 1 else 0 end) rank_diff
from $G.bq1_risk_scores_medium_v1 a join $G.bq1_risk_scores_medium_v2 b on a.transaction_id=b.transaction_id
where a.txn_date=date '$D' and b.txn_date=date '$D'"

echo "== Gold features rows"
q "select (select count(*) from $G.bq1_features_medium_v1 where txn_date=date '$D') v1, (select count(*) from $G.bq1_features_medium_v2 where txn_date=date '$D') v2"

echo "== BQ2 edges v2 vs v1 (rows and rows that differ either way)"
q "with a as (select transaction_id, source_account_key, target_account_key, amount_paid, transaction_timestamp from $G.bq2_graph_edges_medium_v1 where txn_date=date '$D'),
b as (select transaction_id, source_account_key, target_account_key, amount_paid, transaction_timestamp from $G.bq2_graph_edges_medium_v2 where txn_date=date '$D')
select (select count(*) from a) v1, (select count(*) from b) v2, (select count(*) from (select * from a except select * from b)) only_v1, (select count(*) from (select * from b except select * from a)) only_v2"

echo "== Standalone checks (need no v1 output, so they also cover days v1 never scored)"
q "with s as (select * from $G.bq1_risk_scores_medium_v2 where txn_date=date '$D'),
x as (select * from $S2 where date(\"timestamp\")=date '$D')
select (select count(*) from x) silver_rows, (select count(*) from s) score_rows,
(select count(*) from $G.bq1_features_medium_v2 where txn_date=date '$D') feature_rows,
(select count(*) from s where risk_score is null or risk_rank is null or transaction_id is null) score_nulls,
(select count(*) from s where risk_score<0 or risk_score>1) score_out_of_range,
(select count(*) - count(distinct transaction_id) from s) dup_score_ids,
(select count(*) - count(distinct risk_rank) from s) dup_ranks,
(select coalesce(min(risk_rank),0) from s) min_rank, (select coalesce(max(risk_rank),0) from s) max_rank,
(select count(*) from s where transaction_id not in (select transaction_id from x)) scores_not_in_silver,
(select count(*) from x where transaction_id is null or \"timestamp\" is null or amount_paid is null or amount_paid<0) silver_bad_rows,
(select count(*) from $G.bq2_graph_edges_medium_v2 where txn_date=date '$D' and transaction_id not in (select transaction_id from x)) edges_not_in_silver"
echo "(pass = score_rows = feature_rows = silver_rows and every *_nulls/out_of_range/dup/not_in/bad = 0; risk_rank is the rank across all scored days, so min/max are for reference only)"

echo "== BQ1 ledger"
q "select count(*) loads, sum(row_count) ledger_rows, max(processed_at_utc) last_commit from is459_aml_silver_v2.bq1_v2_state_load where status='COMMITTED'"

echo "== Case Graph: downstream edges on $D from the receiver of the top alert of the day before"
q "with ta as (select target_account_key k, transaction_timestamp t from $G.bq1_risk_scores_medium_v2 where txn_date=date '$D' - interval '1' day order by risk_rank limit 1)
select ta.k, ta.t, count(e.transaction_id) downstream_edges_on_day from ta left join $G.bq2_graph_edges_medium_v2 e
on e.txn_date=date '$D' and e.source_account_key=ta.k and e.transaction_timestamp between ta.t and ta.t + interval '48' hour group by ta.k, ta.t"

echo "== Source proof recorded by the audit job for $D (rows, unique ids, per-currency totals vs the branch)"
DC=${D//-/}
K=$(aws s3 ls s3://is459-aml-g1t5/results/etl-v2/live-audit/ | awk '{print $4}' | grep "full-dq-$DC-" | sort | tail -1)
[ -n "$K" ] && aws s3 cp "s3://is459-aml-g1t5/results/etl-v2/live-audit/$K" - | python3 -c "import json,sys;r=json.load(sys.stdin);print(json.dumps({k:r.get(k) for k in ('status','day','rows','source_proof','source_proof_match','dq_rules_passed','audit_snapshot_id','error')},indent=1))"

echo "== No streaming resources from the daily lane (only the RT stream may exist, and only during an RT session)"
aws kinesis list-streams --query 'StreamNames' --output text
aws firehose list-delivery-streams --query 'DeliveryStreamNames' --output text
S=$(aws kinesis list-streams --query 'StreamNames[?@!=`is459-rt-stream`]' --output text); F=$(aws firehose list-delivery-streams --query 'DeliveryStreamNames' --output text)
if [ -z "$S$F" ]; then echo "NO_DAILY_STREAMS_OK"; else echo "UNEXPECTED_STREAMS: $S $F"; fi
