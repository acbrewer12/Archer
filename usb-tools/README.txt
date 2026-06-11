ARCHER DEV USB — PORTABLE WSL2 ENVIRONMENT
==========================================

This USB carries your full Archer development environment.
Plug it into any Windows PC and run the scripts below.

── FIRST TIME (home PC only, once) ──────────────────────────────────────────

1. Open PowerShell as Administrator
2. Run:  .\export-archer.ps1
   - Exports your home PC's WSL2 Ubuntu to  distro\archer-dev.tar.gz
3. Download the WSL2 kernel MSI (needed for PCs without internet):
     https://wslstorestorage.blob.core.windows.net/wslblob/wsl_update_x64.msi
   Place it in:  tools\wsl_update_x64.msi

── ARRIVING AT A REMOTE PC ──────────────────────────────────────────────────

1. Plug in USB
2. Open PowerShell as Administrator
3. Run:  E:\install-archer.ps1        (replace E: with your USB drive letter)
   - Enables WSL2 on the PC (reboots once if needed, then auto-continues)
   - Imports your dev environment
   - Drops you into WSL

4. Work normally. Your Archer repo is at ~/Archer inside WSL.

── BEFORE YOU LEAVE ─────────────────────────────────────────────────────────

1. Open PowerShell as Administrator
2. Run:  E:\uninstall-archer.ps1
   - Exports your current state (git commits, changes) back to USB
   - Removes all traces from the PC
   - Safe to unplug after it completes

── USB CONTENTS ─────────────────────────────────────────────────────────────

  install-archer.ps1       Run on arrival
  uninstall-archer.ps1     Run before leaving
  export-archer.ps1        Run once on home PC to populate USB
  README.txt               This file
  distro\
    archer-dev.tar.gz      Your WSL2 environment (updated each session)
  tools\
    wsl_update_x64.msi     WSL2 kernel (place here manually, see above)
  WSL\
    ArcherDev\             Created during session, removed on uninstall

── NOTES ────────────────────────────────────────────────────────────────────

- The WSL2 data lives ON this USB during sessions (not on the host PC)
- git push/pull works normally — commits go to GitHub as usual
- Build times are slower on USB 2.0 (~40-60 min vs ~11 min on SSD)
  For heavy builds, push your changes and rebuild at home
- If something goes wrong:  distro\archer-dev.tar.gz.bak is the previous version
