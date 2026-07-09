# SPDX-License-Identifier: Apache-2.0
"""Resource-efficiency benchmark for the LMCache Valkey (GLIDE sync) connector.

Measures the *client-side cost* of GET — CPU time per GiB transferred and
process resident memory — for the copy path vs the zero-copy buffer-GET path.
This complements the throughput benchmarks: it quantifies the
resource-efficiency dimension rather than raw GiB/s.

The two paths:

    copy       client receives an intermediate ``bytes`` per value, then copies
               it into the destination buffer (one allocation per GET).
    zero-copy  client reads the value straight into a caller-owned buffer via
               valkey-glide ``get(key, buffer=...)`` (no per-GET allocation).

Both are driven through the connector's own I/O engine (``_ThreadWorkerPool``)
so the numbers reflect the shipping connector. Each path is measured in a
**fresh subprocess** so allocator state and warmed client/connection state from
one path do not bias the other's CPU/RSS. Expect the advantage of zero-copy to
be small and to scale with the *number* of values fetched (allocations avoided);
for a few large values it is typically within run-to-run noise.

Reported per path: GET throughput (GiB/s), client CPU per GiB (``process_time``,
all threads), and process RSS during the run (``/proc/self/statm`` — whole
process, including the connector's worker threads and glide client).

Example::

    python bench_resource_efficiency.py --host 10.4.29.98 --port 6379 \\
        --num-workers 8 --num-keys 256 --chunk-mb 1.0 --loops 20

Requires ``valkey-glide-sync`` (>= 2.3) and an importable ``lmcache`` package.
"""

# Standard
from typing import Dict, List, Tuple
import argparse
import gc
import multiprocessing as mp
import os
import time

KEY_PREFIX = "bench:res:"
_ENTROPY_CAP = 1 << 20


def build_payloads(
    num_keys: int, chunk_bytes: int
) -> Tuple[List[str], List[bytearray], List[memoryview]]:
    """Per-key write payloads plus reusable read buffers/memoryviews."""
    block = os.urandom(min(chunk_bytes, _ENTROPY_CAP))
    template = (block * -(-chunk_bytes // len(block)))[:chunk_bytes]
    keys = [f"{KEY_PREFIX}{i}" for i in range(num_keys)]
    write_bufs = [bytearray(template) for _ in range(num_keys)]
    read_views = [memoryview(bytearray(chunk_bytes)) for _ in range(num_keys)]
    return keys, write_bufs, read_views


def _current_rss_mb() -> float:
    """Resident set size of this process in MiB, from /proc/self/statm."""
    page = os.sysconf("SC_PAGE_SIZE")
    with open("/proc/self/statm", "r", encoding="ascii") as fh:
        resident_pages = int(fh.read().split()[1])
    return resident_pages * page / 1024**2


def _run_gets(pool, keys: List[str], views: List[memoryview]) -> None:
    futs = [pool.submit_get_into(k, views[i]) for i, k in enumerate(keys)]
    for f in futs:
        f.result()


def measure(
    host: str,
    port: int,
    num_workers: int,
    zero_copy: bool,
    num_keys: int,
    chunk_bytes: int,
    loops: int,
) -> Dict[str, float]:
    """Run ``loops`` GET passes for one path; return CPU/GiB, GiB/s, and RSS.

    Builds its own payloads and pool so it can run standalone in a subprocess.
    """
    # First Party
    from lmcache.v1.storage_backend.connector.valkey_connector import _ThreadWorkerPool

    keys, write_bufs, read_views = build_payloads(num_keys, chunk_bytes)
    pool = _ThreadWorkerPool(host, port, num_workers, "", "")
    try:
        if zero_copy and not pool.has_buffer_get:
            raise RuntimeError(
                "zero-copy GET requested but the installed valkey-glide lacks "
                "buffer GET (need valkey-glide-sync >= 2.3)"
            )
        if not zero_copy:
            # Force the copy path; assert the internal flag exists so the
            # benchmark fails loudly if the connector internals move.
            assert hasattr(pool, "_has_buffer_get"), (
                "connector internals changed: _ThreadWorkerPool has no "
                "_has_buffer_get; update the copy-path toggle"
            )
            pool._has_buffer_get = False

        # Prime the keyspace, then warm up (excludes connection/cache setup).
        for f in [pool.submit_set(k, write_bufs[i]) for i, k in enumerate(keys)]:
            f.result()
        _run_gets(pool, keys, read_views)

        gc.collect()
        cpu0 = time.process_time()
        wall0 = time.perf_counter()
        for _ in range(loops):
            _run_gets(pool, keys, read_views)
        cpu_s = time.process_time() - cpu0
        wall_s = time.perf_counter() - wall0
        rss_mb = _current_rss_mb()
    finally:
        pool.close()

    total_gib = num_keys * chunk_bytes * loops / 1024**3
    return {
        "gib_per_s": total_gib / wall_s,
        "cpu_ms_per_gib": cpu_s * 1000.0 / total_gib,
        "rss_mb": rss_mb,
    }


def _worker(queue, kwargs: dict) -> None:
    try:
        queue.put(("ok", measure(**kwargs)))
    except Exception as exc:  # surfaced as a RuntimeError in the parent
        queue.put(("err", f"{type(exc).__name__}: {exc}"))


def measure_isolated(ctx, **kwargs) -> Dict[str, float]:
    """Run ``measure`` in a fresh subprocess so CPU/RSS are not cross-biased."""
    queue = ctx.Queue()
    proc = ctx.Process(target=_worker, args=(queue, kwargs))
    proc.start()
    status, payload = queue.get()
    proc.join()
    if status == "err":
        raise RuntimeError(payload)
    return payload


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
    parser.add_argument("--num-keys", type=_positive_int, default=256)
    parser.add_argument("--chunk-mb", type=_positive_float, default=1.0)
    parser.add_argument("--loops", type=_positive_int, default=20)
    args = parser.parse_args()

    chunk_bytes = int(args.chunk_mb * 1024 * 1024)
    ctx = mp.get_context("spawn")  # fresh interpreter per path

    print(
        f"Resource efficiency: {args.host}:{args.port}  workers={args.num_workers}  "
        f"keys={args.num_keys}  chunk={args.chunk_mb}MB  loops={args.loops}  "
        f"(each path measured in an isolated subprocess)"
    )
    header = f"{'path':<22}{'GET GiB/s':>12}{'CPU ms/GiB':>14}{'RSS MiB':>12}"
    print(header)
    print("-" * len(header))
    rows = []
    for label, zero_copy in (("copy", False), ("zero-copy (buffer)", True)):
        r = measure_isolated(
            ctx,
            host=args.host,
            port=args.port,
            num_workers=args.num_workers,
            zero_copy=zero_copy,
            num_keys=args.num_keys,
            chunk_bytes=chunk_bytes,
            loops=args.loops,
        )
        rows.append((label, r))
        print(
            f"{label:<22}{r['gib_per_s']:>12.3f}{r['cpu_ms_per_gib']:>14.1f}"
            f"{r['rss_mb']:>12.1f}"
        )
    if len(rows) == 2:
        copy_cpu = rows[0][1]["cpu_ms_per_gib"]
        zc_cpu = rows[1][1]["cpu_ms_per_gib"]
        if copy_cpu:
            print("-" * len(header))
            print(
                f"client CPU reduction (zero-copy vs copy): "
                f"{(1 - zc_cpu / copy_cpu) * 100:.0f}%"
            )


if __name__ == "__main__":
    main()
