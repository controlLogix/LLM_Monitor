@echo off
rem Removes the logon shortcut and records the choice in config.json, so the
rem overlay does not recreate it at the next launch.
del "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\GPU Overlay.lnk" 2>nul
powershell -NoProfile -Command ^
  "$p=Join-Path '%~dp0' 'config.json'; if (Test-Path $p) {" ^
  "  $c=Get-Content $p -Raw | ConvertFrom-Json;" ^
  "  $c | Add-Member -NotePropertyName start_on_login -NotePropertyValue $false -Force;" ^
  "  $c | ConvertTo-Json -Depth 10 | Set-Content $p -Encoding UTF8 }"
echo Startup shortcut removed.
pause
