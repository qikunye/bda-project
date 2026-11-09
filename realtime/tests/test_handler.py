"""Local test of app.handler against an in-memory DynamoDB: crash mid-batch + Lambda retry + producer re-send
must leave state and features identical to one clean sequential pass."""
import os, sys, json, base64, random, copy
os.environ['RT_SKIP_MODEL'] = '1'
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rt_core as C
import test_local as T  # synthetic rows, history, slice, bootstrap helpers
from bootstrap_logic import build_items, aggregate_python


class FakeDDB:
    def __init__(self):
        self.t = {'is459_rt_state': {}, 'is459_rt_seen': {}, 'is459_rt_scores': {}, 'is459_rt_alerts': {}}
        self.fail_on = None
        self.commits = 0

    def batch_get_item(self, RequestItems):
        out = {}
        for tbl, req in RequestItems.items():
            kn = list(req['Keys'][0].keys())[0]
            out[tbl] = [copy.deepcopy(self.t[tbl][k[kn]['S']]) for k in req['Keys'] if k[kn]['S'] in self.t[tbl]]
        return {'Responses': out, 'UnprocessedKeys': {}}

    def transact_write_items(self, TransactItems):
        self.commits += 1
        if self.fail_on == self.commits:
            raise RuntimeError('injected crash before commit %d' % self.commits)
        assert len(TransactItems) <= 100
        staged = []
        for x in TransactItems:
            p = x['Put']; tbl = p['TableName']; item = p['Item']
            kn = 'pk' if tbl == 'is459_rt_state' else 'txn'
            k = item[kn]['S']
            if 'ConditionExpression' in p and k in self.t[tbl]:
                raise RuntimeError('TransactionCanceled: ConditionalCheckFailed')
            staged.append((tbl, k, item))
        assert len({(a, b) for a, b, _ in staged}) == len(staged), 'item twice in one transaction'
        for tbl, k, item in staged:
            self.t[tbl][k] = item


import app
fake = FakeDDB()
app.ddb = fake
app.C.score = lambda b, s, feats: [0.0] * len(feats)
app.BOOSTER, app.SCHEMA, app.THRESH = None, None, 2.0

for k, v in build_items(aggregate_python(T.hist, T.sl, T.start)).items():
    fake.t['is459_rt_state'][k] = {'pk': {'S': k}, 'j': {'S': C.dumps(v)}}


def event(rows, seq0):
    return {'Records': [{'kinesis': {'data': base64.b64encode(json.dumps(r).encode()).decode(), 'approximateArrivalTimestamp': 1.0,
                                     'sequenceNumber': str(seq0 + i)}} for i, r in enumerate(rows)]}


seq = 0
for i in range(0, len(T.sl), 80):
    batch = T.sl[i:i + 80]
    if i == 400:
        fake.fail_on = fake.commits + 2           # crash on the 2nd commit of this batch
        try:
            app.handler(event(batch, seq), None)
        except RuntimeError as e:
            print('first attempt failed as injected:', e)
        fake.fail_on = None
    app.handler(event(batch, seq), None)      # Lambda retry of the same batch (or normal run)
    seq += len(batch)
app.handler(event(T.sl[:300], seq), None)     # producer re-sends 300 records later

# reference: one clean sequential pass
ref = C.MemoryStore(build_items(aggregate_python(T.hist, T.sl, T.start)))
ref_f = {r['transaction_id']: C.process(r, ref) for r in T.sl}
got_f = {k: json.loads(v['f']['S']) for k, v in fake.t['is459_rt_scores'].items()}
state_ok = all(json.loads(fake.t['is459_rt_state'][k]['j']['S']) == json.loads(C.dumps(v)) for k, v in ref.items.items())
feat_ok = got_f == {k: json.loads(json.dumps(v)) for k, v in ref_f.items()}
print('scores rows', len(got_f), 'slice rows', len(T.sl), 'seen markers', len(fake.t['is459_rt_seen']))
print('state identical to clean pass:', state_ok, '| features identical:', feat_ok, '| commits:', fake.commits)
