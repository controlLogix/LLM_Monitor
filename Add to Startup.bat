@echo off
setlocal
rem Creates a shortcut in the current user's Startup folder so the overlay
rem comes up at logon, and records the choice in config.json so the overlay's
rem own "Start with Windows" menu item agrees. Run this again after moving the
rem folder. Equivalent to ticking that menu item.
set "SCRIPT=%~dp0gpu_overlay.pyw"
set "LINK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\GPU Overlay.lnk"

rem Point the shortcut at pythonw.exe and the script, the same target the
rem overlay's own set_startup() writes - not at "Start GPU Overlay.bat", which
rem flashes a console at logon and breaks if the .bat is renamed.
rem
rem Resolve the interpreter to an absolute path rather than trusting a bare
rem "pythonw.exe": on a Microsoft Store install that name is an App Execution
rem Alias stub, which is per-user and can be switched off in Settings. A logon
rem shortcut should not depend on it. sys.executable reports the versionless
rem WindowsApps alias directory, so the path survives a Python update.
set "PYW="
for /f "delims=" %%i in ('pythonw.exe -c "import sys; print(sys.executable)" 2^>nul') do set "PYW=%%i"
if not defined PYW for /f "delims=" %%i in ('python.exe -c "import sys, os; print(os.path.join(os.path.dirname(sys.executable), 'pythonw.exe'))" 2^>nul') do set "PYW=%%i"
if not defined PYW (
  echo Could not resolve pythonw.exe. Is Python installed and on PATH?
  pause
  exit /b 1
)

rem [char]34 supplies the quotes around the script path, so no literal double
rem quote has to survive the trip through cmd into powershell -Command.
powershell -NoProfile -NonInteractive -Command ^
  "$q = [char]34;" ^
  "$s = (New-Object -ComObject WScript.Shell).CreateShortcut('%LINK%');" ^
  "$s.TargetPath = '%PYW%';" ^
  "$s.Arguments = $q + '%SCRIPT%' + $q;" ^
  "$s.WorkingDirectory = '%~dp0';" ^
  "$s.Description = 'GPU Overlay'; $s.Save()"
if not exist "%LINK%" (
  echo Failed to create the startup shortcut.
  pause
  exit /b 1
)
call :setflag true
echo Startup shortcut created: %LINK%
echo   target: %PYW%
echo   script: %SCRIPT%
pause
exit /b

:setflag
powershell -NoProfile -NonInteractive -Command ^
  "$p = Join-Path '%~dp0' 'config.json'; if (Test-Path $p) {" ^
  "  $c = Get-Content $p -Raw | ConvertFrom-Json;" ^
  "  $c | Add-Member -NotePropertyName start_on_login -NotePropertyValue ([bool]::Parse('%~1')) -Force;" ^
  "  $c | ConvertTo-Json -Depth 10 | Set-Content $p -Encoding UTF8 }"
exit /b
