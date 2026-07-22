# Valkey Search HNSW Performance: Across Bundle Releases, and vs Redis

Measured with `valkey-lab search` ([cachecannon PR #116](https://github.com/cachecannon/cachecannon/pull/116)), an open benchmarking harness that reports vector-search performance the way ann-benchmarks does: as points in (recall, latency, throughput) space against precomputed ground truth, never as a lone throughput number.

Author: ksmotiv8. 2026-07-22.

## Executive summary

We benchmarked the Valkey Search (HNSW) module across three valkey-bundle releases, spanning Valkey servers 8.1.2, 8.1.8, and 9.0.3 and search module builds 1.0.0 and 1.0.3, using the ann-benchmarks glove-25-angular dataset (1,183,514 vectors, 25 dimensions, cosine distance).

Headline results:

1. **The query path is remarkably stable across releases.** At every `ef_search` operating point, recall@10 agrees to three decimal places across all three releases, and single-client query throughput agrees within about 3 percent in these runs. We found no regression and no measurable improvement between the three bundles on this workload.
2. **The recall/throughput frontier is healthy.** On one Graviton core driving one connection: recall@10 of 0.80 at about 7,000 QPS (`ef_search` 16), 0.96 at about 3,700 (`ef_search` 64), 0.986 at about 2,300 (`ef_search` 128), and 0.997 at about 1,350 (`ef_search` 256), with p99 latency under 1 ms at every point.
3. **Ingest and index build are consistent across releases**: roughly 320,000 to 425,000 vectors/s pipelined HSET ingest and 39 to 42 s to build the 1.18M-vector HNSW index (M=16, EF_CONSTRUCTION=200) on all three bundles.
4. **Against Redis 8.8.0 on dedicated cross-host hardware** (added campaign, see the cross-host section): recall is effectively identical, Valkey builds the HNSW index about 1.8x faster, Redis shows a markedly cleaner p99.9 tail in that setup, and query throughput splits by operating point.
5. **Operational configuration dominates version choice.** The performance differences we found came not from the module version but from server state and configuration: default RDB persistence wedged the server outright under index churn, and a server with that history ingested 7x slower even after persistence was disabled. Details in Findings.

## Methodology

- **Harness**: `valkey-lab search` (Rust, io_uring based, single binary; see reproduction appendix). Three phases measured independently:
  - *Load*: pipelined `HSET doc:<row> vec <float32 little-endian>` of every train vector, byte-budgeted batches on one connection.
  - *Index*: `FT.CREATE ... VECTOR HNSW ... M 16 EF_CONSTRUCTION 200`, then polling `FT.INFO` until `state` is `ready`, `backfill_in_progress` is 0, and `mutation_queue_size` is 0. Build time is measured from issuing `FT.CREATE` to that ready condition, since the `FT.CREATE` reply returns long before indexing finishes.
  - *Query*: the 10,000 test vectors, one client, one request in flight, `FT.SEARCH` KNN with `EF_RUNTIME`, `NOCONTENT`, `LIMIT 0 k`, `DIALECT 2`. Per-query latency is recorded and recall@k is scored as |returned ∩ true_k| / k with set semantics against the dataset's precomputed neighbors.
- **Dataset**: ann-benchmarks glove-25-angular: 1,183,514 train vectors, 10,000 queries, 25 dimensions, angular metric (cosine on unit-normalized vectors), ground truth depth 100. k=10 throughout.
- **Single-client queries are intentional.** The frontier is measured with one request in flight so that contention never confounds a version comparison. Saturation behavior is a separate question for a separate mode.
- **Host**: one AWS Graviton (aarch64) instance running both the harness and the server containers, one version at a time. Valkey servers run as official `valkey/valkey-bundle` Docker images.
- **Persistence disabled** (`save ""`, `appendonly no`) for every measured run. This is a correctness requirement for this workload, not a tuning preference; see Finding 1.
- **Fresh containers**: every reported number comes from a freshly created container. See Finding 2 for why that matters.
- Each version's frontier is a sweep of `ef_search` in {16, 64, 128, 256}, with a full load and index build per point, so build and ingest numbers are measured four times per version.

## Version matrix

| Bundle image | valkey server | Search module version | Ingest (vectors/s) | Index build (s) |
|---|---|---|---|---|
| valkey/valkey-bundle:8.1.0 | 8.1.2 | 1.0.0 | 388k to 423k | 40.0 to 40.4 |
| valkey/valkey-bundle:8.1.8 | 8.1.8 | 1.0.3 | 391k to 421k | 38.8 to 39.2 |
| valkey/valkey-bundle:9 | 9.0.3 | 1.0.0 | 319k to 407k (438k in a separate control run) | 40.1 to 42.2 * |

\* The very first index build on a fresh 9.0.3 container took 73.7 s, about 1.8x the steady-state 40 s; every subsequent build was normal. We observed this cold-start effect only on the first build after container creation. Numbers in the table exclude that first-build outlier; it is footnoted rather than hidden.

Note the module versions: the 8.1.8 bundle carries the newest search module of the three (1.0.3), while both the 8.1.0 and 9.0.3 bundles carry 1.0.0. "Three versions" here means three released bundle configurations, which is what an operator actually deploys.

## Recall / throughput / latency frontier per release

Single client, k=10, M=16, EF_CONSTRUCTION=200. Latency in milliseconds.

### valkey-bundle:8.1.0 (valkey 8.1.2, module 1.0.0)

| ef_search | recall@10 | QPS | p50 | p99 | p99.9 |
|---|---|---|---|---|---|
| 16 | 0.7976 | 7,183 | 0.134 | 0.202 | 0.774 |
| 64 | 0.9563 | 3,738 | 0.265 | 0.353 | 0.926 |
| 128 | 0.9861 | 2,338 | 0.429 | 0.554 | 1.115 |
| 256 | 0.9968 | 1,369 | 0.737 | 0.951 | 1.479 |

### valkey-bundle:8.1.8 (valkey 8.1.8, module 1.0.3)

| ef_search | recall@10 | QPS | p50 | p99 | p99.9 |
|---|---|---|---|---|---|
| 16 | 0.7994 | 6,774 | 0.142 | 0.210 | 0.975 |
| 64 | 0.9561 | 3,622 | 0.273 | 0.365 | 1.134 |
| 128 | 0.9861 | 2,279 | 0.440 | 0.567 | 1.318 |
| 256 | 0.9972 | 1,335 | 0.755 | 0.986 | 1.726 |

### valkey-bundle:9 (valkey 9.0.3, module 1.0.0)

| ef_search | recall@10 | QPS | p50 | p99 | p99.9 |
|---|---|---|---|---|---|
| 16 | 0.7990 | 7,125 | 0.135 | 0.202 | 0.926 |
| 64 | 0.9562 | 3,680 | 0.269 | 0.354 | 1.122 |
| 128 | 0.9860 | 2,308 | 0.434 | 0.555 | 1.319 |
| 256 | 0.9969 | 1,340 | 0.753 | 0.966 | 1.707 |

## Cross-release comparison

- **Recall**: identical to three decimal places at every operating point across all three releases on this dataset and workload.
- **Query throughput and latency**: within about 3 percent across releases at every point in these runs; 8.1.0 was nominally fastest and 8.1.8 nominally slowest. One frontier sweep per release was run, so small differences at this level should not be over-read.
- **Index build**: 38.8 to 42.2 s everywhere (excluding the fresh-container first-build outlier). The 8.1.8 bundle was consistently about 1 to 2 s faster than the other two; a small but repeatable edge.
- **Ingest**: equivalent across releases on fresh servers.

The practical conclusion for operators: on this workload there is no performance reason to prefer one of these releases over another. Choose based on server features and support lifecycle, and spend the tuning attention on `ef_search`, which moves throughput by 5x across the measured recall range.

## Operational findings

These came out of running the harness at real dataset scale and affected results far more than any version difference.

### 1. Default RDB persistence can wedge the server under index churn

With the bundle image's default persistence settings, repeated cycles of loading 1.18M keys and creating/dropping the HNSW index caused background RDB saves (fork plus copy-on-write) to interact badly with the search module's fork callbacks (the module logs fork-callback warnings). In our runs the server eventually stopped answering PING entirely and had to be restarted. Even before wedging, fork pauses polluted tail latency: we measured a 44.9 ms max query latency with persistence on versus under 2 ms without.

Recommendation: benchmark (and, where the durability tradeoff is acceptable, operate) vector-search workloads with `save ""` and `appendonly no`, and treat persistence-on vector serving as a configuration requiring its own qualification. This behavior likely warrants an upstream valkey-search issue.

### 2. Server-state history distorts ingest by 7x

A server that had been through the persistence wedge and restart ingested at about 61,000 vectors/s, versus 319,000 to 438,000 on fresh containers, and the penalty persisted after persistence was disabled on the recovered server. We did not isolate the root cause (candidates include allocator fragmentation and residual module bookkeeping across many create/drop cycles). The reproducible guidance: benchmark on fresh server instances, and treat ingest throughput measured on a long-churned instance with suspicion.

### 3. Client pitfalls the harness had to engineer around

Recorded here because anyone building tooling against Valkey Search will hit them:

- `SCAN`'s `COUNT` is a hint the server can overshoot severalfold (we observed 1,186 keys returned with `COUNT 256`). Clients with bounded reply parsers (ours caps collections at 1,024 elements) must not assume `COUNT` bounds the reply; we moved bulk key cleanup server-side into a short script so the client only parses a cursor.
- `FT.INFO` in Valkey Search reports `state`, `backfill_in_progress`, and `mutation_queue_size` rather than the `percent_indexed` field familiar from other search implementations. Index-build timing must poll for the three-field ready condition; timing the `FT.CREATE` call measures nothing.
- Vectors must be written as little-endian FLOAT32 bytes. A dtype or endianness mismatch does not error; it silently destroys recall.

## Reproduction

```bash
# Server (one version at a time; persistence off is required, see Finding 1)
docker run -d --name vs -p 6390:6379 valkey/valkey-bundle:9
docker exec vs valkey-cli CONFIG SET save ""
docker exec vs valkey-cli CONFIG SET appendonly no

# Harness (cachecannon PR 116 branch)
git clone https://github.com/cachecannon/cachecannon && cd cachecannon
git checkout search-harness
cargo build --release --features search --bin valkey-lab   # needs cmake >= 3.26

# Dataset
curl -LO http://ann-benchmarks.com/glove-25-angular.hdf5

# One frontier point (repeat for ef_search in 16 64 128 256)
./target/release/valkey-lab search --dataset glove-25-angular.hdf5 \
    --port 6390 --ef-search 128
```

Every run performs its own cleanup, load, and index build, so runs are independent and self-contained.

## Cross-host comparison: Valkey Search vs Redis Query Engine

A second measurement campaign on dedicated hardware, added after the single-host results above: two c8gn.16xlarge (Graviton4, 64 vCPU) instances, harness on one, server on the other, so queries cross a real network hop. Servers run as host-network Docker containers with persistence disabled. Same dataset, parameters, and procedure as above.

These numbers are **not comparable to the single-host tables earlier in this report**: different hardware, a network hop instead of loopback, and a newer Valkey Search module build (the `valkey/valkey-bundle:9` tag had moved; this campaign's module reports version 66049, which decodes as 1.2.1, versus the 1.0.x builds measured above).

| Engine | Image | Server version | Vector search |
|---|---|---|---|
| Valkey | valkey/valkey-bundle:9 | 9.0.3 | Valkey Search module 1.2.1 (encoded 66049) |
| Redis | redis:8 | 8.8.0 | Redis Query Engine (built in) |

### Valkey (cross-host)

| ef_search | recall@10 | QPS | p50 | p99 | p99.9 | build (s) | ingest (vec/s) |
|---|---|---|---|---|---|---|---|
| 16 | 0.8011 | 8,223 | 0.115 | 0.199 | 2.857 | 21.7 | 413k |
| 64 | 0.9573 | 5,079 | 0.192 | 0.246 | 2.939 | 21.4 | 371k |
| 128 | 0.9865 | 3,299 | 0.297 | 0.371 | 3.194 | 21.4 | 349k |
| 256 | 0.9971 | 2,005 | 0.491 | 0.616 | 3.378 | 21.4 | 377k |

### Redis (cross-host)

| ef_search | recall@10 | QPS | p50 | p99 | p99.9 | build (s) | ingest (vec/s) |
|---|---|---|---|---|---|---|---|
| 16 | 0.8011 | 7,051 | 0.140 | 0.159 | 0.301 | 39.8 | 447k |
| 64 | 0.9568 | 5,008 | 0.199 | 0.228 | 0.398 | 37.9 | 429k |
| 128 | 0.9861 | 3,652 | 0.274 | 0.314 | 0.516 | 38.2 | 430k |
| 256 | 0.9970 | 2,397 | 0.419 | 0.487 | 0.684 | 37.8 | 425k |

### What the comparison shows

- **Recall is effectively identical.** At ef_search 16 both engines return 0.8011, and the other points agree within a few parts in ten thousand. In this cross-host sweep, the throughput differences are not explained by recall tradeoffs.
- **Valkey builds the index about 1.8x faster** (21.4 to 21.7 s versus 37.8 to 39.8 s for the same 1.18M vectors). On this 64-vCPU host that difference was consistent across all four builds per engine.
- **Query throughput splits by operating point.** Valkey is about 17 percent faster at ef_search 16 (8,223 vs 7,051 QPS), the two tie at ef_search 64, and Redis is about 11 to 20 percent faster at ef_search 128 and 256. Single sweeps per engine; treat differences under about 10 percent with caution.
- **Redis has a markedly cleaner tail in this setup.** Redis p99.9 stays at 0.3 to 0.7 ms across the sweep, while Valkey shows 2.9 to 3.4 ms p99.9 at every operating point (its p50 and p99 remain competitive, so this is a tail phenomenon, not a shift of the whole distribution). We did not root-cause the Valkey tail; it did not appear in the single-host runs of the older 1.0.x module earlier in this report, so it may be specific to this module build, the cross-host path, or their interaction.
- **Ingest is modestly faster on Redis**, about 8 to 23 percent in these runs (425k to 447k vectors/s versus 349k to 413k), with both engines comfortably fast.

The overall picture matches the rest of this report: the engines are close where it matters most (recall, median latency), and the differences that would drive a choice are workload-specific: index build time favors Valkey, tail latency in this configuration favors Redis.

## Limitations

- Single node, single-client query measurement by design; saturation and cluster behavior are out of scope here.
- One dataset (glove-25-angular). Higher-dimensional and larger corpora may rank versions differently.
- The bundle-release comparison ran in Docker on one Graviton host; the Valkey-vs-Redis comparison ran on a dedicated two-instance client/server pair. Neither set of absolute numbers transfers to other hardware, and the two campaigns are not comparable to each other.
- The bundle-release campaign covers 1.0.x search modules; the cross-host campaign used the newer 1.2.1 module build then current on the bundle:9 tag.
- HNSW only, one (M, EF_CONSTRUCTION) setting; the build-parameter space is unexplored.
