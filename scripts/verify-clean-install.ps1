<#
.SYNOPSIS
    Acceptance check 11: a clean copy of this tree installs and serves the built interface.

.DESCRIPTION
    Copies the candidate tree (without .git, virtual environments, node_modules, caches and local
    runtime folders) into a new folder under the system temp directory, runs that copy's
    scripts\install.ps1 with -RuntimeHome inside the same temp folder, starts that copy's
    `readme-monitor ui --no-browser` on a free port, completes the pairing over HTTP, fetches
    / and /api/state, and stops the server. Prints one PASS or FAIL line per check and exits
    non-zero if anything failed.

    Safety: no credential is used (the credential store is replaced by a null backend and
    credential environment variables are removed for every child process), nothing is activated
    or scheduled, and nothing is written outside the temp folder (pip's cache is redirected into
    it, and the account's activation pointer is checked before and after). The only network use
    is pip downloading the pinned dependencies from the package index, and loopback HTTP.

.PARAMETER KeepTemp
    Keep the temporary folder for inspection (its path is printed).
#>
[CmdletBinding()]
param([switch]$KeepTemp)
$ErrorActionPreference = 'Stop'
$source = Split-Path -Parent $PSScriptRoot
$work = Join-Path ([IO.Path]::GetTempPath()) ('rm-clean-' + [guid]::NewGuid().ToString('N').Substring(0, 12))
$failures = New-Object System.Collections.ArrayList
$server = $null

function Report([string]$Name, [bool]$Ok, [string]$Detail = '') {
    if ($Ok) { Write-Host ("PASS  {0}" -f $Name) }
    else { Write-Host ("FAIL  {0}  {1}" -f $Name, $Detail); [void]$failures.Add($Name) }
}

# Environment for every child process: no real credential store, no credential variables, caches inside the temp folder.
$saved = @{}
foreach ($name in 'PYTHON_KEYRING_BACKEND', 'READMEMONITOR_GITHUB_TOKEN', 'ANTHROPIC_API_KEY', 'READMEMONITOR_HOME', 'PIP_CACHE_DIR',
                  'PIP_DISABLE_PIP_VERSION_CHECK', 'PYTHONUNBUFFERED', 'PYTHONDONTWRITEBYTECODE', 'TEMP', 'TMP') {
    $saved[$name] = [Environment]::GetEnvironmentVariable($name)
}
$pointer = Join-Path $HOME '.readme-monitor.json'
$pointerBefore = if (Test-Path $pointer) { (Get-Item $pointer).LastWriteTimeUtc.Ticks } else { 0 }

try {
    New-Item -ItemType Directory -Path $work | Out-Null
    $env:PYTHON_KEYRING_BACKEND = 'keyring.backends.null.Keyring'
    foreach ($name in 'READMEMONITOR_GITHUB_TOKEN', 'ANTHROPIC_API_KEY', 'READMEMONITOR_HOME') { Remove-Item "Env:$name" -ErrorAction SilentlyContinue }
    New-Item -ItemType Directory -Path (Join-Path $work 'tmp') | Out-Null
    $env:TEMP = Join-Path $work 'tmp'   # pip and Python build in the temp folder; keep that inside the work folder too
    $env:TMP = $env:TEMP
    $env:PIP_CACHE_DIR = Join-Path $work 'pip-cache'
    $env:PIP_DISABLE_PIP_VERSION_CHECK = '1'
    $env:PYTHONUNBUFFERED = '1'
    $env:PYTHONDONTWRITEBYTECODE = '1'

    # 1. copy the candidate tree
    $copy = Join-Path $work 'tree'
    $skipDirs = '.git', '.venv', 'venv', 'node_modules', '__pycache__', '.pytest_cache', '.ruff_cache', '.mypy_cache', 'htmlcov',
                'readme_monitor.egg-info', 'bin', 'data', 'logs', 'evidence', 'config', 'dist', 'build'
    $null = & robocopy $source $copy /E /NFL /NDL /NJH /NJS /NP /XD $skipDirs /XF '*.pyc' '*.db' '*.log' 'config.toml' '.env'
    Report 'candidate tree copied' ($LASTEXITCODE -lt 8) "robocopy exit code $LASTEXITCODE"
    $built = Join-Path $copy 'readme_monitor\web\index.html'
    Report 'compiled interface is part of the copy' (Test-Path $built) 'readme_monitor\web\index.html is missing (build it: cd ui; npm ci; npm run build)'

    # 2. install with the copy's own installer
    $runtimeHome = Join-Path $work 'runtime'
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $copy 'scripts\install.ps1') -RuntimeHome $runtimeHome | Out-Host
    $py = Join-Path $copy '.venv\Scripts\python.exe'
    Report 'install.ps1 finished' ($LASTEXITCODE -eq 0) "exit code $LASTEXITCODE"
    Report 'virtual environment created inside the copy' (Test-Path $py)
    Report 'starter configuration created' (Test-Path (Join-Path $runtimeHome 'config.toml'))
    if (-not (Test-Path $py)) { throw 'the installation did not produce a Python environment; later checks are skipped' }
    $active = & $py -m readme_monitor status --json --home $runtimeHome | ConvertFrom-Json
    Report 'installing activated nothing' (-not $active.installation.active -and -not (Test-Path (Join-Path $runtimeHome 'activation.json')))

    # 3. start the interface of the copy on a free port (the server picks the next free one anyway)
    $probe = New-Object System.Net.Sockets.TcpListener([Net.IPAddress]::Loopback, 0)
    $probe.Start(); $port = $probe.LocalEndpoint.Port; $probe.Stop()
    $log = Join-Path $work 'ui.out'
    $server = Start-Process -FilePath $py -ArgumentList @('-m', 'readme_monitor', 'ui', '--no-browser', '--port', "$port", '--home', "`"$runtimeHome`"") `
        -WorkingDirectory $copy -RedirectStandardOutput $log -RedirectStandardError (Join-Path $work 'ui.err') -PassThru -WindowStyle Hidden
    $url = $null
    for ($i = 0; $i -lt 120 -and -not $url; $i++) {
        Start-Sleep -Milliseconds 250
        $seen = if (Test-Path $log) { Get-Content -Raw $log } else { $null }
        if ($seen) { $m = [regex]::Match($seen, 'http://127\.0\.0\.1:\d+/auth\?code=\S+'); if ($m.Success) { $url = $m.Value } }
    }
    Report 'interface server started and printed its sign-in address' ([bool]$url) ('no address after 30 seconds; see ' + $log)
    if (-not $url) { throw 'the interface did not start' }
    $base = ([Uri]$url).GetLeftPart([UriPartial]::Authority)

    # 4. pair and fetch / and /api/state over HTTP
    Add-Type -AssemblyName System.Net.Http
    function New-Client { $h = New-Object System.Net.Http.HttpClientHandler; $h.UseCookies = $true; $h.AllowAutoRedirect = $true; New-Object System.Net.Http.HttpClient($h) }
    $anonymous = New-Client
    $code = $anonymous.GetAsync("$base/api/state").GetAwaiter().GetResult().StatusCode.value__
    Report 'without pairing the API answers 401' ($code -eq 401) "status $code"
    $client = New-Client
    $paired = $client.GetAsync($url).GetAwaiter().GetResult()
    $page = $paired.Content.ReadAsStringAsync().GetAwaiter().GetResult()
    Report 'pairing succeeded and / serves the built page' ($paired.IsSuccessStatusCode -and $page.Contains('<div id="root">') -and $page.Contains('/assets/')) "status $($paired.StatusCode)"
    $asset = [regex]::Match($page, '/assets/[^"'']+\.js').Value
    $assetCode = if ($asset) { $client.GetAsync("$base$asset").GetAwaiter().GetResult().StatusCode.value__ } else { 0 }
    Report 'the page script is served from this server' ($assetCode -eq 200) "$asset -> $assetCode"
    $reply = $client.GetAsync("$base/api/state").GetAwaiter().GetResult()
    $state = $reply.Content.ReadAsStringAsync().GetAwaiter().GetResult() | ConvertFrom-Json
    Report '/api/state answers with the installed version' ($reply.IsSuccessStatusCode -and [bool]$state.version) "status $($reply.StatusCode)"
    Report 'state shows an inactive, non-simulated installation' ((-not $state.installation.active) -and (-not $state.simulation) -and $state.runtime.state -eq 'inactive')
    Report 'no credential was read or stored' ((-not $state.credentials.github.present) -and (-not $state.credentials.anthropic.present))
    $reuse = $anonymous.GetAsync($url).GetAwaiter().GetResult().StatusCode.value__
    Report 'the sign-in address works only once' ($reuse -eq 403) "status $reuse"
} catch {
    Report 'verification ran to the end' $false $_.Exception.Message
} finally {
    if ($server) { & taskkill.exe /PID $server.Id /T /F 2>$null | Out-Null }
    $pointerAfter = if (Test-Path $pointer) { (Get-Item $pointer).LastWriteTimeUtc.Ticks } else { 0 }
    Report 'nothing written to the account activation pointer' ($pointerAfter -eq $pointerBefore)
    foreach ($name in $saved.Keys) { [Environment]::SetEnvironmentVariable($name, $saved[$name]) }
    if ($KeepTemp) { Write-Host "Kept $work" }
    elseif ((Test-Path $work) -and $work.StartsWith([IO.Path]::GetTempPath()) -and (Split-Path -Leaf $work).StartsWith('rm-clean-')) {
        for ($i = 0; $i -lt 5; $i++) { try { Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction Stop; break } catch { Start-Sleep -Seconds 1 } }
        Report 'temporary folder removed' (-not (Test-Path $work)) $work
    }
}
if ($failures.Count -gt 0) { Write-Host ("{0} check(s) FAILED" -f $failures.Count); exit 1 }
Write-Host 'All checks passed'
exit 0
