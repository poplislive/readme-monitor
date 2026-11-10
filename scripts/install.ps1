<#
.SYNOPSIS
    Install or upgrade ReadmeMonitor from this checkout into an isolated .venv.

.DESCRIPTION
    Creates .venv next to this script's repository (if missing), installs the pinned
    dependencies from requirements.txt, installs ReadmeMonitor itself, creates the runtime
    folder and a starter config.toml if none exists, and runs `readme-monitor doctor`.
    Re-running it is the upgrade path after `git pull`.

    Installing or upgrading never activates the worker, never stores credentials, never spends
    anything and never registers the scheduled task. -InstallTask is an explicit opt-in that
    works only for an installation you have already activated.

    If Windows refuses to run this script ("running scripts is disabled" or "not digitally
    signed"), start it for this one run without changing any setting:
        powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1

.EXAMPLE
    .\scripts\install.ps1
.EXAMPLE
    .\scripts\install.ps1 -RuntimeHome D:\Data\ReadmeMonitor -PythonExe C:\Python312\python.exe
.EXAMPLE
    .\scripts\install.ps1 -InstallTask
    (Only after the worker was enabled on this computer: in the interface, or with `readme-monitor activate`.)
#>
[CmdletBinding()]
param(
    # Not under AppData: apps packaged with MSIX redirect AppData writes to a private copy.
    [string]$RuntimeHome = (Join-Path $HOME 'ReadmeMonitor'),
    [string]$PythonExe = '',
    [switch]$Dev,
    [switch]$InstallTask
)
$ErrorActionPreference = 'Stop'

function Invoke-Checked([string]$What, [scriptblock]$Command, [string]$Hint = '') {
    & $Command
    if ($LASTEXITCODE -ne 0) {
        $more = ''
        if ($Hint) { $more = " $Hint" }
        throw "$What failed (exit code $LASTEXITCODE).$more"
    }
}

# Returns the first interpreter that is Python 3.11 or newer, or throws a message a person can act on.
function Find-Python([string]$Wanted) {
    $candidates = @()
    if ($Wanted) { $candidates += , @($Wanted) }
    else {
        if (Get-Command py -ErrorAction SilentlyContinue) { $candidates += , @('py', '-3') }
        if (Get-Command python -ErrorAction SilentlyContinue) { $candidates += , @('python') }
    }
    if (-not $candidates) {
        throw 'Python was not found. Install Python 3.11 or newer from https://www.python.org/downloads/ (tick "Add python.exe to PATH"), then run this script again.'
    }
    foreach ($c in $candidates) {
        $exe = $c[0]
        $rest = @()
        if ($c.Count -gt 1) { $rest = $c[1..($c.Count - 1)] }
        try {
            $found = & $exe @rest -c "import sys; print('%d.%d' % sys.version_info[:2]); sys.exit(0 if sys.version_info >= (3, 11) else 1)" 2>$null
            if ($LASTEXITCODE -eq 0) { return , $c }
            if ($found) { $seen = "$found" }
        } catch { }
    }
    if ($Wanted) { throw "'$Wanted' is not Python 3.11 or newer (or cannot be run). Install Python 3.11+ from https://www.python.org/downloads/ or pass the right path with -PythonExe." }
    if ($seen) { throw "Python $seen was found, but ReadmeMonitor needs 3.11 or newer. Install a newer Python from https://www.python.org/downloads/ and run this script again." }
    throw 'Python 3.11 or newer was not found (the Microsoft Store shortcut named python.exe does not count). Install it from https://www.python.org/downloads/ and run this script again.'
}

try {
    $repo = Split-Path -Parent $PSScriptRoot
    $venv = Join-Path $repo '.venv'
    $py = Join-Path $venv 'Scripts\python.exe'

    if (-not (Test-Path $py)) {
        $found = Find-Python $PythonExe
        $exe = $found[0]
        $rest = @()
        if ($found.Count -gt 1) { $rest = $found[1..($found.Count - 1)] }
        Write-Host "Creating the private Python environment in $venv ..."
        Invoke-Checked 'Creating .venv' { & $exe @rest -m venv $venv } 'Check that the folder is writable and that the Python installation includes the venv module (reinstall Python with default options).'
    }
    Invoke-Checked 'Python version check' { & $py -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" } 'The existing .venv uses an older Python; delete the .venv folder and run this script again.'

    $net = 'Check your internet connection (and proxy settings), then run this script again; it is safe to repeat.'
    Invoke-Checked 'Upgrading pip' { & $py -m pip install --quiet --upgrade pip } $net
    Invoke-Checked 'Installing pinned dependencies' { & $py -m pip install --quiet -r (Join-Path $repo 'requirements.txt') } $net
    Invoke-Checked 'Installing ReadmeMonitor' { & $py -m pip install --quiet --no-deps -e $repo }
    if ($Dev) {
        Invoke-Checked 'Installing test tools' { & $py -m pip install --quiet -r (Join-Path $repo 'requirements-dev.txt') } $net
    }

    New-Item -ItemType Directory -Force -Path $RuntimeHome | Out-Null
    if (-not (Test-Path (Join-Path $RuntimeHome 'config.toml'))) {
        # Only when missing: `init` with an existing config adopts READMEs, which an upgrade must never do.
        Invoke-Checked 'Creating starter configuration' { & $py -m readme_monitor init --home $RuntimeHome }
    }

    Write-Host ''
    Write-Host "ReadmeMonitor installed. Runtime folder: $RuntimeHome"
    Write-Host 'Nothing is enabled yet: no worker, no schedule, no spending, no credentials.'
    & $py -m readme_monitor doctor --home $RuntimeHome
    $doctorExit = $LASTEXITCODE

    if ($InstallTask) {
        $state = & $py -m readme_monitor status --json --home $RuntimeHome | ConvertFrom-Json
        if (-not $state.installation.active) {
            Write-Host ''
            Write-Host 'The scheduled task was NOT registered: this installation is not enabled on this computer yet.'
            Write-Host 'Start the interface, finish the setup steps and use "Enable the worker on this computer",'
            Write-Host "or run:  & '$py' -m readme_monitor activate --home '$RuntimeHome'"
            Write-Host 'Then run this script again with -InstallTask, or save a schedule in the interface.'
            exit 2
        }
        Invoke-Checked 'Registering scheduled task' { & $py -m readme_monitor install-task --home $RuntimeHome }
    }

    Write-Host ''
    Write-Host 'Start the setup interface (it opens in your browser):'
    Write-Host '    double-click ReadmeMonitor.cmd in the repository folder, or run  .\scripts\start.ps1'
    if ($doctorExit -ne 0) {
        Write-Host ''
        Write-Host 'Doctor reported FAIL items above. They are expected before setup (no credentials yet); the interface walks you through them.'
    }
    exit 0  # installation itself succeeded; doctor findings are reported above
} catch {
    Write-Host ''
    Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
