@echo off
cd /d "%~dp0"
powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Startup')+'\Driftline Server.lnk');$s.TargetPath='%~dp0start-server.bat';$s.WorkingDirectory='%~dp0';$s.WindowStyle=7;$s.Save()"
echo Driftline's server will now start by itself every time you sign in to Windows.
pause
