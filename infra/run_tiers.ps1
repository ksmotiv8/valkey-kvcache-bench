<#
.SYNOPSIS
  Deploy the kit to the three hosts, run the tier matrix, and pull results back.

.DESCRIPTION
  Steps, each selectable with -Stage so a failure part-way does not mean
  starting over:
    setup    wait for ssh, upload the kit, install valkey and both GPU nodes
    deploy   re-upload the kit only -- no installs. The iteration loop when a
             script changed but the hosts are already built.
    sanity   one tier, two documents, end to end -- the cheap failure
    single   cpu / disk / valkey on one node (where local tiers win)
    fleet    cold pass on gpu-a, cached pass on gpu-b (where they cannot)
    all      sanity + single + fleet

  Each step is minutes, not hours, so it runs synchronously. Resume with
  -Stage if the laptop sleeps mid-run.

.EXAMPLE
  .\run_tiers.ps1 -Stage setup
  .\run_tiers.ps1 -Stage sanity
  .\run_tiers.ps1 -Stage all
#>
[CmdletBinding()]
param(
  [ValidateSet("setup","deploy","sanity","single","fleet","all")] [string]$Stage = "all",
  [string[]]$Tiers = @("cpu","disk","valkey"),
  [string]$EnvFile = "bench.env.ps1",
  [string]$OutDir  = "",
  [switch]$SkipSetup
)

$ErrorActionPreference = "Stop"
if (-not (Test-Path $EnvFile)) { throw "$EnvFile not found. Run .\provision.ps1 first." }
. (Resolve-Path $EnvFile)

$pem = Join-Path (Get-Location) "$KeyName.pem"
if (-not (Test-Path $pem)) { throw "$pem not found." }

function Say($m) { Write-Host "==> $m" -ForegroundColor Cyan }
$sshOpts = @("-i", $pem, "-o", "StrictHostKeyChecking=no",
             "-o", "UserKnownHostsFile=NUL", "-o", "ConnectTimeout=10",
             "-o", "ServerAliveInterval=30")

# Windows PowerShell 5.1 turns a native command's stderr into a terminating
# error when $ErrorActionPreference is "Stop". ssh and scp write to stderr on
# every ordinary condition, so all native calls run with the preference
# relaxed and are judged on exit code.
function Invoke-Native([scriptblock]$block) {
  $prev = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try { & $block } finally { $ErrorActionPreference = $prev }
}
function Remote($user, $hostIp, $cmd) {
  Invoke-Native { & ssh @sshOpts "$user@$hostIp" $cmd }
  if ($LASTEXITCODE -ne 0) { throw "ssh to $hostIp failed ($LASTEXITCODE): $cmd" }
}
function RemoteSoft($user, $hostIp, $cmd) {
  $out = Invoke-Native { & ssh @sshOpts "$user@$hostIp" $cmd 2>&1 }
  return @{ code = $LASTEXITCODE
            out  = ((@($out) | ForEach-Object { $_.ToString() }) -join "`n") }
}
function WaitForSsh($user, $hostIp, $label) {
  Say "waiting for ssh on $label ($hostIp)"
  for ($i = 0; $i -lt 60; $i++) {
    if ((RemoteSoft $user $hostIp "echo ok").code -eq 0) { Say "$label reachable"; return }
    Start-Sleep -Seconds 10
  }
  throw "$label never came up on ssh"
}

$kitRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
if (-not $OutDir) { $OutDir = Join-Path $kitRoot "results\tiers" }
$gpus = @(@{ n = "gpu-a"; ip = $GpuAPub }, @{ n = "gpu-b"; ip = $GpuBPub })

# --- setup ----------------------------------------------------------------
if ($Stage -eq "setup" -or $Stage -eq "deploy" -or -not $SkipSetup) {
  WaitForSsh $ValkeyUser $ValkeyPub "valkey"
  foreach ($g in $gpus) { WaitForSsh $GpuUser $g.ip $g.n }

  Say "uploading kit"
  foreach ($g in $gpus) {
    Remote $GpuUser $g.ip "rm -rf ~/kit && mkdir -p ~/kit/infra"
    # Only the shell scripts from infra\ go to the hosts. Copying the whole
    # directory would ship the .pem -- the private key for every host here.
    Invoke-Native { & scp @sshOpts -r "$kitRoot\benchmarks" "$kitRoot\corpus" `
        "${GpuUser}@$($g.ip):~/kit/" }
    if ($LASTEXITCODE -ne 0) { throw "scp of kit to $($g.n) failed" }
    Invoke-Native { & scp @sshOpts "$kitRoot\infra\run_tier.sh" "$kitRoot\infra\setup_gpu.sh" `
        "${GpuUser}@$($g.ip):~/kit/infra/" }
    if ($LASTEXITCODE -ne 0) { throw "scp of infra to $($g.n) failed" }
    Remote $GpuUser $g.ip "chmod +x ~/kit/infra/*.sh; rm -rf ~/kit/benchmarks/__pycache__"
  }
  Invoke-Native { & scp @sshOpts "$kitRoot\infra\setup_valkey.sh" "${ValkeyUser}@${ValkeyPub}:~/" }
  if ($LASTEXITCODE -ne 0) { throw "scp of setup_valkey.sh failed" }

  if ($Stage -eq "deploy") {
    Say "kit re-uploaded; skipping installs"
    return
  }

  Say "valkey setup"
  Remote $ValkeyUser $ValkeyPub "chmod +x ~/setup_valkey.sh && ~/setup_valkey.sh"

  # vLLM and torch are a large install; this is the slow step, ~10 min a node.
  foreach ($g in $gpus) {
    Say "$($g.n) setup (installing vllm + lmcache, this takes a while)"
    Remote $GpuUser $g.ip "~/kit/infra/setup_gpu.sh"
  }
  Say "setup complete"
  if ($Stage -eq "setup") { return }
}

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

function RunTier($tier, $label, $cachedUrl, $limit) {
  $out = "~/kit/${label}_${tier}.json"
  Say "$label / $tier : starting vllm on gpu-a"
  Remote $GpuUser $GpuAPub "~/kit/infra/run_tier.sh serve $tier $ValkeyPriv"
  if ($cachedUrl) {
    Say "$label / $tier : starting vllm on gpu-b"
    Remote $GpuUser $GpuBPub "~/kit/infra/run_tier.sh serve $tier $ValkeyPriv"
  }
  Remote $GpuUser $GpuAPub "~/kit/infra/run_tier.sh bench $tier $ValkeyPriv $out '$cachedUrl' $limit"
  Invoke-Native { & scp @sshOpts "${GpuUser}@${GpuAPub}:$out" "$OutDir\${label}_${tier}.json" }
  if ($LASTEXITCODE -ne 0) { throw "could not download results for $label/$tier" }
  Remote $GpuUser $GpuAPub "~/kit/infra/run_tier.sh stop"
  if ($cachedUrl) { Remote $GpuUser $GpuBPub "~/kit/infra/run_tier.sh stop" }
  Say "$label / $tier done -> $OutDir\${label}_${tier}.json"
}

# --- sanity ---------------------------------------------------------------
# Two documents on the cpu tier. Everything downstream depends on vLLM coming
# up, LMCache loading, and the hit counter moving; this proves all three for
# the price of a couple of minutes.
if ($Stage -eq "sanity" -or $Stage -eq "all") {
  RunTier "cpu" "sanity" "" 2
  $j = Get-Content "$OutDir\sanity_cpu.json" -Raw | ConvertFrom-Json
  Say ("sanity: {0}/{1} docs L2-confirmed, median speedup {2:N2}x" -f `
       $j.l2_confirmed, $j.docs, $j.speedup_median)
  if ($j.l2_confirmed -lt 1) {
    throw ("sanity run confirmed no reuse. Check that the server started with " +
           "--no-enable-prefix-caching and that internal_api_server_enabled is " +
           "set in the tier config.")
  }
  if ($Stage -eq "sanity") { return }
}

# --- single node ----------------------------------------------------------
# One node, three tiers. This is where the local tiers are expected to win,
# and publishing that is the point.
if ($Stage -eq "single" -or $Stage -eq "all") {
  foreach ($t in $Tiers) { RunTier $t "single" "" 0 }
}

# --- fleet ----------------------------------------------------------------
# Cold pass on gpu-a, cached pass on gpu-b. A node-local tier cannot serve a
# node that did not compute the KV, so cpu and disk should collapse to no
# reuse here while valkey holds. That contrast is the finding.
if ($Stage -eq "fleet" -or $Stage -eq "all") {
  $cached = "http://${GpuBPriv}:8000"
  foreach ($t in $Tiers) { RunTier $t "fleet" $cached 0 }
}

Say "results in $OutDir"
Get-ChildItem $OutDir | Select-Object Name, Length | Format-Table
Write-Host @"

Compare them:
  python ..\benchmarks\compare_tiers.py $OutDir

Tear down when finished (two GPUs are not cheap to leave running):
  .\teardown.ps1
"@ -ForegroundColor Green
