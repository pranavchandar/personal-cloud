# Keep captioning until nothing is pending, surviving crashes and reboots.
#
# The caption pass is already resumable (every row carries a state; commits land
# every 16 images with synchronous=FULL, so a power cut costs ~22s of work).
# What was missing was something to relaunch it. This loop handles a crash;
# -Register adds a logon task that handles a power outage.
#
#   .\run_captioning.ps1                 # run until done
#   .\run_captioning.ps1 -Register       # also auto-resume at every logon
#   .\run_captioning.ps1 -Unregister     # stop auto-resuming
#   .\run_captioning.ps1 -TagsOnly       # skip prose descriptions (~14h vs ~22h)
#
param(
    [int]$Batch = 16,
    [int]$MaxRetries = 100,
    [switch]$TagsOnly,
    [switch]$Register,
    [switch]$Unregister
)

# NOT 'Stop'. Native executables write progress bars to stderr; PowerShell 5.1
# wraps those in NativeCommandError records, and under 'Stop' that terminated
# this script the instant the model began loading -- silently, because the
# wrapper's own stderr went nowhere.
$ErrorActionPreference = 'Continue'

$Root     = $PSScriptRoot
$Python   = Join-Path $Root 'venv\Scripts\python.exe'
$Script   = Join-Path $Root 'photoindex.py'
$Log      = Join-Path $Root 'captioning.log'
$OutFile  = Join-Path $Root 'captioning.out'
$ErrFile  = Join-Path $Root 'captioning.err'
$TaskName = 'PhotoIndexCaptioning'

function Write-Log([string]$msg) {
    $line = "$(Get-Date -Format s)  $msg"
    Write-Host $line
    # utf8, not Tee-Object: Tee/redirection default to UTF-16 in PS 5.1, which
    # makes the log unreadable to grep, tail, and every other text tool.
    Add-Content -Path $Log -Value $line -Encoding utf8
}

if ($Register) {
    $argline = "-ExecutionPolicy Bypass -WindowStyle Hidden -File `"$PSCommandPath`" -Batch $Batch"
    if ($TagsOnly) { $argline += " -TagsOnly" }
    $action  = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $argline `
                                       -WorkingDirectory $Root
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $set     = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
                   -DontStopIfGoingOnBatteries -StartWhenAvailable `
                   -ExecutionTimeLimit ([TimeSpan]::Zero)
    try {
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
            -Settings $set -Force -ErrorAction Stop | Out-Null
    } catch {
        Write-Error "Could not register task: $_"; exit 1
    }
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Write-Host "Registered '$TaskName' - captioning resumes at every logon."
        Write-Host "Remove with: .\run_captioning.ps1 -Unregister"
    } else {
        Write-Error "Register reported success but the task is absent."; exit 1
    }
    return
}
if ($Unregister) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed '$TaskName'."
    } else {
        Write-Host "No such task '$TaskName' - nothing to remove."
    }
    return
}

# One run at a time. The logon task, the nightly sync and a manual update can all
# start this; two copies would each load the model onto an 8GB GPU and tag the
# same rows. The OS frees the mutex when this process exits, even on a crash.
$mutex = New-Object System.Threading.Mutex($false, 'Global\PhotoIndexCaptioning')
try { $owned = $mutex.WaitOne(0) }
catch [System.Threading.AbandonedMutexException] { $owned = $true }  # holder crashed
if (-not $owned) {
    Write-Log "another tagging run is already active - leaving it to finish"
    exit 0
}

$env:HF_HOME = Join-Path (Split-Path $Root -Qualifier) 'hf-cache'
# The model is fully downloaded, so nothing needs the network. Without this,
# transformers contacts huggingface.co on every load to check the model revision
# -- metadata only, never photo data, but there is no reason to allow it at all.
$env:HF_HUB_OFFLINE = '1'

function Get-Pending {
    # A dedicated subcommand, not inline python: a here-string mangled the quotes
    # in `python -c`, returning empty, which [int] cast to 0 -- so this loop would
    # have declared "nothing pending" and exited without captioning anything.
    $out = & $Python $Script pending 2>$null
    if ($LASTEXITCODE -ne 0 -or "$out" -notmatch '^\s*\d+\s*$') {
        throw "could not read pending count (got '$out', exit $LASTEXITCODE)"
    }
    return [int]("$out".Trim())
}

# The photos are on an external USB drive. After a power cut a logon task can fire
# before the drive is mounted; starting then would fail every image. Wait for it.
$SourceDir = (Get-Content (Join-Path $Root 'config.json') -Raw -Encoding UTF8 |
              ConvertFrom-Json).library_root
for ($w = 1; $w -le 30; $w++) {
    if (Test-Path $SourceDir) { break }
    Write-Log "waiting for source drive ($SourceDir) - attempt $w/30"
    Start-Sleep -Seconds 20
}
if (-not (Test-Path $SourceDir)) {
    Write-Log "source drive not available after 10min - exiting without changes"
    exit 1
}

try {
    for ($try = 1; $try -le $MaxRetries; $try++) {
        $pending = Get-Pending
        if ($pending -eq 0) { Write-Log "nothing pending - complete"; break }
        Write-Log "attempt $try of $MaxRetries, $pending pending"

        # Start-Process with separate redirect files: this is what avoids the
        # NativeCommandError trap entirely, and gives a reliable exit code.
        $pyArgs = @('-u', $Script, 'caption', '--batch', $Batch)
        if ($TagsOnly) { $pyArgs += '--tags-only' }
        $proc = Start-Process -FilePath $Python -ArgumentList $pyArgs `
                    -WorkingDirectory $Root -NoNewWindow -Wait -PassThru `
                    -RedirectStandardOutput $OutFile -RedirectStandardError $ErrFile
        $code = $proc.ExitCode

        $after = Get-Pending
        Write-Log "exit $code, pending $pending -> $after"

        if ($after -eq 0) { Write-Log "complete"; break }
        if ($code -eq 0) {
            # Clean exit with rows left means genuine state='error' rows, not a
            # crash. Retrying would spin on them forever.
            Write-Log "clean exit with $after rows left - inspect state='error'"
            break
        }
        if ($after -eq $pending) {
            Write-Log "crashed without progress - see captioning.err; not retrying blindly"
            if ($try -ge 3) { Write-Log "giving up after $try no-progress attempts"; break }
        }
        Write-Log "retrying in 60s"
        Start-Sleep -Seconds 60
    }
} catch {
    Write-Log "WRAPPER ERROR: $_"
    exit 1
}
