<#
.SYNOPSIS
    Build a portable ProceduralWorldStudio Windows application.
.DESCRIPTION
    Creates an isolated virtual environment, installs project requirements,
    runs tests, generates the deterministic demo, and invokes PyInstaller.
#>
[CmdletBinding()]
param(
    [switch]$OneFile,
    [switch]$SkipTests,
    [switch]$SkipInstall,
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Venv = Join-Path $Root ".build-venv"
$Py = Join-Path $Venv "Scripts\python.exe"

if (-not (Test-Path $Py)) {
    & $Python -m venv $Venv
}
if (-not $SkipInstall) {
    & $Py -m pip install --upgrade pip
    if (Test-Path (Join-Path $Root "requirements.txt")) {
        & $Py -m pip install -r (Join-Path $Root "requirements.txt")
    }
    & $Py -m pip install pyinstaller
}
if (-not $SkipTests) {
    & $Py -m pytest -q
}

$Demo = Join-Path $Root "demo\coastal_city"
& $Py (Join-Path $Root "scripts\generate_demo.py") --output $Demo

$Spec = Join-Path $Root "procedural_world_studio.spec"
# PyInstaller's COLLECT step may reuse stale Qt symlinks after an interrupted
# build.  Both directories are generated artifacts, so clear the exact
# project-owned targets before starting a clean release build.
$BuildDir = Join-Path $Root "build"
$DistDir = Join-Path $Root "dist\ProceduralWorldStudio"
if (Test-Path $BuildDir) { Remove-Item $BuildDir -Recurse -Force }
if (Test-Path $DistDir) { Remove-Item $DistDir -Recurse -Force }
if ($OneFile) {
    $env:PWS_ONEFILE = "1"
}
& $Py -m PyInstaller --noconfirm --clean $Spec

$Dist = Join-Path $Root "dist\ProceduralWorldStudio"
if ($OneFile) {
    $Dist = Join-Path $Root "dist"
}
if (Test-Path $Dist) {
    Copy-Item (Join-Path $Root "README.md") $Dist -Force
    if (Test-Path $Demo) {
        $DemoTarget = Join-Path $Dist "demo\coastal_city"
        New-Item -ItemType Directory -Path $DemoTarget -Force | Out-Null
        Copy-Item (Join-Path $Demo "*") $DemoTarget -Recurse -Force
    }
}
Write-Host "Build complete: $Dist"
