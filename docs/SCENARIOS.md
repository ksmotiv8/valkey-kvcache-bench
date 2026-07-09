# Scenarios to try

Concrete, runnable scenarios for exercising the connector and reproducing the
results. Each lists the **goal**, the **command**, the **expected** outcome
(observed on the reference setup in `METHODOLOGY.md` — your absolute numbers
will vary with hardware and network, but the *ratios* should hold), and what it
**demonstrates**.

Replace `HOST` with your Valkey host. All tools live in `../benchmarks/`.
Prerequisites: `valkey-glide-sync` ≥ 2.3, an importable `lmcache`, and a running
Valkey 9.x. The RESP and `mget`-buffers scenarios have extra prerequisites,
noted inline.

---

## 1. Confirm the ≥2× large-object win

**Goal:** prove the optimized connector delivers ≥2× GET on KV-cache-sized chunks
vs. the explicit pre-optimization baseline, with a defensible PASS/FAIL.

```bash
python benchmarks/bench_baseline.py --host HOST --port 6379 \
    --num-workers 8 --num-keys 64 --chunk-mb 4.0 --loops 10 --reps 10
```

**Expected:** `baseline (1w, copy)` GET ≈ 0.8–0.9 GiB/s; `optimized (8w,
zero-copy)` GET ≈ 2.7 GiB/s → **paired GET speedup median ~3.0–3.3×, worst rep
≥2.1× across 10 counterbalanced (ABBA) reps → `PASS`**. SET ≈ 3.0–3.6×. Re-run;
the worst rep should clear 2× every time.

**Shows:** the retrieval optimizations meet the ≥2× large-object bar against the
"existing connector" baseline, under a counterbalanced paired design (no
arm-order bias, silent misses rejected). For the *parallelism-vs-zero-copy
breakdown*, use Scenario 1b.

---

## 1b. Attribute the gain — parallel fetch vs zero-copy (ablation)

**Goal:** see which optimization the ≥2× comes from.

```bash
python benchmarks/valkey_microbench.py --host HOST --port 6379 \
    --num-workers 32 --num-keys 128 --chunk-mb 4.0 --loops 10 --compare
```

**Expected:** `baseline (1w, copy)` GET ≈ 1.0 GiB/s; `+parallel (32w, copy)` and
`+zero-copy (32w, buf)` GET ≈ 2.2–2.6 GiB/s → **GET speedup ≈ 2.3–2.6×** at 32
workers (past the single-node peak — Scenario 2). The win comes from **parallel
fetch**; zero-copy ≈ parallel at this size.

**Shows:** the ≥2× is a parallel-fetch effect; zero-copy is throughput-neutral at
4 MB (its value is resource/placement — Scenario 9).

---

## 2. Find the optimal worker count (single node)

**Goal:** locate the worker count that maximizes GET on one node.

```bash
for w in 1 4 8 16 32 64; do
  echo "workers=$w"
  python benchmarks/valkey_microbench.py --host HOST --port 6379 \
    --num-workers $w --num-keys 128 --chunk-mb 4.0 --loops 8
done
```

**Expected:** GET rises to a peak at **~4–8 workers (~2.7 GiB/s, near the NIC
ceiling)** then declines (32w ≈ 2.2, 64w ≈ 2.0). SET peaks around 8–16.

**Shows:** the "optimal I/O thread config" lever — ~8 workers is the single-node
sweet spot (the connector default); more workers add contention. On a cluster,
higher counts help because load spreads across nodes.

---

## 3. Payload-size crossover

**Goal:** see how the optimization profile changes from small to large objects.

```bash
for mb in 0.0625 0.25 1.0 4.0; do
  echo "chunk=${mb}MB"
  python benchmarks/valkey_microbench.py --host HOST --port 6379 \
    --num-workers 8 --num-keys 256 --chunk-mb $mb --loops 10 --compare
done
```

**Expected:** at 4 MB the path is link-bound (parallel dominates, zero-copy
neutral). At 64 KB the per-operation overhead dominates and absolute GiB/s drops
sharply — motivating the batched `mget` patch (Scenario 7).

**Shows:** why large chunks (the LMCache "golden spot" ≈ 4 MB) behave very
differently from small-object workloads, and where each optimization pays off.

---

## 4. GLIDE connector vs RESP connector — bulk (≥1 MB)

**Goal:** compare the maintained GLIDE client to a hand-rolled C++ RESP
connector on large objects.
**Prereq:** an `lmcache` built with the C++ Redis extension (for the RESP arm).

```bash
python benchmarks/connector_compare.py --host HOST --port 6379 \
    --num-workers 8 --num-keys 128 --chunk-mb 4.0 --loops 10
```

**Expected:** roughly equal; GLIDE typically **wins 4 MB GET** (≈ 2.7 vs 2.1
GiB/s), SET ≈ tie.

**Shows:** on bulk transfer the connector choice is not the bottleneck — both
saturate the link. GLIDE is competitive *and* maintained (cluster, TLS).

---

## 5. GLIDE vs RESP — small objects & metadata

**Goal:** find where the bespoke C++ connector still leads, and why.

```bash
# small-object GET/SET
python benchmarks/connector_compare.py --host HOST --port 6379 \
    --num-workers 8 --num-keys 1024 --chunk-mb 0.0625 --loops 6
```

**Expected:** RESP ≈ **2.9–3.6× faster** on 64 KB × 1024 (GET/SET) and on
`EXISTS` — because the GLIDE connector fans out per key in Python while RESP
pipelines in C++.

**Shows:** the gap is a *batching* gap in the connector, not a protocol
limitation — exactly what Patches 1 and 2 close (Scenarios 6 and 7).

---

## 6. Connector `EXISTS` pipelining patch — before/after (Patch 1)

**Goal:** measure the metadata-lookup speedup (the L2 prefix-scan that gates
TTFT).

```bash
for n in 128 512 1024; do
  python benchmarks/bench_exists_patch.py --host HOST --port 6379 \
    --num-workers 8 --num-keys $n --loops 20
done
```

**Expected:** before (per-key fan-out) ≈ 17k ops/s flat; after (Batch+exec)
≈ 148k → 220k ops/s → **8.6× at 128 keys, 12.6× at 1024**. Speedup grows with
key count (one pipelined round-trip replaces many).

**Shows:** Patch 1's win, and that it scales with batch size. Apply the patch
itself is upstream: [LMCache PR #3955](https://github.com/LMCache/LMCache/pull/3955).

---

## 7. Batched zero-copy `mget` — before/after (Patch 2)

**Goal:** measure the new `mget(keys, buffers=[...])` against the connector's
current per-key path on small objects.
**Prereq:** a valkey-glide build with `mget(..., buffers=...)`
([valkey-glide PR #6367](https://github.com/valkey-io/valkey-glide/pull/6367));
build the branch and the `glide-sync` FFI.

```python
# scenario_mget_buffers.py — run against a Valkey 9.x with N keys preloaded
import glide_sync, os, time, statistics, threading
from concurrent.futures import ThreadPoolExecutor

HOST, N, SZ, W = "HOST", 1024, 64 * 1024, 8
addr = glide_sync.NodeAddress(HOST, 6379)
mk = lambda: glide_sync.GlideClient.create(
    glide_sync.GlideClientConfiguration(addresses=[addr], request_timeout=15000))
keys = [f"s7:{i}".encode() for i in range(N)]
data = bytes(os.urandom(SZ)); tot = N * SZ
mk().mset({k: data for k in keys})

tl = threading.local()
def cl():
    c = getattr(tl, "c", None)
    if c is None: c = tl.c = mk()
    return c
ex = ThreadPoolExecutor(W); list(ex.map(lambda _: cl(), range(W)))
rbufs = [memoryview(bytearray(SZ)) for _ in range(N)]
shards = [keys[i::W] for i in range(W)]
sbufs = [[memoryview(bytearray(SZ)) for _ in s] for s in shards]

def per_key():   # current connector path
    list(ex.map(lambda i: cl().get(keys[i], buffer=rbufs[i]), range(N)))
def mget_buf():  # Patch 2: each worker mget()s its shard into buffers
    list(ex.map(lambda j: cl().mget(shards[j], buffers=sbufs[j]), range(W)))

def med(fn, it=6):
    per_key() if fn is per_key else mget_buf()  # warmup
    xs = [(_t := time.perf_counter(), fn(), time.perf_counter() - _t)[2] for _ in range(it)]
    return tot / statistics.median(xs) / 1024**3

print(f"per-key buffer-GET (current): {med(per_key):.3f} GiB/s")
print(f"mget-into-buffers (Patch 2) : {med(mget_buf):.3f} GiB/s")
```

**Expected:** per-key ≈ 0.84 GiB/s; `mget`-into-buffers ≈ **2.11 GiB/s → ~2.5×**,
beating the RESP connector (≈ 1.77). Correctness: each value lands byte-exact in
its buffer, missing keys return `None`.

**Shows:** Patch 2 delivers batching **and** zero-copy in one round-trip, closing
the small-object gap from Scenario 5.

---

## 8. NIC / line-rate check

**Goal:** confirm large-object throughput is bounded by the network, not the
client or server.

```bash
python benchmarks/valkey_microbench.py --host HOST --port 6379 \
    --num-workers 8 --num-keys 256 --chunk-mb 4.0 --loops 10
```

While it runs, sample the client NIC (e.g. snapshot `/proc/net/dev` RX bytes at
0.2 s intervals) and convert to Gbps.

**Expected:** GET ≈ 2.7 GiB/s and peak RX near the interface's line rate
(≈ 25 Gbps on the reference setup).

**Shows:** at the optimal worker count, GET is at the NIC ceiling — so further
bulk gains require a faster NIC or sharding across nodes, not a different client.

---

## 9. Resource efficiency — client CPU & memory

**Goal:** quantify the client-side cost of GET (CPU per GiB, RSS) and see where
zero-copy actually helps (it is *not* a bulk-throughput win).

```bash
python benchmarks/bench_resource_efficiency.py --host HOST --port 6379 \
    --num-workers 8 --num-keys 256 --chunk-mb 1.0 --loops 20
```

**Expected:** the copy and zero-copy paths report near-equal **CPU-ms/GiB** and
**RSS** at 1 MB (within run-to-run noise); each path is measured in its own
subprocess so they don't bias each other. The zero-copy advantage grows with the
*number* of values fetched (a 1024-element `mget` avoids 1024 `bytes`
allocations, ≈ +26% — see Scenario 7).

**Shows:** zero-copy's value is **allocation-avoidance / direct placement** into
caller memory (pinned host buffers, tensors), which scales with value count, not
raw GiB/s — measured honestly rather than asserted.

---

## 10. End-to-end with the document corpus (representative KV-cache)

**Goal:** exercise the connector through real LLM serving with the 30 legal/
medical documents and measure TTFT cold vs. L2-cached.
**Prereq:** a vLLM server with `LMCacheConnectorV1` using the Valkey connector.
Start it with **prefix caching disabled** so every reuse comes from Valkey (L2),
not vLLM's GPU cache, and point LMCache at the Valkey server:

```bash
# valkey_single.yaml: remote_url "valkey://HOST:6379", local_cpu false,
#   remote_serde naive, pre_caching_hash_algorithm sha256_cbor_64bit
LMCACHE_CONFIG_FILE=valkey_single.yaml \
vllm serve Qwen/Qwen2.5-7B-Instruct-AWQ --port 8000 --no-enable-prefix-caching \
    --kv-transfer-config '{"kv_connector":"LMCacheConnectorV1","kv_role":"kv_both"}'
```

Then run the harness (it sends each doc cold, then cached, and checks the Valkey
`keyspace_hits` delta per document):

```bash
python benchmarks/bench_corpus_e2e.py --corpus corpus \
    --vllm-url http://localhost:8000 --model Qwen/Qwen2.5-7B-Instruct-AWQ \
    --valkey-host HOST --valkey-port 6379 --flush-l2
```

**Expected:** all 30 docs L2-confirmed; cold TTFT median ≈ **3300 ms**, cached ≈
**330 ms** → **~10× TTFT reduction** (range ~6.5–10.7×), `keyspace_hits` +2340.
A sample run is saved at `benchmarks/results/corpus_e2e_sample_output.txt`.

**Shows:** the storage-backend gains translate into a ~10× end-to-end TTFT
reduction on representative legal/medical workloads, with L2 retrieval
verified per document.

---

## 11. Per-operation latency distribution

**Goal:** report tail latency (p50/p99) per operation, not just throughput.

```bash
for mb in 1.0 4.0; do
  echo "chunk=${mb}MB"
  python benchmarks/bench_latency.py --host HOST --port 6379 \
    --chunk-mb $mb --ops 2000 --warmup 200
done
```

**Expected:** at 1 MB, GET p50 ≈ 1.2 ms / p99 ≈ 1.9 ms; at 4 MB GET p50 ≈ 4.5 ms.
`EXISTS` stays flat (~0.3 ms) regardless of payload (metadata-only). Each op is
issued one at a time (single worker), so these are isolated request latencies.

**Shows:** the per-operation latency dimension — useful for SLO/TTFT budgeting,
complementing the batch-throughput numbers in Scenarios 1–2.
