# Real-time discovery integration handover

Prepared locally on `streamlit`, 8 November 2026. No AWS calls or deployments
were made for this enhancement. Tests use invented offline records only.

## Available now

**Real-Time Monitoring** defaults to **Demo (offline)**. Select **Saved Replay Results**, **Transaction Date** **2022-09-16**, leave the
record count at **20**, and click **Load Results**. Select a transaction
from the table's accompanying selector to read its persisted Qwen explanation.
Refresh is explicit. Changing source/date/count clears the displayed discovery
snapshot and requires another Load. Filtering, selection and expansion use the
snapshot without AWS calls. Manual lookup is in the collapsed advanced section,
with its own Demo/Live source choice and existing 1–20 ID validation.

S3 discovery lists only keys directly under
`s3://is459-aml-g1t5/rt/briefs/dt=YYYY-MM-DD/`. Accepted names are
`txn_<1–252 ASCII alphanumeric characters>.json`. The extracted ID includes
`txn_`. No real IDs are hardcoded in service code or demo fixtures. The two
previously observed example IDs are documented in the team handover; their
continued availability has not been reverified here.

This prefix date is a historical replay partition, not a processing date.
S3 key order is not processing order. Objects indicate explanation output existed,
not that the paired DynamoDB records still exist or that all scored transactions
have explanations. Empty prefixes, expired access, missing records and malformed
records are shown honestly. There is no automatic substitution of demo data.
No S3 explanation bodies are downloaded.

## Service contract and bounds

`RealtimeFeedService.completed(date, limit=20)` and
`RealtimeFeedService.recent(date, limit=20)` return `FeedResult(records, limited,
skipped)`. Both lazily initialise AWS clients only when explicitly invoked.
`limit` must be an integer from 1 to 50. Each action makes at most **three
logical discovery requests**, each with page size equal to `limit` (at most 150
index entries or keys evaluated). Paginators carry the service cursor, including
all DynamoDB key fields; pagination is never based on timestamp alone.
Discovery stops once enough unique IDs are collected. The result reports a
remaining cursor or omitted IDs as limited/incomplete. Invalid S3 filenames are
counted and skipped; incompatible index entries fail rather than inventing IDs.
Provider failures abort discovery with the existing safe error classification.
There is no partial-success listing after a failed discovery request.

IDs are deduplicated, then resolved through `RealtimeService.lookup_many()` in
chunks of at most 20. At most **100 keyed GetItem reads** occur per discovery
action. Per-record read/schema errors use the existing DTOs and mapping.
SDK transport retries may add HTTP attempts; these bounds describe logical
requests. There is no Scan, ListTables, DescribeTable, Lambda invocation,
Kinesis activity, Athena query or model/agent execution in this service.

The indexed feed sorts descending by numeric processing timestamp, then by ID
for ties within the returned set. Equal-time IDs at the limit boundary have no
guaranteed global tie order. The table remains keyed by `txn`; a reprocessed ID
updates its existing alert rather than creating an immutable processing history.
GSI visibility is eventually consistent, and separate canonical alert/brief
reads do not form a transactional snapshot. A rewrite during retrieval can
change canonical fields after index discovery. Filters operate on loaded data,
not an exhaustive day query. Missing scores remain visible under All scores;
threshold filters require both a stored numeric score and threshold. Meeting a
threshold is a screening indication, not a suspicious-activity verdict.

## Real-time teammate: external writer patch

The checkout has no `rt/` directory, scorer/explainer Lambda writer or real-time
DynamoDB infrastructure definition. The prior AWS verification report describes
exports at `s3://is459-aml-g1t5/rt/code/`, including `app.py` and `deploy_rt.py`.
Those exports were not downloaded or executed for this task. Apply the following
patch to the actual scorer and its infrastructure source, after reviewing their
current contents. Do not add a guessed replacement writer to this repository.

At the point the scorer builds the item written to **is459_rt_alerts**, immediately
before its existing PutItem, derive the UTC day from the **same** processing epoch
that is persisted as `scored_at_ms`:

```python
from datetime import datetime, timezone

# alert_item denotes the existing alert item, not a new write operation.
# Keep scored_at_ms a DynamoDB Number; retain the existing risk model and score.
alert_item["feed_day"] = datetime.fromtimestamp(
    int(alert_item["scored_at_ms"]) / 1000, timezone.utc
).date().isoformat()
```

Use the writer's actual item variable name. If it builds low-level AttributeValue
maps, derive the day from the original numeric timestamp and encode feed_day as
`{"S": utc_day}`; leave scored_at_ms encoded as `{"N": "<epoch milliseconds>"}`.
Add `feed_day` only to the alert write. Preserve `txn`, all score/threshold fields,
the existing timestamp, and explanation triggering behavior. Derive it after
scoring, not from `txn_ts`, the S3 `dt` partition, Kinesis arrival time or local
time. Ensure any later full replacement of an alert preserves/recomputes these
attributes. Do not change the explainer, Qwen weights or investigation agent.
Writer tests should cover UTC midnight boundaries, numeric timestamp encoding,
rewrites, retained original fields and both threshold outcomes.

## Real-time teammate: index and access patch (prepare/review; not executed)

Add a GSI named **rt-alerts-by-processing-day** to the existing alerts table:

```json
{
  "AttributeDefinitions": [
    {"AttributeName": "feed_day", "AttributeType": "S"},
    {"AttributeName": "scored_at_ms", "AttributeType": "N"}
  ],
  "GlobalSecondaryIndexUpdates": [{
    "Create": {
      "IndexName": "rt-alerts-by-processing-day",
      "KeySchema": [
        {"AttributeName": "feed_day", "KeyType": "HASH"},
        {"AttributeName": "scored_at_ms", "KeyType": "RANGE"}
      ],
      "Projection": {"ProjectionType": "KEYS_ONLY"}
    }
  }]
}
```

This is the **UpdateTable payload fragment**, not a new table definition or a
command to run now. Retain `txn` as the table HASH key. KEYS_ONLY automatically
includes `txn` and the index keys, which is all the feed queries project.
For a provisioned table, the owner must add a reviewed
`ProvisionedThroughput: {ReadCapacityUnits: ..., WriteCapacityUnits: ...}` to
Create and configure the intended capacity/autoscaling in the owning stack.
For PAY_PER_REQUEST, omit that field. Confirm actual billing mode and existing
indexes first; do not guess or replace the whole table. Update the owner’s
CloudFormation/CDK/other source of truth so subsequent deployment retains the
index. Add the two attribute definitions alongside existing key definitions.
No index or writer deployment is authorised by this handover.

For Streamlit's read role, retain DynamoDB GetItem access on both tables and add
DynamoDB Query on
`arn:aws:dynamodb:us-east-1:<account-id>:table/is459_rt_alerts/index/rt-alerts-by-processing-day`.
S3 discovery needs `s3:ListBucket` on `arn:aws:s3:::is459-aml-g1t5`, restricted
with `s3:prefix` StringLike `rt/briefs/dt=????-??-??/*`.
It needs neither `s3:GetObject` for briefs nor write/invoke permissions.
Use the actual account ID in the owning policy, never commit credentials.

After separately authorised deployment, wait for the index to become ACTIVE,
verify new writes contain both required attributes with correct types, and set:

```bash
export REALTIME_ALERTS_INDEX=rt-alerts-by-processing-day
streamlit run app.py
```

Only then does **Live Recent Alerts (indexed DynamoDB)** appear. Choose the
**UTC processing date** of the replay run (for example the day in 2026 when
historical transactions were scored), not the original 2022 transaction date.
Click Load and verify newest-first results, pending/completed Qwen states and
safe handling of missing access. A configured but absent/wrong index produces
an error and never falls back to S3 or demo automatically.

Existing items without `feed_day` are absent from this sparse index. Creating
the GSI does not invent missing attributes or make old items discoverable. No
backfill is included or authorised; S3 discovery and manual lookup remain usable
for historical items. Any future backfill needs a separate bounded plan and
approval. Confirm index write capacity, hot-day partition throughput and retention
with the pipeline owner before production-scale use.

This feature extends **BQ1 screening**. Its Qwen narratives, typologies and next
checks are pipeline interpretations; viewing them never starts BQ2/Claude
investigation and does not produce validated Claude findings.

## Local validation for this enhancement

- Full offline suite: `python -m pytest tests -q` — **873 passed**.
- Focused Streamlit/UI suite: `python -m pytest tests/ui -q` — **277 passed**.
- `ruff check app.py src/ui tests/ui`, Python compilation, `pip check` and
  `git diff --check` passed.
- Repository-wide Ruff reports **14 existing diagnostics** in unchanged agent,
  evaluation and unit-test files. Those files were left intact.
- UI tests forbid AWS and agent execution, including calls swallowed by error
  handlers. Botocore pagination is exercised with fake API methods, not live AWS.
- No cloud read verification, deployment, resource mutation, processing,
  backfill, push or merge was performed for this enhancement.

Final terminology: **Data Source → Saved Replay Results → Transaction Date →
Load Results**, under **Replay Results**. S3/index/coverage implementation details
are in the collapsed **Technical Details** expander.

The subsequent [live read verification](REALTIME_REPLAY_READ_VERIFICATION.md)
confirmed both known replay IDs and their actual Qwen content through SDK-backed
Streamlit AppTest. This supersedes the earlier “no live verification” checkpoint
for these two replay examples only; browser visuals and indexed discovery remain
unverified. No GSI or scorer deployment was performed.
