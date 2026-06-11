#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Install the full Archer dev environment on any Windows PC from USB.
    Checks every component first - only installs what is missing.
    If everything is already set up, launches immediately.
    Run uninstall-archer.ps1 before you leave to save work and clean up.
#>

$ErrorActionPreference = "Stop"
$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$DistroTar   = "$ScriptDir\distro\archer-dev.tar.gz"
$KernelMsi   = "$ScriptDir\tools\wsl_update_x64.msi"
$VmwareExe   = "$ScriptDir\tools\VMware-player.exe"
$VmdkPath    = "$ScriptDir\vm\archer-os.vmdk"
$VmxPath     = "$ScriptDir\vm\archer-os.vmx"
$DistroName  = "ArcherDev"
$WslInstall  = "$ScriptDir\WSL\$DistroName"

function Get-VmwarePath {
    @(
        "C:\Program Files (x86)\VMware\VMware Player",
        "C:\Program Files\VMware\VMware Player",
        "C:\Program Files (x86)\VMware\VMware Workstation",
        "C:\Program Files\VMware\VMware Workstation"
    ) | Where-Object { Test-Path "$_\vmplayer.exe" } | Select-Object -First 1
}

function Get-VmrunPath {
    @(
        "C:\Program Files (x86)\VMware\VMware Player\vmrun.exe",
        "C:\Program Files\VMware\VMware Player\vmrun.exe",
        "C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe",
        "C:\Program Files\VMware\VMware Workstation\vmrun.exe"
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1
}

Write-Host ""
Write-Host "  +======================================+" -ForegroundColor Cyan
Write-Host "  |   ARCHER FULL ENVIRONMENT INSTALL    |" -ForegroundColor Cyan
Write-Host "  +======================================+" -ForegroundColor Cyan
Write-Host ""

# ------------------------------------------------------------------------------
# STATUS CHECK - inspect everything before touching anything
# ------------------------------------------------------------------------------

Write-Host "  Checking current state..." -ForegroundColor DarkGray
Write-Host ""

$usbDistroOk = Test-Path $DistroTar
$usbVmdkOk   = Test-Path $VmdkPath

$wslFeature  = Get-WindowsOptionalFeature -Online -FeatureName "Microsoft-Windows-Subsystem-Linux"
$vmFeature   = Get-WindowsOptionalFeature -Online -FeatureName "VirtualMachinePlatform"
$wslFeatOk   = ($wslFeature.State -eq "Enabled") -and ($vmFeature.State -eq "Enabled")

$wslKernelOk = $false
try { wsl --version 2>$null | Out-Null; $wslKernelOk = ($LASTEXITCODE -eq 0) } catch {}

$wslDistroOk = [bool](wsl --list --quiet 2>$null | Where-Object { $_ -match $DistroName })

$vmwareDir   = Get-VmwarePath
$vmwareOk    = [bool]$vmwareDir

$vmDeployed  = Test-Path $VmxPath

function Status($label, $ok, $note="") {
    $icon  = if ($ok) { "[OK]" } else { "[  ]" }
    $color = if ($ok) { "Green" } else { "Yellow" }
    $line  = "  $icon  $label"
    if ($note) { $line += "  ($note)" }
    Write-Host $line -ForegroundColor $color
}

Status "WSL2 Windows features"    $wslFeatOk
Status "WSL2 kernel"              $wslKernelOk
Status "ArcherDev WSL distro"     $wslDistroOk
Status "VMware Player"            $vmwareOk   (if ($vmwareDir) { $vmwareDir } else { "" })
Status "Archer OS VM (on USB)"    $vmDeployed
Write-Host ""

# ------------------------------------------------------------------------------
# Preflight - missing USB source files
# ------------------------------------------------------------------------------

$missing = @()
if (!$usbDistroOk) { $missing += "distro\archer-dev.tar.gz  (WSL environment)" }
if (!$usbVmdkOk)   { $missing += "vm\archer-os.vmdk          (Archer OS VM disk)" }
if ($missing.Count -gt 0) {
    Write-Host "  [ERROR] Required files missing from USB:" -ForegroundColor Red
    $missing | ForEach-Object { Write-Host "          - $_" -ForegroundColor Red }
    Write-Host ""
    Write-Host "  Run prepare-usb.ps1 on your home PC first." -ForegroundColor Yellow
    exit 1
}

# ------------------------------------------------------------------------------
# If everything is ready - just launch
# ------------------------------------------------------------------------------

if ($wslFeatOk -and $wslKernelOk -and $wslDistroOk -and $vmwareOk -and $vmDeployed) {
    Write-Host "  Everything is already installed. Launching..." -ForegroundColor Green
    Write-Host ""
    Start-Process "$vmwareDir\vmplayer.exe" -ArgumentList "`"$VmxPath`""
    wsl -d $DistroName
    exit 0
}

Write-Host "  Installing missing components..." -ForegroundColor White
Write-Host ""

# ------------------------------------------------------------------------------
# WSL2 Windows features
# ------------------------------------------------------------------------------

if (!$wslFeatOk) {
    Write-Host "  [WSL] Enabling Windows features..." -ForegroundColor Yellow
    if ($wslFeature.State -ne "Enabled") {
        dism.exe /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart | Out-Null
    }
    if ($vmFeature.State -ne "Enabled") {
        dism.exe /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart | Out-Null
    }
    Set-ItemProperty `
        -Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce" `
        -Name "ArcherInstall" `
        -Value "powershell -ExecutionPolicy Bypass -WindowStyle Normal -File `"$($MyInvocation.MyCommand.Path)`""
    Write-Host ""
    Write-Host "  [!] Reboot required. Installer will resume automatically." -ForegroundColor Yellow
    $c = Read-Host "  Reboot now? (y/n)"
    if ($c -eq "y") { Restart-Computer -Force }
    exit 0
}

# ------------------------------------------------------------------------------
# WSL2 kernel
# ------------------------------------------------------------------------------

if (!$wslKernelOk) {
    Write-Host "  [WSL] Installing WSL2 kernel..." -ForegroundColor Yellow
    if (Test-Path $KernelMsi) {
        Start-Process msiexec.exe -ArgumentList "/i `"$KernelMsi`" /quiet /norestart" -Wait
        Write-Host "        Done (from USB)." -ForegroundColor Gray
    } else {
        Write-Host "        No bundled MSI - running wsl --update (needs internet)..." -ForegroundColor Gray
        wsl --update 2>$null
    }
}

wsl --set-default-version 2 2>$null | Out-Null

# ------------------------------------------------------------------------------
# WSL distro import
# ------------------------------------------------------------------------------

if (!$wslDistroOk) {
    Write-Host "  [WSL] Importing ArcherDev distro..." -ForegroundColor Yellow
    Write-Host "        (a few minutes on USB 2.0)" -ForegroundColor Gray

    if (!(Test-Path $WslInstall)) { New-Item -ItemType Directory -Path $WslInstall -Force | Out-Null }

    wsl --import $DistroName $WslInstall $DistroTar --version 2
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  [ERROR] WSL import failed." -ForegroundColor Red
        exit 1
    }

    wsl -d $DistroName -u root -- bash -c "
        U=\$(getent passwd 1000 | cut -d: -f1)
        if [ -n \"\$U\" ]; then
            printf '[automount]\noptions = \"metadata\"\n[user]\ndefault=%s\n' \"\$U\" > /etc/wsl.conf
        fi
    " 2>$null
    wsl --terminate $DistroName 2>$null
    Write-Host "        Done." -ForegroundColor Gray
}

# ------------------------------------------------------------------------------
# VMware Player
# ------------------------------------------------------------------------------

$skipVm = $false

if (!$vmwareOk) {
    Write-Host "  [VM]  Installing VMware Player..." -ForegroundColor Yellow
    if (!(Test-Path $VmwareExe)) {
        Write-Host "        Installer not found at tools\VMware-player.exe" -ForegroundColor Yellow
        Write-Host "        Skipping VM setup - WSL2 is ready." -ForegroundColor Yellow
        $skipVm = $true
    } else {
        Write-Host "        Running installer (~3 min)..." -ForegroundColor Gray
        Start-Process $VmwareExe -ArgumentList "/s /v`"/qn REBOOT=ReallySuppress EULAS_AGREED=1`"" -Wait
        $vmwareDir = Get-VmwarePath
        if (!$vmwareDir) {
            Write-Host "        Install failed - skipping VM." -ForegroundColor Yellow
            $skipVm = $true
        } else {
            Write-Host "        Done." -ForegroundColor Gray
        }
    }
}

# ------------------------------------------------------------------------------
# Write VMX pointing at VMDK on USB
# ------------------------------------------------------------------------------

if (!$skipVm -and !$vmDeployed) {
    Write-Host "  [VM]  Writing VMX config to USB..." -ForegroundColor Yellow

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
scsi0:0.fileName = "$VmdkPath"
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

    Write-Host "        Done." -ForegroundColor Gray
}

# ------------------------------------------------------------------------------
# Launch
# ------------------------------------------------------------------------------

Write-Host ""
Write-Host "  +======================================+" -ForegroundColor Green
Write-Host "  |          SETUP COMPLETE              |" -ForegroundColor Green
Write-Host "  +======================================+" -ForegroundColor Green
Write-Host ""

if (!$skipVm -and $vmwareDir) {
    Write-Host "  Launching Archer OS VM..." -ForegroundColor Cyan
    Start-Process "$vmwareDir\vmplayer.exe" -ArgumentList "`"$VmxPath`""
}

Write-Host "  Run uninstall-archer.ps1 before you leave." -ForegroundColor Yellow
Write-Host ""

wsl -d $DistroName
