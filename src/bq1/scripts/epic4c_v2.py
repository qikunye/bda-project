"""Compare feature set v1 vs v2 with XGBoost. Choose on VALIDATION; test is reported for confirmation."""
import sys, json, math, time, shutil, os; sys.path.insert(0, ".")
import numpy as np, pandas as pd
from src.bq1 import split as S, train as T, metrics as M

A = "artifacts/small/"
# merge v1 features + pass-through extras into one v2 parquet (pandas, no Spark)
if not os.path.exists("data/features/li_small_v2/part.parquet"):
    v1 = pd.read_parquet("data/features/li_small_v1"); ex = pd.read_parquet("data/features/li_small_v2_extra")
    m = v1.merge(ex, on="transaction_id", how="left", validate="one_to_one")
    assert len(m) == len(v1) and m["snd_in_cnt_24h"].notna().all()
    os.makedirs("data/features/li_small_v2", exist_ok=True)
    m.to_parquet("data/features/li_small_v2/part.parquet", index=False); del v1, ex, m
cfg = json.load(open(A + "split_config.json"))
df = S.load_features("data/features/li_small_v2", cfg["window_start_ts"], cfg["window_end_ts"], variant="v2")
df["split"] = S.assign_split(df["ts"], cfg["t60_ts"], cfg["t80_ts"])
stats = S.fit_train_stats(df[df.split == "train"])
df = S.apply_categories(S.add_amt_vs_ccy_median(df, stats), stats)
tr, va, te = (df[df.split == s].reset_index(drop=True) for s in ["train", "validation", "test"])
tie_te = M.tie_rank(te.transaction_id); y = te.is_laundering.values
f1, f2 = S.feature_order("v1"), S.feature_order("v2")
tuned = dict(max_depth=8, min_child_weight=20)
spw_base = math.sqrt((tr.is_laundering == 0).sum() / tr.is_laundering.sum())
rows = []
for label, feats, spw, kw in [("v1 baseline", f1, spw_base, {}), ("v1 tuned", f1, 5, tuned),
                              ("v2 baseline", f2, spw_base, {}), ("v2 tuned", f2, 5, tuned)]:
    t0 = time.time()
    m, vap, _ = T.fit_xgb(tr, va, spw, features=feats, **kw)
    ts_ = m.predict_proba(te[feats])[:, 1]
    r = dict(model=label, best_iter=int(m.best_iteration), val_pr_auc=round(vap, 4), test_pr_auc=round(M.pr_auc(y, ts_), 4),
             test_R_0p1=round(M.recall_at_k(y, ts_, tie_te, .001), 3), test_R_1=round(M.recall_at_k(y, ts_, tie_te, .01), 3),
             test_P_0p1=round(M.precision_at_k(y, ts_, tie_te, .001), 4), test_P_1=round(M.precision_at_k(y, ts_, tie_te, .01), 4),
             secs=round(time.time() - t0))
    rows.append(r); print(r, flush=True)
    if label == "v2 tuned":
        imp = T.xgb_importance(m, 20); imp.to_csv(A + "xgb_v2_importance_top20.csv", index=False)
        print(imp.head(12).to_string(index=False), flush=True)
pd.DataFrame(rows).to_csv(A + "v1_vs_v2_xgb.csv", index=False)
