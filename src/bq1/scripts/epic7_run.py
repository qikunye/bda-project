"""Epic 7 runner for the SageMaker notebook. Stages: split (T7.3), train (T7.4-T7.6), score (T7.7).

  python scripts/epic7_run.py split|train|score [--root /home/ec2-user/SageMaker/bq1_work]

Writes only under gold/bq1/ and models/bq1/ in S3. A log per stage is uploaded to models/bq1/epic7_logs/.
"""
import argparse, datetime as dt, glob, json, math, os, pickle, subprocess, sys, time
sys.path.insert(0, ".")
import numpy as np, pandas as pd

BUCKET = "s3://is459-aml-g1t5"
FEATURES_S3 = f"{BUCKET}/gold/bq1/features/li-medium-v1/"
SCORES_S3 = f"{BUCKET}/gold/bq1/scores/li-medium-v1/"
MODELS_S3 = f"{BUCKET}/models/bq1"
LOGS_S3 = f"{MODELS_S3}/epic7_logs/"
TUNED = {"spw": 5, "max_depth": 8, "min_child_weight": 20}  # LI-Small validation-tuned; not re-tuned (T7.4)
MODEL_VERSION = "v1"


def sh(*cmd):
    subprocess.run(cmd, check=True)


def s3_empty(prefix):
    out = subprocess.run(["aws", "s3", "ls", prefix], capture_output=True, text=True)
    return out.stdout.strip() == ""


class Tee:
    def __init__(self, path):
        self.f, self.o = open(path, "a"), sys.stdout
    def write(self, s):
        self.o.write(s); self.f.write(s)
    def flush(self):
        self.o.flush(); self.f.flush()


def utc(t):
    return dt.datetime.fromtimestamp(int(t), dt.timezone.utc).strftime("%Y-%m-%d %H:%M")


def fetch_features(feat_dir):
    if not os.path.isdir(feat_dir) or len(os.listdir(feat_dir)) < 27:
        os.makedirs(feat_dir, exist_ok=True)
        sh("aws", "s3", "sync", FEATURES_S3, feat_dir, "--only-show-errors")
    print("feature partitions on disk:", len(os.listdir(feat_dir)), flush=True)


# ------------------------------------------------------------------ T7.3
def stage_split(a):
    from src.bq1 import split as S
    fetch_features(a.feat_dir)
    # only ts and the label are needed here; loading 31M transaction_id strings ran the notebook out of memory
    df = pd.read_parquet(a.feat_dir, columns=["ts", "is_laundering"])
    n, pos = len(df), int(df.is_laundering.sum())
    print(f"loaded {n:,} rows, {pos:,} positives", flush=True)
    daily = df.groupby(df["ts"].astype("int64") // 86400).agg(rows=("ts", "size"), positives=("is_laundering", "sum"))
    daily.index = [dt.datetime.fromtimestamp(int(d) * 86400, dt.timezone.utc).strftime("%Y-%m-%d") for d in daily.index]
    daily["laundering_rate"] = daily.positives / daily.rows
    med = float(daily.rows.median())
    daily["tail_day"] = daily.rows < 0.10 * med
    daily.to_csv(os.path.join(a.out_dir, "eda_daily_medium.csv"))
    print(daily.to_string(), f"\nmedian daily rows {med:,.0f}; tail rule: rows < 10% of median", flush=True)
    keep = daily[~daily.tail_day].index.tolist()
    tail = daily[daily.tail_day].index.tolist()
    first, last = keep[0], keep[-1]
    inner = [d for d in tail if first < d < last]
    if inner:
        print("STOP: tail days inside the window", inner, "- needs a decision"); return
    start = int(dt.datetime.fromisoformat(first).replace(tzinfo=dt.timezone.utc).timestamp())
    end = int((dt.datetime.fromisoformat(last) + dt.timedelta(days=1)).replace(tzinfo=dt.timezone.utc).timestamp())
    w = df[(df.ts >= start) & (df.ts < end)].reset_index(drop=True)
    t60, t80 = S.find_boundaries(w["ts"])
    split = S.assign_split(w["ts"], t60, t80)
    summ = S.split_summary(w, split)
    print(f"window {first} .. {last} inclusive (tail days dropped: {tail}); rows {len(w):,} positives {int(w.is_laundering.sum()):,}")
    print(f"t60={t60} ({utc(t60)} UTC) t80={t80} ({utc(t80)} UTC)\n" + summ.to_string(index=False))
    gate = bool((summ.positives < 300).any())
    print("GATE (a split has <300 positives):", gate)
    cfg = {"dataset": "li_medium", "silver_table": "is459_aml_silver.li_medium_transactions_medium_live_01",
           "timezone": "UTC", "seed": S.SEED,
           "window_start": f"{first} 00:00:00", "window_end_exclusive": utc(end) + ":00",
           "window_start_ts": start, "window_end_ts": end,
           "tail_rule": "day rows < 10% of median daily rows", "tail_days_dropped": tail,
           "tail_rows_dropped": int(daily.loc[tail, "rows"].sum()) if tail else 0,
           "tail_positives_dropped": int(daily.loc[tail, "positives"].sum()) if tail else 0,
           "decision": "same tail rule as LI-Small (plan T7.3)",
           "split_rule": "train: ts < t60; validation: t60 <= ts < t80; test: ts >= t80 (60/20/20 by row)",
           "t60_ts": t60, "t80_ts": t80, "t60_utc": utc(t60), "t80_utc": utc(t80),
           "splits": summ.to_dict(orient="records"), "gate_triggered": gate}
    S.write_json(cfg, os.path.join(a.out_dir, "split_config.json"))
    print("wrote split_config.json")


# ------------------------------------------------------------------ T7.4-T7.6
def stage_train(a):
    from src.bq1 import split as S, train as T, metrics as M
    import xgboost, sklearn
    cfg = json.load(open(os.path.join(a.out_dir, "split_config.json")))
    assert not cfg["gate_triggered"], "split gate was triggered; get a decision first"
    fetch_features(a.feat_dir)
    t0 = time.time()
    df = S.load_features(a.feat_dir, cfg["window_start_ts"], cfg["window_end_ts"], with_id=False)
    df["split"] = S.assign_split(df["ts"], cfg["t60_ts"], cfg["t80_ts"])
    stats = S.fit_train_stats(df[df.split == "train"])
    df = S.apply_categories(S.add_amt_vs_ccy_median(df, stats), stats)
    tr, va, te = (df[df.split == s].reset_index(drop=True) for s in ["train", "validation", "test"])
    del df
    f = S.feature_order()
    print(f"loaded in {time.time()-t0:.0f}s | train {len(tr):,} val {len(va):,} test {len(te):,}", flush=True)
    # transaction_id only for the test rows (tie-break); same row order as te because both follow the file order
    te_ids = []
    for _, path in sorted((os.path.basename(os.path.dirname(p)), p) for p in glob.glob(f"{a.feat_dir}/txn_date=*/*.parquet")):
        x = pd.read_parquet(path, columns=["transaction_id", "ts"])
        te_ids.append(x.loc[(x.ts >= cfg["t80_ts"]) & (x.ts < cfg["window_end_ts"]), "transaction_id"])
    te_ids = pd.concat(te_ids).reset_index(drop=True)
    assert len(te_ids) == len(te), (len(te_ids), len(te))
    tie_te = M.tie_rank(te_ids)
    y = te.is_laundering.values
    scores, secs = {}, {}

    rules = T.fit_rules(tr)
    scores["Rules"] = T.score_rules(te, rules)
    print("rules weights:", json.dumps({k: round(v, 3) for k, v in rules["weights"].items()}), flush=True)

    t1 = time.time()
    lr, lr_info = T.fit_lr(tr)
    scores["Logistic Regression"] = lr.decision_function(te[f]); secs["lr"] = time.time() - t1
    print(f"LR {secs['lr']:.0f}s {lr_info}", flush=True)

    t1 = time.time()
    xgbm, val_ap, _ = T.fit_xgb(tr, va, TUNED["spw"], n_jobs=os.cpu_count(),
                                max_depth=TUNED["max_depth"], min_child_weight=TUNED["min_child_weight"])
    secs["xgb"] = time.time() - t1
    scores["XGBoost"] = xgbm.predict_proba(te[f])[:, 1]
    print(f"XGB {secs['xgb']:.0f}s best_iter {xgbm.best_iteration} val PR-AUC {val_ap:.4f}", flush=True)
    imp = T.xgb_importance(xgbm, 20)
    imp.to_csv(os.path.join(a.out_dir, "xgb_importance_top20.csv"), index=False)
    print(imp.to_string(index=False), flush=True)
    print("day_of_week gain share:", float(imp.loc[imp.feature == "day_of_week", "gain_share"].sum()), flush=True)

    # T7.5 test table + curve
    n, P = len(y), int(y.sum()); prev = P / n
    rows, auc = [], {}
    for nm, s in scores.items():
        auc[nm] = M.pr_auc(y, s)
        d = M.summary_table(y, s, tie_te, nm)
        d["recall_ci95_halfwidth"] = 1.96 * np.sqrt(d.recall * (1 - d.recall) / P)
        rows.append(d)
    rnd = pd.DataFrame([{"model": "Random ranking", "K": k, "alerts_reviewed": int(math.ceil(k * n)), "tp": k * P,
                         "recall": k, "precision": prev, "alert_reduction": 1 - k, "fp_per_tp": (1 - prev) / prev,
                         "recall_ci95_halfwidth": float("nan")} for k in M.K_GRID])
    tab = pd.concat(rows + [rnd]); tab["pr_auc"] = tab.model.map({**auc, "Random ranking": prev})
    tab.to_csv(os.path.join(a.out_dir, "eval_table_test_medium.csv"), index=False)
    show = tab.copy()
    for c in ("recall", "precision", "recall_ci95_halfwidth", "pr_auc"): show[c] = show[c].round(4)
    show["fp_per_tp"] = show.fp_per_tp.round(1); show["K"] = (show.K * 100).round(2).astype(str) + "%"
    pd.set_option("display.width", 220)
    print(f"TEST: {n:,} rows, {P:,} positives, prevalence {prev:.5f}")
    print(show[["model", "K", "alerts_reviewed", "recall", "recall_ci95_halfwidth", "precision", "alert_reduction",
                "fp_per_tp", "pr_auc"]].to_string(index=False), flush=True)
    ks = np.geomspace(0.0001, 0.20, 200); ms = np.maximum(1, np.ceil(ks * n).astype(int))
    curve = pd.DataFrame({"K": ks, "alerts_reviewed": ms})
    for nm, key in [("Rules", "rules"), ("Logistic Regression", "lr"), ("XGBoost", "xgb")]:
        order = M.rank_order(scores[nm], tie_te); ctp = np.cumsum(y[order])
        curve[f"recall_{key}"] = ctp[ms - 1] / P; curve[f"precision_{key}"] = ctp[ms - 1] / ms
    curve["recall_random"] = ks
    curve.to_csv(os.path.join(a.out_dir, "curve_medium.csv"), index=False)

    # leakage audit on test (same rule as T4.5)
    tr_s = tr
    aud = pd.DataFrame([{"feature": c, "test_pr_auc": T.single_feature_prauc(tr_s, te, c)} for c in f]) \
        .sort_values("test_pr_auc", ascending=False)
    aud.to_csv(os.path.join(a.out_dir, "leakage_audit_single_feature_medium.csv"), index=False)
    print("leakage audit top 8 (prevalence %.5f):\n%s" % (prev, aud.head(8).to_string(index=False)))
    print("FLAGGED features (>0.5):", aud[aud.test_pr_auc > 0.5].feature.tolist(),
          "| models with Recall@1% > 0.99:",
          [m for m in scores if float(tab[(tab.model == m) & (tab.K == 0.01)].recall.iloc[0]) > 0.99], flush=True)

    # artefacts
    order = S.feature_order()
    schema = {"feature_version": "v1", "dataset": "li_medium", "seed": S.SEED, "features": order,
              "dtypes": {c: str(tr[c].dtype) for c in order}, "numeric": S.NUMERIC + ["amt_vs_ccy_median"],
              "flags": S.FLAGS, "categorical": S.CATEGORICAL,
              "category_lists": {c: stats["categories"][c] + [S.OTHER] for c in S.CATEGORICAL},
              "fit_on": "train split only",
              "amt_vs_ccy_median": {"definition": "log_amount_paid - median(log_amount_paid) for payment_currency, fitted on train; unseen currency uses the global train median",
                                    "per_currency_median": stats["amt_ccy_median"], "global_median": stats["amt_global_median"]}}
    meta = {"feature_version": "v1", "dataset": "li_medium", "seed": S.SEED,
            "silver_table": cfg["silver_table"], "features_path": FEATURES_S3,
            "library_versions": {"python": sys.version.split()[0], "xgboost": xgboost.__version__,
                                 "scikit-learn": sklearn.__version__, "pandas": pd.__version__, "numpy": np.__version__},
            "hyperparameters": {"xgboost": {**TUNED, "best_iteration": int(xgbm.best_iteration), "val_pr_auc": val_ap,
                                            "note": "LI-Small tuned settings, early stopping on validation, not re-tuned"},
                                "logistic_regression": {"class_weight": "balanced", "max_iter": 1000, **lr_info}},
            "train_seconds": secs, "split": "see split_config.json"}
    mj = {"dataset": "li_medium", "split": "test", "test_rows": n, "test_positives": P, "prevalence": prev, "seed": S.SEED,
          "pr_auc": {**auc, "Random ranking": prev}, "primary_metric": "Recall@1%",
          "table": json.loads(tab.to_json(orient="records"))}
    d = a.out_dir
    S.write_json(rules, f"{d}/rules_v1.json"); S.write_json(schema, f"{d}/feature_schema.json")
    S.write_json(meta, f"{d}/model_metadata.json"); S.write_json(mj, f"{d}/metrics.json")
    xgbm.save_model(f"{d}/model.ubj")
    pickle.dump(lr, open(f"{d}/lr_model.pkl", "wb"))
    from src.bq1.train import lr_coefficients
    lr_coefficients(lr).to_csv(f"{d}/lr_coefficients.csv", index=False)
    common = ["feature_schema.json", "split_config.json", "metrics.json", "model_metadata.json", "rules_v1.json"]
    extra = {"xgboost": ["model.ubj", "xgb_importance_top20.csv"], "logistic_regression": ["lr_model.pkl", "lr_coefficients.csv"],
             "rules": []}
    for m, files in extra.items():
        dest = f"{MODELS_S3}/{m}/{MODEL_VERSION}/"
        assert s3_empty(dest), f"{dest} is not empty; refusing to overwrite"
        for fn in common + files:
            sh("aws", "s3", "cp", f"{d}/{fn}", dest + fn, "--only-show-errors")
    for fn in ["eval_table_test_medium.csv", "curve_medium.csv", "eda_daily_medium.csv",
               "leakage_audit_single_feature_medium.csv"]:
        sh("aws", "s3", "cp", f"{d}/{fn}", f"{MODELS_S3}/xgboost/{MODEL_VERSION}/{fn}", "--only-show-errors")
    print(f"train stage done in {time.time()-t0:.0f}s; artefacts under {MODELS_S3}/*/{MODEL_VERSION}/", flush=True)


# ------------------------------------------------------------------ T7.7
def stage_score(a):
    import xgboost as xgb
    from src.bq1 import score as SC
    cfg = json.load(open(os.path.join(a.out_dir, "split_config.json")))
    schema = json.load(open(os.path.join(a.out_dir, "feature_schema.json")))
    stats = SC.stats_from_schema(schema)
    model = xgb.XGBClassifier(); model.load_model(os.path.join(a.out_dir, "model.ubj"))
    fetch_features(a.feat_dir)
    parts = SC.partition_files(a.feat_dir)
    assert len(parts) == 27, len(parts)
    assert s3_empty(SCORES_S3), f"{SCORES_S3} is not empty; refusing to write"
    t0 = time.time()
    keys, scores, sizes = SC.score_all(parts, stats, model)
    print(f"scored {len(scores):,} rows in {time.time()-t0:.0f}s", flush=True)
    rank = SC.global_rank(keys, scores)
    assert len(rank) == 31_251_483 and rank.min() == 1 and rank.max() == len(rank)
    assert len(np.unique(rank)) == len(rank), "risk_rank not unique"
    print("rank check ok: dense and unique", flush=True)
    out = os.path.join(a.root, "scores_out")
    total = SC.write_partitions(parts, sizes, keys, scores, rank, cfg, out, "xgboost", MODEL_VERSION)
    assert total == 31_251_483
    sh("aws", "s3", "sync", out, SCORES_S3, "--only-show-errors")
    print(f"score stage done in {time.time()-t0:.0f}s; {total:,} rows -> {SCORES_S3}", flush=True)
    sh("aws", "s3", "ls", SCORES_S3, "--recursive", "--summarize", "--human-readable")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["split", "train", "score"])
    ap.add_argument("--root", default=os.path.expanduser("~/SageMaker/bq1_work"))
    a = ap.parse_args()
    a.feat_dir, a.out_dir = os.path.join(a.root, "features"), os.path.join(a.root, "artifacts")
    os.makedirs(a.out_dir, exist_ok=True)
    log = os.path.join(a.out_dir, f"epic7_{a.stage}.log")
    sys.stdout = Tee(log)
    try:
        {"split": stage_split, "train": stage_train, "score": stage_score}[a.stage](a)
    finally:
        sys.stdout.flush()
        subprocess.run(["aws", "s3", "cp", log, LOGS_S3 + os.path.basename(log), "--only-show-errors"])
