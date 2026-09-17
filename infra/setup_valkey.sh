#!/usr/bin/env bash
#
# Valkey host setup. Run on the r7i instance.
#
# This is the remote tier under test, so the confounds to remove are the ones
# that would make it look slower than it is:
#
#   save ""            an RDB fork mid-run is a latency spike that has nothing
#                      to do with the tier being remote
#   appendonly no      same, continuously
#   noeviction         a tier that silently evicts is not the tier you measured
#   THP off            multi-millisecond tail latency, classic cause
#
set -euo pipefail

BUNDLE_TAG="${BUNDLE_TAG:-9.1}"
MAXMEMORY="${MAXMEMORY:-48gb}"

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }

say "installing docker"
sudo dnf install -y -q docker >/dev/null
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER" || true

say "kernel tuning"
sudo sysctl -w net.core.somaxconn=65535 >/dev/null
sudo sysctl -w net.ipv4.tcp_max_syn_backlog=65535 >/dev/null
sudo sysctl -w vm.overcommit_memory=1 >/dev/null
if [ -f /sys/kernel/mm/transparent_hugepage/enabled ]; then
  echo never | sudo tee /sys/kernel/mm/transparent_hugepage/enabled >/dev/null
  echo never | sudo tee /sys/kernel/mm/transparent_hugepage/defrag  >/dev/null
fi

mkdir -p ~/valkey
cat > ~/valkey/valkey.conf <<EOF
bind 0.0.0.0
port 6379
protected-mode no
save ""
appendonly no
maxmemory ${MAXMEMORY}
maxmemory-policy noeviction
tcp-backlog 65535
tcp-keepalive 60
latency-tracking yes
EOF

say "pulling valkey/valkey-bundle:${BUNDLE_TAG}"
sudo docker pull "valkey/valkey-bundle:${BUNDLE_TAG}"
sudo docker rm -f valkey >/dev/null 2>&1 || true
say "starting valkey (host networking, so this is not measuring a NAT hop)"
sudo docker run -d --name valkey --network host \
  --ulimit nofile=100000:100000 \
  -v ~/valkey/valkey.conf:/etc/valkey/valkey.conf:ro \
  "valkey/valkey-bundle:${BUNDLE_TAG}" \
  valkey-server /etc/valkey/valkey.conf

sleep 3
say "verifying"
sudo docker exec valkey valkey-cli INFO server | grep -E "valkey_version|os|arch_bits"
sudo docker exec valkey valkey-cli PING
