#!/usr/bin/env bash
#
# GPU host setup. Run on each g6 instance.
#
# Everything here that is not "install and start" is a confound being removed:
#
#   ephemeral NVMe     the local-disk tier must be real NVMe, not the gp3 root
#                      volume, or "local disk" is quietly measuring EBS
#   THP off            transparent hugepages are the classic source of
#                      multi-millisecond tail latency
#   model prefetch     a cold HuggingFace download inside the first cold pass
#                      lands in the TTFT numbers
#
set -euo pipefail

MODEL="${MODEL:-Qwen/Qwen2.5-7B-Instruct-AWQ}"
NVME_MNT="${NVME_MNT:-/mnt/nvme}"

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }

say "gpu"
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader

# --- ephemeral NVMe -------------------------------------------------------
# The instance store is gone on stop, which is fine: the disk tier is rebuilt
# per run anyway. Two cases to handle:
#
#   1. The Deep Learning AMI has already claimed it. It builds an LVM volume
#      out of the instance store and mounts it at /opt/dlami/nvme at boot, so
#      there is no unpartitioned disk left to find. Use what is there.
#   2. A plain AMI leaves it raw. Format and mount it.
#
# Either way the tier configs refer to $NVME_MNT, so that path has to exist --
# as a real mount, or as a symlink to where the AMI put it.
DLAMI_NVME="/opt/dlami/nvme"
say "locating the instance store"
if mountpoint -q "$DLAMI_NVME"; then
  say "the AMI already mounted it at $DLAMI_NVME"
  sudo mkdir -p "$DLAMI_NVME/lmcache"
  sudo chown -R "$USER:$USER" "$DLAMI_NVME"
  if [ ! -e "$NVME_MNT" ]; then
    sudo ln -s "$DLAMI_NVME" "$NVME_MNT"
    say "linked $NVME_MNT -> $DLAMI_NVME"
  fi
else
  # Largest unmounted, unpartitioned disk. Do not assume nvme1n1: the root
  # volume is not always nvme0.
  DEV=""
  for d in $(lsblk -dpno NAME,TYPE | awk '$2=="disk"{print $1}'); do
    if [ -z "$(lsblk -no MOUNTPOINT "$d" | tr -d '[:space:]')" ] && \
       [ -z "$(lsblk -no NAME "$d" | tail -n +2)" ]; then
      DEV="$d"; break
    fi
  done
  if [ -z "$DEV" ]; then
    echo "ERROR: no instance store found, mounted or raw. lsblk:" >&2
    lsblk >&2
    exit 1
  fi
  say "instance store = $DEV"
  if ! sudo blkid "$DEV" >/dev/null 2>&1; then
    sudo mkfs.xfs -f -q "$DEV"
  fi
  sudo mkdir -p "$NVME_MNT"
  mountpoint -q "$NVME_MNT" || sudo mount -o noatime "$DEV" "$NVME_MNT"
  sudo mkdir -p "$NVME_MNT/lmcache"
  sudo chown -R "$USER:$USER" "$NVME_MNT"
fi
# Confirm the disk tier is on the instance store and not silently on the root
# EBS volume, which would make "local NVMe" a different measurement entirely.
df -h "$NVME_MNT"
lsblk -no NAME,SIZE,MOUNTPOINT | grep -E "$(basename "$(readlink -f "$NVME_MNT")")|nvme" || true

# --- kernel tuning --------------------------------------------------------
say "kernel tuning"
sudo sysctl -w net.core.somaxconn=65535 >/dev/null
sudo sysctl -w vm.overcommit_memory=1 >/dev/null
if [ -f /sys/kernel/mm/transparent_hugepage/enabled ]; then
  echo never | sudo tee /sys/kernel/mm/transparent_hugepage/enabled >/dev/null
  echo never | sudo tee /sys/kernel/mm/transparent_hugepage/defrag  >/dev/null
fi

# --- python env -----------------------------------------------------------
# A venv, not --user: the DLAMI ships its own python packages and pip --user
# resolves torch against them in ways that surface only at model load.
say "python env"
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv python3-dev >/dev/null
[ -d ~/venv ] || python3 -m venv ~/venv
~/venv/bin/pip install -q --upgrade pip
say "installing vllm + lmcache + the glide connector (this takes a few minutes)"
# ninja is here for FlashInfer's JIT: run_tier.sh disables that sampler, but a
# missing ninja turns any other JIT path into a hard startup failure.
~/venv/bin/pip install -q vllm lmcache "valkey-glide-sync>=2.3" requests redis huggingface_hub ninja

say "prefetching $MODEL so the cold pass measures prefill, not a download"
~/venv/bin/python -c "
from huggingface_hub import snapshot_download
snapshot_download('$MODEL')
print('model cached')
"

say "versions"
~/venv/bin/python -c "
import vllm, lmcache, sys
print('python', sys.version.split()[0], 'vllm', vllm.__version__, 'lmcache', lmcache.__version__)
"

cat <<EOF

Ready. The kit is at ~/kit and the disk tier lives at $NVME_MNT/lmcache.

Start a server against one tier:

  LMCACHE_CONFIG_FILE=~/kit/benchmarks/configs/valkey_tier.yaml \\
  ~/venv/bin/vllm serve $MODEL --port 8000 --no-enable-prefix-caching \\
      --kv-transfer-config '{"kv_connector":"LMCacheConnectorV1","kv_role":"kv_both"}'

run_tiers.ps1 does this for you, once per tier.
EOF
