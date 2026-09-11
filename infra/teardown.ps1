<#
.SYNOPSIS
  Terminate everything provision.ps1 created.
.EXAMPLE
  .\teardown.ps1
  .\teardown.ps1 -KeepSecurityGroup
#>
[CmdletBinding()]
param(
  [string]$EnvFile = "bench.env.ps1",
  [switch]$KeepSecurityGroup
)
$ErrorActionPreference = "Stop"
if (-not (Test-Path $EnvFile)) { throw "$EnvFile not found." }
. (Resolve-Path $EnvFile)

function Say($m) { Write-Host "==> $m" -ForegroundColor Cyan }
$script:AwsExe = (Get-Command aws -CommandType Application -ErrorAction Stop |
                  Select-Object -First 1).Source
# Windows PowerShell 5.1 turns ANY native command's stderr into an ErrorRecord,
# and with $ErrorActionPreference = "Stop" that becomes a TERMINATING error --
# even when the command exited 0. Every native invocation runs with the
# preference relaxed and is judged on its exit code instead.
function Invoke-Aws {
  $flat = foreach ($a in $args) {
    if ($a -is [array]) { ($a -join ",") } else { $a }
  }
  $prev = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    $raw = & $script:AwsExe --region $Region @flat 2>&1
    $script:AwsExit = $LASTEXITCODE
  } finally { $ErrorActionPreference = $prev }
  (@($raw) |
    Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] } |
    ForEach-Object { $_.ToString() }) -join "`n"
}

# A failed provision can leave one id blank; passing "" to the CLI errors out
# and the surviving instances keep billing. GPUs make that expensive.
$ids = @($GpuAId, $GpuBId, $ValkeyId) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
if (-not $ids) { throw "bench.env.ps1 has no instance ids; terminate by hand in the console" }
Say ("terminating " + ($ids -join " "))
Invoke-Aws ec2 terminate-instances --instance-ids @ids | Out-Null
Invoke-Aws ec2 wait instance-terminated --instance-ids @ids

Say "deleting placement group $Prefix-pg"
try { Invoke-Aws ec2 delete-placement-group --group-name "$Prefix-pg" | Out-Null } catch {}

if (-not $KeepSecurityGroup) {
  try {
    $sg = Invoke-Aws ec2 describe-security-groups --filters "Name=group-name,Values=$Prefix-sg" `
          --query 'SecurityGroups[0].GroupId' --output text
    if ($sg -and $sg -ne "None") {
      Say "deleting security group $sg"
      Invoke-Aws ec2 delete-security-group --group-id $sg | Out-Null
    }
  } catch { Write-Warning "security group not deleted: $_" }
}

Say "done. Key pair $KeyName and .\$KeyName.pem are left in place."
Say "Instances are gone; check the console if you want to be certain."
