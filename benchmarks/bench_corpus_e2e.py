# SPDX-License-Identifier: Apache-2.0
"""End-to-end KV-cache benchmark over the legal/medical document corpus.

This harness provides a representative end-to-end scenario over a corpus of 30
legal/medical documents. It drives a real vLLM server backed
by LMCache + the Valkey connector and measures the metric that matters for LLM
serving — **time-to-first-token (TTFT) cold vs. cached** — using the documents in
``corpus/`` as prompts, and verifies the cached path actually came from Valkey by
checking the server's ``keyspace_hits`` delta.

What it does
------------
1. Reads the corpus manifest (``corpus/manifest.csv``) and loads each document.
   Each prompt is the document text, optionally repeated up to ``--min-chars`` so
   short documents still span several LMCache chunks (chunk size 256 tokens);
   repeating a document's *own* text keeps every prompt's content distinct, so no
   two documents share a chunk-prefix hash (which would cause cross-document
   cache hits and confound the measurement).
2. **Cold pass** — sends every document once. vLLM computes the prefill KV and
   LMCache stores it to Valkey (L2). Records cold TTFT per document.
3. **Cached pass** — sends every document again. Documents whose KV is no longer
   resident in vLLM's GPU prefix cache are reloaded from Valkey by the LMCache
   connector. Records cached TTFT and the **per-document** ``keyspace_hits``
   delta, so an L2 hit is attributed to the specific document rather than assumed.

Run vLLM with LMCache V1 + the Valkey connector first, e.g.::

    LMCACHE_CONFIG_FILE=valkey_single.yaml \\
    vllm serve Qwen/Qwen2.5-7B-Instruct-AWQ --port 8000 \\
        --kv-transfer-config '{"kv_connector":"LMCacheConnectorV1","kv_role":"kv_both"}'

where ``valkey_single.yaml`` points ``remote_url`` at the Valkey server. Then::

    python bench_corpus_e2e.py --corpus ../corpus --vllm-url http://localhost:8000 \\
        --model Qwen/Qwen2.5-7B-Instruct-AWQ --valkey-host 10.4.29.98 --valkey-port 6379

Requires ``requests`` and ``redis`` (for the keyspace_hits check). To force every
reuse through L2 (deterministic), disable vLLM GPU prefix caching on the server
(``--no-enable-prefix-caching``) or set LMCache ``local_cpu: false``.
"""

# Standard
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import argparse
import csv
import json
import statistics
import sys
import time

# Third Party
import requests


def load_corpus(corpus_dir: Path, limit: Optional[int]) -> List[Tuple[str, str]]:
    """Return [(doc_id, text), ...] from the corpus manifest.

    Each row's path is resolved to a unique file under ``corpus_dir``. Manifest
    paths in this repo are repo-relative (e.g. ``corpus/legal/doc_0000.txt``), so
    a leading ``<corpus_dir-name>/`` segment is stripped before joining. Paths
    are resolved exactly — never by basename — so ``legal/doc_0000.txt`` and
    ``medical/doc_0000.txt`` cannot collide onto the same file. Falls back to
    globbing ``*/*.txt`` only when no manifest is present.
    """
    manifest = corpus_dir / "manifest.csv"
    docs: List[Tuple[str, str]] = []
    seen = set()
    if manifest.exists():
        with manifest.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                rel = _row_path(row)
                if rel is None:
                    continue
                path = _resolve_under(corpus_dir, rel)
                if path is None or not path.exists():
                    print(f"  WARNING: manifest path not found, skipping: {rel}")
                    continue
                key = str(path.resolve())
                if key in seen:  # guard against duplicate rows
                    continue
                seen.add(key)
                doc_id = str(path.relative_to(corpus_dir))
                docs.append((doc_id, path.read_text(encoding="utf-8")))
    if not docs:
        for path in sorted(corpus_dir.glob("*/*.txt")):
            docs.append((str(path.relative_to(corpus_dir)), path.read_text("utf-8")))
    if limit:
        docs = docs[:limit]
    return docs


def _row_path(row: Dict[str, str]) -> Optional[str]:
    """Pick the most path-like column from a manifest row."""
    for key in ("path", "filepath", "file", "filename", "doc", "document"):
        for col, val in row.items():
            if col and col.strip().lower() == key and val:
                return val.strip()
    return None


def _resolve_under(corpus_dir: Path, rel: str) -> Optional[Path]:
    """Resolve a manifest path to a unique file inside ``corpus_dir``.

    Strips a leading ``<corpus_dir-name>/`` so repo-relative manifest paths land
    under ``corpus_dir``; refuses to escape the corpus directory.
    """
    parts = Path(rel).parts
    if parts and parts[0] == corpus_dir.name:
        parts = parts[1:]
    if not parts:
        return None
    candidate = corpus_dir.joinpath(*parts)
    try:
        candidate.resolve().relative_to(corpus_dir.resolve())
    except ValueError:
        return None  # path escapes the corpus dir
    return candidate


def _pad_to_min_chars(text: str, min_chars: int) -> str:
    """Repeat a document's own text until it reaches ``min_chars``.

    Keeps content distinct per document (no shared synthetic preamble) so chunk
    hashes do not collide across documents.
    """
    if len(text) >= min_chars or not text:
        return text
    reps = -(-min_chars // len(text))  # ceil
    return (text * reps)[:min_chars]


def _keyspace_hits(host: str, port: int) -> int:
    """Current cumulative keyspace_hits on the Valkey server (-1 on error)."""
    # Third Party
    import redis

    try:
        r = redis.Redis(host=host, port=port, socket_timeout=5)
        hits = int(r.info("stats").get("keyspace_hits", 0))
        r.close()
        return hits
    except Exception as exc:  # noqa: BLE001 - reported, not fatal
        print(f"  WARNING: keyspace_hits query failed for {host}:{port} — {exc}")
        return -1


def _lmcache_hit_tokens(metrics_url: str) -> int:
    """Cumulative ``lmcache:num_hit_tokens`` from LMCache's /metrics (-1 on error).

    The local tiers have no server-side counter like Valkey's ``keyspace_hits``,
    so reuse on those tiers is attributed from LMCache's own Prometheus counter.
    Requires the server to run with ``internal_api_server_enabled: true``.
    """
    # Every LMCache metric is exported once per role (scheduler and worker) and
    # per worker id, so a single counter is several labelled lines. Summing
    # them is what makes this a total: taking the first match can land on the
    # scheduler's copy, which never moves, and the tier then looks like it
    # served nothing. prometheus_client also appends _total to counters and
    # emits a _created gauge holding a unix timestamp -- that one must be
    # excluded or it swamps the sum.
    #
    # Returning 0 for "metric not found" would be indistinguishable from a real
    # zero delta; -1 makes the harness report "?" rather than a false "no".
    try:
        resp = requests.get(f"{metrics_url.rstrip('/')}/metrics", timeout=5)
        resp.raise_for_status()
        lines = [ln for ln in resp.text.splitlines() if ln and not ln.startswith("#")]
        for prefix in ("lmcache:num_hit_tokens", "lmcache_num_hit_tokens",
                       "lmcache:num_retrieve_requests"):
            total, found = 0.0, False
            for line in lines:
                name = line.split("{")[0].split(" ")[0]
                if not name.startswith(prefix) or name.endswith("_created"):
                    continue
                try:
                    total += float(line.rsplit(" ", 1)[1])
                    found = True
                except (IndexError, ValueError):
                    continue
            if found:
                return int(total)
        names = sorted({ln.split("{")[0].split(" ")[0]
                        for ln in lines if ln.startswith("lmcache")})
        print(f"  WARNING: no hit-token counter at {metrics_url}. "
              f"lmcache metrics present: {names[:8] or 'none'}")
        return -1
    except Exception as exc:  # noqa: BLE001 - reported, not fatal
        print(f"  WARNING: LMCache metrics query failed for {metrics_url} - {exc}")
        return -1


def _tier_hits(args: argparse.Namespace) -> int:
    """Cumulative reuse counter for the tier under test (-1 on error)."""
    if args.backend == "valkey":
        return _keyspace_hits(args.valkey_host, args.valkey_port)
    return _lmcache_hit_tokens(args.lmcache_metrics_url)


def _flush_l2(host: str, port: int) -> None:
    """Flush the Valkey L2 store before the cold pass.

    The Valkey server persists across runs, so KV stored by a previous run would
    make a document's "cold" pass a silent L2 hit (cold TTFT ~= cached, speedup
    ~1x). Flushing removes that *L2* confound. Note it does NOT clear vLLM's GPU
    prefix cache — for a fully clean cold baseline and to guarantee the cached
    pass is served from Valkey (not a higher tier), run the server with vLLM
    prefix caching disabled (``--no-enable-prefix-caching``) and LMCache
    ``local_cpu: false`` (see the module docstring).
    """
    # Third Party
    import redis

    r = redis.Redis(host=host, port=port, socket_timeout=10)
    r.flushall()
    r.close()
    print(f"  flushed Valkey L2 at {host}:{port}")


def _send(url: str, model: str, prompt: str, timeout: int) -> float:
    """Return the time-to-first-token (ms) for a streamed completion.

    Uses ``stream=True`` and timestamps the **first** SSE data chunk, so the
    measurement is true TTFT (prefill + first decode step) and excludes
    full-response serialization/body transfer. Prefill — the KV-cache
    build/reuse the connector affects — dominates TTFT for long prompts.
    """
    payload = {
        "model": model,
        "prompt": prompt,
        "max_tokens": 1,
        "temperature": 0,
        "stream": True,
    }
    t0 = time.perf_counter()
    with requests.post(
        f"{url}/v1/completions", json=payload, timeout=timeout, stream=True
    ) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if line and line.startswith(b"data:") and b"[DONE]" not in line:
                return (time.perf_counter() - t0) * 1000.0
    # No token streamed (unexpected): fall back to total elapsed.
    return (time.perf_counter() - t0) * 1000.0


def _wait_for_server(url: str, model: str, timeout_s: int) -> None:
    """Block until the vLLM server answers a trivial completion."""
    deadline = time.perf_counter() + timeout_s
    last = ""
    while time.perf_counter() < deadline:
        try:
            r = requests.get(f"{url}/v1/models", timeout=5)
            if r.ok:
                return
            last = f"HTTP {r.status_code}"
        except Exception as exc:  # noqa: BLE001
            last = str(exc)
        time.sleep(2)
    print(f"ERROR: vLLM server at {url} not ready after {timeout_s}s ({last})")
    sys.exit(1)


def _tier_label(args: argparse.Namespace) -> str:
    """Human-readable name of the tier under test, for run headers."""
    if args.backend == "valkey":
        return f"valkey {args.valkey_host}:{args.valkey_port}"
    return f"local_{args.backend}"


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError(f"must be > 0, got {parsed}")
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--corpus", required=True, help="Path to the corpus/ dir")
    parser.add_argument("--vllm-url", default="http://localhost:8000")
    parser.add_argument(
        "--cached-vllm-url",
        help="run the cached pass against a SECOND vLLM node (fleet mode). The "
        "local tiers cannot serve a node that did not compute the KV, so this is "
        "what separates a shared tier from a node-local one. Defaults to --vllm-url",
    )
    parser.add_argument("--model", required=True, help="served model name")
    parser.add_argument(
        "--backend", choices=("valkey", "cpu", "disk"), default="valkey",
        help="storage tier under test; selects how cached-pass reuse is verified",
    )
    parser.add_argument("--valkey-host", help="required for --backend valkey")
    parser.add_argument("--valkey-port", type=_positive_int, default=6379)
    parser.add_argument(
        "--lmcache-metrics-url", default="http://localhost:6999",
        help="LMCache internal API server; source of the num_hit_tokens delta "
        "for the cpu/disk tiers",
    )
    parser.add_argument(
        "--min-chars", type=_positive_int, default=6000,
        help="repeat short docs up to this many chars (~1500 tokens)",
    )
    parser.add_argument("--limit", type=int, default=0, help="cap docs (0 = all)")
    parser.add_argument(
        "--flush-l2", action="store_true",
        help="FLUSHALL the Valkey L2 before the cold pass (removes the L2 confound; "
        "does not clear vLLM's GPU prefix cache)",
    )
    parser.add_argument(
        "--json", dest="json_path",
        help="also write the run to this path as JSON, for cross-tier comparison",
    )
    parser.add_argument("--timeout", type=_positive_int, default=600)
    parser.add_argument(
        "--settle", type=_positive_int, default=3,
        help="seconds to wait between cold and cached passes",
    )
    args = parser.parse_args()
    if args.backend == "valkey" and not args.valkey_host:
        parser.error("--valkey-host is required for --backend valkey")
    if args.flush_l2 and args.backend != "valkey":
        parser.error(
            "--flush-l2 only applies to --backend valkey; clear the local tier by "
            "removing the local_disk path and restarting the server"
        )

    corpus_dir = Path(args.corpus)
    docs = load_corpus(corpus_dir, args.limit or None)
    if not docs:
        print(f"ERROR: no documents found under {corpus_dir}")
        sys.exit(1)
    prompts = [(doc_id, _pad_to_min_chars(text, args.min_chars)) for doc_id, text in docs]

    url = args.vllm_url.rstrip("/")
    cached_url = (args.cached_vllm_url or args.vllm_url).rstrip("/")
    _wait_for_server(url, args.model, args.timeout)
    if cached_url != url:
        _wait_for_server(cached_url, args.model, args.timeout)
    print(
        f"E2E corpus benchmark: {len(prompts)} docs  model={args.model}\n"
        f"  vLLM={url}  tier={_tier_label(args)}  "
        f"min_chars={args.min_chars}"
        + (f"\n  cached pass against {cached_url} (fleet mode)"
           if cached_url != url else "")
    )

    # ── Optional: flush L2 so the cold pass is genuinely cold for every doc ──
    if args.flush_l2:
        print("\n=== Flush L2 ===")
        _flush_l2(args.valkey_host, args.valkey_port)

    # ── Cold pass: compute + store KV to L2 ──
    print(f"\n=== Cold pass (compute prefill, store to {_tier_label(args)}) ===")
    cold: Dict[str, float] = {}
    for doc_id, prompt in prompts:
        cold[doc_id] = _send(url, args.model, prompt, args.timeout)
        print(f"  {doc_id:<28} cold TTFT {cold[doc_id]:8.1f} ms")
    time.sleep(args.settle)

    # ── Cached pass: reuse KV (L2 hit when not served by a higher tier) ──
    # keyspace_hits is a *global* Valkey counter; the per-doc before/after delta
    # attributes an L2 hit to a document only because this is a dedicated
    # benchmark server with no other clients active during the run. Treat it as
    # interval evidence under that assumption, not an absolute per-doc proof.
    # Valkey's keyspace_hits moves synchronously, so a per-document before/after
    # delta attributes an L2 hit to a specific document. LMCache's counters are
    # written by a periodic stats flush instead, so a ~100ms window around one
    # request reads the same value twice and every document looks like a miss.
    # For the local tiers, measure the counter across the WHOLE cached pass and
    # attribute at the pass level: coarser, but true.
    per_doc_hits = args.backend == "valkey"
    pass_before = -1 if per_doc_hits else _tier_hits(args)

    label = "per-doc" if per_doc_hits else "pass-level"
    print(f"\n=== Cached pass (reuse KV; {label} hit-counter delta attributes L2) ===")
    rows = []
    for doc_id, prompt in prompts:
        before = _tier_hits(args) if per_doc_hits else -1
        warm_ms = _send(cached_url, args.model, prompt, args.timeout)
        after = _tier_hits(args) if per_doc_hits else -1
        delta = after - before if before >= 0 and after >= 0 else -1
        l2 = "yes" if delta > 0 else ("?" if delta < 0 else "no")
        speedup = cold[doc_id] / warm_ms if warm_ms > 0 else 0.0
        rows.append((doc_id, cold[doc_id], warm_ms, speedup, delta, l2))
        print(
            f"  {doc_id:<28} cached {warm_ms:8.1f} ms  "
            f"{speedup:5.2f}x  hits+{max(delta, 0):<5} L2={l2}"
        )

    # ── Summary: report corpus-wide AND the L2-confirmed subset ──
    def _stats(selected):
        speedups = sorted(r[3] for r in selected)
        return (
            statistics.median(r[1] for r in selected),  # cold median
            statistics.median(r[2] for r in selected),  # cached median
            statistics.median(speedups),
            min(speedups),
            max(speedups),
        )

    # Give the periodic stats flush time to land before the final read.
    pass_delta = -1
    if not per_doc_hits:
        time.sleep(max(args.settle, 15))
        pass_after = _tier_hits(args)
        if pass_before >= 0 and pass_after >= 0:
            pass_delta = pass_after - pass_before
        if pass_delta > 0:
            rows = [(r[0], r[1], r[2], r[3], pass_delta, "yes") for r in rows]
            print(f"\n  pass-level hit-counter delta: +{pass_delta} "
                  f"(attributed to all {len(rows)} docs)")
        else:
            print(f"\n  pass-level hit-counter delta: {pass_delta} "
                  "(no reuse recorded by the tier)")

    l2_rows = [r for r in rows if r[5] == "yes"]
    print("\n" + "=" * 64)
    print(
        f"Documents: {len(rows)}   L2-confirmed reuse: {len(l2_rows)}   "
        f"not-confirmed: {len(rows) - len(l2_rows)}"
    )

    cw = _stats(rows)  # corpus-wide (all docs)
    print(
        f"  All {len(rows)} docs — cold median {cw[0]:.1f} ms, cached median "
        f"{cw[1]:.1f} ms, TTFT speedup median {cw[2]:.2f}x "
        f"(range {cw[3]:.2f}-{cw[4]:.2f}x)"
    )
    if l2_rows:
        s = _stats(l2_rows)
        total_delta = sum(r[4] for r in l2_rows if r[4] > 0)
        print(
            f"  L2-confirmed {len(l2_rows)} docs — cold median {s[0]:.1f} ms, "
            f"cached median {s[1]:.1f} ms, TTFT speedup median {s[2]:.2f}x "
            f"(range {s[3]:.2f}-{s[4]:.2f}x)"
        )
        print(f"  total keyspace_hits delta on cached pass: +{total_delta}")
    if len(l2_rows) == len(rows) and l2_rows:
        print("\n✓ L2 RETRIEVAL CONFIRMED for every document on the corpus")
    elif l2_rows:
        print(
            f"\n~ L2 retrieval confirmed for {len(l2_rows)}/{len(rows)} docs. "
            "Docs without a keyspace_hits delta were likely served by vLLM's GPU "
            "prefix cache — run the server with --no-enable-prefix-caching to "
            "force every reuse through Valkey."
        )
    else:
        print(
            "\n✗ No per-doc keyspace_hits increase observed; reuse was likely "
            "served by vLLM's GPU prefix cache. Restart vLLM with "
            "--no-enable-prefix-caching (or shrink GPU cache) and re-run."
        )
    print("=" * 64)

    # ── Optional machine-readable record, so runs across tiers are comparable ──
    if args.json_path:
        Path(args.json_path).write_text(json.dumps({
            "backend": args.backend,
            "tier": _tier_label(args),
            "model": args.model,
            "vllm_url": url,
            "cached_vllm_url": cached_url,
            "fleet": cached_url != url,
            "min_chars": args.min_chars,
            "docs": len(rows),
            "l2_confirmed": len(l2_rows),
            "cold_median_ms": cw[0],
            "cached_median_ms": cw[1],
            "speedup_median": cw[2],
            "speedup_min": cw[3],
            "speedup_max": cw[4],
            "rows": [
                {"doc_id": r[0], "cold_ms": r[1], "cached_ms": r[2],
                 "speedup": r[3], "hits_delta": r[4], "l2": r[5]}
                for r in rows
            ],
        }, indent=2))
        print(f"wrote {args.json_path}")


if __name__ == "__main__":
    main()
