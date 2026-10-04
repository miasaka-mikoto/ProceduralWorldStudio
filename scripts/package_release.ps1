<#
.SYNOPSIS
    Create portable and source ZIP files for a Windows release.
.DESCRIPTION
    Runs the normal Windows build, then creates two small, explicit archives:
    the portable executable bundle and the reproducible source/demo/tests
    bundle.  It deliberately does not archive virtual environments or build
    caches.
#>
[CmdletBinding()]
param(
    [switch]$SkipTests,
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

& (Join-Path $PSScriptRoot "build_windows.ps1") -SkipTests:$SkipTests -Python $Python

$Version = "0.1.0"
$DistRoot = Join-Path $Root "dist"
$AppDir = Join-Path $DistRoot "ProceduralWorldStudio"
if (-not (Test-Path $AppDir)) {
    throw "Expected onedir app at $AppDir. Rebuild without -OneFile for a portable release archive."
}

$PortableZip = Join-Path $DistRoot "ProceduralWorldStudio_win64_v$Version.zip"
if (Test-Path $PortableZip) { Remove-Item $PortableZip -Force }
Compress-Archive -Path $AppDir -DestinationPath $PortableZip -CompressionLevel Optimal

$SourceItems = @(
    "README.md",
    "pyproject.toml",
    "requirements.txt",
    "main.py",
    "procedural_world_studio.spec",
    "procedural_world_studio",
    "scripts",
    "tests",
    "docs",
    "demo"
) | ForEach-Object { Join-Path $Root $_ } | Where-Object { Test-Path $_ }

$SourceZip = Join-Path $DistRoot "ProceduralWorldStudio_source_v$Version.zip"
if (Test-Path $SourceZip) { Remove-Item $SourceZip -Force }
Compress-Archive -Path $SourceItems -DestinationPath $SourceZip -CompressionLevel Optimal

Write-Host "Portable release: $PortableZip"
Write-Host "Source release:   $SourceZip"
