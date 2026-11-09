"""Local check of silver_audit_dq_job.parse_day + check_against_proof on a synthetic CSV (no AWS).
The first three data lines are the real first lines of LI-Medium_Trans.csv; their ids are the ones in Silver v2."""
import hashlib, os, sys, tempfile
from decimal import Decimal
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import silver_audit_dq_job as J
from pyspark.sql import SparkSession, types as T

REAL = {2: 'txn_72e145315c3f6998409071f5804a42aa', 3: 'txn_54bc3b6776610c37f8e7ce91775f3930', 4: 'txn_dc23b9fac0c6b61816799e0f12eaa6c8'}
lines = ['Timestamp,From Bank,Account,To Bank,Account,Amount Received,Receiving Currency,Amount Paid,Payment Currency,Payment Format,Is Laundering',
         '2022/09/01 00:15,020,800104D70,020,800104D70,8095.07,US Dollar,8095.07,US Dollar,Reinvestment,0',
         '2022/09/01 00:18,03196,800107150,03196,800107150,7739.29,US Dollar,7739.29,US Dollar,Reinvestment,0',
         '2022/09/01 00:23,01208,80010E430,01208,80010E430,2654.22,US Dollar,2654.22,US Dollar,Reinvestment,0',
         '2022/09/17 10:05,0070,ACC1,00,ACC2,100.5,Euro,120.000001,US Dollar,Wire,1',
         '2022/09/16 23:59,1,A,2,B,5,Euro,5,Euro,Cash,0',
         '2022/09/17 00:00,0070,ACC1,0070,ACC1,1,Yuan,1,Yuan,Cheque,0',
         '2022/09/18 00:00,9,X,9,Y,2,Euro,2,Euro,ACH,0',
         '2022/09/17 23:59,00,ACC2,555,NOPE,3.25,Euro,3.25,Euro,ACH,0',
         '2022/09/17 23:59,00,ACC2,555,NOPE,3.25,Euro,3.25,Euro,ACH,0']   # genuine duplicate line: must get its own id
acct = ['Bank Name,Bank ID,Account Number,Entity ID,Entity Name',
        'B70,070,ACC1,E1,Corp #1', 'B0,0,ACC2,E1,Corp #1', 'B9,9,X,E9,Sole #9', 'dup,070,ACC1,E1,Corp #1']
d = tempfile.mkdtemp()
open(d + '/LI-Medium_Trans.csv', 'w').write('\n'.join(lines) + '\n'); open(d + '/acct.csv', 'w').write('\n'.join(acct) + '\n')

spark = (SparkSession.builder.master('local[2]').config('spark.sql.session.timeZone', 'UTC')
         .config('spark.sql.legacy.timeParserPolicy', 'CORRECTED').config('spark.ui.enabled', 'false').getOrCreate())
TYPES = {'row_line_no': T.LongType(), 'timestamp': T.TimestampNTZType(), 'amount_received': T.DecimalType(23, 6),
         'amount_paid': T.DecimalType(23, 6), 'is_laundering': T.IntegerType()}
for c in J.COLS:
    if c.startswith('is_') and c != 'is_laundering' or c == 'both_entities_resolved':
        TYPES[c] = T.BooleanType()
schema = T.StructType([T.StructField(c, TYPES.get(c, T.StringType())) for c in J.COLS])
J.spark, J.sc, J.result, J.INGEST_RUN_ID = spark, spark.sparkContext, {}, 'medium_live_01'
J.args = {'run-id': 'local-test'}

def idf(n, line):
    return 'txn_' + hashlib.sha256('\x01'.join(['li_medium', 'LI-Medium_Trans.csv', str(n)] + line.split(',')).encode()).hexdigest()[:32]

fails = []
def check(name, cond):
    print(('ok   ' if cond else 'FAIL ') + name)
    if not cond: fails.append(name)

J.DAY = '2022-09-01'
t = J.parse_day(schema, 'file://' + d + '/LI-Medium_Trans.csv', 'file://' + d + '/acct.csv')
got = {r['row_line_no']: r['transaction_id'] for r in t.collect()}
check('real ids for lines 2-4 equal Silver v2', got == REAL)

J.DAY = '2022-09-17'; J.result = {}
t = J.parse_day(schema, 'file://' + d + '/LI-Medium_Trans.csv', 'file://' + d + '/acct.csv')
n = J.check_against_proof(t)
rows = {r['row_line_no']: r.asDict() for r in t.collect()}
check('17 Sep rows = 4 (lines 5,7,9,10), numbered before filtering', sorted(rows) == [5, 7, 9, 10] and n == 4)
check('ids follow the v1 formula on whole-file line numbers', all(rows[k]['transaction_id'] == idf(k, lines[k - 1]) for k in rows))
check('duplicate lines keep separate ids', rows[9]['transaction_id'] != rows[10]['transaction_id'])
r5 = rows[5]
check('bank norm + account key', r5['from_bank'] == '0070' and r5['from_bank_norm'] == '70' and r5['to_bank_norm'] == '0'
      and r5['sender_account_key'] == '70|ACC1' and r5['receiver_account_key'] == '0|ACC2')
check('decimal(23,6) exact', r5['amount_paid'] == Decimal('120.000001') and r5['amount_received'] == Decimal('100.500000'))
check('timestamp NTZ wall clock', str(r5['timestamp']) == '2022-09-17 10:05:00')
check('entities joined, dup account row collapsed', r5['sender_entity_id'] == 'E1' and r5['receiver_entity_id'] == 'E1'
      and r5['is_same_entity'] is True and r5['is_same_entity_different_account'] is True and r5['is_cross_entity'] is False)
check('flags', r5['is_cross_bank'] is True and r5['is_cross_currency'] is True and r5['is_same_account'] is False)
r7 = rows[7]
check('same account row', r7['is_same_account'] is True and r7['is_same_entity_different_account'] is False)
r9 = rows[9]
check('unknown receiver -> NULL entity flags', r9['entity_resolution_status'] == 'receiver_unmatched' and r9['is_same_entity'] is None
      and r9['both_entities_resolved'] is False)
check('lineage', r5['dataset_variant'] == 'li_medium' and r5['source_file'] == 'LI-Medium_Trans.csv' and r5['ingest_run_id'] == 'medium_live_01'
      and r5['txn_month'] == '2022-09' and r5['is_laundering'] == 1)
check('source proof matched', J.result['source_proof_match'] and J.result['whole_file'] == {'data_lines': 9, 'quoted': 0, 'no_timestamp_prefix': 0, 'day_lines': 4})
check('per-currency totals', J.result['source_proof']['amount_paid_by_currency'] == {'US Dollar': '120.000001', 'Yuan': '1.000000', 'Euro': '6.500000'})
check('schema = 33 columns in table order', t.columns == J.COLS and len(t.columns) == 33)

# fail-closed paths
open(d + '/bad.csv', 'w').write('\n'.join(lines + ['2022/09/17 01:00,1,A,2,B,1.1234567,Euro,1,Euro,ACH,0']) + '\n')
import shutil; os.makedirs(d + '/b', exist_ok=True); shutil.copy(d + '/bad.csv', d + '/b/LI-Medium_Trans.csv')
J.result = {}
try:
    J.parse_day(schema, 'file://' + d + '/b/LI-Medium_Trans.csv', 'file://' + d + '/acct.csv'); check('7 dp amount rejected', False)
except RuntimeError as e:
    check('7 dp amount rejected (no rounding)', 'bad_amount' in str(e))
open(d + '/amb.csv', 'w').write('\n'.join(acct + ['other,70,ACC1,E2,Other']) + '\n')
J.result = {}
try:
    J.parse_day(schema, 'file://' + d + '/LI-Medium_Trans.csv', 'file://' + d + '/amb.csv'); check('ambiguous key refused', False)
except RuntimeError as e:
    check('ambiguous account key refused', 'ambiguous' in str(e))
spark.stop()
print('FAILED: %s' % fails if fails else 'ALL PASSED')
sys.exit(1 if fails else 0)
