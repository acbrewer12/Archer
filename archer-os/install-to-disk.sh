#!/bin/bash
# ═══════════════════════════════════════════════════════════════
#  ARCHER OS — install the running USB system onto an internal disk
#
#  Optional. The USB image is fully usable on its own; this exists for
#  when you want the head unit to boot from an internal NVMe/SSD instead
#  (faster boot, no stick sticking out of the dash, USB port freed up).
#
#  Run from the desktop (Settings -> Install to Disk) or directly:
#      sudo /opt/archer/install-to-disk.sh --list
#      sudo /opt/archer/install-to-disk.sh /dev/nvme0n1
#
#  THIS ERASES THE TARGET DISK. It refuses to touch the disk it is
#  running from, and it will not proceed without you typing the target
#  device name back.
# ═══════════════════════════════════════════════════════════════
set -euo pipefail

RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[1;33m'; CYN='\033[0;36m'; BLD='\033[1m'; NC='\033[0m'
say()  { echo -e "${BLD}[install]${NC} $1"; }
warn() { echo -e "  ${YLW}!${NC} $1"; }
die()  { echo -e "${RED}ERROR: $1${NC}" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "must run as root (use sudo)"

# ── Which disk are we RUNNING from? Never offer or touch it. ──────────
# findmnt gives the root filesystem's source partition; lsblk -no PKNAME
# walks up to its parent disk. Both are needed: excluding only the
# partition would still happily repartition the disk out from under the
# running system.
ROOT_SRC="$(findmnt -no SOURCE / 2>/dev/null || true)"
[ -n "$ROOT_SRC" ] || die "cannot determine the running root device"
ROOT_DISK=""
if [ -b "$ROOT_SRC" ]; then
    pk="$(lsblk -no PKNAME "$ROOT_SRC" 2>/dev/null | head -1 | tr -d ' ')"
    [ -n "$pk" ] && ROOT_DISK="/dev/$pk"
fi
# Fall back to the source itself if it is already a whole disk.
[ -n "$ROOT_DISK" ] || ROOT_DISK="$ROOT_SRC"

list_targets() {
    echo ""
    printf "  %-14s %-9s %-6s %s\n" "DEVICE" "SIZE" "TYPE" "MODEL"
    printf "  %-14s %-9s %-6s %s\n" "--------------" "---------" "------" "--------------------"
    local found=0
    # MODEL is read LAST and unquoted-remainder on purpose: it is the only
    # field that is routinely empty AND may contain spaces, so any other
    # position makes an empty model silently shift the following column's
    # value into it (ROTA showing up as the "model" was exactly that bug).
    while read -r name size rota model; do
        [ -n "$name" ] || continue
        # Not installable targets: zram is compressed RAM, loop is a file-backed
        # image, ram/sr/fd are RAM disks and optical/floppy. Offering any of
        # them as a destination is at best a failed install.
        case "$name" in zram*|loop*|ram*|sr*|fd*) continue;; esac
        local dev="/dev/$name"
        [ "$dev" = "$ROOT_DISK" ] && continue          # never the running disk
        local kind="SSD"; [ "$rota" = "1" ] && kind="HDD"
        case "$name" in nvme*) kind="NVMe";; esac
        printf "  %-14s %-9s %-6s %s\n" "$dev" "$size" "$kind" "${model:-unknown}"
        found=1
    done < <(lsblk -dno NAME,SIZE,ROTA,MODEL --sort NAME 2>/dev/null)
    [ "$found" = "1" ] || warn "no installable disks found (only the running device is present)"
    echo ""
}

if [ "${1:-}" = "--list" ] || [ $# -eq 0 ]; then
    say "Running from: ${CYN}${ROOT_DISK}${NC} (excluded from the list below)"
    list_targets
    echo "  Install with:  sudo $0 /dev/<device>"
    exit 0
fi

TARGET="$1"
[ -b "$TARGET" ] || die "$TARGET is not a block device"
[ "$TARGET" = "$ROOT_DISK" ] && die "$TARGET is the disk Archer is running from — refusing"
case "$(basename "$TARGET")" in
    zram*|loop*|ram*|sr*|fd*)
        die "$TARGET is not an installable disk (RAM disk, loop device, or optical drive)" ;;
esac

# Reject a partition passed where a whole disk is expected: partitioning
# /dev/nvme0n1p2 would silently destroy its parent's layout.
if [ -n "$(lsblk -no PKNAME "$TARGET" 2>/dev/null | tr -d ' ')" ]; then
    die "$TARGET looks like a partition — pass the whole disk (e.g. /dev/nvme0n1)"
fi

SIZE="$(lsblk -dno SIZE "$TARGET" | tr -d ' ')"
MODEL="$(lsblk -dno MODEL "$TARGET" | sed 's/ *$//')"

echo ""
echo -e "  ${RED}${BLD}════ THIS ERASES EVERYTHING ON THE TARGET ════${NC}"
echo -e "  Target : ${BLD}${TARGET}${NC}  ${SIZE}  ${MODEL:-unknown}"
echo -e "  Source : ${ROOT_DISK} (the system you are running now)"
echo ""
lsblk "$TARGET" 2>/dev/null | sed 's/^/    /' || true
echo ""
read -r -p "  Type the device name to confirm ($TARGET): " CONFIRM
[ "$CONFIRM" = "$TARGET" ] || die "confirmation did not match — nothing was changed"

# ── Partition: same layout the image itself uses ─────────────────────
say "Partitioning $TARGET (GPT: bios_boot + EFI + root)..."
umount "${TARGET}"* 2>/dev/null || true
wipefs -a "$TARGET" >/dev/null 2>&1 || true
parted -s "$TARGET" \
    mklabel gpt \
    mkpart bios_boot 1MiB 2MiB  set 1 bios_grub on \
    mkpart EFI fat32 2MiB 514MiB set 2 esp on \
    mkpart root ext4 514MiB 100%
partprobe "$TARGET" 2>/dev/null || true
sleep 1

# NVMe partitions are p1/p2/p3; SATA are 1/2/3.
case "$TARGET" in *nvme*|*mmcblk*) P="${TARGET}p";; *) P="${TARGET}";; esac
EFI_PART="${P}2"; ROOT_PART="${P}3"

say "Formatting..."
mkfs.fat -F32 -n ARCHER_EFI "$EFI_PART" >/dev/null
# Deliberately NOT labelled ARCHER_OS: the USB stick already carries that
# label, and two filesystems with the same label make GRUB's
# `search --label` ambiguous — it could pick either, so the machine might
# silently keep booting the stick after a "successful" install. The
# installed system is addressed by UUID instead (set below).
mkfs.ext4 -L ARCHER_DISK "$ROOT_PART" -q

MNT="$(mktemp -d)"
cleanup() {
    for m in dev/pts dev proc sys boot/efi ""; do
        umount -R "$MNT/$m" 2>/dev/null || umount "$MNT/$m" 2>/dev/null || true
    done
    rmdir "$MNT" 2>/dev/null || true
}
trap cleanup EXIT

mount "$ROOT_PART" "$MNT"
mkdir -p "$MNT/boot/efi"
mount "$EFI_PART" "$MNT/boot/efi"

# ── Copy the running system ──────────────────────────────────────────
say "Copying system to disk (a few minutes)..."
# Virtual filesystems and runtime dirs are recreated at boot, not copied.
# /boot/efi is excluded because it is a separate mounted filesystem here.
tar --one-file-system -C / -cf - \
    --exclude=./proc/* --exclude=./sys/* --exclude=./dev/* \
    --exclude=./run/* --exclude=./tmp/* --exclude=./mnt/* --exclude=./media/* \
    . 2>/dev/null | tar -C "$MNT" -xf - 2>/dev/null || true
mkdir -p "$MNT"/{proc,sys,dev,run,tmp,mnt,media}
chmod 1777 "$MNT/tmp"

# ── Point the installed system at ITSELF, not the stick ──────────────
ROOT_UUID="$(blkid -s UUID -o value "$ROOT_PART")"
EFI_UUID="$(blkid -s UUID -o value "$EFI_PART")"
say "Installed root UUID: $ROOT_UUID"

cat > "$MNT/etc/fstab" <<FSTAB
UUID=$ROOT_UUID  /          ext4  errors=remount-ro,noatime  0 1
UUID=$EFI_UUID   /boot/efi  vfat  umask=0077                 0 2
FSTAB

# Rewrite the hand-written GRUB entry to boot the installed root by UUID.
# Leaving it as LABEL=ARCHER_OS would send the installed system looking for
# the USB stick, so it would only boot while the stick was still plugged in.
if [ -f "$MNT/etc/grub.d/40_archer" ]; then
    sed -i "s|root=LABEL=ARCHER_OS|root=UUID=$ROOT_UUID|g; \
            s|search --no-floppy --label --set=root ARCHER_OS|search --no-floppy --fs-uuid --set=root $ROOT_UUID|g" \
        "$MNT/etc/grub.d/40_archer"
fi

# ── Bootloader ───────────────────────────────────────────────────────
say "Installing GRUB (UEFI + legacy BIOS)..."
mount --bind /dev  "$MNT/dev"
mount --bind /proc "$MNT/proc"
mount --bind /sys  "$MNT/sys"
mount --bind /dev/pts "$MNT/dev/pts" 2>/dev/null || true

chroot "$MNT" grub-install --target=x86_64-efi --efi-directory=/boot/efi \
       --bootloader-id=ARCHER --removable --no-nvram >/dev/null 2>&1 \
  || warn "UEFI GRUB install reported errors (legacy BIOS may still boot)"
chroot "$MNT" grub-install --target=i386-pc "$TARGET" >/dev/null 2>&1 \
  || warn "legacy BIOS GRUB install reported errors (UEFI may still boot)"
chroot "$MNT" update-grub >/dev/null 2>&1 || warn "update-grub reported errors"

sync
echo ""
echo -e "  ${GRN}${BLD}✓ Archer OS installed to ${TARGET}${NC}"
echo ""
echo "  Next:"
echo "    1. Shut down"
echo "    2. Remove the USB stick"
echo "    3. Power on — the head unit now boots from the internal disk"
echo ""
echo "  Your PIN and settings were copied across. The USB stick still works"
echo "  as a recovery system if you ever need it."
echo ""
