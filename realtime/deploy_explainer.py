#!/usr/bin/env python3
"""Phase C: deploy the alert explainer. Run from ~/rt in CloudShell (or CodeBuild through release.py).
  python3 deploy_explainer.py base                          briefs table, queue + DLQ, second alert-rule target, ECR repo, role, Athena table
  python3 deploy_explainer.py candidate <image_uri> <ver>   new Lambda version with that image and model rt/models/explainer/<ver>/, alias 'candidate'
  python3 deploy_explainer.py promote                       alias 'live' -> the candidate version; SQS trigger on 'live'
  python3 deploy_explainer.py rollback <version>            alias 'live' -> an earlier version
Read-only towards v1/v2 (Athena reads Silver for the as-of context only)."""
import os, sys, json, time
import boto3

R, ACCT, B = 'us-east-1', '941377154820', 'is459-aml-g1t5'
TAGK, TAGV = 'project', 'is459-g1t5-rt'
TAGS_L, TAGS_D = [{'Key': TAGK, 'Value': TAGV}], {TAGK: TAGV}
FN, Q, DLQ, TABLE = 'is459-rt-explainer', 'is459-rt-explain-queue', 'is459-rt-explain-dlq', 'is459_rt_briefs'
ddb, sqs, iam = boto3.client('dynamodb', region_name=R), boto3.client('sqs', region_name=R), boto3.client('iam')
ecr, lam, ev = boto3.client('ecr', region_name=R), boto3.client('lambda', region_name=R), boto3.client('events', region_name=R)
s3, ath = boto3.client('s3', region_name=R), boto3.client('athena', region_name=R)
RULE, BUS = 'is459-rt-alert-to-queue', 'is459-rt-alerts'


def role(name, service, policy):
    trust = {'Version': '2012-10-17', 'Statement': [{'Effect': 'Allow', 'Principal': {'Service': service}, 'Action': 'sts:AssumeRole'}]}
    try:
        arn = iam.get_role(RoleName=name)['Role']['Arn']
    except iam.exceptions.NoSuchEntityException:
        arn = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust), Tags=TAGS_L)['Role']['Arn']
        time.sleep(10)
    iam.put_role_policy(RoleName=name, PolicyName=name + '-inline', PolicyDocument=json.dumps({'Version': '2012-10-17', 'Statement': policy}))
    return arn


def athena(sql):
    qid = ath.start_query_execution(QueryString=sql, ResultConfiguration={'OutputLocation': 's3://%s/athena-results/rt/explainer/' % B})['QueryExecutionId']
    while True:
        st = ath.get_query_execution(QueryExecutionId=qid)['QueryExecution']['Status']
        if st['State'] in ('SUCCEEDED', 'FAILED', 'CANCELLED'):
            break
        time.sleep(1)
    if st['State'] != 'SUCCEEDED':
        raise SystemExit('athena: ' + st.get('StateChangeReason', ''))


def queue_arn(url):
    return sqs.get_queue_attributes(QueueUrl=url, AttributeNames=['QueueArn'])['Attributes']['QueueArn']


def base():
    try:
        ddb.describe_table(TableName=TABLE)
    except ddb.exceptions.ResourceNotFoundException:
        ddb.create_table(TableName=TABLE, BillingMode='PAY_PER_REQUEST', AttributeDefinitions=[{'AttributeName': 'txn', 'AttributeType': 'S'}],
                         KeySchema=[{'AttributeName': 'txn', 'KeyType': 'HASH'}], Tags=TAGS_L)
        ddb.get_waiter('table_exists').wait(TableName=TABLE)
    dlq = sqs.create_queue(QueueName=DLQ, tags=TAGS_D, Attributes={'MessageRetentionPeriod': '1209600'})['QueueUrl']
    q = sqs.create_queue(QueueName=Q, tags=TAGS_D, Attributes={'MessageRetentionPeriod': '345600', 'VisibilityTimeout': '1080',
                                                               'RedrivePolicy': json.dumps({'deadLetterTargetArn': queue_arn(dlq), 'maxReceiveCount': '3'})})['QueueUrl']
    sqs.set_queue_attributes(QueueUrl=q, Attributes={'VisibilityTimeout': '1080', 'RedrivePolicy': json.dumps({'deadLetterTargetArn': queue_arn(dlq), 'maxReceiveCount': '3'})})
    qarn = queue_arn(q)
    rule_arn = ev.describe_rule(Name=RULE, EventBusName=BUS)['Arn']
    sqs.set_queue_attributes(QueueUrl=q, Attributes={'Policy': json.dumps({'Version': '2012-10-17', 'Statement': [{'Effect': 'Allow',
        'Principal': {'Service': 'events.amazonaws.com'}, 'Action': 'sqs:SendMessage', 'Resource': qarn, 'Condition': {'ArnEquals': {'aws:SourceArn': rule_arn}}}]})})
    targets = ev.list_targets_by_rule(Rule=RULE, EventBusName=BUS)['Targets']
    if not any(t['Id'] == 'explainer-queue' for t in targets):
        ev.put_targets(Rule=RULE, EventBusName=BUS, Targets=[{'Id': 'explainer-queue', 'Arn': qarn}])
    try:
        ecr.create_repository(repositoryName=FN, tags=TAGS_L, imageScanningConfiguration={'scanOnPush': False})
    except ecr.exceptions.RepositoryAlreadyExistsException:
        pass
    ecr.put_lifecycle_policy(repositoryName=FN, lifecyclePolicyText=json.dumps({'rules': [{'rulePriority': 1, 'description': 'keep 3',
        'selection': {'tagStatus': 'any', 'countType': 'imageCountMoreThan', 'countNumber': 3}, 'action': {'type': 'expire'}}]}))
    tarn = lambda t: 'arn:aws:dynamodb:%s:%s:table/%s' % (R, ACCT, t)
    role('is459-rt-explainer-role', 'lambda.amazonaws.com', [
        {'Effect': 'Allow', 'Action': ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents'],
         'Resource': 'arn:aws:logs:%s:%s:log-group:/aws/lambda/is459-rt-*:*' % (R, ACCT)},
        {'Effect': 'Allow', 'Action': ['dynamodb:GetItem'], 'Resource': tarn('is459_rt_scores')},
        {'Effect': 'Allow', 'Action': ['dynamodb:PutItem', 'dynamodb:GetItem'], 'Resource': tarn(TABLE)},
        {'Effect': 'Allow', 'Action': ['sqs:ReceiveMessage', 'sqs:DeleteMessage', 'sqs:GetQueueAttributes', 'sqs:ChangeMessageVisibility'], 'Resource': qarn},
        {'Effect': 'Allow', 'Action': ['s3:GetObject'], 'Resource': ['arn:aws:s3:::%s/rt/models/*' % B, 'arn:aws:s3:::%s/silver/v2/*' % B,
                                                                      'arn:aws:s3:::%s/athena-results/rt/explainer/*' % B]},
        {'Effect': 'Allow', 'Action': ['s3:PutObject', 's3:PutObjectTagging'], 'Resource': ['arn:aws:s3:::%s/rt/briefs/*' % B,
                                                                                            'arn:aws:s3:::%s/athena-results/rt/explainer/*' % B]},
        {'Effect': 'Allow', 'Action': ['s3:ListBucket', 's3:GetBucketLocation'], 'Resource': 'arn:aws:s3:::%s' % B},
        {'Effect': 'Allow', 'Action': ['athena:StartQueryExecution', 'athena:GetQueryExecution', 'athena:GetQueryResults', 'athena:GetWorkGroup'], 'Resource': '*'},
        {'Effect': 'Allow', 'Action': ['glue:GetTable', 'glue:GetDatabase', 'glue:GetPartitions', 'glue:GetPartition'], 'Resource': '*'}])
    athena('create database if not exists is459_rt')
    athena("""create external table if not exists is459_rt.alert_briefs (
      txn string, txn_ts string, score double, typology string, confidence string, narrative string, next_checks array<string>,
      status string, typology_hint string, typology_hint_prob double, model_version string, latency_ms bigint)
    partitioned by (dt string)
    row format serde 'org.openx.data.jsonserde.JsonSerDe'
    location 's3://%s/rt/briefs/'
    tblproperties ('projection.enabled'='true', 'projection.dt.type'='date', 'projection.dt.format'='yyyy-MM-dd',
                   'projection.dt.range'='2022-09-01,2022-12-31', 'storage.location.template'='s3://%s/rt/briefs/dt=${dt}/')""" % (B, B))
    print('EXPLAINER_BASE_DONE', json.dumps({'queue': qarn, 'table': TABLE}))


def candidate(image, ver):
    pre = 'rt/models/explainer/%s/' % ver
    sha = s3.get_object(Bucket=B, Key=pre + 'model.sha256')['Body'].read().decode().strip()
    env = {'Variables': {'MODEL_KEY': pre + 'model.gguf', 'MODEL_SHA': sha, 'MODEL_VERSION': 'explainer-' + ver, 'TYPOLOGY_MODE': os.environ.get('TYPOLOGY_MODE', 'model'),
                         'CLF_KEY': pre + 'typology_clf.json', 'CLF_META_KEY': pre + 'typology_clf_meta.json'}}
    arn = 'arn:aws:iam::%s:role/is459-rt-explainer-role' % ACCT
    mem = 10240
    for attempt in range(8):
        try:
            try:
                lam.get_function(FunctionName=FN)
                lam.update_function_code(FunctionName=FN, ImageUri=image)
                lam.get_waiter('function_updated_v2').wait(FunctionName=FN)
                lam.update_function_configuration(FunctionName=FN, Environment=env, MemorySize=mem, Timeout=180, EphemeralStorage={'Size': 4096})
            except lam.exceptions.ResourceNotFoundException:
                lam.create_function(FunctionName=FN, PackageType='Image', Code={'ImageUri': image}, Role=arn, MemorySize=mem, Timeout=180,
                                    EphemeralStorage={'Size': 4096}, Environment=env, Tags=TAGS_D, Architectures=['x86_64'],
                                    Description='IS459 RT: grounded JSON brief per alert from a fine-tuned 1.5B model')
            break
        except lam.exceptions.InvalidParameterValueException as e:
            if 'MemorySize' in str(e) and mem > 3008:
                print('memory quota below 10240 MB, falling back to 3008:', e); mem = 3008
                continue
            print('not ready yet', e); time.sleep(10)
    lam.get_waiter('function_updated_v2').wait(FunctionName=FN)
    v = lam.publish_version(FunctionName=FN, Description='%s %s' % (ver, image.split('@')[-1][:19]))['Version']
    try:
        lam.update_alias(FunctionName=FN, Name='candidate', FunctionVersion=v)
    except lam.exceptions.ResourceNotFoundException:
        lam.create_alias(FunctionName=FN, Name='candidate', FunctionVersion=v)
    print('CANDIDATE', v, 'memory', mem, json.dumps(env['Variables']))
    return v


def set_live(v):
    try:
        lam.update_alias(FunctionName=FN, Name='live', FunctionVersion=v)
    except lam.exceptions.ResourceNotFoundException:
        lam.create_alias(FunctionName=FN, Name='live', FunctionVersion=v)
    live_arn = 'arn:aws:lambda:%s:%s:function:%s:live' % (R, ACCT, FN)
    qarn = queue_arn(sqs.get_queue_url(QueueName=Q)['QueueUrl'])
    maps = lam.list_event_source_mappings(FunctionName=live_arn, EventSourceArn=qarn).get('EventSourceMappings', [])
    if not maps:
        for attempt in range(6):
            try:
                lam.create_event_source_mapping(FunctionName=live_arn, EventSourceArn=qarn, BatchSize=1, Enabled=True,
                                                ScalingConfig={'MaximumConcurrency': 2})
                break
            except lam.exceptions.InvalidParameterValueException as e:
                print('mapping not ready', e); time.sleep(10)
    print('LIVE', v)


def promote():
    v = lam.get_alias(FunctionName=FN, Name='candidate')['FunctionVersion']
    set_live(v)


if __name__ == '__main__':
    cmd = sys.argv[1]
    if cmd == 'base':
        base()
    elif cmd == 'candidate':
        candidate(sys.argv[2], sys.argv[3])
    elif cmd == 'promote':
        promote()
    elif cmd == 'rollback':
        set_live(sys.argv[2])
    else:
        raise SystemExit(__doc__)
