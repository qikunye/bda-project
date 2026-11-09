import numpy as np, pyarrow.parquet as pq, pyarrow as pa
SRC, OUT = "data/silver/li_small/transactions/", "data/features/li_small_v1/"
raw = pq.read_table(SRC, columns=["transaction_id", "timestamp", "sender_account_key", "receiver_account_key"])
feat = pq.read_table(OUT, columns=["transaction_id", "snd_out_cnt_24h", "pair_prior_cnt", "snd_distinct_rcv_to_date"])
enc = lambda c: raw[c].combine_chunks().dictionary_encode().indices.to_numpy(zero_copy_only=False).astype(np.int64)
snd, rcv = enc("sender_account_key"), enc("receiver_account_key")
ts = raw["timestamp"].cast(pa.timestamp("s")).cast(pa.int64()).to_numpy()
tid = raw["transaction_id"].to_numpy(zero_copy_only=False)
f_idx = {t: i for i, t in enumerate(feat["transaction_id"].to_numpy(zero_copy_only=False))}
f24, fpair, fdist = (feat[c].to_numpy() for c in ["snd_out_cnt_24h", "pair_prior_cnt", "snd_distinct_rcv_to_date"])
picks = np.random.default_rng(42).choice(len(tid), size=20, replace=False)
ok = 0
for i in picks:
    m = (snd == snd[i]) & (ts < ts[i])
    brute = (int((m & (ts >= ts[i] - 86400)).sum()), int((m & (rcv == rcv[i])).sum()), int(len(np.unique(rcv[m]))))
    j = f_idx[tid[i]]; got = (int(f24[j]), int(fpair[j]), int(fdist[j]))
    ok += brute == got
    print("OK      " if brute == got else "MISMATCH", tid[i], brute, got)
print(f"T2.8: {ok}/20 match on all three")
