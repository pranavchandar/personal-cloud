# Reconcile the index with the filesystem: pick up new files, drop deleted ones.
#
# `scan` adds and updates, and now prunes rows whose file is gone, so one pass
# covers both directions. A re-scan is far cheaper than the first one -- unchanged
# files are skipped on mtime, so it is a directory walk plus stat calls rather than
# a re-read of 166 GB.
#
#   .\sync.ps1                  # sync once, now, then tag new photos
#                               #   (same as double-clicking "Update Library.cmd")
#   .\sync.ps1 -Register        # sync + tag automatically every day at 03:00
#   .\sync.ps1 -Unregister      # stop doing that
#   .\sync.ps1 -PruneOnly       # just drop deleted files (fast, ~1 min)
#
param(
    [string]$Root = '',              # default: library_root in config.json
    [switch]$PruneOnly,
    [switch]$Register,
    [switch]$Unregister,
    [string]$At = '03:00'
)

$ErrorActionPreference = 'Continue'
$RootDir  = $PSScriptRoot
$Python   = Join-Path $RootDir 'venv\Scripts\python.exe'
$Script   = Join-Path $RootDir 'photoindex.py'
$Log      = Join-Path $RootDir 'sync.log'
$TaskName = 'PhotoIndexSync'

if (-not $Root -and -not $Unregister) {
    # UTF8 explicitly: PowerShell 5.1 reads BOM-less files as ANSI, which would
    # mangle any non-ASCII folder name
    $cfg = Join-Path $RootDir 'config.json'
    if (-not (Test-Path $cfg)) { Write-Error "No config.json - copy config.example.json and edit it."; exit 1 }
    $Root = (Get-Content $cfg -Raw -Encoding UTF8 | ConvertFrom-Json).library_root
}

function Write-Log([string]$m) {
    $line = "$(Get-Date -Format s)  $m"
    Write-Host $line
    Add-Content -Path $Log -Value $line -Encoding utf8
}

if ($Unregister) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed '$TaskName'."
    } else { Write-Host "No such task '$TaskName'." }
    return
}

if ($Register) {
    $argline = "-ExecutionPolicy Bypass -WindowStyle Hidden -File `"$PSCommandPath`" " +
               "-Root `"$Root`""
    $action  = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $argline `
                   -WorkingDirectory $RootDir
    $trigger = New-ScheduledTaskTrigger -Daily -At $At
    # StartWhenAvailable so a missed run (PC off at 03:00) happens at next boot
    $set     = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
                   -DontStopIfGoingOnBatteries -StartWhenAvailable `
                   -ExecutionTimeLimit (New-TimeSpan -Hours 6)
    try {
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
            -Settings $set -Force -ErrorAction Stop | Out-Null
    } catch { Write-Error "Could not register: $_"; exit 1 }
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Write-Host "Registered '$TaskName' - syncs daily at $At."
        Write-Host "Remove with: .\sync.ps1 -Unregister"
    } else { Write-Error "Register reported success but task is absent."; exit 1 }
    return
}

# Refuse to run against a disconnected drive: a scan would find nothing and the
# prune step would see the whole library as deleted. (photoindex.py guards this
# too; failing here keeps it out of the log entirely.)
if (-not (Test-Path $Root)) {
    Write-Log "source not available ($Root) - nothing done"
    exit 1
}

$env:HF_HUB_OFFLINE = '1'
if ($PruneOnly) {
    Write-Log "prune only"
    & $Python $Script prune --root $Root 2>&1 | ForEach-Object { Write-Log "  $_" }
} else {
    Write-Log "sync starting: $Root"
    & $Python $Script scan $Root 2>&1 | ForEach-Object { Write-Log "  $_" }
}
$scanExit = $LASTEXITCODE
Write-Log "sync finished (exit $scanExit)"

# New photos only appear in the web UI once tagged. Without this they sat hidden
# until the next Windows logon. Videos need no tagging and are visible already.
if (-not $PruneOnly -and $scanExit -eq 0) {
    Write-Log "tagging new photos (progress in captioning.log)"
    & (Join-Path $RootDir 'run_captioning.ps1') -TagsOnly
}
