#!/usr/bin/env python3
"""ETL v2 daily lane, step 2: one day from the raw Bronze CSV -> Silver v2 audit branch + source proof + Glue DQ.

Replaces the stream-landing audit job (ETL_Final_Plan.md D1 to D4). No Kinesis, no Firehose, no bronze/stream/.

--mode audit       (state machine) parse the run day from LI-Medium_Trans.csv, prove it against the CSV (source proof),
                   MERGE it into a new audit_* branch forked from main, run the 11 DQ rules, write
                   results/etl-v2/live-audit/<run-id>.json for the quality gate. Never touches main.
                   Refuses a day that is already in main (use parse-test for those).
--mode parse-test  parse the run day the same way, run the 11 DQ rules on it, compare all 33 columns with the rows
                   already in Silver main for that day (EXCEPT ALL both ways), write
                   results/etl-v2/parse-test/<day>/<run-id>/{data/,result.json}. Never creates a branch.

The parse is a port of the v1 Bronze->Silver job (silver/ibm-aml/deployment/v20261004_18, jobs/transaction_reader.py
and jobs/silver_etl.py), which produced every row now in Silver v2:
  * the 11 fields are bound by POSITION (the header has two "Account" columns); quoted lines fail closed;
  * row_line_no = physical 1-based line in the whole file (header is line 1), numbered BEFORE the day filter
    (days are interleaved through the file, so numbering a day slice would change every id);
  * transaction_id = 'txn_' + sha256('li_medium' \\x01 'LI-Medium_Trans.csv' \\x01 row_line_no \\x01 <11 raw fields>)[:32];
  * fields trimmed; bank ids normalised by stripping leading zeros; account key = bank_norm|account;
  * amounts decimal(23,6) (v1's profiled type), rows that do not fit are rejected, never rounded;
  * timestamp 'yyyy/MM/dd HH:mm' read in a UTC session and stored as TimestampNTZ (Silver v2's type);
  * entity ids/names from LI-Medium_accounts.csv by two LEFT joins on the account key, fail closed on ambiguous keys;
  * entity flags NULL when either entity is unknown; lineage columns as v1 (ingest_run_id = medium_live_01).
"""
import csv, io, json, re, sys
from datetime import datetime, timezone, timedelta, date
from decimal import Decimal
import boto3
from pyspark.sql import SparkSession, functions as F, types as T

B = 'is459-aml-g1t5'; R = 'us-east-1'; TABLE = 'glue_catalog.is459_aml_silver_v2.tx_v2'
VARIANT = 'li_medium'; SRC_FILE = 'LI-Medium_Trans.csv'; ACCT_KEY = 'bronze/ibm-aml/li-medium/accounts/LI-Medium_accounts.csv'
P, S = 23, 6
FORMATS = ['ACH', 'Bitcoin', 'Cash', 'Cheque', 'Credit Card', 'Reinvestment', 'Wire']
COLS = ['transaction_id', 'dataset_variant', 'source_file', 'ingest_run_id', 'row_line_no', 'timestamp', 'from_bank',
        'from_bank_norm', 'from_account', 'to_bank', 'to_bank_norm', 'to_account', 'amount_received', 'receiving_currency',
        'amount_paid', 'payment_currency', 'payment_format', 'is_laundering', 'sender_account_key', 'receiver_account_key',
        'sender_entity_id', 'sender_entity_name', 'receiver_entity_id', 'receiver_entity_name', 'both_entities_resolved',
        'entity_resolution_status', 'is_same_account', 'is_same_entity', 'is_same_entity_different_account',
        'is_cross_entity', 'is_cross_bank', 'is_cross_currency', 'txn_month']
RAW = ['timestamp_raw', 'from_bank_raw', 'from_account_raw', 'to_bank_raw', 'to_account_raw', 'amount_received_raw',
       'receiving_currency_raw', 'amount_paid_raw', 'payment_currency_raw', 'payment_format_raw', 'is_laundering_raw']

def parse_line(line):
    for row in csv.reader(io.StringIO(line), skipinitialspace=False):
        return row
    return []


def norm_bank(c):
    t = F.trim(F.col(c))
    return F.when(t.rlike(r'^[0-9]+$'), F.when(F.regexp_replace(t, r'^0+', '') == '', F.lit('0'))
                  .otherwise(F.regexp_replace(t, r'^0+', ''))).otherwise(t)


def amount_fits(c):
    s = F.trim(F.col(c))
    ok = s.rlike(r'^-?\d+(\.\d+)?$')
    body = F.when(s.startswith('-'), F.substring(s, 2, 1000)).otherwise(s)
    ip = F.when(body.contains('.'), F.split(body, r'\.')[0]).otherwise(body)
    fp = F.when(body.contains('.'), F.split(body, r'\.')[1]).otherwise(F.lit(''))
    ic = F.when(ip.rlike(r'^0+$'), F.lit('0')).otherwise(F.regexp_replace(ip, r'^0+', ''))
    return ok & (F.length(ic) <= P - S) & (F.length(fp) <= S)


def parse_day(table_schema, src_uri, acct_uri):
    """CSV -> typed Silver v2 rows for DAY (33 columns, table order and types) + source proof."""
    lines = sc.textFile(src_uri)
    hdr = lines.first().strip().lower()
    if hdr.count('account') < 2 or 'timestamp' not in hdr:
        raise RuntimeError('Unexpected transaction header: %r' % hdr)
    numbered = lines.zipWithIndex().map(lambda t: (t[1] + 1, t[0])).toDF(['row_line_no', 'line']).where('row_line_no > 1')
    slash = DAY.replace('-', '/')
    whole = numbered.agg(F.count('*').alias('data_lines'),
                         F.sum(F.instr('line', '"').cast('boolean').cast('int')).alias('quoted'),
                         F.sum((~F.col('line').rlike(r'^\d{4}/\d{2}/\d{2} \d{2}:\d{2},')).cast('int')).alias('no_timestamp_prefix'),
                         F.sum((F.substring('line', 1, 10) == slash).cast('int')).alias('day_lines')).collect()[0].asDict()
    result['whole_file'] = {k: int(v or 0) for k, v in whole.items()}
    if whole['quoted'] or whole['no_timestamp_prefix']:
        raise RuntimeError('Source has quoted or undateable lines; positional parse not safe: %s' % result['whole_file'])
    day_lines = numbered.where(F.substring('line', 1, 10) == slash).cache()
    # Source proof, computed from the raw text with a plain comma split (independent of the csv parser below).
    sp = day_lines.select(F.split('line', ',').alias('f'))
    proof_rows = day_lines.count()
    paid = {r['cur']: str(r['amt']) for r in sp.groupBy(F.trim(F.col('f')[8]).alias('cur'))
            .agg(F.sum(F.trim(F.col('f')[7]).cast(T.DecimalType(38, S))).alias('amt')).collect()}
    recv = {r['cur']: str(r['amt']) for r in sp.groupBy(F.trim(F.col('f')[6]).alias('cur'))
            .agg(F.sum(F.trim(F.col('f')[5]).cast(T.DecimalType(38, S))).alias('amt')).collect()}
    result['source_proof'] = {'rows': proof_rows, 'amount_paid_by_currency': paid, 'amount_received_by_currency': recv,
                              'line_range': [int(x) for x in day_lines.agg(F.min('row_line_no'), F.max('row_line_no')).collect()[0]]}
    if proof_rows == 0:
        raise RuntimeError('No source rows for %s' % DAY)

    def split(t):
        n, line = t
        f = parse_line(line)
        return (n, len(f), *(f + [None] * 11)[:11])
    raw_schema = T.StructType([T.StructField('row_line_no', T.LongType()), T.StructField('n_fields', T.IntegerType())] +
                              [T.StructField(c, T.StringType()) for c in RAW])
    raw = spark.createDataFrame(day_lines.rdd.map(lambda r: (r['row_line_no'], r['line'])).map(split), raw_schema).cache()
    bad_width = raw.where('n_fields != 11').count()
    raw = raw.where('n_fields = 11').drop('n_fields')
    df = (raw.withColumn('ts', F.to_timestamp('timestamp_raw', 'yyyy/MM/dd HH:mm'))
          .withColumn('from_bank', F.trim('from_bank_raw')).withColumn('from_account', F.trim('from_account_raw'))
          .withColumn('to_bank', F.trim('to_bank_raw')).withColumn('to_account', F.trim('to_account_raw'))
          .withColumn('receiving_currency', F.trim('receiving_currency_raw')).withColumn('payment_currency', F.trim('payment_currency_raw'))
          .withColumn('payment_format', F.trim('payment_format_raw')).withColumn('lbl', F.trim('is_laundering_raw')))
    req = ['from_bank_raw', 'from_account_raw', 'to_bank_raw', 'to_account_raw', 'receiving_currency_raw', 'payment_currency_raw', 'payment_format_raw']
    df = df.withColumn('_rej', F.when(F.col('ts').isNull() | ~F.col('timestamp_raw').rlike(r'^\d{4}/\d{2}/\d{2} \d{2}:\d{2}$'), 'bad_timestamp')
                       .when(~amount_fits('amount_received_raw') | ~amount_fits('amount_paid_raw'), 'bad_amount')
                       .when(~F.col('lbl').isin('0', '1'), 'bad_label')
                       .when(F.col('from_bank').contains('|') | F.col('from_account').contains('|') | F.col('to_bank').contains('|') |
                             F.col('to_account').contains('|'), 'key_delimiter_violation')
                       .when(F.greatest(*[(F.length(F.trim(c)) == 0).cast('int') for c in req]) == 1, 'bad_required_field'))
    rejects = {r['_rej']: r['count'] for r in df.where('_rej is not null').groupBy('_rej').count().collect()}
    if bad_width:
        rejects['bad_field_count'] = bad_width
    result['rejects'] = rejects
    if rejects:
        raise RuntimeError('Rejected source rows for %s (v1 published none for LI-Medium): %s' % (DAY, rejects))
    id_src = F.concat_ws('\u0001', F.lit(VARIANT), F.lit(SRC_FILE), F.col('row_line_no').cast('string'), *RAW)
    df = (df.withColumn('amount_received', F.trim('amount_received_raw').cast(T.DecimalType(P, S)))
          .withColumn('amount_paid', F.trim('amount_paid_raw').cast(T.DecimalType(P, S)))
          .withColumn('is_laundering', F.col('lbl').cast('int'))
          .withColumn('txn_month', F.date_format('ts', 'yyyy-MM'))
          .withColumn('timestamp', F.col('ts').cast('timestamp_ntz'))
          .withColumn('transaction_id', F.concat(F.lit('txn_'), F.substring(F.sha2(id_src, 256), 1, 32)))
          .withColumn('from_bank_norm', norm_bank('from_bank')).withColumn('to_bank_norm', norm_bank('to_bank'))
          .withColumn('sender_account_key', F.concat_ws('|', 'from_bank_norm', 'from_account'))
          .withColumn('receiver_account_key', F.concat_ws('|', 'to_bank_norm', 'to_account')))

    # Accounts dimension, exactly as v1: positional 5 fields, trimmed, fail closed on an ambiguous key.
    a_lines = sc.textFile(acct_uri)
    a_hdr = [t.strip().lower() for t in a_lines.first().split(',')]
    if a_hdr != ['bank name', 'bank id', 'account number', 'entity id', 'entity name']:
        raise RuntimeError('Accounts header mismatch: %s' % a_hdr)
    if a_lines.filter(lambda l: '"' in l).count():
        raise RuntimeError('Accounts file has quoted lines')
    acct_rdd = a_lines.zipWithIndex().filter(lambda t: t[1] > 0).map(lambda t: parse_line(t[0])).filter(lambda f: len(f) == 5)
    acct = (spark.createDataFrame(acct_rdd, 'bank_name_raw string, bank_id_raw string, account_number_raw string, entity_id_raw string, entity_name_raw string')
            .select(F.trim('bank_id_raw').alias('bank_id'), F.trim('account_number_raw').alias('account_number'),
                    F.trim('entity_id_raw').alias('entity_id'), F.trim('entity_name_raw').alias('entity_name'))
            .where(~(F.col('bank_id').contains('|') | F.col('account_number').contains('|')))
            .withColumn('bank_id_norm', norm_bank('bank_id'))
            .withColumn('account_key', F.concat_ws('|', 'bank_id_norm', 'account_number')))
    amb = acct.groupBy('account_key').agg(F.countDistinct('bank_id', 'account_number', 'entity_id', 'entity_name').alias('n')).where('n > 1')
    n_amb = amb.count()
    if n_amb:
        raise RuntimeError('%d ambiguous account keys; refusing (same rule as v1)' % n_amb)
    dim = acct.select('account_key', 'entity_id', 'entity_name').distinct()
    before = df.count()
    df = (df.join(dim.select(F.col('account_key').alias('sender_account_key'), F.col('entity_id').alias('sender_entity_id'),
                             F.col('entity_name').alias('sender_entity_name')), 'sender_account_key', 'left_outer')
          .join(dim.select(F.col('account_key').alias('receiver_account_key'), F.col('entity_id').alias('receiver_entity_id'),
                           F.col('entity_name').alias('receiver_entity_name')), 'receiver_account_key', 'left_outer'))
    both = F.col('sender_entity_id').isNotNull() & F.col('receiver_entity_id').isNotNull()
    df = (df.withColumn('both_entities_resolved', both)
          .withColumn('entity_resolution_status', F.when(both, 'both')
                      .when(F.col('sender_entity_id').isNull() & F.col('receiver_entity_id').isNull(), 'both_unmatched')
                      .when(F.col('sender_entity_id').isNull(), 'sender_unmatched').otherwise('receiver_unmatched'))
          .withColumn('is_same_account', F.col('sender_account_key').eqNullSafe(F.col('receiver_account_key')))
          .withColumn('is_cross_bank', F.col('from_bank_norm') != F.col('to_bank_norm'))
          .withColumn('is_cross_currency', F.col('payment_currency') != F.col('receiving_currency'))
          .withColumn('is_same_entity', F.when(both, F.col('sender_entity_id') == F.col('receiver_entity_id')))
          .withColumn('is_same_entity_different_account', F.when(both, (F.col('sender_entity_id') == F.col('receiver_entity_id')) &
                                                                  ~F.col('sender_account_key').eqNullSafe(F.col('receiver_account_key'))))
          .withColumn('is_cross_entity', F.when(both, F.col('sender_entity_id') != F.col('receiver_entity_id')))
          .withColumn('dataset_variant', F.lit(VARIANT)).withColumn('source_file', F.lit(SRC_FILE))
          .withColumn('ingest_run_id', F.lit(INGEST_RUN_ID)))
    typed = df.select(*[F.col(f.name).cast(f.dataType).alias(f.name) for f in table_schema]).cache()
    n = typed.count()
    if n != before:
        raise RuntimeError('Row count changed in the entity joins: %d -> %d' % (before, n))
    return typed


def check_against_proof(typed):
    """Source proof gate: parsed rows vs raw CSV slice (rows, unique ids, exact per-currency totals, all on the day)."""
    pr = result['source_proof']
    st = typed.agg(F.count('*').alias('n'), F.countDistinct('transaction_id').alias('ids')).collect()[0]
    paid = {r['payment_currency']: str(r['a']) for r in typed.groupBy('payment_currency').agg(F.sum(F.col('amount_paid').cast(T.DecimalType(38, S))).alias('a')).collect()}
    recv = {r['receiving_currency']: str(r['a']) for r in typed.groupBy('receiving_currency').agg(F.sum(F.col('amount_received').cast(T.DecimalType(38, S))).alias('a')).collect()}
    d0 = F.lit(DAY).cast('timestamp_ntz'); d1 = F.lit((date.fromisoformat(DAY) + timedelta(days=1)).isoformat()).cast('timestamp_ntz')
    off_day = typed.where(F.col('timestamp').isNull() | (F.col('timestamp') < d0) | (F.col('timestamp') >= d1)).count()
    m = {'rows': st['n'] == pr['rows'], 'unique_ids': st['ids'] == pr['rows'],
         'amount_paid_by_currency': {k: Decimal(v) for k, v in paid.items()} == {k: Decimal(v) for k, v in pr['amount_paid_by_currency'].items()},
         'amount_received_by_currency': {k: Decimal(v) for k, v in recv.items()} == {k: Decimal(v) for k, v in pr['amount_received_by_currency'].items()},
         'all_on_day': off_day == 0}
    result.update(rows=int(st['n']), unique_ids=int(st['ids']), proof_checks=m, source_proof_match=all(m.values()), source_id_hash=id_hash(typed),
                  amount_paid_total=str(sum(Decimal(v) for v in paid.values())),
                  amount_received_total=str(sum(Decimal(v) for v in recv.values())))
    if not result['source_proof_match']:
        raise RuntimeError('Source proof mismatch: %s' % m)
    return int(st['n'])


def id_hash(df):
    """Same algorithm as the BQ1/BQ2 day jobs' coverage hash (bq1-coverage-v2), so they can check Silver against this proof."""
    import hashlib
    buckets = (df.select('transaction_id').withColumn('_b', F.pmod(F.xxhash64('transaction_id'), F.lit(1024)))
               .withColumn('_d', F.sha2('transaction_id', 256)).groupBy('_b')
               .agg(F.sha2(F.concat_ws('\n', F.sort_array(F.collect_list('_d'))), 256).alias('digest')).collect())
    h = hashlib.sha256(); h.update(b'bq1-coverage-v2:xxhash64:n=1024:'); seen = {x['_b']: x['digest'] for x in buckets}
    for b in range(1024):
        h.update(('%04x|%s\n' % (b, seen.get(b, ''))).encode())
    return h.hexdigest()


def run_dq(df, expected):
    from awsglue.context import GlueContext
    from awsglue.dynamicframe import DynamicFrame
    from awsgluedq.transforms import EvaluateDataQuality
    rules = ['RowCount = ' + str(expected), 'IsComplete "transaction_id"', 'IsUnique "transaction_id"', 'IsComplete "timestamp"',
             'ColumnValues "amount_paid" > 0', 'ColumnValues "amount_received" > 0', 'IsComplete "payment_format"',
             'ColumnValues "payment_format" in ' + json.dumps(FORMATS),
             'ColumnValues "sender_account_key" matches "^[^|]+[|][^|]+$"',
             'ColumnValues "receiver_account_key" matches "^[^|]+[|][^|]+$"', 'ColumnValues "_run_day" = "' + DAY + '"']
    dqdl = 'Rules = [\n' + ',\n'.join(rules) + '\n]'
    ev = df.withColumn('timestamp', F.col('timestamp').cast('timestamp')).withColumn('_run_day', F.to_date('timestamp').cast('string'))
    dyf = DynamicFrame.fromDF(ev, GlueContext(sc), 'csv_day_audit')
    dq = EvaluateDataQuality.apply(frame=dyf, ruleset=dqdl, publishing_options={
        'dataQualityEvaluationContext': args['run-id'], 'enableDataQualityCloudWatchMetrics': False, 'enableDataQualityResultsPublishing': False})
    out = [x.asDict(recursive=True) for x in dq.toDF().collect()]
    if len(out) != 11 or any(x.get('Outcome') not in ('Passed', 'Failed') for x in out):
        raise RuntimeError('Real DQ outcomes unavailable')
    result.update(glue_dq_rule_results=out, dqdl=dqdl, dq_rules_passed=sum(x['Outcome'] == 'Passed' for x in out))
    return all(x['Outcome'] == 'Passed' for x in out)


def refs():
    return {r['name']: str(r['snapshot_id']) for r in spark.sql('SELECT name, snapshot_id FROM ' + TABLE + '.refs').collect()}


def main():
    global args, MODE, DAY, INGEST_RUN_ID, src_key, s3, RESULT_KEY, OUT, result, spark, sc
    args = {}; it = iter(sys.argv[1:])
    for k in it:
        if k.startswith('--'):
            args[k[2:]] = next(it, '')
    MODE = args.get('mode', 'audit')
    for k in ('run-id', 'day', 'source-csv-key'):
        if not args.get(k):
            raise RuntimeError('Missing --' + k)
    if MODE not in ('audit', 'parse-test'):
        raise RuntimeError('Bad --mode')
    if not re.fullmatch('[a-zA-Z0-9_-]{1,100}', args['run-id']):
        raise RuntimeError('Bad --run-id')
    DAY = args['day']
    date.fromisoformat(DAY)
    if not re.fullmatch(r'2022-09-\d{2}', DAY):
        raise RuntimeError('Day outside the LI-Medium source range')
    if MODE == 'audit' and (not args.get('audit-branch', '').startswith('audit_') or not args.get('expected-main-snapshot', '').isdigit()
                            or not re.fullmatch('[a-zA-Z0-9_]{1,100}', args['audit-branch'])):
        raise RuntimeError('Audit mode needs --audit-branch audit_* and a numeric --expected-main-snapshot')
    INGEST_RUN_ID = args.get('ingest-run-id') or 'medium_live_01'
    src_key = args['source-csv-key'].lstrip('/')
    if src_key.startswith('s3://'):
        src_key = src_key.split('/', 3)[3]
    if not src_key.startswith('bronze/ibm-aml/li-medium/'):
        raise RuntimeError('Source must be under bronze/ibm-aml/li-medium/')
    s3 = boto3.client('s3', region_name=R)
    if src_key.endswith('/'):
        objs = [o['Key'] for o in s3.list_objects_v2(Bucket=B, Prefix=src_key).get('Contents', []) if o['Key'].lower().endswith('.csv')]
        if len(objs) != 1:
            raise RuntimeError('Expected exactly one CSV under %s, found %s' % (src_key, objs))
        src_key = objs[0]
    if src_key.rsplit('/', 1)[-1] != SRC_FILE:
        raise RuntimeError('transaction_id embeds the source file name; expected %s' % SRC_FILE)

    if MODE == 'audit':
        RESULT_KEY = 'results/etl-v2/live-audit/' + args['run-id'] + '.json'
    else:
        OUT = 'results/etl-v2/parse-test/%s/%s/' % (DAY, args['run-id'])
        RESULT_KEY = OUT + 'result.json'
    result = {'kind': 'silver_audit_result', 'mode': MODE, 'run_id': args['run-id'], 'day': DAY, 'status': 'RUNNING',
              'source': 's3://%s/%s' % (B, src_key), 'audit_branch': args.get('audit-branch'), 'v1_mutated': False,
              'main_mutated': False, 'started': datetime.now(timezone.utc).isoformat()}


    def save():
        s3.put_object(Bucket=B, Key=RESULT_KEY, Body=json.dumps(result, indent=2, default=str).encode(),
                      ContentType='application/json', Tagging='project=is459-g1t5-etl-v2')


    def head(key):
        h = s3.head_object(Bucket=B, Key=key)
        return {'key': key, 'etag': h['ETag'], 'size': h['ContentLength'], 'last_modified': str(h['LastModified'])}


    spark = (SparkSession.builder
             .config('spark.sql.extensions', 'org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions')
             .config('spark.sql.catalog.glue_catalog', 'org.apache.iceberg.spark.SparkCatalog')
             .config('spark.sql.catalog.glue_catalog.catalog-impl', 'org.apache.iceberg.aws.glue.GlueCatalog')
             .config('spark.sql.catalog.glue_catalog.io-impl', 'org.apache.iceberg.aws.s3.S3FileIO')
             .config('spark.sql.catalog.glue_catalog.warehouse', 's3://' + B + '/silver/v2/')
             .config('spark.sql.session.timeZone', 'UTC').config('spark.sql.legacy.timeParserPolicy', 'CORRECTED')
             .config('spark.sql.shuffle.partitions', '64').getOrCreate())
    spark.conf.set('spark.sql.session.timeZone', 'UTC'); spark.conf.set('spark.sql.legacy.timeParserPolicy', 'CORRECTED')
    sc = spark.sparkContext


    try:
        result['source_object_before'] = head(src_key); result['accounts_object_before'] = head(ACCT_KEY)
        r0 = refs(); main = r0['main']; result['main_snapshot_before'] = main; result['refs_before'] = r0
        if MODE == 'audit' and main != args['expected-main-snapshot']:
            raise RuntimeError('Main snapshot drift')
        table = spark.read.option('snapshot-id', main).table(TABLE)
        if table.columns != COLS or table.schema['timestamp'].dataType != T.TimestampNTZType():
            raise RuntimeError('Silver schema drift: %s' % table.columns)
        d0 = F.lit(DAY).cast('timestamp_ntz'); d1 = F.lit((date.fromisoformat(DAY) + timedelta(days=1)).isoformat()).cast('timestamp_ntz')
        existing = table.where((F.col('timestamp') >= d0) & (F.col('timestamp') < d1))
        n_existing = existing.count(); result['main_rows_for_day'] = n_existing

        typed = parse_day(table.schema, 's3://%s/%s' % (B, src_key), 's3://%s/%s' % (B, ACCT_KEY))
        expected = check_against_proof(typed)
        if head(src_key) != result['source_object_before'] or head(ACCT_KEY) != result['accounts_object_before']:
            raise RuntimeError('Source object changed during the run')

        if MODE == 'parse-test':
            if n_existing == 0:
                raise RuntimeError('Parse-test needs a day already in Silver main')
            typed.write.mode('errorifexists').parquet('s3://%s/%sdata/' % (B, OUT))
            ex = existing.select(*COLS).cache()
            only_parsed = typed.exceptAll(ex).cache(); only_main = ex.exceptAll(typed).cache()
            n_op, n_om = only_parsed.count(), only_main.count()
            diff_cols = {}
            if n_op or n_om:
                j = only_parsed.alias('a').join(only_main.alias('b'), 'transaction_id')
                for c in COLS[1:]:
                    k = j.where(~F.col('a.' + c).eqNullSafe(F.col('b.' + c))).count()
                    if k:
                        diff_cols[c] = k
            st = ex.agg(F.countDistinct('transaction_id').alias('ids'), F.sum('amount_paid').alias('p'), F.sum('amount_received').alias('r')).collect()[0]
            dq_pass = run_dq(typed, expected)
            r1 = refs()
            result.update(parity={'main_rows': n_existing, 'parsed_rows': expected, 'main_unique_ids': int(st['ids']),
                                  'main_amount_paid_total': str(st['p']), 'main_amount_received_total': str(st['r']),
                                  'rows_only_in_parsed': n_op, 'rows_only_in_main': n_om, 'differing_columns': diff_cols,
                                  'sample_ids_only_in_parsed': [r[0] for r in only_parsed.select('transaction_id').limit(5).collect()],
                                  'columns_compared': len(COLS)},
                          refs_after=r1, main_snapshot_after=r1['main'], data='s3://%s/%sdata/' % (B, OUT))
            result['main_mutated'] = r1 != r0
            ok = (n_existing == expected and n_op == 0 and n_om == 0 and int(st['ids']) == expected and
                  Decimal(str(st['p'])) == Decimal(result['amount_paid_total']) and Decimal(str(st['r'])) == Decimal(result['amount_received_total'])
                  and dq_pass and not result['main_mutated'])
            result['status'] = 'PASS' if ok else 'FAIL'
        else:
            if n_existing:
                raise RuntimeError('%s already has %d rows in Silver main; committed days are checked with --mode parse-test' % (DAY, n_existing))
            if args['audit-branch'] in r0:
                raise RuntimeError('Existing audit branch; no reset')
            spark.sql('ALTER TABLE %s CREATE BRANCH %s' % (TABLE, args['audit-branch']))
            audit = TABLE + '.branch_' + args['audit-branch']
            typed.createOrReplaceTempView('csv_day_rows')
            merge = 'MERGE INTO ' + audit + ' t USING csv_day_rows n ON t.transaction_id = n.transaction_id WHEN NOT MATCHED THEN INSERT *'
            spark.sql(merge)
            sel = spark.table(audit).where((F.col('timestamp') >= d0) & (F.col('timestamp') < d1)).select(*COLS).cache()
            if sel.count() != expected or sel.exceptAll(typed).limit(1).count() or typed.exceptAll(sel).limit(1).count():
                raise RuntimeError('Audit branch readback differs from the parsed day')
            spark.sql(merge); spark.catalog.refreshTable(audit)
            after = spark.table(audit).where((F.col('timestamp') >= d0) & (F.col('timestamp') < d1)).select(*COLS)
            if after.count() != expected or after.exceptAll(typed).limit(1).count():
                raise RuntimeError('Repeated MERGE changed content')
            branch_total = spark.table(audit).count()
            if branch_total != table.count() + expected:
                raise RuntimeError('Branch total %d != main + day' % branch_total)
            dq_pass = run_dq(sel, expected)
            r1 = refs()
            if r1['main'] != main:
                raise RuntimeError('Main changed during audit')
            result.update(status='PASS' if dq_pass else 'FAIL', main_snapshot_after=r1['main'], audit_snapshot_id=r1[args['audit-branch']],
                          audit_load_rows=expected, audit_rows_after_repeat=expected, branch_total_rows=branch_total)
        result['completed'] = datetime.now(timezone.utc).isoformat()
        save(); print(json.dumps(result, indent=2, default=str), flush=True)
        if result['status'] != 'PASS':
            raise RuntimeError('%s did not pass' % MODE)
    except Exception as ex:
        if result['status'] not in ('FAIL',):
            result['status'] = 'ERROR'
        result['error'] = repr(ex)[:2000]
        try:
            save()
        except Exception as sx:
            print('RESULT_SAVE_FAILED', repr(sx), flush=True)
        raise
    finally:
        spark.stop()


if __name__ == '__main__':
    main()
