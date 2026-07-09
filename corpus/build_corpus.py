#!/usr/bin/env python3
"""build_corpus.py — Domain-modular corpus generator for KV cache benchmarking.

Generates documents in a specified domain (medical, legal, narrative) using
a running vLLM instance. Each domain has its own generator module with
specialized prompts (see corpus/generators/).

Two-stage pipeline:
  Stage 1 — Scenario generation: LLM produces a JSON array of document scenarios
  Stage 2 — Document generation: For each scenario, LLM writes full documents
            with exact token count control via local tokenizer

Output: corpus/{domain}/doc_XXXX.txt + corpus/{domain}/metadata.json
Documents are generated once and read from disk by the benchmark.

Usage:
    # Start vLLM first:
    vllm serve Qwen/Qwen3-8B-FP8 --gpu-memory-utilization 0.9

    # Generate medical corpus:
    build-corpus \\
        --domain medical \\
        --documents 15 \\
        --target-tokens 10000 \\
        --api-base http://localhost:8000/v1

    # Generate with noise injection:
    build-corpus \\
        --domain medical \\
        --documents 15 \\
        --target-tokens 10000 \\
        --noise \\
        --api-base http://localhost:8000/v1

Install:
    uv sync
"""

import argparse
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI

# ---------------------------------------------------------------------------
# Tokenizer — loaded once, used for exact token counting throughout
# ---------------------------------------------------------------------------

_tokenizer = None


def _load_tokenizer(model_name: str):
    """Load the model's tokenizer for exact local token counting.

    Exits with an error if transformers is not installed, since the
    character-based fallback produces unusable token counts.
    """
    global _tokenizer
    if _tokenizer is not None:
        return _tokenizer
    from transformers import AutoTokenizer
    print(f"Loading tokenizer: {model_name} ...", file=sys.stderr)
    _tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)
    print(f"  vocab_size={_tokenizer.vocab_size}", file=sys.stderr)
    return _tokenizer


def count_tokens(text: str) -> int:
    """Count tokens using local tokenizer, or estimate from char count."""
    if _tokenizer is not None:
        return len(_tokenizer.encode(text))
    return len(text) // 4


def trim_to_tokens(text: str, target: int) -> tuple:
    """Trim text to exactly `target` tokens via encode/decode round-trip."""
    if _tokenizer is None:
        return text, count_tokens(text)
    ids = _tokenizer.encode(text)
    if len(ids) <= target:
        return text, len(ids)
    trimmed_ids = ids[:target]
    trimmed_text = _tokenizer.decode(trimmed_ids, skip_special_tokens=True)
    actual = len(_tokenizer.encode(trimmed_text))
    return trimmed_text, actual


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def _call_with_retry(client: OpenAI, max_retries: int = 3, **kwargs):
    """Call chat.completions.create with exponential backoff."""
    for attempt in range(max_retries):
        try:
            return client.chat.completions.create(**kwargs)
        except (APIConnectionError, APITimeoutError, APIStatusError) as e:
            if isinstance(e, APIStatusError) and e.status_code < 500:
                raise
            if attempt == max_retries - 1:
                raise
            wait = 2 ** attempt
            print(f" [retry {attempt + 1}/{max_retries} in {wait}s: {e}]",
                  end="", flush=True)
            time.sleep(wait)


def _extract_content(resp) -> str:
    """Extract message content from response."""
    if not resp.choices:
        return ""
    content = resp.choices[0].message.content
    return content if content is not None else ""


def _strip_thinking(text: str) -> str:
    """Remove <think>...</think> blocks from model output."""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def _disable_thinking_kwargs() -> dict:
    """Return extra_body kwargs to disable Qwen3 thinking at the API level."""
    return {
        "extra_body": {
            "chat_template_kwargs": {"enable_thinking": False},
        }
    }


# ---------------------------------------------------------------------------
# Stage 1 — Scenario generation
# ---------------------------------------------------------------------------

def generate_scenarios(
    client: OpenAI, model: str, domain_module
) -> List[Dict[str, Any]]:
    """Ask the LLM to produce scenario definitions for the given domain."""
    print(f"Stage 1: Generating scenarios for domain '{domain_module.__name__}'...")

    if domain_module.STAGE1_PROMPT is None:
        print(f"ERROR: Domain '{domain_module.__name__}' has no STAGE1_PROMPT.",
              file=sys.stderr)
        sys.exit(1)

    resp = _call_with_retry(
        client,
        model=model,
        messages=[{"role": "user", "content": domain_module.STAGE1_PROMPT}],
        max_tokens=4096,
        temperature=0.7,
        **_disable_thinking_kwargs(),
    )

    raw = _extract_content(resp)
    if not raw:
        print("ERROR: Stage 1 returned empty response.", file=sys.stderr)
        sys.exit(1)

    raw = raw.strip()
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    raw = re.sub(r"^```\w*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)
    raw = raw.strip()

    bracket_start = raw.find("[")
    bracket_end = raw.rfind("]")
    if bracket_start >= 0 and bracket_end > bracket_start:
        raw = raw[bracket_start : bracket_end + 1]

    try:
        scenarios = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"ERROR: Stage 1 produced invalid JSON: {e}", file=sys.stderr)
        print(f"  Raw output: {raw[:500]}", file=sys.stderr)
        sys.exit(1)

    required = domain_module.REQUIRED_SCENARIO_KEYS
    for i, s in enumerate(scenarios):
        missing = required - set(s.keys())
        if missing:
            print(f"ERROR: Scenario {i} missing keys: {missing}", file=sys.stderr)
            sys.exit(1)

    total_weight = sum(s["weight"] for s in scenarios)
    if total_weight <= 0:
        print("ERROR: Scenario weights sum to zero.", file=sys.stderr)
        sys.exit(1)
    for s in scenarios:
        s["weight"] = s["weight"] / total_weight

    print(f"  Generated {len(scenarios)} scenarios:")
    for s in scenarios:
        print(f"    {s['weight']:.1%}  {s['title']} ({s['doc_type']})")

    return scenarios


# ---------------------------------------------------------------------------
# Stage 2 — Document generation
# ---------------------------------------------------------------------------

MAX_CONTEXT_CHARS = 80_000
STOP_TOLERANCE = 0.02


def generate_document(
    client: OpenAI,
    model: str,
    domain_module,
    scenario: Dict[str, Any],
    token_target: int,
    doc_index: int,
) -> tuple:
    """Generate one document with exact token count control."""
    first_shot_tokens = min(token_target, 16384)

    prompt = domain_module.STAGE2_TEMPLATE.format(
        **scenario,
        token_target=token_target,
        doc_number=doc_index + 1,
    )

    resp = _call_with_retry(
        client,
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=first_shot_tokens,
        temperature=0.9,
        **_disable_thinking_kwargs(),
    )

    raw_content = _extract_content(resp)
    text = _strip_thinking(raw_content)
    total_tokens = count_tokens(text)
    print(f" [{total_tokens}tok]", end="", flush=True)

    # Continuation loop
    max_continuations = 10
    consecutive_low_yield = 0
    min_target = int(token_target * (1 - STOP_TOLERANCE))

    for attempt in range(max_continuations):
        if total_tokens >= min_target:
            break

        remaining_tokens = token_target - total_tokens
        cont_prompt = domain_module.CONTINUATION_TEMPLATE.format(
            **scenario,
            remaining_tokens=remaining_tokens,
        )

        context_text = text
        if len(text) > MAX_CONTEXT_CHARS:
            context_text = "...[earlier content truncated]...\n\n" + text[-MAX_CONTEXT_CHARS:]

        cont_max = min(remaining_tokens + 256, 8192)
        resp = _call_with_retry(
            client,
            model=model,
            messages=[
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": context_text},
                {"role": "user", "content": cont_prompt},
            ],
            max_tokens=cont_max,
            temperature=0.9,
            **_disable_thinking_kwargs(),
        )

        raw_cont = _extract_content(resp)
        continuation = _strip_thinking(raw_cont)
        if not continuation:
            break

        prev_tokens = total_tokens
        text += "\n\n" + continuation
        total_tokens = count_tokens(text)
        new_tokens = total_tokens - prev_tokens
        print(f" +{new_tokens}tok", end="", flush=True)

        if new_tokens < 50:
            consecutive_low_yield += 1
            if consecutive_low_yield >= 2:
                break
        else:
            consecutive_low_yield = 0

    # Trim to exact target
    if total_tokens > token_target:
        text, total_tokens = trim_to_tokens(text, token_target)
        print(f" [trimmed->{total_tokens}]", end="", flush=True)
    elif total_tokens < token_target:
        print(f" [short:{total_tokens}/{token_target}]", end="", flush=True)

    return text, total_tokens


# ---------------------------------------------------------------------------
# Allocation
# ---------------------------------------------------------------------------

def allocate_counts(total: int, weights: List[float]) -> List[int]:
    """Distribute `total` across scenarios by weight (largest-remainder method)."""
    raw = [w * total for w in weights]
    counts = [int(math.floor(x)) for x in raw]
    remainder = total - sum(counts)
    fracs = sorted(
        [(i, raw[i] - counts[i]) for i in range(len(weights))],
        key=lambda x: x[1],
        reverse=True,
    )
    for i in range(remainder):
        counts[fracs[i][0]] += 1
    return counts


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def build_corpus(
    client: OpenAI,
    model: str,
    domain_module,
    domain_name: str,
    num_documents: int,
    target_tokens: int,
    output_dir: str,
    noise_enabled: bool = False,
    noise_rate: float = 0.12,
) -> None:
    """Generate the full corpus and write to disk."""
    # Stage 1
    scenarios = generate_scenarios(client, model, domain_module)

    # Stage 2
    weights = [s["weight"] for s in scenarios]
    counts = allocate_counts(num_documents, weights)
    total_docs = sum(counts)

    out_dir = os.path.join(output_dir, domain_name)
    os.makedirs(out_dir, exist_ok=True)

    noise_fn = None
    if noise_enabled:
        from corpus.noise import get_noise_function
        noise_fn = get_noise_function(domain_name)

    doc_id = 0
    metadata_docs = []
    jsonl_items = []

    for scenario, count in zip(scenarios, counts):
        for j in range(count):
            print(
                f"Stage 2: [{doc_id + 1}/{total_docs}] "
                f"{scenario['title']} ({j + 1}/{count})...",
                end="",
                flush=True,
            )
            t0 = time.time()
            doc_text, actual_tokens = generate_document(
                client, model, domain_module, scenario, target_tokens, doc_id
            )
            elapsed = time.time() - t0

            # Apply noise if enabled, then re-trim to preserve exact token count
            tokens_before_noise = actual_tokens
            if noise_fn is not None:
                doc_text = noise_fn(doc_text, rate=noise_rate, seed=doc_id)
                noisy_tokens = count_tokens(doc_text)
                noise_delta = noisy_tokens - tokens_before_noise
                sign = "+" if noise_delta >= 0 else ""
                print(f" [noise {sign}{noise_delta}tok", end="", flush=True)
                # Re-trim to target so noise doesn't break exact sizing
                if noisy_tokens > target_tokens:
                    doc_text, actual_tokens = trim_to_tokens(doc_text, target_tokens)
                    print(f", retrim->{actual_tokens}]", end="", flush=True)
                else:
                    actual_tokens = noisy_tokens
                    print(f"]", end="", flush=True)

            print(f" -> {actual_tokens} tokens, {elapsed:.1f}s")

            # Write document to disk as .txt
            filename = f"doc_{doc_id:04d}.txt"
            filepath = os.path.join(out_dir, filename)
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(doc_text)

            # Collect for JSONL and metadata
            jsonl_items.append({
                "id": f"{scenario['scenario_id']}-{target_tokens}-{doc_id}",
                "token_target": target_tokens,
                "actual_tokens": actual_tokens,
                "scenario_id": scenario["scenario_id"],
                "scenario_title": scenario["title"],
                "doc_type": scenario["doc_type"],
                "document": doc_text,
            })

            metadata_docs.append({
                "id": doc_id,
                "filename": filename,
                "scenario_id": scenario["scenario_id"],
                "scenario_title": scenario["title"],
                "doc_type": scenario["doc_type"],
                "setting": scenario.get("setting", ""),
                "target_tokens": target_tokens,
                "tokens_before_noise": tokens_before_noise,
                "actual_tokens": actual_tokens,
            })
            doc_id += 1

    # Write JSONL (benchmark-compatible: each line has "document" field)
    jsonl_path = os.path.join(out_dir, f"{domain_name}_docs_{target_tokens}.jsonl")
    with open(jsonl_path, "w", encoding="utf-8") as f:
        for item in jsonl_items:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    # Write metadata
    metadata = {
        "domain": domain_name,
        "documents": num_documents,
        "target_tokens": target_tokens,
        "generator_model": model,
        "generation_date": datetime.now(timezone.utc).isoformat(),
        "noise_enabled": noise_enabled,
        "noise_rate": noise_rate if noise_enabled else None,
        "jsonl_file": os.path.basename(jsonl_path),
        "scenarios": scenarios,
        "docs": metadata_docs,
    }
    meta_path = os.path.join(out_dir, "metadata.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2, ensure_ascii=False)

    # Summary
    tokens = [d["actual_tokens"] for d in metadata_docs]
    print(f"\nWrote {len(metadata_docs)} documents to {out_dir}/")
    print(f"  Target:  {target_tokens} tokens/doc")
    print(f"  Actual:  mean={sum(tokens)/len(tokens):.0f}, "
          f"min={min(tokens)}, max={max(tokens)}")
    if noise_enabled:
        before = [d["tokens_before_noise"] for d in metadata_docs]
        deltas = [d["actual_tokens"] - d["tokens_before_noise"] for d in metadata_docs]
        mean_delta = sum(deltas) / len(deltas)
        pct = mean_delta / target_tokens * 100
        print(f"  Noise impact: mean {mean_delta:+.0f} tokens ({pct:+.2f}%), "
              f"range [{min(deltas):+d}, {max(deltas):+d}]")
    print(f"  JSONL:   {jsonl_path}")
    print(f"  Metadata: {meta_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    from corpus.generators import list_domains

    parser = argparse.ArgumentParser(
        description="Generate domain-specific document corpus for KV cache benchmarking.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  build-corpus --domain medical --documents 15 --target-tokens 10000
  build-corpus --domain medical --documents 15 --target-tokens 10000 --noise
""",
    )
    parser.add_argument(
        "--domain",
        default="medical",
        choices=list_domains(),
        help="Document domain (default: medical)",
    )
    parser.add_argument(
        "--documents",
        type=int,
        default=15,
        help="Number of documents to generate (default: 15)",
    )
    parser.add_argument(
        "--target-tokens",
        type=int,
        default=10000,
        help="Target token count per document (default: 10000)",
    )
    parser.add_argument(
        "--output-dir",
        default="./corpus",
        help="Base output directory (default: ./corpus)",
    )
    parser.add_argument(
        "--api-base",
        default="http://localhost:8000/v1",
        help="vLLM API base URL (default: http://localhost:8000/v1)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Model name. If omitted, auto-detected from vLLM.",
    )
    parser.add_argument(
        "--noise",
        action="store_true",
        help="Apply controlled noise injection to generated documents.",
    )
    parser.add_argument(
        "--noise-rate",
        type=float,
        default=0.12,
        help="Fraction of lines to perturb (default: 0.12).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    client = OpenAI(base_url=args.api_base, api_key="not-needed")

    # Auto-detect model
    model = args.model
    if not model:
        models = client.models.list()
        if not models.data:
            print("ERROR: vLLM server returned no models.", file=sys.stderr)
            sys.exit(1)
        model = models.data[0].id
        print(f"Auto-detected model: {model}")

    _load_tokenizer(model)

    from corpus.generators import get_domain
    domain_module = get_domain(args.domain)

    build_corpus(
        client=client,
        model=model,
        domain_module=domain_module,
        domain_name=args.domain,
        num_documents=args.documents,
        target_tokens=args.target_tokens,
        output_dir=args.output_dir,
        noise_enabled=args.noise,
        noise_rate=args.noise_rate,
    )


if __name__ == "__main__":
    main()
