#!/usr/bin/env python3
"""Measured cost of the real-time sessions since <start> (UTC ISO), from CloudWatch usage x us-east-1 list prices.
Prices checked 20 Oct 2026: DynamoDB on-demand $0.625/M WRU, $0.125/M RRU, streams $0.02/100k; Kinesis $0.015/shard-hour,
$0.014/M PUT units; Lambda $0.0000166667/GB-s; Glue Python shell $0.44/DPU-hour; EventBridge Pipes $0.40/M, events $1/M."""
import sys, json, datetime as dt
import boto3

R = 'us-east-1'
cw, glue = boto3.client('cloudwatch', region_name=R), boto3.client('glue', region_name=R)
start = dt.datetime.fromisoformat(sys.argv[1].replace('Z', '+00:00'))
end = dt.datetime.now(dt.timezone.utc)


def total(ns, metric, dims, stat='Sum'):
    s, t = 0.0, start
    while t < end:
        e = min(t + dt.timedelta(days=1), end)
        r = cw.get_metric_statistics(Namespace=ns, MetricName=metric, Dimensions=[{'Name': k, 'Value': v} for k, v in dims.items()],
                                     StartTime=t, EndTime=e, Period=3600, Statistics=[stat])
        s += sum(p[stat] for p in r['Datapoints'])
        t = e
    return s


out = {}
wru = sum(total('AWS/DynamoDB', 'ConsumedWriteCapacityUnits', {'TableName': t}) for t in ['is459_rt_state', 'is459_rt_seen', 'is459_rt_scores', 'is459_rt_alerts'])
rru = sum(total('AWS/DynamoDB', 'ConsumedReadCapacityUnits', {'TableName': t}) for t in ['is459_rt_state', 'is459_rt_seen', 'is459_rt_scores', 'is459_rt_alerts'])
out['dynamodb_wru'], out['dynamodb_rru'] = int(wru), int(rru)
out['dynamodb_usd'] = round(wru / 1e6 * 0.625 + rru / 1e6 * 0.125, 3)
dur_ms = total('AWS/Lambda', 'Duration', {'FunctionName': 'is459-rt-scorer'})
out['lambda_gb_s'] = round(dur_ms / 1000 * 1.5, 1)
out['lambda_usd'] = round(out['lambda_gb_s'] * 0.0000166667, 3)
recs = total('AWS/Kinesis', 'IncomingRecords', {'StreamName': 'is459-rt-stream'})
out['kinesis_records'] = int(recs)
out['kinesis_usd_put'] = round(recs / 1e6 * 0.014, 4)
dpu_s = 0.0
for r in glue.get_job_runs(JobName='is459_rt_producer', MaxResults=50)['JobRuns']:
    if r['StartedOn'] >= start:
        dpu_s += r.get('ExecutionTime', 0) * 0.0625
out['glue_producer_usd'] = round(dpu_s / 3600 * 0.44, 4)
print(json.dumps(out, indent=1))
print('Add: Kinesis shard-hours x $0.015, Athena bootstrap scans (~$0.03), ECR storage ~1 GB x $0.10/month.')
