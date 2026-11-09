#!/usr/bin/env python3
"""IS459 ETL v2 Gold commit (Glue 5.0, Spark + Iceberg).

Promotes one replayed day from staging into the v2 Gold tables and advances the BQ1
running-state tables, so the next day can be featured incrementally.

Order: checks -> Gold writes + partitions -> state MERGEs (idempotent per load id)
-> ledger row (COMMITTED) -> readback checks -> result JSON.
Never touches v1 tables, Bronze, or the Silver main branch.
"""
import sys, json
from datetime import datetime, timezone
import boto3
from awsglue.utils import getResolvedOptions
from pyspark.sql import SparkSession, functions as F, types as T

A = {k.replace('_', '-'): v for k, v in getResolvedOptions(sys.argv, ['run-id', 'day', 'load-id', 'snapshot-id', 'bq1-run-id', 'bq2-run-id', 'load-fingerprint']).items()}
B = 'is459-aml-g1t5'; R = 'us-east-1'
SDB = 'is459_aml_silver_v2'; GDB = 'is459_aml_gold'
CAT = 'glue_catalog.' + SDB
DAY = A['day']; LID = A['load-id']; RUN = A['run-id']
RESULT_KEY = 'results/etl-v2/gold-commit/' + RUN + '.json'
REL = ['bq1_v2_state_pair', 'bq1_v2_state_sender', 'bq1_v2_state_sender_ccy']

s3 = boto3.client('s3', region_name=R); glue = boto3.client('glue', region_name=R)
res = {'kind': 'gold_commit_result', 'status': 'RUNNING', 'run_id': RUN, 'day': DAY, 'load_id': LID,
       'snapshot_id': A['snapshot-id'], 'bq1_run_id': A['bq1-run-id'], 'bq2_run_id': A['bq2-run-id'],
       'v1_mutated': False, 'started': datetime.now(timezone.utc).isoformat()}


def save():
    s3.put_object(Bucket=B, Key=RESULT_KEY, Body=json.dumps(res, indent=2, default=str).encode(),
                  ContentType='application/json', Tagging='project=is459-g1t5-etl-v2')


def get_json(key):
    return json.loads(s3.get_object(Bucket=B, Key=key)['Body'].read())


def register_partition(table, location):
    t = glue.get_table(DatabaseName=GDB, Name=table)['Table']
    sd = dict(t['StorageDescriptor']); sd['Location'] = location
    pin = {'Values': [DAY], 'StorageDescriptor': sd}
    try:
        glue.create_partition(DatabaseName=GDB, TableName=table, PartitionInput=pin)
    except glue.exceptions.AlreadyExistsException:
        glue.update_partition(DatabaseName=GDB, TableName=table, PartitionValueList=[DAY], PartitionInput=pin)


def table_cols(table):
    return [c['Name'] for c in glue.get_table(DatabaseName=GDB, Name=table)['Table']['StorageDescriptor']['Columns']]


spark = (SparkSession.builder
         .config('spark.sql.extensions', 'org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions')
         .config('spark.sql.catalog.glue_catalog', 'org.apache.iceberg.spark.SparkCatalog')
         .config('spark.sql.catalog.glue_catalog.catalog-impl', 'org.apache.iceberg.aws.glue.GlueCatalog')
         .config('spark.sql.catalog.glue_catalog.io-impl', 'org.apache.iceberg.aws.s3.S3FileIO')
         .config('spark.sql.catalog.glue_catalog.warehouse', 's3://' + B + '/silver/v2/')
         .config('spark.sql.session.timeZone', 'UTC')
         .config('spark.sql.shuffle.partitions', '48')
         .getOrCreate())
try:
    # 1. Staged inputs must be complete and for this exact day/snapshot.
    b1 = get_json('results/etl-v2/bq1/staged/' + A['bq1-run-id'] + '.json')
    b2 = get_json('results/etl-v2/bq2-staged/' + A['bq2-run-id'] + '.json')
    if b1.get('status') != 'STAGED' or b1.get('day') != DAY or str(b1.get('source_snapshot_id')) != A['snapshot-id'] or b1.get('load_id') != LID:
        raise RuntimeError('BQ1 staging result not STAGED for this day/snapshot/load')
    if b2.get('status') != 'PASS' or b2.get('day') != DAY or str(b2.get('source_snapshot_id')) != A['snapshot-id']:
        raise RuntimeError('BQ2 staging result not PASS for this day/snapshot')
    res['bq1_feature_mismatches'] = b1.get('feature_mismatches'); res['bq1_score_mismatches'] = b1.get('score_mismatches')

    # 2. Ledger guards.
    ledger = spark.table(CAT + '.bq1_v2_state_load').collect()
    if not ledger or any(x['status'] != 'COMMITTED' for x in ledger):
        raise RuntimeError('State ledger empty or has an uncommitted load')
    already = any(x['load_id'] == LID for x in ledger)
    start = int(datetime.strptime(DAY, '%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp())
    wm = max(x['watermark_ts'] for x in ledger)
    if not already and wm >= start:
        raise RuntimeError('State already at/after this day (watermark %s)' % wm)
    res['ledger_rows_before'] = int(sum(x['row_count'] for x in ledger)); res['watermark_before'] = wm

    # 3. The day's Silver rows at the published snapshot.
    full = spark.read.option('snapshot-id', A['snapshot-id']).table(CAT + '.tx_v2')
    new = (full.where(F.to_date(F.col('timestamp')) == F.lit(DAY).cast('date'))
           .select('transaction_id', 'timestamp', 'sender_account_key', 'receiver_account_key', 'payment_currency', 'amount_paid')
           .withColumn('_ts', F.unix_timestamp(F.col('timestamp').cast('timestamp_ltz'))).cache())
    n_new = new.count(); res['silver_rows'] = n_new
    if n_new != int(b1['rows_total']) or n_new != int(b2['input_rows']):
        raise RuntimeError('Silver day rows %s differ from staged BQ1 %s / BQ2 %s' % (n_new, b1['rows_total'], b2['input_rows']))

    # 4. Gold writes (partition overwrite is idempotent) + catalog partitions.
    stage1 = 's3://%s/gold/bq1/v2/staging/%s/' % (B, A['bq1-run-id'])
    outs = {
        'bq1_features_medium_v2': (spark.read.parquet(stage1 + 'features/'), 's3://%s/gold/bq1/v2/features/txn_date=%s/' % (B, DAY)),
        'bq1_risk_scores_medium_v2': (spark.read.parquet(stage1 + 'scores/'), 's3://%s/gold/bq1/v2/scores/txn_date=%s/' % (B, DAY)),
        'bq2_graph_edges_medium_v2': (spark.read.parquet(b2['staged_uri']), 's3://%s/gold/bq2/v2/graph_edges/li-medium-v2/txn_date=%s/' % (B, DAY)),
    }
    expect = {'bq1_features_medium_v2': n_new, 'bq1_risk_scores_medium_v2': n_new, 'bq2_graph_edges_medium_v2': int(b2['rows_written'])}
    res['gold'] = {}
    for table, (df, loc) in outs.items():
        cols = table_cols(table)
        missing = [c for c in cols if c not in df.columns]
        if missing:
            raise RuntimeError(table + ' staged data missing columns ' + str(missing))
        df.select(*cols).write.mode('overwrite').option('compression', 'snappy').parquet(loc)
        back = spark.read.parquet(loc).count()
        if back != expect[table]:
            raise RuntimeError('%s readback %s != expected %s' % (table, back, expect[table]))
        register_partition(table, loc)
        res['gold'][table] = {'rows': back, 'location': loc}
    save()

    # 5. Advance BQ1 running state (same aggregates as the state seed job; skipped per row if already applied).
    if not already:
        new.groupBy('sender_account_key', 'receiver_account_key').agg(F.min('_ts').cast('long').alias('first_ts'), F.count('*').cast('long').alias('pair_cnt')).createOrReplaceTempView('day_pair')
        new.groupBy('sender_account_key').agg(F.count('*').cast('long').alias('txn_cnt'), F.max('_ts').cast('long').alias('last_out_ts')).createOrReplaceTempView('day_sender')
        new.groupBy('sender_account_key', 'payment_currency').agg(F.sum(F.col('amount_paid').cast('double')).alias('amt_sum'), F.count('*').cast('long').alias('txn_cnt')).createOrReplaceTempView('day_ccy')
        L = "'" + LID + "'"
        spark.sql(f"""MERGE INTO {CAT}.bq1_v2_state_pair t USING day_pair s
            ON t.sender_account_key = s.sender_account_key AND t.receiver_account_key = s.receiver_account_key
            WHEN MATCHED AND NOT array_contains(t.applied_load_ids, {L}) THEN UPDATE SET
              t.pair_cnt = t.pair_cnt + s.pair_cnt, t.first_ts = least(t.first_ts, s.first_ts),
              t.applied_load_ids = concat(t.applied_load_ids, array({L})), t.updated_load_id = {L}
            WHEN NOT MATCHED THEN INSERT (sender_account_key, receiver_account_key, first_ts, pair_cnt, applied_load_ids, updated_load_id)
              VALUES (s.sender_account_key, s.receiver_account_key, s.first_ts, s.pair_cnt, array({L}), {L})""")
        spark.sql(f"""MERGE INTO {CAT}.bq1_v2_state_sender t USING day_sender s
            ON t.sender_account_key = s.sender_account_key
            WHEN MATCHED AND NOT array_contains(t.applied_load_ids, {L}) THEN UPDATE SET
              t.txn_cnt = t.txn_cnt + s.txn_cnt, t.last_out_ts = greatest(t.last_out_ts, s.last_out_ts),
              t.applied_load_ids = concat(t.applied_load_ids, array({L})), t.updated_load_id = {L}
            WHEN NOT MATCHED THEN INSERT (sender_account_key, txn_cnt, last_out_ts, applied_load_ids, updated_load_id)
              VALUES (s.sender_account_key, s.txn_cnt, s.last_out_ts, array({L}), {L})""")
        spark.sql(f"""MERGE INTO {CAT}.bq1_v2_state_sender_ccy t USING day_ccy s
            ON t.sender_account_key = s.sender_account_key AND t.payment_currency = s.payment_currency
            WHEN MATCHED AND NOT array_contains(t.applied_load_ids, {L}) THEN UPDATE SET
              t.amt_sum = t.amt_sum + s.amt_sum, t.txn_cnt = t.txn_cnt + s.txn_cnt,
              t.applied_load_ids = concat(t.applied_load_ids, array({L})), t.updated_load_id = {L}
            WHEN NOT MATCHED THEN INSERT (sender_account_key, payment_currency, amt_sum, txn_cnt, applied_load_ids, updated_load_id)
              VALUES (s.sender_account_key, s.payment_currency, s.amt_sum, s.txn_cnt, array({L}), {L})""")
        b = new.agg(F.min('_ts').alias('mn'), F.max('_ts').alias('mx')).first()
        fields = [T.StructField('load_id', T.StringType(), False), T.StructField('watermark_ts', T.LongType(), False),
                  T.StructField('min_ts', T.LongType(), False), T.StructField('max_ts', T.LongType(), False),
                  T.StructField('row_count', T.LongType(), False), T.StructField('row_sha256', T.StringType(), False),
                  T.StructField('relations_done', T.StringType(), False), T.StructField('status', T.StringType(), False),
                  T.StructField('processed_at_utc', T.StringType(), False)]
        row = (LID, int(b['mx']), int(b['mn']), int(b['mx']), int(n_new), A['load-fingerprint'], ','.join(REL), 'COMMITTED', datetime.now(timezone.utc).isoformat())
        spark.createDataFrame([row], T.StructType(fields)).writeTo(CAT + '.bq1_v2_state_load').append()
    res['state_advanced'] = not already

    # 6. Readback: every state table must account for exactly the ledger's rows.
    total = int(spark.table(CAT + '.bq1_v2_state_load').agg(F.sum('row_count')).first()[0])
    sums = {
        'bq1_v2_state_pair': int(spark.table(CAT + '.bq1_v2_state_pair').agg(F.sum('pair_cnt')).first()[0]),
        'bq1_v2_state_sender': int(spark.table(CAT + '.bq1_v2_state_sender').agg(F.sum('txn_cnt')).first()[0]),
        'bq1_v2_state_sender_ccy': int(spark.table(CAT + '.bq1_v2_state_sender_ccy').agg(F.sum('txn_cnt')).first()[0]),
    }
    res['ledger_rows_after'] = total; res['state_row_sums'] = sums
    if any(v != total for v in sums.values()):
        raise RuntimeError('State sums %s do not match ledger total %s' % (sums, total))
    if total != res['ledger_rows_before'] + (0 if already else n_new):
        raise RuntimeError('Ledger total did not advance by the day rows')
    res['status'] = 'COMMITTED'; res['completed'] = datetime.now(timezone.utc).isoformat(); save()
    print(json.dumps(res, default=str))
except Exception as ex:
    res['status'] = 'ERROR'; res['error'] = repr(ex)
    try:
        save()
    finally:
        raise
finally:
    spark.stop()
