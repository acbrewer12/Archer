#!/bin/bash
# archer-os/kernel/build-kernel.sh
# Downloads, configures, and compiles the Archer custom Linux kernel.
# Called from build-vm.sh; installs kernel + modules into the image root.
#
# Usage: sudo bash build-kernel.sh <install-root>
#   install-root: image mount point (e.g. /tmp/archer-build/mnt)

set -e

INSTALL_ROOT="${1:-}"
[ -n "$INSTALL_ROOT" ] || { echo "Usage: $0 <install-root>"; exit 1; }
[ -d "$INSTALL_ROOT" ] || { echo "ERROR: install-root '$INSTALL_ROOT' not found"; exit 1; }

KERNEL_SERIES="6.12"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUILD_DIR="/tmp/archer-kernel"
JOBS=$(nproc)

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BOLD='\033[1m'; NC='\033[0m'
log()  { echo -e "${BOLD}[KERNEL]${NC} $1"; }
ok()   { echo -e "  ${GREEN}✓${NC} $1"; }
warn() { echo -e "  ${YELLOW}!${NC} $1"; }
die()  { echo -e "${RED}ERROR: $1${NC}"; exit 1; }

# ── Resolve latest 6.12.x from kernel.org ────────────────────────────────────
log "Resolving latest Linux ${KERNEL_SERIES}.x..."
KERNEL_VERSION=$(curl -fsSL --max-time 15 "https://www.kernel.org/releases.json" \
    | python3 -c "
import sys, json
releases = json.load(sys.stdin)['releases']
v = [r['version'] for r in releases if r['version'].startswith('${KERNEL_SERIES}.')]
print(sorted(v, key=lambda s: list(map(int, s.split('.'))))[-1])
" 2>/dev/null) || KERNEL_VERSION="${KERNEL_SERIES}.0"

ok "Kernel version: ${KERNEL_VERSION}"

KERNEL_MAJ="${KERNEL_VERSION%%.*}"
KERNEL_TAR="linux-${KERNEL_VERSION}.tar.xz"
KERNEL_URL="https://cdn.kernel.org/pub/linux/kernel/v${KERNEL_MAJ}.x/${KERNEL_TAR}"
KERNEL_DIR="${BUILD_DIR}/linux-${KERNEL_VERSION}"

# ── Install build dependencies ────────────────────────────────────────────────
log "Installing kernel build dependencies..."
apt-get install -y -qq \
    build-essential bc kmod cpio flex bison \
    libssl-dev libelf-dev libncurses-dev \
    dwarves pahole python3 rsync xz-utils
ok "Build deps installed"

mkdir -p "$BUILD_DIR"

# ── Download source ───────────────────────────────────────────────────────────
if [ ! -f "${BUILD_DIR}/${KERNEL_TAR}" ]; then
    log "Downloading linux-${KERNEL_VERSION}.tar.xz (~130MB)..."
    curl -fsSL --progress-bar -o "${BUILD_DIR}/${KERNEL_TAR}" "$KERNEL_URL"
    ok "Downloaded"
else
    ok "Source archive already cached"
fi

# ── Extract ───────────────────────────────────────────────────────────────────
if [ ! -d "$KERNEL_DIR" ]; then
    log "Extracting source..."
    tar -xf "${BUILD_DIR}/${KERNEL_TAR}" -C "$BUILD_DIR"
    ok "Extracted to $KERNEL_DIR"
else
    ok "Source already extracted"
fi

cd "$KERNEL_DIR"

# ── Configure ─────────────────────────────────────────────────────────────────
log "Configuring kernel (base: x86_64_defconfig + archer overrides)..."

# Start from the minimal x86_64 base config
make ARCH=x86_64 x86_64_defconfig >/dev/null 2>&1

# Merge our config fragment on top — our settings override the base
ARCH=x86_64 scripts/kconfig/merge_config.sh \
    -m .config "$SCRIPT_DIR/archer.config" >/dev/null 2>&1

# Resolve any dependency conflicts (sets unresolved options to their defaults)
make ARCH=x86_64 olddefconfig >/dev/null 2>&1

ok "Configured: $(grep -c '=y' .config) built-in, $(grep -c '=m' .config) modules"

# ── Compile ───────────────────────────────────────────────────────────────────
log "Compiling kernel with ${JOBS} jobs (this takes ~15-25 min in CI)..."
START_TIME=$(date +%s)

make ARCH=x86_64 -j"$JOBS" bzImage modules \
    2>&1 | grep -E '(CC|LD|AR|LINK|error:|warning:|ERROR)' | tail -5 || true

# Verify the image was actually produced
[ -f "arch/x86/boot/bzImage" ] || die "bzImage not found — compilation failed"

ELAPSED=$(( $(date +%s) - START_TIME ))
ok "Compiled in $((ELAPSED/60))m $((ELAPSED%60))s"

# ── Install ───────────────────────────────────────────────────────────────────
KERNEL_RELEASE="${KERNEL_VERSION}-archer"

log "Installing modules to ${INSTALL_ROOT}..."
make ARCH=x86_64 INSTALL_MOD_PATH="$INSTALL_ROOT" modules_install >/dev/null 2>&1
ok "Modules installed to ${INSTALL_ROOT}/lib/modules/${KERNEL_RELEASE}/"

log "Installing kernel image..."
BOOT="${INSTALL_ROOT}/boot"
mkdir -p "$BOOT"
cp "arch/x86/boot/bzImage"  "${BOOT}/vmlinuz-${KERNEL_RELEASE}"
cp "System.map"              "${BOOT}/System.map-${KERNEL_RELEASE}"
cp ".config"                 "${BOOT}/config-${KERNEL_RELEASE}"

VMLINUZ_SIZE=$(( $(stat -c%s "${BOOT}/vmlinuz-${KERNEL_RELEASE}") / 1024 ))
ok "vmlinuz-${KERNEL_RELEASE} installed (${VMLINUZ_SIZE} KB)"

echo ""
echo -e "  ${GREEN}${BOLD}Kernel build complete: ${KERNEL_RELEASE}${NC}"
echo ""
