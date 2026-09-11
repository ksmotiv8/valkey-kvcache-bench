#!/usr/bin/env bash
#
# Host-side tier driver. Lives on a GPU node; run_tiers.ps1 calls it over ssh.
#
#   run_tier.sh serve <tier> <valkey_host>
#   run_tier.sh bench <tier> <valkey_host> <out.json> [cached_url] [limit]
#   run_tier.sh stop
#
# One vLLM process at a time, always launched with --no-enable-prefix-caching:
# a GPU prefix-cache hit is not the tier under test, and it would silently make
# every tier look identical.
#
set -euo pipefail

KIT="${KIT:-$HOME/kit}"
VENV="${VENV:-$HOME/venv}"
MODEL="${MODEL:-Qwen/Qwen2.5-7B-Instruct-AWQ}"
NVME_MNT="${NVME_MNT:-/mnt/nvme}"
PORT="${PORT:-8000}"
LOG="$HOME/vllm.log"

# vLLM defaults to filling 90% of the card with KV and then needs more on top
# of that to capture CUDA graphs, which a 24GB L4 does not have -- it OOMs
# during capture. Bound it explicitly:
#
#   --gpu-memory-utilization  leave headroom for the capture
#   --max-model-len           the corpus prompts are ~1.5k tokens; 16k is
#                             plenty and keeps the KV allocation small
#   --max-num-seqs            this harness sends one request at a time
#   --enforce-eager           skips graph capture entirely, which is the
#                             allocation that failed. It costs a little decode
#                             speed and nothing on prefill, and TTFT is what
#                             is being measured -- identically on every tier.
#
# Override with VLLM_ARGS to change the memory envelope for a bigger card.
VLLM_ARGS="${VLLM_ARGS:---gpu-memory-utilization 0.80 --max-model-len 16384 --max-num-seqs 8 --enforce-eager}"

# FlashInfer JIT-compiles its sampling kernel during warmup and needs ninja
# plus a compiler on the box. Two reasons not to go that way: it fails outright
# where ninja is missing, and even when it works it burns minutes of compile on
# a cold cache -- once per vllm start, and this matrix restarts vllm for every
# tier. Sampling is one token per request and TTFT is measured at the FIRST
# token, so the built-in sampler changes nothing being reported here.
export VLLM_USE_FLASHINFER_SAMPLER="${VLLM_USE_FLASHINFER_SAMPLER:-0}"

# Belt and braces alongside pre_caching_hash_algorithm in the tier configs:
# an unsalted interpreter keeps any builtin-hash path deterministic too.
export PYTHONHASHSEED="${PYTHONHASHSEED:-0}"

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }

# vLLM's API server and its EngineCore are separate processes, and the child
# is the one holding the GPU allocation. Killing only "vllm serve" leaves a
# dead run's EngineCore squatting on the card, and the next serve fails with
# "Free memory on device cuda:0 (2.39/22.04 GiB)" -- which reads like a config
# problem and is not one. Kill both, then confirm against the card itself.
gpu_used_mib() {
  nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null |
    head -1 | tr -d '[:space:]'
}

stop_vllm() {
  pkill -f "vllm serve"     >/dev/null 2>&1 || true
  pkill -f "VLLM::"         >/dev/null 2>&1 || true
  pkill -f "EngineCore"     >/dev/null 2>&1 || true
  for _ in $(seq 1 20); do
    pgrep -f "vllm serve|VLLM::|EngineCore" >/dev/null 2>&1 || break
    sleep 1
  done
  pkill -9 -f "vllm serve|VLLM::|EngineCore" >/dev/null 2>&1 || true

  # Anything still holding the card, whatever it is called.
  for pid in $(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null); do
    say "killing leftover GPU process $pid"
    sudo kill -9 "$pid" >/dev/null 2>&1 || true
  done

  # The driver does not free memory the instant the process dies.
  for _ in $(seq 1 30); do
    used="$(gpu_used_mib)"
    [ -z "$used" ] && break
    [ "$used" -lt 1024 ] && break
    sleep 2
  done
  say "gpu memory in use after stop: $(gpu_used_mib) MiB"
}

dump_log() {
  echo "--- first errors in $LOG ---" >&2
  grep -n -i -E "error|exception|failed|traceback|assert" "$LOG" 2>/dev/null |
    grep -v -i "error_|errors=0" | head -25 >&2 || true
  echo "--- last 60 lines ---" >&2
  tail -60 "$LOG" >&2
  echo "--- full log is at $LOG on this host ---" >&2
}

wait_ready() {
  local deadline=$(( SECONDS + 900 ))
  while [ $SECONDS -lt $deadline ]; do
    if curl -sf "http://localhost:${PORT}/v1/models" >/dev/null 2>&1; then
      say "vllm ready on :${PORT}"
      return 0
    fi
    if ! pgrep -f "vllm serve" >/dev/null 2>&1; then
      echo "ERROR: vllm exited during startup." >&2
      dump_log
      return 1
    fi
    sleep 5
  done
  echo "ERROR: vllm not ready after 900s." >&2
  dump_log
  return 1
}

# Resolve the tier config, substituting the Valkey endpoint. Written to /tmp so
# the checked-in config keeps its placeholder and the repo stays clean.
tier_config() {
  local tier="$1" vk="${2:-}"
  local src="$KIT/benchmarks/configs/${tier}_tier.yaml"
  local dst="/tmp/lmcache_${tier}.yaml"
  [ -f "$src" ] || { echo "ERROR: no config at $src" >&2; return 1; }
  sed "s/VALKEY_HOST/${vk}/g" "$src" > "$dst"
  echo "$dst"
}

# A cold pass is only cold if the tier is empty. Valkey is flushed by the
# harness's own --flush-l2; the local tiers have to be cleared here, and the
# page cache with them, or the disk tier is quietly measuring RAM.
clear_tier() {
  local tier="$1"
  case "$tier" in
    disk)
      say "clearing $NVME_MNT/lmcache and dropping the page cache"
      rm -rf "${NVME_MNT:?}/lmcache/"* 2>/dev/null || true
      sync
      echo 3 | sudo tee /proc/sys/vm/drop_caches >/dev/null
      ;;
    cpu)
      # The CPU tier lives inside the vLLM process, so stopping the server is
      # the flush. stop_vllm has already run by the time this is called.
      say "cpu tier cleared with the previous server process"
      ;;
  esac
}

cmd="${1:?usage: run_tier.sh serve|bench|stop ...}"

case "$cmd" in
  serve)
    tier="${2:?tier}"; vk="${3:-}"
    stop_vllm
    clear_tier "$tier"
    cfg="$(tier_config "$tier" "$vk")"
    say "serving tier=$tier config=$cfg"
    # shellcheck disable=SC2086 -- VLLM_ARGS is a deliberate word-split list
    say "flashinfer sampler: $VLLM_USE_FLASHINFER_SAMPLER"
    LMCACHE_CONFIG_FILE="$cfg" nohup "$VENV/bin/vllm" serve "$MODEL" \
      --port "$PORT" --no-enable-prefix-caching $VLLM_ARGS \
      --kv-transfer-config '{"kv_connector":"LMCacheConnectorV1","kv_role":"kv_both"}' \
      > "$LOG" 2>&1 &
    say "vllm args: $VLLM_ARGS"
    wait_ready
    # One look at what LMCache is actually exposing. The reuse check reads a
    # counter from here, and a silently-absent counter reads as "no reuse",
    # which is the most misleading failure this harness can produce.
    # Report the hit counters specifically, not the first 20 metrics
    # alphabetically -- and without a head in the pipeline, whose SIGPIPE
    # makes a working endpoint look like a dead one.
    say "lmcache hit counters on :6999"
    metrics="$(curl -sf --max-time 5 http://localhost:6999/metrics 2>/dev/null || true)"
    if [ -z "$metrics" ]; then
      echo "  WARNING: nothing served on :6999 (internal_api_server_enabled?)"
    else
      echo "$metrics" | grep -E "^lmcache[:_]num_(hit|retrieve|lookup)" |
        grep -v "_created" | sed 's/^/  /' || echo "  (no hit counters exposed)"
    fi
    ;;

  bench)
    tier="${2:?tier}"; vk="${3:-}"; out="${4:?out.json}"
    cached_url="${5:-}"; limit="${6:-0}"
    args=(--corpus "$KIT/corpus" --model "$MODEL"
          --vllm-url "http://localhost:${PORT}"
          --backend "$tier" --json "$out" --limit "$limit")
    if [ "$tier" = "valkey" ]; then
      args+=(--valkey-host "$vk" --flush-l2)
    fi
    if [ -n "$cached_url" ]; then
      args+=(--cached-vllm-url "$cached_url")
    fi
    say "benchmarking tier=$tier${cached_url:+ (fleet -> $cached_url)}"
    "$VENV/bin/python" "$KIT/benchmarks/bench_corpus_e2e.py" "${args[@]}"
    rc=$?
    # Ground truth after the fact: whatever the harness concluded, this is what
    # the tier itself recorded.
    say "lmcache counters after the run"
    curl -sf --max-time 5 http://localhost:6999/metrics 2>/dev/null |
      grep -E "^lmcache[:_]num_(hit|retrieve|lookup)" | grep -v "_created" |
      sed -E 's/\{[^}]*\}//' | sed 's/^/  /' || true
    exit $rc
    ;;

  stop)
    stop_vllm
    say "stopped"
    ;;

  *)
    echo "unknown command: $cmd" >&2
    exit 2
    ;;
esac
