<#
  Local external trigger: every day at 19:20 (Beijing) call workflow_dispatch
  with the already-authenticated gh CLI. Works even when GitHub's native cron
  does not deliver, as long as this PC is on and online at 19:20.
  Log: <repo>\.local\trigger-cloud.log
#>
param(
    [ValidateSet('book', 'check')]
    [string]$Mode = 'book'
)

$ErrorActionPreference = 'Stop'
$repo = 'TaoTao-319/HDU-Library-CloudBooking'
$workflow = 'main.yml'
$root = Split-Path -Parent $PSScriptRoot
$logDir = Join-Path $root '.local'
$logPath = Join-Path $logDir 'trigger-cloud.log'

New-Item -ItemType Directory -Force -Path $logDir | Out-Null

function Write-Log([string]$message) {
    $line = '{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $message
    Add-Content -LiteralPath $logPath -Value $line -Encoding utf8
}

$gh = (Get-Command gh -ErrorAction SilentlyContinue).Source
if (-not $gh) {
    $candidate = 'C:\Program Files\GitHub CLI\gh.exe'
    if (Test-Path -LiteralPath $candidate) { $gh = $candidate }
}
if (-not $gh) {
    Write-Log 'ERROR: gh.exe not found'
    exit 1
}

Write-Log "trigger start mode=$Mode gh=$gh"

# The booking window closes at 20:15 Beijing. A dispatch after that only
# produces a red run that skips every round, so skip it here instead.
if ($Mode -eq 'book') {
    $windowClose = Get-Date -Hour 20 -Minute 14 -Second 0
    if ((Get-Date) -ge $windowClose) {
        Write-Log 'skip: past 20:14 window, nothing to do'
        exit 0
    }
}

$deadline = (Get-Date).AddMinutes(8)
$attempt = 0
$ok = $false
while (((Get-Date) -lt $deadline) -and (-not $ok)) {
    $attempt++
    $output = & $gh workflow run $workflow --repo $repo -f "mode=$Mode" 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Log "attempt $attempt dispatched"
        $ok = $true
    }
    else {
        Write-Log ("attempt {0} failed: {1}" -f $attempt, ($output -join ' | '))
        Start-Sleep -Seconds 30
    }
}

if (-not $ok) {
    Write-Log 'ERROR: all dispatch attempts failed'
    exit 1
}
exit 0
