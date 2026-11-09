import boto3,json,re
s=boto3.client('s3',region_name='us-east-1')
# Quality gate for the CSV-sourced daily lane. Same checks as the stream-era gate (it never read the landing proof),
# plus: the result must come from the CSV audit job in audit mode and its source proof must have matched.
def handler(event,context):
    key=event['result_key']
    if not isinstance(key,str) or not re.fullmatch(r'results/etl-v2/live-audit/[a-zA-Z0-9_-]+[.]json',key):raise ValueError('result path outside v2 audit')
    obj=s.get_object(Bucket='is459-aml-g1t5',Key=key);x=json.loads(obj['Body'].read())
    if x.get('run_id')!=event['run_id']:raise ValueError('run identity mismatch')
    if x.get('kind')!='silver_audit_result' or x.get('mode')!='audit':raise ValueError('not a CSV audit result')
    if x.get('source_proof_match') is not True or x.get('rows')!=(x.get('source_proof') or {}).get('rows'):raise ValueError('CSV source proof did not match')
    if x.get('main_snapshot_before')!=event['expected_main_snapshot'] or x.get('main_snapshot_after')!=event['expected_main_snapshot']:raise ValueError('main snapshot invariant failed')
    rules=x.get('glue_dq_rule_results')
    if not isinstance(rules,list) or len(rules)!=11 or not all(isinstance(r,dict) and r.get('Outcome') in ['Passed','Failed'] for r in rules):raise ValueError('missing/unreadable real GlueDQ results')
    computed='PASS' if all(r['Outcome']=='Passed' for r in rules) else 'FAIL'
    if x.get('status')!=computed:raise ValueError('quality status inconsistent')
    return {'status':computed,'run_id':event['run_id'],'result_key':key,'main_snapshot_before':x['main_snapshot_before'],'main_snapshot_after':x['main_snapshot_after'],'audit_snapshot_id':x['audit_snapshot_id'],'rows':x['rows'],'failed_rules':[r['Rule'] for r in rules if r['Outcome']!='Passed']}
