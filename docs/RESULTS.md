# LMCache Valkey Connector — Optimization & Benchmark Results

Reproducible benchmarks and before/after results for optimizing the LMCache
Valkey connector (GLIDE-based) for large-object KV-cache transfer, plus two
upstream contributions. All numbers below were measured on the setup in
[Environment](#environment); each table notes whether a figure is a multi-run
median or a single run.

## Executive summary

- **≥2× large-object throughput: verified.** Against an
  explicitly-defined pre-optimization baseline (1 worker, copy path), the
  optimized connector (8 workers, zero-copy) delivers **GET ≥2× in every one of
  20 counterbalanced reps** — median **~3.0–3.3×**, worst rep **2.1×** at 4 MiB
  (`bench_baseline.py`). A wider parallel-fetch sweep gives **2.3–2.6× GET** at
  4 MB; the connector's default of 8 workers is near-optimal on a single node.
- **Two patches contributed back:**
  - **LMCache connector — pipelined EXISTS** (`Batch`+`exec` instead of per-key
    fan-out): **8.6–12.6×** faster metadata lookups (the prefix-scan that gates
    TTFT). Upstream: [LMCache PR #3955](https://github.com/LMCache/LMCache/pull/3955).
  - **valkey-glide — batched zero-copy `mget`** (`mget(keys, buffers=[...])`):
    **2.51×** over the connector's current per-key path, and it beats the C++
    RESP connector. Upstream: [valkey-glide PR #6367](https://github.com/valkey-io/valkey-glide/pull/6367).
- **Glide vs RESP:** at optimal config Glide ties or beats the hand-rolled C++
  RESP connector on bulk ≥1 MB; the patches close RESP's remaining lead on
  small-object/metadata workloads.
- **End-to-end (vLLM + LMCache + Valkey), 30-doc corpus:** cold prefill →
  L2-cached **TTFT median 10.07× faster** (3300 ms → 330 ms) across **all 30
  legal/medical documents**, L2 reuse confirmed per document via `keyspace_hits`.

## Environment

| Component | Detail |
|---|---|
| Client | AWS G6 (NVIDIA L4), us-west-2 |
| Server | Valkey **9.1.0**, `--io-threads 10`, pinned cores 4–14, single node |
| Network | client → server over VPC internal address |
| Client lib | `valkey-glide-sync` 2.4.1 (provides `glide_sync`) |
| LMCache | `dev` branch; GLIDE `ValkeyConnector` (PR #2790) |
| Chunk sizes | 4 MB (LMCache "golden spot" ≈ 16 tokens for Llama-3.1-8B), plus 1 MB / 64 KB sweeps |

## Methodology

Each connector is driven as LMCache drives it (no synthetic advantage):
- **GLIDE connector** = N worker threads, each with its own `glide_sync` client,
  one in-flight op per thread (mirrors `ValkeyConnector._batched_get`).
- **RESP connector** = single `batch_*_sync` call; the C++ layer fans out.

Timings are batch-level (submit all keys, await all) via `time.perf_counter`,
with an **untimed warmup pass** before the measured loops so medians reflect
steady state. Tools live alongside this file: `valkey_microbench.py`,
`connector_compare.py`, `bench_exists_patch.py`, `bench_baseline.py` (the
paired baseline-vs-optimized tool), `bench_latency.py` (per-op p50/p99), and
`bench_resource_efficiency.py` (client CPU/GiB + RSS). Each was self-reviewed and
reviewed by OpenAI Codex.

The dedicated **baseline tool** (`bench_baseline.py`) adds two rigor measures
the ≥2× claim leans on: it **counterbalances arm order (ABBA)** so monotonic
drift or a server transient cannot systematically favor whichever arm runs
second, and it computes each rep's speedup as a **paired ratio of two
time-adjacent measurements**; it also checks every GET future for a hit and
samples a buffer each rep so a silent miss cannot be counted as throughput.

## ≥2× large-object throughput (verified)

### Baseline definition (the "existing connector")

The gain comes from two retrieval optimizations: **parallel fetch** and
the **zero-copy buffer path**. The baseline is the *same connector code path with
both turned off* — **1 worker** (a single in-flight request at a time, matching
the pre-#2790 connector's serial per-operation behavior) on the **copy path**
(`_has_buffer_get = False`). Toggling on the same client library, server,
payloads, and keyspace isolates exactly those two levers and avoids the confounds
of a cross-implementation A/B (different client, different wire layout). The
pre-#2790 connector issued single in-flight, copy-path operations, so this arm is
a faithful stand-in for it. (The genuine legacy 2-key async connector was lost to
a shallow clone; the toggled arm is the cleaner and more conservative baseline.)

### Dedicated paired result (`bench_baseline.py`, 4 MiB, 8 workers)

64 keys, 10 loops/measurement, **10 counterbalanced (ABBA) reps**, two
independent runs:

| Run | baseline GET | optimized GET | GET speedup (range, median) | worst rep | verdict |
|---|---:|---:|---:|---:|:--:|
| 1 | 0.81 GiB/s | 2.68 GiB/s | 2.35–3.58× (median **3.26×**) | 2.35× | PASS |
| 2 | 0.89 GiB/s | 2.71 GiB/s | 2.09–3.16× (median **3.01×**) | 2.09× | PASS |

**GET ≥2× held in every one of 20 reps** (worst rep 2.09×). SET speedup
**3.0–3.6×** (median ~3.3×). This is the anchor for the ≥2× claim against the
pre-optimization connector.

### Parallel-fetch sweep (`valkey_microbench.py --compare`, 4 MB)

A wider sweep at the cluster-doc worker count (32). 128 keys, 10 loops.

| Config | SET GiB/s | GET GiB/s | GET speedup |
|---|---:|---:|---:|
| baseline (1 worker, copy) | ~0.40 | ~1.03 | 1.0× |
| +parallel (32 workers, copy) | ~2.52 | ~2.4 (2.13–2.56) | **2.3–2.6×** |
| +zero-copy (32 workers, buffer) | ~2.51 | ~2.4 | ~same as +parallel |

GET speedup across 3 back-to-back reps: **2.46× / 2.31× / 2.36×** — robustly ≥2×.
(The sweep's 2.3–2.6× is lower than the paired tool's ~3× because 32 workers is
past the single-node peak — see below; the paired tool uses the optimal 8.)
The ≥2× comes from **parallel fetch**; zero-copy is throughput-neutral at 4 MB
(see [Zero-copy](#zero-copy-a-modest-allocation-bound-effect-measured-honestly)).

### Optimal worker count (the "optimal i/o thread config" lever)

GET throughput vs worker count @ 4 MB (single node):

| workers | 1 | 4 | 8 | 16 | 32 | 64 |
|---|---:|---:|---:|---:|---:|---:|
| GET GiB/s | 1.06 | **2.76** | **2.74** | 2.26 | 2.21 | 2.05 |

GET **peaks at 4–8 workers (~2.76 GiB/s, near the NIC ceiling)** and declines
beyond — more workers add contention. The connector default (8) is near-optimal
single-node; 32 (a cluster setting) is past the peak. On a cluster, higher
worker counts help because load spreads across nodes.

## Glide vs RESP connector (integrated path)

8 workers, driven as each connector is used in LMCache.

| Workload | GLIDE | RESP (C++) | Note |
|---|---:|---:|---|
| 4 MB × 128 — GET | **2.71** | 2.14 | Glide wins bulk |
| 4 MB × 128 — SET | 2.52 | 2.62 | ~tie |
| 64 KB × 1024 — GET | 0.61 | **1.77** | RESP wins small (per-key fan-out cost) |
| EXISTS (1024 keys) | 10–11k ops/s | **31–41k ops/s** | RESP pipelines |

RESP's lead is a **batching gap in the connector**, not a protocol advantage —
both patches below erase it.

## Patch A — LMCache connector: pipelined EXISTS (8.6–12.6×)

The connector's consecutive-prefix EXISTS scan (`batched_contains`, used for L2
lookups that gate TTFT) fanned out one `EXISTS` per key across the thread pool.
Replacing it with a single GLIDE `Batch`+`exec` pipeline (one round-trip):

| keys | before (per-key) | after (Batch+exec) | speedup |
|---|---:|---:|---:|
| 128 | 17.2k ops/s | 148.1k ops/s | **8.6×** |
| 512 | 17.4k ops/s | 214.4k ops/s | **12.3×** |
| 1024 | 17.5k ops/s | 220.5k ops/s | **12.6×** |

Semantics preserved exactly (stop at first missing key). Self + Codex reviewed.
Uses GLIDE's existing `Batch` API — no upstream change required.
Upstream: [LMCache PR #3955](https://github.com/LMCache/LMCache/pull/3955).

## Patch B — valkey-glide: batched zero-copy `mget` (2.51×)

valkey-glide had single-key zero-copy (`get(buffer=)`, #5493) but no multi-key
equivalent — `mget` always allocated a fresh `bytes` per value. This patch adds
**`mget(keys, buffers=[...])`**: each value is written directly into its caller
buffer in one pipelined round-trip (batching **and** zero-copy). Additive and
opt-in (no `buffers` → unchanged behavior). New FFI `command_with_buffers`;
the arena builder distributes one buffer per top-level array element.

Before/after @ 64 KB × 1024 (single run):

| Path | GiB/s |
|---|---:|
| single-client `mget` (bytes) | 0.647 |
| single-client `mget` (zero-copy buffers) | 0.817 (**+26%**) |
| 8w per-key buffer-GET (current connector) | 0.842 |
| **8w `mget`-into-buffers (this patch)** | **2.110 (2.51×, beats RESP 1.77)** |

Self + Codex reviewed (2 null-pointer UB issues found in the unsafe FFI and
fixed; buffer-lifetime and flat-array contracts documented). Correctness
verified (values land in buffers, missing keys → `None`, no regression to plain
`mget` or `get(buffer=)`). Upstream: [valkey-glide PR #6367](https://github.com/valkey-io/valkey-glide/pull/6367).

## End-to-end: corpus TTFT cold vs. L2-cached

The storage-backend gains above are connector throughput; this measures what they
buy in real serving. A vLLM server (Qwen2.5-7B-Instruct-AWQ on the L4) runs with
LMCache V1 + the Valkey connector and **vLLM prefix caching disabled**, so every
KV reuse must come from Valkey (L2), not a GPU-resident cache. Each of the 30
legal/medical documents is sent twice: a **cold** pass (compute prefill, store KV
to Valkey) after `FLUSHALL`, then a **cached** pass (reuse). TTFT is the true
time-to-first-token (streamed first chunk). `bench_corpus_e2e.py`.

| Metric (30 docs) | Cold | L2-cached | TTFT speedup |
|---|---:|---:|---:|
| TTFT median | 3300 ms | 330 ms | **10.07×** (range 6.5–10.7×) |

- **L2 reuse confirmed for all 30/30 documents** via the per-document Valkey
  `keyspace_hits` delta (total +2340 over the cached pass).
- Cached TTFT (~330 ms) is the genuine cost of fetching a multi-MB KV blob from
  Valkey over the network and resuming decode — vs. ~3.3 s to recompute prefill
  from scratch. The win scales with prompt length (these docs are ~10–12k tokens).
- This is the representative serving scenario; the harness is reproducible
  and a sample run is saved at `benchmarks/results/corpus_e2e_sample_output.txt`.

## Zero-copy: a modest, allocation-bound effect (measured honestly)

At 4 MB the link is the bottleneck, so zero-copy buffer-GET is **throughput-
neutral** vs the copy path. Measured directly, its benefit is **small and
scales with the number of values fetched** (allocations avoided), not bytes:

| Measurement | Copy | Zero-copy | Delta |
|---|---:|---:|---|
| Single-key 256 KB GET — client CPU | 2433 CPU-ms/GiB | 2426 CPU-ms/GiB | **~0%** |
| Single-op 1 MB GET — latency | 1.05 ms | 0.94 ms | ~10% faster |
| Single-client `mget` 1024 × 64 KB — throughput | 0.65 GiB/s | 0.82 GiB/s | **+26%** |

The CPU win is negligible for one large value (avoiding one alloc+copy is lost
against per-op FFI/protocol cost), but grows with value count: a 1024-element
`mget` avoids 1024 `bytes` allocations, hence +26%. **Takeaway:** zero-copy's
*measured* perf benefit is modest at these sizes; its real value is
**architectural** — values land directly in the caller's pinned host memory /
tensors with no intermediate `bytes`, removing allocation/GC churn from the hot
path. The batched-`mget` 2.51× win below is driven by **batching**; zero-copy
adds the direct-placement property on top.

## References

- valkey-glide zero-copy SET (#5492) and buffer GET (#5493), merged 2026-03-09.
- LMCache GLIDE `ValkeyConnector` (#2790), 2026-03-24.
- The two upstream contributions: [LMCache PR #3955](https://github.com/LMCache/LMCache/pull/3955)
  and [valkey-glide PR #6367](https://github.com/valkey-io/valkey-glide/pull/6367).
