import sys, json, time, math, pickle; sys.path.insert(0, ".")
import numpy as np, pandas as pd
from src.bq1 import split as S, train as T, metrics as M

A = "artifacts/small/"
cfg = json.load(open(A + "split_config.json"))
t0 = time.time()
df = S.load_features("data/features/li_small_v1", cfg["window_start_ts"], cfg["window_end_ts"])
df["split"] = S.assign_split(df["ts"], cfg["t60_ts"], cfg["t80_ts"])
stats = S.fit_train_stats(df[df.split == "train"])
df = S.apply_categories(S.add_amt_vs_ccy_median(df, stats), stats)
tr, va, te = (df[df.split == s].reset_index(drop=True) for s in ["train", "validation", "test"])
tie_va, tie_te = M.tie_rank(va.transaction_id), M.tie_rank(te.transaction_id)
print(f"loaded {len(df)} rows in {time.time()-t0:.0f}s | train {len(tr)} val {len(va)} test {len(te)}", flush=True)
scores = pd.DataFrame({"transaction_id": pd.concat([va.transaction_id, te.transaction_id]).values,
                       "split": ["validation"] * len(va) + ["test"] * len(te),
                       "y": np.concatenate([va.is_laundering, te.is_laundering])})

# T4.1 rules
rules = T.fit_rules(tr)
S.write_json(rules, A + "rules_v1.json")
scores["rules"] = np.concatenate([T.score_rules(va, rules), T.score_rules(te, rules)])
print("T4.1 rules:", json.dumps({k: round(v, 3) for k, v in rules["weights"].items()}), flush=True)
for k, v in rules["detail"].items(): print("   ", k, v)

# T4.2 LR
t1 = time.time()
lr, lr_info = T.fit_lr(tr)
fl = S.feature_order()
scores["lr"] = np.concatenate([lr.decision_function(va[fl]), lr.decision_function(te[fl])])
T.lr_coefficients(lr).to_csv(A + "lr_coefficients.csv", index=False)
pickle.dump(lr, open(A + "lr_model.pkl", "wb"))
lr_time = time.time() - t1
print(f"T4.2 LR done in {lr_time:.0f}s {lr_info}", flush=True)
scores.to_parquet(A + "scores_small.parquet")

# T4.3 XGBoost
neg, pos = int((tr.is_laundering == 0).sum()), int((tr.is_laundering == 1).sum())
trials = {"sqrt(neg/pos)": math.sqrt(neg / pos), "neg/pos": neg / pos}
best = None
for name, spw in trials.items():
    t1 = time.time()
    m, vap, _ = T.fit_xgb(tr, va, spw)
    print(f"T4.3 spw={name} ({spw:.1f}): best_iter {m.best_iteration}, val PR-AUC {vap:.4f}, {time.time()-t1:.0f}s", flush=True)
    if best is None or vap > best[2]:
        best = (name, spw, vap, m, time.time() - t1)
name, spw, vap, xgbm, xgb_time = best
fx = S.feature_order()
scores["xgb"] = np.concatenate([xgbm.predict_proba(va[fx])[:, 1], xgbm.predict_proba(te[fx])[:, 1]])
import os; os.makedirs(A + "xgboost", exist_ok=True)
xgbm.save_model(A + "xgboost/model.ubj")
scores.to_parquet(A + "scores_small.parquet")
S.write_json({"chosen_scale_pos_weight": name, "scale_pos_weight": spw, "best_iteration": int(xgbm.best_iteration),
              "val_pr_auc": vap, "trials": {k: v for k, v in trials.items()}, "train_seconds_chosen": xgb_time,
              "neg": neg, "pos": pos}, A + "xgboost/xgb_summary.json")
print(f"T4.3 chosen: spw={name}, best_iter {xgbm.best_iteration}, val PR-AUC {vap:.4f}", flush=True)

# T4.4 importance
imp = T.xgb_importance(xgbm)
imp.to_csv(A + "xgb_importance_top20.csv", index=False)
print("T4.4 top 20 gain:\n" + imp.to_string(index=False), flush=True)

# T4.5 leakage audit (test split)
rows = [{"feature": f, "test_pr_auc": T.single_feature_prauc(tr, te, f)} for f in fl]
aud = pd.DataFrame(rows).sort_values("test_pr_auc", ascending=False)
aud["flag_gt_0.5"] = aud["test_pr_auc"] > 0.5
aud.to_csv(A + "leakage_audit_single_feature.csv", index=False)
print("T4.5 single-feature test PR-AUC (random baseline = prevalence %.5f):" % te.is_laundering.mean())
print(aud.head(12).to_string(index=False), flush=True)
mflag = []
for mdl in ["rules", "lr", "xgb"]:
    ts_ = scores[scores.split == "test"]
    r1 = M.recall_at_k(ts_.y.values, ts_[mdl].values, tie_te, 0.01)
    ap = M.pr_auc(ts_.y.values, ts_[mdl].values)
    print(f"   {mdl}: test Recall@1% = {r1:.4f}, PR-AUC = {ap:.4f}")
    if r1 > 0.99: mflag.append(mdl)
print("FLAGGED features:", aud[aud["flag_gt_0.5"]].feature.tolist(), "| FLAGGED models (Recall@1%>0.99):", mflag)
print(f"total {time.time()-t0:.0f}s")
