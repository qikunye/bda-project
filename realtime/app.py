"""is459-rt-scorer: Kinesis -> features from DynamoDB state -> frozen XGBoost score -> DynamoDB (+ alerts table).
Exactly-once per transaction: each commit is ONE DynamoDB transaction holding the 'seen' markers (attribute_not_exists),
the updated state items and the score rows. A retried or re-sent record is skipped because its marker exists.
Alert events leave through the alerts table's stream (EventBridge Pipe), so a crash after commit cannot lose them."""
import os, json, time, base64, hashlib, pathlib
import boto3
from botocore.config import Config
import rt_core as C

B = 'is459-aml-g1t5'
MODEL_KEY, MODEL_SHA = 'exported/etl-v2/models/bq1/v1/model.ubj', '29e51521f3e33a85378c948d0cf73ed42507efd51e48f3870aae4a229f6fa758'
SCHEMA_KEY, SCHEMA_SHA = 'exported/etl-v2/live-verified/bq1/feature_schema.json', '49371c2e9b01d005b9528fb5c59c8951af22521374ab51b97aef2d6d4f1578fd'
MODEL_VERSION = 'xgboost-3.2.0-iter0-96'
STATE, SEEN, SCORES, ALERTS = (os.environ.get(k, d) for k, d in [('STATE_TABLE', 'is459_rt_state'), ('SEEN_TABLE', 'is459_rt_seen'),
                                                                   ('SCORES_TABLE', 'is459_rt_scores'), ('ALERTS_TABLE', 'is459_rt_alerts')])
THRESH = float(os.environ.get('ALERT_THRESHOLD', '0.5'))
TTL_SECONDS = 14 * 86400
ddb = boto3.client('dynamodb', config=Config(retries={'max_attempts': 10, 'mode': 'adaptive'}))


def _load():
    import xgboost as xgb
    s3 = boto3.client('s3')
    mb = s3.get_object(Bucket=B, Key=MODEL_KEY)['Body'].read()
    sb = s3.get_object(Bucket=B, Key=SCHEMA_KEY)['Body'].read()
    if hashlib.sha256(mb).hexdigest() != MODEL_SHA or hashlib.sha256(sb).hexdigest() != SCHEMA_SHA:
        raise RuntimeError('Frozen model/schema bytes differ')
    if xgb.__version__ != '3.2.0':
        raise RuntimeError('Frozen XGBoost version mismatch')
    p = pathlib.Path('/tmp/model-%s.ubj' % MODEL_SHA)
    p.write_bytes(mb)
    bst = xgb.Booster()
    bst.load_model(str(p))
    if bst.attr('best_iteration') != '95' or bst.num_boosted_rounds() != 146 or bst.num_features() != 27:
        raise RuntimeError('Frozen model shape/iteration differs')
    return bst, json.loads(sb)


if os.environ.get('RT_SKIP_MODEL') != '1':
    BOOSTER, SCHEMA = _load()


def _batch_get(table, keyname, keys, attr):
    out, keys = {}, list(keys)
    for i in range(0, len(keys), 100):
        req = {table: {'Keys': [{keyname: {'S': k}} for k in keys[i:i + 100]], 'ConsistentRead': True,
                       'ProjectionExpression': '#k' + (', #a' if attr else ''),
                       'ExpressionAttributeNames': dict({'#k': keyname}, **({'#a': attr} if attr else {}))}}
        tries = 0
        while req:
            r = ddb.batch_get_item(RequestItems=req)
            for it in r['Responses'].get(table, []):
                out[it[keyname]['S']] = it[attr]['S'] if attr else True
            req = r.get('UnprocessedKeys') or {}
            tries += 1
            if req:
                time.sleep(min(0.05 * 2 ** tries, 2))
    return out


def _chunks(work, item_keys):
    """Greedy chunks whose distinct DynamoDB items (state + seen + score + alert) stay <= 100 per transaction."""
    chunk, items = [], set()
    for w in work:
        need = set(item_keys(w)) | {'seen#' + w['tid'], 'score#' + w['tid'], 'alert#' + w['tid']}
        if chunk and len(items | need) > 100:
            yield chunk
            chunk, items = [], set()
        chunk.append(w)
        items |= need
    if chunk:
        yield chunk


def handler(event, context):
    t_in = time.time()
    work, dup_in_batch = [], 0
    ids = set()
    for rec in event['Records']:
        k = rec['kinesis']
        r = json.loads(base64.b64decode(k['data']))
        r.pop('is_laundering', None)  # defensive: the label is never used even if a producer sent it
        tid = r['transaction_id']
        if tid in ids:
            dup_in_batch += 1
            continue
        ids.add(tid)
        work.append({'tid': tid, 'r': r, 'arrival_ms': int(float(k['approximateArrivalTimestamp']) * 1000), 'seq': k['sequenceNumber']})
    seen = _batch_get(SEEN, 'txn', ids, None)
    work = [w for w in work if w['tid'] not in seen]
    store = C.MemoryStore()
    keys = {k for w in work for k in C.keys_for(w['r']).values()}
    for k, j in _batch_get(STATE, 'pk', keys, 'j').items():
        store.items[k] = json.loads(j)
    # 1) features in stream order, snapshot touched state at each chunk end
    chunks = list(_chunks(work, lambda w: C.keys_for(w['r']).values()))
    snaps = []
    for ch in chunks:
        touched = set()
        for w in ch:
            w['f'] = C.process(w['r'], store)
            touched |= set(C.keys_for(w['r']).values())
        snaps.append({k: C.dumps(store.items[k]) for k in touched})
    # 2) score everything in one matrix
    if work:
        sc = C.score(BOOSTER, SCHEMA, [w['f'] for w in work])
        for w, s in zip(work, sc):
            w['score'] = float(s)
    # 3) commit chunk by chunk (atomic per chunk)
    alerts = 0
    for ch, snap in zip(chunks, snaps):
        now_ms = int(time.time() * 1000)
        items = [{'Put': {'TableName': STATE, 'Item': {'pk': {'S': k}, 'j': {'S': j}, 'seq': {'S': ch[-1]['seq']}}}} for k, j in snap.items()]
        for w in ch:
            r = w['r']
            base = {'txn': {'S': w['tid']}, 'txn_ts': {'S': r['timestamp']}, 'sender': {'S': r['sender_account_key']},
                    'receiver': {'S': r['receiver_account_key']}, 'amount': {'S': str(r['amount_paid'])},
                    'currency': {'S': r['payment_currency']}, 'payment_format': {'S': r['payment_format']},
                    'score': {'N': repr(w['score'])}, 'model_version': {'S': MODEL_VERSION},
                    'scored_at_ms': {'N': str(now_ms)}, 'arrival_ms': {'N': str(w['arrival_ms'])},
                    'ingest_to_score_ms': {'N': str(now_ms - w['arrival_ms'])}}
            items.append({'Put': {'TableName': SEEN, 'Item': {'txn': {'S': w['tid']}, 'exp': {'N': str(int(time.time()) + TTL_SECONDS)}},
                                  'ConditionExpression': 'attribute_not_exists(txn)'}})
            items.append({'Put': {'TableName': SCORES, 'Item': dict(base, f={'S': json.dumps(w['f'], separators=(',', ':'))}, seq={'S': w['seq']})}})
            if w['score'] >= THRESH:
                alerts += 1
                items.append({'Put': {'TableName': ALERTS, 'Item': dict(base, threshold={'N': repr(THRESH)})}})
        ddb.transact_write_items(TransactItems=items)
    print(json.dumps({'batch': len(event['Records']), 'scored': len(work), 'skipped_seen': len(seen), 'dup_in_batch': dup_in_batch,
                      'chunks': len(chunks), 'alerts': alerts, 'handler_ms': int((time.time() - t_in) * 1000)}))
    return {'scored': len(work)}
