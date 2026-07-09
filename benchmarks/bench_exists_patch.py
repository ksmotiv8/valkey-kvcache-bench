# SPDX-License-Identifier: Apache-2.0
"""Before/after benchmark for the connector EXISTS pipelining patch.

Measures the consecutive-prefix EXISTS path (used by ``batched_contains`` —
the L2 lookup that gates TTFT) two ways, through the real
``_ThreadWorkerPool``:

    before  - per-key fan-out: one ``submit_exists`` per key, gathered.
    after   - one pipelined GLIDE ``Batch`` + ``exec`` round-trip (the patch).

The patch methods are applied here by monkeypatch so this script validates the
exact change proposed upstream (https://github.com/LMCache/LMCache/pull/3955)
without needing a rebuilt connector.  Both paths are verified to return identical
results before timing.

Example::

    python bench_exists_patch.py --host 10.4.29.98 --port 6379 \\
        --num-workers 8 --num-keys 512 --loops 20
"""

# Standard
from concurrent.futures import Future
from statistics import median
from typing import List
import argparse
import time

# First Party
from lmcache.v1.storage_backend.connector.valkey_connector import _ThreadWorkerPool


def _do_batch_exists(self, key_strs: List[str]) -> List[bool]:
    """Mirror of the patch's pool method: pipelined EXISTS in one round-trip."""
    if not key_strs:
        return []
    # Third Party
    import glide_sync  # type: ignore[import-untyped]

    client = self._get_client()
    batch = glide_sync.Batch(is_atomic=False)
    for key_str in key_strs:
        batch.exists([key_str.encode()])
    results = client.exec(batch, raise_on_error=True)
    if results is None:
        raise RuntimeError("GLIDE Batch.exec returned no results for EXISTS")
    return [bool(r) for r in results]


def _submit_batch_exists(self, key_strs: List[str]) -> Future:
    return self._executor.submit(self._do_batch_exists, key_strs)


# Apply the patch under test (mirror of valkey_connector_batched_exists.patch).
_ThreadWorkerPool._do_batch_exists = _do_batch_exists
_ThreadWorkerPool.submit_batch_exists = _submit_batch_exists


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=6379)
    parser.add_argument("--num-workers", type=int, default=8)
    parser.add_argument("--num-keys", type=int, default=512)
    parser.add_argument("--loops", type=int, default=20)
    args = parser.parse_args()

    pool = _ThreadWorkerPool(args.host, args.port, args.num_workers, "", "")
    keys = [f"exbench:{i}" for i in range(args.num_keys)]
    try:
        # Prime all keys so the full prefix exists.
        for fut in [pool.submit_set(k, bytearray(64)) for k in keys]:
            fut.result()

        def before() -> List[bool]:
            return [f.result() for f in [pool.submit_exists(k) for k in keys]]

        def after() -> List[bool]:
            return pool.submit_batch_exists(keys).result()

        # Correctness: both must agree, and report all keys present.
        b0, a0 = before(), after()
        if b0 != a0 or a0 != [True] * len(keys):
            raise RuntimeError("before/after EXISTS results disagree or missing keys")

        before()  # warmup
        after()

        def med(fn) -> float:
            samples = []
            for _ in range(args.loops):
                t0 = time.perf_counter()
                fn()
                samples.append(time.perf_counter() - t0)
            return median(samples)

        t_before = med(before)
        t_after = med(after)
        n = args.num_keys
        print(
            f"EXISTS prefix-scan: {n} keys, {args.num_workers} workers, "
            f"median of {args.loops} loops"
        )
        print(
            f"  before (per-key fan-out): {t_before * 1000:8.2f} ms = "
            f"{n / t_before / 1000:8.1f}k ops/s"
        )
        print(
            f"  after  (Batch + exec):    {t_after * 1000:8.2f} ms = "
            f"{n / t_after / 1000:8.1f}k ops/s"
        )
        print(f"  speedup: {t_before / t_after:.1f}x")
    finally:
        pool.close()


if __name__ == "__main__":
    main()
