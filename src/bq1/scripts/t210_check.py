import numpy as np, pyarrow.parquet as pq, pyarrow as pa, pyarrow.compute as pc
SRC, V2 = "data/silver/li_small/transactions/", "data/features/li_small_v2_extra/"
raw = pq.read_table(SRC, columns=["transaction_id", "timestamp", "sender_account_key", "receiver_account_key"])
both = pa.chunked_array(raw["sender_account_key"].chunks + raw["receiver_account_key"].chunks).combine_chunks().dictionary_encode()
codes = both.indices.to_numpy(zero_copy_only=False).astype(np.int64)      # ONE shared encoding for both columns
n = len(raw); snd, rcv = codes[:n], codes[n:]
ts = raw["timestamp"].cast(pa.timestamp("s")).cast(pa.int64()).to_numpy()
tid = raw["transaction_id"].to_numpy(zero_copy_only=False)
f = pq.read_table(V2)
idx = {t: i for i, t in enumerate(f["transaction_id"].to_numpy(zero_copy_only=False))}
a = f["snd_in_cnt_24h"].to_numpy(zero_copy_only=False); b = f["snd_secs_since_last_in"].to_pandas().to_numpy(); c = f["rcv_out_cnt_24h"].to_numpy(zero_copy_only=False)
rng = np.random.default_rng(42)
rand = list(rng.choice(n, 20, replace=False))
nz = [i for i in rng.choice(n, 4000, replace=False) if a[idx[tid[i]]] > 0][:20]
ok = 0
for i in rand + nz:
    mi = (rcv == snd[i]) & (ts < ts[i])
    exp = (int((mi & (ts >= ts[i] - 86400)).sum()), float(ts[i] - ts[mi].max()) if mi.any() else None,
           int(((snd == rcv[i]) & (ts < ts[i]) & (ts >= ts[i] - 86400)).sum()))
    j = idx[tid[i]]
    got = (int(a[j]), None if np.isnan(b[j]) else float(b[j]), int(c[j]))
    ok += exp == got
    if exp != got: print("MISMATCH", tid[i], exp, got)
print(f"v2 brute-force check: {ok}/{len(rand)+len(nz)} match ({len(nz)} chosen with non-zero inbound history)")
