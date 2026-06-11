ARCHER DEV USB — PORTABLE FULL ENVIRONMENT
==========================================

Plug this USB into any Windows PC and run the scripts below.
Install sets up WSL2 (dev environment) + VMware Player + Archer OS VM.
Uninstall saves your work back to USB and removes everything — zero trace left.

── FIRST TIME (home PC only, once) ──────────────────────────────────────────

1. Open PowerShell as Administrator
2. Run:  .\prepare-usb.ps1
   - Exports your WSL2 distro  →  distro\archer-dev.tar.gz
   - Copies archer-os.vmdk     →  vm\archer-os.vmdk
   - Shows checklist of what's missing (VMware installer, WSL kernel MSI)

3. Optional but recommended — add these to the USB manually:
   tools\wsl_update_x64.msi   WSL2 kernel (no internet needed on remote PC)
   tools\VMware-player.exe    VMware Workstation Player installer

── ARRIVING AT A REMOTE PC ──────────────────────────────────────────────────

1. Plug in USB
2. Open PowerShell as Administrator
3. Run:  E:\install-archer.ps1        (replace E: with your USB drive letter)

   What it does automatically:
   - Enables WSL2 Windows features (reboots once if needed, then resumes)
   - Installs WSL2 kernel from USB (no internet needed)
   - Imports your dev environment into WSL2
   - Installs VMware Player from USB (silent, ~3 min)
   - Copies archer-os.vmdk to local drive for performance
   - Launches Archer OS VM in VMware
   - Drops you into WSL2

4. Work normally. Archer OS VM is running. Your repo is at ~/Archer in WSL.

── BEFORE YOU LEAVE ─────────────────────────────────────────────────────────

1. Open PowerShell as Administrator
2. Run:  E:\uninstall-archer.ps1

   What it does automatically:
   - Stops the Archer OS VM
   - Deletes the VMDK copy from the host PC
   - Uninstalls VMware Player completely
   - Exports your current WSL2 state back to this USB (saves all git commits,
     file changes, installed packages)
   - Unregisters and removes the WSL2 distro from Windows
   - Cleans up all leftover folders

   Result: zero trace of Archer left on the PC. Safe to unplug.

── USB CONTENTS ─────────────────────────────────────────────────────────────

  install-archer.ps1       Run on arrival (WSL + VMware + VM)
  uninstall-archer.ps1     Run before leaving (stop VM + uninstall + save)
  prepare-usb.ps1          Run once on home PC to populate USB
  export-archer.ps1        Alternative: exports WSL only (no VM)
  README.txt               This file

  distro\
    archer-dev.tar.gz      WSL2 environment (updated each session on uninstall)

  vm\
    archer-os.vmdk         Archer OS VM disk image (~1.5GB)

  tools\
    wsl_update_x64.msi     WSL2 kernel update (add manually — see prepare-usb.ps1)
    VMware-player.exe      VMware installer (add manually — see prepare-usb.ps1)

  WSL\                     Created during session, removed on uninstall
  (session VHDX lives here ON the USB, not on host PC)

── NOTES ────────────────────────────────────────────────────────────────────

- VM VMDK is copied to host SSD during session for performance
  (USB 2.0 is too slow to run the VM directly from USB)
- WSL2 VHDX lives ON the USB during the session
- git push/pull works normally from WSL — commits go to GitHub
- Build times in WSL on USB 2.0 are slow (~40-60 min vs ~11 min on SSD)
  For heavy builds, push changes and rebuild at home
- If export fails: distro\archer-dev.tar.gz.bak is the previous version
- VMware is fully uninstalled on each session — no licensing issues
