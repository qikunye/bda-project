#!/usr/bin/env python3
"""IS459 ETL v2: undo the 18-27 Sep backfill (Glue 5.0, Spark + Iceberg). One-off, 8 Nov 2026.

Rolls Silver tx_v2 and the four BQ1 state/ledger tables back to their last snapshot of 24 Oct
(state after 17 Sep), refusing if any table has a snapshot the backfill did not make, then drops
the 18-27 Sep partitions from the three Gold v2 tables. Gold files stay in S3; a rerun of a day
overwrites its folder. Iceberg keeps the rolled-back snapshots, so this can itself be undone with
rollback_to_snapshot to the ids recorded in the result file.
"""
import json
from datetime import date, datetime, timedelta, timezone

import boto3
from pyspark.sql import SparkSession

B, R = 'is459-aml-g1t5', 'us-east-1'
SDB, GDB = 'is459_aml_silver_v2', 'is459_aml_gold'
BACKFILL_FROM = '2026-11-07 08:00:00'  # Backfill started 08:07 UTC; nothing else wrote after 24 Oct.
TARGETS = {  # table: last snapshot before the backfill (verified: each gives 31,250,493 rows)
    'tx_v2': 3551179523339518083,
    'bq1_v2_state_load': 7671102128983049349,
    'bq1_v2_state_pair': 6382411279535544789,
    'bq1_v2_state_sender': 1798091238118376967,
    'bq1_v2_state_sender_ccy': 4674663418078998699,
}
GOLD = ['bq1_features_medium_v2', 'bq1_risk_scores_medium_v2', 'bq2_graph_edges_medium_v2']
DAYS = [(date(2022, 9, 18) + timedelta(i)).isoformat() for i in range(10)]
KEY = 'results/etl-v2/rollback/rollback-20220918-20220927.json'

spark = (SparkSession.builder
         .config('spark.sql.extensions', 'org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions')
         .config('spark.sql.catalog.glue_catalog', 'org.apache.iceberg.spark.SparkCatalog')
         .config('spark.sql.catalog.glue_catalog.catalog-impl', 'org.apache.iceberg.aws.glue.GlueCatalog')
         .config('spark.sql.catalog.glue_catalog.io-impl', 'org.apache.iceberg.aws.s3.S3FileIO')
         .config('spark.sql.catalog.glue_catalog.warehouse', 's3://' + B + '/silver/v2/')
         .config('spark.sql.session.timeZone', 'UTC')
         .getOrCreate())
s3 = boto3.client('s3', region_name=R)
glue = boto3.client('glue', region_name=R)
res = {'kind': 'rollback_result', 'days': DAYS, 'started': datetime.now(timezone.utc).isoformat(), 'tables': {}}

# 1. Guard: every snapshot after the target must be from the backfill window.
for t, target in TARGETS.items():
    snaps = spark.sql(f"SELECT snapshot_id, committed_at FROM glue_catalog.{SDB}.{t}.snapshots ORDER BY committed_at").collect()
    ids = [s['snapshot_id'] for s in snaps]
    if target not in ids:
        raise RuntimeError(f'{t}: target snapshot {target} not found')
    after = snaps[ids.index(target) + 1:]
    bad = [str(s['committed_at']) for s in after if str(s['committed_at']) < BACKFILL_FROM]
    if bad:
        raise RuntimeError(f'{t}: snapshots after target but before the backfill: {bad}')
    current = spark.sql(f"SELECT snapshot_id FROM glue_catalog.{SDB}.{t}.history WHERE is_current_ancestor ORDER BY made_current_at DESC LIMIT 1").first()[0]
    res['tables'][t] = {'current_before': current, 'target': target, 'snapshots_undone': len(after)}

# 2. Roll back.
for t, target in TARGETS.items():
    spark.sql(f"CALL glue_catalog.system.rollback_to_snapshot('{SDB}.{t}', {target})")

# 3. Readback.
res['silver_rows_after'] = spark.table(f'glue_catalog.{SDB}.tx_v2').count()
res['silver_max_day_after'] = str(spark.sql(f"SELECT max(to_date(`timestamp`)) FROM glue_catalog.{SDB}.tx_v2").first()[0])
res['ledger_rows_after'] = int(spark.sql(f"SELECT sum(row_count) FROM glue_catalog.{SDB}.bq1_v2_state_load").first()[0])
for t, col in [('bq1_v2_state_pair', 'pair_cnt'), ('bq1_v2_state_sender', 'txn_cnt'), ('bq1_v2_state_sender_ccy', 'txn_cnt')]:
    res['tables'][t]['row_sum_after'] = int(spark.sql(f"SELECT sum({col}) FROM glue_catalog.{SDB}.{t}").first()[0])
if not (res['silver_rows_after'] == res['ledger_rows_after'] == 31250493 and res['silver_max_day_after'] == '2022-09-17'):
    res['status'] = 'CHECK_FAILED'
else:
    res['status'] = 'ROLLED_BACK'

# 4. Gold: drop the 18-27 Sep partitions (files kept).
res['gold_partitions_dropped'] = {}
for table in GOLD:
    existing = {tuple(p['Values']) for p in glue.get_partitions(DatabaseName=GDB, TableName=table)['Partitions']}
    drop = [[d] for d in DAYS if (d,) in existing]
    for i in range(0, len(drop), 25):
        glue.batch_delete_partition(DatabaseName=GDB, TableName=table,
                                    PartitionsToDelete=[{'Values': v} for v in drop[i:i + 25]])
    res['gold_partitions_dropped'][table] = [v[0] for v in drop]

res['finished'] = datetime.now(timezone.utc).isoformat()
s3.put_object(Bucket=B, Key=KEY, Body=json.dumps(res, indent=2, default=str).encode(),
              ContentType='application/json', Tagging='project=is459-g1t5-etl-v2')
print(json.dumps(res, default=str))
if res['status'] != 'ROLLED_BACK':
    raise RuntimeError('Readback failed: ' + json.dumps(res, default=str))
