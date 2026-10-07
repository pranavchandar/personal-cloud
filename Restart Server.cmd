@echo off
rem Double-click if the web page stops loading.
title Restart photo server
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Stop-ScheduledTask PhotoIndexServer -ErrorAction SilentlyContinue;" ^
  "Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force };" ^
  "Start-Sleep 2; Start-ScheduledTask PhotoIndexServer;" ^
  "for($i=0;$i -lt 20;$i++){ Start-Sleep 2; if(Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue){ break } };" ^
  "$ip = & 'C:\Program Files\Tailscale\tailscale.exe' ip -4 | Select-Object -First 1;" ^
  "if(Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue){ Write-Host \"Server running: http://${ip}:8765\" } else { Write-Host 'Server did not start - see F:\photo-index\server.err' }"
pause
