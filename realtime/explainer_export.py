#!/usr/bin/env python3
"""Export the explainer's training and evaluation candidates with read-only Athena queries (1 to 16 Sep).
Candidates = every laundering-labelled transaction plus every transaction at or above the live alert threshold.
Each row carries the 26 BQ1 features (Gold v1, identical to v2 on 14 to 16 Sep), the v1 risk score, and five context
numbers computed only from transactions strictly before it (same windows the explainer Lambda uses at run time).
The label column is exported ONLY so the notebook can build training targets; it is never an explainer input.
Usage: python3 explainer_export.py      -> s3://is459-aml-g1t5/rt/explainer-data/candidates.csv.gz"""
import time, gzip, json
import boto3

R, B = 'us-east-1', 'is459-aml-g1t5'
THRESH = 0.36046239733695984
ath, s3 = boto3.client('athena', region_name=R), boto3.client('s3', region_name=R)
FEATS = ['hour_of_day', 'day_of_week', 'log_amount_paid', 'payment_format', 'payment_currency', 'receiving_currency',
         'is_same_account', 'is_same_entity', 'is_same_entity_different_account', 'is_cross_entity', 'is_cross_bank', 'is_cross_currency',
         'snd_out_cnt_1h', 'snd_out_cnt_24h', 'snd_out_amt_24h_ccy', 'rcv_in_cnt_1h', 'rcv_in_cnt_24h', 'rcv_in_amt_24h_ccy', 'pair_prior_cnt',
         'is_new_pair', 'snd_distinct_rcv_to_date', 'rcv_distinct_snd_to_date', 'snd_prior_txn_cnt', 'snd_prior_mean_amt_ccy',
         'amt_ratio_to_snd_mean', 'snd_secs_since_last_out']
# The single query scanned Silver four times and hit the account's 10 GB per-query Athena limit, so the export
# stages a slim copy of Silver (1 to 16 Sep, needed columns only) and the candidate list as temporary Parquet tables
# under rt/explainer-data/tmp/ (database is459_rt), runs the context joins on those, then drops both tables and their files.
TMP_DB, TMP = 'is459_rt', 'rt/explainer-data/tmp/'
STAGE_S = """create table {db}.expl_tmp_s with (format = 'PARQUET', external_location = 's3://{b}/{tmp}s/') as
select transaction_id, cast(timestamp as timestamp(3)) as timestamp, sender_account_key, receiver_account_key, from_bank_norm, to_bank_norm,
       amount_paid, amount_received, cast(is_laundering as integer) as is_laundering
from is459_aml_silver_v2.tx_v2
where timestamp >= timestamp '2022-09-01 00:00:00' and timestamp < timestamp '2022-09-17 00:00:00'"""
STAGE_C = """create table {db}.expl_tmp_c with (format = 'PARQUET', external_location = 's3://{b}/{tmp}c/') as
select s.*, g.risk_score from {db}.expl_tmp_s s join
  (select transaction_id, risk_score from is459_aml_gold.bq1_risk_scores_medium_v1
   where txn_date between date '2022-09-01' and date '2022-09-16') g on s.transaction_id = g.transaction_id
where s.is_laundering = 1 or g.risk_score >= {thr}"""
SQL = """
with s as (select * from {db}.expl_tmp_s), c as (select * from {db}.expl_tmp_c),
ci as (select c.transaction_id, count(x.transaction_id) as n, count(distinct x.sender_account_key) as d
       from c left join s x on x.receiver_account_key = c.sender_account_key
        and x.timestamp >= c.timestamp - interval '1' day and x.timestamp < c.timestamp group by c.transaction_id),
co as (select c.transaction_id, count(x.transaction_id) as n, count(distinct x.receiver_account_key) as d
       from c left join s x on x.sender_account_key = c.receiver_account_key
        and x.timestamp >= c.timestamp - interval '1' day and x.timestamp < c.timestamp group by c.transaction_id),
cr as (select c.transaction_id, count(x.transaction_id) as n
       from c left join s x on x.sender_account_key = c.receiver_account_key and x.receiver_account_key = c.sender_account_key
        and x.timestamp >= c.timestamp - interval '4' day and x.timestamp < c.timestamp group by c.transaction_id)
select c.transaction_id, date_format(c.timestamp, '%Y-%m-%d %H:%i') as ts_minute, c.sender_account_key, c.receiver_account_key,
       c.from_bank_norm, c.to_bank_norm, cast(c.amount_paid as varchar) as amount_paid, cast(c.amount_received as varchar) as amount_received,
       c.is_laundering, c.risk_score, {feats},
       ci.n as ctx_snd_in_cnt_24h, ci.d as ctx_snd_in_distinct_24h, co.n as ctx_rcv_out_cnt_24h, co.d as ctx_rcv_out_distinct_24h,
       cr.n as ctx_reverse_pair_96h
from c join is459_aml_gold.bq1_features_medium_v1 f on f.transaction_id = c.transaction_id
     join ci on ci.transaction_id = c.transaction_id join co on co.transaction_id = c.transaction_id join cr on cr.transaction_id = c.transaction_id
where f.txn_date between date '2022-09-01' and date '2022-09-16'
order by c.timestamp, c.transaction_id
"""
FMT = dict(db=TMP_DB, b=B, tmp=TMP, thr=repr(THRESH), feats=', '.join('f.' + x for x in FEATS))


def cleanup():
    for t in ('expl_tmp_s', 'expl_tmp_c'):
        run('drop table if exists %s.%s' % (TMP_DB, t))
    for page in s3.get_paginator('list_objects_v2').paginate(Bucket=B, Prefix=TMP):
        keys = [{'Key': o['Key']} for o in page.get('Contents', [])]
        if keys:
            s3.delete_objects(Bucket=B, Delete={'Objects': keys})


def run(sql):
    qid = ath.start_query_execution(QueryString=sql, ResultConfiguration={'OutputLocation': 's3://%s/athena-results/rt/explainer/' % B})['QueryExecutionId']
    while True:
        st = ath.get_query_execution(QueryExecutionId=qid)['QueryExecution']
        if st['Status']['State'] in ('SUCCEEDED', 'FAILED', 'CANCELLED'):
            break
        time.sleep(3)
    if st['Status']['State'] != 'SUCCEEDED':
        raise SystemExit('query failed: ' + st['Status'].get('StateChangeReason', ''))
    return st


if __name__ == '__main__':
    t = time.time()
    cleanup()
    scanned = 0
    try:
        for q in (STAGE_S, STAGE_C):
            scanned += run(q.format(**FMT))['Statistics']['DataScannedInBytes']
        st = run(SQL.format(**FMT))
        scanned += st['Statistics']['DataScannedInBytes']
        loc = st['ResultConfiguration']['OutputLocation'].replace('s3://%s/' % B, '')
        body = s3.get_object(Bucket=B, Key=loc)['Body'].read()
    finally:
        cleanup()
    s3.put_object(Bucket=B, Key='rt/explainer-data/candidates.csv.gz', Body=gzip.compress(body), Tagging='project=is459-g1t5-rt')
    meta = {'rows': body.count(b'\n') - 1, 'scanned_mb': scanned // 1000000, 'seconds': round(time.time() - t),
            'threshold': THRESH, 'query_id': st['QueryExecutionId']}
    s3.put_object(Bucket=B, Key='rt/explainer-data/candidates_meta.json', Body=json.dumps(meta).encode(), Tagging='project=is459-g1t5-rt')
    print('EXPORT_DONE', json.dumps(meta))
