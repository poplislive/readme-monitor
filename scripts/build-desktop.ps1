<#
.SYNOPSIS
  Build the ReadmeMonitor desktop installer on Windows (x64): frozen engine + Tauri shell + NSIS.
.DESCRIPTION
  Needs Python 3.11+ (3.12 used for the verified build), Node 24, Rust stable (MSVC) and the
  Visual Studio C++ build tools. Uses no credential unless TAURI_SIGNING_PRIVATE_KEY is set in the
  environment: then the updater artifacts (.sig) are produced too; without it the build is a
  development build whose installer works but cannot be offered as an update.
  Output: desktop\src-tauri\target\release\bundle\nsis\ReadmeMonitor_<version>_x64-setup.exe
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\build-desktop.ps1
#>
param([string]$Python = "python")
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

& $Python packaging/check_versions.py
if ($LASTEXITCODE) { throw "version check failed" }

$venv = Join-Path $root "build\desktop-venv"
if (-not (Test-Path "$venv\Scripts\python.exe")) { & $Python -m venv $venv }
$py = "$venv\Scripts\python.exe"
& $py -m pip install --quiet --upgrade pip
& $py -m pip install --quiet -r packaging/requirements-build.txt
& $py -m pip install --quiet --no-deps --force-reinstall .
& $py packaging/python_notices.py build/notices/PYTHON_THIRD_PARTY.md
& $py -m PyInstaller packaging/engine.spec --distpath build/engine-dist --workpath build/pyi --noconfirm --log-level WARN
if ($LASTEXITCODE) { throw "PyInstaller failed" }
& "build\engine-dist\engine\rm-engine.exe" --version
if ($LASTEXITCODE) { throw "the frozen engine does not start" }

& $py packaging/rust_notices.py build/notices/RUST_THIRD_PARTY.md
Push-Location desktop
try {
  npm ci --no-fund --no-audit
  if ($env:TAURI_SIGNING_PRIVATE_KEY) {
    npx tauri build --bundles nsis
  } else {
    Write-Warning "TAURI_SIGNING_PRIVATE_KEY is not set: development build without updater signatures."
    # A file, not inline JSON: Windows PowerShell 5.1 and PowerShell 7 quote native arguments differently.
    $dev = Join-Path $root "build\desktop-development.conf.json"
    Set-Content -Path $dev -Value '{"bundle":{"createUpdaterArtifacts":false}}' -Encoding ascii
    npx tauri build --bundles nsis --config $dev
  }
  if ($LASTEXITCODE) { throw "tauri build failed" }
} finally { Pop-Location }
Get-ChildItem desktop\src-tauri\target\release\bundle\nsis | ForEach-Object {
  "{0}  {1}" -f (Get-FileHash $_.FullName -Algorithm SHA256).Hash, $_.FullName
}
