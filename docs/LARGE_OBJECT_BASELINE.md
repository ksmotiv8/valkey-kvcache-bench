# Valkey engine baseline: large-object SET/GET (1-64 MB)

Engine-level baseline for large-object performance across five Valkey
releases, measured with a RESP client directly against the server (no LMCache,
no connector). This is the raw-engine reference point for the KV-cache
workloads in the rest of this repo: KV-cache chunks are MB-sized values, and
these tables show what the engine itself delivers at those sizes.

Measured July 2026. Longer-form analysis of the same data:
[Valkey's strongest consistency model is not what you think](https://ksh-public.s3.us-west-2.amazonaws.com/valkey-large-objects/index.html).

## Summary

- GET: Valkey 8.x is limited to roughly 30-33 Gbps for values of 8 MB and
  larger. Valkey 9.0 and 9.1 deliver 190-201 Gbps (network line rate on this
  rig) at every size tested, 1 MB through 64 MB. At 64 MB that is about 6x
  (31 to 191 Gbps), and GET p99 drops from just under 1 s to about 230 ms.
- SET: all versions write near line rate up to 4 MB. From 8 MB up, 8.0
  through 9.0 cluster at 130-140 Gbps; 9.1 adds 11-24% (149-166 Gbps).
- 8.0.9 and 8.1.8 are equivalent on this workload (within run-to-run noise).
- Valkey 7.2 (pre IO-threading rebuild) never exceeds 35 Gbps on GET or 55 Gbps
  on SET at any size tested; the 8.0 threading rebuild is worth roughly 4-6x
  at most sizes. A control with `io-threads-do-reads yes` on 7.2 changed only
  the 1 MB GET cell (32 to 41 Gbps).
- The likely mechanism for the GET change is reply copy avoidance
  ([valkey-io/valkey#2078](https://github.com/valkey-io/valkey/pull/2078),
  new in 9.0); for the SET change, the 9.1 IO threading redesign
  ([valkey-io/valkey#3324](https://github.com/valkey-io/valkey/pull/3324)).
  These are release-to-release comparisons; individual changes were not
  isolated.

## Environment

| Component | Detail |
|---|---|
| Instances | 2x AWS `c8gn.16xlarge` (Graviton4, 64 vCPU, 200 Gbps), same cluster placement group, Amazon Linux 2023 |
| Server | Official `valkey/valkey` Docker images: `7.2` (7.2.13), `8.0.9`, `8.1.8`, `9.0.4`, `9.1.0`; host networking; `--io-threads 16`; cpuset 8-23; `--maxmemory 60gb`; persistence disabled; no TLS |
| Client | valkey-lab ([cachecannon](https://github.com/cachecannon/cachecannon)), io_uring based; 16 threads pinned to cores 4-19; 32 connections; pipeline depth 1 |
| Interrupts | `irqbalance` off on both nodes; ENA NIC at 4 combined queues, IRQs pinned to cores 0-3 |
| Network | Single-flow TCP caps at ~9.5 Gbps on AWS (verified 9.53 Gbps with iperf3), so 32 connections are required to reach line rate |

## Method

- GET and SET measured in separate passes. GET passes prefill a 500-key
  keyspace (16-byte keys) and run at a 100% hit rate. `FLUSHALL` between
  cells.
- Each cell is one 15 s measurement after a 5 s warmup, closed loop.
- Client latency is measured to full response completion, not first byte.
  (Note: `valkey-benchmark` records latency at the first read event of a
  reply, `valkey-benchmark.c:689-692` in 9.1.0, so it under-reports
  large-object latency by orders of magnitude; it is not suitable for these
  tables.)
- During this work we found and fixed a quadratic re-copy bug in the load
  generator's receive path that capped large-value reads
  ([ringline-rs/ringline#275](https://github.com/ringline-rs/ringline/pull/275),
  merged via #279, in cachecannon v0.0.18). All GET numbers below were taken
  with the corrected client. Reproduction note: cachecannon v0.0.18 release
  builds show a residual 64 MB GET shortfall (~145-180 Gbps instead of 200)
  from an unrelated ringline 0.5.x regression; the tables here used a
  corrected ringline 0.4.1-based build that holds line rate at 64 MB.

## GET, 32 connections, 100% hit rate

**Bandwidth, Gbps**

| Value size | 7.2.13 | 8.0.9 | 8.1.8 | 9.0.4 | 9.1.0 |
|---|---|---|---|---|---|
| 1 MB | 32 | 183 | 183 | 200 | 201 |
| 2 MB | 35 | 92* | 55* | 193 | 201 |
| 4 MB | 26 | 41 | 45 | 197 | 201 |
| 8 MB | 21 | 33 | 33 | 201 | 195 |
| 12 MB | 21 | 32 | 32 | 191 | 201 |
| 16 MB | 21 | 32 | 32 | 201 | 200 |
| 32 MB | 21 | 32 | 32 | 196 | 189 |
| 64 MB | 22 | 30 | 31 | 191 | 201 |

**Throughput, requests per second**

| Value size | 7.2.13 | 8.0.9 | 8.1.8 | 9.0.4 | 9.1.0 |
|---|---|---|---|---|---|
| 1 MB | 3,800 | 21,800 | 21,800 | 23,900 | 23,900 |
| 2 MB | 2,100 | 5,500* | 3,300* | 11,500 | 12,000 |
| 4 MB | 772 | 1,200 | 1,300 | 5,900 | 6,000 |
| 8 MB | 319 | 490 | 490 | 3,000 | 2,900 |
| 12 MB | 213 | 320 | 321 | 1,900 | 2,000 |
| 16 MB | 158 | 239 | 241 | 1,500 | 1,500 |
| 32 MB | 79 | 118 | 119 | 732 | 704 |
| 64 MB | 40 | 56 | 57 | 355 | 374 |

**Latency, p99**

| Value size | 7.2.13 | 8.0.9 | 8.1.8 | 9.0.4 | 9.1.0 |
|---|---|---|---|---|---|
| 1 MB | 13 ms | 2.1 ms | 2.2 ms | 2.4 ms | 3.4 ms |
| 2 MB | 20 ms | 17 ms* | 19 ms* | 5.1 ms | 6.6 ms |
| 4 MB | 58 ms | 46 ms | 41 ms | 9.4 ms | 14 ms |
| 8 MB | 174 ms | 113 ms | 112 ms | 26 ms† | 33 ms† |
| 12 MB | 244 ms | 171 ms | 166 ms | 56 ms | 44 ms |
| 16 MB | 329 ms | 238 ms | 238 ms | 56 ms | 58 ms |
| 32 MB | 531 ms | 457 ms | 489 ms | 116 ms | 94 ms |
| 64 MB | 948 ms | 965 ms | 1.02 s | 230 ms | 224 ms |

\* 8.x cells at 2 MB sit on the 8.x throughput cliff and are noisy run to
run; treat them as a 40-90 Gbps band rather than a point.
† Median of four measurements; single 8 MB runs varied 22-30 ms (9.0.4) and
27-41 ms (9.1.0).

## SET, 32 connections

**Bandwidth, Gbps**

| Value size | 7.2.13 | 8.0.9 | 8.1.8 | 9.0.4 | 9.1.0 |
|---|---|---|---|---|---|
| 1 MB | 51 | 201 | 201 | 201 | 201 |
| 2 MB | 53 | 200 | 200 | 200 | 201 |
| 4 MB | 55 | 200 | 200 | 200 | 201 |
| 8 MB | 23 | 135 | 137 | 134 | 166 |
| 12 MB | 23 | 140 | 135 | 139 | 166 |
| 16 MB | 24 | 142 | 137 | 139 | 165 |
| 32 MB | 24 | 134 | 139 | 134 | 159 |
| 64 MB | 24 | 135 | 130 | 134 | 149 |

**Throughput, requests per second**

| Value size | 7.2.13 | 8.0.9 | 8.1.8 | 9.0.4 | 9.1.0 |
|---|---|---|---|---|---|
| 1 MB | 6,000 | 23,900 | 23,900 | 23,900 | 23,900 |
| 2 MB | 3,100 | 11,900 | 11,900 | 11,900 | 12,000 |
| 4 MB | 1,600 | 6,000 | 6,000 | 6,000 | 6,000 |
| 8 MB | 342 | 2,000 | 2,000 | 2,000 | 2,500 |
| 12 MB | 231 | 1,400 | 1,300 | 1,400 | 1,600 |
| 16 MB | 175 | 1,100 | 1,000 | 1,000 | 1,200 |
| 32 MB | 89 | 497 | 516 | 500 | 593 |
| 64 MB | 45 | 250 | 242 | 249 | 277 |

**Latency, p99**

| Value size | 7.2.13 | 8.0.9 | 8.1.8 | 9.0.4 | 9.1.0 |
|---|---|---|---|---|---|
| 1 MB | 8.7 ms | 3.2 ms | 3.3 ms | 3.2 ms | 3.2 ms |
| 2 MB | 17 ms | 6.3 ms | 6.5 ms | 6.7 ms | 5.3 ms |
| 4 MB | 35 ms | 12 ms | 12 ms | 13 ms | 14 ms |
| 8 MB | 164 ms | 43 ms | 41 ms | 49 ms | 34 ms |
| 12 MB | 289 ms | 54 ms | 62 ms | 57 ms | 52 ms |
| 16 MB | 357 ms | 65 ms | 77 ms | 70 ms | 60 ms |
| 32 MB | 1.58 s | 262 ms | 118 ms | 143 ms | 107 ms |
| 64 MB | 2.58 s | 225 ms | 237 ms | 213 ms | 206 ms |

## Scope and caveats

- 7.2.13 was measured in a follow-up session with the identical method and
  client; single run per cell, plus the `io-threads-do-reads` control at
  1/8/64 MB GET.
- Single node, no TLS, persistence disabled, 32 closed-loop connections,
  pipeline depth 1, 100% hit rate. Most cells are a single 15 s measurement
  after warmup.
- One cell deviated on first run (9.1.0, 16 MB GET); it was re-run and
  reproduced line rate in 3 of 4 total measurements. The 9.0.4 vs 9.1.0 GET
  latency comparison at 1/4/8 MB was re-measured three times per cell.
- These results describe this rig and workload. Broader production claims
  need repeated runs and additional configurations (TLS, persistence, mixed
  workloads, cluster mode).

## Reproduction

Server, per version:

```
docker run --network host --cpuset-cpus 8-23 \
  --ulimit nofile=32768:65536 valkey/valkey:<version> \
  --save '' --appendonly no --io-threads 16 \
  --protected-mode no --maxmemory 60gb
```

Client, per cell (GET pass shown; SET pass uses `-r 0:100` and no
`--prefill`):

```
valkey-lab -h <server> -c 32 -P 1 -t 16 --cpu-list 4-19 \
  -n 500 -r 100:0 --prefill --warmup 5s -d 15s \
  -s <value_bytes> --key-size 16
```
