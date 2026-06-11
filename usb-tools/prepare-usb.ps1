#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Prepare the USB with everything needed for remote use.
    Run this ONCE on your home PC before taking the USB anywhere.
    After that, uninstall-archer.ps1 keeps your WSL state updated automatically.

    Copies to USB:
      distro\archer-dev.tar.gz   — your WSL2 dev environment
      vm\archer-os.vmdk          — Archer OS VM disk (built by build-vm.sh)
#>

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host ""
Write-Host "  ╔══════════════════════════════════════╗" -ForegroundColor Cyan
Write-Host "  ║        ARCHER USB PREPARATION        ║" -ForegroundColor Cyan
Write-Host "  ╚══════════════════════════════════════╝" -ForegroundColor Cyan
Write-Host ""

# ── WSL distro export ─────────────────────────────────────────────────────────

Write-Host "  [ STEP 1 — WSL2 ENVIRONMENT ]" -ForegroundColor Yellow
Write-Host ""
Write-Host "  Available WSL2 distros:" -ForegroundColor White
wsl --list --verbose
Write-Host ""

$DistroName = Read-Host "  Enter distro name to export (e.g. Ubuntu)"

$found = wsl --list --quiet 2>$null | Where-Object { $_ -match $DistroName }
if (!$found) {
    Write-Host "  [ERROR] '$DistroName' not found." -ForegroundColor Red
    exit 1
}

$DistroTar = "$ScriptDir\distro\archer-dev.tar.gz"
if (!(Test-Path "$ScriptDir\distro")) {
    New-Item -ItemType Directory -Path "$ScriptDir\distro" -Force | Out-Null
}

Write-Host ""
Write-Host "  Exporting '$DistroName'..." -ForegroundColor White
Write-Host "  (5-15 min depending on distro size)" -ForegroundColor Gray

wsl --terminate $DistroName 2>$null
Start-Sleep -Seconds 2

wsl --export $DistroName $DistroTar
if ($LASTEXITCODE -ne 0) {
    Write-Host "  [ERROR] Export failed." -ForegroundColor Red
    exit 1
}

$sizeMB = [math]::Round((Get-Item $DistroTar).Length / 1MB)
Write-Host "  Exported: ${sizeMB}MB → $DistroTar" -ForegroundColor Gray

# ── VMDK copy ─────────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "  [ STEP 2 — ARCHER OS VM DISK ]" -ForegroundColor Yellow
Write-Host ""

if (!(Test-Path "$ScriptDir\vm")) {
    New-Item -ItemType Directory -Path "$ScriptDir\vm" -Force | Out-Null
}

$VmdkDest = "$ScriptDir\vm\archer-os.vmdk"

# Try to find VMDK automatically in WSL filesystem
$wslPaths = @(
    "\\wsl$\$DistroName\home\ayden\Archer\archer-os.vmdk",
    "\\wsl$\$DistroName\home\user\Archer\archer-os.vmdk",
    "\\wsl$\Ubuntu\home\ayden\Archer\archer-os.vmdk",
    "\\wsl$\Ubuntu\home\user\Archer\archer-os.vmdk"
)

$foundVmdk = $wslPaths | Where-Object { Test-Path $_ } | Select-Object -First 1

if ($foundVmdk) {
    Write-Host "  Found VMDK at: $foundVmdk" -ForegroundColor Gray
    Write-Host "  Copying to USB (archer-os.vmdk ~1.5GB)..." -ForegroundColor White
    Copy-Item $foundVmdk $VmdkDest -Force
    $vmSizeMB = [math]::Round((Get-Item $VmdkDest).Length / 1MB)
    Write-Host "  Copied: ${vmSizeMB}MB → $VmdkDest" -ForegroundColor Gray
} else {
    Write-Host "  Could not find archer-os.vmdk automatically." -ForegroundColor Yellow
    Write-Host "  Enter the full path to archer-os.vmdk" -ForegroundColor White
    Write-Host "  (or press Enter to skip — you can copy it manually later)" -ForegroundColor Gray
    $manualPath = Read-Host "  Path"
    if ($manualPath -and (Test-Path $manualPath)) {
        Write-Host "  Copying..." -ForegroundColor White
        Copy-Item $manualPath $VmdkDest -Force
        Write-Host "  Copied → $VmdkDest" -ForegroundColor Gray
    } else {
        Write-Host "  Skipped. Copy archer-os.vmdk to: $VmdkDest" -ForegroundColor Yellow
    }
}

# ── WSL kernel MSI reminder ───────────────────────────────────────────────────

Write-Host ""
Write-Host "  [ STEP 3 — WSL2 KERNEL (optional but recommended) ]" -ForegroundColor Yellow
Write-Host ""

$KernelMsi = "$ScriptDir\tools\wsl_update_x64.msi"
if (Test-Path $KernelMsi) {
    Write-Host "  WSL2 kernel MSI already present." -ForegroundColor Gray
} else {
    if (!(Test-Path "$ScriptDir\tools")) {
        New-Item -ItemType Directory -Path "$ScriptDir\tools" -Force | Out-Null
    }
    Write-Host "  Download the WSL2 kernel and save to:" -ForegroundColor Yellow
    Write-Host "  $KernelMsi" -ForegroundColor White
    Write-Host ""
    Write-Host "  Without it, install-archer.ps1 will try 'wsl --update' (needs internet)." -ForegroundColor Gray
}

# ── VMware installer reminder ─────────────────────────────────────────────────

$VmwareExe = "$ScriptDir\tools\VMware-player.exe"
if (Test-Path $VmwareExe) {
    Write-Host ""
    Write-Host "  VMware installer already present." -ForegroundColor Gray
} else {
    Write-Host ""
    Write-Host "  [ STEP 4 — VMWARE PLAYER INSTALLER (optional) ]" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  Download VMware Workstation Player and save to:" -ForegroundColor Yellow
    Write-Host "  $VmwareExe" -ForegroundColor White
    Write-Host ""
    Write-Host "  Without it, install-archer.ps1 will skip the VM on PCs without VMware." -ForegroundColor Gray
}

# ── Summary ───────────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "  ╔══════════════════════════════════════╗" -ForegroundColor Green
Write-Host "  ║           USB IS READY               ║" -ForegroundColor Green
Write-Host "  ╚══════════════════════════════════════╝" -ForegroundColor Green
Write-Host ""

$items = @(
    @{ Path = "$ScriptDir\distro\archer-dev.tar.gz"; Label = "WSL2 environment" },
    @{ Path = "$ScriptDir\vm\archer-os.vmdk";        Label = "Archer OS VM disk" },
    @{ Path = "$ScriptDir\tools\wsl_update_x64.msi"; Label = "WSL2 kernel MSI" },
    @{ Path = "$ScriptDir\tools\VMware-player.exe";  Label = "VMware installer" }
)
foreach ($item in $items) {
    $status = if (Test-Path $item.Path) { "[✓]" } else { "[ ]" }
    $color  = if (Test-Path $item.Path) { "Green" } else { "Yellow" }
    Write-Host "  $status $($item.Label)" -ForegroundColor $color
}

Write-Host ""
Write-Host "  Take the USB to any Windows PC and run install-archer.ps1 as Admin." -ForegroundColor Cyan
Write-Host ""
