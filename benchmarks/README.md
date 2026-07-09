# Benchmark tools

Self-contained tools for measuring the LMCache Valkey connector across three
dimensions: **throughput, latency, and resource efficiency**.
Most report median throughput (GiB/s) or ops/s over N loops with an untimed
warmup pass first. See `../docs/METHODOLOGY.md` for what each measures.

| Tool | Dimension | What it answers |
|---|---|---|
| `bench_baseline.py` | throughput | Is large-object GET ≥2× vs the pre-optimization baseline? (paired, ABBA) |
| `valkey_microbench.py` | throughput | Baseline / +parallel / +zero-copy breakdown; worker & payload sweeps |
| `connector_compare.py` | throughput | GLIDE connector vs RESP connector, same server |
| `bench_exists_patch.py` | throughput | Before/after for the EXISTS-pipelining change ([LMCache#3955](https://github.com/LMCache/LMCache/pull/3955)) |
| `bench_latency.py` | latency | Per-op p50/p90/p99/p99.9 for SET/GET/EXISTS |
| `bench_resource_efficiency.py` | resource | Client CPU-ms/GiB and RSS, copy vs zero-copy |
| `bench_corpus_e2e.py` | end-to-end | vLLM+LMCache TTFT cold-vs-cached over the 30-doc corpus, L2 verified |

## Requirements

- `valkey-glide-sync` ≥ 2.3 (provides `glide_sync`)
- An importable `lmcache` package (the microbench and EXISTS bench drive the
  connector's internal `_ThreadWorkerPool`; `connector_compare.py` also needs an
  `lmcache` built with the C++ Redis extension for the RESP arm)
- A running Valkey 9.x reachable from the client

## `bench_baseline.py`

The headline benchmark: large-object GET/SET for the **pre-optimization baseline**
(1 worker, copy path) vs the **optimized** connector (N workers, zero-copy),
reported as a paired per-rep speedup. Arm order is **counterbalanced (ABBA)**
across reps so neither arm is systematically favored, every GET is hit-checked,
and a buffer is verified each rep. Prints a `PASS/FAIL` on the ≥2× GET target
(worst rep and median).

```bash
python bench_baseline.py --host 10.0.0.1 --port 6379 \
    --num-workers 8 --num-keys 64 --chunk-mb 4.0 --loops 10 --reps 10
```

| Flag | Default | Meaning |
|---|---|---|
| `--num-workers` | 8 | Optimized-arm worker threads (baseline is fixed at 1) |
| `--num-keys` | 64 | Keys per pass |
| `--chunk-mb` | 4.0 | Payload size per key, MiB (use > 1 for the large-object claim) |
| `--loops` | 10 | Measured passes per rep |
| `--reps` | 10 | Counterbalanced reps (paired speedup spread) |

For the parallelism-vs-zero-copy ablation, use `valkey_microbench.py --compare`.

## `valkey_microbench.py`

Three-config matrix (baseline / +parallel / +zero-copy) and worker/payload
sweeps.

```bash
python valkey_microbench.py --host 10.0.0.1 --port 6379 \
    --num-workers 32 --num-keys 128 --chunk-mb 4.0 --loops 10 --compare
```

| Flag | Default | Meaning |
|---|---|---|
| `--num-workers` | 32 | Worker threads (connections) |
| `--num-keys` | 128 | Keys per batch |
| `--chunk-mb` | 4.0 | Payload size per key, MB |
| `--loops` | 10 | Measured loops (median reported) |
| `--compare` | off | Run baseline / +parallel / +zero-copy and report speedup |
| `--no-verify` | off | Skip first-pass data verification |

Without `--compare` it runs a single optimized config — useful for worker sweeps.

## `connector_compare.py`

GLIDE connector vs RESP connector, same server, each via its native batch path.

```bash
python connector_compare.py --host 10.0.0.1 --port 6379 \
    --num-workers 8 --num-keys 128 --chunk-mb 4.0 --loops 10
```

`--backends glide,resp` (default) selects which to run. Output includes an
explicit note that the comparison is the integrated path (GLIDE per-key
thread-pool fan-out vs RESP single native batch), not a raw wire comparison.

## `bench_exists_patch.py`

Before/after for the connector `EXISTS`-pipelining patch (Patch 1), driving the
real thread pool. Applies the patch's pool methods by monkeypatch so it runs
without a rebuilt connector, and verifies both paths return identical results.

```bash
python bench_exists_patch.py --host 10.0.0.1 --port 6379 \
    --num-workers 8 --num-keys 512 --loops 20
```

## `bench_latency.py`

Per-operation latency distribution (p50/p90/p99/p99.9/mean/max) for SET, GET, and
EXISTS, issued **one at a time** (single worker) so each sample is an isolated
request latency rather than amortized batch throughput. GET uses the zero-copy
buffer path. Latency grows with chunk size, so the payload is configurable.

```bash
python bench_latency.py --host 10.0.0.1 --port 6379 \
    --chunk-mb 1.0 --ops 2000 --warmup 200
```

## `bench_resource_efficiency.py`

Client-side cost of GET — **CPU-ms per GiB** (`process_time`, all threads) and
process **RSS** — for the copy path vs the zero-copy buffer path. Each path runs
in a **fresh subprocess** so allocator/connection warm-up from one path cannot
bias the other. The advantage of zero-copy scales with the *number* of values
fetched (allocations avoided); for a few large values it is within run-to-run
noise.

```bash
python bench_resource_efficiency.py --host 10.0.0.1 --port 6379 \
    --num-workers 8 --num-keys 256 --chunk-mb 1.0 --loops 20
```

## `bench_corpus_e2e.py`

End-to-end scenario: drives a real **vLLM** server (LMCache V1 + the Valkey
connector) over the 30-document corpus and measures **TTFT cold vs. L2-cached**.
Each document is sent twice — cold (compute prefill, store KV to Valkey), then
cached (reuse) — with true streamed TTFT and a per-document Valkey
`keyspace_hits` check to confirm L2 reuse. Start the vLLM server with
`--no-enable-prefix-caching` and LMCache `local_cpu: false` so every reuse is
forced through Valkey (see `../docs/SCENARIOS.md` §10 for the server command).

```bash
python bench_corpus_e2e.py --corpus ../corpus \
    --vllm-url http://localhost:8000 --model Qwen/Qwen2.5-7B-Instruct-AWQ \
    --valkey-host 10.0.0.1 --valkey-port 6379 --flush-l2
```

Extra requirement: `redis` (for the `keyspace_hits` check). A sample run is in
`results/corpus_e2e_sample_output.txt` (~10× TTFT reduction across all 30 docs).
