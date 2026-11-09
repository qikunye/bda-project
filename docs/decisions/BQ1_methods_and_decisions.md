# BQ1 methods and decisions

BQ1 ranks transactions for analyst review using synthetic IBM AML data. These
results measure recovery of synthetic labels, not real-world detection.

## Scope and data

Feature engineering, model training and model artefacts are maintained outside
this repository. Historical source/artefact names below refer to that external
project. Current batch Gold tables are `is459_aml_gold.bq1_risk_scores_medium_v2`
and `bq1_features_medium_v2`; the retained `src/bq1` serving helper and historical
BQ1 SQL still target v1. The dashboard's agent queue uses label-free v2 tables.
Ready-made historical queries: [BQ1 SQL](../../sql/bq1/BQ1_athena_queries.sql).

LI-Small (6,924,049 rows, 3,565 positives) was used to develop and select features
and hyperparameters. Its sparse 11–17 September tail (223 rows, 134 positives)
was excluded. The usable 60/20/20 split boundaries were 6 September 13:31 UTC
and 8 September 16:06 UTC; all rows at a shared boundary minute enter the later
split. Train/validation/test held 4,153,991 / 1,385,006 / 1,384,829 rows.

LI-Medium has 31,251,483 rows and 16,041 positives. The time split contains
18,748,707 training rows (1–9 September), 6,251,102 validation rows (9–14), and
6,250,304 test rows (14–16), with 3,013 test positives. Dates overlap at split
boundaries; a date alone is not a split filter. The 1,370 rows on 17–27 September
are scored but excluded from headline performance. Evaluate only `data_split =
'test'`; a global ranking mixes seen and unseen rows. Agent new-day ranking of
excluded rows does not make them part of the held-out evaluation.

## Causal features and leakage controls

Features use only strictly earlier minutes; same-minute transactions are excluded.
Amounts are compared within currency. Counts and sums with no history are zero;
prior means, time since prior payment, and amount ratios without a nonzero prior
mean are null. Distinct-counterparty counts use first-pair occurrence followed
by a strictly earlier running sum. Features can be computed before excluding the
tail because later rows cannot affect earlier features.

The external LI-Small checks preserved all 6,924,049 distinct IDs, matched 20/20
random brute-force recomputations (seed 42), and reproduced all earlier features
when later transactions were removed (3,462,207 retained rows; relative tolerance
1e-9). Glue parity reproduced the full LI-Small local features. LI-Medium Glue
output preserved 31,251,483 IDs and 16,041 labels; no local Medium feature parity
was available. These are historical methodology checks, not tests run by this repo.

### Feature definitions

Features are computed per transaction in time order. "Earlier" always means strictly before this transaction's minute, so same-minute transactions are not counted. A "sender" or "receiver" is an account key. Amounts are never summed across currencies.

| Feature | Plain meaning |
| --- | --- |
| `hour_of_day`, `day_of_week` | Hour (0 to 23) and weekday (Monday = 0) of the transaction, in UTC |
| `log_amount_paid` | `log(1 + amount paid)`, so very large amounts do not dominate |
| `payment_format`, `payment_currency`, `receiving_currency` | Taken straight from Silver (for example ACH, Cheque, Wire; the two currencies) |
| `is_same_account`, `is_same_entity`, `is_same_entity_different_account`, `is_cross_entity`, `is_cross_bank`, `is_cross_currency` | The six yes/no flags already in Silver, passed through. They say whether sender and receiver are the same account or entity, belong to different entities or banks, or use different currencies. |
| `snd_out_cnt_1h`, `snd_out_cnt_24h` | How many transactions this sender made in the previous 1 hour / 24 hours |
| `snd_out_amt_24h_ccy` | Total this sender paid in the previous 24 hours, in the same currency as this payment (0 if none) |
| `rcv_in_cnt_1h`, `rcv_in_cnt_24h` | How many transactions this receiver got in the previous 1 hour / 24 hours |
| `rcv_in_amt_24h_ccy` | Total this receiver got in the previous 24 hours, in the same currency as this receipt (0 if none) |
| `pair_prior_cnt` | How many earlier transactions went from this sender to this receiver |
| `is_new_pair` | True when `pair_prior_cnt` is 0, so this is the first payment between the two |
| `snd_distinct_rcv_to_date` | How many different receivers this sender has paid before |
| `rcv_distinct_snd_to_date` | How many different senders have paid this receiver before |
| `snd_prior_txn_cnt` | This sender's total number of earlier transactions |
| `snd_prior_mean_amt_ccy` | This sender's average earlier amount in the same currency (empty if there is no history) |
| `amt_ratio_to_snd_mean` | This amount divided by that average, so 3 means three times the sender's usual size (empty if there is no history) |
| `snd_secs_since_last_out` | Seconds since this sender's previous payment (empty if this is the first) |
| `amt_vs_ccy_median` | Added at training time: `log_amount_paid` minus the typical (median) value for that currency in the training days. Shows whether the amount is large or small for its currency. |

That makes 27 model inputs: 26 stored features plus train-fitted
`amt_vs_ccy_median`. Model artefacts and training source are external to this
checkout; this repository contains serving and evaluation integration.

## Model selection on LI-Small

- **Rules.** Six indicators, weight = max(0, ln(train rate with indicator / train base rate)), score = sum of weights. Weights: ACH 1.878,
  `snd_out_cnt_24h >= p95` 1.142, `is_new_pair` 0.855, `rcv_distinct_snd_to_date >= p95` 0.644, `amt_ratio_to_snd_mean >= 3` 0.169,
  `is_cross_currency` 0 (no train positives). Top-lift format picked with a minimum support of 1,000 train rows.
  Rules produce few distinct scores, so many rows tie; ties are broken by `transaction_id`, which is effectively random order.
  That is why Rules precision is flat between K=0.1% and K=0.5%.
- **Logistic regression.** All 1,813 train positives plus a 5% random sample of train negatives (207,609, seed 42). `log1p` on 13 nonnegative
  count/amount/gap columns, median imputation with missing indicators, standard scaling, one-hot categoricals, `class_weight="balanced"`, `max_iter=1000`.
- **XGBoost.** Full train set, `hist`, categorical support, `learning_rate=0.1`, `subsample=0.8`, `colsample_bytree=0.8`, `aucpr`, up to 2000 rounds, early stopping 50 on validation.
  - First pass (plan settings): `scale_pos_weight` = sqrt(neg/pos) = 47.9 beat neg/pos = 2,290 (val PR-AUC 0.2436 vs 0.1739).
  - Tuning (extra, requested by the user, validation only): staged search over `scale_pos_weight` {5, 10, 20, 47.9}, then `max_depth` {4, 8},
    `min_child_weight` {5, 20}, `reg_lambda` {10, 50}; a change was kept only for a gain of at least 1% relative on validation PR-AUC.
    Chosen: **`scale_pos_weight` 5, `max_depth` 8, `min_child_weight` 20**. Val PR-AUC 0.2438 to 0.2662; best iteration 74.
    The tuning log belongs to the external training project. These LI-Small settings are reused for LI-Medium without re-tuning.
  - Early stopping uses the validation split, so validation PR-AUC is slightly optimistic.
- **v2 features (T2.10) tried and not adopted.** Added `snd_in_cnt_24h`, `snd_secs_since_last_in`, `rcv_out_cnt_24h` from a long account-events table.
  Verified (brute force 40/40, truncation test clean) but no real gain (tuned XGBoost test PR-AUC 0.2195 vs 0.2191). v1 stays production.
  The `variant="v2"` code path is kept for the Glue `--variant` argument but is unused.
- **Importance (T4.4).** Top gain for the first XGBoost: `is_new_pair` 29%, `payment_format` 14%, `is_same_account` 8%, `is_cross_currency` 6%, `pair_prior_cnt` 6%.
  These importance figures describe the LI-Small experiment.
- **Leakage audit (T4.5).** Best single feature on test: `pair_prior_cnt` PR-AUC 0.0069 (prevalence 0.00057; flag threshold 0.5). No model had Recall@1% above 0.99. Nothing flagged, no gate.
  Single-feature scores use the better of x and -x; categoricals use a train-rate encoding.
- `day_of_week` is a mild concern: with only ten days it can proxy for specific dates. It has about 2.6% of gain. Test weekdays (Thu to Sat) all appear in train. Importance can vary by dataset.

## Final LI-Medium results

LI-Small hyperparameters were reused without Medium retuning. The held-out test
contains 6,250,304 rows and 3,013 positives; ties are ordered by transaction ID.

| Model | Recall@0.1% | Recall@1% | Recall@10% | Precision@1% |
|---|---|---|---|---|
| Random | 0.001 | 0.010 | 0.100 | 0.05% |
| Rules | 0.009 | 0.188 | 0.689 | 0.9% |
| Logistic regression | 0.183 | 0.446 | 0.885 | 2.2% |
| XGBoost (selected) | 0.302 | 0.634 | 0.928 | 3.1% |

XGBoost test PR-AUC is 0.2394. Recall@1% has an approximate 95% interval of
±0.017. LI-Small Recall@1% was 0.632 (791 test positives). A perfect 1% Medium
queue could attain only about 4.8% precision because labels are rare. Risk scores
are ranking signals, not calibrated probabilities. One time split was used;
there is no cross-validation or HI-Medium evaluation in this submission.

## Explanation limitations

Top-three feature explanations were produced for the top 1% of the test queue
(62,504 rows), separately from scores. They describe model contributions and do
not prove laundering. In a later label-free audit of the top 100 per day on
14–16 September, the same two reasons appeared on all 300 alerts and 36 (12%)
contained `nan`. Therefore the investigation agent never selects those reasons
and the current dashboard does not show them. This limitation concerns the
explanations rather than the measured ranking performance.

## LI-Medium tail-day analysis (10 October 2026)

**Question.** The tail days (17 to 27 Sep: 1,370 rows, 771 positives, laundering rate 56%) are excluded from every split (section 5, T7.3) and appear in Gold as `data_split = 'excluded'`.
They come after the test window, so the model never saw them and their scores are out of sample. How does the model rank them? This is a separate report; the split, the models and the headline numbers did not change.
**Method.** Rank arithmetic ran in Athena over `bq1_risk_scores_medium_v1` (rank = score descending, ties by `transaction_id`); only small tables came back (`scripts/epic9_tail.py`, `scripts/epic9_features.py`, `scripts/epic9_chart.py`).
Supporting files named in this section belong to the external BQ1 training project, not this checkout. The in-SQL recomputation of the headline reproduced T7.5 exactly (Recall@1% 0.6343, PR-AUC 0.2394), which checks the arithmetic.

**T9.1.** 1,370 rows, 771 positives, 11 dates; per-date counts match the T7.3 daily table.

**T9.2 and T9.3: would the test-time review budget have caught the tail laundering?** The threshold is the score of the m-th ranked test row; a tail row is flagged if its score is at or above it. No ties at any threshold.

| K (test queue) | Test threshold | Tail rows flagged | Tail positives flagged | Tail recall | Tail precision |
| --- | --- | --- | --- | --- | --- |
| 0.1% (m = 6,251) | 0.2354 | 761 | 693 | 0.899 | 0.911 |
| 0.5% (m = 31,252) | 0.0501 | 838 | 748 | 0.970 | 0.893 |
| 1% (m = 62,504) | 0.0193 | 864 | 760 | 0.986 | 0.880 |
| 5% (m = 312,516) | 0.0077 | 887 | 764 | 0.991 | 0.861 |
| 10% (m = 625,031) | 0.0050 | 903 | 766 | 0.994 | 0.848 |

Yes. At the same score cut-off that gives 1% alerts on the test days, the model flags 63% of tail rows and 98.6% of tail laundering rows.
That is far above the 63.4% recall at 1% on the test days, so tail laundering is easier to spot than test-day laundering.

**T9.4: ranking within the tail only (prevalence 0.563, tail PR-AUC 0.956).**

| Top K of tail rows | Rows | Recall | Precision | Lift over 0.563 |
| --- | --- | --- | --- | --- |
| 1% | 14 | 0.018 | 1.000 | 1.78 |
| 10% | 137 | 0.178 | 1.000 | 1.78 |
| 25% | 343 | 0.432 | 0.971 | 1.73 |
| 50% | 685 | 0.811 | 0.912 | 1.62 |

Precision is high for any ranking here because over half the rows are positive, so judge by the lift (1.6x to 1.8x), not the raw precision.

**T9.5: score distributions** (`tail_score_summary_medium.csv`, chart `tail_score_distributions_medium.png`; tail exact, test quartiles from Athena `approx_percentile`).
Median `risk_score`: tail positives 0.94, tail negatives 0.0014, test positives 0.086, test negatives 0.000026. Tail rows of both labels score higher than their test counterparts, and tail positives are pushed close to 1.

**T9.6: what the headline would have been with the tail pooled in** (6,251,674 rows, 3,784 positives).

| K | Recall, test only | Recall, test + tail | Precision, test only | Precision, test + tail |
| --- | --- | --- | --- | --- |
| 0.1% | 0.302 | 0.414 | 14.6% | 25.0% |
| 0.5% | 0.561 | 0.640 | 5.4% | 7.8% |
| 1% | 0.634 | 0.705 | 3.1% | 4.3% |
| 5% | 0.777 | 0.820 | 0.75% | 0.99% |
| 10% | 0.928 | 0.941 | 0.45% | 0.57% |

PR-AUC rises from 0.239 to 0.385. Pooling the tail would inflate every metric (Recall@1% by 0.07), because those rows are easy and nearly all positive. This quantifies the distortion that justified dropping the tail days. The headline stays the test-only number.

**T9.7: why the model ranks them where it does (descriptive; `tail_feature_compare_medium.csv`, `tail_payment_format_medium.csv`).** Joined to the Gold features (read-only external table `bq1_features_medium_v1`, `docs/athena_bq1_features.sql`).

| | Tail negatives | Tail positives | Test negatives | Test positives |
| --- | --- | --- | --- | --- |
| Rows | 599 | 771 | 6,247,291 | 3,013 |
| Payment format ACH | 100% | 100% | 12.7% | 69.5% |
| `is_new_pair` | 27.0% | 96.4% | 2.4% | 57.2% |
| `is_same_account` | 82.3% | 0.4% | 1.9% | 0.1% |
| `is_cross_entity` | 17.7% | 99.6% | 97.7% | 99.4% |
| `is_cross_currency` | 82.3% | 0% | 1.6% | 0% |
| Median `snd_out_cnt_24h` | 0 | 0 | 5 | 2 |
| Median `snd_prior_txn_cnt` | 12 | 18 | 66 | 20 |
| Median `pair_prior_cnt` | 1 | 0 | 23 | 0 |
| Median `amt_ratio_to_snd_mean` | 0.81 | 0.84 | 0.22 | 0.03 |

Reading: every tail row is ACH, so the top-gain feature `payment_format` (20% of gain) cannot separate them. What does separate them is `is_same_account` (12% of gain) and `is_new_pair` (9%):
82% of tail negatives are same-account cross-currency rows, which the model has learned are benign, while 96% of tail positives are new, cross-entity pairs, which the model has learned are risky.
The tail rows also look unlike normal days: a few hundred rows a day against 0.8 to 4.4 million, a median of zero outgoing transactions in the previous 24 hours, and a 56% laundering rate against 0.05%.
Inference, not verified against the generator: late in the window ordinary traffic dries up while laundering-labelled ACH transfers continue, which makes the remaining rows easy to separate.
There is no sign of leakage: the features are causal (Epic 2 tests) and the leakage audit passed on the test split.

**Decision (user, 2026-10-10).** Keep excluding the tail days from every split and from the headline numbers. The findings above are kept as a separate, documented analysis. The split, the models and the Gold scores are unchanged.
