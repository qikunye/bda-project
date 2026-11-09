"""Shared by the Kaggle training notebook, the explainer Lambda, the evaluation and the tests, so all four agree on:
the input allowlist, the prompt, the output schema, the grounding check and the template fallback.
The laundering label and the typology label are NEVER part of an input (see ALLOWED / build_input)."""
import json, math, re

TYPOLOGIES = ['fan_out', 'fan_in', 'gather_scatter', 'scatter_gather', 'cycle', 'bipartite', 'stack', 'random', 'other_suspicious', 'none_evident']
PATTERN_NAMES = {'FAN-OUT': 'fan_out', 'FAN-IN': 'fan_in', 'GATHER-SCATTER': 'gather_scatter', 'SCATTER-GATHER': 'scatter_gather',
                 'CYCLE': 'cycle', 'BIPARTITE': 'bipartite', 'STACK': 'stack', 'RANDOM': 'random'}
CONFIDENCE = ['low', 'medium', 'high']
CONTEXT = ['ctx_snd_in_cnt_24h', 'ctx_snd_in_distinct_24h', 'ctx_rcv_out_cnt_24h', 'ctx_rcv_out_distinct_24h', 'ctx_reverse_pair_96h']
NUMERIC = ['hour_of_day', 'day_of_week', 'snd_out_cnt_1h', 'snd_out_cnt_24h', 'snd_out_amt_24h_ccy', 'rcv_in_cnt_1h', 'rcv_in_cnt_24h',
           'rcv_in_amt_24h_ccy', 'pair_prior_cnt', 'is_new_pair', 'snd_distinct_rcv_to_date', 'rcv_distinct_snd_to_date', 'snd_prior_txn_cnt',
           'snd_prior_mean_amt_ccy', 'amt_ratio_to_snd_mean', 'snd_secs_since_last_out']
FLAGS = ['is_same_account', 'is_same_entity', 'is_same_entity_different_account', 'is_cross_entity', 'is_cross_bank', 'is_cross_currency']
CATEG = ['payment_format', 'payment_currency', 'receiving_currency']
# Everything the model may see. Account ids, the laundering label and typology labels are deliberately absent.
ALLOWED = ['risk_score', 'amount_paid'] + CATEG + FLAGS + NUMERIC + CONTEXT + ['typology_hint', 'typology_hint_prob']
FORBIDDEN = {'is_laundering', 'label', 'typology', 'transaction_id', 'sender_account_key', 'receiver_account_key', 'sender', 'receiver'}
WINDOW_CONSTANTS = {0, 1, 4, 24, 96}

SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['typology', 'confidence', 'narrative', 'next_checks'],
          'properties': {'typology': {'type': 'string', 'enum': TYPOLOGIES},
                         'confidence': {'type': 'string', 'enum': CONFIDENCE},
                         'narrative': {'type': 'string', 'maxLength': 600},
                         'next_checks': {'type': 'array', 'maxItems': 3, 'items': {'type': 'string', 'maxLength': 140}}}}

SYSTEM = ('You explain anti-money-laundering alerts to an analyst. You get one flagged transaction as JSON: its risk score, '
          'its features, recent activity around the two accounts, and a typology hint from a classifier. '
          'Reply with JSON only: {"typology", "confidence", "narrative", "next_checks"}. '
          'typology is one of: ' + ', '.join(TYPOLOGIES) + '. Usually keep the hint; change it only if the numbers clearly contradict it. '
          'narrative: at most 3 sentences, plain English, and every number you write must appear in the input. '
          'next_checks: up to 3 short, concrete things the analyst should look at next in the case graph or account history. '
          'Never claim the transaction is laundering; say what the evidence suggests.')


def _num(v):
    if v is None or v == '':
        return None
    if isinstance(v, bool):
        return int(v)
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return x


def _flag(v):
    if isinstance(v, bool):
        return int(v)
    return 1 if str(v).strip().lower() in ('true', '1', 't') else 0


def _round(x):
    if x is None:
        return None
    if abs(x - round(x)) < 1e-9:
        return int(round(x))
    return round(x, 2)


def build_input(row):
    """row: any dict holding the score, features, context and (optionally) the classifier hint.
    Returns the allowlisted, rounded input the model sees. Raises if a forbidden key would leak."""
    inp = {'risk_score': round(float(row['risk_score']), 3), 'amount_paid': _round(_num(row['amount_paid']))}
    for c in CATEG:
        inp[c] = str(row.get(c) or '')
    for c in FLAGS:
        inp[c] = _flag(row.get(c))
    for c in NUMERIC + CONTEXT:
        inp[c] = _round(_num(row.get(c)))
    if row.get('typology_hint'):
        inp['typology_hint'] = row['typology_hint']
        inp['typology_hint_prob'] = round(float(row.get('typology_hint_prob') or 0), 2)
    leak = (set(inp) - set(ALLOWED)) | (set(inp) & FORBIDDEN)
    if leak:
        raise ValueError('input leak: %s' % sorted(leak))
    return inp


def messages(inp):
    return [{'role': 'system', 'content': SYSTEM}, {'role': 'user', 'content': 'ALERT ' + json.dumps(inp, separators=(',', ':'))}]


NUM_RE = re.compile(r'(?<![A-Za-z_])-?\d[\d,]*(?:\.\d+)?')


def _variants(x):
    out = set()
    if x is None:
        return out
    for d in (0, 1, 2, 3):
        out.add(round(x, d))
    out.add(round(x * 100, 0))  # a ratio quoted as a percentage
    out.add(round(x * 100, 1))
    return out


def grounding_problems(text, inp):
    """Numbers in text that do not appear in the input (at the precision they are written), excluding window sizes."""
    allowed = set(WINDOW_CONSTANTS)
    for k, v in inp.items():
        x = _num(v)
        allowed |= _variants(x)
        if k in ('snd_secs_since_last_out',) and x is not None:  # seconds quoted as minutes or hours
            allowed |= _variants(x / 60) | _variants(x / 3600)
    bad = []
    for m in NUM_RE.findall(text):
        s = m.replace(',', '')
        try:
            n = float(s)
        except ValueError:
            continue
        d = len(s.split('.')[1]) if '.' in s else 0
        if round(n, d) not in {round(a, d) for a in allowed}:
            bad.append(m)
    return bad


def validate(obj, inp):
    """Returns a list of problems; empty means the brief can be stored as is."""
    p = []
    if not isinstance(obj, dict):
        return ['not an object']
    extra = set(obj) - set(SCHEMA['properties'])
    if extra:
        p.append('extra keys %s' % sorted(extra))
    if obj.get('typology') not in TYPOLOGIES:
        p.append('bad typology')
    if obj.get('confidence') not in CONFIDENCE:
        p.append('bad confidence')
    n = obj.get('narrative')
    if not isinstance(n, str) or not n.strip() or len(n) > 600:
        p.append('bad narrative')
    nc = obj.get('next_checks')
    if not isinstance(nc, list) or len(nc) > 3 or not all(isinstance(x, str) and 0 < len(x) <= 140 for x in nc):
        p.append('bad next_checks')
    if isinstance(n, str):
        g = grounding_problems(n, inp) + [x for c in (nc if isinstance(nc, list) else []) if isinstance(c, str) for x in grounding_problems(c, inp)]
        if g:
            p.append('invented numbers %s' % g[:5])
    return p


def template_brief(inp, typology=None):
    """Deterministic fallback that only quotes input numbers."""
    t = typology or inp.get('typology_hint') or 'none_evident'
    parts = ['Risk score %s for a %s payment of %s %s.' % (inp['risk_score'], inp.get('payment_format') or 'unknown', inp['amount_paid'], inp.get('payment_currency'))]
    if inp.get('snd_out_cnt_24h'):
        parts.append('The sender made %s payments in the previous 24 hours to %s different receivers so far.' % (inp['snd_out_cnt_24h'], inp.get('snd_distinct_rcv_to_date')))
    if inp.get('rcv_in_cnt_24h'):
        parts.append('The receiver got %s payments in the previous 24 hours.' % inp['rcv_in_cnt_24h'])
    if inp.get('ctx_snd_in_cnt_24h'):
        parts.append('The sender itself received %s payments in the previous 24 hours.' % inp['ctx_snd_in_cnt_24h'])
    checks = ['Review the sender\'s outgoing payments in the case graph', 'Review the receiver\'s onward payments in the case graph']
    return {'typology': t, 'confidence': 'low', 'narrative': ' '.join(parts)[:600], 'next_checks': checks}


def macro_f1(y_true, y_pred, labels=None):
    labels = labels or sorted(set(y_true))
    f1s, per = [], {}
    for c in labels:
        tp = sum(1 for a, b in zip(y_true, y_pred) if a == c and b == c)
        fp = sum(1 for a, b in zip(y_true, y_pred) if a != c and b == c)
        fn = sum(1 for a, b in zip(y_true, y_pred) if a == c and b != c)
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        f = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
        per[c] = round(f, 3)
        f1s.append(f)
    return (sum(f1s) / len(f1s) if f1s else 0.0), per


# ---------------------------------------------------------------- teacher-reference score (release gate vs the base model)
_TOK = re.compile(r'[a-z]+|\d[\d,]*(?:\.\d+)?')


def _brief_text(b):
    return ((b.get('narrative') or '') + ' ' + ' '.join(b.get('next_checks') or [])).lower()


def _lcs(a, b):
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    for x in a:
        cur = [0]
        for j, y in enumerate(b):
            cur.append(prev[j] + 1 if x == y else max(prev[j + 1], cur[j]))
        prev = cur
    return prev[-1]


def rouge_l(cand, ref):
    a, b = _TOK.findall(cand), _TOK.findall(ref)
    l = _lcs(a, b)
    if not l:
        return 0.0
    p, r = l / len(a), l / len(b)
    return 2 * p * r / (p + r)


def _nums(text):
    return {n.replace(',', '') for n in NUM_RE.findall(text)}


def ref_score(brief, ref_brief):
    """Similarity of a brief to the teacher's reference brief for the same alert, 0..1.
    Mean of ROUGE-L F1 (narrative + next checks) and recall of the numbers the teacher cited; ROUGE-L alone if the teacher cited none."""
    c, r = _brief_text(brief), _brief_text(ref_brief)
    rl = rouge_l(c, r)
    rn = _nums(ref_brief.get('narrative') or '')
    if not rn:
        return {'rouge_l': round(rl, 4), 'num_recall': None, 'score': round(rl, 4)}
    nr = len(rn & _nums(brief.get('narrative') or '')) / len(rn)
    return {'rouge_l': round(rl, 4), 'num_recall': round(nr, 4), 'score': round((rl + nr) / 2, 4)}
