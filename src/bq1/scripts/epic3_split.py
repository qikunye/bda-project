import sys, json, datetime as dt; sys.path.insert(0, ".")
import pandas as pd
from src.bq1 import split as S

START = int(dt.datetime(2022, 9, 1, tzinfo=dt.timezone.utc).timestamp())
END = int(dt.datetime(2022, 9, 11, tzinfo=dt.timezone.utc).timestamp())
df = S.load_features("data/features/li_small_v1", START, END)
print("usable window rows", len(df), "positives", int(df.is_laundering.sum()))

t60, t80 = S.find_boundaries(df["ts"])
df["split"] = S.assign_split(df["ts"], t60, t80)
summ = S.split_summary(df, df["split"].values)
fmt = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%d %H:%M")
print(f"t60={t60} ({fmt(t60)} UTC)  t80={t80} ({fmt(t80)} UTC)")
print(summ.to_string(index=False))
gate = bool((summ["positives"] < 300).any())
print("GATE (a split has <300 positives):", gate)

cfg = {"dataset": "li_small", "silver_table": "is459_aml_silver.li_small_transactions_small_live_03",
       "timezone": "UTC", "seed": S.SEED,
       "window_start": "2022-09-01 00:00:00", "window_end_exclusive": "2022-09-11 00:00:00",
       "window_start_ts": START, "window_end_ts": END,
       "tail_rule": "day rows < 10% of median daily rows",
       "tail_days_dropped": [f"2022-09-{d}" for d in range(11, 18)],
       "tail_rows_dropped": 223, "tail_positives_dropped": 134, "decision": "user chose to drop tail days (Epic 1 gate)",
       "split_rule": "train: ts < t60; validation: t60 <= ts < t80; test: ts >= t80 (60/20/20 by row)",
       "t60_ts": t60, "t80_ts": t80, "t60_utc": fmt(t60), "t80_utc": fmt(t80),
       "splits": summ.to_dict(orient="records")}
S.write_json(cfg, "artifacts/small/split_config.json")
if gate:
    print("Stopping before T3.3: gate triggered."); sys.exit(0)

# T3.3 fit on TRAIN only
train = df[df["split"] == "train"]
stats = S.fit_train_stats(train)
df = S.add_amt_vs_ccy_median(df, stats)
df = S.apply_categories(df, stats)
print("currency medians (train):", {k: round(v, 3) for k, v in stats["amt_ccy_median"].items()})
print("categories:", {k: len(v) for k, v in stats["categories"].items()})
print("unseen-category rows outside train:",
      {c: int((df.loc[df.split != 'train', c] == S.OTHER).sum()) for c in S.CATEGORICAL})

# T3.4 schema
order = S.feature_order()
dtypes = {c: str(df[c].dtype) for c in order}
schema = {"feature_version": "v1", "seed": S.SEED, "features": order, "dtypes": dtypes,
          "numeric": S.NUMERIC + ["amt_vs_ccy_median"], "flags": S.FLAGS, "categorical": S.CATEGORICAL,
          "nullable_numeric": [c for c in S.NUMERIC if df[c].isna().any()],
          "category_lists": {c: stats["categories"][c] + [S.OTHER] for c in S.CATEGORICAL},
          "fit_on": "train split only",
          "amt_vs_ccy_median": {"definition": "log_amount_paid - median(log_amount_paid) for payment_currency, fitted on train; unseen currency uses the global train median",
                                "per_currency_median": stats["amt_ccy_median"], "global_median": stats["amt_global_median"]}}
S.write_json(schema, "artifacts/small/feature_schema.json")
print("n features", len(order), "| nullable numeric:", schema["nullable_numeric"])
print(pd.Series(dtypes).value_counts().to_dict())
