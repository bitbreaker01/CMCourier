<#
.SYNOPSIS
  Build a self-contained offline installation bundle for CMCourier targeting
  an air-gapped Windows Server with Python already installed.

.DESCRIPTION
  Run this on a Windows machine WITH internet access. Output is a single .zip
  you transfer to the air-gapped server via SFTP / share. The server installs
  it offline using the bundled install.bat.

.PARAMETER PythonVersion
  Target Python version on the air-gapped server (e.g. "3.11", "3.12").
  Must match the server EXACTLY. Defaults to the version running this script.

.PARAMETER OutputDir
  Where to drop the final .zip. Defaults to .\dist-offline.

.PARAMETER SkipBuild
  Skip rebuilding the project wheel if one already exists in dist\.

.EXAMPLE
  .\installer\build-offline-bundle.ps1
  .\installer\build-offline-bundle.ps1 -PythonVersion 3.11
  .\installer\build-offline-bundle.ps1 -PythonVersion 3.12 -OutputDir C:\releases
#>

[CmdletBinding()]
param(
    [string]$PythonVersion = "",
    [string]$OutputDir = "dist-offline",
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

function Write-Step  { param($msg) Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Ok    { param($msg) Write-Host "    $msg" -ForegroundColor Green }
function Write-Warn  { param($msg) Write-Host "    $msg" -ForegroundColor Yellow }
function Fail        { param($msg) Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }

# ---------------------------------------------------------------------------
# 0. Locate repo root and verify we are in a CMCourier checkout
# ---------------------------------------------------------------------------
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

if (-not (Test-Path "pyproject.toml")) {
    Fail "pyproject.toml not found. Run from a CMCourier checkout."
}

# ---------------------------------------------------------------------------
# 1. Prerequisites
# ---------------------------------------------------------------------------
Write-Step "Checking prerequisites"

$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCmd) { Fail "python not found in PATH." }

$detectedVersion = (& python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')").Trim()
if (-not $PythonVersion) { $PythonVersion = $detectedVersion }
Write-Ok "Python (build host): $detectedVersion"
Write-Ok "Python (target):     $PythonVersion"

if ($PythonVersion -ne $detectedVersion) {
    Write-Warn "Target Python ($PythonVersion) differs from build host ($detectedVersion)."
    Write-Warn "pip download will request wheels for the target ABI; some packages may not have them."
}

$uvCmd = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uvCmd) { Fail "uv not found in PATH. Install from https://docs.astral.sh/uv/" }
Write-Ok "uv: $((& uv --version).Trim())"

# ---------------------------------------------------------------------------
# 2. Read project version from pyproject.toml
# ---------------------------------------------------------------------------
Write-Step "Reading project version"
$projectVersion = (Select-String -Path "pyproject.toml" -Pattern '^version\s*=\s*"([^"]+)"' |
    Select-Object -First 1).Matches.Groups[1].Value
if (-not $projectVersion) { Fail "Could not parse version from pyproject.toml" }
Write-Ok "cmcourier version: $projectVersion"

# ---------------------------------------------------------------------------
# 3. Clean and prepare bundle layout
# ---------------------------------------------------------------------------
$BundleName = "cmcourier-offline-$projectVersion-py$PythonVersion-win_amd64"
$BundleDir  = Join-Path $OutputDir $BundleName
$WheelsDir  = Join-Path $BundleDir "wheels"
$ConfigDir  = Join-Path $BundleDir "config"

Write-Step "Preparing bundle directory: $BundleDir"
if (Test-Path $BundleDir) { Remove-Item -Recurse -Force $BundleDir }
New-Item -ItemType Directory -Path $WheelsDir | Out-Null
New-Item -ItemType Directory -Path $ConfigDir | Out-Null

# ---------------------------------------------------------------------------
# 4. Export pinned requirements from uv.lock
# ---------------------------------------------------------------------------
Write-Step "Exporting requirements from uv.lock"
$reqFile = Join-Path $BundleDir "requirements.txt"
& uv export --no-hashes --no-dev --format requirements-txt -o $reqFile
if ($LASTEXITCODE -ne 0) { Fail "uv export failed." }
Write-Ok "Wrote $reqFile"

# ---------------------------------------------------------------------------
# 5. Build the project wheel (unless skipped)
# ---------------------------------------------------------------------------
$projectWheel = "dist\cmcourier-$projectVersion-py3-none-any.whl"
if ($SkipBuild -and (Test-Path $projectWheel)) {
    Write-Step "Skipping wheel build (using existing $projectWheel)"
} else {
    Write-Step "Building project wheel"
    if (Test-Path "dist") { Remove-Item -Recurse -Force "dist" }
    & uv build --wheel
    if ($LASTEXITCODE -ne 0) { Fail "uv build failed." }
}
if (-not (Test-Path $projectWheel)) { Fail "Expected wheel not found: $projectWheel" }
Copy-Item $projectWheel $WheelsDir
Write-Ok "Bundled $projectWheel"

# ---------------------------------------------------------------------------
# 6. Download dependency wheels for Windows x86_64 / target Python
# ---------------------------------------------------------------------------
Write-Step "Downloading dependency wheels (target: win_amd64 / cp$($PythonVersion.Replace('.','')))"
$abi = "cp$($PythonVersion.Replace('.',''))"

& python -m pip download `
    --dest $WheelsDir `
    --platform win_amd64 `
    --python-version $PythonVersion `
    --implementation cp `
    --abi $abi `
    --only-binary=:all: `
    -r $reqFile

if ($LASTEXITCODE -ne 0) {
    Fail @"
pip download failed. Common causes:
  - A dependency only ships an sdist (source) — needs manual wheel build.
  - Wrong --python-version vs server.
  - Network/proxy issue on this build machine.
"@
}

# Also stage a recent pip wheel so the offline installer can upgrade pip if it wants
& python -m pip download `
    --dest $WheelsDir `
    --platform win_amd64 `
    --python-version $PythonVersion `
    --implementation cp `
    --abi $abi `
    --only-binary=:all: `
    pip setuptools wheel | Out-Null

$wheelCount = (Get-ChildItem $WheelsDir -Filter *.whl).Count
Write-Ok "Wheels staged: $wheelCount"

# ---------------------------------------------------------------------------
# 7. Copy runtime config and reference data
# ---------------------------------------------------------------------------
Write-Step "Bundling config and reference data"

$sampleConfigs = @(
    "sample\config-staging.yaml",
    "sample\clients.csv",
    "sample\MapeoRVI_CM.csv",
    "sample\MetadatosCM.csv"
)
foreach ($f in $sampleConfigs) {
    if (Test-Path $f) {
        Copy-Item $f $ConfigDir
        Write-Ok "Bundled $f"
    } else {
        Write-Warn "Skipped (missing): $f"
    }
}

# Always bundle the annotated config reference so the operator has the full
# configurable surface even when sample/ is absent (it is gitignored).
if (Test-Path "docs\reference\config-reference.yaml") {
    Copy-Item "docs\reference\config-reference.yaml" $ConfigDir
    Write-Ok "Bundled docs\reference\config-reference.yaml"
}

# Rename main config to a production-friendly name
$stagingConfig = Join-Path $ConfigDir "config-staging.yaml"
$prodConfig    = Join-Path $ConfigDir "config-prod.yaml.template"
if (Test-Path $stagingConfig) { Move-Item $stagingConfig $prodConfig }

if (Test-Path "reference-data") {
    Copy-Item -Recurse "reference-data" (Join-Path $BundleDir "reference-data")
    Write-Ok "Bundled reference-data\"
}

if (Test-Path "README.md") {
    Copy-Item "README.md" $BundleDir
}

# ---------------------------------------------------------------------------
# 8. Generate install.bat
# ---------------------------------------------------------------------------
Write-Step "Generating install.bat"
$installBat = @"
@echo off
setlocal

rem CMCourier offline installer
rem Generated by build-offline-bundle.ps1
rem Target: Python $PythonVersion on Windows x86_64

set "INSTALL_DIR=%~dp0"
cd /d "%INSTALL_DIR%"

echo === CMCourier offline installer ===
echo Install dir: %INSTALL_DIR%
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo ERROR: python not found in PATH.
    echo        Install Python $PythonVersion x86_64 first.
    exit /b 1
)

echo [1/4] Creating virtualenv (.venv)...
if exist .venv (
    echo       .venv already exists, reusing.
) else (
    python -m venv .venv
    if errorlevel 1 (
        echo ERROR: Failed to create .venv.
        exit /b 1
    )
)

echo [2/4] Upgrading pip from local wheelhouse...
".venv\Scripts\python.exe" -m pip install --no-index --find-links wheels --upgrade pip setuptools wheel
if errorlevel 1 (
    echo ERROR: pip upgrade failed.
    exit /b 1
)

echo [3/4] Installing cmcourier and dependencies (offline)...
".venv\Scripts\python.exe" -m pip install --no-index --find-links wheels cmcourier
if errorlevel 1 (
    echo ERROR: cmcourier install failed.
    exit /b 1
)

echo [4/4] Verifying...
".venv\Scripts\cmcourier.exe" --version
if errorlevel 1 (
    echo ERROR: cmcourier failed to run.
    exit /b 1
)

echo.
echo === Installed successfully ===
echo.
echo Next steps:
echo   1. Install IBM i Access ODBC driver if not present.
echo   2. Configure a System DSN for the AS400 / RVI source.
echo   3. Edit config\config-prod.yaml (copy from .template).
echo   4. Run:  .venv\Scripts\cmcourier.exe --config config\config-prod.yaml [command]
echo.
endlocal
"@
$installBat | Out-File -FilePath (Join-Path $BundleDir "install.bat") -Encoding ASCII

# ---------------------------------------------------------------------------
# 9. Generate INSTALL.txt with operator instructions
# ---------------------------------------------------------------------------
$installTxt = @"
CMCourier $projectVersion — Offline Install
============================================

Target: Windows Server x86_64 with Python $PythonVersion already installed.

Prerequisites on the server
---------------------------
1. Python $PythonVersion x86_64 in PATH.
2. Microsoft Visual C++ Redistributable 2015-2022 x64 (usually pre-installed).
3. IBM i Access ODBC driver (or equivalent for the RVI source database).
4. A System DSN configured under "ODBC Data Sources (64-bit)".

Installation
------------
1. Copy this bundle to the server (already done if you are reading this there).
2. Open CMD or PowerShell in this directory.
3. Run:
       install.bat
   The installer creates a local .venv and installs cmcourier offline.

Configuration
-------------
1. Copy config\config-prod.yaml.template to config\config-prod.yaml.
2. Edit connection strings, paths, and credentials.
3. Adjust clients.csv / MapeoRVI_CM.csv / MetadatosCM.csv if needed.

Running
-------
   .venv\Scripts\cmcourier.exe --config config\config-prod.yaml --help

Updating
--------
Re-run install.bat with a new bundle. The existing .venv is reused; pip
upgrades changed packages from the new wheels\ folder.
"@
$installTxt | Out-File -FilePath (Join-Path $BundleDir "INSTALL.txt") -Encoding UTF8

# ---------------------------------------------------------------------------
# 10. Archive the bundle as .zip
# ---------------------------------------------------------------------------
Write-Step "Compressing bundle"
$zipPath = Join-Path $OutputDir "$BundleName.zip"
if (Test-Path $zipPath) { Remove-Item -Force $zipPath }
Compress-Archive -Path $BundleDir -DestinationPath $zipPath -CompressionLevel Optimal

$zipSizeMb = [math]::Round((Get-Item $zipPath).Length / 1MB, 1)

Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host " Bundle ready" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Host " Path:    $zipPath"
Write-Host " Size:    $zipSizeMb MB"
Write-Host " Wheels:  $wheelCount"
Write-Host " Target:  Windows x86_64, Python $PythonVersion"
Write-Host ""
Write-Host " Transfer this .zip to the air-gapped server, extract,"
Write-Host " and run install.bat."
Write-Host "============================================================" -ForegroundColor Green
