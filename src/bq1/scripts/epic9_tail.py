"""Epic 9 (T9.2 to T9.6): how the XGBoost scores rank the excluded tail-day rows of LI-Medium.

All ranking arithmetic runs inside Athena over is459_aml_gold.bq1_risk_scores_medium_v1 and only small result tables
come back (a 440 MB score download was too slow). Rank rule everywhere: risk_score descending, ties by transaction_id
ascending, the same as bq1/metrics.py. `evaluation_is_laundering` is used only to evaluate.
Nothing is changed in the split, the models or the headline numbers. Outputs go to artifacts/medium_tail/.

  .venv/bin/python scripts/epic9_tail.py
"""
import json, math, sys, time
sys.path.insert(0, ".")
import boto3, numpy as np, pandas as pd
from src.bq1 import metrics as M

TBL = "is459_aml_gold.bq1_risk_scores_medium_v1"
OUT = "artifacts/medium_tail/"
RESULTS = "s3://is459-aml-g1t5/gold/bq1/athena-results/"
K_GRID = M.K_GRID
K_TAIL = [0.01, 0.05, 0.10, 0.25, 0.50]
ath = boto3.client("athena", region_name="us-east-1")


def q(sql):
    """Run one Athena query and return rows as a list of dicts of strings."""
    qid = ath.start_query_execution(QueryString=sql, WorkGroup="primary",
                                    ResultConfiguration={"OutputLocation": RESULTS})["QueryExecutionId"]
    while True:
        st = ath.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]
        if st["State"] not in ("QUEUED", "RUNNING"):
            break
        time.sleep(2)
    if st["State"] != "SUCCEEDED":
        raise RuntimeError(f"{st['State']}: {st.get('StateChangeReason')}\n{sql}")
    rows, tok = [], None
    while True:
        kw = {"QueryExecutionId": qid, "MaxResults": 1000, **({"NextToken": tok} if tok else {})}
        r = ath.get_query_results(**kw)
        rows += [[c.get("VarCharValue") for c in x["Data"]] for x in r["ResultSet"]["Rows"]]
        tok = r.get("NextToken")
        if not tok:
            break
    head, body = rows[0], rows[1:]
    return [dict(zip(head, b)) for b in body]


def values(pairs):
    return ", ".join(f"({k}, {m})" for k, m in pairs)


# ---------------------------------------------------------------- counts and sanity (T9.1 again, plus test size)
c = {r["data_split"]: r for r in q(f"SELECT data_split, count(*) AS n, sum(evaluation_is_laundering) AS p FROM {TBL} "
                                    f"WHERE data_split IN ('test','excluded') GROUP BY 1")}
n_te, p_te, n_tl, p_tl = int(c["test"]["n"]), int(c["test"]["p"]), int(c["excluded"]["n"]), int(c["excluded"]["p"])
print(f"test {n_te:,} rows / {p_te:,} pos | tail {n_tl:,} rows / {p_tl:,} pos (rate {p_tl/n_tl:.4f})", flush=True)
assert (n_te, p_te, n_tl, p_tl) == (6_250_304, 3_013, 1_370, 771)

# ---------------------------------------------------------------- T9.2 thresholds from the test queue
ms = [(k, int(math.ceil(k * n_te))) for k in K_GRID]
RANKED_TEST = (f"r AS (SELECT risk_score, row_number() OVER (ORDER BY risk_score DESC, transaction_id) AS rn "
               f"FROM {TBL} WHERE data_split = 'test')")
thr = q(f"WITH {RANKED_TEST} SELECT v.k AS k, v.m AS m, r.rn AS rn, r.risk_score AS score "
        f"FROM r JOIN (VALUES {values(ms)}) AS v(k, m) ON r.rn IN (v.m, v.m + 1) ORDER BY v.k, r.rn")
by = {}
for row in thr:
    by.setdefault(float(row["k"]), {})[int(row["rn"])] = float(row["score"])
THR = (f"WITH {RANKED_TEST}, thr AS (SELECT v.k AS k, v.m AS m, r.risk_score AS thr "
       f"FROM r JOIN (VALUES {values(ms)}) AS v(k, m) ON r.rn = v.m)")
ge = {float(r["k"]): int(r["n_ge"]) for r in q(
    f"{THR} SELECT thr.k AS k, count_if(x.risk_score >= thr.thr) AS n_ge FROM thr CROSS JOIN "
    f"(SELECT risk_score FROM {TBL} WHERE data_split = 'test') x GROUP BY thr.k")}
rows = []
for k, m in ms:
    s = by[k]
    rows.append({"K": k, "m_alerts": m, "threshold": s[m], "next_score_tied": s[m + 1] == s[m], "test_rows_ge_threshold": ge[k]})
t2 = pd.DataFrame(rows)
t2.to_csv(OUT + "tail_thresholds_medium.csv", index=False)
print("\nT9.2 thresholds\n" + t2.to_string(index=False), flush=True)
assert t2.loc[t2.K == 0.01, "m_alerts"].iloc[0] == 62_504

# ---------------------------------------------------------------- T9.3 tail rows against the test queue (threshold taken inside SQL)
r3 = q(f"{THR} SELECT thr.k AS k, count_if(x.risk_score >= thr.thr) AS flagged, "
       f"sum(CASE WHEN x.risk_score >= thr.thr THEN x.y ELSE 0 END) AS flagged_pos FROM thr CROSS JOIN "
       f"(SELECT risk_score, evaluation_is_laundering AS y FROM {TBL} WHERE data_split = 'excluded') x GROUP BY thr.k ORDER BY thr.k")
rows = []
for r in r3:
    k, fr, fp = float(r["k"]), int(r["flagged"]), int(r["flagged_pos"])
    rows.append({"K": k, "test_threshold": float(t2.loc[t2.K == k, "threshold"].iloc[0]), "tail_rows_flagged": fr,
                 "tail_positives_flagged": fp, "tail_recall": fp / p_tl, "tail_precision": fp / fr if fr else float("nan"),
                 "tail_flag_rate": fr / n_tl, "test_flag_rate": k})
t3 = pd.DataFrame(rows)
t3.to_csv(OUT + "tail_vs_test_queue_medium.csv", index=False)
print("\nT9.3 tail vs test queue\n" + t3.round(4).to_string(index=False), flush=True)

# ---------------------------------------------------------------- the 1,370 tail rows (small) for T9.4 and T9.5
tail = pd.DataFrame(q(f"SELECT transaction_id, risk_score, evaluation_is_laundering AS y, txn_date FROM {TBL} "
                      f"WHERE data_split = 'excluded'"))
tail["risk_score"], tail["y"] = tail.risk_score.astype("float64"), tail.y.astype(int)
tail.to_csv(OUT + "tail_rows_scores_medium.csv", index=False)
assert len(tail) == n_tl and int(tail.y.sum()) == p_tl
s_tl, y_tl, tie_tl = tail.risk_score.to_numpy(), tail.y.to_numpy(), M.tie_rank(tail.transaction_id)

# ---------------------------------------------------------------- T9.4 ranking within the tail set
prev_tl, rows = p_tl / n_tl, []
for k in K_TAIL:
    m, tp = M._topk(y_tl, s_tl, tie_tl, k)
    rows.append({"K_of_tail_rows": k, "rows_reviewed": m, "tp": tp, "recall": tp / p_tl, "precision": tp / m,
                 "lift_over_prevalence": (tp / m) / prev_tl, "random_recall": k, "random_precision": prev_tl})
t4 = pd.DataFrame(rows)
t4["tail_pr_auc"], t4["tail_prevalence"] = M.pr_auc(y_tl, s_tl), prev_tl
t4.to_csv(OUT + "tail_within_medium.csv", index=False)
print("\nT9.4 within tail\n" + t4.round(4).to_string(index=False), flush=True)

# ---------------------------------------------------------------- T9.5 score distributions (tail exact; test via approx_percentile)
def qs(x):
    v = np.quantile(x, [0, 0.25, 0.5, 0.75, 0.95, 1.0])
    return dict(zip(["min", "p25", "median", "p75", "p95", "max"], v))
rows = [{"group": "tail positives", "rows": int((y_tl == 1).sum()), "method": "exact", **qs(s_tl[y_tl == 1])},
        {"group": "tail negatives", "rows": int((y_tl == 0).sum()), "method": "exact", **qs(s_tl[y_tl == 0])}]
PCT = ", ".join(f"{i / 100:.2f}" for i in range(101))
curves = {"tail positives": np.quantile(s_tl[y_tl == 1], np.linspace(0, 1, 101)),
          "tail negatives": np.quantile(s_tl[y_tl == 0], np.linspace(0, 1, 101))}
for r in q(f"SELECT evaluation_is_laundering AS y, count(*) AS n, min(risk_score) AS mn, max(risk_score) AS mx, "
           f"approx_percentile(risk_score, ARRAY[0.25, 0.5, 0.75, 0.95]) AS pq, "
           f"approx_percentile(risk_score, ARRAY[{PCT}]) AS pc FROM {TBL} WHERE data_split = 'test' GROUP BY 1"):
    g = "test positives" if r["y"] == "1" else "test negatives"
    pq = [float(v) for v in r["pq"].strip("[]").split(",")]
    rows.append({"group": g, "rows": int(r["n"]), "method": "min/max exact; quartiles and p95 approximate",
                 "min": float(r["mn"]), "p25": pq[0], "median": pq[1], "p75": pq[2], "p95": pq[3], "max": float(r["mx"])})
    curves[g] = np.array([float(v) for v in r["pc"].strip("[]").split(",")])
t5 = pd.DataFrame(rows)
t5.to_csv(OUT + "tail_score_summary_medium.csv", index=False)
pd.DataFrame(curves).rename_axis("percentile").to_csv(OUT + "tail_score_percentiles_medium.csv")
print("\nT9.5 score summary\n" + t5.to_string(index=False, float_format=lambda v: f"{v:.3g}"), flush=True)

# ---------------------------------------------------------------- T9.6 headline with and without the tail
def pooled(where, label):
    ms_ = [(k, int(math.ceil(k * (n_te + n_tl if "excluded" in where else n_te)))) for k in K_GRID]
    tps = ", ".join(f"sum(CASE WHEN rn <= {m} THEN y ELSE 0 END) AS tp{i}" for i, (_, m) in enumerate(ms_))
    r = q(f"WITH p AS (SELECT transaction_id, risk_score, evaluation_is_laundering AS y FROM {TBL} WHERE data_split IN {where}), "
          f"ranked AS (SELECT y, row_number() OVER (ORDER BY risk_score DESC, transaction_id) AS rn, "
          f"sum(y) OVER (ORDER BY risk_score DESC, transaction_id ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS cp FROM p) "
          f"SELECT count(*) AS n, sum(y) AS pos, {tps}, sum(CASE WHEN y = 1 THEN cast(cp AS double) / rn END) / sum(y) AS ap FROM ranked")[0]
    n, P = int(r["n"]), int(r["pos"])
    return pd.DataFrame([{"version": label, "K": k, "alerts_reviewed": m, "tp": int(r[f"tp{i}"]),
                          "recall": int(r[f"tp{i}"]) / P, "precision": int(r[f"tp{i}"]) / m, "pr_auc": float(r["ap"]),
                          "rows_ranked": n, "positives": P} for i, (k, m) in enumerate(ms_)])
t6 = pd.concat([pooled("('test')", "test only (headline)"), pooled("('test','excluded')", "test + tail pooled")])
t6.to_csv(OUT + "headline_with_tail_medium.csv", index=False)
print("\nT9.6 headline vs pooled\n" + t6.round(4).to_string(index=False), flush=True)
r1 = float(t6[(t6.version.str.startswith("test only")) & (t6.K == 0.01)].recall.iloc[0])
print(f"\ncheck: recomputed headline Recall@1% = {r1:.4f} (T7.5 logged 0.634)")
