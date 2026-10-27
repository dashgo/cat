#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
case "${1:-usb}" in
  usb) APP=firmware; BUILD=build; VERSION=CAT-USB-Speaker ;;
  mic) APP=mic-firmware; BUILD=build-mic; VERSION=CAT-Mic-Monitor ;;
  app) APP=app-firmware; BUILD=build-app; VERSION=CAT-App-Audio ;;
  *) echo 'Usage: bash scripts/build.sh [usb|mic|app]' >&2; exit 2 ;;
esac
TC=/opt/nordic/ncs/toolchains/b8efef2ad5
export PATH="$TC/bin:$TC/usr/bin:$TC/usr/local/bin:$PATH"
export ZEPHYR_BASE=/opt/nordic/ncs/v2.9.2/zephyr
export ZEPHYR_TOOLCHAIN_VARIANT=zephyr
export ZEPHYR_SDK_INSTALL_DIR="$TC/opt/zephyr-sdk"
cd /opt/nordic/ncs/v2.9.2
west build -p auto -b nrf5340dk/nrf5340/cpuapp -d "$ROOT/$BUILD" "$ROOT/$APP" -- -DBUILD_VERSION="$VERSION"
