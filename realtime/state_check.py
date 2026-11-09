#!/usr/bin/env python3
"""Compare live DynamoDB state with state rebuilt from Silver at the next slice start, then preload only missing keys.
Usage: python3 state_check.py rtdata/<next_tag> [--load-missing]
For every key the next slice needs: if the live table has it, the live item must give the same answer as the
Silver rebuild for any transaction at or after the slice start (counters, sums, last timestamp, 24 h windows)."""
import sys, csv, json, time
from concurrent.futures import ThreadPoolExecutor
import boto3
from botocore.config import Config
sys.path.insert(0, '.')
import rt_core as C
from bootstrap_logic import build_items

d = sys.argv[1]
load_missing = '--load-missing' in sys.argv
ddb = boto3.client('dynamodb', region_name='us-east-1', config=Config(retries={'max_attempts': 10, 'mode': 'adaptive'}, max_pool_connections=16))
meta = json.load(open(d + '/meta.json'))
S = C.epoch(meta['slice_start'])


def load(name):
    with open('%s/%s.csv' % (d, name), newline='') as fh:
        r = csv.reader(fh); next(r)
        return [tuple(x) for x in r]


boot = build_items({k: load(k) for k in ['pairs', 'senders', 'receivers', 'sender_ccy', 'snd_win', 'sc_win', 'rcv_win', 'rc_win']})
with open(d + '/slice.csv', newline='') as fh:
    need = {k for r in csv.DictReader(fh) for k in C.keys_for(r).values()}


def get(keys):
    out, req = {}, {'is459_rt_state': {'Keys': [{'pk': {'S': k}} for k in keys], 'ConsistentRead': True}}
    while req:
        r = ddb.batch_get_item(RequestItems=req)
        for it in r['Responses'].get('is459_rt_state', []):
            out[it['pk']['S']] = json.loads(it['j']['S'])
        req = r.get('UnprocessedKeys') or {}
    return out


keys = sorted(need)
with ThreadPoolExecutor(12) as ex:
    live = {}
    for part in ex.map(get, [keys[i:i + 100] for i in range(0, len(keys), 100)]):
        live.update(part)


def close(a, b):
    return abs(a - b) <= 1e-6 + 1e-9 * abs(b)


def same(k, a, b):
    """a = live item, b = Silver rebuild (or empty). Equal for any query at ts >= S."""
    kind = k.split('#', 1)[0]
    b = b or C.empty(kind)
    probs = []
    for c in [x for x in ('cnt', 'dr', 'ds', 'sum') if x in b]:
        if not close(C.prior(a[c], S), b[c]['t']): probs.append(c)
    if kind == 'S' and (a['last'] if a['last'] is not None and a['last'] < S else a['prev']) != b['last']:
        probs.append('last')
    if 'w' in b:
        for lo, hi in [(S - C.DAY, S), (S - C.HOUR, S)]:
            if not close(C.wsum(a['w'], lo, hi), C.wsum(b['w'], lo, hi)): probs.append('w%d' % (hi - lo))
    return probs


both = [k for k in keys if k in live]
bad = {k: p for k in both for p in [same(k, live[k], boot.get(k))] if p}
missing = [k for k in keys if k not in live]
res = {'next_slice_keys': len(keys), 'already_live': len(both), 'live_vs_silver_mismatches': len(bad),
       'examples': list(bad.items())[:5], 'missing_with_history_to_preload': sum(1 for k in missing if k in boot), 'missing_brand_new': sum(1 for k in missing if k not in boot)}
print(json.dumps(res, indent=1))
if bad:
    sys.exit(3)  # live state disagrees with Silver: never continue a session on top of it
if load_missing:
    reqs = [{'PutRequest': {'Item': {'pk': {'S': k}, 'j': {'S': C.dumps(boot[k])}, 'seq': {'S': 'bootstrap'}}}} for k in missing if k in boot]

    def put(batch):
        req = {'is459_rt_state': batch}
        while req:
            req = ddb.batch_write_item(RequestItems=req).get('UnprocessedItems') or {}
        return len(batch)
    with ThreadPoolExecutor(12) as ex:
        print('PRELOADED', sum(ex.map(put, [reqs[i:i + 25] for i in range(0, len(reqs), 25)])))
