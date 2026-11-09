"""Kaggle notebook script for the IS459 alert explainer. Run on a GPU T4 x2 session with internet on.
  python kaggle_explainer.py prep '<presigned GET url of candidates.csv.gz>'   labels from the patterns file, splits, typology classifier
  python kaggle_explainer.py teacher                                          Qwen2.5-7B-Instruct writes target briefs (both GPUs)
  python kaggle_explainer.py train                                            LoRA fine-tune of Qwen2.5-1.5B-Instruct, merge, GGUF q8_0, quick check
  python kaggle_explainer.py upload '<json {file: presigned PUT url}>'        send the outputs back to S3
No AWS keys are used anywhere: data moves only through short-lived presigned links. Everything lands in /kaggle/working/out.
Labels (is_laundering, patterns typology) are used only to build targets and grades; build_input() refuses them as inputs."""
import os, sys, json, gzip, io, re, random, subprocess, hashlib, time, math
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import explainer_common as E

OUT = '/kaggle/working/out'
os.makedirs(OUT, exist_ok=True)
SEED = 7
THRESH = 0.36046239733695984
TEACHER = 'Qwen/Qwen2.5-7B-Instruct'
STUDENT = 'Qwen/Qwen2.5-1.5B-Instruct'
DATASET = 'ealtman2019/ibm-transactions-for-anti-money-laundering-aml'
CLF_FEATS = ['risk_score', 'log_amount'] + E.FLAGS + E.NUMERIC + E.CONTEXT + ['pf_idx']
TEACHER_GUIDE = {
    'fan_out': 'one account spreads money to many different receivers',
    'fan_in': 'many different senders pay into one account',
    'gather_scatter': 'an account collects from many senders and then pays out to many receivers',
    'scatter_gather': 'money is split across several intermediaries and then regathered into one account',
    'cycle': 'money moves through a chain of accounts and comes back to where it started',
    'bipartite': 'a group of senders pays a group of receivers, many to many',
    'stack': 'money is passed along layered chains of accounts, each forwarding what it received',
    'random': 'money hops between accounts with no fixed shape',
    'other_suspicious': 'the activity is suspicious but does not match one of the standard shapes',
    'none_evident': 'the score is high but no laundering shape is visible in these numbers; it may well be benign'}


def sh(cmd):
    print('$', cmd, flush=True)
    subprocess.run(cmd, shell=True, check=True)


def wjsonl(path, rows):
    with open(path, 'w') as f:
        for r in rows:
            f.write(json.dumps(r, separators=(',', ':')) + '\n')


def rjsonl(path):
    return [json.loads(l) for l in open(path)]


# ------------------------------------------------------------------------------------------------ prep
def acct(key):
    return str(key).split('|')[-1]


def bank(b):
    s = str(b).strip()
    return str(int(s)) if s.isdigit() else s.lstrip('0')


def load_patterns():
    path = None
    for root, _, files in os.walk('/kaggle/input'):
        if 'LI-Medium_Patterns.txt' in files:
            path = os.path.join(root, 'LI-Medium_Patterns.txt')
    if path is None:
        import kagglehub
        path = kagglehub.dataset_download(DATASET, path='LI-Medium_Patterns.txt')
    lab, cur, att = {}, None, 0
    for line in open(path):
        line = line.strip()
        if line.startswith('BEGIN'):
            cur = E.PATTERN_NAMES[re.match(r'BEGIN LAUNDERING ATTEMPT - ([A-Z\-]+)', line).group(1)]
            att += 1
            continue
        if not line or line.startswith('END'):
            continue
        p = line.split(',')
        key = (p[0].replace('/', '-'), bank(p[1]), p[2], bank(p[3]), p[4], round(float(p[7]), 2))
        lab[key] = (cur, att)
    print('patterns: %d transactions in %d attempts' % (len(lab), att))
    return lab


def prep(url):
    import pandas as pd, numpy as np, requests
    raw = requests.get(url, timeout=600).content if url.startswith('http') else open(url, 'rb').read()
    df = pd.read_csv(io.BytesIO(gzip.decompress(raw)), dtype={'from_bank_norm': str, 'to_bank_norm': str, 'sender_account_key': str,
                                                                 'receiver_account_key': str, 'amount_paid': str})
    print('candidates', len(df), 'laundering', int(df.is_laundering.sum()), 'alerts', int((df.risk_score >= THRESH).sum()))
    lab = load_patterns()
    keys = list(zip(df.ts_minute, df.from_bank_norm.map(bank), df.sender_account_key.map(acct), df.to_bank_norm.map(bank),
                    df.receiver_account_key.map(acct), df.amount_paid.astype(float).round(2)))
    df['typology'] = [lab.get(k, (None, None))[0] for k in keys]
    df['attempt'] = [lab.get(k, (None, None))[1] for k in keys]
    matched = df.typology.notna().sum()
    print('pattern rows matched in candidates: %d of %d (%.1f%%)' % (matched, len(lab), 100.0 * matched / len(lab)))
    if matched < 0.8 * sum(1 for k in lab if k[0] < '2022-09-17'):
        print('WARNING: low match rate; check bank / account normalisation')
    df.loc[df.typology.isna() & (df.is_laundering == 1), 'typology'] = 'other_suspicious'
    df.loc[df.typology.isna() & (df.is_laundering == 0), 'typology'] = 'none_evident'
    df['split'] = np.where(df.ts_minute < '2022-09-14', 'train', 'heldout')
    df['is_alert'] = df.risk_score >= THRESH
    rng = np.random.RandomState(SEED)
    parts = []
    for split, n_other, n_none in [('train', 500, 600), ('heldout', 150, 250)]:
        d = df[df.split == split]
        pat = d[d.typology.isin(list(E.PATTERN_NAMES.values()))]
        oth = d[d.typology == 'other_suspicious']
        oth = pd.concat([oth[oth.is_alert], oth[~oth.is_alert].sample(frac=1, random_state=SEED)]).head(n_other)
        non = d[d.typology == 'none_evident']
        non = non.sample(n=min(n_none, len(non)), random_state=SEED)
        parts.append(pd.concat([pat, oth, non]))
    data = pd.concat(parts).sample(frac=1, random_state=SEED).reset_index(drop=True)
    pfs = sorted(df.payment_format.dropna().unique().tolist())
    data['log_amount'] = np.log1p(data.amount_paid.astype(float))
    data['pf_idx'] = data.payment_format.map({p: i for i, p in enumerate(pfs)}).fillna(-1)
    for c in E.FLAGS:
        data[c] = data[c].map(lambda v: E._flag(v))
    print(data.groupby(['split', 'typology']).size().unstack(0).fillna(0).astype(int))
    # typology classifier (the hint): out-of-fold hints on train, full-train model for heldout and production
    import xgboost as xgb
    classes = E.TYPOLOGIES
    tr, ho = data[data.split == 'train'].copy(), data[data.split == 'heldout'].copy()
    Xtr, ytr = tr[CLF_FEATS].astype('float32').values, tr.typology.map(classes.index).values
    params = dict(objective='multi:softprob', num_class=len(classes), max_depth=6, eta=0.1, subsample=0.9, colsample_bytree=0.9,
                  tree_method='hist', seed=SEED, eval_metric='mlogloss')
    oof = np.zeros((len(tr), len(classes)))
    folds = np.arange(len(tr)) % 5
    rng.shuffle(folds)
    for k in range(5):
        m = xgb.train(params, xgb.DMatrix(Xtr[folds != k], label=ytr[folds != k]), num_boost_round=300)
        oof[folds == k] = m.predict(xgb.DMatrix(Xtr[folds == k]))
    clf = xgb.train(params, xgb.DMatrix(Xtr, label=ytr), num_boost_round=300)
    clf.save_model(OUT + '/typology_clf.json')
    json.dump({'features': CLF_FEATS, 'classes': classes, 'payment_formats': pfs, 'xgboost': xgb.__version__},
              open(OUT + '/typology_clf_meta.json', 'w'))
    hop = clf.predict(xgb.DMatrix(ho[CLF_FEATS].astype('float32').values))
    tr['typology_hint'] = [classes[i] for i in oof.argmax(1)]
    tr['typology_hint_prob'] = oof.max(1)
    ho['typology_hint'] = [classes[i] for i in hop.argmax(1)]
    ho['typology_hint_prob'] = hop.max(1)
    f1_tr, _ = E.macro_f1(list(tr.typology), list(tr.typology_hint), classes)
    f1_ho, per = E.macro_f1(list(ho.typology), list(ho.typology_hint), classes)
    acc_ho = float((ho.typology == ho.typology_hint).mean())
    majority = tr.typology.value_counts().index[0]
    f1_maj, _ = E.macro_f1(list(ho.typology), [majority] * len(ho), classes)
    metrics = {'classifier_oof_macro_f1_train': round(f1_tr, 3), 'classifier_macro_f1_heldout': round(f1_ho, 3), 'classifier_acc_heldout': round(acc_ho, 3),
               'classifier_per_class_heldout': per, 'majority_class': majority, 'majority_macro_f1_heldout': round(f1_maj, 3),
               'rows': {s: int((data.split == s).sum()) for s in ('train', 'heldout')}, 'pattern_rows_matched': int(matched)}
    print(json.dumps(metrics, indent=1))
    json.dump(metrics, open(OUT + '/prep_metrics.json', 'w'), indent=1)

    def rows(d):
        out = []
        for r in d.to_dict('records'):
            inp = E.build_input(r)
            out.append({'transaction_id': r['transaction_id'], 'ts_minute': r['ts_minute'], 'label': r['typology'], 'is_alert': bool(r['is_alert']),
                        'attempt': None if pd.isna(r['attempt']) else int(r['attempt']), 'clf_x': [float(r[c]) for c in CLF_FEATS], 'input': inp})
        return out
    tr_rows, ho_rows = rows(tr), rows(ho)
    wjsonl(OUT + '/train.jsonl', tr_rows)
    wjsonl(OUT + '/heldout.jsonl', ho_rows)
    # fixed evaluation sample: 300 held-out rows, at least 15 per class where available
    by = {}
    for r in ho_rows:
        by.setdefault(r['label'], []).append(r)
    rnd = random.Random(SEED)
    sample = []
    for c, lst in by.items():
        rnd.shuffle(lst)
        sample += lst[:15]
    rest = [r for r in ho_rows if r not in sample]
    rnd.shuffle(rest)
    sample += rest[:300 - len(sample)]
    wjsonl(OUT + '/eval_sample.jsonl', sample)
    print('PREP_DONE train=%d heldout=%d eval_sample=%d' % (len(tr_rows), len(ho_rows), len(sample)))


# ------------------------------------------------------------------------------------------------ teacher
def teacher_prompt(row):
    lab = row['label']
    sysmsg = ('You write short, factual explanations of flagged bank transactions for an anti-money-laundering analyst. '
              'You are told the true typology of the activity; explain which numbers in the input support it or are missing. '
              'Use ONLY numbers that appear in the input JSON, written exactly as they appear. Do not invent amounts, counts or dates. '
              'If the numbers do not show the shape, say it is not visible from these numbers and use low confidence. '
              'Reply with JSON only: {"confidence": "low|medium|high", "narrative": "<= 3 sentences, <= 550 characters", '
              '"next_checks": ["<= 3 short concrete checks in the case graph or account history"]}')
    user = ('INPUT %s\nTRUE TYPOLOGY: %s (%s)\nField meanings: snd_* = the sender\'s earlier payments, rcv_* = payments the receiver got earlier, '
            'ctx_snd_in_* = payments the sender itself received in the previous 24 hours, ctx_rcv_out_* = payments the receiver made in the previous 24 hours, '
            'ctx_reverse_pair_96h = earlier payments from the receiver back to the sender, typology_hint = a classifier guess.'
            % (json.dumps(row['input'], separators=(',', ':')), lab, TEACHER_GUIDE[lab]))
    return [{'role': 'system', 'content': sysmsg}, {'role': 'user', 'content': user}]


def parse_json(text):
    m = re.search(r'\{.*\}', text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def teacher_worker(gpu, infile, outfile):
    os.environ['CUDA_VISIBLE_DEVICES'] = str(gpu)
    import torch
    from transformers import AutoTokenizer, AutoModelForCausalLM, BitsAndBytesConfig
    tok = AutoTokenizer.from_pretrained(TEACHER)
    tok.padding_side = 'left'
    model = AutoModelForCausalLM.from_pretrained(TEACHER, device_map={'': 0}, torch_dtype=torch.float16,
                                                 quantization_config=BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_compute_dtype=torch.float16,
                                                                                         bnb_4bit_quant_type='nf4'))
    model.eval()
    rows = rjsonl(infile)
    done = {json.loads(l)['transaction_id'] for l in open(outfile)} if os.path.exists(outfile) else set()
    rows = [r for r in rows if r['transaction_id'] not in done]
    bs = 24
    t0 = time.time()
    with open(outfile, 'a') as f:
        for i in range(0, len(rows), bs):
            chunk = rows[i:i + bs]
            texts = [tok.apply_chat_template(teacher_prompt(r), tokenize=False, add_generation_prompt=True) for r in chunk]
            enc = tok(texts, return_tensors='pt', padding=True).to('cuda')
            with torch.no_grad():
                out = model.generate(**enc, max_new_tokens=230, do_sample=False, pad_token_id=tok.eos_token_id)
            for r, o in zip(chunk, out):
                txt = tok.decode(o[enc['input_ids'].shape[1]:], skip_special_tokens=True)
                f.write(json.dumps({'transaction_id': r['transaction_id'], 'raw': txt}) + '\n')
            f.flush()
            print('gpu%d %d/%d %.0fs' % (gpu, i + len(chunk), len(rows), time.time() - t0), flush=True)


def teacher():
    tr = rjsonl(OUT + '/train.jsonl')
    ev = rjsonl(OUT + '/eval_sample.jsonl')
    allrows = tr + ev
    if os.environ.get('LIMIT'):  # dry run on a few rows
        allrows = allrows[:int(os.environ['LIMIT'])]
        ev = [r for r in ev if r in allrows]
    halves = [allrows[0::2], allrows[1::2]]
    procs = []
    for g in (0, 1):
        wjsonl(OUT + '/teacher_in_%d.jsonl' % g, halves[g])
        procs.append(subprocess.Popen([sys.executable, __file__, 'teacher_worker', str(g), OUT + '/teacher_in_%d.jsonl' % g,
                                       OUT + '/teacher_out_%d.jsonl' % g]))
    rc = [p.wait() for p in procs]
    if any(rc):
        raise SystemExit('teacher worker failed %s' % rc)
    raw = {}
    for g in (0, 1):
        for l in open(OUT + '/teacher_out_%d.jsonl' % g):
            x = json.loads(l)
            raw[x['transaction_id']] = x['raw']
    kept, dropped, reasons = [], 0, {}
    for r in allrows:
        obj = parse_json(raw.get(r['transaction_id'], ''))
        if obj is None:
            dropped += 1; reasons['unparsed'] = reasons.get('unparsed', 0) + 1
            continue
        brief = {'typology': r['label'], 'confidence': obj.get('confidence'), 'narrative': str(obj.get('narrative', '')).strip(),
                 'next_checks': [str(x).strip()[:140] for x in (obj.get('next_checks') or [])][:3]}
        probs = E.validate(brief, r['input'])
        if probs:
            dropped += 1
            k = probs[0].split(' ')[0] + ' ' + probs[0].split(' ')[1] if ' ' in probs[0] else probs[0]
            reasons[k] = reasons.get(k, 0) + 1
            continue
        kept.append(dict(r, target=brief))
    ev_ids = {r['transaction_id'] for r in ev}
    wjsonl(OUT + '/sft_train.jsonl', [r for r in kept if r['transaction_id'] not in ev_ids])
    wjsonl(OUT + '/teacher_eval_ref.jsonl', [r for r in kept if r['transaction_id'] in ev_ids])
    stats = {'teacher': TEACHER, 'prompts': len(allrows), 'kept': len(kept), 'dropped': dropped, 'drop_reasons': reasons}
    json.dump(stats, open(OUT + '/teacher_stats.json', 'w'), indent=1)
    print('TEACHER_DONE', json.dumps(stats))


# ------------------------------------------------------------------------------------------------ train
def train():
    import torch
    from transformers import AutoTokenizer, AutoModelForCausalLM
    from peft import LoraConfig, get_peft_model
    torch.manual_seed(SEED)
    random.seed(SEED)
    tok = AutoTokenizer.from_pretrained(STUDENT)
    model = AutoModelForCausalLM.from_pretrained(STUDENT, torch_dtype=torch.float16, device_map={'': 0})
    model.gradient_checkpointing_enable()
    model.enable_input_require_grads()
    model = get_peft_model(model, LoraConfig(r=16, lora_alpha=32, lora_dropout=0.05, task_type='CAUSAL_LM',
                                             target_modules=['q_proj', 'k_proj', 'v_proj', 'o_proj', 'gate_proj', 'up_proj', 'down_proj']))
    for p in model.parameters():
        if p.requires_grad:
            p.data = p.data.float()
    rows = rjsonl(OUT + '/sft_train.jsonl')
    ex = []
    for r in rows:
        p_ids = tok(tok.apply_chat_template(E.messages(r['input']), tokenize=False, add_generation_prompt=True), add_special_tokens=False)['input_ids']
        t_ids = tok(json.dumps(r['target'], separators=(',', ':')) + '<|im_end|>', add_special_tokens=False)['input_ids']
        ids = (p_ids + t_ids)[:1024]
        lab = ([-100] * len(p_ids) + t_ids)[:1024]
        ex.append((ids, lab))
    print('sft examples', len(ex), 'max len', max(len(i) for i, _ in ex), flush=True)
    opt = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad], lr=2e-4, weight_decay=0.0)
    scaler = torch.cuda.amp.GradScaler()
    epochs, micro, accum = int(os.environ.get('EPOCHS', 2)), 4, 4
    steps_total = epochs * math.ceil(len(ex) / (micro * accum))
    sched = torch.optim.lr_scheduler.LambdaLR(opt, lambda s: min(1.0, (s + 1) / 20) * max(0.0, 1 - s / steps_total))
    model.train()
    step, t0 = 0, time.time()
    for ep in range(epochs):
        random.shuffle(ex)
        for i in range(0, len(ex), micro):
            batch = ex[i:i + micro]
            L = max(len(a) for a, _ in batch)
            ids = torch.tensor([a + [tok.pad_token_id] * (L - len(a)) for a, _ in batch], device='cuda')
            lab = torch.tensor([b + [-100] * (L - len(b)) for _, b in batch], device='cuda')
            att = torch.tensor([[1] * len(a) + [0] * (L - len(a)) for a, _ in batch], device='cuda')
            with torch.autocast('cuda', dtype=torch.float16):
                loss = model(input_ids=ids, attention_mask=att, labels=lab).loss / accum
            scaler.scale(loss).backward()
            if (i // micro + 1) % accum == 0:
                scaler.unscale_(opt)
                torch.nn.utils.clip_grad_norm_([p for p in model.parameters() if p.requires_grad], 1.0)
                scaler.step(opt); scaler.update(); opt.zero_grad(); sched.step(); step += 1
                if step % 10 == 0:
                    print('epoch %d step %d/%d loss %.4f %.0fs' % (ep, step, steps_total, loss.item() * accum, time.time() - t0), flush=True)
    model.save_pretrained(OUT + '/lora')
    del model, opt
    torch.cuda.empty_cache()
    # merge into fp16 weights and convert to GGUF q8_0
    from peft import PeftModel
    base = AutoModelForCausalLM.from_pretrained(STUDENT, torch_dtype=torch.float16)
    merged = PeftModel.from_pretrained(base, OUT + '/lora').merge_and_unload()
    merged.save_pretrained('/kaggle/working/merged', safe_serialization=True)
    tok.save_pretrained('/kaggle/working/merged')
    if not os.path.exists('/kaggle/working/llama.cpp'):
        sh('git clone --depth 1 https://github.com/ggml-org/llama.cpp /kaggle/working/llama.cpp')
    sh('pip -q install -e /kaggle/working/llama.cpp/gguf-py')
    # Q4_K_M is about twice as fast on Lambda CPUs as q8_0; fall back to q8_0 if building the quantizer fails
    quant = 'q4_k_m'
    try:
        sh('python /kaggle/working/llama.cpp/convert_hf_to_gguf.py /kaggle/working/merged --outtype f16 --outfile /kaggle/working/model-f16.gguf')
        sh('cd /kaggle/working/llama.cpp && cmake -B build -DGGML_NATIVE=OFF -DLLAMA_CURL=OFF -DGGML_CUDA=OFF > /dev/null && '
           'cmake --build build --target llama-quantize -j 4 > /dev/null')
        sh('/kaggle/working/llama.cpp/build/bin/llama-quantize /kaggle/working/model-f16.gguf %s/model.gguf Q4_K_M' % OUT)
    except Exception as ex:
        print('Q4_K_M failed, using q8_0:', ex)
        quant = 'q8_0'
        sh('python /kaggle/working/llama.cpp/convert_hf_to_gguf.py /kaggle/working/merged --outtype q8_0 --outfile %s/model.gguf' % OUT)
    def sha_of(p):
        h = hashlib.sha256()
        with open(p, 'rb') as f:
            for b in iter(lambda: f.read(1 << 24), b''):
                h.update(b)
        return h.hexdigest()
    sha = sha_of(OUT + '/model.gguf')
    open(OUT + '/model.sha256', 'w').write(sha)
    # the untuned base model, same format: the release gate must reject it (negative test)
    try:
        from huggingface_hub import snapshot_download
        bdir = snapshot_download(STUDENT)
        if quant == 'q4_k_m':
            sh('python /kaggle/working/llama.cpp/convert_hf_to_gguf.py %s --outtype f16 --outfile /kaggle/working/base-f16.gguf' % bdir)
            sh('/kaggle/working/llama.cpp/build/bin/llama-quantize /kaggle/working/base-f16.gguf %s/base.gguf Q4_K_M' % OUT)
        else:
            sh('python /kaggle/working/llama.cpp/convert_hf_to_gguf.py %s --outtype q8_0 --outfile %s/base.gguf' % (bdir, OUT))
        open(OUT + '/base.sha256', 'w').write(sha_of(OUT + '/base.gguf'))
    except Exception as ex:
        print('base model conversion failed (negative test will be skipped):', ex)
    # quick check with the merged fp16 model on the GPU (no grammar here; the Lambda adds one)
    merged = merged.to('cuda').eval()
    tok.padding_side = 'left'
    ev = rjsonl(OUT + '/eval_sample.jsonl')[:60]
    ok_json, ok_valid, agree = 0, 0, 0
    for i in range(0, len(ev), 12):
        chunk = ev[i:i + 12]
        texts = [tok.apply_chat_template(E.messages(r['input']), tokenize=False, add_generation_prompt=True) for r in chunk]
        enc = tok(texts, return_tensors='pt', padding=True).to('cuda')
        with torch.no_grad():
            out = merged.generate(**enc, max_new_tokens=256, do_sample=False, pad_token_id=tok.eos_token_id)
        for r, o in zip(chunk, out):
            obj = parse_json(tok.decode(o[enc['input_ids'].shape[1]:], skip_special_tokens=True))
            if obj is not None:
                ok_json += 1
                ok_valid += not E.validate(obj, r['input'])
                agree += obj.get('typology') == r['label']
    for f in ('/kaggle/working/model-f16.gguf', '/kaggle/working/base-f16.gguf'):  # keep /kaggle/working under its 20 GB limit
        if os.path.exists(f):
            os.remove(f)
    card = {'base_model': STUDENT, 'license': 'Apache-2.0', 'method': 'LoRA r16 alpha32 dropout0.05, all linear layers, 2 epochs, lr 2e-4, fp16 autocast',
            'train_rows': len(ex), 'train_dates': '2022-09-01 to 2022-09-13', 'heldout_dates': '2022-09-14 to 2022-09-16', 'seed': SEED,
            'teacher': TEACHER, 'gguf': quant, 'sha256': sha, 'size_bytes': os.path.getsize(OUT + '/model.gguf'),
            'quick_check_60': {'json_parsed': ok_json, 'valid': ok_valid, 'typology_correct': agree},
            'input_allowlist': E.ALLOWED, 'never_inputs': sorted(E.FORBIDDEN)}
    json.dump(card, open(OUT + '/training_card.json', 'w'), indent=1)
    print('TRAIN_DONE', json.dumps(card['quick_check_60']), sha)


def upload(urls):
    import requests
    for name, url in json.loads(urls).items():
        path = os.path.join(OUT, name)
        size = os.path.getsize(path)
        with open(path, 'rb') as f:
            r = requests.put(url, data=f, headers={'Content-Length': str(size)}, timeout=3600)
        print('UPLOAD', name, size, r.status_code, r.text[:200], flush=True)
        r.raise_for_status()
    print('UPLOAD_DONE')


if __name__ == '__main__':
    cmd = sys.argv[1]
    if cmd == 'prep':
        prep(sys.argv[2])
    elif cmd == 'teacher':
        teacher()
    elif cmd == 'teacher_worker':
        teacher_worker(int(sys.argv[2]), sys.argv[3], sys.argv[4])
    elif cmd == 'train':
        train()
    elif cmd == 'upload':
        upload(sys.argv[2])
