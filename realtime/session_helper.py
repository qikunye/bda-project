"""Lambda is459-rt-session-helper: the small steps of the is459-rt-session state machine, plus the hourly reaper.
Actions (event['action']):
  init     run id, session start time, budget check, whether the slice is already bootstrapped
  start    create the stream (1 shard), wait ACTIVE, tag it, create the scorer mapping, wait Enabled
  drain    has every produced record been scored? (seen markers committed since the session started)
  briefs   does every alert raised in this session have a brief yet? (explainer may be absent)
  stop     delete the mapping and the stream, stop a producer run that is still going; idempotent
  summary  read the run's results, email the summary, say whether the session passed
  reap     hourly: if a stream or mapping exists and no session is running, delete them and email
Read-only towards v1/v2. Never touches anything outside the real-time resources."""
import json, time, calendar
import boto3

R, ACCT, B = 'us-east-1', '941377154820', 'is459-aml-g1t5'
STREAM, FN = 'is459-rt-stream', 'is459-rt-scorer'
DLQ = 'arn:aws:sqs:%s:%s:is459-rt-scorer-dlq' % (R, ACCT)
TOPIC = 'arn:aws:sns:%s:%s:is459-rt-session-topic' % (R, ACCT)
SM = 'arn:aws:states:%s:%s:stateMachine:is459-rt-session' % (R, ACCT)
TAG = {'project': 'is459-g1t5-rt'}
SEEN_TTL = 14 * 86400
kin, lam, ddb = boto3.client('kinesis', region_name=R), boto3.client('lambda', region_name=R), boto3.client('dynamodb', region_name=R)
s3, sns, glue = boto3.client('s3', region_name=R), boto3.client('sns', region_name=R), boto3.client('glue', region_name=R)
sfn, cw = boto3.client('stepfunctions', region_name=R), boto3.client('cloudwatch', region_name=R)
budgets = boto3.client('budgets', region_name=R)


def s3json(key):
    try:
        return json.loads(s3.get_object(Bucket=B, Key=key)['Body'].read())
    except s3.exceptions.NoSuchKey:
        return None


def mappings():
    return [m for m in lam.list_event_source_mappings(FunctionName=FN).get('EventSourceMappings', [])
            if ':stream/%s' % STREAM in m.get('EventSourceArn', '')]


def stream_exists():
    try:
        return kin.describe_stream_summary(StreamName=STREAM)['StreamDescriptionSummary']['StreamStatus']
    except kin.exceptions.ResourceNotFoundException:
        return None


def scan_count(table, expr, vals, names=None):
    kw = {'TableName': table, 'Select': 'COUNT', 'FilterExpression': expr, 'ExpressionAttributeValues': vals}
    if names:
        kw['ExpressionAttributeNames'] = names
    n = 0
    while True:
        r = ddb.scan(**kw)
        n += r['Count']
        if 'LastEvaluatedKey' not in r:
            return n
        kw['ExclusiveStartKey'] = r['LastEvaluatedKey']


def scan_items(table, expr, vals, proj):
    kw = {'TableName': table, 'FilterExpression': expr, 'ExpressionAttributeValues': vals, 'ProjectionExpression': proj}
    out = []
    while True:
        r = ddb.scan(**kw)
        out += r['Items']
        if 'LastEvaluatedKey' not in r:
            return out
        kw['ExclusiveStartKey'] = r['LastEvaluatedKey']


def table_exists(t):
    try:
        ddb.describe_table(TableName=t)
        return True
    except ddb.exceptions.ResourceNotFoundException:
        return False


def init(e):
    now = time.time()
    b = budgets.describe_budget(AccountId=ACCT, BudgetName='is459-rt-guardrail')['Budget']
    actual = float(b.get('CalculatedSpend', {}).get('ActualSpend', {}).get('Amount', '0'))
    limit = float(b['BudgetLimit']['Amount'])
    run_id = '%s-%s' % (e['tag'], e['exec_name'][:36])
    exists = s3json('rt/bootstrap/%s/meta.json' % e['tag']) is not None
    return {'run_id': run_id, 'start_epoch': int(now), 'start_ms': int(now * 1000), 'budget_ok': actual <= 0.8 * limit,
            'budget_actual': actual, 'budget_limit': limit, 'slice_exists': exists}


def start(e):
    if stream_exists() is None:
        kin.create_stream(StreamName=STREAM, ShardCount=1, StreamModeDetails={'StreamMode': 'PROVISIONED'})
    kin.get_waiter('stream_exists').wait(StreamName=STREAM, WaiterConfig={'Delay': 5, 'MaxAttempts': 36})
    arn = kin.describe_stream_summary(StreamName=STREAM)['StreamDescriptionSummary']['StreamARN']
    kin.add_tags_to_stream(StreamName=STREAM, Tags=TAG)
    if not mappings():
        for attempt in range(8):
            try:
                lam.create_event_source_mapping(FunctionName=FN, EventSourceArn=arn, StartingPosition='TRIM_HORIZON', BatchSize=300,
                                                MaximumBatchingWindowInSeconds=1, ParallelizationFactor=1, MaximumRetryAttempts=3,
                                                BisectBatchOnFunctionError=True, MaximumRecordAgeInSeconds=3600,
                                                DestinationConfig={'OnFailure': {'Destination': DLQ}})
                break
            except lam.exceptions.InvalidParameterValueException as ex:
                print('mapping not ready', ex)
                time.sleep(10)
        else:
            raise RuntimeError('could not create the scorer mapping')
    for _ in range(40):
        st = mappings()[0]['State']
        if st == 'Enabled':
            return {'stream_arn': arn, 'mapping': mappings()[0]['UUID']}
        time.sleep(5)
    raise RuntimeError('mapping stuck in state ' + st)


def drain(e):
    prod = s3json('rt/runs/%s/producer.json' % e['run_id'])
    if prod is None:
        raise RuntimeError('producer.json missing for ' + e['run_id'])
    expected = prod['rows']
    scored = scan_count('is459_rt_seen', '#e >= :t', {':t': {'N': str(int(e['start_epoch']) + SEEN_TTL)}}, {'#e': 'exp'})
    fin = calendar.timegm(time.strptime(prod['finished'], '%Y-%m-%dT%H:%M:%SZ'))
    now = time.time()
    return {'done': scored >= expected, 'timeout': now > fin + 1800, 'scored': scored, 'expected': expected, 'drained_at': int(now)}


def session_alerts(start_ms):
    return scan_items('is459_rt_alerts', 'scored_at_ms >= :t', {':t': {'N': str(int(start_ms))}}, 'txn, score, txn_ts')


def briefs_for(ids):
    out = {}
    if not ids or not table_exists('is459_rt_briefs'):
        return out
    ids = list(ids)
    for i in range(0, len(ids), 100):
        req = {'is459_rt_briefs': {'Keys': [{'txn': {'S': t}} for t in ids[i:i + 100]]}}
        while req:
            r = ddb.batch_get_item(RequestItems=req)
            for it in r['Responses'].get('is459_rt_briefs', []):
                out[it['txn']['S']] = it
            req = r.get('UnprocessedKeys') or {}
    return out


def briefs(e):
    ids = [a['txn']['S'] for a in session_alerts(e['start_ms'])]
    if not table_exists('is459_rt_briefs'):
        return {'done': True, 'timeout': False, 'alerts': len(ids), 'briefs': 0, 'note': 'explainer not deployed'}
    have = briefs_for(ids)
    return {'done': len(have) >= len(ids), 'timeout': time.time() > int(e['drained_at']) + 600, 'alerts': len(ids), 'briefs': len(have)}


def stop(e=None):
    for m in mappings():
        try:
            lam.delete_event_source_mapping(UUID=m['UUID'])
        except lam.exceptions.ResourceNotFoundException:
            pass
    for _ in range(40):
        if not mappings():
            break
        time.sleep(5)
    if stream_exists() is not None:
        try:
            kin.delete_stream(StreamName=STREAM, EnforceConsumerDeletion=True)
        except kin.exceptions.ResourceNotFoundException:
            pass
        kin.get_waiter('stream_not_exists').wait(StreamName=STREAM, WaiterConfig={'Delay': 5, 'MaxAttempts': 40})
    active = [r['Id'] for r in glue.get_job_runs(JobName='is459_rt_producer', MaxResults=5)['JobRuns']
              if r['JobRunState'] in ('RUNNING', 'STARTING', 'WAITING')]
    if active:
        glue.batch_stop_job_run(JobName='is459_rt_producer', JobRunIds=active)
    left = {'streams': [s for s in kin.list_streams()['StreamNames'] if s == STREAM], 'mappings': len(mappings()), 'producer_runs_stopped': active}
    if left['streams'] or left['mappings']:
        raise RuntimeError('teardown incomplete ' + json.dumps(left))
    return left


def metric_sum(ns, name, dims, start, end, stat='Sum'):
    r = cw.get_metric_statistics(Namespace=ns, MetricName=name, Dimensions=[{'Name': k, 'Value': v} for k, v in dims.items()],
                                 StartTime=start, EndTime=end, Period=300, Statistics=[stat])
    return sum(p[stat] for p in r['Datapoints'])


def cost_estimate(start_ms):
    import datetime as dt
    s = dt.datetime.fromtimestamp(start_ms / 1000, dt.timezone.utc)
    e_ = dt.datetime.now(dt.timezone.utc)
    tables = ['is459_rt_state', 'is459_rt_seen', 'is459_rt_scores', 'is459_rt_alerts', 'is459_rt_briefs']
    wru = sum(metric_sum('AWS/DynamoDB', 'ConsumedWriteCapacityUnits', {'TableName': t}, s, e_) for t in tables)
    rru = sum(metric_sum('AWS/DynamoDB', 'ConsumedReadCapacityUnits', {'TableName': t}, s, e_) for t in tables)
    gbs = metric_sum('AWS/Lambda', 'Duration', {'FunctionName': FN}, s, e_) / 1000 * 1.5
    gbs += metric_sum('AWS/Lambda', 'Duration', {'FunctionName': 'is459-rt-explainer'}, s, e_) / 1000 * 10.0
    hours = (e_ - s).total_seconds() / 3600
    usd = wru / 1e6 * 0.625 + rru / 1e6 * 0.125 + gbs * 0.0000166667 + hours * 0.015 + 0.05
    return {'dynamodb_wru': int(wru), 'dynamodb_rru': int(rru), 'lambda_gb_s': round(gbs, 1), 'session_hours': round(hours, 2), 'usd_estimate': round(usd, 2)}


def summary(e):
    st = e.get('state', {})
    ctx = st.get('ctx', {})
    run = ctx.get('run_id', 'unknown')
    start_ms = ctx.get('start_ms', int((time.time() - 4 * 3600) * 1000))
    v = s3json('rt/runs/%s/validate.json' % run) or {}
    prod = s3json('rt/runs/%s/producer.json' % run) or {}
    alerts = session_alerts(start_ms)
    have = briefs_for([a['txn']['S'] for a in alerts])
    status = e.get('status', 'completed')
    ok = status == 'completed' and v.get('parity') == 'PASS' and v.get('v1_v2_untouched') is True
    lines = ['IS459 real-time session %s: %s' % (run, 'PASSED' if ok else 'FAILED'),
             'Slice: %s to %s (tag %s), mode %s, speed %sx   status: %s' % (st.get('slice_start'), st.get('slice_end'), st.get('tag'),
                                                                         st.get('mode'), st.get('speedup'), status)]
    if st.get('error'):
        err = st['error']
        lines.append('Error: %s %s' % (err.get('Error', ''), str(err.get('Cause', ''))[:600]))
    lines += ['Rows produced: %s   scored in slice: %s' % (prod.get('rows'), v.get('scored_rows_in_slice')),
              'Parity vs v2 Gold: %s (feature mismatches %s, largest score difference %s)' % (v.get('parity'), v.get('feature_mismatches'), v.get('score_max_abs_diff')),
              'Latency ms: %s' % json.dumps(v.get('latency_ms')),
              'v1/v2 untouched: %s' % v.get('v1_v2_untouched'),
              'Alerts this session: %d   briefs written: %d' % (len(alerts), len(have)), '']
    for a in sorted(alerts, key=lambda a: -float(a['score']['N']))[:25]:
        t = a['txn']['S']
        b = have.get(t)
        if b:
            lines.append('- %s score %.3f  %s (%s): %s' % (t, float(a['score']['N']), b.get('typology', {}).get('S'), b.get('confidence', {}).get('S'),
                                                         b.get('narrative', {}).get('S', '')[:300]))
        else:
            lines.append('- %s score %.3f  (no brief)' % (t, float(a['score']['N'])))
    try:
        c = cost_estimate(start_ms)
        lines += ['', 'Estimated session cost: US$%s %s' % (c['usd_estimate'], json.dumps(c))]
    except Exception as ex:
        c = {'error': str(ex)}
    left = {'streams': [s for s in kin.list_streams()['StreamNames'] if s == STREAM], 'mappings': len(mappings())}
    lines.append('Teardown check: %s' % json.dumps(left))
    body = '\n'.join(lines)
    sns.publish(TopicArn=TOPIC, Subject=('IS459 RT session %s %s' % ('PASSED' if ok else 'FAILED', run))[:99], Message=body)
    s3.put_object(Bucket=B, Key='rt/runs/%s/session_summary.txt' % run, Body=body.encode(), Tagging='project=is459-g1t5-rt')
    return {'ok': ok, 'parity': v.get('parity'), 'untouched': v.get('v1_v2_untouched'), 'alerts': len(alerts), 'briefs': len(have)}


def reap(e):
    running = sfn.list_executions(stateMachineArn=SM, statusFilter='RUNNING', maxResults=5).get('executions', [])
    st, maps = stream_exists(), mappings()
    if running or (st is None and not maps):
        return {'reaped': False, 'running_sessions': len(running), 'stream': st, 'mappings': len(maps)}
    left = stop()
    sns.publish(TopicArn=TOPIC, Subject='IS459 RT reaper removed a forgotten stream',
                Message='No session was running, but the stream (%s) or %d scorer mapping(s) existed. Both were deleted. %s' % (st, len(maps), json.dumps(left)))
    return {'reaped': True, 'stream': st, 'mappings': len(maps)}


ACTIONS = {'init': init, 'start': start, 'drain': drain, 'briefs': briefs, 'stop': stop, 'summary': summary, 'reap': reap}


def handler(event, context):
    out = ACTIONS[event['action']](event)
    print(json.dumps({'action': event['action'], 'out': out}, default=str))
    return out
