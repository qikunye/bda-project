"""Build the engine's starting state (as of slice start S) from history rows, only for keys the slice touches.
Mirrors the Athena bootstrap queries in bootstrap.py; used by the offline gate and the local test."""
from rt_core import counter, keys_for, epoch, DAY


def build_items(hist_aggs):
    """hist_aggs: dict with lists of tuples, the exact shape the Athena queries return:
       pairs:      (snd, rcv, cnt)
       senders:    (snd, txn_cnt, last_ts, distinct_rcv)
       receivers:  (rcv, distinct_snd)
       sender_ccy: (snd, ccy, cnt, amt_sum)
       snd_win:    (snd, minute_ts, cnt)
       sc_win:     (snd, ccy, minute_ts, amt)
       rcv_win:    (rcv, minute_ts, cnt)
       rc_win:     (rcv, ccy, minute_ts, amt)"""
    items = {}

    def get(key, init):
        if key not in items:
            items[key] = init()
        return items[key]

    for s, v, c in hist_aggs['pairs']:
        get('P#%s#%s' % (s, v), lambda: {'cnt': counter()})['cnt']['t'] = int(c)
    for s, n, last, dr in hist_aggs['senders']:
        it = get('S#' + s, lambda: {'cnt': counter(), 'last': None, 'prev': None, 'dr': counter(), 'w': {}})
        it['cnt']['t'], it['last'], it['dr']['t'] = int(n), (int(last) if last not in (None, '') else None), int(dr)
    for v, ds in hist_aggs['receivers']:
        get('R#' + v, lambda: {'ds': counter(), 'w': {}})['ds']['t'] = int(ds)
    for s, ccy, n, sm in hist_aggs['sender_ccy']:
        it = get('SC#%s#%s' % (s, ccy), lambda: {'cnt': counter(), 'sum': counter(0.0), 'w': {}})
        it['cnt']['t'], it['sum']['t'] = int(n), float(sm)
    for s, m, n in hist_aggs['snd_win']:
        it = get('S#' + s, lambda: {'cnt': counter(), 'last': None, 'prev': None, 'dr': counter(), 'w': {}})
        it['w'][str(int(m))] = int(n)
    for s, ccy, m, a in hist_aggs['sc_win']:
        it = get('SC#%s#%s' % (s, ccy), lambda: {'cnt': counter(), 'sum': counter(0.0), 'w': {}})
        it['w'][str(int(m))] = float(a)
    for v, m, n in hist_aggs['rcv_win']:
        get('R#' + v, lambda: {'ds': counter(), 'w': {}})['w'][str(int(m))] = int(n)
    for v, ccy, m, a in hist_aggs['rc_win']:
        get('RC#%s#%s' % (v, ccy), lambda: {'w': {}})['w'][str(int(m))] = float(a)
    return items


def aggregate_python(hist, slice_rows, start):
    """Pure-Python version of the Athena bootstrap (for the local test)."""
    snd = {r['sender_account_key'] for r in slice_rows}
    rcv = {r['receiver_account_key'] for r in slice_rows}
    prs = {(r['sender_account_key'], r['receiver_account_key']) for r in slice_rows}
    sccy = {(r['sender_account_key'], r['payment_currency']) for r in slice_rows}
    rccy = {(r['receiver_account_key'], r['receiving_currency']) for r in slice_rows}
    from collections import defaultdict
    pc, sn, sl, sdr, rds = defaultdict(int), defaultdict(int), {}, defaultdict(set), defaultdict(set)
    scn, scs = defaultdict(int), defaultdict(float)
    sw, scw, rw, rcw = defaultdict(int), defaultdict(float), defaultdict(int), defaultdict(float)
    for r in hist:
        s, v, t = r['sender_account_key'], r['receiver_account_key'], epoch(r['timestamp'])
        pa, ra = float(r['amount_paid']), float(r['amount_received'])
        if (s, v) in prs: pc[(s, v)] += 1
        if s in snd:
            sn[s] += 1; sl[s] = max(sl.get(s, t), t); sdr[s].add(v)
            if t >= start - DAY: sw[(s, t)] += 1
        if v in rcv:
            rds[v].add(s)
            if t >= start - DAY: rw[(v, t)] += 1
        if (s, r['payment_currency']) in sccy:
            scn[(s, r['payment_currency'])] += 1; scs[(s, r['payment_currency'])] += pa
            if t >= start - DAY: scw[(s, r['payment_currency'], t)] += pa
        if (v, r['receiving_currency']) in rccy and t >= start - DAY:
            rcw[(v, r['receiving_currency'], t)] += ra
    return {'pairs': [(s, v, c) for (s, v), c in pc.items()],
            'senders': [(s, sn[s], sl[s], len(sdr[s])) for s in sn],
            'receivers': [(v, len(x)) for v, x in rds.items()],
            'sender_ccy': [(s, c, scn[(s, c)], scs[(s, c)]) for (s, c) in scn],
            'snd_win': [(s, t, n) for (s, t), n in sw.items()],
            'sc_win': [(s, c, t, a) for (s, c, t), a in scw.items()],
            'rcv_win': [(v, t, n) for (v, t), n in rw.items()],
            'rc_win': [(v, c, t, a) for (v, c, t), a in rcw.items()]}
