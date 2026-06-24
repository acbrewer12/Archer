#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Export your home PC's WSL2 distro to the USB drive.
    Run this ONCE on your home PC to populate the USB before taking it remote.
    After that, uninstall-archer.ps1 keeps it updated automatically.
#>

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$DistroTar = "$ScriptDir\distro\archer-dev.tar.gz"

Write-Host ""
Write-Host "  ARCHER DEV ENVIRONMENT EXPORT" -ForegroundColor Cyan
Write-Host "  ===============================" -ForegroundColor Cyan
Write-Host ""

# List available distros so user can pick the right one
Write-Host "Available WSL2 distros:" -ForegroundColor Yellow
wsl --list --verbose
Write-Host ""

$DistroName = Read-Host "Enter the distro name to export (e.g. Ubuntu)"

$existing = wsl --list --quiet 2>$null | Where-Object { $_ -match $DistroName }
if (!$existing) {
    Write-Host "[ERROR] '$DistroName' not found in WSL." -ForegroundColor Red
    exit 1
}

if (!(Test-Path "$ScriptDir\distro")) {
    New-Item -ItemType Directory -Path "$ScriptDir\distro" -Force | Out-Null
}

Write-Host ""
Write-Host "Exporting '$DistroName' to $DistroTar ..." -ForegroundColor Yellow
Write-Host "(This may take 5-15 minutes depending on distro size)" -ForegroundColor Gray
Write-Host ""

wsl --terminate $DistroName 2>$null
Start-Sleep -Seconds 2

wsl --export $DistroName $DistroTar

if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] Export failed." -ForegroundColor Red
    exit 1
}

$sizeMB = [math]::Round((Get-Item $DistroTar).Length / 1MB)
Write-Host ""
Write-Host "  DONE. Exported ${sizeMB}MB to USB." -ForegroundColor Green
Write-Host "  Take the USB to any Windows PC and run install-archer.ps1" -ForegroundColor Cyan
Write-Host ""
Write-Host "  NOTE: Also download the WSL2 kernel MSI and place it at:" -ForegroundColor Yellow
Write-Host "        $ScriptDir\tools\wsl_update_x64.msi" -ForegroundColor Yellow
Write-Host "  Download from: https://wslstorestorage.blob.core.windows.net/wslblob/wsl_update_x64.msi" -ForegroundColor Gray
Write-Host ""
