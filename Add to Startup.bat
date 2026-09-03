@echo off
rem Creates a shortcut in the current user's Startup folder so the overlay
rem comes up at logon. Run "Add to Startup.bat" again after moving the folder.
set "TARGET=%~dp0Start GPU Overlay.bat"
set "LINK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\GPU Overlay.lnk"
powershell -NoProfile -Command ^
  "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%LINK%');" ^
  "$s.TargetPath='%TARGET%'; $s.WorkingDirectory='%~dp0';" ^
  "$s.WindowStyle=7; $s.Description='GPU Overlay'; $s.Save()"
echo Startup shortcut created: %LINK%
pause
