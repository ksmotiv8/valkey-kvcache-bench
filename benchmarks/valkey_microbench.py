# SPDX-License-Identifier: Apache-2.0
"""Storage-backend micro-benchmark for the LMCache Valkey (GLIDE sync) connector.

Measures end-to-end client SET / GET / EXISTS throughput (batch submission +
completion, as the connector itself issues it) against a single Valkey server,
isolating the two retrieval optimizations the connector relies on so the
before/after is attributable to each:

    parallel fetch  - N worker threads, each with its own GLIDE sync client,
                      issuing independent round-trips concurrently.
    zero-copy GET   - read the value straight into a caller-owned buffer via
                      valkey-glide ``get(key, buffer=...)`` (PR #5493).  The
                      baseline instead receives an intermediate ``bytes`` and
                      copies it into the destination.

The benchmark drives the connector's own I/O engine (``_ThreadWorkerPool`` from
``lmcache.v1.storage_backend.connector.valkey_connector``) so the numbers
reflect the shipping connector, not a re-implementation.  Timings are
batch-level (submit all keys, wait for all) and therefore include the Python
submission overhead the connector pays in production; they are *not* a
single-op wire latency.

Three configurations are reported with ``--compare`` (each isolates one knob)::

    baseline    1 worker,  copy path     (parallel OFF, zero-copy OFF)
    +parallel   N workers, copy path     (parallel ON,  zero-copy OFF)
    +zero-copy  N workers, buffer GET    (parallel ON,  zero-copy ON)

Each measured loop is preceded by an untimed warmup pass so the reported
medians reflect steady state, not one-time client/connection setup.

Example::

    python valkey_microbench.py --host 10.4.29.98 --port 6379 \\
        --num-workers 32 --num-keys 128 --chunk-mb 4.0 --loops 10 --compare

Requires ``valkey-glide-sync`` (>= 2.3) and an importable ``lmcache`` package.
"""

# Standard
from statistics import mean, median, pstdev
from typing import Dict, List, Tuple
import argparse
import os
import time

# First Party - the connector's real I/O engine
from lmcache.v1.storage_backend.connector.valkey_connector import _ThreadWorkerPool

#: Key namespace for benchmark data (so a FLUSH-free reuse is unambiguous).
KEY_PREFIX = "bench:valkey:"
#: Cap on the random entropy block tiled into each payload (1 MiB).
_ENTROPY_CAP = 1 << 20


def build_payloads(
    num_keys: int, chunk_bytes: int
) -> Tuple[List[str], List[bytearray], List[bytearray], List[memoryview]]:
    """Allocate per-key write payloads, read buffers, and read memoryviews.

    A single random block is tiled into each payload to avoid a slow per-byte
    Python fill; value *content* is not what is under test, only the bytes
    transferred.  Read memoryviews are pre-built so wrapping cost stays out of
    the timed region.
    """
    block = os.urandom(min(chunk_bytes, _ENTROPY_CAP))
    reps = -(-chunk_bytes // len(block))  # ceil division
    template = (block * reps)[:chunk_bytes]
    keys = [f"{KEY_PREFIX}{i}" for i in range(num_keys)]
    write_bufs = [bytearray(template) for _ in range(num_keys)]
    read_bufs = [bytearray(chunk_bytes) for _ in range(num_keys)]
    read_views = [memoryview(b) for b in read_bufs]
    return keys, write_bufs, read_bufs, read_views


def _drain(futures: list) -> list:
    """Block on a list of futures, returning results in submission order."""
    return [f.result() for f in futures]


def _time_set(pool: _ThreadWorkerPool, keys: List[str], bufs: List[bytearray]) -> float:
    t0 = time.perf_counter()
    futs = [pool.submit_set(k, bufs[i]) for i, k in enumerate(keys)]
    _drain(futs)
    return time.perf_counter() - t0


def _time_get(
    pool: _ThreadWorkerPool, keys: List[str], views: List[memoryview]
) -> Tuple[float, list]:
    t0 = time.perf_counter()
    futs = [pool.submit_get_into(k, views[i]) for i, k in enumerate(keys)]
    results = _drain(futs)
    return time.perf_counter() - t0, results


def _time_exists(pool: _ThreadWorkerPool, keys: List[str]) -> Tuple[float, list]:
    t0 = time.perf_counter()
    futs = [pool.submit_exists(k) for k in keys]
    results = _drain(futs)
    return time.perf_counter() - t0, results


def _summary(values: List[float]) -> Dict[str, float]:
    """min / max / mean / median / population-stdev of a non-empty sample."""
    return {
        "min": min(values),
        "max": max(values),
        "mean": mean(values),
        "median": median(values),
        "stdev": pstdev(values) if len(values) > 1 else 0.0,
    }


def bench_config(
    host: str,
    port: int,
    num_workers: int,
    zero_copy: bool,
    keys: List[str],
    write_bufs: List[bytearray],
    read_bufs: List[bytearray],
    read_views: List[memoryview],
    loops: int,
    chunk_bytes: int,
    verify: bool,
) -> Dict[str, Dict[str, float]]:
    """Run ``loops`` SET/GET/EXISTS passes for one connector configuration.

    Returns per-phase throughput summaries: SET/GET in GiB/s, EXISTS in ops/s.
    """
    pool = _ThreadWorkerPool(host, port, num_workers, "", "")
    if zero_copy and not pool.has_buffer_get:
        pool.close()
        raise RuntimeError(
            "zero-copy GET requested but the installed valkey-glide lacks "
            "buffer GET (need PR #5493 / valkey-glide-sync >= 2.3)"
        )
    if not zero_copy:
        # Force the copy path so the baseline does not silently use buffer GET.
        # This intentionally toggles an internal flag; assert it exists so the
        # benchmark fails loudly (not silently) if the connector internals move.
        assert hasattr(pool, "_has_buffer_get"), (
            "connector internals changed: _ThreadWorkerPool has no "
            "_has_buffer_get; update the microbench's copy-path toggle"
        )
        pool._has_buffer_get = False

    total_bytes = len(keys) * chunk_bytes
    set_gibs: List[float] = []
    get_gibs: List[float] = []
    exists_ops: List[float] = []
    try:
        # Prime the keyspace, then run one untimed warmup pass so the first
        # measured loop excludes client/connection setup and cold caches.
        _drain([pool.submit_set(k, write_bufs[i]) for i, k in enumerate(keys)])
        _time_set(pool, keys, write_bufs)
        _time_get(pool, keys, read_views)
        _time_exists(pool, keys)

        for n in range(loops):
            set_s = _time_set(pool, keys, write_bufs)
            get_s, get_results = _time_get(pool, keys, read_views)
            exists_s, hits = _time_exists(pool, keys)
            set_gibs.append(total_bytes / set_s / 1024**3)
            get_gibs.append(total_bytes / get_s / 1024**3)
            exists_ops.append(len(keys) / exists_s)
            if verify and n == 0:
                if not all(get_results):
                    raise RuntimeError("GET reported a miss on primed keys")
                if sum(bool(h) for h in hits) != len(keys):
                    raise RuntimeError("EXISTS reported a miss on primed keys")
                mismatch = [i for i in range(len(keys)) if read_bufs[i] != write_bufs[i]]
                if mismatch:
                    raise RuntimeError(
                        f"GET data mismatch on {len(mismatch)} key(s), "
                        f"first index {mismatch[0]}"
                    )
    finally:
        pool.close()

    return {
        "set_gibs": _summary(set_gibs),
        "get_gibs": _summary(get_gibs),
        "exists_ops": _summary(exists_ops),
    }


def _print_table(rows: List[Tuple[str, Dict[str, Dict[str, float]]]]) -> None:
    """Render per-config medians and each config's GET speedup vs the baseline."""
    header = f"{'config':<26}{'SET GiB/s':>12}{'GET GiB/s':>12}{'EXISTS k/s':>12}"
    print(header)
    print("-" * len(header))
    for label, res in rows:
        s = res["set_gibs"]["median"]
        g = res["get_gibs"]["median"]
        e = res["exists_ops"]["median"] / 1000.0
        print(f"{label:<26}{s:>12.3f}{g:>12.3f}{e:>12.1f}")
    if len(rows) > 1:
        base_get = rows[0][1]["get_gibs"]["median"]
        print("-" * len(header))
        print(f"GET speedup vs '{rows[0][0]}':")
        for label, res in rows[1:]:
            ratio = res["get_gibs"]["median"] / base_get if base_get else float("nan")
            print(f"  {label:<26}{ratio:>6.2f}x")


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
    parser.add_argument("--num-workers", type=_positive_int, default=32)
    parser.add_argument("--num-keys", type=_positive_int, default=128)
    parser.add_argument("--chunk-mb", type=_positive_float, default=4.0)
    parser.add_argument("--loops", type=_positive_int, default=10)
    parser.add_argument(
        "--compare",
        action="store_true",
        help="Run the baseline / +parallel / +zero-copy matrix and report speedup.",
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip first-pass data verification (saves one comparison).",
    )
    args = parser.parse_args()

    chunk_bytes = int(args.chunk_mb * 1024 * 1024)
    keys, write_bufs, read_bufs, read_views = build_payloads(args.num_keys, chunk_bytes)
    verify = not args.no_verify

    print(
        f"Valkey micro-benchmark: {args.host}:{args.port}  "
        f"workers={args.num_workers}  keys={args.num_keys}  "
        f"chunk={args.chunk_mb}MB  loops={args.loops}  "
        f"({args.num_keys * chunk_bytes / 1024**3:.2f} GiB/pass)"
    )

    if args.compare:
        configs = [
            ("baseline (1w, copy)", 1, False),
            (f"+parallel ({args.num_workers}w, copy)", args.num_workers, False),
            (f"+zero-copy ({args.num_workers}w, buf)", args.num_workers, True),
        ]
    else:
        configs = [(f"optimized ({args.num_workers}w, buf)", args.num_workers, True)]

    rows: List[Tuple[str, Dict[str, Dict[str, float]]]] = []
    for label, workers, zero_copy in configs:
        res = bench_config(
            args.host,
            args.port,
            workers,
            zero_copy,
            keys,
            write_bufs,
            read_bufs,
            read_views,
            args.loops,
            chunk_bytes,
            verify,
        )
        rows.append((label, res))

    print()
    _print_table(rows)


if __name__ == "__main__":
    main()
