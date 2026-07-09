# SPDX-License-Identifier: Apache-2.0
"""Baseline-vs-optimized benchmark for the LMCache Valkey (GLIDE sync) connector.

This tool measures the retrieval optimizations against a **baseline** of the
pre-optimization connector, with a **>= 2x** large-object (> 1 MB) throughput
target. This tool makes that comparison explicit and statistically
defensible by running the two arms side by side over several repetitions and
reporting the per-rep speedup spread (not a single ratio).

Baseline definition (the "existing connector")
-----------------------------------------------
The optimizations under test are (1) parallel fetch across worker threads
and (2) the zero-copy buffer path. The baseline is the **same connector code
path with both optimizations turned off**:

    baseline    one worker (a single in-flight request at a time, matching the
                pre-optimization connector's serial per-operation behavior) and
                the copy path (``_has_buffer_get = False``).
    optimized   ``--num-workers`` worker threads (parallel fetch) and the
                zero-copy buffer path (``get(key, buffer=...)``).

Toggling the optimizations on the *same* client library, server, payloads, and
keyspace isolates exactly the two levers under test and removes confounds that a
cross-implementation A/B (different client, different wire layout) would
introduce. The pre-#2790 connector issued single in-flight, copy-path
operations, so the single-worker copy arm is a faithful stand-in for it.

This tool reports the *combined* baseline-vs-optimized ratio (both levers on).
To attribute the gain between parallel fetch and the zero-copy path
individually, use ``valkey_microbench.py --compare``, which adds the
intermediate ``N workers + copy`` and zero-copy ablation arms; in practice
nearly all of the large-object gain comes from parallel fetch, with the
zero-copy path being throughput-neutral at these sizes (see ``docs/RESULTS.md``).

Both arms run through the connector's own I/O engine (``_ThreadWorkerPool``), so
the numbers reflect the shipping connector. The two pools are built once and
each is primed and warmed before timing. Within each rep the two arms are then
measured **back to back and interleaved** (baseline GET, optimized GET, baseline
SET, optimized SET), so the per-rep speedup is a ratio of two time-adjacent
measurements taken under the same instantaneous network/server conditions; a
transient that slows one arm tends to slow the other in the same rep rather than
biasing the ratio. The speedup is reported per rep plus min/mean/max, so the
>= 2x claim can be stated as "held in every rep" rather than "on average".

Example::

    python bench_baseline.py --host 10.4.29.98 --port 6379 \\
        --num-workers 8 --num-keys 64 --chunk-mb 4.0 --loops 5 --reps 5

Requires ``valkey-glide-sync`` (>= 2.3) and an importable ``lmcache`` package.
"""

# Standard
from typing import Dict, List, Tuple
import argparse
import gc
import os
import statistics
import time

# First Party
from lmcache.v1.storage_backend.connector.valkey_connector import _ThreadWorkerPool

KEY_PREFIX = "bench:base:"
_ENTROPY_CAP = 1 << 20


def build_payloads(
    num_keys: int, chunk_bytes: int
) -> Tuple[List[str], bytearray]:
    """Keys and the shared write payload."""
    block = os.urandom(min(chunk_bytes, _ENTROPY_CAP))
    payload = bytearray((block * -(-chunk_bytes // len(block)))[:chunk_bytes])
    keys = [f"{KEY_PREFIX}{i}" for i in range(num_keys)]
    return keys, payload


def build_views(num_keys: int, chunk_bytes: int) -> List[memoryview]:
    """A fresh set of per-key reusable read buffers (one set per pool)."""
    return [memoryview(bytearray(chunk_bytes)) for _ in range(num_keys)]


def _get_pass(pool, keys: List[str], views: List[memoryview]) -> None:
    """One GET of every key into its buffer (fan out, then gather).

    ``submit_get_into`` resolves to ``True`` on a hit and ``False`` on a miss or
    error, so a silent miss (which would leave a stale buffer and count as free
    throughput) raises here instead of inflating the measured GiB/s.
    """
    futs = [pool.submit_get_into(k, views[i]) for i, k in enumerate(keys)]
    for i, f in enumerate(futs):
        if not f.result():
            raise RuntimeError(f"GET miss/error for key {keys[i]} during timed pass")


def _set_pass(pool, keys: List[str], payload: bytearray) -> None:
    futs = [pool.submit_set(k, payload) for k in keys]
    for f in futs:
        f.result()


def _gib_per_s(num_keys: int, chunk_bytes: int, loops: int, seconds: float) -> float:
    return num_keys * chunk_bytes * loops / 1024**3 / seconds


def _verify_reads(views: List[memoryview], payload: bytearray) -> None:
    """Fail loudly if any read buffer does not hold the written payload."""
    expected = bytes(payload)
    for i, v in enumerate(views):
        if v.tobytes() != expected:
            raise RuntimeError(f"read verification failed for key index {i}")


def _verify_sample(views: List[memoryview], payload: bytearray) -> None:
    """Cheap per-rep corruption check: verify the first buffer only.

    A full ``_verify_reads`` after every rep would add a per-rep copy that
    biases timing across reps; sampling one buffer catches gross corruption
    without that cost (the full check still runs once after warmup).
    """
    if views and views[0].tobytes() != bytes(payload):
        raise RuntimeError("sampled read verification failed during timed reps")


def _timed_get(pool, keys, views, chunk_bytes, loops) -> float:
    t0 = time.perf_counter()
    for _ in range(loops):
        _get_pass(pool, keys, views)
    return _gib_per_s(len(keys), chunk_bytes, loops, time.perf_counter() - t0)


def _timed_set(pool, keys, payload, chunk_bytes, loops) -> float:
    t0 = time.perf_counter()
    for _ in range(loops):
        _set_pass(pool, keys, payload)
    return _gib_per_s(len(keys), chunk_bytes, loops, time.perf_counter() - t0)


def _prime_and_warm(pool, keys, payload, views) -> None:
    """Prime the keyspace once, then a warm pass (excludes connection setup)."""
    _set_pass(pool, keys, payload)
    _get_pass(pool, keys, views)
    _verify_reads(views, payload)  # guards the copy-vs-buffer toggle is honest


def measure_paired(
    base,
    opt,
    keys: List[str],
    payload: bytearray,
    base_views: List[memoryview],
    opt_views: List[memoryview],
    chunk_bytes: int,
    loops: int,
    reps: int,
) -> Dict[str, Dict[str, List[float]]]:
    """Interleave the two arms within each rep; return per-rep GiB/s per arm."""
    _prime_and_warm(base, keys, payload, base_views)
    _prime_and_warm(opt, keys, payload, opt_views)

    out = {
        "base": {"get": [], "set": []},
        "opt": {"get": [], "set": []},
    }

    def get_base():
        out["base"]["get"].append(_timed_get(base, keys, base_views, chunk_bytes, loops))
        _verify_sample(base_views, payload)

    def get_opt():
        out["opt"]["get"].append(_timed_get(opt, keys, opt_views, chunk_bytes, loops))
        _verify_sample(opt_views, payload)

    def set_base():
        out["base"]["set"].append(_timed_set(base, keys, payload, chunk_bytes, loops))

    def set_opt():
        out["opt"]["set"].append(_timed_set(opt, keys, payload, chunk_bytes, loops))

    for rep in range(reps):
        gc.collect()
        # Counterbalance arm order each rep (ABBA): half the reps run the
        # baseline first and half the optimized first, so monotonic drift,
        # allocator/cache warming, or a server transient does not systematically
        # favor whichever arm always runs second.
        if rep % 2 == 0:
            get_base(); get_opt(); set_base(); set_opt()
        else:
            get_opt(); get_base(); set_opt(); set_base()
    return out


def _summary(values: List[float]) -> Dict[str, float]:
    return {
        "min": min(values),
        "median": statistics.median(values),
        "mean": sum(values) / len(values),
        "max": max(values),
    }


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
    parser.add_argument("--num-workers", type=_positive_int, default=8)
    parser.add_argument("--num-keys", type=_positive_int, default=64)
    parser.add_argument("--chunk-mb", type=_positive_float, default=4.0)
    parser.add_argument("--loops", type=_positive_int, default=10)
    parser.add_argument("--reps", type=_positive_int, default=10)
    args = parser.parse_args()

    if args.chunk_mb <= 1.0:
        print(
            f"warning: --chunk-mb {args.chunk_mb} is not a large object (> 1 MiB); "
            "the >= 2x target is about large-object throughput."
        )

    chunk_bytes = int(args.chunk_mb * 1024 * 1024)
    keys, payload = build_payloads(args.num_keys, chunk_bytes)
    base_views = build_views(args.num_keys, chunk_bytes)
    opt_views = build_views(args.num_keys, chunk_bytes)

    # Baseline: 1 worker (serial, single in-flight) + copy path.
    base = _ThreadWorkerPool(args.host, args.port, 1, "", "")
    # Optimized: N workers (parallel fetch) + zero-copy buffer path.
    opt = _ThreadWorkerPool(args.host, args.port, args.num_workers, "", "")
    try:
        if not opt.has_buffer_get:
            raise RuntimeError(
                "zero-copy buffer GET unavailable (need valkey-glide-sync >= 2.3)"
            )
        # Force the copy path on the baseline; assert the internal flag exists so
        # the benchmark fails loudly if the connector internals move.
        assert hasattr(base, "_has_buffer_get"), (
            "connector internals changed: _ThreadWorkerPool has no _has_buffer_get"
        )
        base._has_buffer_get = False

        res = measure_paired(
            base, opt, keys, payload, base_views, opt_views,
            chunk_bytes, args.loops, args.reps,
        )
        base_res, opt_res = res["base"], res["opt"]
    finally:
        base.close()
        opt.close()

    print(
        f"Baseline vs optimized: {args.host}:{args.port}  chunk={args.chunk_mb}MiB  "
        f"keys={args.num_keys}  loops={args.loops}  reps={args.reps} (ABBA order)\n"
        f"  baseline  = 1 worker, copy path (pre-optimization behavior)\n"
        f"  optimized = {args.num_workers} workers, zero-copy buffer path\n"
        f"  throughput columns are medians (GiB/s); speedup is the paired "
        f"per-rep ratio"
    )
    header = f"{'op':<6}{'baseline':>11}{'optimized':>12}{'paired speedup x':>32}"
    print(header)
    print("-" * len(header))
    all_get_ratios: List[float] = []
    for op in ("get", "set"):
        b = _summary(base_res[op])
        o = _summary(opt_res[op])
        ratios = sorted(o_v / b_v for o_v, b_v in zip(opt_res[op], base_res[op]))
        if op == "get":
            all_get_ratios = ratios
        ratio_str = (
            f"{min(ratios):.2f}-{max(ratios):.2f} "
            f"(median {statistics.median(ratios):.2f}, {len(ratios)} reps)"
        )
        print(
            f"{op.upper():<6}{b['median']:>8.3f}G/s{o['median']:>9.3f}G/s{ratio_str:>32}"
        )

    print("-" * len(header))
    worst = min(all_get_ratios)
    median_ratio = statistics.median(all_get_ratios)
    verdict = "PASS" if worst >= 2.0 else "FAIL"
    # Descriptive verdict: this reports the observed per-rep distribution under a
    # counterbalanced paired design, not a formal confidence bound. "held in
    # every rep" means the worst observed rep still cleared 2x.
    print(
        f"Large-object GET >= 2x target: {verdict} "
        f"(median {median_ratio:.2f}x, worst-rep {worst:.2f}x over "
        f"{len(all_get_ratios)} reps)"
    )


if __name__ == "__main__":
    main()
