<#
.SYNOPSIS
  Provision the three-host tier test bed from a Windows machine using an AWS CLI profile.

.DESCRIPTION
  Native PowerShell so there is no WSL/Git-Bash dependency. Creates a key pair,
  security group, cluster placement group, and three instances, then writes
  bench.env.ps1 with everything the other scripts need.

  Instance sizing rationale:
    gpu-a   g6.xlarge   1x L4 24GB, 250GB ephemeral NVMe
            Qwen2.5-7B-AWQ is ~5.5GB of weights, so one L4 holds the model
            with room for a real KV budget. The ephemeral NVMe is the point:
            it is the local-disk tier, on the same box as the GPU.
    gpu-b   g6.xlarge   identical, and that is the experiment
            The fleet axis runs the cold pass on gpu-a and the cached pass on
            gpu-b. A node-local tier cannot serve a node that did not compute
            the KV; a shared tier can. Two identical nodes is the only way to
            measure that difference rather than assert it.
    valkey  r7i.2xlarge 8 vCPU / 64 GiB
            Deliberately not the bottleneck. The corpus KV is a few GiB.

  Same AZ, same subnet, cluster placement group -- network jitter between the
  hosts is measurement error here.

.EXAMPLE
  .\provision.ps1 -Profile dev -Region us-east-1
#>
[CmdletBinding()]
param(
  [Alias("Profile")]
  [string]$AwsProfile  = $env:AWS_PROFILE,
  [string]$Region      = "us-east-1",
  [string]$AZ          = "",
  [string]$KeyName     = "valkey-tierbench",
  [string]$Prefix      = "tierbench",
  # A list, tried in order. GPU capacity for a PAIR is scarce and varies by
  # family, region and hour; a single hard-coded type turns provisioning into
  # a guessing game. All of these hold Qwen2.5-7B-AWQ with room for a KV
  # budget and ship a local NVMe, which is what the disk tier needs.
  [string[]]$GpuType   = @("g6.xlarge", "g5.xlarge", "g4dn.xlarge"),
  [string]$ValkeyType  = "r7i.2xlarge",
  [int]   $VolumeGB    = 120,

  # Dead-man's switch. Every instance is launched with
  # instance-initiated-shutdown-behavior=terminate and a `shutdown -h` armed
  # at boot, so they terminate themselves after this many hours no matter what
  # happens to the laptop that started the run. GPU hours are not cheap.
  [int]   $MaxHours    = 8,

  # Cluster placement groups minimise inter-host latency but are the most
  # common source of InsufficientInstanceCapacity, and G instances are scarcer
  # than general purpose. Dropping it costs a little network jitter.
  [switch]$NoPlacementGroup,

  # Your public IP, for the SSH ingress rule. Looked up automatically; pass it
  # explicitly if this machine cannot reach checkip.amazonaws.com.
  [string]$MyIp        = ""
)

$ErrorActionPreference = "Stop"
if (-not $AwsProfile) { throw "Pass -Profile or set `$env:AWS_PROFILE" }
if (-not $AZ) { $AZ = "${Region}a" }

function Say($m) { Write-Host "==> $m" -ForegroundColor Cyan }
# Resolve the executable once. A function named `AWS` that calls `aws`
# recurses forever: PowerShell resolves command names case-insensitively
# and functions win over applications.
$script:AwsExe = (Get-Command aws -CommandType Application -ErrorAction Stop |
                  Select-Object -First 1).Source
# Windows PowerShell 5.1 turns ANY native command's stderr into an ErrorRecord,
# and with $ErrorActionPreference = "Stop" that becomes a TERMINATING error --
# even when the command exited 0. aws and ssh both write to stderr routinely on
# conditions we handle ourselves, so every native invocation runs with the
# preference relaxed and is judged on its exit code instead.
function Invoke-Aws {
  $flat = foreach ($a in $args) {
    if ($a -is [array]) { ($a -join ",") } else { $a }
  }
  $prev = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    $raw = & $script:AwsExe --profile $AwsProfile --region $Region @flat 2>&1
    $script:AwsExit = $LASTEXITCODE
  } finally { $ErrorActionPreference = $prev }
  $script:AwsErr = (@($raw) |
    Where-Object { $_ -is [System.Management.Automation.ErrorRecord] } |
    ForEach-Object { $_.ToString() }) -join "`n"
  (@($raw) |
    Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] } |
    ForEach-Object { $_.ToString() }) -join "`n"
}

Say "profile=$AwsProfile region=$Region az=$AZ"
$who = Invoke-Aws sts get-caller-identity --query Arn --output text
Say "authenticated as $who"

# --- AMIs -----------------------------------------------------------------
# GPU hosts need the NVIDIA driver preinstalled; building it at boot adds
# 15 minutes per node and fails in interesting ways. The Deep Learning base
# AMI is the supported way to get one. SSM first, describe-images as the
# fallback, because the SSM alias path has moved between DLAMI generations.
$dlamiParam = "/aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-ubuntu-22.04/latest/ami-id"
$gpuAmi = Invoke-Aws ssm get-parameters --names $dlamiParam `
  --query 'Parameters[0].Value' --output text
if ($script:AwsExit -ne 0 -or [string]::IsNullOrWhiteSpace($gpuAmi) -or $gpuAmi -eq "None") {
  Say "SSM alias missed; falling back to an image search"
  $gpuAmi = Invoke-Aws ec2 describe-images --owners amazon `
    --filters "Name=name,Values=Deep Learning Base OSS Nvidia Driver GPU AMI (Ubuntu 22.04)*" `
              "Name=state,Values=available" `
    --query 'sort_by(Images,&CreationDate)[-1].ImageId' --output text
}
if ([string]::IsNullOrWhiteSpace($gpuAmi) -or $gpuAmi -eq "None") {
  throw "Could not resolve a GPU AMI in $Region. aws said: $script:AwsErr"
}
$valkeyAmi = Invoke-Aws ssm get-parameters `
  --names /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 `
  --query 'Parameters[0].Value' --output text
Say "gpu ami=$gpuAmi  valkey ami=$valkeyAmi"

$vpc = Invoke-Aws ec2 describe-vpcs --filters "Name=isDefault,Values=true" `
  --query 'Vpcs[0].VpcId' --output text
# Empty is the dangerous case, not "None": a failed aws call prints to stderr,
# returns an empty string, and would otherwise sail on into run-instances.
if ([string]::IsNullOrWhiteSpace($vpc) -or $vpc -eq "None") {
  throw "Could not resolve a default VPC in $Region. aws said: $script:AwsErr"
}
Say "vpc=$vpc"

function SubnetIn($az) {
  $s = Invoke-Aws ec2 describe-subnets `
    --filters "Name=vpc-id,Values=$vpc" "Name=availability-zone,Values=$az" `
    --query 'Subnets[0].SubnetId' --output text
  if ([string]::IsNullOrWhiteSpace($s) -or $s -eq "None") { return "" }
  $s
}

# G instances are scarce and capacity is per-AZ, so an explicit -AZ is a
# preference, not a constraint: without one, try every AZ that has a subnet.
# All three hosts must land in the SAME AZ (same subnet, one placement group),
# so the first GPU launch decides it for the rest.
if ($PSBoundParameters.ContainsKey("AZ")) {
  $azCandidates = @($AZ)
} else {
  $azCandidates = @(Invoke-Aws ec2 describe-availability-zones `
    --filters "Name=state,Values=available" `
    --query 'AvailabilityZones[].ZoneName' --output text) -split '\s+' |
    Where-Object { $_ }
  if (-not $azCandidates) { $azCandidates = @($AZ) }
}
# Not every AZ offers every instance type -- us-east-1e has no G instances at
# all. Ask which ones do rather than finding out one failed launch at a time.
function OfferedAzs($type) {
  $o = @(Invoke-Aws ec2 describe-instance-type-offerings `
    --location-type availability-zone `
    --filters "Name=instance-type,Values=$type" `
    --query 'InstanceTypeOfferings[].Location' --output text) -split '\s+' |
    Where-Object { $_ }
  @($azCandidates | Where-Object { $o -contains $_ })
}
Say ("az candidates: " + ($azCandidates -join " "))
Say ("gpu types, in order: " + ($GpuType -join " "))

# --- key pair -------------------------------------------------------------
# A native executable exiting non-zero does not throw in PowerShell, so
# existence checks test $LASTEXITCODE rather than sitting in a try/catch.
$pem = Join-Path (Get-Location) "$KeyName.pem"
Invoke-Aws ec2 describe-key-pairs --key-names $KeyName | Out-Null
$haveKey = ($script:AwsExit -eq 0)
if (-not $haveKey) {
  # Key pairs are per-region, so a .pem left by a run in another region is for
  # a key that does not exist here. It is also read-only by then (icacls grants
  # R only, which is why overwriting it fails), and it may still be the only
  # way into instances elsewhere -- so move it aside rather than clobber it.
  if (Test-Path $pem) {
    $stamp  = Get-Date -Format "yyyyMMdd-HHmmss"
    $backup = "$pem.$stamp.bak"
    icacls $pem /grant:r "$($env:USERNAME):(F)" *> $null
    Move-Item -LiteralPath $pem -Destination $backup -Force
    Say "existing $([IO.Path]::GetFileName($pem)) belongs to another region; kept it as $([IO.Path]::GetFileName($backup))"
  }
  Say "creating key pair -> $pem"
  $material = Invoke-Aws ec2 create-key-pair --key-name $KeyName --query KeyMaterial --output text
  # No BOM: OpenSSH rejects a UTF-8 BOM in a private key.
  [IO.File]::WriteAllText($pem, ($material -replace "`r`n", "`n"))
} elseif (-not (Test-Path $pem)) {
  throw "Key pair '$KeyName' exists in AWS but $pem is not here. Delete the key pair or point -KeyName at a new name."
}

Say "locking down $pem"
icacls $pem /inheritance:r *> $null
icacls $pem /grant:r "$($env:USERNAME):(R)" *> $null

# --- security group -------------------------------------------------------
$sg = Invoke-Aws ec2 describe-security-groups `
  --filters "Name=group-name,Values=$Prefix-sg" "Name=vpc-id,Values=$vpc" `
  --query 'SecurityGroups[0].GroupId' --output text
if ($script:AwsExit -ne 0) { $sg = "None" }

if ($sg -eq "None" -or [string]::IsNullOrWhiteSpace($sg)) {
  if (-not $MyIp) {
    try {
      $MyIp = (Invoke-RestMethod -Uri "https://checkip.amazonaws.com" -TimeoutSec 15).ToString().Trim()
    } catch {
      throw "Could not look up your public IP (checkip.amazonaws.com unreachable). Re-run with -MyIp <your.public.ip>."
    }
  }
  Say "creating security group (ssh from $MyIp/32 only)"
  $sg = Invoke-Aws ec2 create-security-group --group-name "$Prefix-sg" --vpc-id $vpc `
        --description "valkey kv cache tier benchmark" --query GroupId --output text
  Invoke-Aws ec2 authorize-security-group-ingress --group-id $sg `
    --protocol tcp --port 22 --cidr "$MyIp/32" | Out-Null
  # Everything else is reachable only from inside the group: 6379 Valkey,
  # 8000 vLLM (gpu-a must reach gpu-b for the fleet pass), 6999 LMCache
  # metrics (the harness reads num_hit_tokens from it).
  foreach ($p in 6379, 8000, 6999) {
    Invoke-Aws ec2 authorize-security-group-ingress --group-id $sg `
      --protocol tcp --port $p --source-group $sg | Out-Null
  }
}
Say "sg=$sg"

if (-not $NoPlacementGroup) {
  # A cluster placement group pins itself to the AZ of its first instance and
  # keeps that pin after the instance is gone, so a group left over from an
  # earlier attempt refuses every other AZ ("must be launched in the X
  # Availability Zone") and defeats the AZ fallback below. Placement groups
  # cost nothing, so start each provision with a fresh, unpinned one.
  Invoke-Aws ec2 delete-placement-group --group-name "$Prefix-pg" | Out-Null
  $pgDeleted = ($script:AwsExit -eq 0)
  Invoke-Aws ec2 create-placement-group --group-name "$Prefix-pg" --strategy cluster | Out-Null
  if ($script:AwsExit -ne 0) {
    if (-not $pgDeleted) {
      Say "reusing the existing placement group (it still holds instances)"
    } else {
      throw "could not create placement group $Prefix-pg: $script:AwsErr"
    }
  }
}

# --- instances ------------------------------------------------------------
$userDataPath = Join-Path ([IO.Path]::GetTempPath()) "tierbench-userdata.sh"
@"
#!/bin/bash
# Self-terminate after $MaxHours hours so a forgotten GPU run cannot bill forever.
shutdown -h +$($MaxHours * 60)
"@ -replace "`r`n", "`n" | Set-Content -Encoding ascii -NoNewline $userDataPath

function Launch($name, $type, $ami, $az, $subnet) {
  $placementValue = if ($NoPlacementGroup) { "AvailabilityZone=$az" }
                    else { "GroupName=$Prefix-pg,AvailabilityZone=$az" }
  $id = Invoke-Aws ec2 run-instances `
    --image-id $ami --instance-type $type --key-name $KeyName `
    --security-group-ids $sg --subnet-id $subnet `
    --instance-initiated-shutdown-behavior terminate `
    --user-data "file://$userDataPath" `
    --placement $placementValue `
    --block-device-mappings "DeviceName=/dev/sda1,Ebs={VolumeSize=$VolumeGB,VolumeType=gp3,Iops=6000,Throughput=250}" `
    --metadata-options "HttpTokens=required,HttpEndpoint=enabled" `
    --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$Prefix-$name}]" `
    --query 'Instances[0].InstanceId' --output text
  # run-instances can fail for reasons unrelated to the request being wrong --
  # InsufficientInstanceCapacity for a cluster placement group of G instances
  # is the common one. Without this check an empty id propagates into
  # bench.env.ps1 and a "gpu" host silently becomes something else.
  if ($script:AwsExit -ne 0 -or [string]::IsNullOrWhiteSpace($id) -or $id -eq "None") {
    throw ("could not launch the $name instance ($type) in ${az}.`n  aws said: " +
           $script:AwsErr + "`n" +
           "  If this is InsufficientInstanceCapacity, try -NoPlacementGroup, " +
           "-GpuType g5.xlarge, or another -Region.")
  }
  $id
}

# Both GPUs in ONE request. Asking for them one at a time is how you end up
# holding a running gpu-a in an AZ that will not sell you a gpu-b: the first
# call succeeds, the second hits InsufficientInstanceCapacity, and the orphan
# bills until someone notices. --count 2 is allocated atomically, so a capacity
# shortfall costs nothing and simply means "try the next AZ".
function LaunchGpuPair($type, $az, $subnet) {
  $placementValue = if ($NoPlacementGroup) { "AvailabilityZone=$az" }
                    else { "GroupName=$Prefix-pg,AvailabilityZone=$az" }
  $out = Invoke-Aws ec2 run-instances `
    --image-id $gpuAmi --instance-type $type --key-name $KeyName `
    --security-group-ids $sg --subnet-id $subnet `
    --count 2 `
    --instance-initiated-shutdown-behavior terminate `
    --user-data "file://$userDataPath" `
    --placement $placementValue `
    --block-device-mappings "DeviceName=/dev/sda1,Ebs={VolumeSize=$VolumeGB,VolumeType=gp3,Iops=6000,Throughput=250}" `
    --metadata-options "HttpTokens=required,HttpEndpoint=enabled" `
    --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$Prefix-gpu}]" `
    --query 'Instances[].InstanceId' --output text
  if ($script:AwsExit -ne 0) { throw "run-instances failed: $script:AwsErr" }
  $pair = @($out -split '\s+' | Where-Object { $_ })
  if ($pair.Count -ne 2) {
    throw "expected 2 gpu instance ids, got $($pair.Count): $out"
  }
  $pair
}

# A capacity error here is not a failure, it is the answer to "which type and
# which AZ"; anything else is fatal. All three hosts share the winning AZ.
$gpuPair = @(); $subnet = ""; $chosenType = ""
foreach ($type in $GpuType) {
  $typeAzs = OfferedAzs $type
  if (-not $typeAzs) { Say "$type is not offered anywhere in $Region, skipping"; continue }
  foreach ($az in $typeAzs) {
    $candidateSubnet = SubnetIn $az
    if (-not $candidateSubnet) { Say "$az has no subnet in $vpc, skipping"; continue }
    Say "trying a $type pair in $az (subnet $candidateSubnet)"
    try {
      $gpuPair = LaunchGpuPair $type $az $candidateSubnet
      $AZ = $az; $subnet = $candidateSubnet; $chosenType = $type
      break
    } catch {
      if ($script:AwsErr -match "InsufficientInstanceCapacity") {
        Say "no $type capacity for a pair in $az"
        continue
      }
      if ($script:AwsErr -match "Unsupported|not supported in your requested Availability Zone") {
        Say "$type is not offered in $az"
        continue
      }
      if ($script:AwsErr -match "must be launched in the (\S+) Availability Zone") {
        throw ("placement group $Prefix-pg is still pinned to $($Matches[1]) by a " +
               "live instance. Terminate anything left from an earlier attempt, or " +
               "re-run with -NoPlacementGroup.")
      }
      throw
    }
  }
  if ($gpuPair.Count -eq 2) { break }
}
if ($gpuPair.Count -ne 2) {
  throw ("no capacity for a PAIR of " + ($GpuType -join "/") + " in any AZ of " +
         "$Region. Try another -Region (us-west-2 has the largest GPU fleet), " +
         "or ask for spot capacity.")
}
$GpuType = $chosenType
Say "landed on $GpuType in $AZ"
$gpuAId = $gpuPair[0]; $gpuBId = $gpuPair[1]
Say "az=$AZ subnet=$subnet gpus=$gpuAId $gpuBId"
# The pair launched under one Name tag; split them so the console shows which
# node the fleet pass reads from.
Invoke-Aws ec2 create-tags --resources $gpuAId --tags "Key=Name,Value=$Prefix-gpu-a" | Out-Null
Invoke-Aws ec2 create-tags --resources $gpuBId --tags "Key=Name,Value=$Prefix-gpu-b" | Out-Null

$vkId = Launch "valkey" $ValkeyType $valkeyAmi $AZ $subnet
Say "all three instances self-terminate after $MaxHours h even if teardown never runs"
Say "waiting for $gpuAId (gpu-a), $gpuBId (gpu-b), $vkId (valkey)"
Invoke-Aws ec2 wait instance-running --instance-ids $gpuAId $gpuBId $vkId

function IpOf($id, $field) {
  Invoke-Aws ec2 describe-instances --instance-ids $id `
    --query "Reservations[0].Instances[0].$field" --output text
}

# Three instances, three identities. A run where gpu-a and gpu-b collapse into
# one host is not a slower experiment, it is a different one: the fleet pass
# would be reading the cache it just wrote, on the node that wrote it.
$ids = @{ "gpu-a" = $gpuAId; "gpu-b" = $gpuBId; "valkey" = $vkId }
foreach ($k in $ids.Keys) {
  if ([string]::IsNullOrWhiteSpace($ids[$k])) { throw "empty instance id for $k" }
}
if (($ids.Values | Select-Object -Unique).Count -ne 3) {
  throw "the three launches did not produce three distinct instance ids"
}

$gpuAPub  = IpOf $gpuAId PublicIpAddress
$gpuAPriv = IpOf $gpuAId PrivateIpAddress
$gpuBPub  = IpOf $gpuBId PublicIpAddress
$gpuBPriv = IpOf $gpuBId PrivateIpAddress
$vkPriv   = IpOf $vkId   PrivateIpAddress
$vkPub    = IpOf $vkId   PublicIpAddress

foreach ($pair in @(@("gpu-a public", $gpuAPub), @("gpu-a private", $gpuAPriv),
                    @("gpu-b public", $gpuBPub), @("gpu-b private", $gpuBPriv),
                    @("valkey private", $vkPriv), @("valkey public", $vkPub))) {
  if ([string]::IsNullOrWhiteSpace($pair[1]) -or $pair[1] -eq "None") {
    throw "could not resolve $($pair[0]) ip; aws said: $script:AwsErr"
  }
}
if ($gpuAPub -eq $gpuBPub) {
  throw ("gpu-a and gpu-b resolved to the SAME public IP ($gpuAPub). The fleet " +
         "pass would be measuring a co-resident node.")
}

@"
`$env:AWS_PROFILE  = "$AwsProfile"
`$Global:Region    = "$Region"
`$Global:GpuAId    = "$gpuAId"
`$Global:GpuBId    = "$gpuBId"
`$Global:ValkeyId  = "$vkId"
`$Global:GpuAPub   = "$gpuAPub"
`$Global:GpuAPriv  = "$gpuAPriv"
`$Global:GpuBPub   = "$gpuBPub"
`$Global:GpuBPriv  = "$gpuBPriv"
`$Global:ValkeyPub = "$vkPub"
`$Global:ValkeyPriv= "$vkPriv"
`$Global:KeyName   = "$KeyName"
`$Global:Prefix    = "$Prefix"
`$Global:GpuType   = "$GpuType"
`$Global:ValkeyType= "$ValkeyType"
`$Global:GpuUser   = "ubuntu"
`$Global:ValkeyUser= "ec2-user"
"@ | Set-Content -Encoding ascii bench.env.ps1

Say "wrote bench.env.ps1"
Get-Content bench.env.ps1

Write-Host @"

Instances are up but cloud-init may still be finishing; run_tiers.ps1 waits
for SSH on its own.

Next:
  .\run_tiers.ps1 -Stage sanity

Tear down when finished (two GPUs are not cheap to leave running):
  .\teardown.ps1
"@ -ForegroundColor Green
