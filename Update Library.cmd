@echo off
rem Double-click after copying new photos/videos into the backup folder (or deleting some).
title Photo library update
echo Scanning your library folder (library_root in config.json) for new, changed and deleted files,
echo then tagging new photos. Keep this window open until it says Done.
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0sync.ps1"
echo.
echo Done. Reload the web page to see the changes.
pause
