# Agent evaluation results, 14 to 16 Sep 2022

Run on 8 Nov 2026 from the committed code (freeze e3349bc plus D41, D42) with Sonnet 4.6.
Synthetic IBM AML data (LI-Medium). Labels were read only by `run_evaluation`, after the briefs
were made. All files are in `s3://is459-aml-g1t5/agent/traces/eval-runs/sonnet46/` (S2, repeats,
reports, summary, review sheets); the S1 and deeper S1 files are one level up.

## Runs

| Day | S2 run (saved to DynamoDB and S3) | Cases | Valid | Repaired | Fallback | Est. spend | Median latency |
|---|---|---|---|---|---|---|---|
| 14 Sep | `batch-2022-09-14-20261108T020412Z` | 78 | 60 | 14 | 4 | US$8.00 | 29.5 s |
| 15 Sep | `batch-2022-09-15-20261108T020412Z` | 77 | 65 | 9 | 3 | US$7.92 | 31.0 s |
| 16 Sep | `batch-2022-09-16-20261108T020412Z` | 82 | 66 | 9 | 7 | US$9.08 | 29.0 s |
| All | | 237 | 191 (81%) | 32 (14%) | 14 (6%) | US$25.00 | about 30 s |

Every fallback was "brief still invalid after one repair"; none came from the LLM call failing, so
the D41 save guard let all three days save. Observations: 426 kept, 93 dropped by the validator.
Deeper S1 (code only, top 500): 425, 410 and 424 cases, no failures. A second 14 Sep top-500 run
gave identical cases, so S1 is reproducible.

## Does a finding mean more laundering? (S1, top 500, three days pooled)

The top 100 is almost all laundering (236 of 237 cases), so it cannot show whether a finding adds
anything. The top-500 list can: 443 of 1,259 cases (35%) are laundering.

| Finding | Laundering when it fires | When it doesn't | Ranks 101-250 (with / without) | Ranks 251+ (with / without) |
|---|---|---|---|---|
| FAN_OUT | 83% (94/113, CI 75-89%) | 30% (349/1146) | 42/48 (88%) vs 106/266 (40%) | 6/19 (32%) vs 52/689 (8%) |
| PASS_THROUGH | 33% (13/40, CI 20-48%) | 35% | 8/15 vs 140/299 | 0/20 vs 58/688 |
| FAN_IN | 19% (33/173, CI 14-26%) | 38% | 4/27 vs 144/287 | 5/122 vs 53/586 |
| MONEY_RETURNS | 9% (1/11) | 35% | 1/3 vs 147/311 | 0/8 vs 58/700 |

- **FAN_OUT adds information beyond the BQ1 rank.** At the same rank band it roughly doubles to
  quadruples the laundering rate, so it is worth surfacing to an investigator.
- **FAN_IN points the other way**: cases with it are less often laundering at every band. Many
  legitimate accounts receive from many payers. Treat it as context, not a risk sign.
- PASS_THROUGH shows no lift; MONEY_RETURNS is too rare to judge.
- NEIGHBOURS_HIGH_RISK and REPEAT_ACCOUNT_ALERTS are not measured on the top-500 list (they count
  top-K alerts outside the case, and a deep list pulls those into the case). In the top 100 every
  case is laundering whether or not they fire, so their value is not shown by this data.

Pattern match (deeper S1, cases whose alerts belong to one labelled laundering attempt): the
FAN-OUT pattern is caught by FAN_OUT 55%, 43% and 53% of the time; FAN-IN by FAN_IN 40%, 38% and
40%; CYCLE by MONEY_RETURNS 0%.

## Does the LLM brief stay faithful? (S2 vs S1, same cases)

S2 keeps the code's findings almost exactly: across the three days it dropped 9 findings (mostly
NEIGHBOURS_HIGH_RISK) and added 7 (mostly REPEAT_ACCOUNT_ALERTS) out of several hundred. Every
finding it keeps is checked against the tool results by the validator.

## Repeatability (14 Sep, top 50, three S2 runs)

41 cases: same findings in all three runs 97.6% (40/41), same next checks 97.6%, same validation
outcome 92.7%, mean pairwise finding Jaccard 0.99. Observation wording varies run to run (17
cases had a different number of observations).

## Still open

- Mark the observations by hand: `review_2022-09-1{4,5,6}.csv` in the S3 folder above, then rerun
  `run_evaluation --marked-csv`. Until then, whether observations are useful is not measured.
- The deployed Fargate trigger still uses Sonnet 4.5 until the task role allows Sonnet 4.6 (D42).
