#!/usr/bin/env python3
"""Start / stop a real-time session (the only pay-per-hour parts).
  python3 session.py start      create is459-rt-stream (1 shard) + event source mapping to is459-rt-scorer
  python3 session.py stop       delete the mapping and the stream, then print the teardown checklist"""
import sys, time, json
import boto3

R, ACCT = 'us-east-1', '941377154820'
kin, lam, sqs = boto3.client('kinesis', region_name=R), boto3.client('lambda', region_name=R), boto3.client('sqs', region_name=R)
glue = boto3.client('glue', region_name=R)
STREAM, FN = 'is459-rt-stream', 'is459-rt-scorer'
DLQ = 'arn:aws:sqs:%s:%s:is459-rt-scorer-dlq' % (R, ACCT)


def mappings():
    return lam.list_event_source_mappings(FunctionName=FN).get('EventSourceMappings', [])


if sys.argv[1] == 'start':
    try:
        kin.create_stream(StreamName=STREAM, ShardCount=1, StreamModeDetails={'StreamMode': 'PROVISIONED'}, Tags={'project': 'is459-g1t5-rt'})
    except kin.exceptions.ResourceInUseException:
        pass
    kin.get_waiter('stream_exists').wait(StreamName=STREAM)
    arn = kin.describe_stream_summary(StreamName=STREAM)['StreamDescriptionSummary']['StreamARN']
    if not mappings():
        for attempt in range(6):
            try:
                m = lam.create_event_source_mapping(FunctionName=FN, EventSourceArn=arn, StartingPosition='TRIM_HORIZON', BatchSize=300,
                                                    MaximumBatchingWindowInSeconds=1, ParallelizationFactor=1, MaximumRetryAttempts=3,
                                                    BisectBatchOnFunctionError=True, MaximumRecordAgeInSeconds=3600,
                                                    DestinationConfig={'OnFailure': {'Destination': DLQ}}, Tags={'project': 'is459-g1t5-rt'})
                break
            except lam.exceptions.InvalidParameterValueException as e:
                print('waiting for permissions', e); time.sleep(10)
    for _ in range(30):
        st = mappings()[0]['State']
        if st == 'Enabled':
            break
        time.sleep(5)
    print('SESSION_STARTED', arn, mappings()[0]['UUID'], st)
else:
    for m in mappings():
        lam.delete_event_source_mapping(UUID=m['UUID'])
    for _ in range(30):
        if not mappings():
            break
        time.sleep(5)
    try:
        kin.delete_stream(StreamName=STREAM, EnforceConsumerDeletion=True)
        kin.get_waiter('stream_not_exists').wait(StreamName=STREAM)
    except kin.exceptions.ResourceNotFoundException:
        pass
    running = [r['Id'] for r in glue.get_job_runs(JobName='is459_rt_producer', MaxResults=5)['JobRuns'] if r['JobRunState'] in ('RUNNING', 'STARTING', 'STOPPING')]
    print('TEARDOWN_CHECK', json.dumps({'kinesis_streams': kin.list_streams()['StreamNames'], 'scorer_mappings': len(mappings()),
                                        'producer_runs_active': running}))
