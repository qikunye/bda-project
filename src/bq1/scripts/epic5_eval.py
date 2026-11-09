import sys, json, math, time, pickle, os; sys.path.insert(0, ".")
import numpy as np, pandas as pd
from src.bq1 import split as S, train as T, metrics as M

A = "artifacts/small/"
cfg = json.load(open(A + "split_config.json")); tuned = json.load(open(A + "xgb_tuned_params.json"))
df = S.load_features("data/features/li_small_v1", cfg["window_start_ts"], cfg["window_end_ts"])
df["split"] = S.assign_split(df["ts"], cfg["t60_ts"], cfg["t80_ts"])
stats = S.fit_train_stats(df[df.split == "train"])
df = S.apply_categories(S.add_amt_vs_ccy_median(df, stats), stats)
tr, va, te = (df[df.split == s].reset_index(drop=True) for s in ["train", "validation", "test"])
f = S.feature_order()

# retrain XGBoost with the tuned parameters (validation early stopping), save model + scores
t0 = time.time()
kw = {k: tuned[k] for k in ("max_depth", "min_child_weight") if k in tuned}
xgbm, val_ap, _ = T.fit_xgb(tr, va, tuned["spw"], **kw)
xgb_secs = time.time() - t0
os.makedirs(A + "xgboost", exist_ok=True)
xgbm.save_model(A + "xgboost/model.ubj")
T.xgb_importance(xgbm, 20).to_csv(A + "xgb_importance_top20.csv", index=False)
sc = pd.read_parquet(A + "scores_small.parquet")
assert (sc[sc.split == "validation"].transaction_id.values == va.transaction_id.values).all()
sc.loc[sc.split == "validation", "xgb"] = xgbm.predict_proba(va[f])[:, 1]
sc.loc[sc.split == "test", "xgb"] = xgbm.predict_proba(te[f])[:, 1]
sc.to_parquet(A + "scores_small.parquet")
print(f"xgb retrained {xgb_secs:.0f}s best_iter {xgbm.best_iteration} val PR-AUC {val_ap:.4f}", flush=True)

# T5.2 test table
t = sc[sc.split == "test"].reset_index(drop=True)
y = t.y.values; n = len(y); P = int(y.sum()); prev = P / n
tie = M.tie_rank(t.transaction_id)
names = {"rules": "Rules", "lr": "Logistic Regression", "xgb": "XGBoost"}
rows, auc = [], {}
for k, nm in names.items():
    auc[nm] = M.pr_auc(y, t[k].values)
    d = M.summary_table(y, t[k].values, tie, nm)
    d["recall_ci95_halfwidth"] = 1.96 * np.sqrt(d.recall * (1 - d.recall) / P)
    rows.append(d)
tab = pd.concat(rows)
rnd = pd.DataFrame([{"model": "Random ranking", "K": k, "alerts_reviewed": int(math.ceil(k * n)), "tp": k * P,
                     "recall": k, "precision": prev, "alert_reduction": 1 - k, "fp_per_tp": (1 - prev) / prev,
                     "recall_ci95_halfwidth": float("nan")} for k in M.K_GRID])
tab = pd.concat([tab, rnd]); tab["pr_auc"] = tab.model.map({**auc, "Random ranking": prev})
tab.to_csv(A + "eval_table_test.csv", index=False)
pd.set_option("display.width", 220)
print(f"TEST: {n} rows, {P} positives, prevalence {prev:.5f}")
show = tab.copy()
for c in ("recall", "precision", "recall_ci95_halfwidth", "pr_auc"): show[c] = show[c].round(4)
show["fp_per_tp"] = show.fp_per_tp.round(1); show["K"] = (show.K * 100).round(2).astype(str) + "%"
print(show[["model", "K", "alerts_reviewed", "recall", "recall_ci95_halfwidth", "precision", "alert_reduction", "fp_per_tp", "pr_auc"]].to_string(index=False))

# T5.3 curve: 200 K values, geometric from 0.01% to 20%
ks = np.geomspace(0.0001, 0.20, 200); ms = np.maximum(1, np.ceil(ks * n).astype(int))
curve = pd.DataFrame({"K": ks, "alerts_reviewed": ms})
for k, nm in names.items():
    order = M.rank_order(t[k].values, tie); ctp = np.cumsum(y[order])
    curve[f"recall_{k}"] = ctp[ms - 1] / P; curve[f"precision_{k}"] = ctp[ms - 1] / ms
curve["recall_random"] = ks
curve.to_csv(A + "curve_small.csv", index=False)
print("curve rows", len(curve), "K range", ks[0], ks[-1])

# metrics.json
mj = {"dataset": "li_small", "split": "test", "test_rows": n, "test_positives": P, "prevalence": prev, "seed": S.SEED,
      "pr_auc": {**auc, "Random ranking": prev}, "primary_metric": "Recall@1%",
      "xgb": {"val_pr_auc": val_ap, "best_iteration": int(xgbm.best_iteration), "train_seconds": xgb_secs, **tuned},
      "table": json.loads(tab.to_json(orient="records"))}
S.write_json(mj, A + "metrics.json")
