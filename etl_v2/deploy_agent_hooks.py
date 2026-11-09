#!/usr/bin/env python3
"""Historical agent integration (docs/decisions.md): the hooks the daily pipeline exposes for the AI agent. Run from CloudShell.

  python3 deploy_agent_hooks.py all       # views + decisions table + day-ready trigger + agent read policy
  python3 deploy_agent_hooks.py views     # only (re)create the label-free Athena views
  python3 deploy_agent_hooks.py check     # print what exists

What it creates (all tagged project=is459-g1t5-etl-v2, nothing is written to Silver or Gold):
  * Glue database is459_agent with three Athena views over the Gold v2 tables. Each view selects every column
    except the label columns (LABEL_COLS). The script prints which columns it dropped for each table.
  * DynamoDB is459_agent_decisions, key transaction_id (S), on-demand. Written only by the agent, read by Streamlit.
    Key name: transaction_id, the same as Gold, so a decision joins straight onto bq1_risk_scores_medium_v2.
  * SQS is459-etl-v2-day-ready (+ DLQ) and EventBridge rule is459-etl-v2-day-succeeded on the default bus:
    Step Functions is459-etl-v2-silver-wap status SUCCEEDED -> queue. The message is the full status-change
    event; the agent reads json.loads(event['detail']['input'])['day'].
  * Managed policy is459-agent-readonly: Athena queries, Glue reads, S3 reads of the Gold v2 data, plus
    DynamoDB write on the decisions table and SQS consume on the queue. Attach it to the agent's role.
    Note: with plain IAM (no Lake Formation) Athena needs read on the base tables behind a view, so this policy
    cannot by itself stop a hand-written query on the base table. The guarantee is the agent's tools only
    naming is459_agent.* views; for a hard guarantee add a Lake Formation column filter later.
"""
import json, sys, time
import boto3

R = 'us-east-1'; B = 'is459-aml-g1t5'; ACCT = '941377154820'
TAGK, TAGV = 'project', 'is459-g1t5-etl-v2'
GOLD = 'is459_aml_gold'; VDB = 'is459_agent'
TABLES = ['bq1_risk_scores_medium_v2', 'bq1_features_medium_v2', 'bq2_graph_edges_medium_v2']
LABEL_COLS = {'is_laundering', 'evaluation_is_laundering', 'label', 'is_laundering_label', 'laundering_label'}
SM = 'arn:aws:states:%s:%s:stateMachine:is459-etl-v2-silver-wap' % (R, ACCT)
OUT = 's3://%s/athena-results/agent/' % B
glue = boto3.client('glue', region_name=R); ath = boto3.client('athena', region_name=R)
ddb = boto3.client('dynamodb', region_name=R); sqs = boto3.client('sqs', region_name=R)
ev = boto3.client('events', region_name=R); iam = boto3.client('iam')


def athena(sql):
    q = ath.start_query_execution(QueryString=sql, ResultConfiguration={'OutputLocation': OUT})['QueryExecutionId']
    while True:
        s = ath.get_query_execution(QueryExecutionId=q)['QueryExecution']['Status']
        if s['State'] in ('SUCCEEDED', 'FAILED', 'CANCELLED'):
            break
        time.sleep(1)
    if s['State'] != 'SUCCEEDED':
        raise RuntimeError('%s: %s' % (sql[:80], s.get('StateChangeReason')))


def views():
    try:
        glue.create_database(DatabaseInput={'Name': VDB, 'Description': 'Label-free views over Gold v2 for the AI agent'},
                             Tags={TAGK: TAGV})
        print('created database', VDB)
    except glue.exceptions.AlreadyExistsException:
        pass
    for t in TABLES:
        tb = glue.get_table(DatabaseName=GOLD, Name=t)['Table']
        cols = [c['Name'] for c in tb['StorageDescriptor']['Columns']] + [c['Name'] for c in tb.get('PartitionKeys', [])]
        keep = [c for c in cols if c.lower() not in LABEL_COLS]
        dropped = [c for c in cols if c.lower() in LABEL_COLS]
        name = 'v_' + t.replace('_medium_v2', '')
        athena('CREATE OR REPLACE VIEW %s.%s AS SELECT %s FROM %s.%s' % (VDB, name, ', '.join('"%s"' % c for c in keep), GOLD, t))
        print('view %s.%s: %d columns, dropped %s' % (VDB, name, len(keep), dropped or 'none (no label column in this table)'))


def table():
    try:
        ddb.create_table(TableName='is459_agent_decisions', BillingMode='PAY_PER_REQUEST',
                         AttributeDefinitions=[{'AttributeName': 'transaction_id', 'AttributeType': 'S'}],
                         KeySchema=[{'AttributeName': 'transaction_id', 'KeyType': 'HASH'}],
                         Tags=[{'Key': TAGK, 'Value': TAGV}])
        ddb.get_waiter('table_exists').wait(TableName='is459_agent_decisions')
        print('created is459_agent_decisions')
    except ddb.exceptions.ResourceInUseException:
        print('is459_agent_decisions exists')


def trigger():
    dlq = sqs.create_queue(QueueName='is459-etl-v2-day-ready-dlq', tags={TAGK: TAGV})['QueueUrl']
    dlq_arn = sqs.get_queue_attributes(QueueUrl=dlq, AttributeNames=['QueueArn'])['Attributes']['QueueArn']
    q = sqs.create_queue(QueueName='is459-etl-v2-day-ready', tags={TAGK: TAGV},
                         Attributes={'MessageRetentionPeriod': '1209600', 'VisibilityTimeout': '900',
                                     'RedrivePolicy': json.dumps({'deadLetterTargetArn': dlq_arn, 'maxReceiveCount': '5'})})['QueueUrl']
    q_arn = sqs.get_queue_attributes(QueueUrl=q, AttributeNames=['QueueArn'])['Attributes']['QueueArn']
    rule = ev.put_rule(Name='is459-etl-v2-day-succeeded', State='ENABLED',
                       Description='ETL v2 day committed to Gold -> agent queue (Trigger B)',
                       EventPattern=json.dumps({'source': ['aws.states'], 'detail-type': ['Step Functions Execution Status Change'],
                                                'detail': {'status': ['SUCCEEDED'], 'stateMachineArn': [SM]}}),
                       Tags=[{'Key': TAGK, 'Value': TAGV}])['RuleArn']
    sqs.set_queue_attributes(QueueUrl=q, Attributes={'Policy': json.dumps({'Version': '2012-10-17', 'Statement': [{
        'Effect': 'Allow', 'Principal': {'Service': 'events.amazonaws.com'}, 'Action': 'sqs:SendMessage', 'Resource': q_arn,
        'Condition': {'ArnEquals': {'aws:SourceArn': rule}}}]})})
    ev.put_targets(Rule='is459-etl-v2-day-succeeded', Targets=[{'Id': 'agent-queue', 'Arn': q_arn}])
    print('trigger', rule, '->', q_arn)
    return q_arn


def policy(q_arn):
    doc = {'Version': '2012-10-17', 'Statement': [
        {'Sid': 'Athena', 'Effect': 'Allow', 'Action': ['athena:StartQueryExecution', 'athena:GetQueryExecution',
                                                         'athena:GetQueryResults', 'athena:StopQueryExecution', 'athena:GetWorkGroup'], 'Resource': '*'},
        {'Sid': 'Catalog', 'Effect': 'Allow', 'Action': ['glue:GetDatabase', 'glue:GetTable', 'glue:GetTables', 'glue:GetPartition',
                                                          'glue:GetPartitions'],
         'Resource': ['arn:aws:glue:%s:%s:catalog' % (R, ACCT), 'arn:aws:glue:%s:%s:database/%s' % (R, ACCT, VDB),
                      'arn:aws:glue:%s:%s:table/%s/*' % (R, ACCT, VDB), 'arn:aws:glue:%s:%s:database/%s' % (R, ACCT, GOLD)] +
                     ['arn:aws:glue:%s:%s:table/%s/%s' % (R, ACCT, GOLD, t) for t in TABLES]},
        {'Sid': 'GoldRead', 'Effect': 'Allow', 'Action': ['s3:GetObject', 's3:ListBucket', 's3:GetBucketLocation'],
         'Resource': ['arn:aws:s3:::%s' % B, 'arn:aws:s3:::%s/gold/*' % B]},
        {'Sid': 'AthenaResults', 'Effect': 'Allow', 'Action': ['s3:GetObject', 's3:PutObject', 's3:AbortMultipartUpload'],
         'Resource': 'arn:aws:s3:::%s/athena-results/agent/*' % B},
        {'Sid': 'Decisions', 'Effect': 'Allow', 'Action': ['dynamodb:PutItem', 'dynamodb:UpdateItem', 'dynamodb:GetItem', 'dynamodb:Query'],
         'Resource': 'arn:aws:dynamodb:%s:%s:table/is459_agent_decisions' % (R, ACCT)},
        {'Sid': 'Trigger', 'Effect': 'Allow', 'Action': ['sqs:ReceiveMessage', 'sqs:DeleteMessage', 'sqs:GetQueueAttributes',
                                                          'sqs:ChangeMessageVisibility'], 'Resource': q_arn}]}
    arn = 'arn:aws:iam::%s:policy/is459-agent-readonly' % ACCT
    try:
        iam.get_policy(PolicyArn=arn)
        vs = iam.list_policy_versions(PolicyArn=arn)['Versions']
        for v in vs:
            if not v['IsDefaultVersion'] and len(vs) >= 5:
                iam.delete_policy_version(PolicyArn=arn, VersionId=v['VersionId']); break
        iam.create_policy_version(PolicyArn=arn, PolicyDocument=json.dumps(doc), SetAsDefault=True)
        print('updated policy', arn)
    except iam.exceptions.NoSuchEntityException:
        iam.create_policy(PolicyName='is459-agent-readonly', PolicyDocument=json.dumps(doc), Tags=[{'Key': TAGK, 'Value': TAGV}],
                          Description='AI agent: Athena reads over Gold v2 views, decisions table write, day-ready queue consume')
        print('created policy', arn)
    print('NOTE: check the gold/ S3 prefix above matches the Gold v2 table locations printed by "check"')


def check():
    for t in TABLES:
        print(t, glue.get_table(DatabaseName=GOLD, Name=t)['Table']['StorageDescriptor']['Location'])
    try:
        print([t['Name'] for t in glue.get_tables(DatabaseName=VDB)['TableList']])
    except glue.exceptions.EntityNotFoundException:
        print('no', VDB)
    print(ev.list_rules(NamePrefix='is459-etl-v2-day')['Rules'])


if __name__ == '__main__':
    a = sys.argv[1] if len(sys.argv) > 1 else 'check'
    if a == 'views':
        views()
    elif a == 'all':
        views(); table(); policy(trigger()); check()
    else:
        check()
