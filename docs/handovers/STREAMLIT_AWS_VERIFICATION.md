# Streamlit AWS verification

**Date:** 8 November 2026 (Asia/Singapore)

**Branch / verification baseline:** `streamlit`, `42498b1`

**Region:** `us-east-1`

**Outcome:** blocked at authentication; resource and live workflow verification
have not occurred. This report must not be treated as a successful AWS smoke test.

## Git and identity

The working tree was clean. `git fetch origin` confirmed that `streamlit` was
one commit ahead with no remote-only commits. The explicitly authorised
`git push origin streamlit` published `42498b1`. `main` was not modified.

One SDK `STS.GetCallerIdentity` invocation was attempted using the existing
credential chain. Botocore returned **`LoginRefreshRequired`**: the session has
expired or credentials have changed and require reauthentication. No authenticated
identity response was obtained; no account identifiers or credentials were
displayed. The STS request may have been blocked before transmission by the
credential provider; no successful STS API call is claimed.

All further AWS checks stopped at this gate, as requested. No login flow,
credential-file inspection, IAM changes or access-key creation was performed.
Refresh the existing AWS login before resuming verification.

## Resource checks

| Resource / path | Outcome |
|---|---|
| Glue `is459_aml_gold.agent_day_queue_v2` | Not inspected; columns, types and partitions unverified against deployment |
| Glue `is459_aml_gold.agent_scores_v2` | Not inspected |
| Glue `is459_aml_gold.agent_edges_v2` | Not inspected |
| DynamoDB `is459_agent_decisions` | DescribeTable, TTL and BatchGetItem not attempted |
| DynamoDB `is459_agent_cases` | DescribeTable, TTL and GetItem not attempted |
| DynamoDB `is459_rt_alerts` | DescribeTable, TTL and keyed reads not attempted |
| DynamoDB `is459_rt_briefs` | DescribeTable, TTL and keyed reads not attempted |
| S3 saved case trace | No case or object fetched; compatibility and graph rendering from real data unverified |
| S3 `s3://is459-aml-g1t5/rt/code/` | No prefix listing or writer-source object read attempted |

Table status, partition/sort keys, GSIs, key attribute types, TTL and effective
Athena/Glue/S3/DynamoDB permissions remain unknown. Authentication failure is not
evidence that resources or permissions are missing.

## Bounded query and saved results

- Athena executions: **0**. No SQL was submitted, including the planned
  14 September 2022 `tools.load_day_queue(..., top_k=5)` smoke query.
- Query result count, bytes scanned and execution latency: **not available**.
  No Athena query-result writes occurred.
- Saved decisions sampled: **0**; found/missing/incompatible counts cannot be
  determined. No claim is made about the existence of published results.
- Representative resolution, case membership and S3 envelope compatibility:
  **not live-verified**.
- Live service path, interactive browser workflow and real-data graph rendering:
  **not tested**. Existing offline tests use injected fakes only.

## Real-time contracts

The previous local evidence remains unchanged: `PLAN.md` F21 documents `txn`
keys on both tables and a string alert `amount`. Complete writer fields,
timestamps, statuses, indexes, TTL and bounded session-ID manifests remain
unverified. No production field mapping was inferred or changed in this run.

Real-time pairs read: **0**. Neither known-ID availability nor missing/delayed
brief behavior has been tested against deployment. Automatic listing stays
disabled and no scan was attempted.

## Observed issue and offline fix

The live identity attempt exposed a UI compatibility issue: the installed
Botocore `LoginRefreshRequired` exception fell through to the generic request
failure category. `src/ui/services/errors.py` now identifies it explicitly as
an authentication failure and asks for an AWS login refresh. The original
exception remains server-side; credentials and provider responses are not shown.

Offline regression tests instantiate this exact exception type and verify both
the error category and the Live queue error screen, with no demo substitution,
automatic retry or subsequent saved-case request. No production agent, pipeline,
SQL, AWS resource or real-time record schema was changed.

## Offline validation

- Full unit/UI suite: **743 passed**, including the previous 741 and two new
  authentication regressions. Existing offline rehearsal, agent, graph/evidence,
  on-demand, real-time and legacy BQ2 checks passed.
- Ruff on `src/ui` and `tests/ui`: passed.
- Repository-wide Ruff: the same **14 unchanged baseline findings**, compared
  with committed `42498b1`; no new diagnostics.
- Python compilation, `pip check` and Git whitespace checks: passed.
- Regression tests used injected failures and fail-on-use AWS guards. They do
  not establish live resource access or a successful live browser workflow.

## Operations and next verification boundary

Explicit AWS operations requested by this run: one attempted
`STS.GetCallerIdentity`, blocked by credential refresh. Glue, DynamoDB, S3,
Athena, Bedrock, Lambda, Kinesis and SQS resource operations: **none**.
No agent or batch ran; no infrastructure or table data was changed.

After the login is refreshed, resume at STS identity verification. Then follow
the existing bounded plan: metadata reads; one date-filtered top-five Athena
query; keyed decisions; at most one available representative/case/trace path;
bounded writer-source inspection; and at most five known real-time pairs.
Do not broaden reads, scan tables, run Bedrock or manufacture missing results.

Remaining blocker: **expired/changed AWS login**. All resource, permission,
data-availability and live schema conclusions await successful authentication.
