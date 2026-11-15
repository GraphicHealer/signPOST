#!/usr/bin/env bash
# Local signPOST firmware build — mirrors .github/workflows/build-uf2.yml.
# Usage: ./ci/build.sh [build_root]   (build_root defaults to ./build)
set -euo pipefail

BOARD=signpost_i75w_rp2350
MICROPYTHON_VERSION=${MICROPYTHON_VERSION:-master}
PIMORONI_PICO_VERSION=${PIMORONI_PICO_VERSION:-feature/picovector2-and-layers}
PY_DECL_VERSION=v0.0.3
DIR2UF2_VERSION=v0.0.9

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_ROOT="$(realpath "${1:-$PROJECT_ROOT/build}")"
mkdir -p "$BUILD_ROOT/tools" && cd "$BUILD_ROOT"

[ -d micropython ] || git clone https://github.com/micropython/micropython -b "$MICROPYTHON_VERSION" micropython
(cd micropython && git submodule update --init lib/pico-sdk lib/tinyusb lib/btstack lib/cyw43-driver lib/lwip lib/mbedtls lib/micropython-lib)

[ -d pimoroni-pico ] || git clone https://github.com/pimoroni/pimoroni-pico -b "$PIMORONI_PICO_VERSION" pimoroni-pico
(cd pimoroni-pico && git submodule update --init)

[ -d tools/py_decl ] || git clone https://github.com/gadgetoid/py_decl -b "$PY_DECL_VERSION" tools/py_decl
[ -d tools/dir2uf2 ] || git clone https://github.com/gadgetoid/dir2uf2 -b "$DIR2UF2_VERSION" tools/dir2uf2
python3 -m pip install --user littlefs-python==0.12.0 || true

make -C micropython/mpy-cross -j"$(nproc)"

cmake -S micropython/ports/rp2 -B "build-$BOARD" \
    -DPICOTOOL_FORCE_FETCH_FROM_GIT=1 \
    -DPICO_BUILD_DOCS=0 \
    -DPICO_NO_COPRO_DIS=1 \
    -DPICOTOOL_FETCH_FROM_GIT_PATH="$BUILD_ROOT/tools/picotool" \
    -DPIMORONI_PICO_PATH="$BUILD_ROOT/pimoroni-pico" \
    -DPIMORONI_TOOLS_DIR="$BUILD_ROOT/tools" \
    -DUSER_C_MODULES="$PROJECT_ROOT/boards/$BOARD/usermodules.cmake" \
    -DMICROPY_BOARD_DIR="$PROJECT_ROOT/boards/$BOARD" \
    -DMICROPY_BOARD="$BOARD"

cmake --build "build-$BOARD" -j"$(nproc)"

echo ""
echo "Done:"
echo "  $BUILD_ROOT/build-$BOARD/firmware.uf2                   (firmware only)"
echo "  $BUILD_ROOT/build-$BOARD/firmware-with-filesystem.uf2   (firmware + signPOST app)"
