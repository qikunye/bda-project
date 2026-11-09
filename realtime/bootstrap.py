#!/usr/bin/env python3
"""Pull one replay slice and its starting state from Silver v2 with read-only Athena queries.
Usage: python3 bootstrap.py 2022-09-16T01:00:00 2022-09-16T02:00:00 <tag>
Writes CSVs to ./rtdata/<tag>/ and s3://is459-aml-g1t5/rt/bootstrap/<tag>/. Never selects is_laundering."""
import sys, time, os, csv, io, json
import boto3

B = 'is459-aml-g1t5'; R = 'us-east-1'; T = 'is459_aml_silver_v2.tx_v2'; G = 'is459_aml_gold'
S, E, TAG = sys.argv[1].replace('T', ' '), sys.argv[2].replace('T', ' '), sys.argv[3]
ath = boto3.client('athena', region_name=R); s3 = boto3.client('s3', region_name=R)
OUT = 's3://%s/athena-results/rt/%s/' % (B, TAG)
os.makedirs('rtdata/' + TAG, exist_ok=True)
SL = "(select * from %s where timestamp >= timestamp '%s' and timestamp < timestamp '%s')" % (T, S, E)
H = "(select * from %s where timestamp < timestamp '%s')" % (T, S)
W = "(select * from %s where timestamp >= timestamp '%s' - interval '1' day and timestamp < timestamp '%s')" % (T, S, S)
ms = "cast(to_unixtime(timestamp) as bigint)"
FLAGS = ['is_same_account', 'is_same_entity', 'is_same_entity_different_account', 'is_cross_entity', 'is_cross_bank', 'is_cross_currency']
FEATS = ['hour_of_day', 'day_of_week', 'log_amount_paid', 'payment_format', 'payment_currency', 'receiving_currency'] + FLAGS + [
    'snd_out_cnt_1h', 'snd_out_cnt_24h', 'snd_out_amt_24h_ccy', 'rcv_in_cnt_1h', 'rcv_in_cnt_24h', 'rcv_in_amt_24h_ccy', 'pair_prior_cnt',
    'is_new_pair', 'snd_distinct_rcv_to_date', 'rcv_distinct_snd_to_date', 'snd_prior_txn_cnt', 'snd_prior_mean_amt_ccy',
    'amt_ratio_to_snd_mean', 'snd_secs_since_last_out']
Q = {
    'slice': "select transaction_id, date_format(timestamp, '%%Y-%%m-%%d %%H:%%i:%%s') as timestamp, sender_account_key, receiver_account_key, "
             "sender_entity_id, receiver_entity_id, from_bank_norm, to_bank_norm, cast(amount_paid as varchar) amount_paid, "
             "cast(amount_received as varchar) amount_received, payment_currency, receiving_currency, payment_format, %s "
             "from %s order by timestamp, transaction_id" % (', '.join('cast(%s as varchar) %s' % (f, f) for f in FLAGS), SL),
    'pairs': "select h.sender_account_key, h.receiver_account_key, count(*) from %s h join (select distinct sender_account_key, receiver_account_key from %s) p "
             "on h.sender_account_key = p.sender_account_key and h.receiver_account_key = p.receiver_account_key group by 1, 2" % (H, SL),
    'senders': "select sender_account_key, count(*), max(%s), count(distinct receiver_account_key) from %s "
               "where sender_account_key in (select sender_account_key from %s) group by 1" % (ms, H, SL),
    'receivers': "select receiver_account_key, count(distinct sender_account_key) from %s "
                 "where receiver_account_key in (select receiver_account_key from %s) group by 1" % (H, SL),
    'sender_ccy': "select h.sender_account_key, h.payment_currency, count(*), sum(cast(h.amount_paid as double)) from %s h join "
                  "(select distinct sender_account_key, payment_currency from %s) k on h.sender_account_key = k.sender_account_key "
                  "and h.payment_currency = k.payment_currency group by 1, 2" % (H, SL),
    'snd_win': "select sender_account_key, %s, count(*) from %s where sender_account_key in (select sender_account_key from %s) group by 1, 2" % (ms, W, SL),
    'sc_win': "select h.sender_account_key, h.payment_currency, %s, sum(cast(h.amount_paid as double)) from %s h join "
              "(select distinct sender_account_key, payment_currency from %s) k on h.sender_account_key = k.sender_account_key "
              "and h.payment_currency = k.payment_currency group by 1, 2, 3" % (ms.replace('timestamp', 'h.timestamp'), W, SL),
    'rcv_win': "select receiver_account_key, %s, count(*) from %s where receiver_account_key in (select receiver_account_key from %s) group by 1, 2" % (ms, W, SL),
    'rc_win': "select h.receiver_account_key, h.receiving_currency, %s, sum(cast(h.amount_received as double)) from %s h join "
              "(select distinct receiver_account_key, receiving_currency from %s) k on h.receiver_account_key = k.receiver_account_key "
              "and h.receiving_currency = k.receiving_currency group by 1, 2, 3" % (ms.replace('timestamp', 'h.timestamp'), W, SL),
    'gold_features': "select f.transaction_id, %s from %s.bq1_features_medium_v2 f join %s s on f.transaction_id = s.transaction_id "
                     "where f.txn_date between date '%s' and date '%s'" % (', '.join('f.' + c for c in FEATS), G, SL, S[:10], E[:10]),
    'gold_scores': "select g.transaction_id, g.risk_score, g.risk_rank from %s.bq1_risk_scores_medium_v2 g join %s s on g.transaction_id = s.transaction_id "
                   "where g.txn_date between date '%s' and date '%s'" % (G, SL, S[:10], E[:10]),
}


def run(name, sql):
    qid = ath.start_query_execution(QueryString=sql, ResultConfiguration={'OutputLocation': OUT})['QueryExecutionId']
    while True:
        st = ath.get_query_execution(QueryExecutionId=qid)['QueryExecution']
        if st['Status']['State'] in ('SUCCEEDED', 'FAILED', 'CANCELLED'):
            break
        time.sleep(2)
    if st['Status']['State'] != 'SUCCEEDED':
        raise RuntimeError(name + ' ' + st['Status'].get('StateChangeReason', ''))
    loc = st['ResultConfiguration']['OutputLocation'].replace('s3://' + B + '/', '')
    body = s3.get_object(Bucket=B, Key=loc)['Body'].read()
    open('rtdata/%s/%s.csv' % (TAG, name), 'wb').write(body)
    s3.put_object(Bucket=B, Key='rt/bootstrap/%s/%s.csv' % (TAG, name), Body=body, Tagging='project=is459-g1t5-rt')
    rows = body.count(b'\n') - 1
    print('%-14s rows=%-8d scannedMB=%d' % (name, rows, st['Statistics']['DataScannedInBytes'] // 1000000), flush=True)
    return rows


meta = {'slice_start': S, 'slice_end': E, 'tag': TAG, 'rows': {}}
for n, q in Q.items():
    meta['rows'][n] = run(n, q)
open('rtdata/%s/meta.json' % TAG, 'w').write(json.dumps(meta, indent=1))
s3.put_object(Bucket=B, Key='rt/bootstrap/%s/meta.json' % TAG, Body=json.dumps(meta, indent=1).encode(), Tagging='project=is459-g1t5-rt')
print('BOOTSTRAP_DONE', json.dumps(meta['rows']))
