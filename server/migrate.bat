@echo off
title Driftline - copy accounts from the old cloud
cd /d "%~dp0"
echo.
echo  Copies every Driftline account, bag, coin balance, friend list, battle,
echo  market listing and gift code from the old Firebase cloud to this server.
echo  Make sure the Driftline server window is CLOSED first.
echo.
pause
python migrate_from_firestore.py
pause
