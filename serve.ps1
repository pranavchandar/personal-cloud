# Run the photo server, optionally keeping it running across reboots.
#
# "Access my photos from anywhere" needs the server up whenever the PC is on, so
# a logon task is not a nicety here -- without it you would have to start this by
# hand after every restart.
#
#   .\serve.ps1                  # local only (http://127.0.0.1:8765) for testing
#   .\serve.ps1 -Tailscale       # reachable from your signed-in devices
#   .\serve.ps1 -Register        # start automatically at every logon (Tailscale)
#   .\serve.ps1 -Unregister      # stop doing that
#
param(
    [switch]$Tailscale,
    [int]$Port = 8765,
    [switch]$Register,
    [switch]$Unregister
)

$ErrorActionPreference = 'Continue'
$Root     = $PSScriptRoot
$Python   = Join-Path $Root 'venv\Scripts\python.exe'
$Script   = Join-Path $Root 'server.py'
$TaskName = 'PhotoIndexServer'
$TsExe    = 'C:\Program Files\Tailscale\tailscale.exe'

if ($Unregister) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed '$TaskName'."
    } else { Write-Host "No such task '$TaskName'." }
    return
}

if ($Register) {
    # Registering is pointless if Tailscale is not signed in yet -- the server
    # would exit immediately at every logon with no IP to bind to. Check first.
    if (-not (Test-Path $TsExe)) { Write-Error "Tailscale not installed."; exit 1 }
    $ip = (& $TsExe ip -4 2>$null | Select-Object -First 1)
    if (-not $ip) {
        Write-Error "Tailscale is not signed in yet. Run 'tailscale up' first, then re-run -Register."
        exit 1
    }
    Write-Host "Tailscale IP: $ip"
    # Run through this script, not python directly. A hidden task launching python
    # with no stdout handle hung on its first print(): the socket bound but no
    # request was ever answered, and nothing was logged because the task action
    # redirects nowhere. Going via PowerShell lets us redirect explicitly.
    $action  = New-ScheduledTaskAction -Execute 'powershell.exe' `
                   -Argument ("-ExecutionPolicy Bypass -WindowStyle Hidden -File " +
                              "`"$PSCommandPath`" -Tailscale -Port $Port") `
                   -WorkingDirectory $Root
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $set     = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
                   -DontStopIfGoingOnBatteries -StartWhenAvailable `
                   -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 `
                   -RestartInterval (New-TimeSpan -Minutes 1)
    try {
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
            -Settings $set -Force -ErrorAction Stop | Out-Null
    } catch { Write-Error "Could not register: $_"; exit 1 }
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Write-Host "Registered '$TaskName' - server starts at every logon."
        Write-Host "Browse from any signed-in device: http://${ip}:$Port"
    } else { Write-Error "Register reported success but task is absent."; exit 1 }
    return
}

$pyArgs = @($Script, '--port', $Port)
if ($Tailscale) { $pyArgs += '--tailscale' }

$Out = Join-Path $Root 'server.out'
$Err = Join-Path $Root 'server.err'
# Start-Process with explicit redirects, not `& $Python`: this is what keeps the
# server alive under a hidden scheduled task, and it is the only way any startup
# error ends up somewhere readable.
$proc = Start-Process -FilePath $Python -ArgumentList $pyArgs -WorkingDirectory $Root `
            -NoNewWindow -Wait -PassThru -RedirectStandardOutput $Out `
            -RedirectStandardError $Err
exit $proc.ExitCode
