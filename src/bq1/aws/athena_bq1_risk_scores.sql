-- T7.8: Gold risk scores for LI-Medium (partitioned by txn_date, same layout as bq2_graph_edges_medium_v1)
CREATE EXTERNAL TABLE IF NOT EXISTS is459_aml_gold.bq1_risk_scores_medium_v1 (
  transaction_id            string,
  source_account_key        string,
  target_account_key        string,
  transaction_timestamp     timestamp,
  risk_score                double,
  risk_rank                 bigint,
  model_name                string,
  model_version             string,
  data_split                string,
  evaluation_is_laundering  int
)
PARTITIONED BY (txn_date date)
STORED AS PARQUET
LOCATION 's3://is459-aml-g1t5/gold/bq1/scores/li-medium-v1/'
TBLPROPERTIES ('parquet.compression'='SNAPPY');

-- then: MSCK REPAIR TABLE is459_aml_gold.bq1_risk_scores_medium_v1;
