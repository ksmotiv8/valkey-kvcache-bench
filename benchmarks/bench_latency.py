# SPDX-License-Identifier: Apache-2.0
"""Latency benchmark for the LMCache Valkey (GLIDE sync) connector.

Reports the per-operation latency distribution (p50 / p90 / p99 / p99.9 / mean /
max) for SET, GET, and EXISTS through the connector's I/O engine
(``_ThreadWorkerPool``), complementing the throughput benchmarks with the
latency dimension.

Operations are issued **one at a time** (a single worker, sequentially), so each
sample is an isolated request latency — network round-trip + server processing +
client/FFI overhead (the connector's per-op cost is intentionally included) —
rather than amortized batch throughput. GET uses the zero-copy buffer path.
Latency naturally grows with chunk size (transfer time), so the payload is
configurable.

Keys are cycled round-robin over a preloaded keyset, so this reports warm-path
latency; raise ``--num-keys`` to reduce server-side cache locality. The three
ops are measured in a fixed order (SET, GET, EXISTS), each with its own warmup.

Example::

    python bench_latency.py --host 10.4.29.98 --port 6379 \\
        --chunk-mb 1.0 --ops 2000

Requires ``valkey-glide-sync`` (>= 2.3) and an importable ``lmcache`` package.
"""

# Standard
from typing import Callable, Dict, List
import argparse
import math
import os
import time

# First Party
from lmcache.v1.storage_backend.connector.valkey_connector import _ThreadWorkerPool

KEY_PREFIX = "bench:lat:"
_ENTROPY_CAP = 1 << 20


def _stats_ms(samples: List[float]) -> Dict[str, float]:
    """p50/p90/p99/p99.9/mean/max of a latency sample (milliseconds)."""
    ordered = sorted(samples)
    n = len(ordered)

    def pct(q: float) -> float:
        # Nearest-rank percentile: 1-indexed rank ceil(q/100 * n) -> 0-indexed.
        rank = math.ceil(q / 100.0 * n)
        return ordered[min(n - 1, max(0, rank - 1))]

    return {
        "count": n,
        "mean": sum(ordered) / n,
        "p50": pct(50),
        "p90": pct(90),
        "p99": pct(99),
        "p999": pct(99.9),
        "max": ordered[-1],
    }


def _sample(op: Callable[[int], None], ops: int, warmup: int) -> List[float]:
    """Time ``ops`` single operations (after ``warmup`` untimed ones)."""
    for i in range(warmup):
        op(i)
    samples: List[float] = []
    for i in range(ops):
        t0 = time.perf_counter()
        op(i)
        samples.append((time.perf_counter() - t0) * 1000.0)
    return samples


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError(f"must be > 0, got {parsed}")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError(f"must be > 0, got {parsed}")
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=_positive_int, default=6379)
    parser.add_argument("--chunk-mb", type=_positive_float, default=1.0)
    parser.add_argument("--ops", type=_positive_int, default=2000)
    parser.add_argument("--warmup", type=_positive_int, default=200)
    parser.add_argument("--num-keys", type=_positive_int, default=512)
    args = parser.parse_args()

    chunk_bytes = int(args.chunk_mb * 1024 * 1024)
    block = os.urandom(min(chunk_bytes, _ENTROPY_CAP))
    payload = bytearray((block * -(-chunk_bytes // len(block)))[:chunk_bytes])
    keys = [f"{KEY_PREFIX}{i}" for i in range(args.num_keys)]
    read_view = memoryview(bytearray(chunk_bytes))

    # Single worker: one in-flight op at a time → clean per-op latency.
    pool = _ThreadWorkerPool(args.host, args.port, 1, "", "")
    try:
        if not pool.has_buffer_get:
            raise RuntimeError(
                "GLIDE buffer GET unavailable (need valkey-glide-sync >= 2.3)"
            )
        for k in keys:
            pool.submit_set(k, payload).result()

        def do_set(i: int) -> None:
            pool.submit_set(keys[i % len(keys)], payload).result()

        def do_get(i: int) -> None:
            pool.submit_get_into(keys[i % len(keys)], read_view).result()

        def do_exists(i: int) -> None:
            pool.submit_exists(keys[i % len(keys)]).result()

        results = {
            "SET": _stats_ms(_sample(do_set, args.ops, args.warmup)),
            "GET": _stats_ms(_sample(do_get, args.ops, args.warmup)),
            "EXISTS": _stats_ms(_sample(do_exists, args.ops, args.warmup)),
        }
    finally:
        pool.close()

    print(
        f"Latency: {args.host}:{args.port}  chunk={args.chunk_mb}MB  "
        f"ops={args.ops} (warmup {args.warmup}), single in-flight op, ms"
    )
    header = (
        f"{'op':<8}{'p50':>9}{'p90':>9}{'p99':>9}{'p99.9':>9}{'mean':>9}{'max':>9}"
    )
    print(header)
    print("-" * len(header))
    for op_name, s in results.items():
        print(
            f"{op_name:<8}{s['p50']:>9.3f}{s['p90']:>9.3f}{s['p99']:>9.3f}"
            f"{s['p999']:>9.3f}{s['mean']:>9.3f}{s['max']:>9.3f}"
        )


if __name__ == "__main__":
    main()
