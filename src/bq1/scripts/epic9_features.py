"""Epic 9 T9.7: descriptive feature comparison, tail vs test, positives vs negatives (read-only Athena joins).
Uses is459_aml_gold.bq1_risk_scores_medium_v1 joined to bq1_features_medium_v1 on transaction_id."""
import time
import boto3, pandas as pd

ath = boto3.client("athena", region_name="us-east-1")
OUT = "artifacts/medium_tail/"
S, F = "is459_aml_gold.bq1_risk_scores_medium_v1", "is459_aml_gold.bq1_features_medium_v1"
GRP = ("CASE WHEN s.data_split = 'excluded' THEN 'tail' ELSE 'test' END || CASE WHEN s.evaluation_is_laundering = 1 "
       "THEN ' positives' ELSE ' negatives' END")


def q(sql):
    qid = ath.start_query_execution(QueryString=sql, WorkGroup="primary", ResultConfiguration={
        "OutputLocation": "s3://is459-aml-g1t5/gold/bq1/athena-results/"})["QueryExecutionId"]
    while True:
        st = ath.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]
        if st["State"] not in ("QUEUED", "RUNNING"):
            break
        time.sleep(2)
    assert st["State"] == "SUCCEEDED", st
    rows = [[c.get("VarCharValue") for c in r["Data"]] for r in ath.get_query_results(QueryExecutionId=qid, MaxResults=1000)["ResultSet"]["Rows"]]
    return pd.DataFrame(rows[1:], columns=rows[0])


med = lambda c: f"approx_percentile(f.{c}, 0.5) AS med_{c}"
num = q(f"SELECT {GRP} AS grp, count(*) AS rows, " + ", ".join(med(c) for c in [
    "snd_out_cnt_24h", "snd_prior_txn_cnt", "pair_prior_cnt", "rcv_in_cnt_24h", "snd_distinct_rcv_to_date",
    "amt_ratio_to_snd_mean", "log_amount_paid"]) +
    ", avg(CASE WHEN f.is_new_pair THEN 1.0 ELSE 0.0 END) AS share_new_pair, "
    "avg(CASE WHEN f.is_same_account THEN 1.0 ELSE 0.0 END) AS share_same_account, "
    "avg(CASE WHEN f.is_cross_entity THEN 1.0 ELSE 0.0 END) AS share_cross_entity, "
    "avg(CASE WHEN f.is_cross_currency THEN 1.0 ELSE 0.0 END) AS share_cross_currency, "
    "avg(CASE WHEN f.payment_format = 'ACH' THEN 1.0 ELSE 0.0 END) AS share_ach "
    f"FROM {S} s JOIN {F} f ON s.transaction_id = f.transaction_id WHERE s.data_split IN ('test', 'excluded') GROUP BY 1 ORDER BY 1")
num.to_csv(OUT + "tail_feature_compare_medium.csv", index=False)
fmt = q(f"SELECT {GRP} AS grp, f.payment_format AS payment_format, count(*) AS rows "
        f"FROM {S} s JOIN {F} f ON s.transaction_id = f.transaction_id WHERE s.data_split IN ('test', 'excluded') GROUP BY 1, 2 ORDER BY 1, 3 DESC")
fmt["rows"] = fmt.rows.astype(int)
fmt["share"] = fmt.rows / fmt.groupby("grp").rows.transform("sum")
fmt.to_csv(OUT + "tail_payment_format_medium.csv", index=False)
pd.set_option("display.width", 250)
print(num.T.to_string(header=False)); print(); print(fmt.round(3).to_string(index=False))
