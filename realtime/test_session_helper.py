"""Local test of session_helper with moto: drain counting, briefs waiting, stop idempotency, summary and reaper decisions."""
import os, json, time, io, zipfile
os.environ.update(AWS_DEFAULT_REGION='us-east-1', AWS_ACCESS_KEY_ID='x', AWS_SECRET_ACCESS_KEY='x')
from moto import mock_aws


@mock_aws
def run():
    import boto3
    R = 'us-east-1'
    ddb, s3, sns, kin = (boto3.client(x, region_name=R) for x in ('dynamodb', 's3', 'sns', 'kinesis'))
    iam, lam, glue, sqs = (boto3.client(x, region_name=R) for x in ('iam', 'lambda', 'glue', 'sqs'))
    budgets = boto3.client('budgets', region_name=R)
    for t, k in [('is459_rt_seen', 'txn'), ('is459_rt_alerts', 'txn'), ('is459_rt_briefs', 'txn'), ('is459_rt_scores', 'txn')]:
        ddb.create_table(TableName=t, BillingMode='PAY_PER_REQUEST', AttributeDefinitions=[{'AttributeName': k, 'AttributeType': 'S'}],
                         KeySchema=[{'AttributeName': k, 'KeyType': 'HASH'}])
    s3.create_bucket(Bucket='is459-aml-g1t5')
    sns.create_topic(Name='is459-rt-session-topic')
    sqs.create_queue(QueueName='is459-rt-scorer-dlq')
    glue.create_job(Name='is459_rt_producer', Role='r', Command={'Name': 'pythonshell', 'ScriptLocation': 's3://x/y.py'})
    budgets.create_budget(AccountId='123456789012', Budget={'BudgetName': 'is459-rt-guardrail', 'BudgetLimit': {'Amount': '25', 'Unit': 'USD'},
                                                            'TimeUnit': 'MONTHLY', 'BudgetType': 'COST'})
    role = iam.create_role(RoleName='r', AssumeRolePolicyDocument='{}')['Role']['Arn']
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('a.py', 'def h(e, c): return 1')
    lam.create_function(FunctionName='is459-rt-scorer', Runtime='python3.12', Role=role, Handler='a.h', Code={'ZipFile': buf.getvalue()})

    import session_helper as H
    H.ACCT = '123456789012'
    H.TOPIC = 'arn:aws:sns:us-east-1:123456789012:is459-rt-session-topic'
    H.DLQ = 'arn:aws:sqs:us-east-1:123456789012:is459-rt-scorer-dlq'
    H.SM = 'arn:aws:states:us-east-1:123456789012:stateMachine:is459-rt-session'
    boto3.client('stepfunctions', region_name=R).create_state_machine(name='is459-rt-session', definition='{"StartAt":"a","States":{"a":{"Type":"Succeed"}}}', roleArn=role)

    out = H.init({'tag': 's0220h1', 'exec_name': 'abc-123'})
    assert out['run_id'] == 's0220h1-abc-123' and out['budget_ok'] is True and out['slice_exists'] is False, out
    start = out['start_epoch']
    # 3 markers from this session, 2 older ones
    for i, exp in enumerate([start + H.SEEN_TTL + 1, start + H.SEEN_TTL + 5, start + H.SEEN_TTL + 9, start - 100, start + 10]):
        ddb.put_item(TableName='is459_rt_seen', Item={'txn': {'S': 't%d' % i}, 'exp': {'N': str(exp)}})
    s3.put_object(Bucket='is459-aml-g1t5', Key='rt/runs/%s/producer.json' % out['run_id'],
                  Body=json.dumps({'rows': 3, 'finished': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}).encode())
    d = H.drain({'run_id': out['run_id'], 'start_epoch': start})
    assert d['done'] and d['scored'] == 3 and not d['timeout'], d
    # alerts: two in this session, one older
    for t, ms in [('a1', out['start_ms'] + 10), ('a2', out['start_ms'] + 20), ('old', out['start_ms'] - 10)]:
        ddb.put_item(TableName='is459_rt_alerts', Item={'txn': {'S': t}, 'score': {'N': '0.9'}, 'txn_ts': {'S': '2022-09-16 02:21:00'},
                                                        'scored_at_ms': {'N': str(ms)}})
    b = H.briefs({'start_ms': out['start_ms'], 'drained_at': int(time.time())})
    assert b == {'done': False, 'timeout': False, 'alerts': 2, 'briefs': 0}, b
    ddb.put_item(TableName='is459_rt_briefs', Item={'txn': {'S': 'a1'}, 'typology': {'S': 'fan_out'}, 'confidence': {'S': 'high'},
                                                    'narrative': {'S': 'Sender paid 9 new receivers in 24 hours.'}})
    ddb.put_item(TableName='is459_rt_briefs', Item={'txn': {'S': 'a2'}, 'typology': {'S': 'none_evident'}, 'confidence': {'S': 'low'},
                                                    'narrative': {'S': 'Nothing stands out.'}})
    b = H.briefs({'start_ms': out['start_ms'], 'drained_at': int(time.time())})
    assert b['done'] and b['briefs'] == 2, b
    # stop is idempotent with nothing running, and removes a stream + mapping
    assert H.stop({})['mappings'] == 0
    kin.create_stream(StreamName='is459-rt-stream', ShardCount=1)
    arn = kin.describe_stream_summary(StreamName='is459-rt-stream')['StreamDescriptionSummary']['StreamARN']
    lam.create_event_source_mapping(FunctionName='is459-rt-scorer', EventSourceArn=arn, StartingPosition='TRIM_HORIZON')
    assert len(H.mappings()) == 1
    r = H.reap({})
    assert r['reaped'] is True, r
    assert H.stream_exists() is None and not H.mappings()
    r = H.reap({})
    assert r['reaped'] is False, r
    # summary of a failed session with no validate.json still emails and reports not ok
    s3.put_object(Bucket='is459-aml-g1t5', Key='rt/runs/%s/validate.json' % out['run_id'],
                  Body=json.dumps({'parity': 'PASS', 'v1_v2_untouched': True, 'latency_ms': {'p50': 2400}}).encode())
    H.cost_estimate = lambda ms: {'usd_estimate': 1.2}
    s = H.summary({'status': 'completed', 'state': {'ctx': out, 'tag': 's0220h1', 'slice_start': 'a', 'slice_end': 'b'}})
    assert s['ok'] and s['alerts'] == 2 and s['briefs'] == 2, s
    s = H.summary({'status': 'failed', 'state': {'ctx': out, 'error': {'Error': 'X', 'Cause': 'boom'}}})
    assert not s['ok']
    body = s3.get_object(Bucket='is459-aml-g1t5', Key='rt/runs/%s/session_summary.txt' % out['run_id'])['Body'].read().decode()
    assert 'boom' in body and 'fan_out' in body, body
    print(body)
    print('SESSION_HELPER_TESTS_PASS')


run()


@mock_aws
def run_start():
    import boto3, io, zipfile
    R = 'us-east-1'
    iam, lam, sqs = (boto3.client(x, region_name=R) for x in ('iam', 'lambda', 'sqs'))
    q = sqs.create_queue(QueueName='is459-rt-scorer-dlq')
    role = iam.create_role(RoleName='r', AssumeRolePolicyDocument='{}')['Role']['Arn']
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('a.py', 'def h(e, c): return 1')
    lam.create_function(FunctionName='is459-rt-scorer', Runtime='python3.12', Role=role, Handler='a.h', Code={'ZipFile': buf.getvalue()})
    import importlib, session_helper as H
    importlib.reload(H)
    H.DLQ = 'arn:aws:sqs:us-east-1:123456789012:is459-rt-scorer-dlq'
    out = H.start({})
    assert out['mapping'] and H.stream_exists() == 'ACTIVE', out
    out2 = H.start({})  # idempotent: no second mapping
    assert len(H.mappings()) == 1
    print('START_TESTS_PASS')


run_start()
