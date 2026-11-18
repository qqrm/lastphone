#!/usr/bin/env bash
# Run the explicit x86_64/4 KB test target and retain device/GPU evidence.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
REPORTS="$ROOT/mobile/android/app/build/reports/emulator"
mkdir -p "$REPORTS"

collect_logs() {
    local status=$?
    adb logcat -d -v threadtime > "$REPORTS/logcat.txt" 2>&1 || true
    exit "$status"
}
trap collect_logs EXIT

adb wait-for-device
adb shell getprop > "$REPORTS/device-properties.txt"
adb shell cat /proc/meminfo > "$REPORTS/memory.txt"
PAGE_SIZE="$(adb shell getconf PAGE_SIZE | tr -d '\r')"
ABI="$(adb shell getprop ro.product.cpu.abi | tr -d '\r')"
printf 'ABI=%s\nPAGE_SIZE=%s\n' "$ABI" "$PAGE_SIZE" | tee "$REPORTS/test-target.txt"
if [[ "$ABI" != x86_64 || "$PAGE_SIZE" != 4096 ]]; then
    echo "This test variant requires the standard x86_64, 4 KB emulator image." >&2
    exit 1
fi

cd "$ROOT/mobile/android"
./gradlew --no-daemon --max-workers=2 connectedEmulatorDebugAndroidTest
