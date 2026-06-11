#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Save your work back to USB and remove ArcherDev from this PC.
    Run this before you leave a remote machine.
#>

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$DistroName = "ArcherDev"
$DistroTar  = "$ScriptDir\distro\archer-dev.tar.gz"
$BackupTar  = "$ScriptDir\distro\archer-dev.tar.gz.bak"

Write-Host ""
Write-Host "  ARCHER DEV ENVIRONMENT UNINSTALLER" -ForegroundColor Cyan
Write-Host "  ====================================" -ForegroundColor Cyan
Write-Host ""

# ── Check distro exists ───────────────────────────────────────────────────────

$existing = wsl --list --quiet 2>$null | Where-Object { $_ -match $DistroName }
if (!$existing) {
    Write-Host "[OK] $DistroName is not installed on this PC. Nothing to do." -ForegroundColor Green
    exit 0
}

# ── Save work back to USB ─────────────────────────────────────────────────────

Write-Host "[1/2] Exporting current state back to USB..." -ForegroundColor Yellow
Write-Host "      This saves everything — git commits, file changes, packages installed." -ForegroundColor Gray
Write-Host "      (Takes a few minutes on USB 2.0)" -ForegroundColor Gray
Write-Host ""

# Keep previous tar as backup in case export fails mid-way
if (Test-Path $DistroTar) {
    Copy-Item $DistroTar $BackupTar -Force
}

wsl --terminate $DistroName 2>$null
Start-Sleep -Seconds 2

wsl --export $DistroName $DistroTar

if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] Export failed. Distro NOT removed." -ForegroundColor Red
    Write-Host "        Your previous backup is at: $BackupTar" -ForegroundColor Red
    exit 1
}

Write-Host "      Export complete: $DistroTar" -ForegroundColor Gray

# Remove the .bak now that export succeeded
if (Test-Path $BackupTar) { Remove-Item $BackupTar -Force }

# ── Unregister from Windows ───────────────────────────────────────────────────

Write-Host "[2/2] Unregistering from Windows..." -ForegroundColor Yellow

wsl --unregister $DistroName

if ($LASTEXITCODE -ne 0) {
    Write-Host "[WARN] Unregister may have failed. Check 'wsl --list' manually." -ForegroundColor Yellow
} else {
    Write-Host "      Unregistered. No trace left on this PC." -ForegroundColor Gray
}

# Clean up the WSL folder on USB (the VHDX used during the session)
$InstallPath = "$ScriptDir\WSL\$DistroName"
if (Test-Path $InstallPath) {
    Remove-Item -Recurse -Force $InstallPath
    Write-Host "      Cleaned up session VHDX from USB." -ForegroundColor Gray
}

Write-Host ""
Write-Host "  DONE. Safe to unplug." -ForegroundColor Green
Write-Host "  Your work is saved to: $DistroTar" -ForegroundColor Cyan
Write-Host ""
