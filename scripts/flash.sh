#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
JLINK=/Applications/SEGGER/JLink_V986a/JLinkExe
SERIAL="${1:-1050072057}"
case "${2:-usb}" in
  usb) HEX=build/firmware/zephyr/zephyr.hex ;;
  mic) HEX=build-mic/mic-firmware/zephyr/zephyr.hex ;;
  app) HEX=build-app/app-firmware/zephyr/zephyr.hex ;;
  *) echo 'Usage: bash scripts/flash.sh [serial] [usb|mic|app]' >&2; exit 2 ;;
esac
test -f "$HEX"
mkdir -p artifacts
cat > artifacts/reflash.jlink <<JLINK_COMMANDS
loadfile $HEX
r
g
exit
JLINK_COMMANDS
"$JLINK" -device NRF5340_XXAA_APP -if SWD -speed 1000 -USB "$SERIAL" -autoconnect 1 -ExitOnError 1 -CommandFile artifacts/reflash.jlink
