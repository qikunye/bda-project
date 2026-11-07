# Running the agent evaluation

Run from `bda-project/` with `.venv/bin/python` (bare `python` fails). Do the steps in order: labels are
read only after the thresholds and prompt are frozen, and observations are marked before anyone sees labels.

**What is compared.** For each of 14, 15 and 16 Sep (top 100 alerts, grouped into cases) there are two
briefs: **S1** (code only) and **S2** (LLM). S2's findings are nearly always a subset of S1's, so for
findings S2 vs S1 shows how much the LLM leaves out. The LLM's value is the summary, next checks and
observations.

## Before you start

- AWS login (Athena and Bedrock) and `.venv/bin/python -m pytest tests` passes.
- The freeze is committed: the thresholds in `src/config.py`, the prompt in `src/agent/prompts.py` and the rest of `src/`, with no later
  change to `src/`. Make the S1 and S2 files in step 1 from that committed code. Anything changed after labels are read must be reported as "tuned after the results".

## 1. Make the briefs (once per day)

```
mkdir -p runs
.venv/bin/python -m scripts.run_s1 --day <day> --out runs/s1_<day>.json
.venv/bin/python -m scripts.run_batch --day <day> --spend-cap 20 --out runs/s2_<day>.json
```

`<day>` is `2022-09-14`, `2022-09-15` or `2022-09-16`. S1 takes about 26 minutes. S2 costs about US$11 a day and takes about 20 minutes. Keep `--spend-cap 20`: the default cap of US$10 stops a full day early.
`run_batch` is a dry run; add `--save` only for the final runs, and note the run IDs in PLAN.md.

Copy the summary `run_batch` prints: valid / repaired / fallback rates, observations kept and dropped,
tokens, latency and spend.

## 2. Mark observations by hand (before labels)

```
.venv/bin/python -m scripts.run_evaluation --day <day> --s1 runs/s1_<day>.json \
  --s2 runs/s2_<day>.json --review-only --review-csv runs/review_<day>.csv
```

Fill the `mark` column of about 50 rows with `useful` (accurate and adds something), `obvious` (accurate but
repeats the findings) or `wrong` (not supported by the cited payments).

## 3. Run the evaluation

```
.venv/bin/python -m scripts.run_evaluation --day <day> --s1 runs/s1_<day>.json \
  --s2 runs/s2_<day>.json --marked-csv runs/review_<day>.csv --out runs/eval_<day>.json --confirm-frozen
```

`--confirm-frozen` means the freeze is committed; the script refuses to read labels without it. It costs a few
cents.

## 4. Read the report

| Key | What to take from it |
|---|---|
| `labels_found` | Check first: how many alerts have a label and how many are laundering. |
| `s1_code_rates`, `s2_code_rates` | Per finding, the laundering rate of cases with it vs without it, with a 95% interval. |
| `s1_bq1_derived_by_rank_band` | For `NEIGHBOURS_HIGH_RISK` and `REPEAT_ACCOUNT_ALERTS`, the same comparison inside rank bands. Only this counts for them, as they come from BQ1 scores. |
| `s1_pattern_match`, `s2_pattern_match`, `pattern_coverage` | Whether the finding matching the attempt type fired. Read coverage first: many attempts will not match. |
| `s2_vs_s1` | Findings S2 dropped from S1, and any it added. |
| `reliability_s2`, `observation_marks` | Fallback and repair rates, and the share of observations marked useful, obvious and wrong. |

Keep it honest:
- With few laundering cases, a per-code rate means little. Quote the interval and say so.
- If a finding fires on very few cases (`PASS_THROUGH`, `MONEY_RETURNS`), report the count. Do not loosen a
  threshold to make it fire.
- Report each day separately.
- Write plainly if the LLM adds nothing over code. Add a few example briefs: a good one, an ordinary one and a
  fallback.

## If something fails

- `Refusing to read labels`: pass `--confirm-frozen` after the freeze is committed.
- `LabelLeakError`: a label reached the agent. Stop and tell the owner.
- `ModuleNotFoundError: src`: run from `bda-project/` with `-m scripts.<name>`.
