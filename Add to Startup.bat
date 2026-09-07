@echo off
rem Creates a shortcut in the current user's Startup folder so the overlay
rem comes up at logon, and records the choice in config.json so the overlay's
rem own "Start with Windows" menu item agrees. Run this again after moving the
rem folder. Equivalent to ticking that menu item.
set "TARGET=%~dp0Start GPU Overlay.bat"
set "LINK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\GPU Overlay.lnk"
powershell -NoProfile -Command ^
  "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%LINK%');" ^
  "$s.TargetPath='%TARGET%'; $s.WorkingDirectory='%~dp0';" ^
  "$s.WindowStyle=7; $s.Description='GPU Overlay'; $s.Save()"
call :setflag true
echo Startup shortcut created: %LINK%
pause
exit /b

:setflag
powershell -NoProfile -Command ^
  "$p=Join-Path '%~dp0' 'config.json'; if (Test-Path $p) {" ^
  "  $c=Get-Content $p -Raw | ConvertFrom-Json;" ^
  "  $c | Add-Member -NotePropertyName start_on_login -NotePropertyValue ([bool]::Parse('%~1')) -Force;" ^
  "  $c | ConvertTo-Json -Depth 10 | Set-Content $p -Encoding UTF8 }"
exit /b
