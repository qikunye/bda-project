#!/usr/bin/env python3
"""Deploy the CSV-sourced daily lane (docs/decisions.md, CSV design). Run from ~/etlv2 in CloudShell.

  python3 deploy_csv.py audit     # upload silver_audit_dq_job.py, point is459_etl_v2_job-silver-audit-dq at it,
                                  # let the Glue role read the raw CSV and write parse-test results
  python3 deploy_csv.py dayjobs   # BQ1/BQ2 day jobs: read the CSV source proof (--source-proof-key) instead of the replay proof
  python3 deploy_csv.py lambdas   # main-snapshot Lambda (adds the refs read) + promote-gold Lambda without landing checks
  python3 deploy_csv.py sfn       # save the current state machine definition, deploy etl_v2_pipeline.asl.json
  python3 deploy_csv.py retire    # D7: save the replay/reconcile job definitions + scripts, then delete the two jobs
  python3 deploy_csv.py all       # audit, dayjobs, lambdas, sfn (retire stays a separate, explicit step)

Touches no Silver/Gold data and no v1 resource. Every replaced definition is saved under results/etl-v2/csv-cutover/.
"""
import io, json, sys, time, zipfile
import boto3

R = 'us-east-1'; B = 'is459-aml-g1t5'; ACCT = '941377154820'
TAGS = 'project=is459-g1t5-etl-v2'
FINAL = 'exported/etl-v2/final/'
SAVE = 'results/etl-v2/csv-cutover/' + time.strftime('%Y%m%d%H%M', time.gmtime()) + '/'
SRC_PREFIX = 'bronze/ibm-aml/li-medium/'
s3 = boto3.client('s3', region_name=R); glue = boto3.client('glue', region_name=R)
iam = boto3.client('iam'); lam = boto3.client('lambda', region_name=R); sfn = boto3.client('stepfunctions', region_name=R)
AUDIT_JOB = 'is459_etl_v2_job-silver-audit-dq'
RETIRE = ['is459_etl_v2_job-stream-replay', 'is459_etl_v2_job-reconcile-day14-exact']


def put(key, body, ctype='text/plain'):
    s3.put_object(Bucket=B, Key=key, Body=body if isinstance(body, bytes) else body.encode(), Tagging=TAGS, ContentType=ctype)


def save_job(name):
    j = glue.get_job(JobName=name)['Job']
    put(SAVE + 'jobs/%s.json' % name, json.dumps(j, default=str, indent=1), 'application/json')
    loc = j['Command']['ScriptLocation']; b, k = loc[5:].split('/', 1)
    put(SAVE + 'jobs/%s.py' % name, s3.get_object(Bucket=b, Key=k)['Body'].read())
    return j


def audit():
    j = save_job(AUDIT_JOB)
    put(FINAL + 'silver_audit_dq_job.py', open('silver_audit_dq_job.py', 'rb').read())
    args = {k: v for k, v in j.get('DefaultArguments', {}).items() if k not in ('--stream-run-id', '--reconciled-landing')}
    args.update({'--source-csv-key': SRC_PREFIX + 'transactions/', '--mode': 'audit'})
    spec = {'Description': 'Silver v2: parse one day from the raw Bronze CSV into an Iceberg audit branch, source proof, Glue Data Quality '
                           '(or --mode parse-test: parse to results/etl-v2/parse-test/ and compare with main, no branch)',
            'Role': j['Role'], 'Command': dict(j['Command'], ScriptLocation='s3://%s/%ssilver_audit_dq_job.py' % (B, FINAL)),
            'DefaultArguments': args, 'GlueVersion': j['GlueVersion'], 'WorkerType': j['WorkerType'],
            'NumberOfWorkers': max(j['NumberOfWorkers'], 10), 'Timeout': max(j.get('Timeout', 60), 60),
            'ExecutionProperty': {'MaxConcurrentRuns': 1}, 'MaxRetries': 0}
    glue.update_job(JobName=AUDIT_JOB, JobUpdate=spec)
    role = j['Role'].split('/')[-1]
    iam.put_role_policy(RoleName=role, PolicyName='IS459Etlv2CsvSource', PolicyDocument=json.dumps({'Version': '2012-10-17', 'Statement': [
        {'Effect': 'Allow', 'Action': ['s3:GetObject'], 'Resource': 'arn:aws:s3:::%s/%s*' % (B, SRC_PREFIX)},
        {'Effect': 'Allow', 'Action': ['s3:ListBucket'], 'Resource': 'arn:aws:s3:::%s' % B},  # Spark's writer lists parent prefixes
        {'Effect': 'Allow', 'Action': ['s3:GetObject', 's3:PutObject', 's3:DeleteObject'],
         'Resource': 'arn:aws:s3:::%s/results/etl-v2/parse-test/*' % B}]}))
    print('audit job ->', spec['Command']['ScriptLocation'], 'workers', spec['NumberOfWorkers'], '| role policy on', role)


# BQ1/BQ2 day jobs: the only stream-era coupling is the external "replay source" coverage proof and a 14-16 Sep
# whitelist. Swap both for the CSV audit result (passed as --source-proof-key). Each patch must match exactly once.
GATE = ("if proof.get('kind')!='silver_audit_result' or proof.get('mode')!='audit' or proof.get('status')!='PASS' or proof.get('day')!=day "
        "or proof.get('source_proof_match') is not True or str(proof.get('audit_snapshot_id'))!=%s['snapshot-id']:raise RuntimeError('CSV source proof gate failed')")
SRC = ("{'source_expected_rows':int(proof['source_proof']['rows']),'source_expected_hash':proof['source_id_hash'],"
       "'amount_paid_total':proof['amount_paid_total'],'amount_received_total':proof['amount_received_total']}")
BQ1_PATCHES = [
    (r"args\['day'\] not in \('2022-09-14','2022-09-15','2022-09-16'\)",
     r"not re.fullmatch(r'2022-09-\d{2}',args['day']) or not args.get('source-proof-key','').startswith('results/etl-v2/live-audit/')"),
    (r"coverage=json\.loads\(s\.get_object\(Bucket=B,Key='results/etl-v2/replay-source/coverage/[^']+'\)\['Body'\]\.read\(\)\)$",
     "proof=json.loads(s.get_object(Bucket=B,Key=args['source-proof-key'])['Body'].read())"),
    (r"if coverage\['status'\]!='ALL_SOURCE_ID_COVERAGE_VERIFIED'.*$", None),        # -> GATE % 'args'
    (r"source=next\(x for x in coverage\['days'\] if x\['day'\]==day\);expected=source\['source_expected_rows'\]$",
     None),                                                                           # -> source=SRC;expected=...
    (r"if source\.get\('status'\)!='PASS' or expected!=\{.*\}\[day\]:raise RuntimeError\('Source whole-day proof missing'\)$",
     "if expected<=0:raise RuntimeError('Source whole-day proof missing')"),
    (r"expected_baseline=24511248\+sum\(x\['source_expected_rows'\] for x in coverage\['days'\] if x\['day'\]<day\)$",
     "expected_baseline=full.where(F.col('timestamp')<F.lit(day).cast('timestamp_ltz')).count()"),
]
BQ2_PATCHES = [
    (r"a\['day'\] not in \('2022-09-14','2022-09-15','2022-09-16'\)",
     r"not re.fullmatch(r'2022-09-\d{2}',a['day']) or not a.get('source-proof-key','').startswith('results/etl-v2/live-audit/')"),
    (r"proof=json\.loads\(s\.get_object\(Bucket=B,Key='results/etl-v2/replay-source/coverage/[^']+'\)\['Body'\]\.read\(\)\)$",
     "proof=json.loads(s.get_object(Bucket=B,Key=a['source-proof-key'])['Body'].read())"),
    (r"if proof\['status'\]!='ALL_SOURCE_ID_COVERAGE_VERIFIED'.*$", None),           # -> GATE % 'a'
    (r"expected=next\(x for x in proof\['days'\] if x\['day'\]==day\)$", None),       # -> expected=SRC
]


def patch_script(src, patches, fills):
    import re
    out = src
    for (pat, rep), fill in zip(patches, fills):
        rep = rep if rep is not None else fill
        n = len(re.findall(pat, out, flags=re.M))
        if n != 1:
            raise SystemExit('patch anchor matched %d times: %s' % (n, pat))
        out = re.sub(pat, lambda m: rep, out, count=1, flags=re.M)
    if 'replay-source' in out or "'2022-09-14','2022-09-15','2022-09-16'" in out:
        raise SystemExit('stream-era reference left in patched script')
    return out


def dayjobs():
    for job, patches, fills, name in (
            ('is459_etl_v2_job-bq1-day', BQ1_PATCHES, [None, None, GATE % 'args', 'source=%s;expected=source[\'source_expected_rows\']' % SRC, None, None], 'bq1_day_job.py'),
            ('is459_etl_v2_job-bq2-day', BQ2_PATCHES, [None, None, GATE % 'a', 'expected=%s' % SRC], 'bq2_day_job.py')):
        j = save_job(job)
        loc = j['Command']['ScriptLocation']; b, k = loc[5:].split('/', 1)
        src = s3.get_object(Bucket=b, Key=k)['Body'].read().decode()
        new = patch_script(src, patches, fills)
        compile(new, name, 'exec')
        open(name, 'w').write(new)
        put(FINAL + name, new)
        spec = {key: j[key] for key in ('Role', 'GlueVersion', 'WorkerType', 'NumberOfWorkers', 'Timeout', 'MaxRetries', 'ExecutionProperty', 'DefaultArguments')}
        spec['Description'] = j.get('Description', '') + ' (source proof: CSV audit result)'
        spec['Command'] = dict(j['Command'], ScriptLocation='s3://%s/%s%s' % (B, FINAL, name))
        glue.update_job(JobName=job, JobUpdate=spec)
        print('patched', job, '->', spec['Command']['ScriptLocation'])


def zip_one(path, name='lambda_function.py'):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.write(path, name)
    return buf.getvalue()


def lambdas():
    for fn, path in (('is459-etl-v2-main-snapshot', 'main_snapshot_lambda.py'), ('is459-etl-v2-promote-gold', 'promote_gold_lambda.py')):
        cfg = lam.get_function(FunctionName=fn)
        put(SAVE + 'lambda/%s.json' % fn, json.dumps(cfg['Configuration'], default=str, indent=1), 'application/json')
        handler_file = cfg['Configuration']['Handler'].split('.')[0] + '.py'
        lam.update_function_code(FunctionName=fn, ZipFile=zip_one(path, handler_file))
        lam.get_waiter('function_updated_v2').wait(FunctionName=fn)
        put(FINAL + path, open(path, 'rb').read())
        print('updated', fn, '(handler file %s)' % handler_file)


def state_machine():
    arn = 'arn:aws:states:%s:%s:stateMachine:is459-etl-v2-silver-wap' % (R, ACCT)
    put(SAVE + 'sfn-definition-before.json', sfn.describe_state_machine(stateMachineArn=arn)['definition'], 'application/json')
    new_def = open('etl_v2_pipeline.asl.json').read(); json.loads(new_def)
    sfn.update_state_machine(stateMachineArn=arn, definition=new_def)
    put(FINAL + 'etl_v2_pipeline.asl.json', new_def, 'application/json')
    for f in ('run_pipeline_day.sh', 'schedule_day.py', 'validate_day.sh', 'parse_test.sh', 'deploy_csv.py', 'deploy_agent_hooks.py', 'silver_audit_dq_job.py'):
        put(FINAL + f, open(f, 'rb').read())
    print('state machine updated; scripts copied to', FINAL)


def retire():
    for name in RETIRE:
        try:
            save_job(name)
        except glue.exceptions.EntityNotFoundException:
            print('already gone', name); continue
        runs = glue.get_job_runs(JobName=name, MaxResults=5)['JobRuns']
        if any(r['JobRunState'] in ('RUNNING', 'STARTING', 'STOPPING', 'WAITING') for r in runs):
            raise SystemExit('%s has a run in progress; not deleting' % name)
        glue.delete_job(JobName=name)
        print('retired', name, '(definition and script saved under s3://%s/%sjobs/)' % (B, SAVE))


if __name__ == '__main__':
    a = sys.argv[1] if len(sys.argv) > 1 else ''
    steps = {'audit': [audit], 'dayjobs': [dayjobs], 'lambdas': [lambdas], 'sfn': [state_machine], 'retire': [retire],
             'all': [audit, dayjobs, lambdas, state_machine]}.get(a)
    if not steps:
        raise SystemExit(__doc__)
    for s in steps:
        s()
    print('saved previous definitions under s3://%s/%s' % (B, SAVE))
    print('DEPLOY_DONE')
