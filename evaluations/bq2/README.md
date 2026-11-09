# BQ2 historical window evaluation

BQ2 retrieves one-hop payments into an alert's sender before the alert and out
of its receiver afterwards. These experiments compare recovery of known partner
accounts in sampled synthetic RANDOM laundering patterns against the number of
candidate accounts and payment edges presented for review.

| File | Role |
|---|---|
| `evaluate_bq2.py` | Reads the S3 pattern ground truth and extracts eligible middle-of-chain cases and required timing windows |
| `evaluate_bq2_athena.py` | Retrieves Athena edges for a fixed 30-hour window and calculates recovery/noise metrics |
| `evaluate_bq2_windows.py` | Compares 6, 12, 24, 30, 48 and 72 hours using the same sampled cases |
| `bq2_random_evaluation_cases.csv` | 176 sampled cases with expected upstream/downstream partners and timing |
| `bq2_30h_athena_evaluation.csv` | Per-case results and failure classification for the fixed 30-hour experiment |
| `bq2_window_case_results.csv` | Per-case recovery, candidate workload and timing ceilings for every window |
| `bq2_window_tradeoff_evaluation.csv` | Summary used to select the 48-hour default: about 89% recovery of both partners, median three candidate accounts |

## Reproduction

Use the root [Python environment](../../README.md#getting-started). From the
repository root, the original experiment runs in this order:

```bash
.venv/bin/python evaluations/bq2/evaluate_bq2.py
.venv/bin/python evaluations/bq2/evaluate_bq2_athena.py
.venv/bin/python evaluations/bq2/evaluate_bq2_windows.py
```

These commands require AWS credentials, S3 access to
`bronze/ibm-aml/li-medium/patterns/LI-Medium_Patterns.txt` in the project bucket,
Athena/Glue access and a writable query-results prefix. They incur AWS charges
and replace the named output CSVs; they are not offline smoke-test commands.
No evaluation data was regenerated during repository organisation.

All input/output defaults resolve relative to this directory via `__file__`, so
invocation from another working directory uses the same files. The existing
`load_cases(path=...)` override accepts an explicit alternative input path.
Calculations and the checked-in CSV contents have not changed.

The historical Athena dependency is
`is459_aml_gold.bq2_graph_edges_medium_v1`. The final agent pipeline uses v2;
these are preserved v1 experiments, not newly measured v2 results. Reproduction
needs the original v1 table and source ground truth. A future v2 evaluation must
be reported separately after confirming parity.

## Offline validation and data boundary

```bash
.venv/bin/python -m pytest tests/unit/test_bq2_evaluation_paths.py -q
```

The tests load the checked-in cases, verify paths from an unrelated working
directory, check the input override and reproduce the summary from per-case
CSVs without Athena. SQL builders are checked without executing queries.

Evaluation labels and expected partners are ground truth used only to judge
retrieval. They never enter agent tools, inference inputs or runtime tables.
Application BQ2 logic remains in [src/bq2](../../src/bq2/); the label-free agent
and agent evaluation remain separately in `src/agent` and `src/evaluation`.
