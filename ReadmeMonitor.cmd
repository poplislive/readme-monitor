@echo off
rem Starts the ReadmeMonitor setup interface. Double-click this file, or run it from a terminal.
rem It runs scripts\start.ps1 with an execution-policy bypass for this one process only, so no Windows setting changes.
rem Options are passed through, for example:  ReadmeMonitor.cmd -Demo   or   ReadmeMonitor.cmd -Port 9000
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start.ps1" %*
if errorlevel 1 (
  echo.
  echo ReadmeMonitor did not start. Read the message above, then press any key to close this window.
  pause >nul
)
