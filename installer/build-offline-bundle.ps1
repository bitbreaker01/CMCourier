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

# python with fallback to python3 (pwsh on Linux/macOS usually only has python3).
$PythonExe = if (Get-Command python -ErrorAction SilentlyContinue) {
    "python"
} elseif (Get-Command python3 -ErrorAction SilentlyContinue) {
    "python3"
} else {
    Fail "Neither python nor python3 found in PATH."
}

$detectedVersion = (& $PythonExe -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')").Trim()
if (-not $PythonVersion) { $PythonVersion = $detectedVersion }
Write-Ok "Python (build host): $detectedVersion ($PythonExe)"
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

function Join-Paths {
    param([Parameter(Mandatory)][string[]]$Segments)
    $result = $Segments[0]
    foreach ($segment in $Segments[1..($Segments.Count - 1)]) {
        $result = Join-Path $result $segment
    }
    return $result
}

Write-Step "Preparing bundle directory: $BundleDir"
if (Test-Path $BundleDir) { Remove-Item -Recurse -Force $BundleDir }
New-Item -ItemType Directory -Path $WheelsDir | Out-Null
New-Item -ItemType Directory -Path $ConfigDir | Out-Null

# ---------------------------------------------------------------------------
# 4. Export pinned requirements from uv.lock
# ---------------------------------------------------------------------------
Write-Step "Exporting requirements from uv.lock"
$reqFile = Join-Path $BundleDir "requirements.txt"
& uv export --no-emit-project --no-hashes --no-dev --format requirements-txt -o $reqFile
if ($LASTEXITCODE -ne 0) { Fail "uv export failed." }
Write-Ok "Wrote $reqFile"

# requirements-download.txt: same lines, markers stripped (everything from
# " ;" onward). pip download ignores platform markers and would otherwise
# download for the BUILD host, not the target — see REQ-001 in spec 142.
$reqDownloadFile = Join-Path $BundleDir "requirements-download.txt"
$downloadLines = Get-Content $reqFile |
    Where-Object { $_.Trim() -ne "" -and -not $_.TrimStart().StartsWith("#") } |
    ForEach-Object { ($_ -split ' ;', 2)[0].Trim() }
Set-Content -Path $reqDownloadFile -Value $downloadLines -Encoding UTF8
Write-Ok "Wrote $reqDownloadFile"

# ---------------------------------------------------------------------------
# 5. Build the project wheel (unless skipped)
# ---------------------------------------------------------------------------
$projectWheel = Join-Path "dist" "cmcourier-$projectVersion-py3-none-any.whl"
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

& $PythonExe -m pip download `
    --dest $WheelsDir `
    --platform win_amd64 `
    --python-version $PythonVersion `
    --implementation cp `
    --abi $abi `
    --only-binary=:all: `
    -r $reqDownloadFile

if ($LASTEXITCODE -ne 0) {
    Fail @"
pip download failed. Common causes:
  - A dependency only ships an sdist (source) — needs manual wheel build.
  - Wrong --python-version vs server.
  - Network/proxy issue on this build machine.
"@
}

# Also stage a recent pip wheel so the offline installer can upgrade pip if it wants
& $PythonExe -m pip download `
    --dest $WheelsDir `
    --platform win_amd64 `
    --python-version $PythonVersion `
    --implementation cp `
    --abi $abi `
    --only-binary=:all: `
    pip setuptools wheel | Out-Null

$wheelCount = (Get-ChildItem $WheelsDir -Filter *.whl).Count
Write-Ok "Wheels staged: $wheelCount"

$projectWheelCount = (Get-ChildItem $WheelsDir -Filter "cmcourier-*.whl").Count
if ($projectWheelCount -ne 1) {
    Fail "Expected exactly one cmcourier-*.whl in $WheelsDir, found $projectWheelCount."
}

# ---------------------------------------------------------------------------
# 7. Copy runtime config and reference data
# ---------------------------------------------------------------------------
Write-Step "Bundling config and reference data"

$sampleConfigs = @(
    (Join-Path "sample" "config-staging.yaml"),
    (Join-Path "sample" "clients.csv"),
    (Join-Path "sample" "MapeoRVI_CM.csv"),
    (Join-Path "sample" "MetadatosCM.csv")
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
$configReferencePath = Join-Paths @("docs", "reference", "config-reference.yaml")
if (Test-Path $configReferencePath) {
    Copy-Item $configReferencePath $ConfigDir
    Write-Ok "Bundled $configReferencePath"
}

# Rename main config to a production-friendly name
$stagingConfig = Join-Path $ConfigDir "config-staging.yaml"
$prodConfig    = Join-Path $ConfigDir "config-prod.yaml.template"
if (Test-Path $stagingConfig) { Move-Item $stagingConfig $prodConfig }

if (Test-Path "reference-data") {
    Copy-Item -Recurse "reference-data" (Join-Path $BundleDir "reference-data")
    Write-Ok "Bundled reference-data/"
}

if (Test-Path "README.md") {
    Copy-Item "README.md" $BundleDir
}

# ---------------------------------------------------------------------------
# 8. Generate install.bat from installer/templates/install.bat.tmpl
# ---------------------------------------------------------------------------
Write-Step "Generating install.bat from template"
function Render-Template {
    param([string]$TemplatePath, [string]$OutPath)
    $content = Get-Content -Raw -Path $TemplatePath
    $content = $content.Replace("@@PYTHON_VERSION@@", $PythonVersion)
    $content = $content.Replace("@@PROJECT_VERSION@@", $projectVersion)
    $content = $content.Replace("@@PYTHON_VERSION_NODOT@@", $PythonVersion.Replace(".", ""))
    # cmd.exe misparses labels/blocks in LF-only files; the template lives in git with LF.
    $content = $content -replace "`r?`n", "`r`n"
    Set-Content -Path $OutPath -Value $content -Encoding ASCII -NoNewline
}
$batTemplate = Join-Paths @($RepoRoot, "installer", "templates", "install.bat.tmpl")
Render-Template -TemplatePath $batTemplate -OutPath (Join-Path $BundleDir "install.bat")

# ---------------------------------------------------------------------------
# 9. Generate INSTALL.txt with operator instructions
# ---------------------------------------------------------------------------
$installTxt = @"
CMCourier $projectVersion — Offline Install
============================================

Target: Windows Server x86_64 with Python $PythonVersion already installed.

Prerequisites on the server
---------------------------
1. Python $PythonVersion x86_64 from python.org (NOT the Microsoft Store
   alias), with the "py" launcher, in PATH or reachable via "py -$PythonVersion".
   If neither is available, point to it with the PYTHON environment variable
   before running install.bat:
       set PYTHON=C:\PythonXXX\python.exe
       install.bat
2. Microsoft Visual C++ Redistributable 2015-2022 x64 (usually pre-installed).
3. IBM i Access ODBC driver (or equivalent) if there is an AS400/RVI source.
4. Microsoft ODBC Driver 18 for SQL Server if there is an MSSQL source
   (connections.<alias>.kind: mssql in the config).
5. A System DSN (64-bit) configured under "ODBC Data Sources (64-bit)" for
   each ODBC source that uses one.

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
(($installTxt -replace "`r?`n", "`r`n") + "`r`n") |
    Out-File -FilePath (Join-Path $BundleDir "INSTALL.txt") -Encoding UTF8 -NoNewline

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
