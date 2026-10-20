-- BQ1 reasons for the agent: top three model contributions per alert.
-- Source: Owen's data/bq1_explanations_top1pct.csv (built by explanations/make_explanations.py
-- from the frozen BQ1 model). 62,504 rows = top 1% of the test queue for 14-16 Sep 2022.
-- v1 and v2 scores are identical on 14-16 Sep (checked 2026-10-21), so the file is valid for v2.
--
-- Upload:
--   aws s3 cp bq1_explanations_top1pct.csv \
--     s3://is459-aml-g1t5/gold/bq1/v2/explanations/bq1_explanations_top1pct.csv
--
-- OpenCSVSerde is needed because some reason sentences contain quoted commas
-- ("Sender has paid 1,613 different accounts so far"). It reads every column as string;
-- cast contribution_N to double when querying.
-- These are model explanations, not evidence of laundering.

CREATE EXTERNAL TABLE IF NOT EXISTS is459_aml_gold.bq1_explanations_top1pct_v2 (
  transaction_id string,
  feature_1 string,
  reason_1 string,
  contribution_1 string,
  feature_2 string,
  reason_2 string,
  contribution_2 string,
  feature_3 string,
  reason_3 string,
  contribution_3 string
)
ROW FORMAT SERDE 'org.apache.hadoop.hive.serde2.OpenCSVSerde'
WITH SERDEPROPERTIES ('separatorChar' = ',', 'quoteChar' = '"', 'escapeChar' = '\\')
STORED AS TEXTFILE
LOCATION 's3://is459-aml-g1t5/gold/bq1/v2/explanations/'
TBLPROPERTIES ('skip.header.line.count' = '1', 'project' = 'is459-g1t5-agent');
