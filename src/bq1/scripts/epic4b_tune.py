"""Option 2: tune XGBoost on VALIDATION only. Test is read once at the end for the chosen config."""
import sys, json, time, math; sys.path.insert(0, ".")
import numpy as np, pandas as pd
from src.bq1 import split as S, train as T, metrics as M

A = "artifacts/small/"
cfg = json.load(open(A + "split_config.json"))
df = S.load_features("data/features/li_small_v1", cfg["window_start_ts"], cfg["window_end_ts"])
df["split"] = S.assign_split(df["ts"], cfg["t60_ts"], cfg["t80_ts"])
stats = S.fit_train_stats(df[df.split == "train"])
df = S.apply_categories(S.add_amt_vs_ccy_median(df, stats), stats)
tr, va, te = (df[df.split == s].reset_index(drop=True) for s in ["train", "validation", "test"])
tie_va, tie_te = M.tie_rank(va.transaction_id), M.tie_rank(te.transaction_id)
yv = va.is_laundering.values
log = []

def trial(label, spw, **kw):
    t0 = time.time()
    m, ap, vs = T.fit_xgb(tr, va, spw, **kw)
    r = dict(label=label, spw=spw, **kw, best_iter=int(m.best_iteration), val_pr_auc=ap,
             val_recall_0p1=M.recall_at_k(yv, vs, tie_va, 0.001), val_recall_1=M.recall_at_k(yv, vs, tie_va, 0.01),
             val_prec_0p1=M.precision_at_k(yv, vs, tie_va, 0.001), secs=round(time.time() - t0))
    log.append(r); pd.DataFrame(log).to_csv(A + "xgb_tuning_log.csv", index=False)
    print({k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()}, flush=True)
    return r

# stage 1: scale_pos_weight (47.9 = current baseline)
best = max([trial(f"spw{w}", w) for w in (5, 10, 20, 47.9)], key=lambda r: r["val_pr_auc"])
spw = best["spw"]; print("stage1 best spw", spw, flush=True)
# stage 2: one parameter at a time around the best spw
cur = {}
for name, vals in [("max_depth", (4, 8)), ("min_child_weight", (5, 20)), ("reg_lambda", (10, 50))]:
    cands = [trial(f"{name}={v}", spw, **{**cur, name: v}) for v in vals]
    b = max(cands, key=lambda r: r["val_pr_auc"])
    if b["val_pr_auc"] > best["val_pr_auc"] * 1.01:   # require a 1% relative gain to adopt
        best = b; cur[name] = b[name]
        print("adopt", name, b[name], flush=True)
final = {"spw": spw, **cur, "val_pr_auc": best["val_pr_auc"]}
json.dump(final, open(A + "xgb_tuned_params.json", "w"), indent=2)
print("FINAL", final, flush=True)

# one test read: baseline vs tuned
for name, kw, w in [("baseline", {}, math.sqrt((tr.is_laundering == 0).sum() / tr.is_laundering.sum())),
                    ("tuned", cur, spw)]:
    m, ap, _ = T.fit_xgb(tr, va, w, **kw)
    ts_ = m.predict_proba(te[S.feature_order()])[:, 1]
    print(name, "TEST PR-AUC %.4f" % M.pr_auc(te.is_laundering.values, ts_),
          "| R@0.1%% %.3f R@1%% %.3f | P@0.1%% %.4f P@1%% %.4f" % tuple(
              f(te.is_laundering.values, ts_, tie_te, k) for f, k in
              [(M.recall_at_k, .001), (M.recall_at_k, .01), (M.precision_at_k, .001), (M.precision_at_k, .01)]), flush=True)
