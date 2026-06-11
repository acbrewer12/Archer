#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Save your work, stop the VM, uninstall VMware, and remove WSL2.
    Run this before you leave a remote machine. Leaves zero trace.
#>

$ErrorActionPreference = "Stop"
$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$DistroName = "ArcherDev"
$DistroTar  = "$ScriptDir\distro\archer-dev.tar.gz"
$BackupTar  = "$ScriptDir\distro\archer-dev.tar.gz.bak"
$VmDir      = "$env:LOCALAPPDATA\ArcherVM"
$VmxPath    = "$VmDir\archer-os.vmx"

Write-Host ""
Write-Host "  ╔══════════════════════════════════════╗" -ForegroundColor Cyan
Write-Host "  ║   ARCHER FULL ENVIRONMENT UNINSTALL  ║" -ForegroundColor Cyan
Write-Host "  ╚══════════════════════════════════════╝" -ForegroundColor Cyan
Write-Host ""

# ════════════════════════════════════════════════════════════════════════════════
# PART 1 — STOP AND REMOVE VM
# ════════════════════════════════════════════════════════════════════════════════

Write-Host "  [ ARCHER OS VM ]" -ForegroundColor Yellow
Write-Host ""

function Get-VmrunPath {
    $candidates = @(
        "C:\Program Files (x86)\VMware\VMware Player\vmrun.exe",
        "C:\Program Files\VMware\VMware Player\vmrun.exe",
        "C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe",
        "C:\Program Files\VMware\VMware Workstation\vmrun.exe"
    )
    return $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
}

function Get-VmwareUninstaller {
    # Check both 32-bit and 64-bit registry hives
    $hives = @(
        "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*",
        "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*"
    )
    foreach ($hive in $hives) {
        $key = Get-ItemProperty $hive -ErrorAction SilentlyContinue |
               Where-Object { $_.DisplayName -match "VMware.*(Player|Workstation)" } |
               Select-Object -First 1
        if ($key) { return $key }
    }
    return $null
}

# Stop the VM if running
$vmrun = Get-VmrunPath
if ($vmrun -and (Test-Path $VmxPath)) {
    Write-Host "  [1/2] Stopping Archer OS VM..." -ForegroundColor White
    & $vmrun -T player stop $VmxPath nogui 2>$null
    Start-Sleep -Seconds 3
    Write-Host "        Stopped." -ForegroundColor Gray
} else {
    Write-Host "  [1/2] VM not running (or vmrun not found) — skipping stop." -ForegroundColor Gray
}

# Delete local VM files (VMDK copy + VMX)
Write-Host "  [2/2] Removing VM files from local drive..." -ForegroundColor White
if (Test-Path $VmDir) {
    Remove-Item -Recurse -Force $VmDir
    Write-Host "        Deleted: $VmDir" -ForegroundColor Gray
} else {
    Write-Host "        No VM directory found — already clean." -ForegroundColor Gray
}

Write-Host ""

# ════════════════════════════════════════════════════════════════════════════════
# PART 2 — UNINSTALL VMWARE PLAYER
# ════════════════════════════════════════════════════════════════════════════════

Write-Host "  [ VMWARE PLAYER ]" -ForegroundColor Yellow
Write-Host ""

$vmwareKey = Get-VmwareUninstaller
if (!$vmwareKey) {
    Write-Host "  [1/1] VMware not installed — skipping." -ForegroundColor Gray
} else {
    Write-Host "  [1/1] Uninstalling $($vmwareKey.DisplayName)..." -ForegroundColor White

    # VMware Player uses an exe-based uninstaller
    # The UninstallString points to the exe; add /S for silent
    $uninstallExe = $vmwareKey.UninstallString -replace '"', ''
    if (Test-Path $uninstallExe) {
        Start-Process $uninstallExe -ArgumentList "/S" -Wait
    } elseif ($vmwareKey.QuietUninstallString) {
        $quiet = $vmwareKey.QuietUninstallString
        Start-Process "cmd.exe" -ArgumentList "/c $quiet" -Wait
    } else {
        # Fallback: MsiExec with product code
        if ($vmwareKey.PSChildName -match "^\{") {
            Start-Process msiexec.exe -ArgumentList "/x `"$($vmwareKey.PSChildName)`" /qn REBOOT=ReallySuppress" -Wait
        } else {
            Write-Host "        Could not determine uninstall method. Remove VMware manually." -ForegroundColor Yellow
        }
    }

    # Verify removed
    Start-Sleep -Seconds 5
    if (Get-VmwareUninstaller) {
        Write-Host "        [WARN] VMware may still be installed. Check Programs & Features." -ForegroundColor Yellow
    } else {
        Write-Host "        Uninstalled." -ForegroundColor Gray
    }
}

# Clean up VMware leftover folders
$vmwareFolders = @(
    "$env:ProgramFiles\VMware",
    "${env:ProgramFiles(x86)}\VMware",
    "$env:ALLUSERSPROFILE\VMware"
)
foreach ($f in $vmwareFolders) {
    if (Test-Path $f) {
        Remove-Item -Recurse -Force $f -ErrorAction SilentlyContinue
        Write-Host "        Removed: $f" -ForegroundColor Gray
    }
}

Write-Host ""

# ════════════════════════════════════════════════════════════════════════════════
# PART 3 — SAVE WSL2 WORK AND UNREGISTER
# ════════════════════════════════════════════════════════════════════════════════

Write-Host "  [ WSL2 ENVIRONMENT ]" -ForegroundColor Yellow
Write-Host ""

$existing = wsl --list --quiet 2>$null | Where-Object { $_ -match $DistroName }
if (!$existing) {
    Write-Host "  WSL distro not found — already removed or never installed." -ForegroundColor Gray
} else {
    # Export — keep .bak in case export fails mid-write
    Write-Host "  [1/2] Exporting dev environment back to USB..." -ForegroundColor White
    Write-Host "        Saves all git commits, file changes, installed packages." -ForegroundColor Gray
    Write-Host "        (Takes a few minutes on USB 2.0)" -ForegroundColor Gray

    if (Test-Path $DistroTar) { Copy-Item $DistroTar $BackupTar -Force }

    wsl --terminate $DistroName 2>$null
    Start-Sleep -Seconds 2

    wsl --export $DistroName $DistroTar
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[ERROR] WSL export failed. Distro NOT removed." -ForegroundColor Red
        Write-Host "        Your previous backup: $BackupTar" -ForegroundColor Red
        exit 1
    }

    if (Test-Path $BackupTar) { Remove-Item $BackupTar -Force }
    Write-Host "        Exported to: $DistroTar" -ForegroundColor Gray

    # Unregister
    Write-Host "  [2/2] Unregistering from Windows..." -ForegroundColor White
    wsl --unregister $DistroName

    # Clean WSL folder on USB (the session VHDX)
    $WslInstall = "$ScriptDir\WSL\$DistroName"
    if (Test-Path $WslInstall) {
        Remove-Item -Recurse -Force $WslInstall
        Write-Host "        Cleaned session VHDX from USB." -ForegroundColor Gray
    }

    Write-Host "        Unregistered." -ForegroundColor Gray
}

# ════════════════════════════════════════════════════════════════════════════════
# DONE
# ════════════════════════════════════════════════════════════════════════════════

Write-Host ""
Write-Host "  ╔══════════════════════════════════════╗" -ForegroundColor Green
Write-Host "  ║         CLEAN. SAFE TO UNPLUG.       ║" -ForegroundColor Green
Write-Host "  ╚══════════════════════════════════════╝" -ForegroundColor Green
Write-Host ""
Write-Host "  Work saved to: $DistroTar" -ForegroundColor Cyan
Write-Host "  This PC has no trace of Archer." -ForegroundColor Cyan
Write-Host ""
