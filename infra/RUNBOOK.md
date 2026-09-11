# Runbook: the tier comparison, start to finish

Everything runs from `infra\` in PowerShell. Roughly 25 minutes of setup and
40 minutes of measurement, at about $2.14/hour for the three hosts.

## 0. Keep the previous campaign

Results are written to `results\tiers\` and a re-run overwrites them. Move the
last set aside first, or you lose the ability to compare campaigns:

```powershell
cd C:\git\momento\valkey-kvcache-bench\infra
Rename-Item ..\results\tiers tiers-2026-08-31
```

## 1. Provision, install, and prove one document works

```powershell
.\Run-TierBench.ps1 -AwsProfile dev -Region us-west-2 -NoPlacementGroup -Stage sanity
```

This preflights (aws/ssh/scp on PATH, profile authenticates, default VPC, GPU
quota >= 8 vCPU), shows the hourly cost and waits for a `y`, then provisions two
GPU nodes plus a Valkey host, installs vLLM and LMCache on both GPUs, and runs
two documents on the cpu tier.

Stop and look at the sanity line before going further:

```
==> sanity: 2/2 docs L2-confirmed, median speedup 37.93x
```

`0/2` means reuse was not attributed and the matrix will not mean anything.

Capacity notes: `-Region us-west-2` and `-NoPlacementGroup` are what worked on
2026-08-31; us-east-1 and us-east-2 had no `g6.xlarge` pairs in any AZ. Provision
sweeps `g6.xlarge`, `g5.xlarge`, `g4dn.xlarge` across every AZ that offers them,
so a capacity miss moves on by itself.

## 2. The matrix

```powershell
.\run_tiers.ps1 -Stage single -SkipSetup
.\run_tiers.ps1 -Stage fleet  -SkipSetup
```

`single` is three tiers on one node, about 15 minutes. `fleet` is the same three
with the cached pass on the second node, about 20 minutes because each tier
starts vLLM on both. Results land in `results\tiers\` at the repo root.

## 3. Capture the environment BEFORE tearing down

None of this is recoverable once the instances are gone.

```powershell
. .\bench.env.ps1
ssh -i .\valkey-tierbench.pem -o StrictHostKeyChecking=no ubuntu@$GpuAPub "nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader; df -h /mnt/nvme | tail -1; lsblk -no NAME,SIZE,MOUNTPOINT | grep -i nvme" > ..\results\tiers\env.txt 2>&1
ssh -i .\valkey-tierbench.pem -o StrictHostKeyChecking=no ubuntu@$GpuAPub "~/venv/bin/pip list 2>/dev/null | grep -E '^(vllm|lmcache|torch|valkey-glide-sync) '" >> ..\results\tiers\env.txt 2>&1
ssh -i .\valkey-tierbench.pem -o StrictHostKeyChecking=no ec2-user@$ValkeyPub "sudo docker exec valkey valkey-cli INFO server | grep valkey_version" >> ..\results\tiers\env.txt 2>&1
type ..\results\tiers\env.txt
```

The `lsblk` line is the one that matters. It is the evidence that the disk tier
sat on the instance store and not the EBS root, which is the first thing anyone
will attack about a result where the network beats local NVMe.

## 4. Stop the meter

```powershell
.\teardown.ps1
```

Instances also self-terminate 8 hours after launch, but that is a backstop, not
a plan.

## 5. Read the results

```powershell
python ..\benchmarks\compare_tiers.py ..\results\tiers
python ..\benchmarks\compare_tiers.py ..\results\tiers-2026-08-31
```

Run both and compare campaigns. What should reproduce:

- valkey around 6x on one node and the same on the second, 30/30 both
- disk around 2x on one node, 0/30 on the second
- cpu at roughly 1x with 0/30, because 30 documents of KV does not fit in 8 GB

If the second campaign disagrees on the first line, that is the finding and the
blog post needs to know before it publishes rather than after.

## If something breaks

`-Stage deploy` re-uploads the kit to both GPU nodes without reinstalling
anything, which is the fast loop when a script changed:

```powershell
.\run_tiers.ps1 -Stage deploy
```

vLLM failures print the first errors in the log plus the last 60 lines. The full
log lives at `~/vllm.log` on the GPU node.
