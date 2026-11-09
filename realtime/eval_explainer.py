#!/usr/bin/env python3
"""Evaluate the explainer candidate on the fixed 300-alert held-out sample (14 to 16 Sep) and promote it only if every gate holds.
  python3 eval_explainer.py <report_id> [--promote] [--n 300] [--as-baseline] [--qualifier <alias|version>]
Calls alias 'candidate' directly with ready-made inputs (mode=eval), four at a time. Report: rt/builds/<report_id>/eval.json
Gates: 100% schema-valid briefs; 0 invented numbers in stored briefs; needs_review <= 5%; typology macro-F1 >= the classifier hint
alone minus 0.02 and >= the majority-class baseline; warm p95 latency <= 30 s; beats the untuned base model on the teacher
reference: mean ref score >= base + 0.02 and wins > 50% of the decided head-to-head alerts (ROUGE-L + number recall vs
teacher_eval_ref.jsonl, see explainer_common.ref_score).
--as-baseline: evaluate the base model on 'candidate' and record it as the comparison point (rt/explainer-data/baseline_ref.json);
no promotion and no base-model gate in that mode."""
import sys, json, time, io, statistics
from concurrent.futures import ThreadPoolExecutor
import boto3
from botocore.config import Config
sys.path.insert(0, '.')
import explainer_common as E

R, B, FN = 'us-east-1', 'is459-aml-g1t5', 'is459-rt-explainer'
lam = boto3.client('lambda', region_name=R, config=Config(read_timeout=200, retries={'max_attempts': 2}))
s3 = boto3.client('s3', region_name=R)
rid = sys.argv[1]
N = int(sys.argv[sys.argv.index('--n') + 1]) if '--n' in sys.argv else 300


def jl(key):
    return [json.loads(l) for l in s3.get_object(Bucket=B, Key=key)['Body'].read().decode().splitlines() if l.strip()]


rows = jl('rt/explainer-data/eval_sample.jsonl')[:N]
REFS = {r['transaction_id']: r['target'] for r in jl('rt/explainer-data/teacher_eval_ref.jsonl')}
AS_BASE = '--as-baseline' in sys.argv
QUAL = sys.argv[sys.argv.index('--qualifier') + 1] if '--qualifier' in sys.argv else 'candidate'
prep = json.loads(s3.get_object(Bucket=B, Key='rt/explainer-data/prep_metrics.json')['Body'].read())


def call(r):
    t = time.time()
    resp = lam.invoke(FunctionName=FN, Qualifier=QUAL, Payload=json.dumps({'mode': 'eval', 'input': r['input']}).encode())
    out = json.loads(resp['Payload'].read())
    if 'FunctionError' in resp:
        return {'error': out, 'wall_ms': int((time.time() - t) * 1000)}
    out['wall_ms'] = int((time.time() - t) * 1000)
    return out


t0 = time.time()
first = call(rows[0])  # cold start, timed separately
with ThreadPoolExecutor(4) as ex:
    rest = list(ex.map(call, rows[1:]))
res = [first] + rest
errors = [r for r in res if 'error' in r]
ok = [(row, r) for row, r in zip(rows, res) if 'error' not in r]
schema_bad = [r for row, r in ok if any(not p.startswith('invented') for p in E.validate(r['brief'], row['input']))]
invented = [(row['transaction_id'], E.validate(r['brief'], row['input'])) for row, r in ok if any(p.startswith('invented') for p in E.validate(r['brief'], row['input']))]
needs_review = sum(1 for _, r in ok if r['status'] == 'needs_review')
y = [row['label'] for row, _ in ok]
yp = [r['brief']['typology'] for _, r in ok]
yh = [row['input'].get('typology_hint') for row, _ in ok]
f1, per = E.macro_f1(y, yp, E.TYPOLOGIES)
f1h, perh = E.macro_f1(y, yh, E.TYPOLOGIES)
f1m, _ = E.macro_f1(y, [prep['majority_class']] * len(y), E.TYPOLOGIES)
scores = {row['transaction_id']: E.ref_score(r['brief'], REFS[row['transaction_id']]) for row, r in ok if row['transaction_id'] in REFS}
ref_mean = round(statistics.mean(v['score'] for v in scores.values()), 4) if scores else 0.0
rl_mean = round(statistics.mean(v['rouge_l'] for v in scores.values()), 4) if scores else 0.0
base = None
if not AS_BASE:
    bp = json.loads(s3.get_object(Bucket=B, Key='rt/explainer-data/baseline_ref.json')['Body'].read())
    bs = {x['transaction_id']: x['ref'] for x in jl('rt/builds/%s/briefs.jsonl' % bp['report_id']) if x.get('ref')}
    shared = [t for t in scores if t in bs]
    wins = sum(1 for t in shared if scores[t]['score'] > bs[t]['score'])
    losses = sum(1 for t in shared if scores[t]['score'] < bs[t]['score'])
    base_mean = round(statistics.mean(bs[t]['score'] for t in shared), 4) if shared else 0.0
    cand_shared = round(statistics.mean(scores[t]['score'] for t in shared), 4) if shared else 0.0
    base = {'baseline_report': bp['report_id'], 'shared_alerts': len(shared), 'cand_mean': cand_shared, 'base_mean': base_mean,
            'wins': wins, 'losses': losses, 'ties': len(shared) - wins - losses,
            'win_rate_decided': round(wins / max(1, wins + losses), 3)}
warm = sorted(r['ms'] for _, r in ok[1:]) or [0]
p95 = warm[min(len(warm) - 1, int(0.95 * len(warm)))]
gates = {'all_answered': len(errors) == 0,
         'schema_valid_100pct': not schema_bad,
         'zero_invented_numbers': not invented,
         'needs_review_le_5pct': needs_review <= 0.05 * max(1, len(ok)),
         'macro_f1_ge_hint_minus_0.02': f1 >= f1h - 0.02,
         'macro_f1_ge_majority': f1 >= f1m,
         'warm_p95_le_30s': p95 <= 30000}
if base is not None:
    gates['teacher_ref_beats_base_by_0.02'] = base['shared_alerts'] >= 50 and base['cand_mean'] >= base['base_mean'] + 0.02
    gates['teacher_ref_wins_gt_50pct'] = base['win_rate_decided'] > 0.5
report = {'report_id': rid, 'n': len(rows), 'errors': len(errors), 'error_examples': errors[:3], 'needs_review': needs_review,
          'invented_examples': invented[:5], 'typology_accuracy': round(sum(a == b for a, b in zip(y, yp)) / max(1, len(y)), 3),
          'typology_macro_f1': round(f1, 3), 'per_class_f1': per, 'hint_only_macro_f1': round(f1h, 3), 'hint_per_class_f1': perh,
          'majority_macro_f1': round(f1m, 3), 'cold_ms': first.get('ms'), 'warm_ms': {'p50': warm[len(warm) // 2], 'p95': p95, 'max': warm[-1]},
          'teacher_ref': {'alerts_scored': len(scores), 'mean_score': ref_mean, 'mean_rouge_l': rl_mean, 'vs_base': base},
          'wall_minutes': round((time.time() - t0) / 60, 1), 'gates': gates, 'passed': all(gates.values()),
          'candidate_version': QUAL if QUAL.isdigit() else lam.get_alias(FunctionName=FN, Name=QUAL)['FunctionVersion'],
          'samples': [{'transaction_id': row['transaction_id'], 'label': row['label'], 'brief': r['brief'], 'status': r['status']} for row, r in ok[:8]]}
s3.put_object(Bucket=B, Key='rt/builds/%s/briefs.jsonl' % rid, Tagging='project=is459-g1t5-rt',
              Body='\n'.join(json.dumps({'transaction_id': row['transaction_id'], 'label': row['label'], 'status': r['status'], 'brief': r['brief'],
                                         'ref': scores.get(row['transaction_id'])}) for row, r in ok).encode())
if AS_BASE:
    s3.put_object(Bucket=B, Key='rt/explainer-data/baseline_ref.json', Tagging='project=is459-g1t5-rt',
                  Body=json.dumps({'report_id': rid, 'candidate_version': report['candidate_version'], 'n': len(rows), 'mean_score': ref_mean}).encode())
    print('BASELINE_RECORDED', rid)
s3.put_object(Bucket=B, Key='rt/builds/%s/eval.json' % rid, Body=json.dumps(report, indent=1).encode(), Tagging='project=is459-g1t5-rt')
print(json.dumps({k: v for k, v in report.items() if k != 'samples'}, indent=1))
if '--promote' in sys.argv and not AS_BASE and QUAL == 'candidate':
    if report['passed']:
        import deploy_explainer
        deploy_explainer.promote()
        print('PROMOTED', report['candidate_version'])
    else:
        print('NOT_PROMOTED: live alias unchanged')
        sys.exit(2)
