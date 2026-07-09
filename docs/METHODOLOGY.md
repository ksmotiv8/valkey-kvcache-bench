# Methodology

How the benchmarks were run, what each one measures, and how to reproduce them.

## Environment

| Component | Detail |
|---|---|
| Client | AWS G6 instance (NVIDIA L4), us-west-2 |
| Server | Valkey **9.1.0**, started with `--io-threads 10`, CPU-pinned to cores 4–14, single node |
| Network | Client → server over the VPC internal address |
| Client library | `valkey-glide-sync` 2.4.1 (provides the `glide_sync` module) |
| LMCache | `dev` branch; the GLIDE-based `ValkeyConnector` |
| Chunk sizes | 4 MB (LMCache "golden spot" for high-throughput transfer, ≈ 16 tokens for Llama-3.1-8B), with 1 MB and 64 KB sweeps |

Server launched as a container pinned to cores 4–14:

```bash
docker run -d --name valkey --cpuset-cpus=4-14 -p 6379:6379 valkey/valkey:9.1 \
  valkey-server --protected-mode no --save '' --appendonly no --io-threads 10 --port 6379
```

## How each connector is driven

The benchmarks drive each connector **the way LMCache drives it in production**,
so the comparison reflects real behavior rather than a synthetic best case:

- **GLIDE connector**: N worker threads, each with its own `glide_sync` client,
  one in-flight operation per thread. Mirrors `ValkeyConnector._batched_get`,
  which submits one operation per key across a `ThreadPoolExecutor` and gathers.
- **RESP connector** (for the comparison): a single `batch_*_sync` call; the C++
  layer fans out across its own worker threads.

## Timing

- All timings use `time.perf_counter`, measured at the batch level: submit all
  keys, then wait for all to complete.
- Each measured loop is preceded by an **untimed warmup pass** so the reported
  medians reflect steady state, not one-time client/connection setup.
- Reported figures are medians over N loops; where a result is sensitive to
  run-to-run variance (notably 1 MB), multiple back-to-back repetitions were run
  and the spread is reported in `docs/RESULTS.md`.

## Baseline definition

"Baseline" is the connector with its retrieval optimizations **disabled** —
1 worker thread (no parallel fetch) on the copy path (no zero-copy buffer GET).
"Optimized" enables them — N workers with zero-copy buffer GET. This isolates
the *retrieval optimizations* under test (parallel fetch, optimal I/O thread
configuration) on identical code and hardware. The pre-#2790 connector issued
single in-flight, copy-path operations, so the 1-worker copy arm is a faithful
stand-in for the "existing connector." (The true legacy 2-key async connector
was lost to a shallow clone; the toggled arm is the cleaner, more conservative
baseline and removes the confounds of a cross-implementation A/B.)

The dedicated baseline tool, `bench_baseline.py`, hardens this comparison so the
≥2× claim is defensible:

- **Counterbalanced arm order (ABBA).** Half the reps run the baseline first and
  half run the optimized first, so monotonic drift, cache/allocator warming, or a
  server transient cannot systematically favor whichever arm always runs second.
- **Paired per-rep speedup.** Each rep's ratio is computed from two time-adjacent
  measurements, so a transient that slows one arm tends to slow the other in the
  same rep rather than skewing the ratio. The result is reported as a spread
  (min–max, median) plus a worst-rep `PASS/FAIL` on the ≥2× target — so the claim
  is stated as "held in every rep," not merely "on average."
- **Honest toggle + verification.** The copy path is forced via the connector's
  internal `_has_buffer_get` flag (guarded by an assertion that fails loudly if
  the internals move); every GET future is checked for a hit and a buffer is
  sampled each rep, so a silent miss cannot be counted as free throughput.

## The benchmarks

The suite covers three dimensions — throughput, latency, and
resource efficiency.

### `bench_baseline.py` (throughput, the headline benchmark)
The dedicated baseline-vs-optimized tool described under
[Baseline definition](#baseline-definition). Reports the paired,
counterbalanced GET/SET speedup at large object sizes and a `PASS/FAIL` on ≥2×.

### `valkey_microbench.py` (throughput)
Drives the connector's internal thread-pool I/O engine directly and reports a
three-config matrix — **baseline (1 worker, copy)**, **+parallel (N workers,
copy)**, **+zero-copy (N workers, buffer GET)** — so each optimization's
contribution is attributable. Supports payload and worker-count sweeps. This is
the tool for the parallelism-vs-zero-copy **ablation** that `bench_baseline.py`
deliberately leaves combined.

### `connector_compare.py`
Runs the GLIDE connector and the RESP connector against the **same** server with
the **same** keys/payloads, each through its native batch path, and reports a
side-by-side table with an explicit "integrated path" caveat (the two batch the
same way each is used in LMCache, not at the raw wire-protocol level).

### `bench_exists_patch.py` (throughput)
Measures the consecutive-prefix `EXISTS` scan (used by `batched_contains`, the L2
lookup that gates TTFT) before and after Patch 1, through the real thread pool,
with a correctness check that both paths return identical results.

### `bench_latency.py` (latency)
Reports the per-operation latency distribution (p50/p90/p99/p99.9/mean/max) for
SET, GET, and EXISTS, issued **one at a time** through a single worker so each
sample is an isolated request latency (round-trip + server + client/FFI cost),
not amortized batch throughput. GET uses the zero-copy buffer path. Percentiles
use the nearest-rank method.

### `bench_resource_efficiency.py` (resource efficiency)
Measures the client-side cost of GET — **CPU-ms per GiB** (`process_time`, all
threads) and process **RSS** — for the copy path vs the zero-copy buffer path.
Each path runs in a **fresh subprocess** so allocator/connection warm-up from one
path cannot bias the other's CPU/RSS.

### `bench_corpus_e2e.py` (end-to-end scenario)
Drives a real **vLLM** server (LMCache V1 + the Valkey connector) over the 30-doc
corpus and measures **TTFT cold vs. L2-cached**. Each document is sent twice — a
cold pass (compute prefill, store KV to Valkey) after an optional `FLUSHALL`,
then a cached pass (reuse). TTFT is the true streamed first-token time, and L2
reuse is attributed per document by the Valkey `keyspace_hits` delta (valid
because the benchmark server is dedicated). Run the vLLM server with
`--no-enable-prefix-caching` and LMCache `local_cpu: false` so every reuse is
forced through Valkey rather than a higher cache tier; the harness reports both
corpus-wide and L2-confirmed-subset statistics. Result: ~10× TTFT reduction
across all 30 docs (3300 ms → 330 ms).

## Document corpus

`corpus/` contains **30 documents** — 15 legal and 15 medical — under
`corpus/legal/` and `corpus/medical/`, indexed by `corpus/manifest.csv`
(domain, filename, title, word/char counts). It provides a representative
workload of long-context documents for end-to-end KV-cache scenarios.

## Reproduction

```bash
# Bulletproof ≥2x anchor (paired, counterbalanced, PASS/FAIL)
python benchmarks/bench_baseline.py --host <host> --port 6379 \
    --num-workers 8 --num-keys 64 --chunk-mb 4.0 --loops 10 --reps 10

# ≥2x and the per-optimization breakdown (ablation)
python benchmarks/valkey_microbench.py --host <host> --port 6379 \
    --num-workers 32 --num-keys 128 --chunk-mb 4.0 --loops 10 --compare

# Worker-count sweep (find the single-node optimum)
for w in 1 4 8 16 32 64; do
  python benchmarks/valkey_microbench.py --host <host> --port 6379 \
    --num-workers $w --num-keys 128 --chunk-mb 4.0 --loops 8
done

# GLIDE vs RESP connector
python benchmarks/connector_compare.py --host <host> --port 6379 \
    --num-workers 8 --num-keys 128 --chunk-mb 4.0 --loops 10

# Patch 1 before/after
python benchmarks/bench_exists_patch.py --host <host> --port 6379 \
    --num-workers 8 --num-keys 512 --loops 20

# Latency distribution (p50/p99) per op
python benchmarks/bench_latency.py --host <host> --port 6379 \
    --chunk-mb 1.0 --ops 2000

# Resource efficiency (client CPU/GiB + RSS), copy vs zero-copy
python benchmarks/bench_resource_efficiency.py --host <host> --port 6379 \
    --num-workers 8 --num-keys 256 --chunk-mb 1.0 --loops 20

# End-to-end corpus TTFT (needs a vLLM server, prefix caching disabled — see
# docs/SCENARIOS.md §10 for the server command)
python benchmarks/bench_corpus_e2e.py --corpus corpus \
    --vllm-url http://localhost:8000 --model Qwen/Qwen2.5-7B-Instruct-AWQ \
    --valkey-host <host> --valkey-port 6379 --flush-l2
```
