#!/usr/bin/env python3
"""Run several days through the ETL v2 pipeline in order, server-side, one at a time.
A tiny Step Functions machine (is459-etl-v2-backfill) maps over the days with MaxConcurrency 1 and starts the existing
pipeline machine with .sync, so the next day starts only after the previous one SUCCEEDED (Gold commit needs day order).
Any failed day stops the run; later days are not attempted.
  python3 deploy_backfill.py deploy
  python3 deploy_backfill.py run 2022-09-18 2022-09-27 [run_tag]
  python3 deploy_backfill.py status
"""
import sys, json, time, datetime as dt
import boto3

R, A = 'us-east-1', '941377154820'
PIPE = 'arn:aws:states:%s:%s:stateMachine:is459-etl-v2-silver-wap' % (R, A)
NAME, ROLE = 'is459-etl-v2-backfill', 'is459-etl-v2-backfill-role'
SRC = 'bronze/ibm-aml/li-medium/transactions/'
sf = boto3.client('stepfunctions', region_name=R)
iam = boto3.client('iam')

ASL = {
    'Comment': 'IS459 ETL v2: run days through is459-etl-v2-silver-wap strictly in order',
    'StartAt': 'Days',
    'States': {'Days': {
        'Type': 'Map', 'ItemsPath': '$.days', 'MaxConcurrency': 1,
        'ItemProcessor': {'ProcessorConfig': {'Mode': 'INLINE'}, 'StartAt': 'RunDay', 'States': {'RunDay': {
            'Type': 'Task', 'Resource': 'arn:aws:states:::states:startExecution.sync:2',
            'Parameters': {'StateMachineArn': PIPE,
                           'Name.$': "States.Format('pipeline-{}-{}', $.day_compact, $.run_tag)",
                           'Input': {'day.$': '$.day', 'day_compact.$': '$.day_compact',
                                     'run_tag.$': '$.run_tag', 'source_csv_key.$': '$.source_csv_key'}},
            'ResultSelector': {'day.$': '$.Input', 'status.$': '$.Status', 'execution.$': '$.ExecutionArn',
                               'start.$': '$.StartDate', 'stop.$': '$.StopDate'},
            'End': True}}},
        'End': True}}}

POLICY = {'Version': '2012-10-17', 'Statement': [
    {'Effect': 'Allow', 'Action': 'states:StartExecution', 'Resource': PIPE},
    {'Effect': 'Allow', 'Action': ['states:DescribeExecution', 'states:StopExecution'],
     'Resource': 'arn:aws:states:%s:%s:execution:is459-etl-v2-silver-wap:*' % (R, A)},
    {'Effect': 'Allow', 'Action': ['events:PutTargets', 'events:PutRule', 'events:DescribeRule'],
     'Resource': 'arn:aws:events:%s:%s:rule/StepFunctionsGetEventsForStepFunctionsExecutionRule' % (R, A)}]}
TRUST = {'Version': '2012-10-17', 'Statement': [{'Effect': 'Allow', 'Principal': {'Service': 'states.amazonaws.com'},
                                                 'Action': 'sts:AssumeRole'}]}
TAGS = [{'Key': 'project', 'Value': 'is459-g1t5-etl'}]


def deploy():
    try:
        arn = iam.create_role(RoleName=ROLE, AssumeRolePolicyDocument=json.dumps(TRUST), Tags=TAGS)['Role']['Arn']
        time.sleep(12)
    except iam.exceptions.EntityAlreadyExistsException:
        arn = iam.get_role(RoleName=ROLE)['Role']['Arn']
    iam.put_role_policy(RoleName=ROLE, PolicyName='run-pipeline-in-order', PolicyDocument=json.dumps(POLICY))
    sm = 'arn:aws:states:%s:%s:stateMachine:%s' % (R, A, NAME)
    try:
        sf.describe_state_machine(stateMachineArn=sm)
        sf.update_state_machine(stateMachineArn=sm, definition=json.dumps(ASL), roleArn=arn)
    except sf.exceptions.StateMachineDoesNotExist:
        for i in range(5):
            try:
                sf.create_state_machine(name=NAME, definition=json.dumps(ASL), roleArn=arn, tags=[{'key': 'project', 'value': 'is459-g1t5-etl'}])
                break
            except Exception as e:  # new role can take a few seconds to be assumable
                print('retry', e); time.sleep(10)
        else:
            raise SystemExit('could not create state machine')
    print('DEPLOYED', sm)


def run(first, last, tag):
    d0, d1 = dt.date.fromisoformat(first), dt.date.fromisoformat(last)
    days = [{'day': (d0 + dt.timedelta(i)).isoformat(), 'day_compact': (d0 + dt.timedelta(i)).strftime('%Y%m%d'),
             'run_tag': tag, 'source_csv_key': SRC} for i in range((d1 - d0).days + 1)]
    r = sf.start_execution(stateMachineArn='arn:aws:states:%s:%s:stateMachine:%s' % (R, A, NAME),
                           name='backfill-%s-%s-%s' % (days[0]['day_compact'], days[-1]['day_compact'], tag),
                           input=json.dumps({'days': days}))
    print('STARTED', r['executionArn'], len(days), 'days')


def status():
    ex = sf.list_executions(stateMachineArn='arn:aws:states:%s:%s:stateMachine:%s' % (R, A, NAME), maxResults=1)['executions']
    if ex:
        print('BACKFILL', ex[0]['name'], ex[0]['status'])
    for e in sf.list_executions(stateMachineArn=PIPE, maxResults=12)['executions']:
        print(e['name'], e['status'], e['startDate'].strftime('%H:%M'), e.get('stopDate').strftime('%H:%M') if e.get('stopDate') else '-')


if __name__ == '__main__':
    c = sys.argv[1]
    if c == 'deploy':
        deploy()
    elif c == 'run':
        run(sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else dt.datetime.utcnow().strftime('bf%m%d%H%M'))
    else:
        status()
