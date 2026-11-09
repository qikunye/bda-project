"""recall_at_k, precision_at_k, fp_per_tp_at_k, summary table.

Rank by score descending, ties broken by transaction_id ascending (passed as an integer `tie` rank so the
ordering is deterministic and cheap). K is a fraction of the rows being ranked (0.01 = 1%).
"""
import math
import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score

K_GRID = [0.001, 0.005, 0.01, 0.05, 0.10]


def tie_rank(transaction_ids):
    """Integer rank of transaction_id (ascending) for use as the tie-breaker."""
    return pd.Series(np.asarray(transaction_ids)).rank(method="first").astype("int64").to_numpy()


def rank_order(score, tie):
    """Indices that sort rows by score descending, then transaction_id ascending."""
    return np.lexsort((np.asarray(tie), -np.asarray(score, dtype="float64")))


def _topk(y, score, tie, k):
    n = len(y)
    m = max(1, int(math.ceil(k * n)))
    order = rank_order(score, tie)[:m]
    return m, int(np.asarray(y)[order].sum())


def recall_at_k(y, score, tie, k):
    total = int(np.asarray(y).sum())
    _, tp = _topk(y, score, tie, k)
    return tp / total if total else float("nan")


def precision_at_k(y, score, tie, k):
    m, tp = _topk(y, score, tie, k)
    return tp / m


def fp_per_tp_at_k(y, score, tie, k):
    m, tp = _topk(y, score, tie, k)
    return (m - tp) / tp if tp else float("inf")


def pr_auc(y, score):
    return float(average_precision_score(y, score))


def summary_table(y, score, tie, name, ks=K_GRID):
    n = len(y)
    rows = []
    for k in ks:
        m, tp = _topk(y, score, tie, k)
        total = int(np.asarray(y).sum())
        rows.append({"model": name, "K": k, "alerts_reviewed": m, "tp": tp,
                     "recall": tp / total if total else float("nan"), "precision": tp / m,
                     "alert_reduction": 1 - k, "fp_per_tp": (m - tp) / tp if tp else float("inf")})
    return pd.DataFrame(rows)
