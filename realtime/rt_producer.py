"""Glue Python shell job is459_rt_producer: replays one bootstrapped slice into Kinesis in timestamp order.
Args: --slice_key rt/bootstrap/<tag>/slice.csv --stream is459-rt-stream --speedup 4 --minutes 0 --run_id <id> --resend 0 [--spread 1]
One PutRecords call never mixes two minutes, and calls run one after another, so the shard keeps timestamp order
(rows in the same minute never see each other in the features, so their order inside a minute does not matter).
Partition key = sender account key. The label column is not in the slice file and is dropped again here."""
import sys, csv, io, json, time
import boto3
from awsglue.utils import getResolvedOptions

A = getResolvedOptions(sys.argv, ['slice_key', 'stream', 'speedup', 'minutes', 'run_id', 'resend'])
B = 'is459-aml-g1t5'
s3 = boto3.client('s3', region_name='us-east-1'); kin = boto3.client('kinesis', region_name='us-east-1')
rows = list(csv.DictReader(io.StringIO(s3.get_object(Bucket=B, Key=A['slice_key'])['Body'].read().decode())))
for r in rows:
    r.pop('is_laundering', None)
speed, minutes, resend = float(A['speedup']), int(A['minutes']), int(A['resend'])
spread = '--spread' in sys.argv and sys.argv[sys.argv.index('--spread') + 1] == '1'
first = rows[0]['timestamp']


def minute_of(ts):
    h, m = int(ts[11:13]), int(ts[14:16])
    return int(ts[8:10]) * 1440 + h * 60 + m


m0 = minute_of(first)
if minutes > 0:
    rows = [r for r in rows if minute_of(r['timestamp']) - m0 < minutes]
groups = []
for r in rows:
    if groups and groups[-1][0] == r['timestamp']:
        groups[-1][1].append(r)
    else:
        groups.append((r['timestamp'], [r]))
if resend > 0:  # idempotency test: send the first N rows again, after everything else
    groups.append(('resend', rows[:resend]))
stats = {'run_id': A['run_id'], 'rows': len(rows), 'resend': resend, 'minutes': len(groups), 'put_calls': 0, 'throttled_retries': 0}
t_start = time.time()
stats['started'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(t_start))
for ts, grp in groups:
    if ts != 'resend':
        due = t_start + (minute_of(ts) - m0) * 60.0 / speed
        if due > time.time():
            time.sleep(due - time.time())
    subs = [grp] if not spread or ts == 'resend' else [grp[j::12] for j in range(12)]
    for j, rows_j in enumerate(subs):
      if spread and ts != 'resend':  # spread one minute's rows evenly over its (sped-up) minute
        d2 = due + j * (60.0 / speed) / len(subs)
        if d2 > time.time():
            time.sleep(d2 - time.time())
      for i in range(0, len(rows_j), 500):
        part = [{'Data': json.dumps(dict(r, sent_at_ms=int(time.time() * 1000)), separators=(',', ':')).encode(),
                 'PartitionKey': r['sender_account_key']} for r in rows_j[i:i + 500]]
        tries = 0
        while part:
            resp = kin.put_records(StreamName=A['stream'], Records=part)
            stats['put_calls'] += 1
            if not resp['FailedRecordCount']:
                break
            part = [p for p, x in zip(part, resp['Records']) if 'ErrorCode' in x]
            stats['throttled_retries'] += len(part)
            tries += 1
            time.sleep(min(0.1 * 2 ** tries, 2))
stats['finished'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
stats['wall_seconds'] = round(time.time() - t_start, 1)
s3.put_object(Bucket=B, Key='rt/runs/%s/producer.json' % A['run_id'], Body=json.dumps(stats, indent=1).encode(), Tagging='project=is459-g1t5-rt')
print('PRODUCER_DONE', json.dumps(stats))
