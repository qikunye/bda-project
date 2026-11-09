import json
import boto3

glue = boto3.client('glue')
s3 = boto3.client('s3')


def handler(event, context):
    """Return the current main-branch snapshot id of the v2 Silver Iceberg table (read-only).
    With {"refs": true} also return every branch/tag name and its snapshot id (used by parse_test.sh)."""
    loc = glue.get_table(DatabaseName='is459_aml_silver_v2', Name='tx_v2')['Table']['Parameters']['metadata_location']
    bucket, key = loc[len('s3://'):].split('/', 1)
    meta = json.loads(s3.get_object(Bucket=bucket, Key=key)['Body'].read())
    out = {'main_snapshot': str(meta['refs']['main']['snapshot-id']), 'metadata_location': loc,
           'snapshot_count': len(meta.get('snapshots', []))}
    if (event or {}).get('refs'):
        out['refs'] = {k: str(v['snapshot-id']) for k, v in sorted(meta.get('refs', {}).items())}
    return out
