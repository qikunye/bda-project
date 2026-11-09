"""Chunked batch scoring (T7.7): score one txn_date partition at a time, then assign a global dense risk_rank.

Pass 1 scores every partition and keeps only (transaction_id, risk_score) in memory. The global rank sorts by
score descending, ties by transaction_id ascending (same rule as bq1/metrics.py). Pass 2 re-reads each partition for
the key columns, attaches rank/score and writes one Snappy Parquet folder per txn_date.
"""
import glob
import os

import numpy as np
import pandas as pd

from src.bq1 import split as S
from src.bq1.features import feature_cols

KEY_READ = ["timestamp", "sender_account_key", "receiver_account_key"]


def partition_files(feature_dir):
    """Sorted (txn_date, file) pairs from a Hive layout <dir>/txn_date=YYYY-MM-DD/*.parquet."""
    out = []
    for d in sorted(glob.glob(os.path.join(feature_dir, "txn_date=*"))):
        files = sorted(glob.glob(os.path.join(d, "*.parquet")))
        assert len(files) == 1, f"expected one parquet in {d}, got {len(files)}"
        out.append((os.path.basename(d).split("=", 1)[1], files[0]))
    return out


def load_partition(path, stats, columns, variant="v1"):
    """One partition with float32 numerics, train-fitted amt_vs_ccy_median and categories applied."""
    df = pd.read_parquet(path, columns=columns)
    for c in S.numeric_cols(variant):
        df[c] = df[c].astype("float32")
    for c in S.FLAGS:
        df[c] = df[c].astype("int8")
    return S.apply_categories(S.add_amt_vs_ccy_median(df, stats), stats)


def stats_from_schema(schema):
    """Rebuild the train-fitted stats dict from feature_schema.json."""
    m = schema["amt_vs_ccy_median"]
    return {"amt_ccy_median": m["per_currency_median"], "amt_global_median": m["global_median"],
            "categories": {c: [v for v in schema["category_lists"][c] if v != S.OTHER] for c in S.CATEGORICAL}}


def id_keys(ids):
    """transaction_id is 'txn_' + 32 lowercase hex chars, so string order == numeric order of (hi, lo) uint64 pairs.
    Using them as the tie-break key avoids ranking 31M strings."""
    s = pd.Series(ids)
    assert s.str.fullmatch(r"txn_[0-9a-f]{32}").all(), "unexpected transaction_id format"
    hi = s.str.slice(4, 20).map(lambda h: int(h, 16)).to_numpy(dtype="uint64")
    lo = s.str.slice(20, 36).map(lambda h: int(h, 16)).to_numpy(dtype="uint64")
    return hi, lo


def score_all(parts, stats, model, variant="v1"):
    """Pass 1. Returns a frame with transaction_id, risk_score and the partition offsets (parts order)."""
    f = S.feature_order(variant)
    cols = ["transaction_id", "ts", "is_laundering"] + feature_cols(variant)
    his, los, scores, sizes = [], [], [], []
    for d, path in parts:
        df = load_partition(path, stats, cols, variant)
        hi, lo = id_keys(df["transaction_id"])
        his.append(hi)
        los.append(lo)
        scores.append(model.predict_proba(df[f])[:, 1].astype("float64"))
        sizes.append(len(df))
        print(f"  scored {d}: {len(df):,} rows", flush=True)
    return (np.concatenate(his), np.concatenate(los)), np.concatenate(scores), np.array(sizes)


def global_rank(keys, scores):
    """Dense unique rank, 1 = highest score; ties broken by transaction_id ascending (keys from id_keys)."""
    hi, lo = keys
    order = np.lexsort((lo, hi, -np.asarray(scores, dtype="float64")))
    rank = np.empty(len(order), dtype="int64")
    rank[order] = np.arange(1, len(order) + 1, dtype="int64")
    return rank


def write_partitions(parts, sizes, keys, scores, rank, cfg, out_dir, model_name, model_version):
    """Pass 2. Writes <out_dir>/txn_date=<d>/part-0.parquet. Returns total rows written."""
    off, total = 0, 0
    for (d, path), n in zip(parts, sizes):
        df = pd.read_parquet(path, columns=["transaction_id", "ts", "is_laundering"] + KEY_READ)
        hi, lo = id_keys(df["transaction_id"])
        assert len(df) == n and (hi == keys[0][off:off + n]).all() and (lo == keys[1][off:off + n]).all(), d
        ts = df["ts"].to_numpy()
        split = np.where((ts < cfg["window_start_ts"]) | (ts >= cfg["window_end_ts"]), "excluded",
                         S.assign_split(ts, cfg["t60_ts"], cfg["t80_ts"]))
        out = pd.DataFrame({
            "transaction_id": df["transaction_id"], "source_account_key": df["sender_account_key"],
            "target_account_key": df["receiver_account_key"], "transaction_timestamp": df["timestamp"].astype("datetime64[us]"),
            "risk_score": scores[off:off + n], "risk_rank": rank[off:off + n],
            "model_name": model_name, "model_version": model_version, "data_split": split,
            "evaluation_is_laundering": df["is_laundering"].astype("int32")})
        target = os.path.join(out_dir, f"txn_date={d}")
        os.makedirs(target, exist_ok=True)
        out.to_parquet(os.path.join(target, "part-0.parquet"), compression="snappy", index=False)
        off += n
        total += n
        print(f"  wrote {d}: {n:,} rows", flush=True)
    return total
