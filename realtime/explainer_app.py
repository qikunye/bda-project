"""Lambda is459-rt-explainer: one alert in, one grounded JSON brief out.
Trigger: SQS is459-rt-explain-queue (the alert rule's second target), batch size 1, alias 'live'.
Steps per alert: read the features from is459_rt_scores, add five as-of context numbers (Athena over Silver, strictly before
the alert), get a typology hint from the small XGBoost classifier, ask the fine-tuned 1.5B model for the brief under a JSON
grammar, check schema and grounding (one retry), else store a template brief marked needs_review.
Output: is459_rt_briefs (one row per transaction id, overwritten on re-delivery) and s3://.../rt/briefs/dt=<day>/<txn>.json.
Direct invoke {"mode": "eval", "input": {...}} returns the brief for a ready-made input (used by eval_explainer.py)."""
import os, json, time, hashlib, math, re
import boto3
import explainer_common as E

R, B = 'us-east-1', 'is459-aml-g1t5'
MODEL_KEY, MODEL_SHA = os.environ.get('MODEL_KEY', ''), os.environ.get('MODEL_SHA', '')
MODEL_VERSION = os.environ.get('MODEL_VERSION', 'unset')
CLF_KEY, CLF_META_KEY = os.environ.get('CLF_KEY', ''), os.environ.get('CLF_META_KEY', '')
BRIEFS, SCORES = os.environ.get('BRIEFS_TABLE', 'is459_rt_briefs'), os.environ.get('SCORES_TABLE', 'is459_rt_scores')
ddb, s3, ath = boto3.client('dynamodb', region_name=R), boto3.client('s3', region_name=R), boto3.client('athena', region_name=R)
_LLM, _CLF = None, None
SAFE = re.compile(r'^[A-Za-z0-9|_\-\.]+$')


def _fetch(key, sha=None):
    path = '/tmp/' + key.replace('/', '_')
    if not os.path.exists(path):
        s3.download_file(B, key, path + '.part')
        os.replace(path + '.part', path)
    if sha:
        h = hashlib.sha256()
        with open(path, 'rb') as f:
            for b in iter(lambda: f.read(1 << 24), b''):
                h.update(b)
        if h.hexdigest() != sha:
            os.remove(path)
            raise RuntimeError('model sha mismatch for ' + key)
    return path


def llm():
    global _LLM
    if _LLM is None:
        from llama_cpp import Llama
        _LLM = Llama(model_path=_fetch(MODEL_KEY, MODEL_SHA), n_ctx=2048, n_threads=os.cpu_count() or 6, n_batch=512, verbose=False)
    return _LLM


def clf():
    global _CLF
    if _CLF is None:
        import xgboost as xgb
        m = xgb.Booster()
        m.load_model(_fetch(CLF_KEY))
        meta = json.load(open(_fetch(CLF_META_KEY)))
        _CLF = (m, meta)
    return _CLF


def hint(row):
    import numpy as np, xgboost as xgb
    m, meta = clf()
    pf = {p: i for i, p in enumerate(meta['payment_formats'])}
    vals = {'risk_score': float(row['risk_score']), 'log_amount': math.log1p(float(row['amount_paid'])),
            'pf_idx': float(pf.get(row.get('payment_format'), -1))}
    for c in E.FLAGS:
        vals[c] = float(E._flag(row.get(c)))
    for c in E.NUMERIC + E.CONTEXT:
        x = E._num(row.get(c))
        vals[c] = float('nan') if x is None else x
    p = m.predict(xgb.DMatrix(np.array([[vals[c] for c in meta['features']]], dtype='float32')))[0]
    i = int(p.argmax())
    return meta['classes'][i], float(p[i])


def context(txn_ts, sender, receiver):
    """Five as-of numbers, same windows as explainer_export.py: [t-1d, t) for in/out, [t-4d, t) for the reverse pair."""
    if not (SAFE.match(sender) and SAFE.match(receiver)):
        raise ValueError('unexpected account key format')
    ts = txn_ts[:16] + ':00'
    sql = ("select count_if(receiver_account_key = '{s}' and timestamp >= timestamp '{t}' - interval '1' day), "
           "count(distinct case when receiver_account_key = '{s}' and timestamp >= timestamp '{t}' - interval '1' day then sender_account_key end), "
           "count_if(sender_account_key = '{r}' and timestamp >= timestamp '{t}' - interval '1' day), "
           "count(distinct case when sender_account_key = '{r}' and timestamp >= timestamp '{t}' - interval '1' day then receiver_account_key end), "
           "count_if(sender_account_key = '{r}' and receiver_account_key = '{s}') "
           "from is459_aml_silver_v2.tx_v2 where timestamp >= timestamp '{t}' - interval '4' day and timestamp < timestamp '{t}' "
           "and (receiver_account_key = '{s}' or sender_account_key = '{r}')").format(s=sender, r=receiver, t=ts)
    qid = ath.start_query_execution(QueryString=sql, ResultConfiguration={'OutputLocation': 's3://%s/athena-results/rt/explainer/' % B})['QueryExecutionId']
    for _ in range(120):
        st = ath.get_query_execution(QueryExecutionId=qid)['QueryExecution']['Status']['State']
        if st in ('SUCCEEDED', 'FAILED', 'CANCELLED'):
            break
        time.sleep(0.5)
    if st != 'SUCCEEDED':
        raise RuntimeError('context query ' + st)
    vals = [int(c.get('VarCharValue', '0')) for c in ath.get_query_results(QueryExecutionId=qid)['ResultSet']['Rows'][1]['Data']]
    return dict(zip(E.CONTEXT, vals))


# TYPOLOGY_MODE=hint pins the brief's typology to the XGBoost classifier's hint (the grammar only allows that one value, and
# typology is generated first, so the narrative is written for it). TYPOLOGY_MODE=model lets the language model pick.
TYPOLOGY_MODE = os.environ.get('TYPOLOGY_MODE', 'model')


def schema_for(inp):
    if TYPOLOGY_MODE == 'hint' and inp.get('typology_hint') in E.TYPOLOGIES:
        sc = json.loads(json.dumps(E.SCHEMA))
        sc['properties']['typology']['enum'] = [inp['typology_hint']]
        return sc
    return E.SCHEMA


def generate(inp):
    """Model brief for one input; returns (brief, status, problems)."""
    probs = []
    for attempt in range(2):
        out = llm().create_chat_completion(messages=E.messages(inp), temperature=0.0 if attempt == 0 else 0.3, max_tokens=320, seed=7,
                                           response_format={'type': 'json_object', 'schema': schema_for(inp)})
        try:
            obj = json.loads(out['choices'][0]['message']['content'])
        except Exception:
            probs = ['unparsable']
            continue
        probs = E.validate(obj, inp)
        if not probs:
            return obj, 'ok', []
    return E.template_brief(inp), 'needs_review', probs


def explain_alert(detail):
    t0 = time.time()
    txn = detail['transaction_id']
    item = ddb.get_item(TableName=SCORES, Key={'txn': {'S': txn}}, ConsistentRead=True).get('Item')
    if not item:
        raise RuntimeError('score row missing for ' + txn)
    f = json.loads(item['f']['S'])
    row = dict(f, risk_score=float(item['score']['N']), amount_paid=item['amount']['S'], payment_format=item['payment_format']['S'])
    row.update(context(item['txn_ts']['S'], item['sender']['S'], item['receiver']['S']))
    row['typology_hint'], row['typology_hint_prob'] = hint(row)
    inp = E.build_input(row)
    brief, status, probs = generate(inp)
    ms = int((time.time() - t0) * 1000)
    rec = {'txn': txn, 'txn_ts': item['txn_ts']['S'], 'score': float(item['score']['N']), 'typology': brief['typology'],
           'confidence': brief['confidence'], 'narrative': brief['narrative'], 'next_checks': brief['next_checks'], 'status': status,
           'problems': probs, 'typology_hint': row['typology_hint'], 'typology_hint_prob': round(row['typology_hint_prob'], 3),
           'model_version': MODEL_VERSION, 'latency_ms': ms, 'alert_scored_at_ms': int(item['scored_at_ms']['N']),
           'written_at_ms': int(time.time() * 1000), 'input': inp}
    ddb.put_item(TableName=BRIEFS, Item={
        'txn': {'S': txn}, 'txn_ts': {'S': rec['txn_ts']}, 'score': {'N': repr(rec['score'])}, 'typology': {'S': rec['typology']},
        'confidence': {'S': rec['confidence']}, 'narrative': {'S': rec['narrative']}, 'next_checks': {'S': json.dumps(rec['next_checks'])},
        'status': {'S': status}, 'typology_hint': {'S': rec['typology_hint']}, 'model_version': {'S': MODEL_VERSION},
        'latency_ms': {'N': str(ms)}, 'alert_scored_at_ms': {'N': str(rec['alert_scored_at_ms'])}, 'input': {'S': json.dumps(inp)}})
    s3.put_object(Bucket=B, Key='rt/briefs/dt=%s/%s.json' % (rec['txn_ts'][:10], txn), Body=json.dumps(rec).encode(),
                  Tagging='project=is459-g1t5-rt')
    print(json.dumps({'txn': txn, 'status': status, 'typology': rec['typology'], 'hint': rec['typology_hint'], 'ms': ms}))
    return rec


def handler(event, context_):
    if event.get('mode') == 'eval':
        t0 = time.time()
        inp = event['input']
        if event.get('with_hint', True) and 'clf_x' in event:
            import numpy as np, xgboost as xgb
            m, meta = clf()
            p = m.predict(xgb.DMatrix(np.array([event['clf_x']], dtype='float32')))[0]
            inp = dict(inp, typology_hint=meta['classes'][int(p.argmax())], typology_hint_prob=round(float(p.max()), 2))
        brief, status, probs = generate(inp)
        return {'brief': brief, 'status': status, 'problems': probs, 'ms': int((time.time() - t0) * 1000), 'hint': inp.get('typology_hint')}
    for rec in event.get('Records', []):
        body = json.loads(rec['body'])
        explain_alert(body.get('detail', body))
    return {'ok': True}
