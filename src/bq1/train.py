"""Rules baseline, logistic regression, XGBoost and the leakage audit (Epic 4). Everything is fit on TRAIN only."""
import math
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import FunctionTransformer, OneHotEncoder, StandardScaler

from src.bq1.split import CATEGORICAL, FLAGS, NUMERIC, SEED, feature_order

LABEL = "is_laundering"

# nonnegative count / amount / time-gap features that get log1p before scaling (T4.2)
LOG_COLS = ["snd_out_cnt_1h", "snd_out_cnt_24h", "snd_out_amt_24h_ccy", "rcv_in_cnt_1h", "rcv_in_cnt_24h",
            "rcv_in_amt_24h_ccy", "pair_prior_cnt", "snd_distinct_rcv_to_date", "rcv_distinct_snd_to_date",
            "snd_prior_txn_cnt", "snd_prior_mean_amt_ccy", "amt_ratio_to_snd_mean", "snd_secs_since_last_out"]
PLAIN_NUM = ["hour_of_day", "day_of_week", "log_amount_paid", "amt_vs_ccy_median"]
LOG_COLS_V2 = ["snd_in_cnt_24h", "snd_secs_since_last_in", "rcv_out_cnt_24h"]


# ---------------------------------------------------------------- T4.1 rules
def fit_rules(train, min_support=1000):
    base = float(train[LABEL].mean())
    g = train.groupby("payment_format", observed=True)[LABEL].agg(["mean", "size"])
    top_fmt = str(g[g["size"] >= min_support]["mean"].idxmax())
    params = {"top_payment_format": top_fmt,
              "snd_out_cnt_24h_p95": float(train["snd_out_cnt_24h"].quantile(0.95)),
              "rcv_distinct_snd_p95": float(train["rcv_distinct_snd_to_date"].quantile(0.95)),
              "amt_ratio_min": 3.0}
    ind = rule_indicators(train, params)
    weights, detail = {}, {}
    for name, col in ind.items():
        n, pos = int(col.sum()), int(train.loc[col, LABEL].sum())
        rate = pos / n if n else 0.0
        w = max(0.0, math.log(rate / base)) if rate > 0 else 0.0
        weights[name] = w
        detail[name] = {"train_rows": n, "train_positives": pos, "train_rate": rate, "lift": rate / base if base else None}
    return {"base_rate": base, "params": params, "weights": weights, "detail": detail}


def rule_indicators(df, p):
    return {
        f"payment_format == {p['top_payment_format']}": (df["payment_format"].astype(str) == p["top_payment_format"]),
        "is_new_pair": df["is_new_pair"] == 1,
        "is_cross_currency": df["is_cross_currency"] == 1,
        "snd_out_cnt_24h >= train p95": df["snd_out_cnt_24h"] >= p["snd_out_cnt_24h_p95"],
        "amt_ratio_to_snd_mean >= 3": df["amt_ratio_to_snd_mean"].fillna(-1) >= p["amt_ratio_min"],
        "rcv_distinct_snd_to_date >= train p95": df["rcv_distinct_snd_to_date"] >= p["rcv_distinct_snd_p95"],
    }


def score_rules(df, rules):
    ind = rule_indicators(df, rules["params"])
    s = np.zeros(len(df), dtype="float64")
    for name, col in ind.items():
        s += rules["weights"][name] * col.to_numpy()
    return s


# ---------------------------------------------------------------- T4.2 logistic regression
def build_lr(variant="v1"):
    log_cols = LOG_COLS + (LOG_COLS_V2 if variant == "v2" else [])
    log_pipe = Pipeline([("log1p", FunctionTransformer(np.log1p, feature_names_out="one-to-one")),
                         ("imp", SimpleImputer(strategy="median", add_indicator=True)),
                         ("sc", StandardScaler())])
    num_pipe = Pipeline([("imp", SimpleImputer(strategy="median", add_indicator=True)), ("sc", StandardScaler())])
    pre = ColumnTransformer([("log", log_pipe, log_cols),
                             ("num", num_pipe, PLAIN_NUM + FLAGS),
                             ("cat", OneHotEncoder(handle_unknown="ignore", sparse_output=False), CATEGORICAL)])
    return Pipeline([("pre", pre),
                     ("lr", LogisticRegression(class_weight="balanced", max_iter=1000, random_state=SEED))])


def fit_lr(train, neg_frac=0.05, variant="v1"):
    pos = train[train[LABEL] == 1]
    neg = train[train[LABEL] == 0].sample(frac=neg_frac, random_state=SEED)
    s = pd.concat([pos, neg])
    model = build_lr(variant).fit(s[feature_order(variant)], s[LABEL])
    return model, {"train_positives": len(pos), "train_negatives_sampled": len(neg)}


def lr_coefficients(model):
    names = model.named_steps["pre"].get_feature_names_out()
    return (pd.DataFrame({"feature": names, "coef": model.named_steps["lr"].coef_[0]})
            .assign(abs_coef=lambda d: d["coef"].abs()).sort_values("abs_coef", ascending=False))


# ---------------------------------------------------------------- T4.3 XGBoost
XGB_DEFAULTS = dict(tree_method="hist", enable_categorical=True, max_depth=6, learning_rate=0.1, subsample=0.8,
                    colsample_bytree=0.8, eval_metric="aucpr", n_estimators=2000, early_stopping_rounds=50,
                    random_state=SEED)


def fit_xgb(train, val, spw, n_jobs=8, features=None, **overrides):
    import xgboost as xgb
    f = features or feature_order()
    params = {**XGB_DEFAULTS, **overrides}
    m = xgb.XGBClassifier(scale_pos_weight=spw, n_jobs=n_jobs, **params)
    m.fit(train[f], train[LABEL], eval_set=[(val[f], val[LABEL])], verbose=False)
    val_score = m.predict_proba(val[f])[:, 1]
    return m, float(average_precision_score(val[LABEL], val_score)), val_score


def xgb_importance(model, top=20):
    gain = model.get_booster().get_score(importance_type="gain")
    s = pd.Series(gain, name="gain").sort_values(ascending=False)
    return s.head(top).rename_axis("feature").reset_index().assign(gain_share=lambda d: d["gain"] / s.sum())


# ---------------------------------------------------------------- T4.5 leakage audit
def single_feature_prauc(train, test, feat):
    y = test[LABEL].to_numpy()
    if feat in CATEGORICAL:
        enc = train.groupby(feat, observed=True)[LABEL].mean()
        s = test[feat].astype("object").map(enc).astype(float).fillna(float(train[LABEL].mean())).to_numpy()
    else:
        x = test[feat].astype("float64")
        s = x.fillna(x.min() - 1).to_numpy()
    # direction-agnostic: take the better of x and -x
    return float(max(average_precision_score(y, s), average_precision_score(y, -s)))
