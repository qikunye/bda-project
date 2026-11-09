#!/usr/bin/env python3
"""Go/no-go gate: run the per-record engine over the first N minutes of a bootstrapped slice and compare with v2 Gold.
Usage (inside the scorer image): python3 offline_gate.py <data_dir> <minutes> <model.ubj> <feature_schema.json>
Tolerances are exactly v2's: features |a-b| <= 1e-6 + 1e-9|b| (double), flags/categories exact;
scores |a-b| <= 1e-6 + 1e-5|b|, at most max(10, 0.01% of rows) outside, none above 0.01."""
import sys, csv, json, math, time
import rt_core as C
from bootstrap_logic import build_items

d, minutes, model_path, schema_path = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]


def load(name):
    with open('%s/%s.csv' % (d, name), newline='') as fh:
        r = csv.reader(fh)
        next(r)
        return [tuple(x) for x in r]


t0 = time.time()
aggs = {k: load(k) for k in ['pairs', 'senders', 'receivers', 'sender_ccy', 'snd_win', 'sc_win', 'rcv_win', 'rc_win']}
store = C.MemoryStore(build_items(aggs))
with open(d + '/slice.csv', newline='') as fh:
    rows = list(csv.DictReader(fh))
start = C.epoch(rows[0]['timestamp'])
rows = [r for r in rows if C.epoch(r['timestamp']) < start + 60 * minutes] if minutes > 0 else rows
feats, ids = [], []
for r in rows:
    feats.append(C.process(r, store))
    ids.append(r['transaction_id'])
t1 = time.time()

import xgboost as xgb
schema = json.load(open(schema_path))
booster = xgb.Booster()
booster.load_model(model_path)
assert xgb.__version__ == '3.2.0' and booster.num_features() == 27 and booster.attr('best_iteration') == '95'
scores = C.score(booster, schema, feats)
t2 = time.time()

with open(d + '/gold_features.csv', newline='') as fh:
    gf = {x['transaction_id']: x for x in csv.DictReader(fh)}
with open(d + '/gold_scores.csv', newline='') as fh:
    gs = {x['transaction_id']: float(x['risk_score']) for x in csv.DictReader(fh)}


def num(v):
    return None if v in ('', None) else float(v)


per, examples, n = {c: 0 for c in C.FEATURES}, [], 0
for tid, f in zip(ids, feats):
    g = gf.get(tid)
    if g is None:
        per.setdefault('_missing_in_gold', 0)
        per['_missing_in_gold'] += 1
        continue
    n += 1
    for c in C.FEATURES:
        a, b = f[c], g[c]
        if c in ('payment_format', 'payment_currency', 'receiving_currency'):
            ok = (a or '__other__') == (b or '__other__')
        elif c in C.FLAGS or c == 'is_new_pair':
            ok = int(a or 0) == C.flag(b)
        else:
            a, b = (None if a is None else float(a)), num(b)
            ok = (a is None and b is None) or (a is not None and b is not None and abs(a - b) <= 1e-6 + 1e-9 * abs(b))
        if not ok:
            per[c] += 1
            if len(examples) < 12:
                examples.append((tid, c, a, b))
sc_n, sc_bad, sc_max = 0, 0, 0.0
for tid, s in zip(ids, scores):
    if tid in gs:
        sc_n += 1
        diff = abs(s - gs[tid])
        sc_max = max(sc_max, diff)
        if diff > 1e-6 + 1e-5 * abs(gs[tid]):
            sc_bad += 1
allow = max(10, int(sc_n * 1e-4))
res = {'rows': len(rows), 'compared': n, 'feature_mismatches': sum(v for k, v in per.items() if not k.startswith('_')),
       'per_feature': {k: v for k, v in per.items() if v}, 'score_compared': sc_n, 'score_outside_tol': sc_bad,
       'score_allowance': allow, 'score_max_abs_diff': sc_max, 'engine_seconds': round(t1 - t0, 1), 'score_seconds': round(t2 - t1, 1),
       'examples': [list(map(str, e)) for e in examples]}
res['gate'] = 'PASS' if res['feature_mismatches'] == 0 and n == len(rows) and sc_n == len(rows) and sc_bad <= allow and sc_max <= 0.01 else 'FAIL'
print(json.dumps(res, indent=1))
open(d + '/gate_%dmin.json' % minutes, 'w').write(json.dumps(res, indent=1))
