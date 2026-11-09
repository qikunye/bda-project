"""Local check: brute-force Spark-window semantics vs bootstrap + per-record engine, on synthetic data with heavy ties."""
import random, math, sys
sys.path.insert(0, '.')
import rt_core as C
from bootstrap_logic import build_items, aggregate_python

random.seed(7)
accts = ['%d|A%03d' % (random.choice([1, 2, 3]), i) for i in range(40)]
ccys = ['US Dollar', 'Euro']
base = C.epoch('2022-09-14 00:00:00')
rows = []
for i in range(6000):
    t = base + 60 * random.randint(0, 2200)  # ~18h of minutes over 3 days scaled: lots of ties
    s, v = random.choice(accts), random.choice(accts)
    rows.append({'transaction_id': 'tx%05d' % i, 'timestamp': __import__('datetime').datetime.utcfromtimestamp(t).strftime('%Y-%m-%d %H:%M:%S'),
                 'sender_account_key': s, 'receiver_account_key': v, 'amount_paid': '%.2f' % random.uniform(1, 5000),
                 'amount_received': '%.2f' % random.uniform(1, 5000), 'payment_currency': random.choice(ccys),
                 'receiving_currency': random.choice(ccys), 'payment_format': 'ACH', **{f: 'false' for f in C.FLAGS}})
rows.sort(key=lambda r: (C.epoch(r['timestamp']), r['transaction_id']))
start = C.epoch('2022-09-15 02:00:00')
hist = [r for r in rows if C.epoch(r['timestamp']) < start]
sl = [r for r in rows if C.epoch(r['timestamp']) >= start]


def brute(r):
    t = C.epoch(r['timestamp']); s, v = r['sender_account_key'], r['receiver_account_key']
    E = [x for x in rows if C.epoch(x['timestamp']) < t]
    so = [x for x in E if x['sender_account_key'] == s]
    ri = [x for x in E if x['receiver_account_key'] == v]
    f = {}
    f['snd_out_cnt_1h'] = sum(1 for x in so if C.epoch(x['timestamp']) >= t - 3600)
    f['snd_out_cnt_24h'] = sum(1 for x in so if C.epoch(x['timestamp']) >= t - 86400)
    f['snd_out_amt_24h_ccy'] = sum(float(x['amount_paid']) for x in so if x['payment_currency'] == r['payment_currency'] and C.epoch(x['timestamp']) >= t - 86400)
    f['rcv_in_cnt_1h'] = sum(1 for x in ri if C.epoch(x['timestamp']) >= t - 3600)
    f['rcv_in_cnt_24h'] = sum(1 for x in ri if C.epoch(x['timestamp']) >= t - 86400)
    f['rcv_in_amt_24h_ccy'] = sum(float(x['amount_received']) for x in ri if x['receiving_currency'] == r['receiving_currency'] and C.epoch(x['timestamp']) >= t - 86400)
    f['pair_prior_cnt'] = sum(1 for x in so if x['receiver_account_key'] == v)
    f['is_new_pair'] = int(f['pair_prior_cnt'] == 0)
    f['snd_distinct_rcv_to_date'] = len({x['receiver_account_key'] for x in so})
    f['rcv_distinct_snd_to_date'] = len({x['sender_account_key'] for x in ri})
    f['snd_prior_txn_cnt'] = len(so)
    cc = [float(x['amount_paid']) for x in so if x['payment_currency'] == r['payment_currency']]
    mean = sum(cc) / len(cc) if cc else None
    f['snd_prior_mean_amt_ccy'] = mean
    f['amt_ratio_to_snd_mean'] = float(r['amount_paid']) / mean if mean and mean > 0 else None
    f['snd_secs_since_last_out'] = t - max(C.epoch(x['timestamp']) for x in so) if so else None
    return f


store = C.MemoryStore(build_items(aggregate_python(hist, sl, start)))
bad = 0
for r in sl:
    got = C.process(r, store)
    want = brute(r)
    for k, w in want.items():
        g = got[k]
        ok = (g is None and w is None) or (g is not None and w is not None and abs(g - w) <= 1e-6 + 1e-9 * abs(w))
        if not ok:
            bad += 1
            if bad < 10: print('MISMATCH', r['transaction_id'], k, g, w)
print('slice rows', len(sl), 'history rows', len(hist), 'feature mismatches', bad)
# idempotency of the in-memory path is handled by the Lambda seen markers; here check ties exist
from collections import Counter
print('max rows sharing one minute', max(Counter(r['timestamp'] for r in sl).values()))
