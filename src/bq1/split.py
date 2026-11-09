"""Chronological split helpers (T3.1 to T3.4). Fit statistics use TRAIN rows only."""
import json
import numpy as np
import pandas as pd

from src.bq1.features import FEATURE_COLS, FEATURE_COLS_V2, PASSTHROUGH_CATEGORICAL, SILVER_FLAGS, feature_cols

SEED = 42
CATEGORICAL = PASSTHROUGH_CATEGORICAL
FLAGS = SILVER_FLAGS + ["is_new_pair"]
NUMERIC = [c for c in FEATURE_COLS if c not in CATEGORICAL and c not in FLAGS]
NUMERIC_V2 = [c for c in FEATURE_COLS_V2 if c not in CATEGORICAL and c not in FLAGS]


def numeric_cols(variant="v1"):
    return NUMERIC_V2 if variant == "v2" else NUMERIC
OTHER = "__other__"
LABEL_COLS = ["transaction_id", "ts", "is_laundering"]


def load_features(path, window_start_ts, window_end_ts, variant="v1", with_id=True):
    """Load feature Parquet with float32 numerics and int8 flags, restricted to [start, end).
    with_id=False skips transaction_id (large strings) for stages that do not need it."""
    cols = LABEL_COLS if with_id else [c for c in LABEL_COLS if c != "transaction_id"]
    df = pd.read_parquet(path, columns=cols + feature_cols(variant))
    df = df[(df["ts"] >= window_start_ts) & (df["ts"] < window_end_ts)].reset_index(drop=True)
    for c in numeric_cols(variant):
        df[c] = df[c].astype("float32")
    for c in FLAGS:
        df[c] = df[c].astype("int8")
    df["is_laundering"] = df["is_laundering"].astype("int8")
    return df


def find_boundaries(ts, train_frac=0.6, val_frac=0.8):
    """Timestamps at the 60th/80th row percentiles. A row goes to the later split when ts >= boundary,
    so all rows sharing a timestamp land on the same side."""
    s = np.sort(np.asarray(ts))
    n = len(s)
    return int(s[int(n * train_frac)]), int(s[int(n * val_frac)])


def assign_split(ts, t60, t80):
    ts = np.asarray(ts)
    return np.where(ts < t60, "train", np.where(ts < t80, "validation", "test"))


def split_summary(df, split):
    rows = []
    for name in ["train", "validation", "test"]:
        m = split == name
        n, pos = int(m.sum()), int(df.loc[m, "is_laundering"].sum())
        rows.append({"split": name, "rows": n, "positives": pos, "prevalence": pos / n if n else float("nan"),
                     "ts_min": int(df.loc[m, "ts"].min()), "ts_max": int(df.loc[m, "ts"].max())})
    return pd.DataFrame(rows)


def fit_train_stats(train):
    """Per-currency median of log_amount_paid, global fallback, and category lists. Train rows only."""
    med = train.groupby("payment_currency")["log_amount_paid"].median().astype("float64").to_dict()
    cats = {c: sorted(train[c].dropna().unique().tolist()) for c in CATEGORICAL}
    return {"amt_ccy_median": {k: float(v) for k, v in med.items()},
            "amt_global_median": float(train["log_amount_paid"].median()),
            "categories": cats}


def add_amt_vs_ccy_median(df, stats):
    """amt_vs_ccy_median = log_amount_paid - train median for that payment_currency (global median if unseen)."""
    m = df["payment_currency"].map(stats["amt_ccy_median"]).fillna(stats["amt_global_median"])
    df["amt_vs_ccy_median"] = (df["log_amount_paid"] - m).astype("float32")
    return df


def apply_categories(df, stats):
    """Cast categoricals to the train category list; unseen values map to OTHER."""
    for c in CATEGORICAL:
        cats = stats["categories"][c] + [OTHER]
        df[c] = pd.Categorical(df[c].where(df[c].isin(stats["categories"][c]), OTHER), categories=cats)
    return df


def feature_order(variant="v1"):
    return numeric_cols(variant) + ["amt_vs_ccy_median"] + FLAGS + CATEGORICAL


def write_json(obj, path):
    with open(path, "w") as f:
        json.dump(obj, f, indent=2, sort_keys=False)
