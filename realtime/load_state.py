#!/usr/bin/env python3
"""Load the bootstrapped starting state (only keys the slice touches) into is459_rt_state.
Usage: python3 load_state.py rtdata/<tag>"""
import sys, csv, json, time
from concurrent.futures import ThreadPoolExecutor
import boto3
from botocore.config import Config
sys.path.insert(0, '.')
import rt_core as C
from bootstrap_logic import build_items

d = sys.argv[1]
ddb = boto3.client('dynamodb', region_name='us-east-1', config=Config(retries={'max_attempts': 10, 'mode': 'adaptive'}, max_pool_connections=16))


def load(name):
    with open('%s/%s.csv' % (d, name), newline='') as fh:
        r = csv.reader(fh); next(r)
        return [tuple(x) for x in r]


items = build_items({k: load(k) for k in ['pairs', 'senders', 'receivers', 'sender_ccy', 'snd_win', 'sc_win', 'rcv_win', 'rc_win']})
reqs = [{'PutRequest': {'Item': {'pk': {'S': k}, 'j': {'S': C.dumps(v)}, 'seq': {'S': 'bootstrap'}}}} for k, v in items.items()]
print('items to load', len(reqs), 'largest item bytes', max(len(r['PutRequest']['Item']['j']['S']) for r in reqs), flush=True)


def put(batch):
    req = {'is459_rt_state': batch}
    n = 0
    while req:
        r = ddb.batch_write_item(RequestItems=req)
        req = r.get('UnprocessedItems') or {}
        n += 1
        if req:
            time.sleep(min(0.05 * 2 ** n, 2))
    return len(batch)


t = time.time()
with ThreadPoolExecutor(12) as ex:
    done = sum(ex.map(put, [reqs[i:i + 25] for i in range(0, len(reqs), 25)]))
print('LOADED', done, 'items in', round(time.time() - t, 1), 's')
