#!/usr/bin/env python3
"""Validate one real-time run.
  python3 validate_rt.py guard-before <run_id>          fingerprint everything v1/v2 (S3 outside rt/ and athena-results/, Iceberg pointers)
  python3 validate_rt.py check <run_id> rtdata/<tag>   parity vs v2 Gold, latency, alerts, state checksum, guard-after
Parity uses v2's own tolerances. Read-only towards v1/v2."""
import sys, csv, json, hashlib, time, statistics
from concurrent.futures import ThreadPoolExecutor
import boto3
sys.path.insert(0, '.')
import rt_core as C

R, B = 'us-east-1', 'is459-aml-g1t5'
s3, ddb, glue, sqs = (boto3.client(x, region_name=R) for x in ('s3', 'dynamodb', 'glue', 'sqs'))
cmd, run = sys.argv[1], sys.argv[2]


def fingerprint():
    h, n = hashlib.sha256(), 0
    for p in s3.get_paginator('list_objects_v2').paginate(Bucket=B):
        for o in p.get('Contents', []):
            if o['Key'].startswith(('rt/', 'athena-results/')):
                continue
            h.update(('%s|%s|%s\n' % (o['Key'], o['ETag'], o['LastModified'].isoformat())).encode()); n += 1
    ice = {}
    for db, t in [('is459_aml_silver_v2', x) for x in ('tx_v2', 'bq1_v2_state_pair', 'bq1_v2_state_sender', 'bq1_v2_state_sender_ccy', 'bq1_v2_state_load')]:
        ice[db + '.' + t] = glue.get_table(DatabaseName=db, Name=t)['Table']['Parameters'].get('metadata_location')
    return {'objects': n, 's3_sha256': h.hexdigest(), 'iceberg': ice}


def scan(table):
    def seg(i):
        out, kw = [], {'TableName': table, 'Segment': i, 'TotalSegments': 8}
        while True:
            r = ddb.scan(**kw)
            out += r['Items']
            if 'LastEvaluatedKey' not in r:
                return out
            kw['ExclusiveStartKey'] = r['LastEvaluatedKey']
    with ThreadPoolExecutor(8) as ex:
        return [x for part in ex.map(seg, range(8)) for x in part]


def put(name, doc):
    body = json.dumps(doc, indent=1, default=str).encode()
    s3.put_object(Bucket=B, Key='rt/runs/%s/%s.json' % (run, name), Body=body, Tagging='project=is459-g1t5-rt')
    open('rt_%s_%s.json' % (run, name), 'wb').write(body)


if cmd == 'guard-before':
    put('guard_before', fingerprint())
    print('GUARD_BEFORE saved')
    sys.exit()

d = sys.argv[3]
scores = {x['txn']['S']: x for x in scan('is459_rt_scores')}
with open(d + '/slice.csv', newline='') as fh:
    slice_ids = [r['transaction_id'] for r in csv.DictReader(fh)]
with open(d + '/gold_features.csv', newline='') as fh:
    gf = {x['transaction_id']: x for x in csv.DictReader(fh)}
with open(d + '/gold_scores.csv', newline='') as fh:
    gs = {x['transaction_id']: float(x['risk_score']) for x in csv.DictReader(fh)}
per, examples, n, sc_bad, sc_max, lat = {c: 0 for c in C.FEATURES}, [], 0, 0, 0.0, []
for tid in slice_ids:
    x = scores.get(tid)
    if x is None:
        continue
    n += 1
    f, g = json.loads(x['f']['S']), gf[tid]
    for c in C.FEATURES:
        a, b = f[c], g[c]
        if c in ('payment_format', 'payment_currency', 'receiving_currency'):
            ok = (a or '__other__') == (b or '__other__')
        elif c in C.FLAGS or c == 'is_new_pair':
            ok = int(a or 0) == C.flag(b)
        else:
            b = None if b == '' else float(b)
            ok = (a is None and b is None) or (a is not None and b is not None and abs(float(a) - b) <= 1e-6 + 1e-9 * abs(b))
        if not ok:
            per[c] += 1
            if len(examples) < 10: examples.append([tid, c, a, b])
    s = float(x['score']['N']); diff = abs(s - gs[tid]); sc_max = max(sc_max, diff)
    sc_bad += diff > 1e-6 + 1e-5 * abs(gs[tid])
    lat.append(int(x['ingest_to_score_ms']['N']))
lat.sort()
pct = lambda p: lat[min(len(lat) - 1, int(p * len(lat)))] if lat else None
state = scan('is459_rt_state')
h = hashlib.sha256()
for it in sorted(state, key=lambda i: i['pk']['S']):
    h.update((it['pk']['S'] + '|' + it['j']['S'] + '\n').encode())
alerts = scan('is459_rt_alerts')
q = sqs.get_queue_url(QueueName='is459-rt-alert-events')['QueueUrl']
qn = int(sqs.get_queue_attributes(QueueUrl=q, AttributeNames=['ApproximateNumberOfMessages'])['Attributes']['ApproximateNumberOfMessages'])
sample = sqs.receive_message(QueueUrl=q, MaxNumberOfMessages=1, VisibilityTimeout=5).get('Messages', [])
allow = max(10, int(n * 1e-4))
res = {'run_id': run, 'slice_rows': len(slice_ids), 'scored_rows_in_slice': n, 'scores_table_rows': len(scores),
       'feature_mismatches': sum(per.values()), 'per_feature': {k: v for k, v in per.items() if v}, 'examples': examples,
       'score_outside_tol': int(sc_bad), 'score_allowance': allow, 'score_max_abs_diff': sc_max,
       'latency_ms': {'p50': pct(0.50), 'p95': pct(0.95), 'p99': pct(0.99), 'max': lat[-1] if lat else None, 'n': len(lat)},
       'alerts_rows': len(alerts), 'alert_events_in_queue': qn, 'alert_event_sample': json.loads(sample[0]['Body']) if sample else None,
       'state_items': len(state), 'state_sha256': h.hexdigest()}
res['parity'] = 'PASS' if n == len(slice_ids) and res['feature_mismatches'] == 0 and sc_bad <= allow and sc_max <= 0.01 else 'FAIL'
try:
    before = json.loads(s3.get_object(Bucket=B, Key='rt/runs/%s/guard_before.json' % run)['Body'].read())
    after = fingerprint()
    res['v1_v2_untouched'] = before == after
except s3.exceptions.NoSuchKey:
    res['v1_v2_untouched'] = 'no guard_before'
put('validate', res)
print(json.dumps({k: v for k, v in res.items() if k != 'alert_event_sample'}, indent=1))
print('SAMPLE_EVENT', json.dumps(res['alert_event_sample'])[:900])
