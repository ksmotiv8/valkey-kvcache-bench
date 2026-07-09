# SPDX-License-Identifier: Apache-2.0
"""Side-by-side throughput comparison: LMCache Valkey GLIDE connector vs RESP connector.

Drives both the GLIDE-sync Valkey connector engine (``_ThreadWorkerPool``) and
the C++ RESP connector (``RESPClient``) against the SAME server with the SAME
keys and payloads, so SET / GET / EXISTS throughput is directly comparable.

Both connectors are driven through their real **async** batch paths under one
event loop — the same way ``RemoteConnector`` calls them in production:

    GLIDE  - per-key submit across a thread pool of sync clients, each future
             wrapped with ``asyncio.wrap_future`` and gathered (mirrors
             ``ValkeyConnector._batched_get``).
    RESP   - a single ``batch_*`` coroutine; the C++ layer fans out across its
             own worker threads and signals completion via an eventfd that the
             loop drains.

Both use caller-owned ``memoryview`` buffers (zero-copy receive).  Each backend
runs an untimed warmup pass before the measured loops so the medians reflect
steady state, not one-time client/connection setup.

Example::

    python connector_compare.py --host 10.4.29.98 --port 6379 \\
        --num-workers 8 --num-keys 128 --chunk-mb 4.0 --loops 10

Requires an importable ``lmcache`` built with the C++ Redis extension (for
RESP) and ``valkey-glide-sync`` >= 2.3 (for GLIDE buffer GET).
"""

# Standard
from statistics import mean, median, pstdev
from typing import Dict, List, Tuple
import argparse
import asyncio
import os
import time

KEY_PREFIX = "bench:cmp:"
_ENTROPY_CAP = 1 << 20


def build_payloads(
    num_keys: int, chunk_bytes: int
) -> Tuple[List[str], List[memoryview], List[bytearray], List[memoryview]]:
    """Return keys, write memoryviews, read buffers, and read memoryviews.

    Both connectors accept ``memoryview`` payloads, so wrapping is done once
    here and kept out of the timed region.
    """
    block = os.urandom(min(chunk_bytes, _ENTROPY_CAP))
    reps = -(-chunk_bytes // len(block))  # ceil division
    template = (block * reps)[:chunk_bytes]
    keys = [f"{KEY_PREFIX}{i}" for i in range(num_keys)]
    write_views = [memoryview(bytearray(template)) for _ in range(num_keys)]
    read_bufs = [bytearray(chunk_bytes) for _ in range(num_keys)]
    read_views = [memoryview(b) for b in read_bufs]
    return keys, write_views, read_bufs, read_views


def _summary(values: List[float]) -> Dict[str, float]:
    """min / max / mean / median / population-stdev of a non-empty sample."""
    return {
        "min": min(values),
        "max": max(values),
        "mean": mean(values),
        "median": median(values),
        "stdev": pstdev(values) if len(values) > 1 else 0.0,
    }


class GlideBackend:
    """Valkey GLIDE-sync connector engine, driven via its native per-key fan-out."""

    label = "glide (per-key fan-out)"

    def __init__(self, host: str, port: int, num_workers: int):
        # First Party
        from lmcache.v1.storage_backend.connector.valkey_connector import (
            _ThreadWorkerPool,
        )

        self._pool = _ThreadWorkerPool(host, port, num_workers, "", "")
        if not self._pool.has_buffer_get:
            self._pool.close()
            raise RuntimeError(
                "GLIDE buffer GET unavailable (need valkey-glide-sync >= 2.3)"
            )

    async def set_batch(self, keys: List[str], write_views: List[memoryview]) -> None:
        await asyncio.gather(
            *[
                asyncio.wrap_future(self._pool.submit_set(k, write_views[i]))
                for i, k in enumerate(keys)
            ]
        )

    async def get_batch(
        self, keys: List[str], read_views: List[memoryview]
    ) -> List[bool]:
        return list(
            await asyncio.gather(
                *[
                    asyncio.wrap_future(self._pool.submit_get_into(k, read_views[i]))
                    for i, k in enumerate(keys)
                ]
            )
        )

    async def exists_batch(self, keys: List[str]) -> List[bool]:
        return list(
            await asyncio.gather(
                *[asyncio.wrap_future(self._pool.submit_exists(k)) for k in keys]
            )
        )

    def close(self) -> None:
        self._pool.close()


class RespBackend:
    """C++ RESP connector, driven via its native single-call async batch API."""

    label = "resp (C++ batch)"

    def __init__(self, host: str, port: int, num_workers: int):
        # First Party
        from lmcache.v1.storage_backend.native_clients.resp_client import RESPClient

        # Constructed inside the running loop so RESPClient binds to it.
        self._client = RESPClient(host, port, num_workers)

    async def set_batch(self, keys: List[str], write_views: List[memoryview]) -> None:
        await self._client.batch_set(keys, write_views)

    async def get_batch(
        self, keys: List[str], read_views: List[memoryview]
    ) -> List[bool]:
        await self._client.batch_get(keys, read_views)  # fills buffers in place
        return [True] * len(keys)

    async def exists_batch(self, keys: List[str]) -> List[bool]:
        return await self._client.batch_exists(keys)

    def close(self) -> None:
        self._client.close()


async def bench_backend(
    backend,
    keys: List[str],
    write_views: List[memoryview],
    read_bufs: List[bytearray],
    read_views: List[memoryview],
    loops: int,
    chunk_bytes: int,
    verify: bool,
) -> Dict[str, Dict[str, float]]:
    """Run ``loops`` SET/GET/EXISTS batch passes; return GiB/s and ops/s summaries."""
    total_bytes = len(keys) * chunk_bytes
    set_gibs: List[float] = []
    get_gibs: List[float] = []
    exists_ops: List[float] = []
    try:
        # Prime + one untimed warmup pass.
        await backend.set_batch(keys, write_views)
        await backend.get_batch(keys, read_views)
        await backend.exists_batch(keys)

        # Untimed verification pass.  Zero the read buffers first so a missed
        # key is detectable: a stale buffer left by the warmup GET would
        # otherwise satisfy the byte-compare even if this GET silently missed
        # (this also gives RESP real per-key miss detection, since its
        # batch_get returns no hit vector).
        if verify:
            for rb in read_bufs:
                rb[:] = bytes(len(rb))
            got = await backend.get_batch(keys, read_views)
            hits = await backend.exists_batch(keys)
            if not all(got):
                raise RuntimeError(f"{backend.label}: GET miss on primed keys")
            if sum(bool(h) for h in hits) != len(keys):
                raise RuntimeError(f"{backend.label}: EXISTS miss on primed keys")
            mismatch = [
                i
                for i in range(len(keys))
                if bytes(read_bufs[i]) != bytes(write_views[i])
            ]
            if mismatch:
                raise RuntimeError(
                    f"{backend.label}: GET data mismatch on {len(mismatch)} key(s)"
                )

        for _ in range(loops):
            t0 = time.perf_counter()
            await backend.set_batch(keys, write_views)
            t1 = time.perf_counter()
            await backend.get_batch(keys, read_views)
            t2 = time.perf_counter()
            await backend.exists_batch(keys)
            t3 = time.perf_counter()
            set_gibs.append(total_bytes / (t1 - t0) / 1024**3)
            get_gibs.append(total_bytes / (t2 - t1) / 1024**3)
            exists_ops.append(len(keys) / (t3 - t2))
    finally:
        backend.close()

    return {
        "set_gibs": _summary(set_gibs),
        "get_gibs": _summary(get_gibs),
        "exists_ops": _summary(exists_ops),
    }


def _print_table(rows: List[Tuple[str, Dict[str, Dict[str, float]]]]) -> None:
    header = f"{'backend':<26}{'SET GiB/s':>12}{'GET GiB/s':>12}{'EXISTS k/s':>12}"
    print(header)
    print("-" * len(header))
    for label, res in rows:
        print(
            f"{label:<26}{res['set_gibs']['median']:>12.3f}"
            f"{res['get_gibs']['median']:>12.3f}"
            f"{res['exists_ops']['median'] / 1000.0:>12.1f}"
        )
    if len(rows) == 2:
        print("-" * len(header))
        for phase, unit in (("set_gibs", "SET"), ("get_gibs", "GET")):
            a = rows[0][1][phase]["median"]
            b = rows[1][1][phase]["median"]
            if b:
                print(f"{unit} ratio ({rows[0][0]} / {rows[1][0]}): {a / b:.2f}x")
    print(
        "\nnote: each connector is driven the way LMCache drives it "
        "(glide = per-key threadpool fan-out, resp = single native C++ batch). "
        "Ratios reflect the integrated path, not a raw wire-protocol comparison."
    )


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


async def amain(args: argparse.Namespace) -> None:
    chunk_bytes = int(args.chunk_mb * 1024 * 1024)
    keys, write_views, read_bufs, read_views = build_payloads(args.num_keys, chunk_bytes)
    verify = not args.no_verify

    print(
        f"Connector comparison: {args.host}:{args.port}  "
        f"workers={args.num_workers}  keys={args.num_keys}  "
        f"chunk={args.chunk_mb}MB  loops={args.loops}  "
        f"({args.num_keys * chunk_bytes / 1024**3:.2f} GiB/pass)"
    )

    factories = {"glide": GlideBackend, "resp": RespBackend}
    requested = [b.strip() for b in args.backends.split(",") if b.strip()]
    unknown = [b for b in requested if b not in factories]
    if unknown:
        raise SystemExit(f"unknown backend(s): {unknown}; choose from {list(factories)}")

    rows: List[Tuple[str, Dict[str, Dict[str, float]]]] = []
    for name in requested:
        backend = factories[name](args.host, args.port, args.num_workers)
        res = await bench_backend(
            backend, keys, write_views, read_bufs, read_views, args.loops,
            chunk_bytes, verify,
        )
        rows.append((backend.label, res))

    print()
    _print_table(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=_positive_int, default=6379)
    parser.add_argument("--num-workers", type=_positive_int, default=8)
    parser.add_argument("--num-keys", type=_positive_int, default=128)
    parser.add_argument("--chunk-mb", type=_positive_float, default=4.0)
    parser.add_argument("--loops", type=_positive_int, default=10)
    parser.add_argument(
        "--backends",
        default="glide,resp",
        help="Comma-separated subset of {glide,resp} to run (default: both).",
    )
    parser.add_argument("--no-verify", action="store_true")
    asyncio.run(amain(parser.parse_args()))


if __name__ == "__main__":
    main()
