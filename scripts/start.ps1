<#
.SYNOPSIS
    Start the ReadmeMonitor setup interface in your browser.

.DESCRIPTION
    Finds the private environment created by install.ps1 and runs `readme-monitor ui`, which opens
    your browser on this computer only (127.0.0.1). No Node.js is needed: the compiled interface
    ships in the package. Leave this window open while you use the interface; press Ctrl+C or
    close the window to stop it. Starting the interface does not enable the worker, create a
    schedule or spend anything.

.EXAMPLE
    .\scripts\start.ps1
.EXAMPLE
    .\scripts\start.ps1 -Demo
    (A simulation with invented repositories in a temporary folder; nothing real is contacted.)
.EXAMPLE
    .\scripts\start.ps1 -RuntimeHome D:\Data\ReadmeMonitor -Port 9000
#>
[CmdletBinding()]
param(
    # Default: READMEMONITOR_HOME, else the activated folder, else %USERPROFILE%\ReadmeMonitor.
    [string]$RuntimeHome = '',
    [int]$Port = 8765,
    [switch]$Demo,
    [switch]$NoBrowser
)
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$py = Join-Path $repo '.venv\Scripts\python.exe'

if (-not (Test-Path $py)) {
    Write-Host 'ReadmeMonitor is not installed on this computer yet.' -ForegroundColor Yellow
    Write-Host 'Run the installer once, then start again:'
    Write-Host '    powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1'
    exit 1
}

$arguments = @('-m', 'readme_monitor', 'ui', '--port', "$Port")
if ($RuntimeHome) { $arguments += @('--home', $RuntimeHome) }
if ($Demo) { $arguments += '--demo' }
if ($NoBrowser) { $arguments += '--no-browser' }

Write-Host 'Starting the ReadmeMonitor setup interface. Leave this window open; press Ctrl+C to stop.'
& $py @arguments
$code = $LASTEXITCODE
if ($code -ne 0) {
    Write-Host ''
    Write-Host "The interface stopped with exit code $code." -ForegroundColor Yellow
    Write-Host "If another program uses port $Port, choose a different one:  .\scripts\start.ps1 -Port 9000"
    Write-Host 'For anything else run  readme-monitor doctor  and see docs/troubleshooting.md.'
    exit $code
}
