#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Install Archer WSL2 dev environment from USB onto any Windows PC.
    Run this when you arrive at a remote machine.
    Run uninstall-archer.ps1 before you leave to save your work back to USB.
#>

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$DistroTar = "$ScriptDir\distro\archer-dev.tar.gz"
$KernelMsi = "$ScriptDir\tools\wsl_update_x64.msi"
$DistroName = "ArcherDev"
$InstallPath = "$ScriptDir\WSL\$DistroName"   # VHDX lives ON the USB, not host PC

Write-Host ""
Write-Host "  ARCHER DEV ENVIRONMENT INSTALLER" -ForegroundColor Cyan
Write-Host "  ==================================" -ForegroundColor Cyan
Write-Host ""

# ── Preflight checks ──────────────────────────────────────────────────────────

if (!(Test-Path $DistroTar)) {
    Write-Host "[ERROR] Distro not found: $DistroTar" -ForegroundColor Red
    Write-Host "        Run export-archer.ps1 on your home PC first to create it." -ForegroundColor Red
    exit 1
}

# Check if already installed
$existing = wsl --list --quiet 2>$null | Where-Object { $_ -match $DistroName }
if ($existing) {
    Write-Host "[OK] $DistroName is already installed. Starting..." -ForegroundColor Green
    wsl -d $DistroName
    exit 0
}

# ── Enable WSL2 Windows features ──────────────────────────────────────────────

Write-Host "[1/4] Checking Windows features..." -ForegroundColor Yellow

$wslFeature = Get-WindowsOptionalFeature -Online -FeatureName "Microsoft-Windows-Subsystem-Linux"
$vmFeature  = Get-WindowsOptionalFeature -Online -FeatureName "VirtualMachinePlatform"
$needsReboot = $false

if ($wslFeature.State -ne "Enabled") {
    Write-Host "      Enabling WSL..." -ForegroundColor Gray
    dism.exe /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart | Out-Null
    $needsReboot = $true
}

if ($vmFeature.State -ne "Enabled") {
    Write-Host "      Enabling VirtualMachinePlatform..." -ForegroundColor Gray
    dism.exe /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart | Out-Null
    $needsReboot = $true
}

if ($needsReboot) {
    # Schedule this script to re-run after reboot via RunOnce
    $scriptPath = $MyInvocation.MyCommand.Path
    Set-ItemProperty `
        -Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce" `
        -Name "ArcherDevInstall" `
        -Value "powershell -ExecutionPolicy Bypass -WindowStyle Normal -File `"$scriptPath`""

    Write-Host ""
    Write-Host "[!] Reboot required to enable WSL2." -ForegroundColor Yellow
    Write-Host "    The installer will continue automatically after reboot." -ForegroundColor Yellow
    Write-Host ""
    $choice = Read-Host "Reboot now? (y/n)"
    if ($choice -eq "y") { Restart-Computer -Force }
    exit 0
}

Write-Host "      Features already enabled." -ForegroundColor Gray

# ── Install WSL2 kernel update (bundled on USB, no internet needed) ───────────

Write-Host "[2/4] Installing WSL2 kernel..." -ForegroundColor Yellow

if (Test-Path $KernelMsi) {
    Write-Host "      Installing from USB: $KernelMsi" -ForegroundColor Gray
    Start-Process msiexec.exe -ArgumentList "/i `"$KernelMsi`" /quiet /norestart" -Wait
    Write-Host "      Done." -ForegroundColor Gray
} else {
    Write-Host "      Bundled MSI not found — trying wsl --update (needs internet)..." -ForegroundColor Gray
    wsl --update 2>$null
}

wsl --set-default-version 2 | Out-Null

# ── Import the distro onto USB ────────────────────────────────────────────────

Write-Host "[3/4] Importing ArcherDev distro (this takes a few minutes on USB 2.0)..." -ForegroundColor Yellow

if (!(Test-Path $InstallPath)) {
    New-Item -ItemType Directory -Path $InstallPath -Force | Out-Null
}

wsl --import $DistroName $InstallPath $DistroTar --version 2

if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] Import failed. Check that the distro tar is not corrupted." -ForegroundColor Red
    exit 1
}

Write-Host "      Import complete." -ForegroundColor Gray

# ── Configure default user ────────────────────────────────────────────────────

Write-Host "[4/4] Configuring default user..." -ForegroundColor Yellow

# wsl --import always sets root as default user; restore the archer/ayden user
wsl -d $DistroName -u root -- bash -c "
    # Find the non-root user (uid 1000)
    USER1000=\$(getent passwd 1000 | cut -d: -f1)
    if [ -n \"\$USER1000\" ]; then
        echo '[automount]\noptions = \"metadata\"' > /etc/wsl.conf
        printf '[user]\ndefault=%s\n' \"\$USER1000\" >> /etc/wsl.conf
        echo \"Default user set to: \$USER1000\"
    fi
" 2>$null

# Restart so wsl.conf takes effect
wsl --terminate $DistroName 2>$null

Write-Host ""
Write-Host "  DONE. Starting ArcherDev..." -ForegroundColor Green
Write-Host ""
Write-Host "  Your work is saved to: $InstallPath" -ForegroundColor Cyan
Write-Host "  Run uninstall-archer.ps1 before you leave." -ForegroundColor Cyan
Write-Host ""

wsl -d $DistroName
