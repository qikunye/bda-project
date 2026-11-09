"""Local tests for the explainer: input allowlist (no label can leak), grounding checker, schema checks, template fallback,
duplicate SQS delivery (one row), and the handler end to end with a fake model, classifier, Athena and DynamoDB."""
import json, sys, types, math
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import explainer_common as E

row = {'risk_score': 0.81234, 'amount_paid': '12491.16', 'payment_format': 'ACH', 'payment_currency': 'US Dollar', 'receiving_currency': 'US Dollar',
       'is_same_account': 'false', 'is_same_entity': False, 'is_same_entity_different_account': 0, 'is_cross_entity': 'true', 'is_cross_bank': 1,
       'is_cross_currency': 'false', 'hour_of_day': 0, 'day_of_week': 3, 'snd_out_cnt_1h': 0, 'snd_out_cnt_24h': 4, 'snd_out_amt_24h_ccy': 34349.88,
       'rcv_in_cnt_1h': 0, 'rcv_in_cnt_24h': 1, 'rcv_in_amt_24h_ccy': 500.0, 'pair_prior_cnt': 0, 'is_new_pair': 1, 'snd_distinct_rcv_to_date': 7,
       'rcv_distinct_snd_to_date': 2, 'snd_prior_txn_cnt': 9, 'snd_prior_mean_amt_ccy': 3816.653333, 'amt_ratio_to_snd_mean': 3.2728,
       'snd_secs_since_last_out': 7200, 'ctx_snd_in_cnt_24h': 3, 'ctx_snd_in_distinct_24h': 2, 'ctx_rcv_out_cnt_24h': 0, 'ctx_rcv_out_distinct_24h': 0,
       'ctx_reverse_pair_96h': 0, 'typology_hint': 'fan_out', 'typology_hint_prob': 0.71}
inp = E.build_input(row)
assert inp['amount_paid'] == 12491.16 and inp['risk_score'] == 0.812 and inp['snd_prior_mean_amt_ccy'] == 3816.65 and inp['is_cross_entity'] == 1
for bad in ('is_laundering', 'typology', 'sender_account_key', 'label'):
    assert bad not in inp
# a label passed in the row is dropped by the allowlist, never copied
assert 'is_laundering' not in E.build_input(dict(row, is_laundering=1, typology='fan_out'))

good = {'typology': 'fan_out', 'confidence': 'medium',
        'narrative': 'The sender made 4 payments in the previous 24 hours and has paid 7 different receivers; this payment of 12,491.16 is 3.27 times its usual size.',
        'next_checks': ['Check the 7 receivers for onward payments within 24 hours']}
assert E.validate(good, inp) == [], E.validate(good, inp)
bad = dict(good, narrative='The sender moved 55,000 dollars to 12 accounts.')
p = E.validate(bad, inp)
assert p and p[0].startswith('invented'), p
assert E.validate(dict(good, typology='smurfing'), inp) == ['bad typology']
assert E.validate(dict(good, next_checks=['a', 'b', 'c', 'd']), inp) == ['bad next_checks']
assert E.validate(dict(good, extra=1), inp)[0].startswith('extra keys')
assert E.grounding_problems('It was 2 hours since the last payment, 120 minutes.', inp) == []  # 7200 s = 120 min = 2 h
t = E.template_brief(inp)
assert E.validate(t, inp) == [], E.validate(t, inp)
f, per = E.macro_f1(['a', 'a', 'b'], ['a', 'b', 'b'], ['a', 'b'])
assert abs(f - 2 / 3) < 1e-9

# handler end to end with fakes
fake_np = types.ModuleType('numpy')
sys.modules.setdefault('numpy', fake_np)
import explainer_app as A


class FakeDDB:
    def __init__(self):
        self.t = {'is459_rt_briefs': {}}

    def get_item(self, TableName, Key, ConsistentRead=True):
        f = {k: row[k] for k in E.NUMERIC + E.FLAGS + E.CATEG}
        return {'Item': {'txn': {'S': 'tx1'}, 'f': {'S': json.dumps(f)}, 'score': {'N': '0.81234'}, 'amount': {'S': '12491.16'},
                         'payment_format': {'S': 'ACH'}, 'txn_ts': {'S': '2022-09-16 02:31:00'}, 'sender': {'S': '20|800CB3BB0'},
                         'receiver': {'S': '212790|80BE8D400'}, 'scored_at_ms': {'N': '1760000000000'}}}

    def put_item(self, TableName, Item):
        self.t[TableName][Item['txn']['S']] = Item


class FakeS3:
    def __init__(self):
        self.objs = {}

    def put_object(self, Bucket, Key, Body, Tagging=None):
        self.objs[Key] = Body


class FakeLLM:
    def __init__(self, answers):
        self.answers = list(answers)

    def create_chat_completion(self, **kw):
        sc = kw['response_format']['schema']
        if A.TYPOLOGY_MODE == 'hint':  # grammar pins typology to the classifier hint
            assert sc['properties']['typology']['enum'] == [json.loads(kw['messages'][1]['content'][6:])['typology_hint']]
        else:
            assert sc == E.SCHEMA
        assert 'ALERT ' in kw['messages'][1]['content'] and 'is_laundering' not in kw['messages'][1]['content']
        return {'choices': [{'message': {'content': json.dumps(self.answers.pop(0))}}]}


A.ddb, A.s3 = FakeDDB(), FakeS3()
A.context = lambda ts, s, r: {k: row[k] for k in E.CONTEXT}
A.hint = lambda r: ('fan_out', 0.71)
A._LLM = FakeLLM([good, good])
rec = A.explain_alert({'transaction_id': 'tx1'})
assert rec['status'] == 'ok' and rec['typology'] == 'fan_out'
A.explain_alert({'transaction_id': 'tx1'})  # SQS re-delivery of the same alert
assert len(A.ddb.t['is459_rt_briefs']) == 1 and list(A.s3.objs) == ['rt/briefs/dt=2022-09-16/tx1.json']
A._LLM = FakeLLM([bad, bad])  # two invented answers -> template brief, needs_review
rec = A.explain_alert({'transaction_id': 'tx1'})
assert rec['status'] == 'needs_review' and E.validate({k: rec[k] for k in ('typology', 'confidence', 'narrative', 'next_checks')}, rec['input']) == []
A._LLM = FakeLLM([good])
out = A.handler({'Records': [{'body': json.dumps({'detail': {'transaction_id': 'tx1'}})}]}, None)
# both typology modes build the expected grammar and never mutate the shared schema
for mode in ('model', 'hint'):
    A.TYPOLOGY_MODE = mode
    sc = A.schema_for({'typology_hint': 'cycle'})
    assert (sc['properties']['typology']['enum'] == ['cycle']) == (mode == 'hint')
assert E.SCHEMA['properties']['typology']['enum'] == E.TYPOLOGIES
print('EXPLAINER_TESTS_PASS')

# teacher-reference score
ref = {'narrative': 'The sender made 12 payments in 24 hours to 9 accounts, a fan-out pattern.', 'next_checks': ['Review the receivers']}
same = E.ref_score(ref, ref)
assert same['score'] == 1.0 and same['num_recall'] == 1.0, same
near = E.ref_score({'narrative': 'Sender sent 12 payments in 24 hours to 9 accounts.', 'next_checks': ['Review receivers']}, ref)
far = E.ref_score({'narrative': 'Nothing unusual stands out.', 'next_checks': []}, ref)
assert 0 < far['score'] < near['score'] < 1, (near, far)
assert E.ref_score({'narrative': 'x'}, {'narrative': 'no numbers here'})['num_recall'] is None
assert E.rouge_l('', 'a b') == 0.0
print('REF_SCORE_OK')
