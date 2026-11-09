#!/usr/bin/env python3
"""Create / update the real-time path. Run from ~/rt in CloudShell.
  python3 deploy_rt.py base <alert_email>            tables, queues, roles, ECR repo, alert bus + pipe, producer job, budget
  python3 deploy_rt.py lambda <image_uri> <threshold> create or update the scorer Lambda
Read-only towards v1/v2: the only v2 objects touched are GETs of the frozen model and schema."""
import sys, json, time
import boto3

R, ACCT, B = 'us-east-1', '941377154820', 'is459-aml-g1t5'
TAGK, TAGV = 'project', 'is459-g1t5-rt'
TAGS_L, TAGS_D = [{'Key': TAGK, 'Value': TAGV}], {TAGK: TAGV}
STREAM_ARN = 'arn:aws:kinesis:%s:%s:stream/is459-rt-stream' % (R, ACCT)
TABLES = ['is459_rt_state', 'is459_rt_seen', 'is459_rt_scores', 'is459_rt_alerts']
ddb, sqs, iam = boto3.client('dynamodb', region_name=R), boto3.client('sqs', region_name=R), boto3.client('iam')
ecr, lam, ev = boto3.client('ecr', region_name=R), boto3.client('lambda', region_name=R), boto3.client('events', region_name=R)
pipes, glue, s3 = boto3.client('pipes', region_name=R), boto3.client('glue', region_name=R), boto3.client('s3', region_name=R)
budgets = boto3.client('budgets', region_name=R)


def role(name, service, policy, managed=()):
    trust = {'Version': '2012-10-17', 'Statement': [{'Effect': 'Allow', 'Principal': {'Service': service}, 'Action': 'sts:AssumeRole'}]}
    try:
        arn = iam.get_role(RoleName=name)['Role']['Arn']
    except iam.exceptions.NoSuchEntityException:
        arn = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust), Tags=TAGS_L)['Role']['Arn']
        time.sleep(10)
    iam.put_role_policy(RoleName=name, PolicyName=name + '-inline', PolicyDocument=json.dumps({'Version': '2012-10-17', 'Statement': policy}))
    for m in managed:
        iam.attach_role_policy(RoleName=name, PolicyArn=m)
    return arn


def table_arn(t):
    return 'arn:aws:dynamodb:%s:%s:table/%s' % (R, ACCT, t)


def base(email):
    for t, key in [('is459_rt_state', 'pk'), ('is459_rt_seen', 'txn'), ('is459_rt_scores', 'txn'), ('is459_rt_alerts', 'txn')]:
        try:
            ddb.describe_table(TableName=t)
        except ddb.exceptions.ResourceNotFoundException:
            kw = {'StreamSpecification': {'StreamEnabled': True, 'StreamViewType': 'NEW_IMAGE'}} if t == 'is459_rt_alerts' else {}
            ddb.create_table(TableName=t, BillingMode='PAY_PER_REQUEST', AttributeDefinitions=[{'AttributeName': key, 'AttributeType': 'S'}],
                             KeySchema=[{'AttributeName': key, 'KeyType': 'HASH'}], Tags=TAGS_L, **kw)
    for t in TABLES:
        ddb.get_waiter('table_exists').wait(TableName=t)
    try:
        ddb.update_time_to_live(TableName='is459_rt_seen', TimeToLiveSpecification={'Enabled': True, 'AttributeName': 'exp'})
    except ddb.exceptions.ClientError as e:
        if 'already enabled' not in str(e): raise
    dlq = sqs.create_queue(QueueName='is459-rt-scorer-dlq', tags=TAGS_D, Attributes={'MessageRetentionPeriod': '1209600'})['QueueUrl']
    evq = sqs.create_queue(QueueName='is459-rt-alert-events', tags=TAGS_D, Attributes={'MessageRetentionPeriod': '345600'})['QueueUrl']
    dlq_arn = sqs.get_queue_attributes(QueueUrl=dlq, AttributeNames=['QueueArn'])['Attributes']['QueueArn']
    evq_arn = sqs.get_queue_attributes(QueueUrl=evq, AttributeNames=['QueueArn'])['Attributes']['QueueArn']
    try:
        ecr.create_repository(repositoryName='is459-rt-scorer', tags=TAGS_L, imageScanningConfiguration={'scanOnPush': False})
    except ecr.exceptions.RepositoryAlreadyExistsException:
        pass
    ecr.put_lifecycle_policy(repositoryName='is459-rt-scorer', lifecyclePolicyText=json.dumps({'rules': [{'rulePriority': 1, 'description': 'keep 3',
        'selection': {'tagStatus': 'any', 'countType': 'imageCountMoreThan', 'countNumber': 3}, 'action': {'type': 'expire'}}]}))
    role('is459-rt-scorer-role', 'lambda.amazonaws.com', [
        {'Effect': 'Allow', 'Action': ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents'], 'Resource': 'arn:aws:logs:%s:%s:log-group:/aws/lambda/is459-rt-*:*' % (R, ACCT)},
        {'Effect': 'Allow', 'Action': ['kinesis:GetRecords', 'kinesis:GetShardIterator', 'kinesis:DescribeStream', 'kinesis:DescribeStreamSummary',
                                        'kinesis:ListShards', 'kinesis:ListStreams', 'kinesis:SubscribeToShard'], 'Resource': [STREAM_ARN, STREAM_ARN + '/*']},
        {'Effect': 'Allow', 'Action': 'kinesis:ListStreams', 'Resource': '*'},
        {'Effect': 'Allow', 'Action': ['dynamodb:BatchGetItem', 'dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:ConditionCheckItem', 'dynamodb:BatchWriteItem'],
         'Resource': [table_arn(t) for t in TABLES]},
        {'Effect': 'Allow', 'Action': 's3:GetObject', 'Resource': ['arn:aws:s3:::%s/exported/etl-v2/models/bq1/v1/model.ubj' % B,
                                                                    'arn:aws:s3:::%s/exported/etl-v2/live-verified/bq1/feature_schema.json' % B]},
        {'Effect': 'Allow', 'Action': 'sqs:SendMessage', 'Resource': dlq_arn}])
    bus_arn = 'arn:aws:events:%s:%s:event-bus/is459-rt-alerts' % (R, ACCT)
    try:
        ev.create_event_bus(Name='is459-rt-alerts', Tags=TAGS_L)
    except ev.exceptions.ResourceAlreadyExistsException:
        pass
    rule_arn = ev.put_rule(Name='is459-rt-alert-to-queue', EventBusName='is459-rt-alerts', State='ENABLED', Tags=TAGS_L,
                           EventPattern=json.dumps({'source': ['is459.rt.scorer'], 'detail-type': ['rt-alert']}))['RuleArn']
    sqs.set_queue_attributes(QueueUrl=evq, Attributes={'Policy': json.dumps({'Version': '2012-10-17', 'Statement': [{'Effect': 'Allow',
        'Principal': {'Service': 'events.amazonaws.com'}, 'Action': 'sqs:SendMessage', 'Resource': evq_arn, 'Condition': {'ArnEquals': {'aws:SourceArn': rule_arn}}}]})})
    ev.put_targets(Rule='is459-rt-alert-to-queue', EventBusName='is459-rt-alerts', Targets=[{'Id': 'agent-queue', 'Arn': evq_arn}])
    stream_arn = ddb.describe_table(TableName='is459_rt_alerts')['Table']['LatestStreamArn']
    pipe_role = role('is459-rt-pipe-role', 'pipes.amazonaws.com', [
        {'Effect': 'Allow', 'Action': ['dynamodb:DescribeStream', 'dynamodb:GetRecords', 'dynamodb:GetShardIterator', 'dynamodb:ListStreams'], 'Resource': stream_arn},
        {'Effect': 'Allow', 'Action': 'events:PutEvents', 'Resource': bus_arn}])
    tmpl = ('{"transaction_id": <$.dynamodb.NewImage.txn.S>, "txn_ts": <$.dynamodb.NewImage.txn_ts.S>, "sender": <$.dynamodb.NewImage.sender.S>, '
            '"receiver": <$.dynamodb.NewImage.receiver.S>, "amount": <$.dynamodb.NewImage.amount.S>, "currency": <$.dynamodb.NewImage.currency.S>, '
            '"payment_format": <$.dynamodb.NewImage.payment_format.S>, "score": <$.dynamodb.NewImage.score.N>, '
            '"model_version": <$.dynamodb.NewImage.model_version.S>, "scored_at_ms": <$.dynamodb.NewImage.scored_at_ms.N>, '
            '"ingest_to_score_ms": <$.dynamodb.NewImage.ingest_to_score_ms.N>, "threshold": <$.dynamodb.NewImage.threshold.N>}')
    spec = dict(RoleArn=pipe_role, Source=stream_arn, Target=bus_arn, DesiredState='RUNNING',
                SourceParameters={'DynamoDBStreamParameters': {'StartingPosition': 'TRIM_HORIZON', 'BatchSize': 10},
                                  'FilterCriteria': {'Filters': [{'Pattern': json.dumps({'eventName': ['INSERT']})}]}},
                TargetParameters={'InputTemplate': tmpl, 'EventBridgeEventBusParameters': {'DetailType': 'rt-alert', 'Source': 'is459.rt.scorer'}})
    for attempt in range(6):
        try:
            try:
                pipes.describe_pipe(Name='is459-rt-alert-pipe')
                spec2 = {k: v for k, v in spec.items() if k not in ('Source',)}
                spec2['SourceParameters'] = {'DynamoDBStreamParameters': {'BatchSize': 10}, 'FilterCriteria': spec['SourceParameters']['FilterCriteria']}
                pipes.update_pipe(Name='is459-rt-alert-pipe', **spec2)
            except pipes.exceptions.NotFoundException:
                pipes.create_pipe(Name='is459-rt-alert-pipe', Tags=TAGS_D, **spec)
            break
        except pipes.exceptions.ValidationException as e:
            print('pipe role not ready yet', e); time.sleep(10)
    prod_role = role('is459-rt-producer-role', 'glue.amazonaws.com', [
        {'Effect': 'Allow', 'Action': 's3:GetObject', 'Resource': ['arn:aws:s3:::%s/rt/bootstrap/*' % B, 'arn:aws:s3:::%s/rt/code/*' % B]},
        {'Effect': 'Allow', 'Action': ['s3:PutObject', 's3:PutObjectTagging'], 'Resource': 'arn:aws:s3:::%s/rt/runs/*' % B},
        {'Effect': 'Allow', 'Action': ['kinesis:PutRecords', 'kinesis:PutRecord', 'kinesis:DescribeStreamSummary'], 'Resource': STREAM_ARN}],
        managed=['arn:aws:iam::aws:policy/service-role/AWSGlueServiceRole'])
    s3.upload_file('rt_producer.py', B, 'rt/code/rt_producer.py', ExtraArgs={'Tagging': 'project=is459-g1t5-rt'})
    job = {'Role': prod_role, 'Command': {'Name': 'pythonshell', 'ScriptLocation': 's3://%s/rt/code/rt_producer.py' % B, 'PythonVersion': '3.9'},
           'MaxCapacity': 0.0625, 'Timeout': 90, 'MaxRetries': 0, 'ExecutionProperty': {'MaxConcurrentRuns': 1},
           'DefaultArguments': {'--stream': 'is459-rt-stream', '--speedup': '4', '--minutes': '0', '--resend': '0', '--job-language': 'python'},
           'Description': 'IS459 RT: replay one bootstrapped slice into is459-rt-stream in timestamp order (no label)'}
    try:
        glue.get_job(JobName='is459_rt_producer')
        glue.update_job(JobName='is459_rt_producer', JobUpdate=job)
    except glue.exceptions.EntityNotFoundException:
        glue.create_job(Name='is459_rt_producer', Tags=TAGS_D, **job)
    try:
        budgets.create_budget(AccountId=ACCT, Budget={'BudgetName': 'is459-rt-guardrail', 'BudgetLimit': {'Amount': '15', 'Unit': 'USD'},
                                                     'TimeUnit': 'MONTHLY', 'BudgetType': 'COST'},
                              NotificationsWithSubscribers=[{'Notification': {'NotificationType': 'ACTUAL', 'ComparisonOperator': 'GREATER_THAN', 'Threshold': p,
                                                                              'ThresholdType': 'PERCENTAGE'}, 'Subscribers': [{'SubscriptionType': 'EMAIL', 'Address': email}]}
                                                            for p in (50.0, 80.0, 100.0)])
    except budgets.exceptions.DuplicateRecordException:
        pass
    print('BASE_DONE', json.dumps({'dlq': dlq_arn, 'alert_queue': evq_arn, 'bus': bus_arn, 'pipe_source': stream_arn}))


def function(image, threshold):
    arn = 'arn:aws:iam::%s:role/is459-rt-scorer-role' % ACCT
    env = {'Variables': {'ALERT_THRESHOLD': str(threshold)}}
    try:
        lam.get_function(FunctionName='is459-rt-scorer')
        lam.update_function_code(FunctionName='is459-rt-scorer', ImageUri=image)
        lam.get_waiter('function_updated_v2').wait(FunctionName='is459-rt-scorer')
        lam.update_function_configuration(FunctionName='is459-rt-scorer', Environment=env, MemorySize=1536, Timeout=120)
    except lam.exceptions.ResourceNotFoundException:
        for attempt in range(6):
            try:
                lam.create_function(FunctionName='is459-rt-scorer', PackageType='Image', Code={'ImageUri': image}, Role=arn, MemorySize=1536,
                                    Timeout=120, Environment=env, Tags=TAGS_D, Architectures=['x86_64'],
                                    Description='IS459 RT prototype: per-transaction BQ1 features + frozen xgboost 3.2.0 score (iter 0-96)')
                break
            except lam.exceptions.InvalidParameterValueException as e:
                print('role not ready yet', e); time.sleep(10)
    lam.get_waiter('function_active_v2').wait(FunctionName='is459-rt-scorer')
    lam.get_waiter('function_updated_v2').wait(FunctionName='is459-rt-scorer')
    print('LAMBDA_DONE', image, threshold)


if __name__ == '__main__':
    if sys.argv[1] == 'base':
        base(sys.argv[2])
    else:
        function(sys.argv[2], float(sys.argv[3]))
