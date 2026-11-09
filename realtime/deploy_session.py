#!/usr/bin/env python3
"""Phase A: automate a real-time session end to end. Run from ~/rt in CloudShell.
  python3 deploy_session.py all <email>                 topic, roles, control job, helper Lambda, state machine, reaper, alarms, schedule (DISABLED)
  python3 deploy_session.py start '<input json>'        start one session now
  python3 deploy_session.py schedule <UTC yyyy-mm-ddThh:mm:ss> '<input json>'   one-time scheduled session (ENABLED)
  python3 deploy_session.py disable                     switch the schedule off
Session input: {"slice_start":"2022-09-16T02:20:00","slice_end":"2022-09-16T03:20:00","tag":"s0220h1",
                "speedup":"1","spread":"1","mode":"fresh|continue"}
Read-only towards v1/v2. Everything created is tagged project=is459-g1t5-rt."""
import sys, json, time, io, zipfile
import boto3

R, ACCT, B = 'us-east-1', '941377154820', 'is459-aml-g1t5'
TAGK, TAGV = 'project', 'is459-g1t5-rt'
TAGS_L, TAGS_D = [{'Key': TAGK, 'Value': TAGV}], {TAGK: TAGV}
iam, lam, glue = boto3.client('iam'), boto3.client('lambda', region_name=R), boto3.client('glue', region_name=R)
sfn, sns, ev = boto3.client('stepfunctions', region_name=R), boto3.client('sns', region_name=R), boto3.client('events', region_name=R)
cw, s3, sched = boto3.client('cloudwatch', region_name=R), boto3.client('s3', region_name=R), boto3.client('scheduler', region_name=R)
TOPIC_NAME, SM_NAME, HELPER, CONTROL = 'is459-rt-session-topic', 'is459-rt-session', 'is459-rt-session-helper', 'is459_rt_control'
SM_ARN = 'arn:aws:states:%s:%s:stateMachine:%s' % (R, ACCT, SM_NAME)
TOPIC_ARN = 'arn:aws:sns:%s:%s:%s' % (R, ACCT, TOPIC_NAME)
HELPER_ARN = 'arn:aws:lambda:%s:%s:function:%s' % (R, ACCT, HELPER)
RT_TABLES = ['is459_rt_state', 'is459_rt_seen', 'is459_rt_scores', 'is459_rt_alerts', 'is459_rt_briefs']
CODE = ['rt_control.py', 'rt_core.py', 'bootstrap_logic.py', 'bootstrap.py', 'load_state.py', 'state_check.py', 'validate_rt.py']


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


def tarn(t):
    return 'arn:aws:dynamodb:%s:%s:table/%s' % (R, ACCT, t)


def topic(email):
    arn = sns.create_topic(Name=TOPIC_NAME, Tags=TAGS_L)['TopicArn']
    subs = sns.list_subscriptions_by_topic(TopicArn=arn)['Subscriptions']
    if email and email not in [s['Endpoint'] for s in subs]:
        sns.subscribe(TopicArn=arn, Protocol='email', Endpoint=email)
        print('SUBSCRIBED', email, '(confirm the email from AWS Notifications)')
    return arn


def control_job():
    for f in CODE:
        s3.upload_file(f, B, 'rt/code/' + f, ExtraArgs={'Tagging': 'project=is459-g1t5-rt'})
    q = 'arn:aws:sqs:%s:%s:is459-rt-alert-events' % (R, ACCT)
    arn = role('is459-rt-control-role', 'glue.amazonaws.com', [
        {'Effect': 'Allow', 'Action': ['s3:GetObject'], 'Resource': 'arn:aws:s3:::%s/*' % B},
        {'Effect': 'Allow', 'Action': ['s3:ListBucket', 's3:GetBucketLocation'], 'Resource': 'arn:aws:s3:::%s' % B},
        {'Effect': 'Allow', 'Action': ['s3:PutObject', 's3:PutObjectTagging', 's3:AbortMultipartUpload'],
         'Resource': ['arn:aws:s3:::%s/rt/*' % B, 'arn:aws:s3:::%s/athena-results/rt/*' % B]},
        {'Effect': 'Allow', 'Action': ['athena:StartQueryExecution', 'athena:GetQueryExecution', 'athena:GetQueryResults', 'athena:StopQueryExecution',
                                        'athena:GetWorkGroup'], 'Resource': '*'},
        {'Effect': 'Allow', 'Action': ['glue:GetTable', 'glue:GetTables', 'glue:GetDatabase', 'glue:GetDatabases', 'glue:GetPartition', 'glue:GetPartitions'],
         'Resource': '*'},
        {'Effect': 'Allow', 'Action': ['dynamodb:BatchGetItem', 'dynamodb:BatchWriteItem', 'dynamodb:Scan', 'dynamodb:DescribeTable', 'dynamodb:GetItem'],
         'Resource': [tarn(t) for t in RT_TABLES]},
        {'Effect': 'Allow', 'Action': ['sqs:GetQueueUrl', 'sqs:GetQueueAttributes', 'sqs:ReceiveMessage'], 'Resource': q},
        {'Effect': 'Allow', 'Action': ['cloudwatch:GetMetricStatistics'], 'Resource': '*'}],
        managed=['arn:aws:iam::aws:policy/service-role/AWSGlueServiceRole'])
    job = {'Role': arn, 'Command': {'Name': 'pythonshell', 'ScriptLocation': 's3://%s/rt/code/rt_control.py' % B, 'PythonVersion': '3.9'},
           'MaxCapacity': 1.0, 'Timeout': 60, 'MaxRetries': 0, 'ExecutionProperty': {'MaxConcurrentRuns': 2},
           'DefaultArguments': {'--job-language': 'python', '--tag': 'none', '--run_id': 'none', '--slice_start': 'none', '--slice_end': 'none'},
           'Description': 'IS459 RT: one control step of a real-time session (bootstrap, starting state, guard, validate)'}
    try:
        glue.get_job(JobName=CONTROL)
        glue.update_job(JobName=CONTROL, JobUpdate=job)
    except glue.exceptions.EntityNotFoundException:
        glue.create_job(Name=CONTROL, Tags=TAGS_D, **job)
    print('CONTROL_JOB ready')


def helper():
    kstream = 'arn:aws:kinesis:%s:%s:stream/is459-rt-stream' % (R, ACCT)
    arn = role('is459-rt-helper-role', 'lambda.amazonaws.com', [
        {'Effect': 'Allow', 'Action': ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents'],
         'Resource': 'arn:aws:logs:%s:%s:log-group:/aws/lambda/is459-rt-*:*' % (R, ACCT)},
        {'Effect': 'Allow', 'Action': ['kinesis:CreateStream', 'kinesis:DeleteStream', 'kinesis:DescribeStreamSummary', 'kinesis:DescribeStream',
                                        'kinesis:AddTagsToStream'], 'Resource': kstream},
        {'Effect': 'Allow', 'Action': ['kinesis:ListStreams'], 'Resource': '*'},
        {'Effect': 'Allow', 'Action': ['lambda:ListEventSourceMappings', 'lambda:CreateEventSourceMapping', 'lambda:DeleteEventSourceMapping',
                                        'lambda:GetEventSourceMapping', 'lambda:TagResource'], 'Resource': '*'},
        {'Effect': 'Allow', 'Action': ['dynamodb:Scan', 'dynamodb:BatchGetItem', 'dynamodb:DescribeTable'], 'Resource': [tarn(t) for t in RT_TABLES]},
        {'Effect': 'Allow', 'Action': ['s3:GetObject'], 'Resource': 'arn:aws:s3:::%s/rt/*' % B},
        {'Effect': 'Allow', 'Action': ['s3:PutObject', 's3:PutObjectTagging'], 'Resource': 'arn:aws:s3:::%s/rt/runs/*' % B},
        {'Effect': 'Allow', 'Action': ['s3:ListBucket'], 'Resource': 'arn:aws:s3:::%s' % B},
        {'Effect': 'Allow', 'Action': ['sns:Publish'], 'Resource': TOPIC_ARN},
        {'Effect': 'Allow', 'Action': ['glue:GetJobRuns', 'glue:BatchStopJobRun'], 'Resource': 'arn:aws:glue:%s:%s:job/is459_rt_producer' % (R, ACCT)},
        {'Effect': 'Allow', 'Action': ['states:ListExecutions'], 'Resource': SM_ARN},
        {'Effect': 'Allow', 'Action': ['cloudwatch:GetMetricStatistics'], 'Resource': '*'},
        {'Effect': 'Allow', 'Action': ['budgets:ViewBudget'], 'Resource': '*'}])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        z.write('session_helper.py')
    code = buf.getvalue()
    cfg = dict(Runtime='python3.12', Handler='session_helper.handler', Timeout=300, MemorySize=256, Role=arn,
               Description='IS459 RT: session steps (start, drain, briefs, stop, summary) and hourly reaper')
    for attempt in range(6):
        try:
            try:
                lam.get_function(FunctionName=HELPER)
                lam.update_function_code(FunctionName=HELPER, ZipFile=code)
                lam.get_waiter('function_updated_v2').wait(FunctionName=HELPER)
                lam.update_function_configuration(FunctionName=HELPER, **cfg)
            except lam.exceptions.ResourceNotFoundException:
                lam.create_function(FunctionName=HELPER, Code={'ZipFile': code}, Tags=TAGS_D, **cfg)
            break
        except lam.exceptions.InvalidParameterValueException as e:
            print('role not ready yet', e); time.sleep(10)
    lam.get_waiter('function_updated_v2').wait(FunctionName=HELPER)
    print('HELPER ready')


def L(name, payload, result_path, selector=None):
    st = {'Type': 'Task', 'Resource': 'arn:aws:states:::lambda:invoke', 'Parameters': {'FunctionName': HELPER_ARN, 'Payload': payload},
          'ResultPath': result_path,
          'Retry': [{'ErrorEquals': ['Lambda.ServiceException', 'Lambda.AWSLambdaException', 'Lambda.SdkClientException', 'Lambda.TooManyRequestsException'],
                     'IntervalSeconds': 2, 'MaxAttempts': 4, 'BackoffRate': 2}]}
    st['ResultSelector'] = selector or {'out.$': '$.Payload'}
    return st


def G(job, args, nxt):
    return {'Type': 'Task', 'Resource': 'arn:aws:states:::glue:startJobRun.sync', 'Parameters': {'JobName': job, 'Arguments': args},
            'ResultPath': None, 'Next': nxt, 'Catch': [{'ErrorEquals': ['States.ALL'], 'ResultPath': '$.error', 'Next': 'StopOnError'}]}


def ctl(action):
    return {'--action': action, '--tag.$': '$.tag', '--run_id.$': '$.ctx.run_id', '--slice_start.$': '$.slice_start', '--slice_end.$': '$.slice_end'}


def catch(st, nxt='StopOnError'):
    st['Catch'] = [{'ErrorEquals': ['States.ALL'], 'ResultPath': '$.error', 'Next': nxt}]
    return st


def asl():
    S = {}
    S['Init'] = catch(dict(L('init', {'action': 'init', 'tag.$': '$.tag', 'exec_name.$': '$$.Execution.Name'}, '$.ctx',
                            {'run_id.$': '$.Payload.run_id', 'start_epoch.$': '$.Payload.start_epoch', 'start_ms.$': '$.Payload.start_ms',
                             'budget_ok.$': '$.Payload.budget_ok', 'slice_exists.$': '$.Payload.slice_exists'}), Next='CheckBudget'))
    S['CheckBudget'] = {'Type': 'Choice', 'Choices': [{'Variable': '$.ctx.budget_ok', 'BooleanEquals': False, 'Next': 'BudgetBlocked'}], 'Default': 'HaveSlice'}
    S['BudgetBlocked'] = {'Type': 'Pass', 'Result': {'Error': 'BudgetAbove80Percent', 'Cause': 'is459-rt-guardrail actual spend is above 80% of its limit'},
                          'ResultPath': '$.error', 'Next': 'NotifyError'}
    S['HaveSlice'] = {'Type': 'Choice', 'Choices': [{'Variable': '$.ctx.slice_exists', 'BooleanEquals': True, 'Next': 'StartingState'}], 'Default': 'Bootstrap'}
    S['Bootstrap'] = G(CONTROL, ctl('bootstrap'), 'StartingState')
    S['StartingState'] = {'Type': 'Choice', 'Choices': [{'Variable': '$.mode', 'StringEquals': 'continue', 'Next': 'StateCheck'}], 'Default': 'LoadState'}
    S['LoadState'] = G(CONTROL, ctl('load_state'), 'GuardBefore')
    S['StateCheck'] = G(CONTROL, ctl('state_check'), 'GuardBefore')
    S['GuardBefore'] = G(CONTROL, ctl('guard'), 'StartSession')
    S['StartSession'] = catch(dict(L('start', {'action': 'start'}, '$.session'), Next='Produce'))
    S['Produce'] = G('is459_rt_producer', {'--slice_key.$': "States.Format('rt/bootstrap/{}/slice.csv', $.tag)", '--run_id.$': '$.ctx.run_id',
                                           '--speedup.$': '$.speedup', '--minutes': '0', '--resend': '0', '--spread.$': '$.spread'}, 'DrainWait')
    S['DrainWait'] = {'Type': 'Wait', 'Seconds': 30, 'Next': 'DrainCheck'}
    S['DrainCheck'] = catch(dict(L('drain', {'action': 'drain', 'run_id.$': '$.ctx.run_id', 'start_epoch.$': '$.ctx.start_epoch'}, '$.drain',
                                 {'done.$': '$.Payload.done', 'timeout.$': '$.Payload.timeout', 'scored.$': '$.Payload.scored',
                                  'expected.$': '$.Payload.expected', 'drained_at.$': '$.Payload.drained_at'}), Next='Drained'))
    S['Drained'] = {'Type': 'Choice', 'Choices': [{'Variable': '$.drain.done', 'BooleanEquals': True, 'Next': 'BriefsWait'},
                                                  {'Variable': '$.drain.timeout', 'BooleanEquals': True, 'Next': 'DrainTimedOut'}], 'Default': 'DrainWait'}
    S['DrainTimedOut'] = {'Type': 'Pass', 'Result': {'Error': 'DrainTimeout', 'Cause': 'not every produced record was scored within 30 minutes'},
                          'ResultPath': '$.error', 'Next': 'StopOnError'}
    S['BriefsWait'] = {'Type': 'Wait', 'Seconds': 20, 'Next': 'BriefsCheck'}
    S['BriefsCheck'] = catch(dict(L('briefs', {'action': 'briefs', 'start_ms.$': '$.ctx.start_ms', 'drained_at.$': '$.drain.drained_at'}, '$.briefs',
                                  {'done.$': '$.Payload.done', 'timeout.$': '$.Payload.timeout', 'alerts.$': '$.Payload.alerts', 'briefs.$': '$.Payload.briefs'}),
                                 Next='BriefsReady'))
    S['BriefsReady'] = {'Type': 'Choice', 'Choices': [{'Or': [{'Variable': '$.briefs.done', 'BooleanEquals': True},
                                                              {'Variable': '$.briefs.timeout', 'BooleanEquals': True}], 'Next': 'Validate'}],
                        'Default': 'BriefsWait'}
    S['Validate'] = G(CONTROL, ctl('validate'), 'Stop')
    stop = L('stop', {'action': 'stop'}, '$.teardown')
    stop['Retry'].append({'ErrorEquals': ['States.ALL'], 'IntervalSeconds': 10, 'MaxAttempts': 5, 'BackoffRate': 2})
    S['Stop'] = dict(stop, Next='Summary')
    S['Summary'] = catch(dict(L('summary', {'action': 'summary', 'status': 'completed', 'state.$': '$'}, '$.summary',
                                {'ok.$': '$.Payload.ok', 'parity.$': '$.Payload.parity', 'alerts.$': '$.Payload.alerts', 'briefs.$': '$.Payload.briefs'}),
                              Next='Passed'), nxt='SessionFailed')
    S['Passed'] = {'Type': 'Choice', 'Choices': [{'Variable': '$.summary.ok', 'BooleanEquals': True, 'Next': 'SessionPassed'}], 'Default': 'SessionChecksFailed'}
    S['SessionPassed'] = {'Type': 'Succeed'}
    S['SessionChecksFailed'] = {'Type': 'Fail', 'Error': 'SessionChecksFailed', 'Cause': 'parity or the v1/v2 guard did not pass; see the summary email'}
    S['StopOnError'] = dict(stop, ResultPath='$.teardown', Next='NotifyError')
    S['StopOnError'] = catch(S['StopOnError'], 'NotifyError')
    S['StopOnError']['Catch'][0]['ResultPath'] = '$.teardown_error'
    S['NotifyError'] = catch(dict(L('summary', {'action': 'summary', 'status': 'failed', 'state.$': '$'}, '$.summary'), Next='SessionFailed'), 'SessionFailed')
    S['NotifyError']['Catch'][0]['ResultPath'] = '$.notify_error'
    S['SessionFailed'] = {'Type': 'Fail', 'Error': 'SessionFailed', 'Cause': 'a session step failed; the stream was torn down; see the email'}
    return {'Comment': 'IS459 real-time session: bootstrap, starting state, guard, stream, produce, drain, briefs, validate, always stop, notify',
            'StartAt': 'Init', 'TimeoutSeconds': 4 * 3600, 'States': S}


def state_machine():
    arn = role('is459-rt-session-sfn-role', 'states.amazonaws.com', [
        {'Effect': 'Allow', 'Action': ['lambda:InvokeFunction'], 'Resource': [HELPER_ARN, HELPER_ARN + ':*']},
        {'Effect': 'Allow', 'Action': ['glue:StartJobRun', 'glue:GetJobRun', 'glue:GetJobRuns', 'glue:BatchStopJobRun'],
         'Resource': ['arn:aws:glue:%s:%s:job/%s' % (R, ACCT, CONTROL), 'arn:aws:glue:%s:%s:job/is459_rt_producer' % (R, ACCT)]}])
    definition = json.dumps(asl())
    for attempt in range(6):
        try:
            try:
                sfn.describe_state_machine(stateMachineArn=SM_ARN)
                sfn.update_state_machine(stateMachineArn=SM_ARN, definition=definition, roleArn=arn)
            except sfn.exceptions.StateMachineDoesNotExist:
                sfn.create_state_machine(name=SM_NAME, definition=definition, roleArn=arn, type='STANDARD', tags=[{'key': TAGK, 'value': TAGV}])
            break
        except (sfn.exceptions.InvalidDefinition, sfn.exceptions.ClientError) as e:
            if 'InvalidDefinition' in str(type(e)) or 'SCHEMA_VALIDATION' in str(e):
                raise
            print('sfn role not ready yet', e); time.sleep(10)
    print('STATE_MACHINE ready', SM_ARN)


def reaper():
    rule = ev.put_rule(Name='is459-rt-reaper-hourly', ScheduleExpression='rate(1 hour)', State='ENABLED', Tags=TAGS_L,
                       Description='IS459 RT: delete a forgotten stream or mapping when no session is running')['RuleArn']
    try:
        lam.add_permission(FunctionName=HELPER, StatementId='reaper-events', Action='lambda:InvokeFunction', Principal='events.amazonaws.com', SourceArn=rule)
    except lam.exceptions.ResourceConflictException:
        pass
    ev.put_targets(Rule='is459-rt-reaper-hourly', Targets=[{'Id': 'helper', 'Arn': HELPER_ARN, 'Input': json.dumps({'action': 'reap'})}])
    print('REAPER ready')


def alarms():
    spec = [('is459-rt-scorer-dlq', 'AWS/SQS', 'ApproximateNumberOfMessagesVisible', {'QueueName': 'is459-rt-scorer-dlq'}, 'Maximum', 300, 0),
            ('is459-rt-explain-dlq', 'AWS/SQS', 'ApproximateNumberOfMessagesVisible', {'QueueName': 'is459-rt-explain-dlq'}, 'Maximum', 300, 0),
            ('is459-rt-stream-lag', 'AWS/Kinesis', 'GetRecords.IteratorAgeMilliseconds', {'StreamName': 'is459-rt-stream'}, 'Maximum', 300, 60000),
            ('is459-rt-explainer-errors', 'AWS/Lambda', 'Errors', {'FunctionName': 'is459-rt-explainer'}, 'Sum', 900, 0),
            ('is459-rt-session-failed', 'AWS/States', 'ExecutionsFailed', {'StateMachineArn': SM_ARN}, 'Sum', 300, 0)]
    for name, ns, metric, dims, stat, period, thr in spec:
        cw.put_metric_alarm(AlarmName=name, Namespace=ns, MetricName=metric, Dimensions=[{'Name': k, 'Value': v} for k, v in dims.items()],
                            Statistic=stat, Period=period, EvaluationPeriods=1, Threshold=thr, ComparisonOperator='GreaterThanThreshold',
                            TreatMissingData='notBreaching', AlarmActions=[TOPIC_ARN], Tags=TAGS_L,
                            AlarmDescription='IS459 RT: see the real-time runbook')
    print('ALARMS ready', len(spec))


def sched_role():
    return role('is459-rt-scheduler-role', 'scheduler.amazonaws.com', [{'Effect': 'Allow', 'Action': ['states:StartExecution'], 'Resource': SM_ARN}])


def put_schedule(expr, payload, state):
    kw = dict(Name='is459-rt-session-schedule', ScheduleExpression=expr, ScheduleExpressionTimezone='UTC', FlexibleTimeWindow={'Mode': 'OFF'},
              Target={'Arn': SM_ARN, 'RoleArn': sched_role(), 'Input': json.dumps(payload)}, State=state,
              Description='IS459 RT: start one real-time session; keep DISABLED outside demos')
    for attempt in range(6):
        try:
            try:
                sched.get_schedule(Name='is459-rt-session-schedule')
                sched.update_schedule(**kw)
            except sched.exceptions.ResourceNotFoundException:
                sched.create_schedule(**kw)
            break
        except sched.exceptions.ValidationException as e:
            if 'role' not in str(e).lower():
                raise
            print('scheduler role not ready yet', e); time.sleep(10)
    print('SCHEDULE', state, expr)


EXAMPLE = {'slice_start': '2022-09-16T02:20:00', 'slice_end': '2022-09-16T03:20:00', 'tag': 's0220h1', 'speedup': '1', 'spread': '1', 'mode': 'continue'}

if __name__ == '__main__':
    cmd = sys.argv[1]
    if cmd == 'all':
        topic(sys.argv[2] if len(sys.argv) > 2 else None)
        control_job(); helper(); state_machine(); reaper(); alarms()
        put_schedule('rate(1 day)', EXAMPLE, 'DISABLED')
        print('PHASE_A_DEPLOYED')
    elif cmd == 'asl':
        print(json.dumps(asl(), indent=1))
    elif cmd == 'start':
        r = sfn.start_execution(stateMachineArn=SM_ARN, input=sys.argv[2])
        print('STARTED', r['executionArn'])
    elif cmd == 'schedule':
        put_schedule('at(%s)' % sys.argv[2], json.loads(sys.argv[3]), 'ENABLED')
    elif cmd == 'disable':
        s = sched.get_schedule(Name='is459-rt-session-schedule')
        put_schedule(s['ScheduleExpression'], json.loads(s['Target']['Input']), 'DISABLED')
    else:
        raise SystemExit(__doc__)
