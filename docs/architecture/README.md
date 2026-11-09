# Architecture decisions and limits

The [repository overview](../../README.md#architecture) describes the daily,
on-demand and real-time paths. No approved final diagram was found in the
repository or supplied project assets. A diagram remains an optional documentation
artifact; the textual architecture is the submission reference.

## Agent data boundary

Gold contains evaluation labels. SQL in [sql/agent](../../sql/agent/) projects
label-free views, then materialises `agent_scores_v2`, `agent_edges_v2` and
`agent_day_queue_v2`. Agent queries select allowlisted columns and reject labels;
`data_split` and the patterns ground truth also stay outside agent inputs.
The investigation code never imports `src/evaluation`. Historical BQ1/BQ2
analysis and evaluation scripts can read labelled Gold and are distinct from
the label-free investigation path.

Scores and edges are Hive Parquet partitioned by `txn_date`, with 32 buckets
by transaction ID and account key respectively; the daily queue is partitioned
but not bucketed. Silver Iceberg belongs to [ETL v2](../../etl_v2/docs/runbook.md), not these agent
tables. Athena cannot insert into bucketed tables, so extending coverage requires
a rebuild rather than an append. Current date coverage follows `AGENT_LAST_DAY`
(default 16 September 2022); edges include the 72-hour lookback from 11 September.

The ETL success event reaches EventBridge, which starts the agent Step Functions
workflow. The first Fargate task rebuilds tables under a separate role able to
read Gold and write only the agent table prefix. The second task uses a label-free
role to investigate and publish. Both receive the event day as `AGENT_LAST_DAY`.
Rebuilds skip already covered days, reject shrinking coverage and compare daily
rank fingerprints. See [infrastructure setup](../../infra/README.md).

## Investigation scope

Daily work is bounded to the top 100 BQ1 alerts, grouped when accounts overlap
within the window, at most 20 members per case. The best-ranked member represents
the case. BQ1 order is preserved; the brief adds evidence without a verdict.
BQ2 is one hop (6–72 hours, default 48), which is the evaluated scope. Following
counterparties for a second hop would be additional, unevaluated analysis.

Daily runs stop at the end of the alert's day. On-demand `investigate()` works on
one alert, has no historical end-of-day cutoff, and can see later available data
up to its dataset horizon. It does not publish over a daily saved brief. Comparing
these views is not a controlled experiment because membership and cutoffs differ.

Tool payloads cap edges (50 per direction) and neighbour IDs (200); currency totals
remain separate. `quick_onward_share` divides quick onward payments by the alert
amount in the same currency and can exceed one for busy accounts. One-hop data
cannot establish which incoming payment funded an outgoing payment. Findings
indicate patterns consistent with the evidence, not source-of-funds attribution.

Thresholds in [src/config.py](../../src/config.py) were set from label-free
activity distributions before evaluation. Fan thresholds use five counterparties,
above the ordinary-receiver 99th percentile of four; pass-through requires an
upstream payment in the same currency, onward payments within six hours and at
least half the alert amount. These thresholds were not loosened to make rare
findings appear. Account history on the evaluated days is only three to five days;
activity-spike claims were omitted because the history is short and overlaps the
investigation window. Dataset truncation and short observed time are separate gaps.

The LLM selects tools and findings; code validates cited IDs and finding rules,
and renders numbers from returned evidence. S1 uses the same tools and rules and
is also the fallback. Observations have ID and wording checks only, not semantic
verification. Their usefulness has not been manually measured. See the
[output contract](../agent_output_format.md) and [evaluation results](../evaluation_results.md).

## Publishing and operational limits

Decisions use transaction IDs; cases use day and case ID; S3 holds full traces.
A completed batch writes new rows, then removes stale rows for that day. A failed
case prevents publication. The CLI also refuses `--save` after LLM-call or spend-cap
failures unless explicitly overridden with `--allow-llm-failures`. Publication is
not an atomic multi-service transaction; readers validate matching metadata.

New days must run one at a time: concurrent rebuilds share the same tables and
agent tools are unavailable while tables are rebuilt. Rebuild scan cost grows
with retained days (about 2.7 GB at the documented checkpoint). The four-case
concurrency, spend cap per process, account-wide Athena concurrency and Bedrock
quotas are scaling limits, not throughput guarantees. No load test was performed.

The 17 September new-day check rebuilt tables with unchanged 14–16 September
ranks and started investigation, but stopped at the model's daily token quota
before saving. A fully saved new-day run therefore remains unverified. Existing
14–16 September saved results and evaluation evidence remain available.

The ETL is tailored to LI-Medium: its audit reads the source file for
each day and its BQ1 parity check depends on v1 output. Larger datasets need
source/table changes and re-evaluation. Real-time processing was demonstrated on
historical replay slices, not continuous production traffic. Its writers and
session lifecycle are implemented in [realtime](../../realtime/README.md); Streamlit displays persisted Qwen outputs and
does not launch streaming or agent investigation from that page. The optional
recent-alert index was not deployed and is unnecessary for saved-replay browsing.

This checkout scripts deployment of the agent batch using AWS CLI, an existing
ETL event rule, existing Gold/S3 resources and a default VPC. It is not a complete
platform deployment package. ETL jobs, BQ1 training/feature engineering and
real-time writers are now included, but historical Glue templates, original
source data, frozen production model assets and existing project AWS resources
are still required. See the [ETL prerequisites](../../etl_v2/docs/runbook.md#working-directory-and-deployment-dependencies).
