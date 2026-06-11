#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Install the full Archer dev environment on any Windows PC from USB.
    Sets up WSL2 (dev environment) + VMware Player + Archer OS VM.
    Run uninstall-archer.ps1 before you leave to save work and clean up.
#>

$ErrorActionPreference = "Stop"
$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$DistroTar   = "$ScriptDir\distro\archer-dev.tar.gz"
$KernelMsi   = "$ScriptDir\tools\wsl_update_x64.msi"
$VmwareExe   = "$ScriptDir\tools\VMware-player.exe"
$VmdkSrc     = "$ScriptDir\vm\archer-os.vmdk"
$VmDir       = "$env:LOCALAPPDATA\ArcherVM"        # VM lives on host SSD during session
$VmxPath     = "$VmDir\archer-os.vmx"
$DistroName  = "ArcherDev"
$WslInstall  = "$ScriptDir\WSL\$DistroName"        # WSL VHDX lives on USB

Write-Host ""
Write-Host "  ╔══════════════════════════════════════╗" -ForegroundColor Cyan
Write-Host "  ║    ARCHER FULL ENVIRONMENT INSTALL   ║" -ForegroundColor Cyan
Write-Host "  ╚══════════════════════════════════════╝" -ForegroundColor Cyan
Write-Host ""

# ── Preflight ─────────────────────────────────────────────────────────────────

$missingFiles = @()
if (!(Test-Path $DistroTar)) { $missingFiles += "distro\archer-dev.tar.gz (WSL environment)" }
if (!(Test-Path $VmdkSrc))   { $missingFiles += "vm\archer-os.vmdk (Archer OS VM disk)" }
if ($missingFiles.Count -gt 0) {
    Write-Host "[ERROR] Missing files on USB:" -ForegroundColor Red
    $missingFiles | ForEach-Object { Write-Host "        - $_" -ForegroundColor Red }
    Write-Host ""
    Write-Host "        Run prepare-usb.ps1 on your home PC first." -ForegroundColor Yellow
    exit 1
}

# ════════════════════════════════════════════════════════════════════════════════
# PART 1 — WSL2
# ════════════════════════════════════════════════════════════════════════════════

Write-Host "  [ WSL2 SETUP ]" -ForegroundColor Yellow
Write-Host ""

# ── Enable Windows features ───────────────────────────────────────────────────

Write-Host "  [1/3] Checking Windows features..." -ForegroundColor White

$wslFeature = Get-WindowsOptionalFeature -Online -FeatureName "Microsoft-Windows-Subsystem-Linux"
$vmFeature  = Get-WindowsOptionalFeature -Online -FeatureName "VirtualMachinePlatform"
$needsReboot = $false

if ($wslFeature.State -ne "Enabled") {
    Write-Host "        Enabling WSL..." -ForegroundColor Gray
    dism.exe /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart | Out-Null
    $needsReboot = $true
}
if ($vmFeature.State -ne "Enabled") {
    Write-Host "        Enabling VirtualMachinePlatform..." -ForegroundColor Gray
    dism.exe /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart | Out-Null
    $needsReboot = $true
}

if ($needsReboot) {
    $scriptPath = $MyInvocation.MyCommand.Path
    Set-ItemProperty `
        -Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce" `
        -Name "ArcherInstall" `
        -Value "powershell -ExecutionPolicy Bypass -WindowStyle Normal -File `"$scriptPath`""
    Write-Host ""
    Write-Host "  [!] Reboot required. Installer will resume automatically after reboot." -ForegroundColor Yellow
    $c = Read-Host "  Reboot now? (y/n)"
    if ($c -eq "y") { Restart-Computer -Force }
    exit 0
}
Write-Host "        Already enabled." -ForegroundColor Gray

# ── WSL2 kernel ───────────────────────────────────────────────────────────────

Write-Host "  [2/3] Installing WSL2 kernel..." -ForegroundColor White

if (Test-Path $KernelMsi) {
    Start-Process msiexec.exe -ArgumentList "/i `"$KernelMsi`" /quiet /norestart" -Wait
    Write-Host "        Installed from USB." -ForegroundColor Gray
} else {
    Write-Host "        No bundled MSI — trying wsl --update (needs internet)..." -ForegroundColor Gray
    wsl --update 2>$null
}
wsl --set-default-version 2 | Out-Null

# ── Import distro ─────────────────────────────────────────────────────────────

$wslAlreadyInstalled = $false
$existing = wsl --list --quiet 2>$null | Where-Object { $_ -match $DistroName }
if ($existing) {
    Write-Host "  [3/3] WSL distro already imported, skipping." -ForegroundColor Gray
    $wslAlreadyInstalled = $true
} else {
    Write-Host "  [3/3] Importing ArcherDev distro..." -ForegroundColor White
    Write-Host "        (Takes a few minutes on USB 2.0)" -ForegroundColor Gray

    if (!(Test-Path $WslInstall)) { New-Item -ItemType Directory -Path $WslInstall -Force | Out-Null }

    wsl --import $DistroName $WslInstall $DistroTar --version 2
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[ERROR] WSL import failed." -ForegroundColor Red
        exit 1
    }

    # Restore default user (wsl --import always defaults to root)
    wsl -d $DistroName -u root -- bash -c "
        U=\$(getent passwd 1000 | cut -d: -f1)
        if [ -n \"\$U\" ]; then
            printf '[automount]\noptions = \"metadata\"\n[user]\ndefault=%s\n' \"\$U\" > /etc/wsl.conf
        fi
    " 2>$null
    wsl --terminate $DistroName 2>$null
    Write-Host "        Import complete." -ForegroundColor Gray
}

Write-Host ""

# ════════════════════════════════════════════════════════════════════════════════
# PART 2 — VMWARE PLAYER
# ════════════════════════════════════════════════════════════════════════════════

Write-Host "  [ VMWARE PLAYER ]" -ForegroundColor Yellow
Write-Host ""

function Get-VmwarePath {
    $candidates = @(
        "C:\Program Files (x86)\VMware\VMware Player",
        "C:\Program Files\VMware\VMware Player",
        "C:\Program Files (x86)\VMware\VMware Workstation",
        "C:\Program Files\VMware\VMware Workstation"
    )
    return $candidates | Where-Object { Test-Path "$_\vmplayer.exe" } | Select-Object -First 1
}

function Get-VmrunPath {
    $candidates = @(
        "C:\Program Files (x86)\VMware\VMware Player\vmrun.exe",
        "C:\Program Files\VMware\VMware Player\vmrun.exe",
        "C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe",
        "C:\Program Files\VMware\VMware Workstation\vmrun.exe"
    )
    return $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
}

$vmwareDir = Get-VmwarePath
if ($vmwareDir) {
    Write-Host "  [1/2] VMware Player already installed at: $vmwareDir" -ForegroundColor Gray
} else {
    Write-Host "  [1/2] Installing VMware Player..." -ForegroundColor White

    if (!(Test-Path $VmwareExe)) {
        Write-Host "[ERROR] VMware installer not found at: $VmwareExe" -ForegroundColor Red
        Write-Host "        Download VMware Workstation Player and save it to tools\VMware-player.exe" -ForegroundColor Yellow
        Write-Host "        Continuing without VM setup." -ForegroundColor Yellow
        Write-Host ""
        goto SkipVm
    }

    Write-Host "        Running installer (this takes 2-3 minutes)..." -ForegroundColor Gray
    Start-Process $VmwareExe -ArgumentList "/s /v`"/qn REBOOT=ReallySuppress EULAS_AGREED=1`"" -Wait

    $vmwareDir = Get-VmwarePath
    if (!$vmwareDir) {
        Write-Host "[ERROR] VMware install appears to have failed." -ForegroundColor Red
        Write-Host "        Continuing without VM — WSL2 is ready." -ForegroundColor Yellow
        goto SkipVm
    }
    Write-Host "        Installed." -ForegroundColor Gray
}

# ── Deploy Archer OS VM ───────────────────────────────────────────────────────

Write-Host "  [2/2] Deploying Archer OS VM..." -ForegroundColor White

if (!(Test-Path $VmDir)) { New-Item -ItemType Directory -Path $VmDir -Force | Out-Null }

# Copy VMDK from USB to local SSD for performance (USB 2.0 would throttle VM badly)
$VmdkDest = "$VmDir\archer-os.vmdk"
if (!(Test-Path $VmdkDest)) {
    Write-Host "        Copying VMDK to local drive for performance..." -ForegroundColor Gray
    Write-Host "        (archer-os.vmdk ~1.5GB — takes ~2 min on USB 2.0)" -ForegroundColor Gray
    Copy-Item $VmdkSrc $VmdkDest -Force
    Write-Host "        Copy complete." -ForegroundColor Gray
} else {
    Write-Host "        VMDK already on local drive." -ForegroundColor Gray
}

# Write VMX config
@"
.encoding = "UTF-8"
config.version = "8"
virtualHW.version = "19"
displayName = "Archer OS"
guestOS = "debian11-64"
memsize = "4096"
numvcpus = "2"
cpuid.coresPerSocket = "2"
scsi0.present = "TRUE"
scsi0.virtualDev = "pvscsi"
scsi0:0.present = "TRUE"
scsi0:0.fileName = "archer-os.vmdk"
scsi0:0.mode = "persistent"
ethernet0.present = "TRUE"
ethernet0.virtualDev = "e1000"
ethernet0.connectionType = "nat"
ethernet0.addressType = "generated"
ethernet0.wakeOnPcktRcv = "FALSE"
usb.present = "TRUE"
usb_xhci.present = "TRUE"
usb.vbluetooth.startConnected = "FALSE"
sound.present = "FALSE"
floppy0.present = "FALSE"
tools.syncTime = "FALSE"
"@ | Set-Content -Path $VmxPath -Encoding UTF8

Write-Host "        VM config written." -ForegroundColor Gray

# Launch
Write-Host "        Launching Archer OS VM..." -ForegroundColor Gray
$vmplayerExe = "$vmwareDir\vmplayer.exe"
Start-Process $vmplayerExe -ArgumentList "`"$VmxPath`""

:SkipVm

# ════════════════════════════════════════════════════════════════════════════════
# DONE
# ════════════════════════════════════════════════════════════════════════════════

Write-Host ""
Write-Host "  ╔══════════════════════════════════════╗" -ForegroundColor Green
Write-Host "  ║           SETUP COMPLETE             ║" -ForegroundColor Green
Write-Host "  ╚══════════════════════════════════════╝" -ForegroundColor Green
Write-Host ""
Write-Host "  WSL2:  wsl -d ArcherDev" -ForegroundColor Cyan
Write-Host "  VM:    VMware Player is open" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Run uninstall-archer.ps1 before you leave." -ForegroundColor Yellow
Write-Host ""

# Drop into WSL
wsl -d $DistroName
