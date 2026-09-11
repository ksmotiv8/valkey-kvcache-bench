# SPDX-License-Identifier: Apache-2.0
"""Token and KV-cache size of every document in the corpus, for a given model.

``bench_corpus_e2e.py`` reports per-document TTFT but not per-document length,
and the two only make sense together: a tier that "serves 30 documents" is a
different claim at 1.5k tokens each than at 12k. This prints what the model's
tokenizer actually produces, and what that costs in KV bytes.

Usage (on a host with the model cached)::

    python corpus_tokens.py ../corpus --model Qwen/Qwen2.5-7B-Instruct-AWQ

KV bytes per token default to Qwen2.5-7B (28 layers x 2 x 4 KV heads x 128 dim x
fp16); pass ``--kv-bytes-per-token`` for another model.
"""

# Standard
from pathlib import Path
import argparse
import csv
import statistics


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("corpus", help="Path to the corpus/ dir")
    parser.add_argument("--model", default="Qwen/Qwen2.5-7B-Instruct-AWQ")
    parser.add_argument("--chunk-size", type=int, default=256, help="LMCache chunk_size")
    parser.add_argument(
        "--kv-bytes-per-token", type=int, default=28 * 2 * 4 * 128 * 2,
        help="per-token KV footprint for the model (default: Qwen2.5-7B fp16)",
    )
    parser.add_argument("--min-chars", type=int, default=6000,
                        help="mirror the harness's padding so counts match what was served")
    args = parser.parse_args()

    # Third Party
    from transformers import AutoTokenizer

    tok = AutoTokenizer.from_pretrained(args.model)
    corpus = Path(args.corpus)
    rows = list(csv.DictReader((corpus / "manifest.csv").open()))

    print(f"{'doc':<26}{'chars':>8}{'tokens':>8}{'full chunks':>13}{'KV MiB':>9}")
    counts = []
    for r in rows:
        rel = r["filename"].split("corpus/", 1)[-1]
        text = (corpus / rel).read_text()
        if 0 < len(text) < args.min_chars:
            reps = -(-args.min_chars // len(text))
            text = (text * reps)[: args.min_chars]
        n = len(tok(text)["input_ids"])
        counts.append(n)
        print(f"{rel:<26}{len(text):>8}{n:>8}{n // args.chunk_size:>13}"
              f"{n * args.kv_bytes_per_token / 2**20:>9.0f}")

    total = sum(counts)
    print("-" * 64)
    print(f"{'30 docs':<26}{'':>8}{total:>8}{sum(c // args.chunk_size for c in counts):>13}"
          f"{total * args.kv_bytes_per_token / 2**20:>9.0f}")
    print(f"\ntokens/doc: min {min(counts)}  median {statistics.median(counts):.0f}  "
          f"max {max(counts)}   corpus KV {total * args.kv_bytes_per_token / 2**30:.1f} GiB")


if __name__ == "__main__":
    main()
