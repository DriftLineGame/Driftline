@echo off
powershell -NoProfile -Command "Remove-Item -ErrorAction SilentlyContinue ([Environment]::GetFolderPath('Startup')+'\Driftline Server.lnk')"
echo Driftline autostart removed.
pause
