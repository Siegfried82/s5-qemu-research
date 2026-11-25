#!/bin/bash
set -euo pipefail
TOOLCHAIN_DIR="$(cd "$(dirname "$0")" && pwd)"
QEMU_SOURCE="$TOOLCHAIN_DIR/qemu-9.2.4"
export PATH="$TOOLCHAIN_DIR/deps/bin:$PATH"
export PKG_CONFIG_PATH="$TOOLCHAIN_DIR/deps/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
export PKG_CONFIG="$TOOLCHAIN_DIR/bin/pkg-config-utf8"

test -x "$TOOLCHAIN_DIR/deps/bin/ninja"
test -f "$QEMU_SOURCE/configure"
python3 "$TOOLCHAIN_DIR/../qemu/generate_board_setup.py"
python3 "$TOOLCHAIN_DIR/../qemu/apply_qemu_patches.py" "$QEMU_SOURCE"
cp "$TOOLCHAIN_DIR/../qemu/s5.c" "$QEMU_SOURCE/hw/arm/s5.c"
cp "$TOOLCHAIN_DIR/../qemu/s5-rtos.c" "$QEMU_SOURCE/hw/arm/s5-rtos.c"
cp "$TOOLCHAIN_DIR/../qemu/s5-rtc-peer.c" "$QEMU_SOURCE/hw/arm/s5-rtc-peer.c"
cp "$TOOLCHAIN_DIR/../qemu/board-setup.h" "$QEMU_SOURCE/hw/arm/board-setup.h"
python3 - "$QEMU_SOURCE/hw/arm/meson.build" <<'PY'
import pathlib, sys
path = pathlib.Path(sys.argv[1])
text = path.read_text()
for source in ('s5.c', 's5-rtos.c', 's5-rtc-peer.c'):
    line = "arm_ss.add(when: 'CONFIG_VEXPRESS', if_true: files('%s'))\n" % source
    if line not in text:
        text += '\n' + line
path.write_text(text)
PY

mkdir -p "$TOOLCHAIN_DIR/build"
cd "$TOOLCHAIN_DIR/build"
if [[ ! -f build.ninja || "${1:-}" == "--configure" ]]; then
    "$QEMU_SOURCE/configure" \
        --target-list=arm-softmmu \
        --without-default-features \
        --enable-tcg --enable-system --enable-fdt=internal \
        --disable-user --disable-werror --disable-download \
        --disable-docs --disable-tools --disable-guest-agent
fi
ninja -j "${S5_BUILD_JOBS:-8}" qemu-system-arm
./qemu-system-arm --version
