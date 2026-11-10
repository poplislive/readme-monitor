<#
.SYNOPSIS
    Remove ReadmeMonitor's scheduled task and activation. By default nothing is deleted.

.DESCRIPTION
    Uses the product's own commands:
      1. `readme-monitor deactivate` removes the scheduled task and revokes the activation (the
         worker can no longer run). Your configuration, history and spending ledger stay.
      2. With -RemoveCredentials, removes the GitHub and Anthropic credentials stored for this
         Windows account (`readme-monitor remove-credential`).
    Then it lists the folders that still hold data. It deletes the runtime folder only when you
    pass -RemoveData and type DELETE when asked. The program itself (the repository folder with
    its .venv) is never deleted by this script; remove that folder by hand if you want it gone.

.EXAMPLE
    .\scripts\uninstall.ps1
.EXAMPLE
    .\scripts\uninstall.ps1 -RemoveCredentials
.EXAMPLE
    .\scripts\uninstall.ps1 -RemoveCredentials -RemoveData
#>
[CmdletBinding()]
param(
    # Default: READMEMONITOR_HOME, else the folder named by the account's activation pointer, else %USERPROFILE%\ReadmeMonitor.
    [string]$RuntimeHome = '',
    [switch]$RemoveCredentials,
    [switch]$RemoveData
)
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$py = Join-Path $repo '.venv\Scripts\python.exe'

function Resolve-Home([string]$Given) {
    if ($Given) { return $Given }
    if ($env:READMEMONITOR_HOME) { return $env:READMEMONITOR_HOME }
    $pointer = Join-Path $HOME '.readme-monitor.json'
    if (Test-Path $pointer) {
        try { $p = Get-Content -Raw $pointer | ConvertFrom-Json; if ($p.home) { return [string]$p.home } } catch { }
    }
    return (Join-Path $HOME 'ReadmeMonitor')
}

try {
    $data = Resolve-Home $RuntimeHome
    if (-not (Test-Path $py)) {
        Write-Host "The program environment ($py) is missing, so the product's own removal commands cannot run."
        Write-Host 'If a scheduled task named \ReadmeMonitor\Sync exists, remove it in Task Scheduler.'
    } elseif (Test-Path $data) {
        & $py -m readme_monitor deactivate --home $data
        if ($LASTEXITCODE -ne 0) { throw "deactivate failed (exit code $LASTEXITCODE)" }
        if ($RemoveCredentials) {
            foreach ($kind in 'github', 'anthropic') {
                & $py -m readme_monitor remove-credential $kind --home $data
                if ($LASTEXITCODE -ne 0) { throw "remove-credential $kind failed (exit code $LASTEXITCODE)" }
            }
        }
    } else {
        Write-Host "No runtime folder at $data ; nothing to deactivate there."
    }

    Write-Host ''
    Write-Host 'Data and program files that are still on this computer:'
    $listed = @(
        @($data, 'runtime folder: configuration, history, spending ledger, backups, logs'),
        @((Join-Path $repo '.venv'), 'program environment, safe to delete'),
        @($repo, 'program files (this repository folder)')
    )
    foreach ($item in $listed) {
        if (Test-Path $item[0]) { Write-Host ('  {0}  [{1}]' -f $item[0], $item[1]) }
    }
    if (-not $RemoveCredentials) { Write-Host '  stored credentials in Windows Credential Manager (run again with -RemoveCredentials to delete them)' }

    if ($RemoveData) {
        $full = [IO.Path]::GetFullPath($data).TrimEnd('\')
        $known = (Test-Path (Join-Path $full 'config.toml')) -or (Test-Path (Join-Path $full 'state.db'))
        $unsafe = ($full -eq [IO.Path]::GetPathRoot($full).TrimEnd('\')) -or ($full -eq ([string]$HOME).TrimEnd('\')) -or
                  $repo.StartsWith($full + '\', [StringComparison]::OrdinalIgnoreCase)
        if (-not (Test-Path $full)) {
            Write-Host "Nothing to remove: $full does not exist."
        } elseif ($unsafe -or -not $known) {
            Write-Host "Refusing to delete $full : it has no config.toml or state.db, or it contains other things." -ForegroundColor Yellow
        } else {
            Write-Host ''
            Write-Host "This permanently deletes $full, including the spending ledger and all history. It cannot be undone."
            $answer = Read-Host 'Type DELETE to confirm, anything else cancels'
            if ($answer -ceq 'DELETE') {
                Remove-Item -LiteralPath $full -Recurse -Force
                Write-Host "Deleted $full"
            } else {
                Write-Host 'Cancelled. Nothing was deleted.'
            }
        }
    } else {
        Write-Host ''
        Write-Host 'Nothing was deleted. Add -RemoveData to delete the runtime folder (you will be asked to confirm).'
    }
    exit 0
} catch {
    Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
