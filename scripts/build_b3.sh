#!/usr/bin/env bash
# Build the B3 stack: OpenSBI + Linux + BusyBox + DTB/initramfs.
# Run from the repository root (or anywhere: paths are resolved from this file).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

KVER="6.6.52"
KERNEL_URL="https://mirrors.aliyun.com/linux-kernel/v6.x/linux-${KVER}.tar.xz"
BB_VER="1.36.1"
BB_URL="https://busybox.net/downloads/busybox-${BB_VER}.tar.bz2"

mkdir -p third_party/dl

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing tool: $1"; exit 1; }; }
for t in riscv64-linux-gnu-gcc riscv64-unknown-elf-gcc dtc cpio make git; do need "$t"; done

# ---------------------------------------------------------------- OpenSBI
if [ ! -d third_party/opensbi ]; then
  git clone --depth 1 https://github.com/riscv-software-src/opensbi.git third_party/opensbi
fi
if ! grep -q "GPCP: single-hart prototype" third_party/opensbi/firmware/fw_base.S; then
  (cd third_party/opensbi && git apply ../../patches/opensbi-gpcp.patch)
fi
echo "[b3] building OpenSBI fw_jump"
make -C third_party/opensbi PLATFORM=generic CROSS_COMPILE=riscv64-linux-gnu- \
  FW_JUMP_ADDR=0x80200000 FW_JUMP_FDT_ADDR=0x81F00000 -j"$(nproc)"

# ---------------------------------------------------------------- Linux
if [ ! -d "third_party/linux-${KVER}" ]; then
  if [ ! -f third_party/dl/linux.tar.xz ]; then
    echo "[b3] downloading linux-${KVER}"
    curl -L --retry 3 -o third_party/dl/linux.tar.xz "$KERNEL_URL"
  fi
  xz -t third_party/dl/linux.tar.xz
  tar xJf third_party/dl/linux.tar.xz -C third_party
fi

cd "third_party/linux-${KVER}"
if [ ! -f arch/riscv/boot/Image ]; then
  echo "[b3] configuring and building the kernel"
  make ARCH=riscv CROSS_COMPILE=riscv64-linux-gnu- defconfig >/dev/null
  ./scripts/config \
    --disable SMP --disable MODULES --disable KALLSYMS --disable EFI \
    --disable DEBUG_KERNEL --disable FTRACE --disable FUNCTION_TRACER \
    --disable SECURITY_SELINUX --disable AUDIT --disable NET \
    --disable SOUND --disable DRM --disable MEDIA_SUPPORT --disable INPUT \
    --disable HID --disable USB_SUPPORT --disable SCSI --disable ATA \
    --disable MD --disable BLOCK --disable SWAP --disable CGROUPS \
    --disable NAMESPACES --enable CC_OPTIMIZE_FOR_SIZE --set-val HZ 100 \
    --enable BLK_DEV_INITRD --enable DEVTMPFS --enable DEVTMPFS_MOUNT \
    --disable STRICT_DEVMEM --enable SERIAL_8250 --enable SERIAL_8250_CONSOLE \
    --disable RANDOMIZE_BASE --disable DEBUG_INFO
  make ARCH=riscv CROSS_COMPILE=riscv64-linux-gnu- olddefconfig >/dev/null
  make -j"$(nproc)" ARCH=riscv CROSS_COMPILE=riscv64-linux-gnu- Image
fi
cd "$ROOT"

# ---------------------------------------------------------------- BusyBox
if [ ! -d "third_party/busybox-${BB_VER}" ]; then
  [ -f third_party/dl/busybox.tar.bz2 ] || curl -L --retry 3 -o third_party/dl/busybox.tar.bz2 "$BB_URL"
  tar xjf third_party/dl/busybox.tar.bz2 -C third_party
fi
if [ ! -x "third_party/busybox-${BB_VER}/busybox" ]; then
  echo "[b3] building busybox"
  make -C "third_party/busybox-${BB_VER}" ARCH=riscv CROSS_COMPILE=riscv64-linux-gnu- defconfig >/dev/null
  sed -i 's/# CONFIG_STATIC is not set/CONFIG_STATIC=y/' "third_party/busybox-${BB_VER}/.config"
  make -C "third_party/busybox-${BB_VER}" -j"$(nproc)" ARCH=riscv CROSS_COMPILE=riscv64-linux-gnu- >/dev/null
fi

# ---------------------------------------------------------------- DTB + initramfs
make -C sw/linux

echo
echo "[b3] done. Run the boot with:"
echo "  make -C sim/soc linux"
