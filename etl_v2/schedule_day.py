#!/usr/bin/env python3
"""Point the EventBridge schedule at one day of the raw Bronze CSV and fire it once (or put it back to disabled daily mode).
Usage:  python3 schedule_day.py once 2022-09-17 <run_tag> [minutes_from_now] [source_csv_key]
        python3 schedule_day.py disable
"""
import sys, json, datetime as dt
import boto3

SRC = 'bronze/ibm-aml/li-medium/transactions/'
sch = boto3.client('scheduler', region_name='us-east-1')
NAME = 'is459-etl-v2-silver-schedule'
cur = sch.get_schedule(Name=NAME)
target = dict(cur['Target'])
if sys.argv[1] == 'once':
    day, tag = sys.argv[2], sys.argv[3]
    mins = int(sys.argv[4]) if len(sys.argv) > 4 else 2
    src = sys.argv[5] if len(sys.argv) > 5 else SRC
    when = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=mins)).strftime('%Y-%m-%dT%H:%M:%S')
    target['Input'] = json.dumps({'day': day, 'day_compact': day.replace('-', ''), 'run_tag': tag, 'source_csv_key': src})
    expr, state = 'at(%s)' % when, 'ENABLED'
else:
    target['Input'] = json.dumps({'note': 'Daily mode is off. Fill day, day_compact, run_tag, source_csv_key before enabling.'})
    expr, state = 'rate(1 day)', 'DISABLED'
sch.update_schedule(Name=NAME, GroupName=cur.get('GroupName', 'default'), ScheduleExpression=expr,
                    ScheduleExpressionTimezone='UTC', FlexibleTimeWindow={'Mode': 'OFF'}, Target=target, State=state,
                    Description='IS459 ETL v2: starts the full pipeline state machine for one day of the raw Bronze CSV')
print(NAME, expr, state, target['Input'])
