#!/usr/bin/env python3
"""One-command release of the explainer (and optionally the scorer), each step gating the next. Runs in CloudShell or CodeBuild.
  python3 release.py <model_version> [--scorer] [--no-promote]
1. unit tests  2. build + push images  3. (with --scorer) offline gate in the scorer image, then update the scorer if no session runs
4. new explainer version on alias 'candidate'  5. eval_explainer.py on 300 held-out alerts  6. promote to 'live' only if every gate holds
Report: s3://is459-aml-g1t5/rt/builds/<build id>/report.json"""
import sys, os, json, time, subprocess
import boto3

R, ACCT, B = 'us-east-1', '941377154820', 'is459-aml-g1t5'
REG = '%s.dkr.ecr.%s.amazonaws.com' % (ACCT, R)
ver = sys.argv[1]
rid = os.environ.get('CODEBUILD_BUILD_NUMBER') and 'cb-' + os.environ['CODEBUILD_BUILD_NUMBER'] or time.strftime('rel-%Y%m%d-%H%M%S')
s3 = boto3.client('s3', region_name=R)
report = {'build_id': rid, 'model_version': ver, 'steps': []}


def step(name, cmd, env=None):
    t = time.time()
    print('\n=== %s: %s' % (name, cmd), flush=True)
    r = subprocess.run(cmd, shell=True, env=dict(os.environ, **(env or {})), capture_output=True, text=True)
    print(r.stdout[-6000:], r.stderr[-3000:], flush=True)
    report['steps'].append({'step': name, 'rc': r.returncode, 'seconds': round(time.time() - t), 'tail': r.stdout[-800:]})
    if r.returncode:
        finish(False, 'failed at ' + name)
    return r.stdout


def finish(ok, note):
    report['passed'], report['note'] = ok, note
    s3.put_object(Bucket=B, Key='rt/builds/%s/report.json' % rid, Body=json.dumps(report, indent=1).encode(), Tagging='project=is459-g1t5-rt')
    print('RELEASE', 'PASSED' if ok else 'FAILED', rid, note)
    sys.exit(0 if ok else 1)


step('unit tests', 'python3 test_local.py && RT_SKIP_MODEL=1 python3 test_handler.py && python3 test_explainer.py')
step('ecr login', 'aws ecr get-login-password --region %s | docker login --username AWS --password-stdin %s' % (R, REG))
if '--scorer' in sys.argv:
    step('build scorer', 'docker build -q -t is459-rt-scorer:rel .')
    step('offline gate', 'docker run --rm --entrypoint python3 -v $HOME/rt/rtdata:/data -w /var/task is459-rt-scorer:rel '
                         'offline_gate.py /data/s0116h1 10 /data/model.ubj /data/feature_schema.json')
    out = step('push scorer', 'docker tag is459-rt-scorer:rel %s/is459-rt-scorer:rel && docker push %s/is459-rt-scorer:rel' % (REG, REG))
    dig = [l.split('digest: ')[1].split(' ')[0] for l in out.splitlines() if 'digest: sha256' in l][-1]
    running = boto3.client('stepfunctions', region_name=R).list_executions(
        stateMachineArn='arn:aws:states:%s:%s:stateMachine:is459-rt-session' % (R, ACCT), statusFilter='RUNNING')['executions']
    if running:
        finish(False, 'a session is running; scorer not updated')
    step('deploy scorer', 'python3 deploy_rt.py lambda %s/is459-rt-scorer@%s 0.36046239733695984' % (REG, dig))
step('build explainer', 'docker build -q -f Dockerfile.explainer -t is459-rt-explainer:%s .' % ver)
out = step('push explainer', 'docker tag is459-rt-explainer:{v} {r}/is459-rt-explainer:{v} && docker push {r}/is459-rt-explainer:{v}'.format(v=ver, r=REG))
dig = [l.split('digest: ')[1].split(' ')[0] for l in out.splitlines() if 'digest: sha256' in l][-1]
report['explainer_image'] = '%s/is459-rt-explainer@%s' % (REG, dig)
step('candidate', 'python3 deploy_explainer.py candidate %s %s' % (report['explainer_image'], ver))
ev = step('evaluate', 'python3 eval_explainer.py %s %s' % (rid, '' if '--no-promote' in sys.argv else '--promote'))
finish(True, 'promoted' if 'PROMOTED' in ev else 'evaluated, not promoted')
