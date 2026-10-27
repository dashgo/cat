#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
DEVICE="${1:-7218E20A-29AC-4C9F-BB06-2734570AE3A6}"
xcrun simctl boot "$DEVICE" 2>/dev/null || true
xcrun simctl bootstatus "$DEVICE" -b
xcodebuild -project ios/CatTalk.xcodeproj -scheme CatTalk -configuration Debug \
  -sdk iphonesimulator -destination "id=$DEVICE" -derivedDataPath ios/build \
  CODE_SIGNING_ALLOWED=NO build
xcrun simctl terminate "$DEVICE" com.chris.cattalk 2>/dev/null || true
xcrun simctl install "$DEVICE" ios/build/Build/Products/Debug-iphonesimulator/CatTalk.app
xcrun simctl launch "$DEVICE" com.chris.cattalk
