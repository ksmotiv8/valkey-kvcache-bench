# valkey-kvcache-bench

Reproducible benchmarks for **KV-cache workloads on Valkey**, measured through
the [LMCache](https://github.com/LMCache/LMCache) Valkey connector (built on
[valkey-glide](https://github.com/valkey-io/valkey-glide)).

LLM serving stores transformer KV-cache blobs in Valkey to avoid recomputing
prefill. Unlike typical Valkey usage (values under 1 MB), these are large
objects (multiple MB per chunk), which stresses different parts of the client
stack. This suite measures that workload end to end: connector throughput,
per-operation latency, client resource cost, and real time-to-first-token
through vLLM.

Two optimizations found with this suite are proposed upstream:
**[LMCache/LMCache#3955](https://github.com/LMCache/LMCache/pull/3955)**
(pipelined batched EXISTS) and
**[valkey-io/valkey-glide#6367](https://github.com/valkey-io/valkey-glide/pull/6367)**
(zero-copy multi-key `mget`). The suite includes the before/after benchmarks
for both.

## Results at a glance

Client: AWS G6 (NVIDIA L4). Server: Valkey 9.1.0, 10 I/O threads, single node.

| Metric | Result |
|---|---|
| GET, 4 MiB: baseline vs optimized connector (8 workers) | ~0.85 vs ~2.7 GiB/s (**over 2x in every one of 20 paired reps**, median ~3x) |
| Optimal worker count (single node) | ~8 (GET peaks ~2.76 GiB/s, near NIC line rate) |
| EXISTS prefix-scan, pipelined vs per-key ([LMCache#3955](https://github.com/LMCache/LMCache/pull/3955)) | 17k vs **220k ops/s** (12.6x at 1024 keys) |
| Batched zero-copy `mget` vs per-key GET, 64 KB x 1024 ([glide#6367](https://github.com/valkey-io/valkey-glide/pull/6367)) | 0.84 vs **2.11 GiB/s** (2.51x) |
| End-to-end TTFT, 30-doc corpus: cold vs Valkey-cached | 3300 vs **330 ms** (~10x, all 30 docs verified) |

Full tables and caveats: [`docs/RESULTS.md`](docs/RESULTS.md).

## The tools

| Tool | Dimension | What it answers |
|---|---|---|
| `bench_baseline.py` | throughput | Is large-object GET at least 2x vs the pre-optimization baseline? (paired, counterbalanced, prints PASS/FAIL) |
| `valkey_microbench.py` | throughput | Which optimization contributes what; worker-count and payload sweeps |
| `connector_compare.py` | throughput | GLIDE connector vs a hand-rolled C++ RESP connector, same server |
| `bench_exists_patch.py` | throughput | Before/after for pipelining the EXISTS metadata scan |
| `bench_latency.py` | latency | Per-op p50/p90/p99/p99.9 for SET/GET/EXISTS |
| `bench_resource_efficiency.py` | resource | Client CPU per GiB and RSS, copy vs zero-copy |
| `bench_corpus_e2e.py` | end-to-end | Cold vs cached TTFT through vLLM + LMCache over the document corpus, with per-document Valkey hit verification |

## Quick start

Requires `valkey-glide-sync` (2.3 or newer), an importable `lmcache`, and a
running Valkey 9.x.

```bash
pip install lmcache valkey-glide-sync

# Headline: baseline vs optimized, paired ABBA design, PASS/FAIL on the 2x target
python benchmarks/bench_baseline.py --host <valkey-host> --port 6379 \
    --num-workers 8 --num-keys 64 --chunk-mb 4.0 --loops 10 --reps 10

# Latency distribution per op
python benchmarks/bench_latency.py --host <valkey-host> --port 6379 \
    --chunk-mb 1.0 --ops 2000
```

Expected `bench_baseline.py` tail on the reference setup:

```
GET      0.81G/s    2.68G/s   2.35-3.58 (median 3.26, 10 reps)
Large-object GET >= 2x target: PASS (median 3.26x, worst-rep 2.35x over 10 reps)
```

See [`benchmarks/README.md`](benchmarks/README.md) for every tool's flags,
[`docs/METHODOLOGY.md`](docs/METHODOLOGY.md) for the environment and how each
number is produced, and [`docs/SCENARIOS.md`](docs/SCENARIOS.md) for eleven
copy-paste scenarios with expected results.

## Repository layout

```
valkey-kvcache-bench/
├── benchmarks/     The seven tools, plus results/ with a sample e2e run
├── corpus/         30 synthetic legal/medical documents + manifest, plus the
│                   two-stage LLM generator that produced them (see corpus/README.md)
├── docs/           RESULTS, METHODOLOGY, SCENARIOS
└── LICENSE         MIT
```

## Related upstream work

Two optimizations measured by this suite are proposed upstream:

- LMCache: pipelined batched EXISTS for the prefix scan that gates TTFT
  ([LMCache/LMCache#3955](https://github.com/LMCache/LMCache/pull/3955))
- valkey-glide: zero-copy `buffers` argument for the sync client's `mget`
  ([valkey-io/valkey-glide#6367](https://github.com/valkey-io/valkey-glide/pull/6367))

## License

MIT. See [`LICENSE`](LICENSE).
