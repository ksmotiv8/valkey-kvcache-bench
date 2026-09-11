<#
.SYNOPSIS
  One command: preflight, provision, deploy, sanity check, run the matrix,
  download results, and optionally tear down.

.DESCRIPTION
  Checks that aws, ssh and scp are on PATH, that the profile authenticates,
  that the region has a default VPC, and that the account actually has GPU
  quota -- all BEFORE launching anything. Then shows the hourly cost and
  waits for a y.

  Re-running is safe: it reuses instances that are already up rather than
  launching a second set, and a stale bench.env.ps1 pointing at terminated
  instances is detected and replaced.

.EXAMPLE
  .\Run-TierBench.ps1 -AwsProfile dev
  .\Run-TierBench.ps1 -AwsProfile dev -Stage sanity
  .\Run-TierBench.ps1 -AwsProfile dev -Teardown
#>
[CmdletBinding()]
param(
  [Alias("Profile")] [string]$AwsProfile = $env:AWS_PROFILE,
  [string]$Region = "us-east-1",
  [ValidateSet("setup","sanity","single","fleet","all")] [string]$Stage = "all",
  [string]$AZ = "",
  [string[]]$GpuType = @(),
  [string]$ValkeyType = "",
  [string]$MyIp = "",
  [switch]$NoPlacementGroup,
  [switch]$Teardown,
  [switch]$Yes
)

$ErrorActionPreference = "Stop"
if (-not $AwsProfile) { throw "Pass -AwsProfile or set `$env:AWS_PROFILE" }
function Say($m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Invoke-Native([scriptblock]$block) {
  $prev = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try { & $block } finally { $ErrorActionPreference = $prev }
}

# --- preflight ------------------------------------------------------------
Say "preflight"
foreach ($exe in "aws", "ssh", "scp") {
  if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) {
    throw "$exe is not on PATH. Install the AWS CLI and the Windows OpenSSH client."
  }
}
$who = Invoke-Native { & aws --profile $AwsProfile --region $Region sts get-caller-identity --query Arn --output text }
if ($LASTEXITCODE -ne 0) { throw "profile '$AwsProfile' does not authenticate" }
Say "authenticated as $who"

$vpc = Invoke-Native { & aws --profile $AwsProfile --region $Region ec2 describe-vpcs `
        --filters "Name=isDefault,Values=true" --query 'Vpcs[0].VpcId' --output text }
if ([string]::IsNullOrWhiteSpace($vpc) -or $vpc -eq "None") {
  throw "no default VPC in $Region; provision.ps1 needs one"
}

# G-instance quota is denominated in vCPUs and is 0 on accounts that have never
# launched one. Two g6.xlarge needs 8. Failing here costs a second; failing
# after provisioning a Valkey host costs money.
$quota = Invoke-Native { & aws --profile $AwsProfile --region $Region service-quotas get-service-quota `
          --service-code ec2 --quota-code L-DB2E81BA --query 'Quota.Value' --output text }
if ($LASTEXITCODE -eq 0 -and $quota -and [double]$quota -lt 8) {
  throw ("GPU quota is $quota vCPUs; two g6.xlarge needs 8. Request an increase: " +
         "aws service-quotas request-service-quota-increase --service-code ec2 " +
         "--quota-code L-DB2E81BA --desired-value 8")
}
Say "gpu quota ok ($quota vCPUs)"

# --- cost gate ------------------------------------------------------------
if (-not $Yes) {
  $gpuLabel = if ($GpuType) { $GpuType } else { "g6.xlarge" }
  $vkLabel  = if ($ValkeyType) { $ValkeyType } else { "r7i.2xlarge" }
  Write-Host @"

  2 x $gpuLabel  (24GB GPU, local NVMe)   ~`$0.81-1.01/hr each
  1 x $vkLabel (valkey)                   ~`$0.53/hr
  ------------------------------------------------------
  about `$2.14/hour. A full matrix is roughly an hour of
  runtime plus ~20 minutes of setup, so budget `$3-5.

  Every instance self-terminates after 8 hours regardless.

"@ -ForegroundColor Yellow
  $a = Read-Host "launch? (y/N)"
  if ($a -ne "y") { Say "nothing launched"; return }
}

# --- provision ------------------------------------------------------------
$here = Split-Path -Parent $PSCommandPath
Push-Location $here
try {
  $needProvision = $true
  if (Test-Path "bench.env.ps1") {
    . (Resolve-Path "bench.env.ps1")
    $state = Invoke-Native { & aws --profile $AwsProfile --region $Region ec2 describe-instances `
              --instance-ids $GpuAId $GpuBId $ValkeyId `
              --query 'Reservations[].Instances[].State.Name' --output text }
    if ($LASTEXITCODE -eq 0 -and $state -and $state -notmatch "terminated|shutting-down") {
      Say "reusing the instances already in bench.env.ps1"
      $needProvision = $false
    } else {
      Say "bench.env.ps1 points at instances that are gone; re-provisioning"
    }
  }
  if ($needProvision) {
    # Splatting MUST be a hashtable. Splatting an array passes the elements
    # POSITIONALLY -- "-Profile" lands in $AwsProfile and the profile name
    # lands in $Region, which fails later and confusingly.
    $pArgs = @{ AwsProfile = $AwsProfile; Region = $Region }
    if ($AZ) { $pArgs.AZ = $AZ }
    if ($GpuType) { $pArgs.GpuType = $GpuType }
    if ($ValkeyType) { $pArgs.ValkeyType = $ValkeyType }
    if ($MyIp) { $pArgs.MyIp = $MyIp }
    if ($NoPlacementGroup) { $pArgs.NoPlacementGroup = $true }
    & (Join-Path $here "provision.ps1") @pArgs
  }

  & (Join-Path $here "run_tiers.ps1") -Stage $Stage -SkipSetup:(-not $needProvision -and $Stage -ne "setup")

  if ($Teardown) {
    Say "tearing down"
    & (Join-Path $here "teardown.ps1")
  } else {
    Write-Host "`nInstances are STILL RUNNING. Stop the meter with .\teardown.ps1" -ForegroundColor Yellow
  }
} finally { Pop-Location }
