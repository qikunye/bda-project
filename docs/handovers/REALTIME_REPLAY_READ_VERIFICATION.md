# Saved replay results: live read verification

Verified 8 November 2026, about **17:18 Asia/Singapore** (09:18 UTC), on
`streamlit`. This report covers the explicitly authorised read-only verification
of the real-time dashboard. It is separate from offline regression results.

## Observed live results

**Passed through the real boto3 adapters and Streamlit AppTest**, using the
existing credential chain in `us-east-1`. The source was **Saved Replay Results**,
**Transaction Date** was **2022-09-16**, and the action was **Load Results**.
The default date and updated button label were checked before loading.

The S3 listing discovered exactly these two known IDs; no static ID results or
demo records were substituted:

| Transaction ID | Alert mapping | Qwen explanation status | Displayed fields checked |
|---|---|---|---|
| `txn_3a56e8f5ad713566b162a9def3eb4d90` | Available | `ok` | Stored narrative, typology and every recommended check |
| `txn_5d363f8f9e859e450d511cb541d05fd5` | Available | `ok` | Stored narrative, typology and every recommended check |

The result table contained both transactions. Selecting each transaction rendered
its actual mapped explanation text and checks. These were comparisons against
freshly returned DTOs, not mocked values or copied historical fixtures. Raw
DynamoDB items, account identifiers and explanation contents are not included in
this report or added to test fixtures. The listing was not truncated.

Successful read operations:

| Phase | S3 ListObjectsV2 | DynamoDB GetItem |
|---|---:|---:|
| Initial Load Results | 1 | 4 |
| Second Load Results | 1 | 4 |
| Refresh | 1 | 4 |
| Select both IDs, change explanation filter, enable/change score filter, passive rerun | 0 | 0 |
| Total dashboard verification | **3** | **12** |

Every listing used `Bucket=is459-aml-g1t5`,
`Prefix=rt/briefs/dt=2022-09-16/`, `MaxKeys=20`, with no continuation request.
All GetItem requests used only the two authorised IDs, `Key={"txn": id}`,
`ConsistentRead=True`, and the existing `is459_rt_alerts` / `is459_rt_briefs`
tables. Each action therefore performed two alert reads and two brief reads.
SDK transport retries were disabled for this verification; each successful
response reported zero retries. No S3 object body was read.

One successful **STS GetCallerIdentity** confirmed credentials without printing
the account or ARN. Initial sandbox preflight attempts did not complete a data
read: the restricted network prevented credential refresh, and an earlier strict
API guard rejected automatic login-token refresh before S3 transmission. The
normal AWS CLI credential chain then refreshed the existing login session outside
the network sandbox and the identity check succeeded. No interactive `aws login`
was needed. These failed preflight attempts are not counted as successful reads
in the table above.

The temporary verification process used an API allowlist: only the stated prefix,
20-key limit, two IDs, two tables and the three-action budget were allowed.
No DescribeTable was needed. There were no DynamoDB queries/scans, writes,
backfills, index changes, Lambda calls, Kinesis activity, Athena queries, Bedrock
calls or investigations. The normal authentication refresh changed only local
credential cache state; no application infrastructure was changed.

## Limits and offline coverage

**No browser visual verification was performed.** No connected browser automation
session was available. AppTest checked actual Streamlit table rows, widget actions
and rendered text using live service responses, but does not establish browser
layout, screenshots, pointer interaction or browser-specific behavior.

Real AWS access failure was not deliberately induced. Offline tests inject access,
schema and resource failures and verify that errors remain visible without demo
substitution. They also cover missing/optional fields, `needs_review`, pending
briefs, bounded pagination, source isolation and passive reruns. Those results
must not be presented as observed live missing/fallback states.

S3 discovery finds saved explanation objects for a historical replay partition.
It neither covers every scored transaction nor orders results by actual processing
time. Availability was observed for these two examples only. The index/scorer
integration remains undeployed; see [the integration handover](REALTIME_FEED_INTEGRATION.md).

A safe summary and the temporary verifier remain outside Git at
`/private/tmp/bda-realtime-finalise/summary.json` and
`/private/tmp/verify_realtime_finalise.py`. Temporary files are not durable team
artifacts; this report records the reviewed outcome without raw response dumps.

## Final offline regression

After the final label and Technical Details changes:

- Full suite: **875 passed** (`python -m pytest tests -q`).
- Focused real-time suite: **88 passed** (`python -m pytest
  tests/ui/test_realtime.py tests/ui/test_realtime_feed.py -q`).
- Changed app/UI/test Ruff checks, Python compilation, dependency checks and
  whitespace checks passed. The prior repository-wide lint checkpoint has
  14 existing diagnostics in unchanged agent/evaluation/unit files.

These offline results are separate from the live read operation counts above.
