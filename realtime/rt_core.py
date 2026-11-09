"""IS459 real-time path: per-transaction port of the v2 BQ1 feature logic (lib/bq1_incremental_distributed.py).
State is kept per account so each transaction is scored from data strictly before its timestamp, exactly like the
Spark windows (rangeBetween(..., -1) on epoch seconds): rows in the same minute never see each other.
Counters remember the latest minute's delta so 'prior' values exclude same-timestamp rows.
No I/O here: the Lambda and the offline gate both use this module with their own store."""
import json, math
from datetime import datetime, timezone

DAY, HOUR = 86400, 3600
FEATURES = ['hour_of_day', 'day_of_week', 'log_amount_paid', 'payment_format', 'payment_currency', 'receiving_currency',
            'is_same_account', 'is_same_entity', 'is_same_entity_different_account', 'is_cross_entity', 'is_cross_bank',
            'is_cross_currency', 'snd_out_cnt_1h', 'snd_out_cnt_24h', 'snd_out_amt_24h_ccy', 'rcv_in_cnt_1h', 'rcv_in_cnt_24h',
            'rcv_in_amt_24h_ccy', 'pair_prior_cnt', 'is_new_pair', 'snd_distinct_rcv_to_date', 'rcv_distinct_snd_to_date',
            'snd_prior_txn_cnt', 'snd_prior_mean_amt_ccy', 'amt_ratio_to_snd_mean', 'snd_secs_since_last_out']
FLAGS = ['is_same_account', 'is_same_entity', 'is_same_entity_different_account', 'is_cross_entity', 'is_cross_bank', 'is_cross_currency']


def epoch(ts):
    """'2022-09-16 01:00:00[.000]' (naive, UTC) -> epoch seconds, as Spark cast(timestamp_ltz as long) with UTC session."""
    return int(datetime.strptime(str(ts)[:19].replace('T', ' '), '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc).timestamp())


def flag(v):
    if isinstance(v, bool):
        return int(v)
    if v is None or v == '':
        return 0
    return 1 if str(v).strip().lower() in ('true', '1', 't') else 0


# ---- state item shapes -------------------------------------------------------------------------------
def counter(base=0):
    return {'t': base, 'm': 0, 'd': 0}


def prior(c, ts):
    return c['t'] - (c['d'] if c['m'] == ts else 0)


def add(c, ts, x):
    if c['m'] == ts:
        c['d'] += x
    else:
        c['m'], c['d'] = ts, x
    c['t'] += x


def wsum(w, lo, hi):
    s = 0
    for k, v in w.items():
        k = int(k)
        if lo <= k < hi:
            s += v
    return s


def wadd(w, ts, x):
    k = str(ts)
    w[k] = w.get(k, 0) + x
    lo = ts - DAY
    for k in [k for k in w if int(k) < lo]:
        del w[k]


def keys_for(r):
    s, v = r['sender_account_key'], r['receiver_account_key']
    return {'S': 'S#' + s, 'SC': 'SC#' + s + '#' + r['payment_currency'], 'R': 'R#' + v,
            'RC': 'RC#' + v + '#' + r['receiving_currency'], 'P': 'P#' + s + '#' + v}


def empty(kind):
    if kind == 'S':
        return {'cnt': counter(), 'last': None, 'prev': None, 'dr': counter(), 'w': {}}
    if kind == 'SC':
        return {'cnt': counter(), 'sum': counter(0.0), 'w': {}}
    if kind == 'R':
        return {'ds': counter(), 'w': {}}
    if kind == 'RC':
        return {'w': {}}
    return {'cnt': counter()}


def features(r, it):
    """r: one transaction (Silver columns, no label). it: dict kind -> state item. Returns the 26 features."""
    ts = epoch(r['timestamp'])
    amt_p, amt_r = float(r['amount_paid']), float(r['amount_received'])
    S, SC, R, RC, P = it['S'], it['SC'], it['R'], it['RC'], it['P']
    f = {'hour_of_day': int((ts % DAY) / HOUR), 'day_of_week': (int(ts / DAY) + 3) % 7, 'log_amount_paid': math.log1p(amt_p),
         'payment_format': r['payment_format'], 'payment_currency': r['payment_currency'], 'receiving_currency': r['receiving_currency']}
    for c in FLAGS:
        f[c] = flag(r[c])
    f['snd_out_cnt_1h'] = wsum(S['w'], ts - HOUR, ts)
    f['snd_out_cnt_24h'] = wsum(S['w'], ts - DAY, ts)
    f['snd_out_amt_24h_ccy'] = float(wsum(SC['w'], ts - DAY, ts))
    f['rcv_in_cnt_1h'] = wsum(R['w'], ts - HOUR, ts)
    f['rcv_in_cnt_24h'] = wsum(R['w'], ts - DAY, ts)
    f['rcv_in_amt_24h_ccy'] = float(wsum(RC['w'], ts - DAY, ts))
    f['pair_prior_cnt'] = prior(P['cnt'], ts)
    f['is_new_pair'] = int(f['pair_prior_cnt'] == 0)
    f['snd_distinct_rcv_to_date'] = prior(S['dr'], ts)
    f['rcv_distinct_snd_to_date'] = prior(R['ds'], ts)
    f['snd_prior_txn_cnt'] = prior(S['cnt'], ts)
    n, sm = prior(SC['cnt'], ts), prior(SC['sum'], ts)
    mean = sm / n if n > 0 else None
    f['snd_prior_mean_amt_ccy'] = mean
    f['amt_ratio_to_snd_mean'] = amt_p / mean if mean is not None and mean > 0 else None
    last = S['prev'] if S['last'] == ts else S['last']
    f['snd_secs_since_last_out'] = ts - last if last is not None else None
    return f


def apply(r, it):
    """Advance state with transaction r (call after features())."""
    ts = epoch(r['timestamp'])
    amt_p, amt_r = float(r['amount_paid']), float(r['amount_received'])
    S, SC, R, RC, P = it['S'], it['SC'], it['R'], it['RC'], it['P']
    add(S['cnt'], ts, 1)
    if S['last'] != ts:
        S['prev'], S['last'] = S['last'], ts
    wadd(S['w'], ts, 1)
    add(SC['cnt'], ts, 1)
    add(SC['sum'], ts, amt_p)
    wadd(SC['w'], ts, amt_p)
    wadd(R['w'], ts, 1)
    wadd(RC['w'], ts, amt_r)
    if P['cnt']['t'] == 0:
        add(S['dr'], ts, 1)
        add(R['ds'], ts, 1)
    add(P['cnt'], ts, 1)


class MemoryStore:
    """Dict-backed store: used by the offline gate and as the Lambda's per-invocation cache."""
    def __init__(self, items=None):
        self.items = items if items is not None else {}

    def get(self, key):
        it = self.items.get(key)
        if it is None:
            it = empty(key.split('#', 1)[0])
            self.items[key] = it
        return it

    def items_for(self, r):
        return {kind: self.get(k) for kind, k in keys_for(r).items()}


def process(r, store):
    it = store.items_for(r)
    f = features(r, it)
    apply(r, it)
    return f


def matrix(pdf, schema):
    """Verbatim copy of matrix() in the v2 BQ1 job: model inputs in schema order, float32 / int8 / ordered categories."""
    import pandas as pd
    out = pdf.copy()
    med = schema['amt_vs_ccy_median']
    out['amt_vs_ccy_median'] = (out['log_amount_paid'] - out['payment_currency'].map(med['per_currency_median']).fillna(med['global_median'])).astype('float32')
    for c in schema['numeric']:
        out[c] = out[c].astype('float32')
    for c in schema['flags']:
        out[c] = out[c].fillna(0).astype('int8')
    for c in schema['categorical']:
        cats = schema['category_lists'][c]
        out[c] = out[c].where(out[c].isin(cats), '__other__').astype(pd.CategoricalDtype(categories=cats, ordered=True))
    return out[schema['features']]


def to_frame(feature_rows):
    import pandas as pd
    import numpy as np
    pdf = pd.DataFrame(feature_rows, columns=FEATURES)
    for c in ['snd_prior_mean_amt_ccy', 'amt_ratio_to_snd_mean', 'snd_secs_since_last_out']:
        pdf[c] = pd.to_numeric(pdf[c], errors='coerce').astype('float64')
    return pdf


def score(booster, schema, feature_rows):
    import xgboost as xgb
    pdf = to_frame(feature_rows)
    return booster.predict(xgb.DMatrix(matrix(pdf, schema), enable_categorical=True), iteration_range=(0, 96)).astype('float64')


def dumps(item):
    return json.dumps(item, separators=(',', ':'))
