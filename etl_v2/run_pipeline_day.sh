#!/bin/bash
# Start one full ETL v2 pipeline run for a day read straight from the raw Bronze CSV
# (parse + source proof + DQ on an audit branch -> publish -> BQ1/BQ2 -> Gold commit). No Kinesis, no Firehose.
# Usage: ./run_pipeline_day.sh 2022-09-17 [run_tag] [source_csv_key]
#   source_csv_key: an S3 key in is459-aml-g1t5, either the CSV itself or a prefix holding exactly one CSV.
set -euo pipefail
DAY=$1; TAG=${2:-$(date -u +%Y%m%d%H%M)}; SRC=${3:-bronze/ibm-aml/li-medium/transactions/}
DC=${DAY//-/}
SM=arn:aws:states:us-east-1:941377154820:stateMachine:is459-etl-v2-silver-wap
INPUT=$(printf '{"day":"%s","day_compact":"%s","run_tag":"%s","source_csv_key":"%s"}' "$DAY" "$DC" "$TAG" "$SRC")
aws stepfunctions start-execution --state-machine-arn $SM --name "pipeline-$DC-$TAG" --input "$INPUT" --query executionArn --output text
