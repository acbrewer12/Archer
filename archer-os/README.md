# Archer OS — custom Linux build for the 2006 GMC Sierra 2500HD

A hand-built Debian (bookworm) image that boots straight into Archer's
kiosk dashboard on the truck's head unit — no login manager, no desktop
environment, no systemd. Everything here exists because the normal distro
assumptions (systemd, a display manager, NSS, a GPU driver) don't hold on
this hardware or don't serve a ~3-second-to-login target. See `CLAUDE.md`
§10 in the repo root for the specific traps this creates when touching the
image later — this doc is the "how do I actually build and boot it"
companion to that, not a replacement for it.

## What makes this not a normal Linux box

- **`init/archer_init.c` is PID 1.** No systemd, no logind, no D-Bus
  session bus, no polkit. It mounts `/proc /sys /dev /tmp`, brings up
  loopback, forks NetworkManager/wpa_supplicant, bluetoothd (+ the
  OBDLink MX+ rfcomm bind), and the Archer Flask app directly, then reaps
  zombies and handles `SIGUSR1`/`SIGUSR2` (reboot/power-off) in its main
  loop. Read the doc comment at the top of `init/archer_init.c` before
  changing anything there — it explains why each step exists.
- **It's statically linked** (`gcc -static -Os`, see `init/Makefile`) —
  `/usr/lib` may not be mounted yet when PID 1 starts, so it can't depend
  on shared libraries. This also means no NSS: no `initgroups`, no
  `getpwnam`. Groups are parsed out of `/etc/group` by hand elsewhere in
  the codebase.
- **No GPU driver.** simpledrm is mode-setting only, so Chromium renders
  through SwiftShader — first paint takes several seconds. That's why
  `console-login.sh` asks for the PIN on tty1 (via `agetty`, about a
  second into userspace) *before* X/openbox/Chromium ever start, instead
  of putting the login screen in the dashboard itself. First boot (no PIN
  set yet) skips straight to the graphical `/setup` wizard instead.
- **Xorg is not setuid.** The session starts unprivileged via
  `Xorg.wrap`, with a sudo fallback.

## Layout

```
build.sh              Full image builder — production/USB target
build-vm.sh           Lightweight image builder — VM testing target
                       (desktop/session code is identical between the two;
                       only sizing and VM-debug conveniences differ — see
                       "Two builders" below)
init/
  archer_init.c        PID 1 — see above
  Makefile             Builds it statically; `make check` confirms no
                        shared-lib dependencies
kernel/
  build-kernel.sh       Downloads/configures/compiles the custom kernel
                        (CAN bus, 1000Hz timer, universal drivers) —
                        called from the builders, not run standalone
  archer.config          Kernel config
config/
  grub.cfg              The GRUB entry template — see "Boot / GRUB" below
overlay/
  etc/systemd/system/    Copied into the image for reference; not
                          executed by anything (there is no systemd here)
obd-auth/
  keygen.sh              Generates the shared HMAC key for OBD2 port auth
  obd_bt_pair.sh          Pairs the OBDLink MX+ over Bluetooth (one-time)
  obd_bt_bind.sh          Binds the paired device to /dev/rfcomm0 at boot
  obd_auth_client.py      Reference client for the handshake
arduino/
  aux_battery_monitor.ino  Runs on an Arduino Uno wired to the aux
                            battery; reports voltage over USB serial,
                            picked up by archer.py's _arduino_reader()
console-login.sh        Runs on tty1 before X starts — see above
settings.sh              System settings menu (WiFi, disk install, PIN
                          reset, logs, power) — launched from the desktop
                          or /opt/archer/settings.sh
install-to-disk.sh       Optional: copies the running USB system onto an
                          internal NVMe/SSD
```

## Two builders — keep them in step

`build.sh` (USB/production, ~7GB image, 15GB free disk + 4GB RAM to build)
and `build-vm.sh` (VM testing, ~4GB image, 8GB free disk + 2GB RAM to
build) share the same desktop/session code by design. **If you change one,
change the other, then diff them with comments stripped to prove they're
still in step** — this is called out explicitly in both files' headers,
because it's easy to fix a bug in one and forget the other exists.

Both:
```
sudo bash build.sh       # or build-vm.sh
```
run on Ubuntu 22.04+, need root, and print a live step counter with a
progress bar and elapsed time. Both run pre-flight checks first (disk
space, RAM, required commands — `debootstrap`, `parted`, `losetup`,
`mkfs.fat`/`mkfs.ext4`, `grub-install`, etc.) and refuse to proceed if
they're not met.

**The image is populated by `git archive HEAD`.** Uncommitted files in
the working tree do not reach it — commit first. Both scripts currently
clone `ARCHER_REPO`/`ARCHER_BRANCH` (hardcoded near the top of each file);
update that once the feature branch merges to `main`, or the image will
keep shipping an old branch.

The real difference between the two is sizing (`IMG_SIZE_MB`, the
pre-flight thresholds) and one VM-only debug convenience: `build-vm.sh`
sets a root password (`archer`/`archer`, via `chpasswd`) for console
debugging inside the VM, which `build.sh` does not. (Passwordless sudo
for the `archer` user is in *both* — production needs it too, for
`nmtui`, `install-to-disk.sh`, and `settings.sh` to work at all.)

Roughly, both go through: install build tools → create and partition the
image → bootstrap Debian → build PortAudio from source without PulseAudio
→ set hostname/locale/networking → create the `archer` user, clone the
repo, install Python deps → set up the X11/Chromium kiosk pipeline →
compile `archer_init` → build the custom kernel → generate initramfs for
both kernels with dracut → install the archer service files → configure
GRUB → write the MOTD → compress with gzip.

## Testing in a VM

Build with `build-vm.sh`, then boot `archer-os.img` in VirtualBox/VMware
(raw disk image, not an ISO). The root password (`archer`) and
passwordless sudo are there specifically so you can debug from a VM
console without needing a working kiosk session first.

## Flashing / installing

1. Build with `build.sh`.
2. Flash `archer-os.img` to an 8GB+ USB stick with Balena Etcher (or
   equivalent `dd`).
3. Boot the head unit from it.
4. **Optional:** from the desktop, Settings → "Install to internal disk",
   or directly: `sudo /opt/archer/install-to-disk.sh --list` then
   `sudo /opt/archer/install-to-disk.sh /dev/nvme0n1`. This copies the
   running USB system onto an internal NVMe/SSD for a faster boot and a
   freed-up USB port — the stick works fine on its own, this is purely an
   upgrade. It refuses to touch the disk it's currently running from, and
   won't proceed without you typing the target device name back.

## First boot / normal boot

- **First boot** (no PIN configured yet): `console-login.sh` exits
  immediately and the graphical `/setup` wizard at `/setup` handles PIN
  creation — worth the slower graphical UI since it only happens once.
- **Every boot after that:** `console-login.sh` asks for the PIN on tty1
  before the graphical stack even starts, verified via `/opt/archer/pin.py`
  — the same module and credential file (scrypt-hashed) the web login
  uses, so there's exactly one implementation of the hashing (see
  `CLAUDE.md` §9). This is what makes a ~3s time-to-login possible despite
  Chromium's slow first paint.

## Runtime operations

There is no `systemctl`. `settings.sh` (desktop menu, or
`/opt/archer/settings.sh` directly) covers:

| Menu item | What it does |
|---|---|
| WiFi / network | `sudo nmtui` — must run as root; there's no polkit here to authorise a non-root NetworkManager request |
| Install to internal disk | Wraps `install-to-disk.sh` |
| Reset login PIN | Deletes `/etc/archer/owner.json`; next dashboard load runs first-time setup again |
| View logs | Backend (`/run/archer.log`), boot (`/run/archer_init.log`), or full kernel log (`dmesg`) |
| Power | `sudo kill -USR1 1` (reboot) / `sudo kill -USR2 1` (power off) — `archer_init`'s own signal protocol, PID 1 has no other way to be told to shut down |

## Boot / GRUB

The GRUB entry is hand-written (`config/grub.cfg` → installed as
`/etc/grub.d/40_archer`), not auto-generated, for two reasons:

- **A GRUB superuser password is required.** Without one, anyone with
  USB/physical access to the head unit can hit `e` or `c` at the GRUB
  prompt, append `init=/bin/sh` to the kernel cmdline, and get a root
  shell that bypasses `archer_init.c` and every auth check it does. The
  password is a real PBKDF2 hash from `grub-mkpasswd-pbkdf2`, never the
  plaintext. The Archer entry is marked `--unrestricted` so it still boots
  without the password being typed at every startup, while edit/command
  mode still requires it.
- **`40_archer`'s entry does not inherit `GRUB_CMDLINE_LINUX_DEFAULT`.**
  Because it's hand-written rather than generated by `grub-mkconfig`, the
  kernel cmdline (including `init=/sbin/archer_init`) has to be written
  directly into this entry — setting `GRUB_CMDLINE_LINUX_DEFAULT` in
  `/etc/default/grub` alone would silently not apply here, even though it
  does apply to the auto-generated `10_linux` entries alongside it.

## OBD2 authentication

`obd-auth/keygen.sh` generates the shared HMAC-SHA256 key once, on a
trusted machine — it gets embedded into the USB image at build time, and
a copy needs to reach the Pi separately (`scp obd_auth.key
pi@<pi-ip>:/etc/archer/obd_auth.key`). Don't regenerate it without
re-flashing the Pi too; the key is gitignored, never committed.
`obd_bt_pair.sh` and `obd_bt_bind.sh` handle the actual Bluetooth pairing
and binding of the paired OBDLink MX+ to `/dev/rfcomm0` at boot.

## Custom kernel

`kernel/build-kernel.sh` (called from both builders, not meant to be run
standalone) downloads and compiles a 6.12-series kernel with CAN bus
support, a 1000Hz timer, and broad driver coverage, then installs it plus
modules into the image root. `kernel/archer.config` is the actual config
used.

## Aux battery monitor (optional peripheral)

`arduino/aux_battery_monitor.ino`, flashed to an Arduino Uno wired to the
truck's second battery via a resistor-divider into `A1`, reports voltage
over USB serial every 2 seconds (`AUX_BATT:<voltage>`). `archer.py`'s
`_arduino_reader()` picks this up into `truck_state`. Not required for
the OS to build or boot — it's a hardware add-on some builds will have and
some won't.

## Before touching the image

Read `CLAUDE.md` §10 in the repo root first. It documents the traps that
have actually bitten this project: `pgrep -f`/`pkill -f` self-matching
their own shell, the pre-existing `/etc/archer/master.key` that must never
be deleted, and the standing rule that **nothing here is verified until
the image is actually rebuilt and booted** — reading a build script proves
nothing about the system it produces.
