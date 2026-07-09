# Document corpus and generator

This directory contains the **30-document benchmark corpus** (15 legal, 15
medical, indexed by `manifest.csv`) and the **generator that produced it**
(`build_corpus.py` + `generators/`), so the corpus can be regenerated, grown,
or extended to new domains.

## Background: why a realistic corpus

KV-cache benchmarks need documents that behave like real workloads: varied
vocabulary, mixed formatting, and the kind of token distributions that stress
compression and cache access patterns in ways synthetic text never will. Random
or repeated filler text produces unrealistically uniform tokens and misleading
cache behavior.

Medical documents came first because clinical notes are a natural fit: they
combine dense abbreviations, numeric lab values, structured sections, and
narrative prose in a single document. That mix produces diverse token patterns
and realistic tensor value distributions. Legal documents were added as a
second domain to validate that the pipeline generalizes: legal text has its own
structure (clauses, cross-references, formal phrasing) and its own compression
characteristics. Supporting multiple domains also lets you test whether
benchmark results are sensitive to corpus choice, or whether findings hold
across document types.

All documents are fully synthetic: fictional patients, parties, and facilities.

## How the generator works

`build_corpus.py` connects to a running vLLM instance (any OpenAI-compatible
endpoint) and works in two stages. The pipeline is domain-agnostic; each domain
(`generators/medical.py`, `generators/legal.py`, `generators/narrative.py`)
supplies its own prompts.

1. **Scenario generation.** The LLM produces a JSON array of document
   scenarios representing a realistic mix for the domain, each with a weight.
   Medical yields EHR document types across inpatient, outpatient, emergency,
   surgical, imaging, lab, specialty, and behavioral health settings; legal
   yields litigation, transactional, regulatory, and advisory document types.
   Weights are normalized and used to allocate documents proportionally.

2. **Document generation.** For each scenario, the LLM writes a complete
   document from the domain's template, then the generator continues and trims
   the output using a local tokenizer until the document hits the exact target
   token count. Optional noise injection (`--noise`) perturbs a fraction of
   lines to vary token patterns further.

Output: `corpus/{domain}/doc_XXXX.txt` plus per-domain metadata. Documents are
generated once and read from disk by the benchmarks, so runs are reproducible.

## Usage

```bash
# Start any vLLM model first:
vllm serve Qwen/Qwen2.5-7B-Instruct-AWQ --port 8000

# Generate 15 medical documents at ~10k tokens each (run from the repo root):
python -m corpus.build_corpus \
    --domain medical \
    --documents 15 \
    --target-tokens 10000 \
    --api-base http://localhost:8000/v1

# Same for legal; add --noise for controlled perturbation:
python -m corpus.build_corpus --domain legal --documents 15 \
    --target-tokens 10000 --noise --noise-rate 0.12
```

| Flag | Default | Meaning |
|---|---|---|
| `--domain` | `medical` | `medical`, `legal`, or `narrative` |
| `--documents` | 15 | Number of documents to generate |
| `--target-tokens` | 10000 | Exact token count per document |
| `--output-dir` | `./corpus` | Base output directory |
| `--api-base` | `http://localhost:8000/v1` | vLLM OpenAI-compatible endpoint |
| `--model` | auto-detect | Model name (auto-detected from the server if omitted) |
| `--noise` | off | Apply controlled noise injection |
| `--noise-rate` | 0.12 | Fraction of lines to perturb |

Requires `openai` and `transformers` (for the tokenizer) in addition to a
running vLLM server. Adding a new domain is a single module in `generators/`
exposing the Stage 1/Stage 2 prompt templates.
