@echo off
rem Double-click entry for start.ps1 (bypasses the script execution policy).
rem All options are forwarded, e.g.:  start.cmd -Full
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
set code=%ERRORLEVEL%
if not "%code%"=="0" pause
