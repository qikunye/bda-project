#!/bin/bash
# One-table validation of a range of pipeline days against v1, plus checks that need no v1 output. Read-only.
# Usage: ./validate_range.sh 2022-09-14 2022-09-27
set +H
source "$(dirname "$0")/athena.sh"
F=$1; L=$2
S1=is459_aml_silver.li_medium_transactions_medium_live_01; S2=is459_aml_silver_v2.tx_v2; G=is459_aml_gold
W="between date '$F' and date '$L'"

echo "== Silver v2 vs v1 per day (rows, exact amount totals, v1 rows missing or different in v2)"
q "with a as (select transaction_id, date(\"timestamp\") d, \"timestamp\" ts, amount_paid from $S1 where date(\"timestamp\") $W),
b as (select transaction_id, date(\"timestamp\") d, \"timestamp\" ts, amount_paid from $S2 where date(\"timestamp\") $W)
select a.d, count(*) v1_rows, count(b.transaction_id) v2_matched, sum(a.amount_paid) v1_amt,
sum(case when b.transaction_id is null or a.ts<>b.ts or a.amount_paid<>b.amount_paid then 1 else 0 end) not_matched,
(select count(*) from b b2 where b2.d=a.d) v2_rows from a left join b on a.transaction_id=b.transaction_id group by a.d order by a.d"

echo "== Gold scores v2 vs v1 per day"
q "select coalesce(a.txn_date,b.txn_date) d, count(a.transaction_id) v1, count(b.transaction_id) v2,
sum(case when a.transaction_id is not null and b.transaction_id is not null then 1 else 0 end) joined,
max(abs(a.risk_score-b.risk_score)) max_diff,
sum(case when abs(a.risk_score-b.risk_score)>1e-6+1e-5*abs(a.risk_score) then 1 else 0 end) outside_tol,
sum(case when a.risk_rank<>b.risk_rank then 1 else 0 end) rank_diff
from (select * from $G.bq1_risk_scores_medium_v1 where txn_date $W) a full join (select * from $G.bq1_risk_scores_medium_v2 where txn_date $W) b
on a.transaction_id=b.transaction_id group by 1 order by 1"

echo "== Features and BQ2 edges per day (v1, v2, rows only in one side)"
q "with fa as (select txn_date d, count(*) n from $G.bq1_features_medium_v1 where txn_date $W group by 1),
fb as (select txn_date d, count(*) n from $G.bq1_features_medium_v2 where txn_date $W group by 1),
ea as (select txn_date d, transaction_id, source_account_key, target_account_key, amount_paid, transaction_timestamp from $G.bq2_graph_edges_medium_v1 where txn_date $W),
eb as (select txn_date d, transaction_id, source_account_key, target_account_key, amount_paid, transaction_timestamp from $G.bq2_graph_edges_medium_v2 where txn_date $W),
o1 as (select d, count(*) n from (select * from ea except select * from eb) group by d),
o2 as (select d, count(*) n from (select * from eb except select * from ea) group by d),
c1 as (select d, count(*) n from ea group by d), c2 as (select d, count(*) n from eb group by d)
select fa.d, fa.n feat_v1, fb.n feat_v2, c1.n edges_v1, c2.n edges_v2, coalesce(o1.n,0) only_v1, coalesce(o2.n,0) only_v2
from fa left join fb on fa.d=fb.d left join c1 on fa.d=c1.d left join c2 on fa.d=c2.d left join o1 on fa.d=o1.d left join o2 on fa.d=o2.d order by 1"

echo "== Checks that need no v1 output, per day"
q "with s as (select * from $G.bq1_risk_scores_medium_v2 where txn_date $W), x as (select *, date(\"timestamp\") d from $S2 where date(\"timestamp\") $W)
select x.d, count(*) silver_rows,
(select count(*) from s where s.txn_date=x.d) score_rows,
(select count(*) from s where s.txn_date=x.d and (risk_score is null or risk_rank is null or risk_score<0 or risk_score>1)) bad_scores,
(select count(*)-count(distinct transaction_id) from s where s.txn_date=x.d) dup_ids,
sum(case when transaction_id is null or \"timestamp\" is null or amount_paid is null or amount_paid<0 then 1 else 0 end) bad_silver
from x group by x.d order by 1"

echo "== Ledger and Silver total"
q "select count(*) loads, sum(row_count) ledger_rows, max(processed_at_utc) last_commit from is459_aml_silver_v2.bq1_v2_state_load where status='COMMITTED'"
q "select count(*) silver_v2_rows, count(distinct transaction_id) silver_v2_ids from $S2"

echo "== Audit result per day (status, rows, source proof match, DQ rules)"
for D in $(python3 -c "import datetime as d;a=d.date.fromisoformat('$F');b=d.date.fromisoformat('$L');print(' '.join((a+d.timedelta(i)).strftime('%Y%m%d') for i in range((b-a).days+1)))"); do
  K=$(aws s3 ls s3://is459-aml-g1t5/results/etl-v2/live-audit/ | awk '{print $4}' | grep "full-dq-$D-" | sort | tail -1)
  [ -n "$K" ] && aws s3 cp "s3://is459-aml-g1t5/results/etl-v2/live-audit/$K" - | python3 -c "import json,sys;r=json.load(sys.stdin);print('$D',r.get('status'),r.get('rows'),r.get('source_proof_match'),r.get('dq_rules_passed'),'$K')"
done
echo "== Streams (none expected)"
aws kinesis list-streams --query StreamNames --output text; aws firehose list-delivery-streams --query DeliveryStreamNames --output text
