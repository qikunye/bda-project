"""Glue Python shell job is459_rt_control: runs one control step of a real-time session for the Step Functions orchestrator.
Args (all always passed): --action bootstrap|load_state|state_check|guard|validate --tag <slice tag> --run_id <id>
                          --slice_start 2022-09-16T02:20:00 --slice_end 2022-09-16T03:20:00
It downloads the session scripts from s3://is459-aml-g1t5/rt/code/ and runs the same commands as the CloudShell runbook,
so the automated session and the manual one execute identical code. Any non-zero exit fails the job (and the session)."""
import sys, os, subprocess
import boto3
from awsglue.utils import getResolvedOptions

A = getResolvedOptions(sys.argv, ['action', 'tag', 'run_id', 'slice_start', 'slice_end'])
B = 'is459-aml-g1t5'
s3 = boto3.client('s3', region_name='us-east-1')
W = '/tmp/rtc'
os.makedirs(W, exist_ok=True)
os.chdir(W)
for m in ['rt_core.py', 'bootstrap_logic.py', 'bootstrap.py', 'load_state.py', 'state_check.py', 'validate_rt.py']:
    s3.download_file(B, 'rt/code/' + m, m)


def pull(tag):
    d = 'rtdata/' + tag
    os.makedirs(d, exist_ok=True)
    n = 0
    for p in s3.get_paginator('list_objects_v2').paginate(Bucket=B, Prefix='rt/bootstrap/%s/' % tag):
        for o in p.get('Contents', []):
            s3.download_file(B, o['Key'], d + '/' + o['Key'].rsplit('/', 1)[1])
            n += 1
    if n == 0:
        raise SystemExit('no bootstrap files for tag ' + tag)
    return d


def run(*args):
    print('RUN', ' '.join(args), flush=True)
    r = subprocess.run([sys.executable] + list(args), capture_output=True, text=True)
    print(r.stdout[-20000:], flush=True)
    if r.stderr:
        print(r.stderr[-20000:], file=sys.stderr, flush=True)
    if r.returncode:
        raise SystemExit('%s failed with exit code %d' % (args[0], r.returncode))


act = A['action']
if act == 'bootstrap':
    run('bootstrap.py', A['slice_start'], A['slice_end'], A['tag'])
elif act == 'load_state':
    run('load_state.py', pull(A['tag']))
elif act == 'state_check':
    run('state_check.py', pull(A['tag']), '--load-missing')
elif act == 'guard':
    run('validate_rt.py', 'guard-before', A['run_id'])
elif act == 'validate':
    run('validate_rt.py', 'check', A['run_id'], pull(A['tag']))
else:
    raise SystemExit('unknown action ' + act)
print('CONTROL_DONE', act)
