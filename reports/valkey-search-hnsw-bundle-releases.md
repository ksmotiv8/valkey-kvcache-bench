# Valkey Search HNSW Performance: Across Bundle Releases, and vs Redis

Measured with `valkey-lab search` ([cachecannon PR #116](https://github.com/cachecannon/cachecannon/pull/116)), an open benchmarking harness that reports vector-search performance the way ann-benchmarks does: as points in (recall, latency, throughput) space against precomputed ground truth, never as a lone throughput number.

Author: ksmotiv8. 2026-07-22; round-three update (saturation throughput, PR 1163 module build, node setup) 2026-07-23.

## Executive summary

We benchmarked the Valkey Search (HNSW) module across three valkey-bundle releases, spanning Valkey servers 8.1.2, 8.1.8, and 9.0.3 and search module builds 1.0.0 and 1.0.3, using the ann-benchmarks glove-25-angular dataset (1,183,514 vectors, 25 dimensions, cosine distance).

Headline results:

1. **The query path is remarkably stable across releases.** At every `ef_search` operating point, recall@10 agrees to three decimal places across all three releases, and single-client query throughput agrees within about 3 percent in these runs. We found no regression and no measurable improvement between the three bundles on this workload.
2. **The recall/throughput frontier is healthy.** On one Graviton core driving one connection: recall@10 of 0.80 at about 7,000 QPS (`ef_search` 16), 0.96 at about 3,700 (`ef_search` 64), 0.986 at about 2,300 (`ef_search` 128), and 0.997 at about 1,350 (`ef_search` 256), with p99 latency under 1 ms at every point.
3. **Ingest and index build are consistent across releases**: roughly 320,000 to 425,000 vectors/s pipelined HSET ingest and 39 to 42 s to build the 1.18M-vector HNSW index (M=16, EF_CONSTRUCTION=200) on all three bundles.
4. **Against Redis 8.8.0 on dedicated cross-host hardware** (added campaign, see the cross-host section): recall is effectively identical and Valkey builds the HNSW index about 1.8x faster. In the single-client measurement (one request in flight on both engines, so latency is load-matched by construction and free of queueing), Redis shows a markedly cleaner p99.9 tail and throughput splits by operating point. Valkey's p99.9 is flat at 2.9 to 3.4 ms from 2k through 8.6k QPS, so the tail gap is load-independent rather than an artifact of comparing at different rates. Under concurrency the throughput picture changes entirely; see result 6.
5. **Operational configuration dominates version choice.** The performance differences we found came not from the module version but from server state and configuration: default RDB persistence wedged the server outright under index churn, and a server with that history ingested 7x slower even after persistence was disabled. Details in Findings.
6. **Under concurrency the picture inverts** (round-three campaign): with engine defaults on a 64-vCPU host, Valkey Search scales to about 65,000 QPS while the Redis Query Engine plateaus at about 19,500 (its WORKERS setting is hard-capped at 16 in this build), a 3.3x ceiling difference that persists even with Valkey lowered to the same 16 threads and that flips Redis's single-client advantage at high `ef_search`. A module build from main (with valkey-search PR 1163) cuts Valkey's p99.9 tail by about 30 percent at identical medians.

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
| valkey/valkey-bundle:9 (cross-host †) | 9.1.0 | 1.2.1 (encoded 66049) | 470k to 565k † | 21.4 to 21.5 |
| valkey/valkey:9 + module from main (cross-host †) | 9.1.1 | main @ `578a75a` (includes PR 1163) | 519k to 599k † | 21.6 to 22.0 |
| redis:8 (cross-host †) | 8.8.0 | Redis Query Engine (built in) | 572k to 678k † | 38.6 to 39.3 |

\* The very first index build on a fresh 9.0.3 container took 73.7 s, about 1.8x the steady-state 40 s; every subsequent build was normal. We observed this cold-start effect only on the first build after container creation. Numbers in the table exclude that first-build outlier; it is footnoted rather than hidden.

† Cross-host rows come from the round-three campaign: a different host pair (client and server on separate c8gn.16xlarge instances), a real network hop, and a client with a deeper load pipeline (256 KiB batches versus 8 KiB). Their ingest and build numbers are not comparable to the single-host rows above them; they are included here so every measured engine appears in one place. Full context in the round-three section.

Note the module versions: the 8.1.8 bundle carries the newest search module of the three (1.0.3), while both the 8.1.0 and 9.0.3 bundles carry 1.0.0. "Three versions" here means three released bundle configurations, which is what an operator actually deploys.

## Frontier across releases and engines, one dimension at a time

Single client, k=10, M=16, EF_CONSTRUCTION=200. Latency in milliseconds. Each table compares every measured configuration on one dimension so differences read directly across a row.

Columns come from two campaigns. The first three are the single-host loopback releases: 8.1.0 (valkey 8.1.2, module 1.0.0), 8.1.8 (valkey 8.1.8, module 1.0.3), 9 (valkey 9.0.3, module 1.0.0). The last three, marked †, are the round-three cross-host engines on a separate instance pair with a real network hop: 1.2.1 (valkey 9.1.0, module 1.2.1), main (valkey 9.1.1, module from main with PR 1163), Redis (8.8.0 Query Engine). Compare freely within each group; across the two groups, recall comparisons are fair (recall does not depend on where the client sits) but QPS and latency are not, because hardware and network path differ.

Recall@10:

| ef_search | 8.1.0 | 8.1.8 | 9 | 1.2.1 † | main † | Redis † |
|---|---|---|---|---|---|---|
| 16 | 0.7976 | 0.7994 | 0.7990 | 0.8030 | 0.8004 | 0.7990 |
| 64 | 0.9563 | 0.9561 | 0.9562 | 0.9573 | 0.9573 | 0.9570 |
| 128 | 0.9861 | 0.9861 | 0.9860 | 0.9867 | 0.9862 | 0.9858 |
| 256 | 0.9968 | 0.9972 | 0.9969 | 0.9972 | 0.9969 | 0.9966 |

QPS:

| ef_search | 8.1.0 | 8.1.8 | 9 | 1.2.1 † | main † | Redis † |
|---|---|---|---|---|---|---|
| 16 | 7,183 | 6,774 | 7,125 | 7,965 | 8,567 | 7,206 |
| 64 | 3,738 | 3,622 | 3,680 | 4,996 | 4,942 | 5,099 |
| 128 | 2,338 | 2,279 | 2,308 | 3,287 | 3,290 | 3,703 |
| 256 | 1,369 | 1,335 | 1,340 | 1,990 | 1,981 | 2,402 |

p50 latency:

| ef_search | 8.1.0 | 8.1.8 | 9 | 1.2.1 † | main † | Redis † |
|---|---|---|---|---|---|---|
| 16 | 0.134 | 0.142 | 0.135 | 0.110 | 0.112 | 0.137 |
| 64 | 0.265 | 0.273 | 0.269 | 0.194 | 0.198 | 0.195 |
| 128 | 0.429 | 0.440 | 0.434 | 0.299 | 0.302 | 0.271 |
| 256 | 0.737 | 0.755 | 0.753 | 0.495 | 0.502 | 0.418 |

p99 latency:

| ef_search | 8.1.0 | 8.1.8 | 9 | 1.2.1 † | main † | Redis † |
|---|---|---|---|---|---|---|
| 16 | 0.202 | 0.210 | 0.202 | 0.261 | 0.143 | 0.156 |
| 64 | 0.353 | 0.365 | 0.354 | 0.252 | 0.260 | 0.223 |
| 128 | 0.554 | 0.567 | 0.555 | 0.372 | 0.370 | 0.309 |
| 256 | 0.951 | 0.986 | 0.966 | 0.621 | 0.619 | 0.485 |

p99.9 latency:

| ef_search | 8.1.0 | 8.1.8 | 9 | 1.2.1 † | main † | Redis † |
|---|---|---|---|---|---|---|
| 16 | 0.774 | 0.975 | 0.926 | 2.882 | 2.001 | 0.317 |
| 64 | 0.926 | 1.134 | 1.122 | 3.027 | 2.119 | 0.419 |
| 128 | 1.115 | 1.318 | 1.319 | 3.150 | 2.214 | 0.487 |
| 256 | 1.479 | 1.726 | 1.707 | 3.374 | 2.489 | 0.693 |

† Cross-host campaign, different instance pair and network path; see the column note above. Ingest and index build for every configuration are compared in the version matrix above.

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
- **Redis has a markedly cleaner tail in this setup.** Redis p99.9 stays at 0.3 to 0.7 ms across the sweep, while Valkey shows 2.9 to 3.4 ms p99.9 at every operating point (its p50 and p99 remain competitive, so this is a tail phenomenon, not a shift of the whole distribution). Because these are one-request-in-flight measurements with no queueing, and the Valkey tail is the same at 2,005 QPS as at 8,223 QPS, the gap is load-independent: about one query in a thousand hits a roughly 3 ms stall regardless of rate, and comparing the engines at identical TPS would not close it. We did not root-cause the Valkey tail; it did not appear in the single-host runs of the older 1.0.x module earlier in this report, so it may be specific to this module build, the cross-host path, or their interaction.
- **Ingest is modestly faster on Redis**, about 8 to 23 percent in these runs (425k to 447k vectors/s versus 349k to 413k), with both engines comfortably fast.

The overall picture matches the rest of this report: the engines are close where it matters most (recall, median latency), and the differences that would drive a choice are workload-specific: index build time favors Valkey, tail latency in this configuration favors Redis.

## Round three, same pair: node setup, saturation throughput, and the PR 1163 module build

A third campaign on the same two-instance pair as the cross-host comparison above, run 2026-07-23 with all three engines as containers on the same server host, measured back to back by the same client host. This round adds three things the earlier campaigns lacked: a full node-setup inventory, closed-loop concurrency to find each engine's throughput ceiling, and a build of the Valkey Search module from main containing [valkey-search PR 1163](https://github.com/valkey-io/valkey-search/pull/1163) (SIMD enabled by default in the build plus a rewritten three-phase prefetch loop in the HNSW query path), which no released module carries yet.

These numbers supersede the cross-host tables above where they overlap (same pair, newer engine builds, newer client) and are still not comparable to the single-host bundle-release tables at the top of this report.

### Node setup

Both instances are identical and were used exclusively for this work:

| | Client (runs harness) | Server (runs engines) |
|---|---|---|
| Instance | c8gn.16xlarge (Graviton, 64 vCPU, 123 GB) | c8gn.16xlarge (Graviton, 64 vCPU, 123 GB) |
| Placement | us-west-2c, same cluster placement group | us-west-2c, same cluster placement group |
| OS / kernel | Amazon Linux 2023, 6.18.36 | Amazon Linux 2023, 6.18.36 |
| Docker | 25.0.14, containers on host networking | 25.0.14, containers on host networking |

Client: `valkey-lab search` built against ringline 0.5.3 (which fixes a client send-path hang the earlier campaigns had to work around with 8 KiB load batches; this round pipelines 256 KiB load batches). One event-loop core drives all query connections. Server: three engine containers side by side on host networking, persistence off (`save ""`, `appendonly no`), one engine exercised at a time while the others idle.

### Engines measured

| Engine | Image | Server | Vector search | Query threading (as shipped) |
|---|---|---|---|---|
| Valkey bundle | valkey/valkey-bundle:9 | 9.1.0 | Valkey Search 1.2.1 (encoded 66049) | search reader-threads 64, writer-threads 64 |
| Valkey + main module | valkey/valkey:9 | 9.1.1 | Valkey Search main @ `578a75a` (includes PR 1163) | search reader-threads 64, writer-threads 64 |
| Redis | redis:8 | 8.8.0 | Redis Query Engine (built in) | WORKERS 16 |

Thread counts are the engines' own defaults on this 64-vCPU host; nothing was tuned for the main tables. The asymmetry (64 search threads versus 16 workers) prompted a follow-up equalization experiment, reported after the saturation section, whose short version is: the asymmetry cannot be tuned away on the Redis side (`FT.CONFIG SET WORKERS 64` is rejected with `SEARCH_LIMIT_OVER: Number of worker threads cannot exceed 16` in this build), and equalizing the Valkey side down to 16 threads barely moves its ceiling.

### Single-client frontier

Each cell comes from an independent full cycle (fresh load, fresh index build, then 10,000 queries on one connection with one request in flight). Each table compares the three engines on one dimension. Columns: Bundle (valkey 9.1.0, module 1.2.1), Main (valkey 9.1.1, module `578a75a` with PR 1163), Redis (8.8.0, freshly restarted process).

Recall@10:

| ef_search | Bundle | Main | Redis |
|---|---|---|---|
| 16 | 0.8030 | 0.8004 | 0.7990 |
| 64 | 0.9573 | 0.9573 | 0.9570 |
| 128 | 0.9867 | 0.9862 | 0.9858 |
| 256 | 0.9972 | 0.9969 | 0.9966 |

QPS:

| ef_search | Bundle | Main | Redis |
|---|---|---|---|
| 16 | 7,965 | 8,567 | 7,206 |
| 64 | 4,996 | 4,942 | 5,099 |
| 128 | 3,287 | 3,290 | 3,703 |
| 256 | 1,990 | 1,981 | 2,402 |

p50 latency (ms):

| ef_search | Bundle | Main | Redis |
|---|---|---|---|
| 16 | 0.110 | 0.112 | 0.137 |
| 64 | 0.194 | 0.198 | 0.195 |
| 128 | 0.299 | 0.302 | 0.271 |
| 256 | 0.495 | 0.502 | 0.418 |

p99 latency (ms):

| ef_search | Bundle | Main | Redis |
|---|---|---|---|
| 16 | 0.261 | 0.143 | 0.156 |
| 64 | 0.252 | 0.260 | 0.223 |
| 128 | 0.372 | 0.370 | 0.309 |
| 256 | 0.621 | 0.619 | 0.485 |

p99.9 latency (ms):

| ef_search | Bundle | Main | Redis |
|---|---|---|---|
| 16 | 2.882 | 2.001 | 0.317 |
| 64 | 3.027 | 2.119 | 0.419 |
| 128 | 3.150 | 2.214 | 0.487 |
| 256 | 3.374 | 2.489 | 0.693 |

Index build (s), four independent builds per engine:

| | Bundle | Main | Redis |
|---|---|---|---|
| build range | 21.4 to 21.5 | 21.6 to 21.7 | 38.6 to 38.8 |

![Recall vs throughput frontier: Valkey Search 1.2.1, Valkey Search main with PR 1163, and Redis Query Engine 8.8 on the same axes](assets/frontier-valkey-vs-redis.png)

The single-client shape repeats the earlier cross-host round: recall parity everywhere, Valkey ahead at ef 16, a tie at ef 64, Redis ahead by 11 to 21 percent at ef 128 and 256, and Redis's p99.9 five to seven times cleaner than the bundle module's. Run-to-run spread at ef 16 was about 8 percent across repeats (7,965 to 8,624 for the bundle), so treat single-digit percentage differences accordingly.

### The PR 1163 module build changes tails, not medians, on this workload

Median latency and QPS for the main-branch module are within 2 to 3 percent of released 1.2.1 at every operating point: no measurable median gain here. What did move is the tail: p99.9 dropped from 2.9 to 3.4 ms (1.2.1) to 2.0 to 2.5 ms at every ef point, roughly 30 percent, consistent across three separate rounds of runs, and the p99 at ef 16 dropped from 0.261 to 0.143 ms. The PR's own benchmarks measured 11 to 16 percent median improvements on 1024-dimensional vectors on an AMD host; at 25 dimensions the distance arithmetic is a far smaller share of query time, so median parity on this dataset is a plausible outcome rather than a contradiction. The tail improvement makes the main-branch module the best Valkey tail behavior we have measured, though still about 4x the Redis tail in this configuration.

### Closed-loop throughput: Valkey's ceiling is about 3.3x Redis's

Same procedure, but N connections each run closed loop with one request in flight (per-request latencies pooled across connections; the single client core was verified not to be the bottleneck at these rates). QPS and p99 (ms) by connection count:

ef_search 16:

| Clients | Bundle QPS | p99 | Main QPS | p99 | Redis QPS | p99 |
|---|---|---|---|---|---|---|
| 2 | 16,510 | 0.228 | 16,890 | 0.154 | 11,417 | 0.196 |
| 4 | 30,103 | 0.256 | 30,920 | 0.203 | 17,927 | 0.297 |
| 8 | 50,644 | 0.261 | 51,152 | 0.217 | 19,058 | 0.681 |
| 16 | 57,581 | 0.438 | 60,094 | 0.430 | 19,084 | 1.360 |
| 32 | 63,353 | 0.776 | 64,906 | 0.783 | 19,548 | 2.526 |
| 64 | 62,718 | 1.199 | 64,556 | 1.166 | 19,500 | 3.699 |

ef_search 128:

| Clients | Bundle QPS | p99 | Main QPS | p99 | Redis QPS | p99 |
|---|---|---|---|---|---|---|
| 2 | 6,572 | 0.385 | 6,666 | 0.365 | 7,295 | 0.322 |
| 4 | 12,658 | 0.466 | 12,743 | 0.427 | 13,016 | 0.484 |
| 8 | 22,315 | 0.598 | 23,120 | 0.589 | 17,993 | 0.649 |
| 16 | 40,254 | 0.655 | 39,574 | 0.644 | 18,858 | 1.193 |
| 32 | 60,544 | 0.782 | 58,055 | 0.798 | 19,415 | 2.657 |
| 64 | 67,495 | 1.728 | 64,096 | 1.868 | 19,581 | 4.952 |

![Throughput vs concurrency at ef 16 and ef 128: both Valkey builds climb to roughly 65k QPS while Redis plateaus at its 19.5k worker ceiling](assets/ladder-valkey-vs-redis.png)

What the ladder shows:

- **Redis hits a hard ceiling at about 19.5k QPS** from 8 connections onward at ef 16 (and from 16 connections at ef 128), at both operating points, which matches its Query Engine's WORKERS value of 16, the maximum this build accepts (see the equalization section). Past the ceiling, added concurrency converts entirely into queueing: p99 climbs from 0.7 ms to 5 ms while QPS stays flat, and its formerly clean tail disappears (p50 alone reaches 3.1 to 3.3 ms at 64 connections).
- **Both Valkey builds scale to roughly 65k QPS** (peak observed 67.5k, bundle at ef 128 with 64 connections), about 3.3x the Redis ceiling, while holding p99 at 1.2 to 1.9 ms at 64 connections. The 64 reader threads as shipped simply cover more of the 64-vCPU host.
- **The crossover flips the single-client story.** Redis's ef 128 single-client advantage disappears by 8 connections; from there Valkey's throughput advantage grows monotonically.
- At saturation the two Valkey builds are equivalent within a few percent; PR 1163's tail advantage persists but compresses (pooled p99.9 at 64 connections: 3.1 to 3.5 ms versus 4.0 to 4.3 ms for 1.2.1).

**Read latencies at equal load, not equal connection count.** These are closed-loop ladders, so a row compares equal concurrency, not equal throughput: at 64 connections Valkey is serving about 64k QPS while Redis is serving about 19.5k, and Redis's multi-millisecond latencies there are saturation queueing, not the latency an operator would see at a sane operating point. Matching rungs by achieved QPS instead (ef 128): at about 7k QPS, Redis p99 0.322 ms versus Valkey 0.385; at about 13k, 0.484 versus 0.466; at 18k to 22k, 0.649 versus 0.598. Below the Redis ceiling, iso-load latencies are close, with Redis slightly ahead at light load. The engines separate not on latency at a given load but on the range of loads they can serve at all: Valkey holds sub-millisecond p99 out to roughly 60k QPS, while Redis has no operating point above roughly 19.5k. (The single-client frontier tables are load-matched by construction, one request in flight on both engines, so their latency comparisons carry no such caveat.)

### Thread equalization: the ceiling gap is architectural, not a thread-count artifact

The obvious objection to the ladder is that 64 search threads against 16 workers is not a fair fight. Two follow-up measurements address it:

- **The Redis side cannot be raised.** `FT.CONFIG SET WORKERS 64` (and any value above 16) is rejected with `SEARCH_LIMIT_OVER: Number of worker threads cannot exceed 16` on this redis:8 build. The 19.5k ceiling is not a conservative default; it is the maximum this build allows.
- **Lowering Valkey to the same 16 threads barely moves its ceiling.** `search.reader-threads` is runtime-tunable; we set it to 16 (verified as exactly 16 live `read-worker` threads in the server process) and re-ran the ladder on the bundle engine:

| Clients | ef 16 QPS | p99 | ef 128 QPS | p99 |
|---|---|---|---|---|
| 4 | 31,319 | 0.179 | 12,335 | 0.472 |
| 8 | 53,797 | 0.230 | 22,516 | 0.574 |
| 16 | 65,210 | 0.409 | 39,906 | 0.632 |
| 32 | 71,876 | 0.686 | 59,436 | 0.760 |
| 64 | 74,085 | 1.048 | 60,185 | 3.356 |

At equal thread counts, Valkey's ceiling is 60k to 74k QPS against Redis's 19.5k: still 3.1x to 3.8x. The per-thread arithmetic is stark: roughly 4,600 QPS per reader thread at ef 16 versus roughly 1,200 QPS per worker. The throughput gap is architectural, not a thread-budget artifact.

Two second-order observations from the same run: at ef 16, sixteen threads actually outperforms the 64-thread default (74.1k versus 64.9k peak, with lower p99), suggesting the default oversubscribes for cheap queries; at ef 128 with 64 connections the smaller pool starts to queue (p99 rises from 1.7 to 3.4 ms and peak drops about 11 percent), so the default earns its keep on expensive queries. Reader threads were restored to 64 afterward.

### Ingest, build, and memory

- Ingest (pipelined HSET from one client connection, 256 KiB batches): Redis 572k to 678k vectors/s, Valkey main 519k to 599k, Valkey bundle 470k to 565k. The deeper client pipeline enabled by the ringline 0.5.3 fix raised everyone's numbers relative to the earlier campaigns (which measured 319k to 447k with 8 KiB batches); Redis keeps its modest ingest lead.
- Index build is unchanged from the earlier round: Valkey 21.4 to 22.0 s, Redis 38.6 to 39.3 s, about 1.8x in Valkey's favor, for the same 1.18M x 25-dim HNSW (M 16, EF_CONSTRUCTION 200).
- Memory after load plus index: Redis 1.02 GiB, Valkey bundle 1.30 GiB, Valkey main 1.31 GiB. Redis holds the same dataset and index in about 22 percent less memory.

### Measurement notes

- **A harness artifact was found and excluded.** This round added an index-reuse mode to the harness so throughput ladders would not reload 1.18M vectors per rung. In that mode only, against Redis only, single-connection runs showed a false latency floor of about 0.26 ms whenever the request rate would otherwise exceed roughly 4k QPS (a connection that has not carried the bulk load traffic behaves differently; root cause on the client side, still under investigation). All single-client numbers in this section therefore come from full-cycle runs, where the effect does not occur; ladder rungs at 2 or more connections were verified unaffected.
- **Long-running server processes drift.** The Redis container that had been serving benchmark churn for hours showed degraded single-client numbers at low ef (ef 64 dropped from about 5,000 to 3,830 QPS) that recovered fully on process restart. This is the query-path sibling of Finding 2 (server-state history distorts ingest); the numbers above are from a freshly restarted process, and the practice of restarting engines before measuring is worth adopting generally.
- One dataset, one host pair, engine defaults as shipped for the main tables; the thread asymmetry is addressed by the equalization experiment above. Saturation was measured with closed-loop clients on one event-loop core; open-loop arrival patterns may place the knee differently.

## Limitations

- Frontier sweeps are single-client by design; the round-three campaign adds closed-loop saturation on the same pair, but open-loop arrival patterns and cluster behavior remain out of scope.
- One dataset (glove-25-angular). Higher-dimensional and larger corpora may rank versions differently.
- The bundle-release comparison ran in Docker on one Graviton host; the Valkey-vs-Redis comparison ran on a dedicated two-instance client/server pair. Neither set of absolute numbers transfers to other hardware, and the two campaigns are not comparable to each other.
- The bundle-release campaign covers 1.0.x search modules; the cross-host campaign used the newer 1.2.1 module build then current on the bundle:9 tag.
- HNSW only, one (M, EF_CONSTRUCTION) setting; the build-parameter space is unexplored.
